"""
tools/builtin/_page_text.py

What `fetch_url` makes of a web page before the model reads it (batch 240).

WHY THIS EXISTS. `fetch_url` used to hand the model the decoded body as it
arrived. Measured on seven live pages, the first 5,000-character page was
80-96% markup -- `<head>`, scripts, styles, navigation -- and the text a
person reads was 1-25% of the HTML. On Wikipedia the article's first
sentence sat at HTML character 73,396, past the 64 KB read cap, so no part
of any Wikipedia article was ever retrievable; GitHub's README began on the
fifth page. Every one of those characters was context the model paid for
and could not use.

MAIN CONTENT, STDLIB, NO NEW DEPENDENCY (the owner's choice). Python's
`html.parser`, and rules this project can read and test:

  * THE REGION. The page's `<main>` or `role="main"` element, else its
    first `<article>`, else `<body>`, else everything. That one choice is
    most of the win: it is where Wikipedia's article, MDN's reference text,
    Python's documentation and GitHub's README live, and it leaves the
    site's banner, menus and footer outside.
  * WHAT IS DROPPED INSIDE IT. Elements that carry no prose (`script`,
    `style`, `svg`, forms, embedded frames), page furniture by tag (`nav`,
    `aside`, `footer`, and `header` when there is no main region to hold an
    article's own), by ARIA role (navigation, banner, complementary,
    contentinfo, search), by being hidden (`hidden`, `aria-hidden`,
    `display:none`), and by a short list of class and id names that mean
    furniture on the web as it is (`navbox`, `breadcrumb`, `sr-only`, ...).
  * WHAT IS KEPT AS STRUCTURE. Headings as `#` lines, list items as `-` or
    `1.`, table rows as cells joined by ` | `, preformatted text verbatim,
    paragraphs separated by a blank line.

LINKS: TEXT INLINE, URLS LISTED APART (the owner's choice). A link keeps its
text in the prose and gains a short marker, `[L<n>]`; `fetch_url` lists the
URL for each marker with the page of text it appears on, so a citation can
be followed without every sentence carrying a URL. Distinct URLs are
numbered once per document, up to MAX_LINKS.

TOTAL OVER MALFORMED HTML. Real pages leave elements unclosed, so an end
tag closes whatever it finds open above it, and an element that never
closes is closed by its parent's end. A dropped element that was never
closed therefore cannot swallow the rest of the document.

PURE. No network, no config, no logging: `fetch_url` decides what to fetch
and what a link may be; this module only turns one string into another.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urldefrag, urljoin

#: How many distinct links one document may number. Past it a link keeps
#: its text and gains no marker: the listing is a route to a citation, not
#: a sitemap, and a Wikipedia article carries hundreds.
MAX_LINKS = 200

#: A title past this is cut; the HTML spec puts no bound on `<title>`.
MAX_TITLE_CHARS = 300

#: How deep the open-element stack may grow. A page of unclosed `<p>`s
#: closed only by `</body>` would otherwise grow it without bound.
_MAX_DEPTH = 4000

#: Elements with no closing tag. Never pushed, so they never wait to close.
_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

#: Elements whose whole subtree carries no prose a reader wants.
_DROP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "math", "canvas",
    "iframe", "object", "video", "audio", "picture", "map", "form",
    "button", "select", "textarea", "label", "nav", "aside", "footer",
    "dialog", "head", "title",
})

#: ARIA roles that are page furniture whatever element carries them.
_DROP_ROLES = frozenset({
    "navigation", "banner", "complementary", "contentinfo", "search",
    "dialog", "alertdialog", "menu", "menubar", "toolbar", "tablist",
})

#: Whole class or id names that mean furniture -- matched as written,
#: because the hyphenated ones only mean it as a whole (`sr-only`).
_DROP_NAMES = frozenset({
    "sr-only", "visually-hidden", "visuallyhidden", "screen-reader-text",
    "skip-link", "skiplink", "skip-to-content", "mw-editsection",
    "mw-jump-link", "noprint", "navbox", "navbar", "breadcrumb",
    "breadcrumbs", "sidebar", "toc", "cookie-banner", "cookie-notice",
    # Sphinx's pilcrow beside every heading: a link to the heading itself.
    "headerlink",
})

#: Words that mean furniture wherever they sit in a class or id: the value
#: is split on anything that is not a letter or digit, so `site-navbox`
#: yields `navbox` while `menuetto` yields nothing. Kept short on purpose --
#: each entry is prose this could drop if it were wrong.
_DROP_WORDS = frozenset({
    "navbox", "breadcrumb", "breadcrumbs", "noprint", "editsection",
    "cookie", "cookies", "consent", "advert", "advertisement", "newsletter",
    # Measured on Wikipedia (batch 240): its article tabs, page tools and
    # 37-language list sit INSIDE <main>, in `vector-menu-content`,
    # `vector-dropdown` and `interlanguage-link` containers, and the
    # language list alone used every one of the MAX_LINKS numbers.
    "menu", "dropdown", "interlanguage",
})

#: Elements that end a paragraph, and those that end a line.
_PARAGRAPH = frozenset({"p", "blockquote", "table", "figure", "section",
                        "article", "details"})
_LINE = frozenset({
    "address", "dd", "div", "dl", "dt", "fieldset", "figcaption", "header",
    "hgroup", "li", "main", "summary", "tbody", "thead", "tfoot", "tr",
    "caption", "ul", "ol",
})

_HEADING = {f"h{n}": n for n in range(1, 7)}
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
_SPACES = re.compile(r"[ \t\f\v\r\n]+")
_MARKER = re.compile(r"\[L(\d+)\]")
_LIST_ITEM = re.compile(r"^( *)(- |\d+\. )")

# A `<pre>` block's text travels between these two so that tidying the
# prose around it cannot reflow it. Neither can occur in parsed HTML text:
# the parser hands control characters through, but these two are removed
# from every data chunk before they could be confused with the sentinels.
_PRE_OPEN, _PRE_CLOSE = "\x02", "\x03"


@dataclass
class Document:
    """One fetched document as the model will read it."""

    text: str
    kind: str                         # "html", "pdf" or "text"
    title: Optional[str] = None
    links: dict = field(default_factory=dict)   # marker number -> url
    pages: Optional[int] = None       # a PDF's page count
    digest: str = ""                  # of the whole text, to tell versions apart

    def __post_init__(self) -> None:
        if not self.digest:
            self.digest = text_digest(self.text)


def text_digest(text: str) -> str:
    """Whether two fetches of one URL produced the same document."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def markers_in(text: str) -> list:
    """The link numbers a slice of text refers to, first appearance first."""
    seen: list = []
    for match in _MARKER.finditer(text):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen


def split_marker_end(text: str, end: int) -> int:
    """`end`, moved past an `[L<n>]` marker it would otherwise cut in two.

    A page boundary that fell inside a two-digit marker would leave its
    first digit on one page and its second on the next, and neither half
    would name a link.
    """
    start = text.rfind("[L", max(0, end - 8), end)
    if start == -1:
        return end
    close = text.find("]", start, start + 10)
    if close != -1 and close >= end and _MARKER.fullmatch(text[start:close + 1]):
        return close + 1
    return end


def text_document(text: str) -> Document:
    """A body that is already text: JSON, plain text, XML, source code."""
    return Document(text=text, kind="text")


# ---- HTML -----------------------------------------------------------------

def _attrs(raw) -> dict:
    return {name.lower(): (value or "") for name, value in raw}


def _is_furniture(tag: str, attrs: dict, drop_header: bool) -> bool:
    if tag in _DROP_TAGS or (drop_header and tag == "header"):
        return True
    if "hidden" in attrs or attrs.get("aria-hidden", "").lower() == "true":
        return True
    if attrs.get("role", "").lower() in _DROP_ROLES:
        return True
    style = attrs.get("style", "").lower().replace(" ", "")
    if "display:none" in style or "visibility:hidden" in style:
        return True
    for name in ("class", "id"):
        value = attrs.get(name, "").lower()
        if not value:
            continue
        if _DROP_NAMES.intersection(value.split()):
            return True
        if _DROP_WORDS.intersection(_WORD_SPLIT.split(value)):
            return True
    return False


class _Scan(HTMLParser):
    """Pass one: where the main region starts, and the page's title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.index = -1
        self.main: Optional[int] = None
        self.article: Optional[int] = None
        self.body: Optional[int] = None
        self._in_title = False
        self.title_parts: list = []

    def handle_starttag(self, tag, attrs):
        self.index += 1
        tag = tag.lower()
        if tag == "title" and not self.title_parts:
            self._in_title = True
        if self.main is None and (
                tag == "main" or _attrs(attrs).get("role", "").lower() == "main"):
            self.main = self.index
        elif tag == "article" and self.article is None:
            self.article = self.index
        elif tag == "body" and self.body is None:
            self.body = self.index

    def handle_startendtag(self, tag, attrs):
        self.index += 1

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)


class _Render(HTMLParser):
    """Pass two: the region's prose, with structure and link markers."""

    def __init__(self, region: Optional[int], drop_header: bool,
                 base_url: str,
                 keep_link: Callable[[str], Optional[str]]) -> None:
        super().__init__(convert_charrefs=True)
        self.region = region            # start-tag index of the region, or None
        self.drop_header = drop_header
        self.base_url = base_url
        self.page_url = urldefrag(base_url)[0]
        self.keep_link = keep_link
        self.index = -1
        self.stack: list = []           # (tag, is furniture)
        self.inside = region is None    # the whole document when no region
        self.region_depth: Optional[int] = None
        self.dropped = 0                # how many furniture elements are open
        self.pre = 0
        self.out: list = []
        self.lists: list = []           # per open list: None for ul, a count for ol
        self.row_cells = 0
        self.link: Optional[str] = None
        self.link_text = False
        self.links: dict = {}           # url -> number
        self.done = False

    def _visible(self) -> bool:
        return self.inside and not self.dropped and not self.done

    def handle_starttag(self, tag, attrs):
        self.index += 1
        tag = tag.lower()
        attrs = _attrs(attrs)
        if tag in _VOID:
            self._void(tag)
            return
        is_region = self.region is not None and self.index == self.region
        if is_region:
            self.inside = True
            self.region_depth = len(self.stack)
        furniture = (self.inside and not is_region
                     and _is_furniture(tag, attrs, self.drop_header))
        if len(self.stack) >= _MAX_DEPTH:
            # Pathological nesting: stop opening, keep reading the text.
            return
        self.stack.append((tag, furniture))
        if furniture:
            self.dropped += 1
        elif self._visible():
            self._open(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        # `<br/>` and friends: a start with no end to wait for.
        self.index += 1
        self._void(tag.lower())

    def _void(self, tag):
        if not self._visible():
            return
        if tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID:
            return
        # An end tag closes the nearest open element of its name and every
        # element opened above it; one with no open match is ignored.
        for depth in range(len(self.stack) - 1, -1, -1):
            if self.stack[depth][0] == tag:
                break
        else:
            return
        while len(self.stack) > depth:
            name, furniture = self.stack.pop()
            if furniture:
                self.dropped -= 1
            elif self._visible():
                self._close(name)
            if (self.region_depth is not None
                    and len(self.stack) == self.region_depth):
                # The region itself just closed: nothing after it is read.
                self.done = True

    def handle_data(self, data):
        if not self._visible():
            return
        data = data.replace(_PRE_OPEN, "").replace(_PRE_CLOSE, "")
        self.out.append(data if self.pre else _SPACES.sub(" ", data))
        if self.link is not None and data.strip():
            self.link_text = True

    def _open(self, tag, attrs):
        if tag in _HEADING:
            self.out.append("\n\n" + "#" * _HEADING[tag] + " ")
        elif tag == "li":
            if self.lists and self.lists[-1] is not None:
                self.lists[-1] += 1
                bullet = f"{self.lists[-1]}. "
            else:
                bullet = "- "
            self.out.append("\n" + "  " * max(len(self.lists) - 1, 0) + bullet)
        elif tag in ("ul", "ol"):
            # A list nested in an item continues that item's lines.
            if not self.lists:
                self.out.append("\n")
            self.lists.append(0 if tag == "ol" else None)
        elif tag == "tr":
            self.row_cells = 0
            self.out.append("\n")
        elif tag in ("td", "th"):
            if self.row_cells:
                self.out.append(" | ")
            self.row_cells += 1
        elif tag == "pre":
            self.pre += 1
            if self.pre == 1:
                self.out.append("\n\n" + _PRE_OPEN)
        elif tag == "a":
            href = attrs.get("href", "")
            self.link = self._link(href) if href else None
            self.link_text = False
        elif tag in _PARAGRAPH:
            self.out.append("\n\n")
        elif tag in _LINE:
            self.out.append("\n")

    def _close(self, tag):
        if tag in _HEADING:
            self.out.append("\n\n")
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self.out.append("\n")
        elif tag == "pre":
            self.pre = max(self.pre - 1, 0)
            if self.pre == 0:
                self.out.append(_PRE_CLOSE + "\n\n")
        elif tag == "a":
            if self.link is not None and self.link_text:
                self.out.append(f"[L{self.links[self.link]}]")
            self.link = None
        elif tag in _PARAGRAPH:
            self.out.append("\n\n")
        elif tag in _LINE and tag not in ("li", "tr"):
            # A list item and a table row START their line; ending it too
            # would leave a blank line between every two of them.
            self.out.append("\n")

    def _link(self, href: str) -> Optional[str]:
        """The link's key in `self.links` if it is numbered, else None."""
        href = href.strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            return None
        absolute = urldefrag(urljoin(self.base_url, href))[0]
        if not absolute.startswith(("http://", "https://")):
            return None
        if absolute == self.page_url:
            return None              # a link to this same page
        kept = self.keep_link(absolute)
        if kept is None:
            return None
        if kept not in self.links:
            if len(self.links) >= MAX_LINKS:
                return None
            self.links[kept] = len(self.links) + 1
        return kept


def _prose(piece: str) -> str:
    """One stretch of text between `<pre>` blocks, tidied."""
    lines = []
    for line in piece.split("\n"):
        item = _LIST_ITEM.match(line)
        lines.append(line.rstrip() if item else line.strip())
    text = "\n".join(lines)
    # A heading whose text was all furniture leaves its `#` behind, and one
    # whose text began inside an inline element leaves extra spaces.
    text = re.sub(r"(?m)^#+ *$", "", text)
    text = re.sub(r"(?m)^(#+) {2,}", r"\1 ", text)
    # A table row's empty leading cells (a logo, a spacer) leave their
    # separators behind; so do runs of empty cells between real ones.
    text = re.sub(r"(?m)^(?:\| *)+", "", text)
    text = re.sub(r"(?: *\|){2,} *", " | ", text)
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)
    return text.strip()


def _tidy(raw: str) -> str:
    """Prose tidied stretch by stretch; a `<pre>` block kept exactly, its
    own blank lines included -- code's layout is its meaning."""
    pieces = re.split(f"{_PRE_OPEN}(.*?){_PRE_CLOSE}", raw, flags=re.S)
    parts: list = []
    for number, piece in enumerate(pieces):
        if number % 2:
            block = "\n".join(line.rstrip() for line in piece.split("\n"))
            block = block.strip("\n")
            if block.strip():
                parts.append(block)
        else:
            prose = _prose(piece)
            if prose:
                parts.append(prose)
    return "\n\n".join(parts)


def html_document(html: str, base_url: str,
                  keep_link: Callable[[str], Optional[str]] = lambda url: url
                  ) -> Document:
    """The readable text of an HTML page, its title and its numbered links.

    `keep_link(url)` decides whether a link may be offered, and may return
    a rewritten URL (a redacted one, say); None leaves the link as plain
    text. `fetch_url` passes its URL policy and its length bound.
    """
    scan = _Scan()
    scan.feed(html)
    scan.close()
    if scan.main is not None:
        region, drop_header = scan.main, False
    elif scan.article is not None:
        region, drop_header = scan.article, False
    else:
        # No region of its own: a top-level <header> is the site's banner.
        region, drop_header = scan.body, True
    render = _Render(region, drop_header, base_url, keep_link)
    render.feed(html)
    render.close()
    title = _SPACES.sub(" ", "".join(scan.title_parts)).strip() or None
    if title is not None:
        title = title[:MAX_TITLE_CHARS]
    links = {number: url for url, number in render.links.items()}
    return Document(text=_tidy("".join(render.out)), kind="html",
                    title=title, links=links)
