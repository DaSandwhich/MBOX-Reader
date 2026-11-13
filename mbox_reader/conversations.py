"""conversations features for the MBOX Reader GUI."""
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

from .core import PAGE_SIZE, conversation_subject, library_file, load_library, save_library, decoded, date_string, part_text, body_text, cache_path, connect, metadata, index_archive, normalize_id, message_refs, safe_filename, index_content_worker, HTMLText

class ConversationsMixin:
    """Feature-specific methods extracted from the original Reader class."""

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

