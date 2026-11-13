"""Render safe clickable URLs and email addresses in Tkinter text widgets."""
import re
import webbrowser
from html.parser import HTMLParser
from urllib.parse import urlsplit
from .core import part_text

URLS=re.compile(r'https?://[^\\s<>"\\']+|mailto:[^\\s<>"\\']+|(?<![\\w@])[\\w.+-]+@[\\w.-]+\\.[a-z]{2,}',re.I)

def safe_url(raw):
    """Only permit browser and mail links, not executable URL schemes."""
    if '@' in raw and ':' not in raw:
        raw='mailto:'+raw
    parts=urlsplit(raw)
    if parts.scheme in ('http','https') and parts.netloc:
        return raw
    if parts.scheme=='mailto' and '@' in parts.path:
        return raw
    return None

def insert_links(widget,text):
    """Insert email text and linkify URLs without executing embedded HTML."""
    widget.insert('end',text)
    for i,match in enumerate(URLS.finditer(text)):
        visible=match.group().rstrip('.,;!?)')
        target=safe_url(visible)
        if not target:continue
        tag=f'link_{i}'
        widget.tag_add(tag,f'1.0+{match.start()}c',f'1.0+{match.start()+len(visible)}c')
        widget.tag_configure(tag,foreground='#0969da',underline=True)
        widget.tag_bind(tag,'<Enter>',lambda e,w=widget:w.configure(cursor='hand2'))
        widget.tag_bind(tag,'<Leave>',lambda e,w=widget:w.configure(cursor='xterm'))
        widget.tag_bind(tag,'<Button-1>',lambda e,url=target:webbrowser.open(url))

class SafeHTML(HTMLParser):
    """Extract readable HTML and visible link destinations, blocking active content."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.skip=0
        self.href=None
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','head'):self.skip+=1
        if self.skip:return
        if tag in ('p','div','br','li','tr'):self.parts.append('\\n')
        if tag=='a':self.href=safe_url(dict(attrs).get('href') or '')
    def handle_endtag(self,tag):
        if tag in ('script','style','head') and self.skip:self.skip-=1;return
        if self.skip:return
        if tag=='a':
            if self.href:self.parts.append(' ('+self.href+')')
            self.href=None
        if tag in ('p','div','li','tr'):self.parts.append('\\n')
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)

def html_text(msg):
    """Provide text and link destinations for HTML-only messages."""
    for part in msg.walk():
        if part.get_content_type()=='text/html' and part.get_content_disposition()!='attachment':
            parser=SafeHTML()
            parser.feed(part_text(part))
            return ''.join(parser.parts).strip()
    return ''
