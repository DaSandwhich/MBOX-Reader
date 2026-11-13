"""Compose the MBOX Reader GUI from feature modules."""
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

from .ui import UiMixin
from .archives import ArchivesMixin
from .conversations import ConversationsMixin
from .exports import ExportsMixin
from .attachments import AttachmentsMixin
from .labels import LabelsMixin

class Reader(UiMixin, ArchivesMixin, ConversationsMixin, ExportsMixin, AttachmentsMixin, LabelsMixin, tk.Tk):
    """Main application window; behavior is divided into mixins."""

    def __init__(self):
        super().__init__(); self.title('Mail Archive — Gmail-style Offline MBOX Reader'); self.geometry('1200x740'); self.minsize(800,480)
        self.path=None; self.db=None; self.conn=None; self.events=queue.Queue(); self.generation=0
        self.offset=0; self.current=None; self.bulk_running=False; self.folder='All Mail'; self.search=tk.StringVar()
        self.sender_filter=tk.StringVar()
        self.recipient_filter=tk.StringVar()
        self.start_date_filter=tk.StringVar()
        self.end_date_filter=tk.StringVar()
        self.status=tk.StringVar(value='Open an MBOX archive. Your emails stay on this computer.')
        self.archives=load_library(); self.enriching=False; self.attachment_window=None
        # A single worker prevents concurrent expensive searches.
        self.search_jobs = queue.Queue(maxsize=1)
        self.search_request = 0
        self.search_timer = None
        self.suggestion_timer = None
        self.suggestion_request = 0
        threading.Thread(target=self._search_worker, daemon=True).start()
        self._build(); self.after(150,self.poll)
        if self.archives: self.after(300,lambda:self.load_archive(self.archives[0],startup=True))

