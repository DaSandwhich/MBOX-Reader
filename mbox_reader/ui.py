"""ui features for the MBOX Reader GUI."""
import email
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime, getaddresses
from html.parser import HTMLParser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sqlite3, threading, queue, re, os, hashlib, time, json, tempfile, webbrowser, html, shutil

from .calendar_picker import show_calendar
from .hyperlinks import insert_links, html_text
from .core import PAGE_SIZE, conversation_subject, library_file, load_library, save_library, decoded, date_string, part_text, body_text, cache_path, connect, metadata, index_archive, normalize_id, message_refs, safe_filename, index_content_worker, HTMLText

class UiMixin:
    """Feature-specific methods extracted from the original Reader class."""

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
        filters=ttk.Frame(self,padding=(12,0,12,8));filters.pack(fill='x')
        ttk.Label(filters,text='From').pack(side='left')
        sender=ttk.Combobox(filters,textvariable=self.sender_filter,width=23)
        sender.pack(side='left',padx=6)
        ttk.Label(filters,text='To').pack(side='left')
        recipient=ttk.Combobox(filters,textvariable=self.recipient_filter,width=23)
        recipient.pack(side='left',padx=6)
        self.address_boxes={'sender':sender,'recipient':recipient}
        for field,widget in self.address_boxes.items():
            widget.bind('<KeyRelease>',lambda event,f=field:self.schedule_address_suggestions(f))
            widget.bind('<<ComboboxSelected>>',lambda event:self.refresh(reset=True))
        ttk.Label(filters,text='From (YYYY-MM-DD)').pack(side='left')
        start=ttk.Entry(filters,textvariable=self.start_date_filter,width=12)
        start.pack(side='left',padx=(6,2))
        ttk.Button(
            filters,text='📅',width=3,
            command=lambda:show_calendar(self,start,self.start_date_filter,
                                         lambda:self.refresh(reset=True))
        ).pack(side='left',padx=(0,6))
        ttk.Label(filters,text='Through (YYYY-MM-DD)').pack(side='left')
        end=ttk.Entry(filters,textvariable=self.end_date_filter,width=12)
        end.pack(side='left',padx=(6,2))
        ttk.Button(
            filters,text='📅',width=3,
            command=lambda:show_calendar(self,end,self.end_date_filter,
                                         lambda:self.refresh(reset=True))
        ).pack(side='left',padx=(0,6))
        for entry in (sender,recipient,start,end):
            entry.bind('<Return>',lambda event:self.refresh(reset=True))
        ttk.Button(filters,text='Apply',command=lambda:self.refresh(reset=True)).pack(side='left')
        ttk.Button(filters,text='Clear filters',command=self.clear_filters).pack(side='left',padx=5)
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

    def clear_filters(self):
        """Clear recipient and date filters."""
        self.sender_filter.set('')
        self.recipient_filter.set('')
        self.start_date_filter.set('')
        self.end_date_filter.set('')
        self.refresh(reset=True)

    def schedule_address_suggestions(self, field):
        """Debounce address suggestions to avoid blocking while typing."""
        if self.suggestion_timer is not None:
            self.after_cancel(self.suggestion_timer)
        self.suggestion_timer=self.after(250,lambda:self._queue_address_suggestions(field))

    def _queue_address_suggestions(self, field):
        self.suggestion_timer=None
        if not self.db or not self.conn:return
        self.suggestion_request+=1
        value=(self.sender_filter if field=='sender' else self.recipient_filter).get().strip()
        if not value:
            self.address_boxes[field]['values']=[]
            return
        self.events.put(('suggestions_requested',self.generation,(self.suggestion_request,field,value,str(self.db))))
        # Run the potentially expensive address scan outside Tk's event loop.
        threading.Thread(target=self._address_suggestion_worker,
                         args=(self.generation,self.suggestion_request,field,value,str(self.db)),
                         daemon=True).start()

    def _address_suggestion_worker(self,generation,request_id,field,value,db):
        """Return distinct sender or recipient email addresses matching input."""
        connection=None
        try:
            connection=sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
            column='sender' if field=='sender' else 'recipient'
            # Limit candidates so large archives don't stall the dropdown.
            rows=connection.execute(
                f'SELECT DISTINCT {column} FROM messages WHERE {column} LIKE ? LIMIT 1200',
                ('%'+value+'%',)).fetchall()
            matches=set()
            for (header,) in rows:
                for _,address in getaddresses([header or '']):
                    if value.casefold() in address.casefold():
                        matches.add(address)
            suggestions=sorted(matches,key=lambda x:(not x.lower().startswith(value.lower()),x.lower()))[:40]
            self.events.put(('address_suggestions',generation,(request_id,field,value,suggestions)))
        except sqlite3.Error:
            pass
        finally:
            if connection:connection.close()

    def display_address_suggestions(self,data):
        request_id,field,value,suggestions=data
        widget=self.address_boxes[field]
        variable=self.sender_filter if field=='sender' else self.recipient_filter
        if request_id!=self.suggestion_request or variable.get().strip()!=value:return
        widget['values']=suggestions

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

    def refresh(self, reset=False):
        """Schedule a search without blocking Tkinter's event loop.

        A short debounce combines rapid folder/search changes. The worker uses
        its own SQLite connection; Tk widgets are updated only by poll().
        """
        if reset:
            self.offset = 0
        if not self.db or not self.conn:
            return

        self.search_request += 1
        request_id = self.search_request
        if self.search_timer is not None:
            self.after_cancel(self.search_timer)
        self.status.set("Searching messages…")
        from datetime import date
        try:
            for value in (self.start_date_filter.get().strip(),self.end_date_filter.get().strip()):
                if value and date.fromisoformat(value).isoformat()!=value:
                    raise ValueError
        except ValueError:
            self.status.set('Use YYYY-MM-DD for dates.')
            return
        if (self.start_date_filter.get().strip() and self.end_date_filter.get().strip()
                and self.start_date_filter.get().strip()>self.end_date_filter.get().strip()):
            self.status.set('Start date must be before end date.')
            return
        self.search_timer = self.after(
            120, lambda: self._queue_search(request_id)
        )

    def _queue_search(self, request_id):
        """Queue only the newest requested search for the background worker."""
        self.search_timer = None
        if request_id != self.search_request or not self.db:
            return
        request = (
            self.generation, request_id, str(self.db),
            self.search.get().strip(), self.folder, self.offset,
            self.sender_filter.get().strip(), self.recipient_filter.get().strip(), self.start_date_filter.get().strip(),
            self.end_date_filter.get().strip()
        )
        # Queue size one: replace obsolete pending searches with the newest.
        try:
            self.search_jobs.put_nowait(request)
        except queue.Full:
            try:
                self.search_jobs.get_nowait()
            except queue.Empty:
                pass
            self.search_jobs.put_nowait(request)

    @staticmethod
    def _search_rows(db, query, folder, offset, sender='', recipient='', start_date='', end_date=''):
        """Query a single page using a connection owned by the worker thread."""
        conditions = []
        args = []
        for token in re.findall(r'"[^"]*"|\S+', query):
            token = token.strip('"')
            if ':' in token:
                field, value = token.split(':', 1)
                field = field.lower()
                if field in ('from', 'to', 'subject', 'after', 'before', 'label', 'type') and value:
                    if field == 'from':
                        conditions.append('m.sender LIKE ?')
                        args.append('%' + value + '%')
                    elif field == 'subject':
                        conditions.append('m.subject LIKE ?')
                        args.append('%' + value + '%')
                    elif field == 'to':
                        conditions.append('m.recipient LIKE ?')
                        args.append('%' + value + '%')
                    elif field == 'after':
                        conditions.append('m.date >= ?')
                        args.append(value)
                    elif field == 'before':
                        conditions.append('m.date < ?')
                        args.append(value)
                    elif field == 'label':
                        conditions.append('EXISTS (SELECT 1 FROM message_labels ml WHERE ml.start=m.start AND ml.label=?)')
                        args.append(value)
                    elif field == 'type':
                        conditions.append('EXISTS (SELECT 1 FROM attachments a WHERE a.start=m.start AND (a.mime LIKE ? OR a.filename LIKE ?))')
                        args.extend(['%' + value + '%', '%' + value + '%'])
                    continue
            if token.lower() == 'has:attachment':
                conditions.append('EXISTS (SELECT 1 FROM attachments a WHERE a.start=m.start)')
                continue
            if token.lower() == 'is:duplicate':
                conditions.append('EXISTS (SELECT 1 FROM extra e WHERE e.start=m.start AND e.duplicate=1)')
                continue
            conditions.append('(' + ' OR '.join([
                'm.date LIKE ?', 'm.sender LIKE ?', 'm.subject LIKE ?',
                'EXISTS (SELECT 1 FROM body_fts bf WHERE bf.start=m.start AND bf.body LIKE ?)'
            ]) + ')')
            args.extend(['%' + token + '%'] * 4)
        if sender:
            conditions.append('m.sender LIKE ?')
            args.append('%'+sender+'%')
        if recipient:
            conditions.append('m.recipient LIKE ?')
            args.append('%'+recipient+'%')
        if start_date:
            conditions.append('m.date >= ?')
            args.append(start_date)
        if end_date:
            from datetime import date, timedelta
            conditions.append('m.date < ?')
            args.append((date.fromisoformat(end_date)+timedelta(days=1)).isoformat())
        if folder.startswith('Label: '):
            conditions.append('EXISTS (SELECT 1 FROM message_labels ml WHERE ml.start=m.start AND ml.label=?)')
            args.append(folder[7:])
        if folder == 'Starred':
            conditions.append('COALESCE(f.starred,0)=1')
        if folder == 'Unread':
            conditions.append('COALESCE(f.read,0)=0')

        base = ' FROM messages m LEFT JOIN flags f ON f.start=m.start'
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        # Read-only connections avoid schema initialization and UI-thread writes.
        connection = sqlite3.connect(
            Path(db).resolve().as_uri() + '?mode=ro', uri=True, timeout=5
        )
        try:
            total = connection.execute(
                'SELECT COUNT(*)' + base + where, args
            ).fetchone()[0]
            if offset >= total:
                offset = max(0, ((total - 1) // PAGE_SIZE) * PAGE_SIZE)
            rows = connection.execute(
                'SELECT m.id,m.date,m.sender,m.subject,'
                'COALESCE(f.starred,0),COALESCE(f.read,0)'
                + base + where + ' ORDER BY m.id DESC LIMIT ? OFFSET ?',
                args + [PAGE_SIZE, offset]
            ).fetchall()
            return offset, total, rows
        finally:
            connection.close()

    def _search_worker(self):
        """Execute database searches away from the Tkinter main thread."""
        while True:
            generation, request_id, db, query, folder, offset, sender, recipient, start_date, end_date = self.search_jobs.get()
            try:
                result = self._search_rows(db, query, folder, offset, sender, recipient, start_date, end_date)
                self.events.put(('search_result', generation, (request_id, result)))
            except sqlite3.Error as exc:
                self.events.put(('search_error', generation, (request_id, str(exc))))
            except Exception as exc:
                self.events.put(('search_error', generation, (request_id, str(exc))))

    def _display_search_results(self, result):
        """Render completed search results on the Tkinter thread only."""
        offset, total, rows = result
        self.offset = offset
        self.tree.delete(*self.tree.get_children())
        self.bulk_btn.configure(state='disabled')
        for mid, date, sender, subject, starred, read in rows:
            self.tree.insert(
                '', 'end', iid=str(mid),
                values=('★' if starred else '☆', sender,
                        ('● ' if not read else '') + subject, date)
            )
        self.page_label.configure(
            text=f'{offset + 1 if total else 0:,}–{min(offset + PAGE_SIZE, total):,} / {total:,}'
        )
        self.prev.configure(state='normal' if offset else 'disabled')
        self.next.configure(
            state='normal' if offset + PAGE_SIZE < total else 'disabled'
        )
        self.status.set(f'{total:,} matching messages.')

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
                # HTML-only messages need their link destinations preserved.
                has_plain=any(p.get_content_type()=='text/plain' and
                              p.get_content_disposition()!='attachment'
                              for p in msg.walk())
                if not has_plain:
                    full=html_text(msg) or full
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
                    body.delete('1.0','end');insert_links(body,content)
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

    def destroy(self):
        if self.conn:self.conn.close()
        super().destroy()

