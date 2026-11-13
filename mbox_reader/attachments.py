"""attachments features for the MBOX Reader GUI."""
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

class AttachmentsMixin:
    """Feature-specific methods extracted from the original Reader class."""

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

