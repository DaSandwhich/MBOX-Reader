"""archives features for the MBOX Reader GUI."""
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

class ArchivesMixin:
    """Feature-specific methods extracted from the original Reader class."""

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

    def start_enrichment(self):
        if not self.path or not self.db or self.enriching:return
        if not self.conn or metadata(self.conn).get('complete')!='1':
            messagebox.showinfo('Please wait','Let the initial MBOX header scan finish first.');return
        self.enriching=True
        self.status.set('Building searchable content, attachment and duplicate index in background…')
        threading.Thread(target=index_content_worker,args=(self.path,self.db,self.events,self.generation),daemon=True).start()

