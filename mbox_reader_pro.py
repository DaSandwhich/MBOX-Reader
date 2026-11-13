"""Fast, offline MBOX reader for large archives. Python 3.10+, standard library only."""
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

class Reader(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('Mail Archive — Gmail-style Offline MBOX Reader'); self.geometry('1200x740'); self.minsize(800,480)
        self.path=None; self.db=None; self.conn=None; self.events=queue.Queue(); self.generation=0
        self.offset=0; self.current=None; self.bulk_running=False; self.folder='All Mail'; self.search=tk.StringVar(); self.status=tk.StringVar(value='Open an MBOX archive. Your emails stay on this computer.')
        self.archives=load_library(); self.enriching=False; self.attachment_window=None
        self._build(); self.after(150,self.poll)
        if self.archives: self.after(300,lambda:self.load_archive(self.archives[0],startup=True))
    def _build(self):
        style=ttk.Style(self)
        if 'clam' in style.theme_names(): style.theme_use('clam')
        style.configure('Gmail.TButton',padding=(12,9))
        style.configure('Sidebar.TButton',padding=(12,12),anchor='w')
        style.configure('Treeview',rowheight=36,font=('Segoe UI',10))
        style.configure('Treeview.Heading',font=('Segoe UI',10,'bold'))
        self.configure(background='#f5f7fb')
        top=ttk.Frame(self,padding=12); top.pack(fill='x')
        ttk.Label(top,text='✉  Mail Archive',font=('Segoe UI',18,'bold')).pack(side='left',padx=(4,24))
        self.open_btn=ttk.Button(top,text='Add MBOX…',command=self.open_archive,style='Gmail.TButton'); self.open_btn.pack(side='right')
        self.archive_choice=ttk.Combobox(top,state='readonly',width=28)
        self.archive_choice.pack(side='right',padx=8)
        self.archive_choice.bind('<<ComboboxSelected>>',self.switch_archive)
        self.forget_btn=ttk.Button(top,text='Remove from list',command=self.forget_archive)
        self.forget_btn.pack(side='right')
        self.update_archive_choices()
        ent=ttk.Entry(top,textvariable=self.search,font=('Segoe UI',11)); ent.pack(side='left',fill='x',expand=True,padx=8)
        ent.bind('<Return>',lambda _:self.refresh(reset=True))
        ttk.Button(top,text='Search',command=lambda:self.refresh(reset=True)).pack(side='left')
        ttk.Button(top,text='Clear',command=lambda:(self.search.set(''),self.refresh(reset=True))).pack(side='left',padx=5)
        ttk.Button(top,text='Attachments',command=self.show_attachment_library).pack(side='left',padx=4)
        ttk.Button(top,text='Index content',command=self.start_enrichment).pack(side='left',padx=4)
        ttk.Button(top,text='Export selected EMLs',command=self.save_selected_emls).pack(side='left',padx=4)
        main=ttk.Frame(self); main.pack(fill='both',expand=True,padx=12,pady=(0,8))
        sidebar=ttk.Frame(main,width=175,padding=(0,12,10,0)); sidebar.pack(side='left',fill='y');sidebar.pack_propagate(False)
        ttk.Label(sidebar,text='FOLDERS',font=('Segoe UI',10,'bold')).pack(anchor='w',padx=10,pady=(0,10))
        self.folder_buttons={}
        for name in ('All Mail','Starred','Unread'):
            btn=ttk.Button(sidebar,text=('★  ' if name=='Starred' else '✉  ' if name=='All Mail' else '●  ')+name,style='Sidebar.TButton',command=lambda n=name:self.set_folder(n))
            btn.pack(fill='x',pady=3);self.folder_buttons[name]=btn
        ttk.Separator(sidebar).pack(fill='x',pady=12)
        ttk.Label(sidebar,text='CUSTOM LABELS',font=('Segoe UI',10,'bold')).pack(anchor='w',padx=10)
        self.label_list=tk.Listbox(sidebar,height=7,exportselection=False)
        self.label_list.pack(fill='x',padx=7,pady=5)
        self.label_list.bind('<<ListboxSelect>>',self.choose_label)
        ttk.Button(sidebar,text='+ New label',command=self.new_label).pack(fill='x',padx=7)
        ttk.Button(sidebar,text='Label selected',command=self.assign_label).pack(fill='x',padx=7,pady=3)
        ttk.Button(sidebar,text='Remove label',command=self.remove_label).pack(fill='x',padx=7)
        ttk.Label(sidebar,text='Local archive only',foreground='#64748b').pack(anchor='w',padx=10,pady=(30,0))
        ttk.Label(sidebar,text='No sending or syncing',foreground='#64748b').pack(anchor='w',padx=10)
        pane=ttk.Panedwindow(main,orient='horizontal'); pane.pack(side='left',fill='both',expand=True)
        left=ttk.Frame(pane); right=ttk.Frame(pane,padding=(12,8));pane.add(left,weight=3);pane.add(right,weight=4)
        nav=ttk.Frame(left);nav.pack(fill='x',pady=(0,6))
        self.folder_heading=ttk.Label(nav,text='All Mail',font=('Segoe UI',15,'bold'));self.folder_heading.pack(side='left')
        self.next=ttk.Button(nav,text='Next ›',command=lambda:self.change_page(1));self.next.pack(side='right')
        self.prev=ttk.Button(nav,text='‹ Prev',command=lambda:self.change_page(-1));self.prev.pack(side='right',padx=4)
        self.page_label=ttk.Label(nav,text='');self.page_label.pack(side='right',padx=10)
        self.tree=ttk.Treeview(left,columns=('star','sender','subject','date'),show='headings',selectmode='extended')
        for col,title,w in [('star','★',36),('sender','From',160),('subject','Subject',270),('date','Date',125)]:
            self.tree.heading(col,text=title);self.tree.column(col,width=w,minwidth=30,stretch=(col!='star'))
        sb=ttk.Scrollbar(left,command=self.tree.yview);self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left',fill='both',expand=True);sb.pack(side='right',fill='y')
        self.tree.bind('<<TreeviewSelect>>',self.select_message)
        self.tree.bind('<Control-a>',self.select_all_page)
        self.tree.bind('<Double-1>',self.toggle_star_from_click)
        self.heading=ttk.Label(right,text='Select an email to read',font=('Segoe UI',16,'bold'),wraplength=560);self.heading.pack(anchor='w',pady=(0,8))
        self.details=ttk.Label(right,text='',justify='left',wraplength=560);self.details.pack(anchor='w',pady=6)
        actions=ttk.Frame(right);actions.pack(fill='x',pady=8)
        self.star_btn=ttk.Button(actions,text='☆ Star',command=self.toggle_star,state='disabled');self.star_btn.pack(side='left')
        self.save_btn=ttk.Button(actions,text='Save email',command=self.save_email,state='disabled');self.save_btn.pack(side='left',padx=6)
        self.pdf_btn=ttk.Button(actions,text='Save as PDF',command=self.save_pdf,state='disabled');self.pdf_btn.pack(side='left',padx=6)
        self.bulk_btn=ttk.Button(actions,text='Save selected PDFs…',command=self.save_selected_pdfs,state='disabled');self.bulk_btn.pack(side='left',padx=6)
        self.attach_btn=ttk.Button(actions,text='Download attachments',command=self.save_attachments,state='disabled');self.attach_btn.pack(side='left')
        self.thread_pdf_btn=ttk.Button(actions,text='Save conversation PDF…',
            command=self.save_current_conversation_pdf,state='disabled')
        self.thread_pdf_btn.pack(side='left',padx=6)
        self.html_btn=ttk.Button(actions,text='View formatted HTML',command=self.view_html,state='disabled')
        self.html_btn.pack(side='left',padx=5)
        body_frame=ttk.Frame(right);body_frame.pack(fill='both',expand=True)
        self.thread_canvas=tk.Canvas(body_frame,highlightthickness=0,background='white')
        bs=ttk.Scrollbar(body_frame,command=self.thread_canvas.yview)
        self.thread_canvas.configure(yscrollcommand=bs.set)
        bs.pack(side='right',fill='y');self.thread_canvas.pack(side='left',fill='both',expand=True)
        self.thread_content=ttk.Frame(self.thread_canvas,padding=10)
        self.thread_window=self.thread_canvas.create_window((0,0),window=self.thread_content,anchor='nw')
        self.thread_content.bind('<Configure>',lambda e:self.thread_canvas.configure(scrollregion=self.thread_canvas.bbox('all')))
        self.thread_canvas.bind('<Configure>',lambda e:self.thread_canvas.itemconfigure(self.thread_window,width=e.width))
        self.displayed_conversation=[]
        self.bar=ttk.Progressbar(self,mode='determinate',maximum=100);self.bar.pack(fill='x',padx=12)
        ttk.Label(self,textvariable=self.status,anchor='w',padding=(12,6)).pack(fill='x')
    def select_all_page(self,event=None):
        self.tree.selection_set(self.tree.get_children())
        return 'break'
    def set_folder(self,name):
        self.folder=name;self.folder_heading.configure(text=name);self.current=None;self.refresh(reset=True)
    def toggle_star_from_click(self,event):
        region=self.tree.identify_region(event.x,event.y)
        if region=='cell' and self.tree.identify_column(event.x)=='#1':
            row=self.tree.identify_row(event.y)
            if row:self.tree.selection_set(row);self.current=int(row);self.toggle_star()
    def toggle_star(self):
        if self.current is None or not self.conn:return
        row=self.conn.execute('SELECT start FROM messages WHERE id=?',(self.current,)).fetchone()
        if not row:return
        start=row[0]
        old=self.conn.execute('SELECT starred FROM flags WHERE start=?',(start,)).fetchone()
        new=0 if old and old[0] else 1
        self.conn.execute('INSERT INTO flags(start,starred) VALUES (?,?) ON CONFLICT(start) DO UPDATE SET starred=excluded.starred',(start,new));self.conn.commit()
        self.star_btn.configure(text='★ Unstar' if new else '☆ Star')
        self.refresh()
    def update_archive_choices(self):
        self.archive_choice['values']=[Path(x).name+'  —  '+str(Path(x).parent) for x in self.archives]
        if self.path in self.archives:self.archive_choice.current(self.archives.index(self.path))
        elif self.archives:self.archive_choice.current(0)
        else:self.archive_choice.set('')
    def switch_archive(self,event=None):
        i=self.archive_choice.current()
        if 0<=i<len(self.archives):self.load_archive(self.archives[i])
    def forget_archive(self):
        path=self.path
        if not path:return
        if not messagebox.askyesno('Remove archive','Remove this MBOX from the saved list?\n\nThe original email file and its index will NOT be deleted.'):
            return
        self.archives=[x for x in self.archives if x!=path]
        save_library(self.archives)
        self.generation+=1
        if self.conn:self.conn.close();self.conn=None
        self.path=None;self.db=None;self.current=None
        self.tree.delete(*self.tree.get_children())
        self.heading.configure(text='Select an email to read');self.details.configure(text='')
        self.update_archive_choices()
        self.status.set('Archive removed from saved list.')
        if self.archives:self.load_archive(self.archives[0])
    def open_archive(self):
        path=filedialog.askopenfilename(title='Choose MBOX archive',filetypes=[('MBOX archives','*.mbox *.mbx'),('All files','*.*')])
        if not path:return
        self.load_archive(path)
    def load_archive(self,path,startup=False):
        path=str(Path(path).expanduser().resolve())
        if not os.path.isfile(path):
            self.status.set('Saved MBOX file not found: '+path)
            if not startup:messagebox.showwarning('Archive unavailable','The original MBOX file could not be found. Reconnect the drive or add the file from its new location.\n\n'+path)
            return
        if path in self.archives:self.archives.remove(path)
        self.archives.insert(0,path)
        save_library(self.archives)
        self.generation+=1; gen=self.generation
        if self.conn:self.conn.close();self.conn=None
        self.path=path; self.db=cache_path(path); self.offset=0; self.current=None
        self.update_archive_choices()
        self.tree.delete(*self.tree.get_children()); self.label_list.delete(0,'end'); self.bulk_btn.configure(state='disabled'); self.open_btn.configure(state='disabled')
        self.status.set('Indexing headers (not loading attachments)…'); self.bar['value']=0
        threading.Thread(target=index_archive,args=(path,self.db,self.events,gen),daemon=True).start()
    def poll(self):
        try:
            while True:
                kind,gen,data=self.events.get_nowait()
                if gen!=self.generation:continue
                if kind=='progress':
                    count,done,total=data; self.bar['value']=100*done/max(total,1)
                    self.status.set(f'Indexing: {count:,} messages • {done/1024**3:.2f} / {total/1024**3:.2f} GB ({100*done/max(total,1):.0f}%)')
                    # The first page becomes available while the index is still being built.
                    if self.conn is None:
                        self.conn=connect(self.db); self.refresh()
                elif kind=='ready':
                    if self.conn:self.conn.close()
                    self.conn=connect(self.db); self.bar['value']=100; self.open_btn.configure(state='normal')
                    self.load_labels(); self.refresh(); n=self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
                    self.status.set(f'{n:,} messages indexed. '+('Loaded saved index instantly.' if data else 'Index saved for faster reopening.'))
                elif kind=='enrich_progress':
                    done,total=data; self.status.set(f'Indexing content, attachments and reply links: {done:,}/{total:,} emails')
                elif kind=='enrich_done':
                    self.enriching=False; self.status.set(f'Content index complete: {data:,} messages. Search now includes email bodies.')
                    self.refresh()
                    if self.attachment_window and self.attachment_window.winfo_exists():self.populate_attachments()
                elif kind=='enrich_error':
                    self.enriching=False;self.status.set('Content index error: '+str(data))
                elif kind=='bulk_progress':
                    n,total,done=data
                    self.status.set(f'Exporting PDFs: {n:,}/{total:,} processed, {done:,} saved…')
                elif kind=='bulk_done':
                    done,total,errors,folder=data
                    self.bulk_running=False
                    self.bulk_btn.configure(state='normal' if self.tree.selection() else 'disabled')
                    self.status.set(f'PDF export finished: {done:,}/{total:,} saved to {folder}')
                    if errors:
                        messagebox.showwarning('PDF export completed with errors',f'Saved {done} of {total} PDFs.\n\nFirst errors:\n'+'\n'.join(errors[:5]))
                    else:messagebox.showinfo('PDF export complete',f'Saved {done} PDFs to:\n{folder}')
                elif kind=='error':
                    self.open_btn.configure(state='normal'); self.status.set('Indexing failed'); messagebox.showerror('MBOX Reader',data)
        except queue.Empty:pass
        self.after(150,self.poll)
    def refresh(self,reset=False):
        if reset:self.offset=0
        if not self.conn:return
        try:
            q=self.search.get().strip();conditions=[];args=[]
            for token in re.findall(r'"[^"]*"|\S+',q):
                token=token.strip('"')
                if ':' in token:
                    field,value=token.split(':',1);field=field.lower()
                    if field in ('from','to','subject','after','before','label','type') and value:
                        if field=='from':conditions.append('m.sender LIKE ?');args.append('%'+value+'%')
                        elif field=='subject':conditions.append('m.subject LIKE ?');args.append('%'+value+'%')
                        elif field=='to':conditions.append('EXISTS (SELECT 1 FROM extra e WHERE e.start=m.start AND e.recipient LIKE ?)');args.append('%'+value+'%')
                        elif field=='after':conditions.append('m.date >= ?');args.append(value)
                        elif field=='before':conditions.append('m.date < ?');args.append(value)
                        elif field=='label':conditions.append('EXISTS (SELECT 1 FROM message_labels ml WHERE ml.start=m.start AND ml.label=?)');args.append(value)
                        elif field=='type':conditions.append('EXISTS (SELECT 1 FROM attachments a WHERE a.start=m.start AND (a.mime LIKE ? OR a.filename LIKE ?))');args.extend(['%'+value+'%','%'+value+'%'])
                        continue
                if token.lower()=='has:attachment':
                    conditions.append('EXISTS (SELECT 1 FROM attachments a WHERE a.start=m.start)');continue
                if token.lower()=='is:duplicate':
                    conditions.append('EXISTS (SELECT 1 FROM extra e WHERE e.start=m.start AND e.duplicate=1)');continue
                conditions.append('('+' OR '.join(['m.date LIKE ?','m.sender LIKE ?','m.subject LIKE ?',
                    'EXISTS (SELECT 1 FROM body_fts bf WHERE bf.start=m.start AND bf.body LIKE ?)'])+')')
                args.extend(['%'+token+'%']*4)
            if self.folder.startswith('Label: '):
                conditions.append('EXISTS (SELECT 1 FROM message_labels ml WHERE ml.start=m.start AND ml.label=?)')
                args.append(self.folder[7:])
            if self.folder=='Starred':conditions.append('COALESCE(f.starred,0)=1')
            if self.folder=='Unread':conditions.append('COALESCE(f.read,0)=0')
            base=' FROM messages m LEFT JOIN flags f ON f.start=m.start'
            where=' WHERE '+' AND '.join(conditions) if conditions else ''
            total=self.conn.execute('SELECT COUNT(*)'+base+where,args).fetchone()[0]
            if self.offset>=total:self.offset=max(0,((total-1)//PAGE_SIZE)*PAGE_SIZE)
            rows=self.conn.execute('SELECT m.id,m.date,m.sender,m.subject,COALESCE(f.starred,0),COALESCE(f.read,0)'+base+where+' ORDER BY m.id DESC LIMIT ? OFFSET ?',args+[PAGE_SIZE,self.offset]).fetchall()
            self.tree.delete(*self.tree.get_children())
            self.bulk_btn.configure(state='disabled')
            for mid,date,sender,subject,starred,read in rows:
                self.tree.insert('','end',iid=str(mid),values=('★' if starred else '☆',sender,('● ' if not read else '')+subject,date))
            self.page_label.configure(text=f'{self.offset+1 if total else 0:,}–{min(self.offset+PAGE_SIZE,total):,} / {total:,}')
            self.prev.configure(state='normal' if self.offset else 'disabled')
            self.next.configure(state='normal' if self.offset+PAGE_SIZE<total else 'disabled')
        except sqlite3.Error as exc:self.status.set(f'Database busy: {exc}')
    def change_page(self,delta):self.offset=max(0,self.offset+delta*PAGE_SIZE);self.refresh()
    def selected_bytes(self):
        if self.current is None or self.conn is None:return None
        row=self.conn.execute('SELECT start,stop FROM messages WHERE id=?',(self.current,)).fetchone()
        if row is None:return None
        start,stop=row
        with open(self.path,'rb') as f:f.seek(start);return f.read(stop-start)
    def select_message(self,_=None):
        selected=self.tree.selection()
        if not selected:
            self.bulk_btn.configure(state='disabled'); return
        self.bulk_btn.configure(state='normal')
        self.current=int(selected[-1]); self.status.set('Reading selected email…'); self.update_idletasks()
        try:
            raw=self.selected_bytes(); msg=email.message_from_bytes(raw,policy=policy.default)
            attachments=[p for p in msg.walk() if not p.is_multipart() and (p.get_filename() or p.get_content_disposition()=='attachment')]
            self.heading.configure(text=decoded(msg.get('Subject','(No subject)')))
            self.details.configure(text=f"From: {decoded(msg.get('From'))}\nTo: {decoded(msg.get('To'))}\nDate: {date_string(msg.get('Date'))}\nAttachments: {len(attachments)}")
            self.render_conversation()
            self.html_btn.configure(state='normal' if any(p.get_content_type()=='text/html' for p in msg.walk()) else 'disabled')
            row=self.conn.execute('SELECT start FROM messages WHERE id=?',(self.current,)).fetchone()
            if row:
                start=row[0];self.conn.execute('INSERT INTO flags(start,read) VALUES (?,1) ON CONFLICT(start) DO UPDATE SET read=1',(start,));self.conn.commit()
                flag=self.conn.execute('SELECT starred FROM flags WHERE start=?',(start,)).fetchone()
                self.star_btn.configure(state='normal',text='★ Unstar' if flag and flag[0] else '☆ Star')
                if self.folder=='Unread':self.refresh()
            self.save_btn.configure(state='normal');self.pdf_btn.configure(state='normal');self.thread_pdf_btn.configure(state='normal');self.attach_btn.configure(state='normal' if attachments else 'disabled')
            self.status.set('Email loaded.')
        except Exception as exc:messagebox.showerror('Read failed',str(exc))
    def conversation_rows(self):
        if self.current is None or not self.conn:return []
        current=self.conn.execute('SELECT id,start,stop,date,sender,subject FROM messages WHERE id=?',(self.current,)).fetchone()
        if not current:return []
        key=conversation_subject(current[5])
        # Candidate selection uses normalized subject; explicit reply headers then split unrelated chains.
        rows=[r for r in self.conn.execute('SELECT id,start,stop,date,sender,subject FROM messages')
              if conversation_subject(r[5])==key]
        if len(rows)>2000:rows=rows[:2000] # Bound pathological generic subjects.
        headers={}; mids={}
        with open(self.path,'rb') as f:
            for row in rows:
                extra=self.conn.execute('SELECT message_id,refs FROM extra WHERE start=?',(row[1],)).fetchone()
                if extra:
                    mid=extra[0]; refs=(extra[1] or '').split()
                else:
                    f.seek(row[1]);raw=bytearray()
                    while len(raw)<262144:
                        line=f.readline()
                        if not line or line in (b'\n',b'\r\n'):break
                        raw.extend(line)
                    h=BytesHeaderParser(policy=policy.default).parsebytes(bytes(raw)+b'\n')
                    mid=normalize_id(h.get('Message-ID'))
                    refs=message_refs(h)
                headers[row[0]]=(mid,refs)
                if mid:mids[mid]=row[0]
        edges={r[0]:set() for r in rows}
        for rid,(_,refs) in headers.items():
            for ref in refs:
                other=mids.get(ref)
                if other is not None and other!=rid:
                    edges[rid].add(other);edges[other].add(rid)
        seen={self.current};pending=[self.current]
        while pending:
            for rid in edges[pending.pop()]:
                if rid not in seen:seen.add(rid);pending.append(rid)
        if len(seen)==1 and not any(edges.values()):seen=set(edges)
        return sorted((r for r in rows if r[0] in seen),key=lambda r:(r[3],r[0]))

    @staticmethod
    def clean_reply(text):
        """Hide common quoted history and signatures, preserving the original for expansion."""
        lines=text.splitlines(); cut=len(lines)
        patterns=(r'^On .{5,200}wrote:\s*$',r'^-{2,}\s*Original Message\s*-{2,}$',
                  r'^From:\s+.+$',r'^_{8,}\s*$',r'^>')
        for i,line in enumerate(lines):
            if any(re.match(p,line.strip(),re.I) for p in patterns):
                # From: is only considered quoted history if preceded by an empty line.
                if line.strip().lower().startswith('from:') and i and lines[i-1].strip():continue
                cut=i;break
        visible='\n'.join(lines[:cut]).strip()
        return visible or text.strip(),cut<len(lines)

    def save_current_conversation_pdf(self):
        if self.displayed_conversation:
            self.save_conversation_pdf(self.displayed_conversation)

    def render_conversation(self):
        """Display the selected email's entire conversation in the right-hand pane."""
        rows=self.conversation_rows()
        self.displayed_conversation=rows
        for child in self.thread_content.winfo_children():
            child.destroy()
        if not rows:return
        self.heading.configure(text=decoded(rows[0][5]))
        self.details.configure(text=f'{len(rows)} message(s) in this conversation')
        self.thread_pdf_btn.configure(state='normal')
        with open(self.path,'rb') as source:
            for index,row in enumerate(rows,1):
                _,start,stop,_,_,_=row
                source.seek(start)
                msg=email.message_from_bytes(source.read(stop-start),policy=policy.default)
                full=body_text(msg).strip()
                short,has_quote=self.clean_reply(full)
                card=ttk.LabelFrame(self.thread_content,
                    text=f'{decoded(msg.get("From"))}  ·  {date_string(msg.get("Date"))}',
                    padding=10)
                card.pack(fill='x',pady=6)
                ttk.Label(card,text=f'To: {decoded(msg.get("To"))}',
                          foreground='#64748b').pack(anchor='w')
                attachments=[decoded(part.get_filename()) for part in msg.walk() if part.get_filename()]
                if attachments:
                    ttk.Label(card,text='Attachments: '+', '.join(attachments),
                              wraplength=460).pack(anchor='w')
                body=tk.Text(card,wrap='word',height=2,font=('Segoe UI',10),
                             relief='flat',borderwidth=0)
                body.pack(fill='x',pady=(8,3))
                expanded=[row[0]==self.current]
                quoted=[False]
                actions=ttk.Frame(card);actions.pack(anchor='w')
                toggle=ttk.Button(actions);toggle.pack(side='left')
                quote_ref=[None]
                def render(body=body,expanded=expanded,quoted=quoted,short=short,full=full,
                           toggle=toggle,quote_ref=quote_ref):
                    content=full if quoted[0] else short
                    if not expanded[0]:
                        content=short[:180]+('…' if len(short)>180 else '')
                    body.configure(state='normal')
                    body.delete('1.0','end');body.insert('1.0',content)
                    body.configure(height=min(28,max(2,content.count('\\n')+1,len(content)//75+1)),
                                   state='disabled')
                    toggle.configure(text='Collapse' if expanded[0] else 'Expand')
                    q=quote_ref[0]
                    if q:q.configure(text='Hide quoted text' if quoted[0] else 'Show quoted text')
                def toggle_expanded(expanded=expanded,render=render):
                    expanded[0]=not expanded[0];render()
                toggle.configure(command=toggle_expanded)
                if has_quote:
                    def toggle_quote(quoted=quoted,expanded=expanded,render=render):
                        quoted[0]=not quoted[0];expanded[0]=True;render()
                    quote_ref[0]=ttk.Button(actions,command=toggle_quote)
                    quote_ref[0].pack(side='left',padx=8)
                render()
        self.thread_canvas.yview_moveto(0)

    def save_conversation_pdf(self,rows):
        target=filedialog.asksaveasfilename(title='Save entire conversation as PDF',
                     defaultextension='.pdf',filetypes=[('PDF documents','*.pdf')])
        if not target:return
        try:
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.pagesizes import letter
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from xml.sax.saxutils import escape
            font='Helvetica'
            for fp in (r'C:\Windows\Fonts\arial.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
                if Path(fp).exists():
                    if 'MailPDF' not in pdfmetrics.getRegisteredFontNames():
                        pdfmetrics.registerFont(TTFont('MailPDF',fp))
                    font='MailPDF';break
            styles=getSampleStyleSheet()
            head=ParagraphStyle('ThreadHead',parent=styles['Heading2'],fontName=font,fontSize=13,leading=18,spaceBefore=16)
            normal=ParagraphStyle('ThreadBody',parent=styles['Normal'],fontName=font,fontSize=9,leading=13,spaceAfter=8,wordWrap='CJK')
            story=[Paragraph(escape(rows[0][5] or 'Conversation'),head),
                   Paragraph(f'{len(rows)} emails in conversation',normal)]
            with open(self.path,'rb') as source:
                for i,(_,start,stop,_,_,_) in enumerate(rows,1):
                    source.seek(start)
                    msg=email.message_from_bytes(source.read(stop-start),policy=policy.default)
                    story.append(Paragraph(escape(f'{i}. '+decoded(msg.get('Subject'))),head))
                    for label in ('From','To','Date'):
                        value=date_string(msg.get(label)) if label=='Date' else decoded(msg.get(label))
                        story.append(Paragraph(escape(label+': '+value),normal))
                    for paragraph in re.split(r'\n\s*\n',self.clean_reply(body_text(msg))[0]):
                        for chunk_start in range(0,len(paragraph),1200):
                            chunk=paragraph[chunk_start:chunk_start+1200]
                            if chunk.strip():
                                story.append(Paragraph(escape(chunk).replace('\n','<br/>'),normal))
                    attachments=[decoded(part.get_filename()) for part in msg.walk() if part.get_filename()]
                    if attachments:
                        story.append(Paragraph(escape('Attachments (not embedded): '+', '.join(attachments)),normal))
                    story.append(Spacer(1,12))
            SimpleDocTemplate(target,pagesize=letter,leftMargin=48,rightMargin=48,
                              topMargin=48,bottomMargin=48).build(story)
            messagebox.showinfo('Saved',f'Conversation PDF saved:\n{target}')
        except ImportError:
            messagebox.showerror('Missing PDF library','Install with: py -m pip install reportlab')
        except Exception as exc:
            messagebox.showerror('PDF export failed',str(exc))

    def save_email(self):
        if self.current is None:return
        target=filedialog.asksaveasfilename(defaultextension='.eml',filetypes=[('Email files','*.eml')])
        if not target:return
        try:Path(target).write_bytes(self.selected_bytes());self.status.set('Email saved.')
        except Exception as exc:messagebox.showerror('Save failed',str(exc))
    @staticmethod
    def write_pdf(raw,target):
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from xml.sax.saxutils import escape
        msg=email.message_from_bytes(raw,policy=policy.default)
        font='Helvetica'
        for fontpath in (r'C:\Windows\Fonts\arial.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
            if Path(fontpath).exists():
                if 'MailPDF' not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont('MailPDF',fontpath))
                font='MailPDF';break
        styles=getSampleStyleSheet()
        title_style=ParagraphStyle('MailTitle',parent=styles['Heading1'],fontName=font,fontSize=15,leading=21,spaceAfter=14)
        meta_style=ParagraphStyle('MailMeta',parent=styles['Normal'],fontName=font,fontSize=9,leading=14,spaceAfter=5)
        body_style=ParagraphStyle('MailBody',parent=styles['Normal'],fontName=font,fontSize=10,leading=15,spaceAfter=8,wordWrap='CJK')
        story=[Paragraph(escape(decoded(msg.get('Subject','(No subject)'))),title_style)]
        for label,header in [('From','From'),('To','To'),('Date','Date'),('Cc','Cc')]:
            val=date_string(msg.get(header)) if header=='Date' else decoded(msg.get(header))
            if val:story.append(Paragraph('<b>'+label+':</b> '+escape(val),meta_style))
        attachments=[decoded(p.get_filename()) or '(unnamed attachment)' for p in msg.walk() if not p.is_multipart() and (p.get_filename() or p.get_content_disposition()=='attachment')]
        if attachments:
            story.append(Paragraph('<b>Attachments (not embedded in this PDF):</b> '+escape(', '.join(attachments)),meta_style))
        story.append(Spacer(1,18))
        content=body_text(msg)
        # Preserve line breaks without creating a single unbreakable paragraph.
        for paragraph in re.split(r'\n\s*\n',content):
            if not paragraph.strip():continue
            lines=paragraph.splitlines()
            for start in range(0,len(lines),40):
                chunk='\n'.join(lines[start:start+40])
                story.append(Paragraph(escape(chunk).replace('\n','<br/>'),body_style))
        doc=SimpleDocTemplate(target,pagesize=letter,rightMargin=48,leftMargin=48,topMargin=48,bottomMargin=48,title=decoded(msg.get('Subject','Email')))
        def footer(canvas,document):
            canvas.setFont('Helvetica',8);canvas.setFillColor(colors.grey)
            canvas.drawRightString(letter[0]-48,28,f'Page {document.page}')
        doc.build(story,onFirstPage=footer,onLaterPages=footer)
    def save_pdf(self):
        if self.current is None:return
        target=filedialog.asksaveasfilename(title='Save email as PDF',defaultextension='.pdf',filetypes=[('PDF documents','*.pdf')])
        if not target:return
        try:
            self.write_pdf(self.selected_bytes(),target)
            self.status.set('PDF saved: '+target)
            messagebox.showinfo('Saved','Email saved as PDF.')
        except ImportError:
            messagebox.showerror('PDF support missing','Install ReportLab with: py -m pip install reportlab')
        except Exception as exc:messagebox.showerror('PDF export failed',str(exc))

    def save_selected_pdfs(self):
        ids=[int(i) for i in self.tree.selection()]
        if not ids or self.bulk_running:return
        folder=filedialog.askdirectory(title=f'Choose folder for {len(ids)} PDFs')
        if not folder:return
        self.bulk_running=True;self.bulk_btn.configure(state='disabled')
        self.status.set(f'Saving {len(ids)} selected emails as PDFs…')
        # Only IDs and file paths are passed to the worker, not the UI SQLite connection.
        threading.Thread(target=self._bulk_pdf_worker,args=(ids,folder,self.path,self.db,self.generation),daemon=True).start()

    def _bulk_pdf_worker(self,ids,folder,path,db,generation):
        done=0;errors=[]
        try:
            conn=sqlite3.connect(str(db),timeout=30)
            try:
                with open(path,'rb') as f:
                    for n,mid in enumerate(ids,1):
                        if generation!=self.generation:break
                        row=conn.execute('SELECT start,stop,subject FROM messages WHERE id=?',(mid,)).fetchone()
                        if row is None:
                            errors.append(f'{mid}: missing email');continue
                        start,stop,subject=row
                        safe=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',subject or 'Email').strip(' .')[:75] or 'Email'
                        target=Path(folder)/f'{mid:08d}_{safe}.pdf'
                        try:
                            f.seek(start)
                            self.write_pdf(f.read(stop-start),str(target))
                            done+=1
                        except Exception as exc:
                            errors.append(f'{mid}: {exc}')
                        self.events.put(('bulk_progress',generation,(n,len(ids),done)))
            finally:conn.close()
        except Exception as exc:errors.append(str(exc))
        self.events.put(('bulk_done',generation,(done,len(ids),errors,folder)))

    def save_attachments(self):
        if self.current is None:return
        folder=filedialog.askdirectory(title='Save attachments to');
        if not folder:return
        try:
            msg=email.message_from_bytes(self.selected_bytes(),policy=policy.default);count=0
            for i,p in enumerate(msg.walk(),1):
                if p.is_multipart() or not (p.get_filename() or p.get_content_disposition()=='attachment'):continue
                name=decoded(p.get_filename()) if p.get_filename() else f'attachment_{i}.bin'
                name=Path(name.replace('\\','/')).name
                name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',name).strip(' .') or f'attachment_{i}.bin'
                target=Path(folder)/name; j=1
                while target.exists():target=Path(folder)/f'{Path(name).stem}_{j}{Path(name).suffix}';j+=1
                target.write_bytes(p.get_payload(decode=True) or b'');count+=1
            messagebox.showinfo('Saved',f'Saved {count} attachment(s).')
        except Exception as exc:messagebox.showerror('Save failed',str(exc))
    def start_enrichment(self):
        if not self.path or not self.db or self.enriching:return
        if not self.conn or metadata(self.conn).get('complete')!='1':
            messagebox.showinfo('Please wait','Let the initial MBOX header scan finish first.');return
        self.enriching=True
        self.status.set('Building searchable content, attachment and duplicate index in background…')
        threading.Thread(target=index_content_worker,args=(self.path,self.db,self.events,self.generation),daemon=True).start()

    def load_labels(self):
        self.label_list.delete(0,'end')
        if self.conn:
            for (name,) in self.conn.execute('SELECT name FROM labels ORDER BY name'):
                self.label_list.insert('end',name)

    def new_label(self):
        if not self.conn:return
        from tkinter import simpledialog
        name=simpledialog.askstring('New label','Label name:',parent=self)
        if not name:return
        name=name.strip()[:80]
        if not name:return
        self.conn.execute('INSERT OR IGNORE INTO labels(name) VALUES (?)',(name,));self.conn.commit();self.load_labels()

    def choose_label(self,event=None):
        selected=self.label_list.curselection()
        if selected:self.set_folder('Label: '+self.label_list.get(selected[0]))

    def assign_label(self):
        if not self.conn:return
        selected=self.label_list.curselection()
        if not selected:
            messagebox.showinfo('Choose a label','Select a label on the left first.');return
        label=self.label_list.get(selected[0])
        ids=[int(x) for x in self.tree.selection()]
        if not ids and self.current is not None:ids=[self.current]
        for mid in ids:
            row=self.conn.execute('SELECT start FROM messages WHERE id=?',(mid,)).fetchone()
            if row:self.conn.execute('INSERT OR IGNORE INTO message_labels VALUES (?,?)',(row[0],label))
        self.conn.commit();self.refresh()
        self.status.set(f'Applied label {label} to {len(ids)} emails.')

    def remove_label(self):
        if not self.conn:return
        selected=self.label_list.curselection()
        if not selected:return
        label=self.label_list.get(selected[0])
        if not messagebox.askyesno('Delete label',f'Delete label "{label}"? Emails will not be deleted.'):return
        self.conn.execute('DELETE FROM labels WHERE name=?',(label,))
        self.conn.execute('DELETE FROM message_labels WHERE label=?',(label,));self.conn.commit()
        self.load_labels();self.set_folder('All Mail')

    def show_attachment_library(self):
        if not self.conn:return
        if self.attachment_window and self.attachment_window.winfo_exists():
            self.attachment_window.lift();return
        win=tk.Toplevel(self);win.title('Attachment library');win.geometry('850x550')
        self.attachment_window=win
        top=ttk.Frame(win,padding=8);top.pack(fill='x')
        ttk.Label(top,text='Attachments found by content indexing').pack(side='left')
        ttk.Button(top,text='Refresh',command=self.populate_attachments).pack(side='right')
        ttk.Button(top,text='Save selected',command=self.export_library_attachments).pack(side='right',padx=5)
        self.attachment_tree=ttk.Treeview(win,columns=('name','type','size','sender'),show='headings',selectmode='extended')
        for col,width in [('name',330),('type',130),('size',90),('sender',240)]:
            self.attachment_tree.heading(col,text=col.title());self.attachment_tree.column(col,width=width)
        self.attachment_tree.pack(fill='both',expand=True,padx=8,pady=8)
        self.populate_attachments()

    def populate_attachments(self):
        if not self.conn or not self.attachment_window or not self.attachment_window.winfo_exists():return
        tree=self.attachment_tree;tree.delete(*tree.get_children())
        rows=self.conn.execute('SELECT a.start,a.part_number,a.filename,a.mime,a.size,m.sender FROM attachments a JOIN messages m ON m.start=a.start ORDER BY m.id DESC LIMIT 2000').fetchall()
        self.attachment_rows=rows
        for i,row in enumerate(rows):tree.insert('','end',iid=str(i),values=(row[2],row[3],f'{row[4]/1024:.1f} KB',row[5]))

    def export_library_attachments(self):
        if not self.conn:return
        indices=[int(x) for x in self.attachment_tree.selection()]
        if not indices:return
        folder=filedialog.askdirectory(title='Save selected attachments');
        if not folder:return
        count=0
        with open(self.path,'rb') as f:
            for index in indices:
                start,partno,filename,*_=self.attachment_rows[index]
                row=self.conn.execute('SELECT stop FROM messages WHERE start=?',(start,)).fetchone()
                if not row:continue
                f.seek(start);msg=email.message_from_bytes(f.read(row[0]-start),policy=policy.default)
                parts=list(msg.walk())
                if partno>=len(parts):continue
                name=safe_filename(filename);target=Path(folder)/name;n=1
                while target.exists():target=Path(folder)/f'{Path(name).stem}_{n}{Path(name).suffix}';n+=1
                target.write_bytes(parts[partno].get_payload(decode=True) or b'');count+=1
        messagebox.showinfo('Saved',f'Saved {count} attachment(s).')

    def save_selected_emls(self):
        if not self.conn:return
        ids=[int(x) for x in self.tree.selection()]
        if not ids:return
        folder=filedialog.askdirectory(title='Export selected emails as EML files')
        if not folder:return
        with open(self.path,'rb') as f:
            for mid in ids:
                row=self.conn.execute('SELECT start,stop,subject FROM messages WHERE id=?',(mid,)).fetchone()
                if not row:continue
                start,stop,subject=row;f.seek(start)
                (Path(folder)/f'{mid:08d}_{safe_filename(subject)[:70]}.eml').write_bytes(f.read(stop-start))
        messagebox.showinfo('Export complete',f'Saved {len(ids)} email files.')

    def view_html(self):
        if self.current is None:return
        msg=email.message_from_bytes(self.selected_bytes(),policy=policy.default)
        html_parts=[part_text(part) for part in msg.walk() if part.get_content_type()=='text/html' and part.get_content_disposition()!='attachment']
        if not html_parts:
            messagebox.showinfo('No HTML','This email has no HTML version.');return
        # CSP blocks remote network requests, scripts, frames, and external styles.
        # A sandboxed iframe prevents the email from navigating the parent viewer.
        import base64
        data=base64.b64encode(html_parts[0].encode('utf-8')).decode('ascii')
        page=("<!doctype html><meta charset='utf-8'><meta http-equiv='Content-Security-Policy' "
              "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; frame-src data:; form-action 'none'\">"
              "<title>Offline email preview</title><style>body{font:14px system-ui;margin:25px}"
              "iframe{width:100%;height:90vh;border:1px solid #ddd}</style>"
              "<h3>Offline email preview — remote images blocked</h3>"
              "<iframe sandbox src='data:text/html;base64,"+data+"'></iframe>")
        output=Path(tempfile.gettempdir())/'mbox_reader_preview.html'
        output.write_text(page,encoding='utf-8')
        webbrowser.open(output.as_uri())

    def destroy(self):
        if self.conn:self.conn.close()
        super().destroy()

if __name__=='__main__':Reader().mainloop()
