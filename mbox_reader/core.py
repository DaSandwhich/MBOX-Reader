"""Shared parsing, indexing, and database helpers for the MBOX reader."""
import email
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sqlite3, threading, queue, re, os, hashlib, time, json, tempfile, webbrowser, html, shutil

PAGE_SIZE = 250

def conversation_subject(subject):
    # Fallback grouping for archives without usable Message-ID/References.
    value = decoded(subject).strip()
    while True:
        cleaned = re.sub(r'^\s*(?:re|fw|fwd)\s*:\s*', '', value, flags=re.I)
        if cleaned == value:
            break
        value = cleaned
    return re.sub(r'\s+', ' ', value).casefold() or '(no subject)'


def library_file():
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'OfflineMboxReader'
    base.mkdir(parents=True, exist_ok=True)
    return base / 'archives.json'

def load_library():
    try:
        data=json.loads(library_file().read_text(encoding='utf-8'))
        return [x for x in data if isinstance(x,str)] if isinstance(data,list) else []
    except (OSError, ValueError):
        return []

def save_library(paths):
    library_file().write_text(json.dumps(paths,ensure_ascii=False,indent=2),encoding='utf-8')


def decoded(v):
    try: return str(make_header(decode_header(str(v or ''))))
    except Exception: return str(v or '')

def date_string(v):
    try: return parsedate_to_datetime(v).strftime('%Y-%m-%d %H:%M')
    except Exception: return decoded(v)

class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts=[]; self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style','head'): self.skip+=1
        elif tag in ('p','div','br','li','tr','h1','h2','blockquote'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style','head') and self.skip: self.skip-=1
        elif tag in ('p','div','li','tr','h1','h2','blockquote'): self.parts.append('\n')
    def handle_data(self, data):
        if not self.skip: self.parts.append(data)

def part_text(p):
    try:
        v=p.get_content()
        if isinstance(v,str): return v
    except Exception: pass
    raw=p.get_payload(decode=True) or b''
    try: return raw.decode(p.get_content_charset() or 'utf-8', errors='replace')
    except LookupError: return raw.decode('utf-8',errors='replace')

def body_text(msg):
    plain=[]; html=[]
    for p in msg.walk():
        if p.is_multipart() or p.get_content_disposition()=='attachment': continue
        if p.get_content_type()=='text/plain': plain.append(part_text(p))
        elif p.get_content_type()=='text/html': html.append(part_text(p))
    if plain: return '\n\n'.join(plain)
    if html:
        parser=HTMLText(); parser.feed('\n'.join(html)); return re.sub(r'\n{3,}','\n\n',''.join(parser.parts)).strip()
    return '(No readable text body found.)'

def cache_path(path):
    # Cache beside the user's Python script in a writable user cache directory.
    root=Path.home()/'.mbox_reader_cache'; root.mkdir(exist_ok=True)
    return root/(hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode('utf-8')).hexdigest()[:24]+'.sqlite3')

def connect(db):
    c=sqlite3.connect(str(db), timeout=20)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, start INTEGER NOT NULL, stop INTEGER NOT NULL, date TEXT, sender TEXT, subject TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS flags (start INTEGER PRIMARY KEY, starred INTEGER NOT NULL DEFAULT 0, read INTEGER NOT NULL DEFAULT 0)')
    c.execute('CREATE TABLE IF NOT EXISTS labels (name TEXT PRIMARY KEY)')
    c.execute('CREATE TABLE IF NOT EXISTS message_labels (start INTEGER, label TEXT, PRIMARY KEY(start,label))')
    c.execute('CREATE TABLE IF NOT EXISTS extra (start INTEGER PRIMARY KEY, message_id TEXT, refs TEXT, recipient TEXT, has_attachment INTEGER DEFAULT 0, duplicate INTEGER DEFAULT 0)')
    c.execute('CREATE INDEX IF NOT EXISTS extra_mid ON extra(message_id)')
    c.execute('CREATE TABLE IF NOT EXISTS attachments (start INTEGER, part_number INTEGER, filename TEXT, mime TEXT, size INTEGER, PRIMARY KEY(start,part_number))')
    c.execute('CREATE VIRTUAL TABLE IF NOT EXISTS body_fts USING fts5(start UNINDEXED, body)')
    return c

def metadata(c): return dict(c.execute('SELECT key,value FROM metadata'))

def index_archive(path, db, events, generation):
    c=connect(db)
    try:
        st=os.stat(path); signature=f'{st.st_size}:{st.st_mtime_ns}'
        meta=metadata(c)
        if meta.get('signature')==signature and meta.get('complete')=='1':
            events.put(('ready',generation,True)); return
        c.execute('DELETE FROM messages'); c.execute('DELETE FROM metadata'); c.execute('DELETE FROM flags');
        c.execute('DELETE FROM extra'); c.execute('DELETE FROM attachments'); c.execute('DELETE FROM body_fts'); c.execute('DELETE FROM message_labels'); c.commit()
        total=st.st_size; count=0; last_update=0; batch=[]
        # Only read RFC822 headers for the index; attachments/bodies are skipped by seeking to the next separator.
        # MBOX separator lines must start with 'From ' at the beginning of a line.
        with open(path,'rb',buffering=1024*1024) as f:
            start=None; header=bytearray(); in_header=False; previous=None
            while True:
                pos=f.tell(); line=f.readline()
                if not line:
                    if start is not None: batch.append((start,pos,bytes(header)))
                    break
                if line.startswith(b'From '):
                    if start is not None: batch.append((start,pos,bytes(header)))
                    start=f.tell(); header=bytearray(); in_header=True
                elif start is not None and in_header:
                    if line in (b'\n',b'\r\n'): in_header=False
                    elif len(header)<256*1024: header.extend(line[:max(0,256*1024-len(header))])
                if len(batch)>=250:
                    rows=[]
                    for a,b,h in batch:
                        try:
                            m=BytesHeaderParser(policy=policy.default).parsebytes(h+b'\n')
                            rows.append((a,b,date_string(m.get('Date','')),decoded(m.get('From','')),decoded(m.get('Subject','(No subject)'))))
                        except Exception: rows.append((a,b,'','','(Unreadable headers)'))
                    c.executemany('INSERT INTO messages(start,stop,date,sender,subject) VALUES (?,?,?,?,?)',rows)
                    c.commit(); count+=len(batch); batch.clear()
                    now=time.monotonic()
                    if now-last_update>0.4:
                        events.put(('progress',generation,(count,f.tell(),total))); last_update=now
            if batch:
                rows=[]
                for a,b,h in batch:
                    try:
                        m=BytesHeaderParser(policy=policy.default).parsebytes(h+b'\n')
                        rows.append((a,b,date_string(m.get('Date','')),decoded(m.get('From','')),decoded(m.get('Subject','(No subject)'))))
                    except Exception: rows.append((a,b,'','','(Unreadable headers)'))
                c.executemany('INSERT INTO messages(start,stop,date,sender,subject) VALUES (?,?,?,?,?)',rows)
                count+=len(rows)
            c.executemany('INSERT INTO metadata(key,value) VALUES (?,?)',[('signature',signature),('complete','1')]); c.commit()
        events.put(('ready',generation,False))
    except Exception as exc: events.put(('error',generation,str(exc)))
    finally: c.close()

def normalize_id(value):
    match=re.search(r'<[^>]+>',str(value or ''))
    return match.group(0).lower() if match else ''

def message_refs(msg):
    return [x.lower() for x in re.findall(r'<[^>]+>',str(msg.get('References',''))+' '+str(msg.get('In-Reply-To','')))]

def safe_filename(name):
    name=Path(str(name).replace('\\','/')).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',name).strip(' .')[:160] or 'attachment.bin'

def index_content_worker(path,db,events,generation):
    conn=connect(db)
    try:
        rows=conn.execute('SELECT start,stop FROM messages ORDER BY id').fetchall()
        total=len(rows)
        with open(path,'rb') as f:
            for i,(start,stop) in enumerate(rows,1):
                if i%100==0:
                    events.put(('enrich_progress',generation,(i,total)))
                if conn.execute('SELECT 1 FROM extra WHERE start=?',(start,)).fetchone():continue
                f.seek(start)
                msg=email.message_from_bytes(f.read(stop-start),policy=policy.default)
                mid=normalize_id(msg.get('Message-ID'))
                refs=' '.join(message_refs(msg))
                recipient=decoded(msg.get('To'))
                dup=bool(mid and conn.execute('SELECT 1 FROM extra WHERE message_id=? LIMIT 1',(mid,)).fetchone())
                parts=[]
                for partno,part in enumerate(msg.walk()):
                    if part.is_multipart():continue
                    if part.get_filename() or part.get_content_disposition()=='attachment':
                        payload=part.get_payload(decode=True) or b''
                        parts.append((start,partno,decoded(part.get_filename() or f'attachment_{partno}.bin'),part.get_content_type(),len(payload)))
                conn.execute('INSERT OR REPLACE INTO extra(start,message_id,refs,recipient,has_attachment,duplicate) VALUES (?,?,?,?,?,?)',
                             (start,mid,refs,recipient,int(bool(parts)),int(dup)))
                if parts:conn.executemany('INSERT OR REPLACE INTO attachments VALUES (?,?,?,?,?)',parts)
                conn.execute('INSERT INTO body_fts(start,body) VALUES (?,?)',(start,body_text(msg)[:500000]))
                if i%25==0:conn.commit()
        conn.commit();events.put(('enrich_done',generation,total))
    except Exception as exc:events.put(('enrich_error',generation,str(exc)))
    finally:conn.close()

