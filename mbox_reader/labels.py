"""labels features for the MBOX Reader GUI."""
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

class LabelsMixin:
    """Feature-specific methods extracted from the original Reader class."""

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

