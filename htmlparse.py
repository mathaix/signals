"""Single-pass HTML parser collecting everything the scraper and signals need."""
import json
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse


def normalize(url: str) -> str:
    """Drop the fragment, lowercase scheme + host, empty path -> '/'."""
    p = urlparse(urldefrag(url).url)
    return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=p.path or "/").geturl()


# second-level suffixes where the registrable domain keeps three labels
SECOND_LEVEL = {
    "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.jp", "co.nz", "co.in",
    "com.br", "com.mx", "com.sg", "co.za", "com.cn", "com.tr",
}


def registrable_domain(host: str) -> str:
    """'jobs.acme.com' / 'www.acme.com' -> 'acme.com'; 'www.acme.co.uk' -> 'acme.co.uk'."""
    labels = host.lower().strip(".").split(".")
    keep = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


CONTAINERS = {"div", "li", "article", "section", "tr", "td"}
BLOCKS = CONTAINERS | {"p", "br", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6"}


class Page(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.title = ""
        self.links = []  # normalized, de-duplicated
        self.anchors = []  # (absolute url, text)
        self.anchor_headings = []  # parallel to anchors: last h1-h4 text before it, else None
        self.anchor_paths = []  # parallel to anchors: ids of the open containers around it
        self.lines = []  # (container path, text) per block of text, in document order
        self.meta = {}  # name/property -> content
        self.canonical = None
        self.feeds = []
        self.assets = []  # <script src> and <link href>
        self.jsonld = []  # parsed JSON-LD blocks
        self.headings = []
        self.text = []
        self._in_title = False
        self._skip = 0
        self._ld = None
        self._heading = None
        self._anchor = None
        self._last_heading = None
        self._h = None  # text of the h1-h4 being read
        self._stack = []  # open (tag, id) containers
        self._ids = 0
        self._line = None  # [path, text] being built

    def _flush(self):
        if self._line and self._line[1].strip():
            self.lines.append((self._line[0], " ".join(self._line[1].split())))
        self._line = None

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag in BLOCKS or tag == "a":
            self._flush()
        if tag in CONTAINERS:
            self._ids += 1
            self._stack.append((tag, self._ids))
        if tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            try:
                url = urljoin(self.base, a["href"])
                ok = urlparse(url).scheme in ("http", "https")
            except ValueError:
                ok = False
            if ok:
                n = normalize(url)
                if n not in self.links:
                    self.links.append(n)
                self._anchor = [url, ""]
                self.anchors.append(self._anchor)
                self.anchor_headings.append(self._last_heading)
                self.anchor_paths.append(tuple(i for _, i in self._stack))
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and "content" in a:
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "link" and a.get("href"):
            rels = a.get("rel", "").lower().split()
            href = urljoin(self.base, a["href"])
            self.assets.append(href)
            if "canonical" in rels:
                self.canonical = href
            if "alternate" in rels and a.get("type", "").lower() in ("application/rss+xml", "application/atom+xml"):
                self.feeds.append(href)
        elif tag == "script":
            self._skip += 1
            if a.get("src"):
                self.assets.append(urljoin(self.base, a["src"]))
            if a.get("type", "").lower() == "application/ld+json":
                self._ld = ""
        elif tag == "style":
            self._skip += 1
        elif tag in ("h1", "h2", "h3"):
            self._heading = ""
        if tag in ("h1", "h2", "h3", "h4"):
            self._h = ""

    def handle_endtag(self, tag):
        if tag in BLOCKS or tag == "a":
            self._flush()
        if tag in CONTAINERS:
            for i in range(len(self._stack) - 1, -1, -1):
                if self._stack[i][0] == tag:
                    del self._stack[i:]
                    break
        if tag in ("h1", "h2", "h3", "h4") and self._h is not None:
            if self._h.strip():
                self._last_heading = " ".join(self._h.split())
            self._h = None
        if tag == "title":
            self._in_title = False
        elif tag == "a":
            self._anchor = None
        elif tag == "script":
            self._skip = max(0, self._skip - 1)
            if self._ld is not None:
                try:
                    self.jsonld.append(json.loads(self._ld))
                except ValueError:
                    pass
                self._ld = None
        elif tag == "style":
            self._skip = max(0, self._skip - 1)
        elif tag in ("h1", "h2", "h3") and self._heading is not None:
            if self._heading.strip():
                self.headings.append(" ".join(self._heading.split()))
            self._heading = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._ld is not None:
            self._ld += data
        elif not self._skip:
            self.text.append(data)
            if self._line is None:
                self._line = [tuple(i for _, i in self._stack), ""]
            self._line[1] += data
            if self._h is not None:
                self._h += data
            if self._heading is not None:
                self._heading += data
            if self._anchor is not None:
                self._anchor[1] += data


def parse(html: str, base: str) -> Page:
    page = Page(base)
    page.feed(html)
    page.close()
    page._flush()
    page.title = page.title.strip()
    return page
