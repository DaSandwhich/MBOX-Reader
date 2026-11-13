"""exports features for the MBOX Reader GUI."""
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

class ExportsMixin:
    """Feature-specific methods extracted from the original Reader class."""

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

