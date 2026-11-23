"""
core/reasoning/wanted_sources.py

ROADMAP_v3 §113. The sources a research run wanted and could not read, the
question it asks about them, and the files the user supplies in answer.

THE RECORD (WT1) IS CODE. A 403 used to be a failed tool call the model
worked around, and nothing kept that a source was wanted, by which claim,
and not obtained. The corpus now keeps every failed fetch beside every text
it holds (source_corpus.py owns URL identity), and this module derives the
record from the two: a URL a fetch never obtained, and a source a claim
cites that the run never read in full -- a failed fetch, a search snippet,
an abstract, or nothing at all. Measured on the owner's largest stored run,
before this was written: 111 distinct URLs never obtained, across 32 hosts,
and 23 cited sources never read in full, cited by 26 of its 98 claims. A
host-class filter for "scholarly" would have dropped 40 of the 111, so the
record keeps every host and puts the cited ones first.

IT CHANGES NO SCORE. A claim whose source was never read already carries
`quote_verified: None`; what this adds is the list, in the report and in
`wanted_sources.json`, so a reader can go and get the paper.

THE QUESTION (WT5) IS ASKED TWICE, EACH ONCE: after Pass 1, where the run
reads most (at least 97 of the 111 had failed by then), and after Pass 3a, which
knows which claims cite what it could not read (the other stored run's ten
all failed in grounding). The answer is the user's, through the pipeline's
own channel; a run with nobody to ask, or one launched unattended, records
what it would have asked and goes on. An answer that brings something new
-- a supplied file, a fetch that now answers, a typed note -- redrafts
Pass 1 at the first point and re-grounds the claims that cite those
sources at the second. One that brings nothing is traced and the run goes
on as if the user had said to proceed.

THE SUPPLIED FOLDER (WT3) BELONGS TO THE RUN. `output/<run_id>/supplied/`
is made when a run that can ask starts, so the folder exists before the
question names it. The HARNESS reads it, once per answer, into the run's
corpus and into the run's authorization, and `read_source` pages through
what was read then -- never the filesystem, never the workspace, and
never `read`. A file added after the answer is not seen.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

import config
from core.reasoning.source_corpus import normalize_url
from safety.policy_enforcement import redact_output_text

logger = logging.getLogger(__name__)

FILENAME = "wanted_sources.json"
SUPPLIED_DIR = "supplied"
SUPPLIED_PREFIX = "supplied/"
HEADING = "## Sources it could not read"

# The two points the run asks at, named by the pass each follows.
AFTER_DRAFT = "Pass 1"
AFTER_GROUNDING = "Pass 3a"

# How much of one supplied file the harness holds: fetch_url's own window
# (HELD_CHARS), so a supplied paper is as reachable as a fetched one, a page
# of fetch_url's size at a time.
SUPPLIED_CHARS = 200_000
PAGE_CHARS = 5000
# A backstop, not a knob: a folder of a thousand files is not a reply.
MAX_SUPPLIED_FILES = 50

TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".rst", ".tex", ".bib",
                           ".csv", ".tsv", ".json", ".xml"})
HTML_SUFFIXES = frozenset({".html", ".htm"})
PDF_SUFFIXES = frozenset({".pdf"})

# What a reading short of the whole is called in a sentence.
READINGS = {"none": "not read", "snippet": "search snippet only",
            "abstract": "abstract only"}

_HTTP = re.compile(r"error '(\d{3})\b")
_STATUS_WORDS = {401: "refused access", 403: "refused access",
                 404: "not found", 410: "gone", 429: "rate limited"}


# ---------------------------------------------------------------------------
# ---- The record (WT1) -----------------------------------------------------
# ---------------------------------------------------------------------------

def reason(error: str) -> str:
    """Why a fetch failed, in a few words, from fetch_url's error text.

    Read off the text because the text is what the run kept: fetch_url's
    error shapes are its own sentences (and httpx's, for a status), and a
    structured status would have to be added to every one of them."""
    text = error or ""
    low = text.lower()
    if low.startswith("could not read the pdf"):
        return "the PDF could not be read"
    if low.startswith("too many redirects"):
        return "too many redirects"
    if (low.startswith(("domain is blocked by policy", "url must start",
                        "url has no host"))):
        return "refused by the URL policy"
    if " is a pdf of " in low and "limit" in low:
        return "larger than the PDF limit"
    if "which is not text, html or a pdf" in low:
        return "not text, HTML or a PDF"
    if "timed out" in low or "timeout" in low:
        return "timed out"
    match = _HTTP.search(text)
    if match:
        code = int(match.group(1))
        words = _STATUS_WORDS.get(code) or (
            "server error" if code >= 500 else "refused")
        return f"{words} (HTTP {code})"
    if low.startswith("could not fetch url"):
        return "the connection failed"
    return "the fetch failed"


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    return host.removeprefix("www.") or "(no host)"


def entries(corpus, claims) -> list[dict]:
    """Every source the run wanted and did not read in full, cited first.

    One entry per normalized URL: a fetch that failed and was never
    answered later, and any source a claim cites with less than its full
    text. Sorted by how many claims cite it, then by host, then by URL, so
    the list is the same on every run with the same inputs."""
    if corpus is None:
        return []
    cited = defaultdict(list)
    spelled = {}
    for claim in claims or ():
        for source in getattr(claim, "grounding_sources", None) or ():
            url = source.get("url") if isinstance(source, dict) else None
            if not isinstance(url, str) or url.strip().startswith(
                    SUPPLIED_PREFIX):
                continue
            key = normalize_url(url)
            if not key:
                continue
            if claim.id not in cited[key]:
                cited[key].append(claim.id)
            spelled.setdefault(key, url.strip())
    out = []
    for key in set(corpus.failures) | set(cited):
        reading = corpus.reading(key)
        if reading == "full":
            continue
        failure = corpus.failures.get(key)
        out.append({
            "url": failure["url"] if failure else spelled[key],
            "reading": reading,
            "reason": (reason(failure["error"]) if failure
                       else ("never fetched" if reading == "none"
                             else READINGS[reading])),
            "passes": list(failure["passes"]) if failure else [],
            "cited_by": list(cited.get(key, ())),
        })
    out.sort(key=lambda e: (-len(e["cited_by"]), _host(e["url"]), e["url"]))
    return out


def record(corpus, claims, asks, supplied) -> dict:
    """What `wanted_sources.json` holds: the sources, every question the run
    asked (or would have), and every file it read from supplied/."""
    return {"sources": entries(corpus, claims),
            "asks": list(asks or ()),
            "supplied": [doc.artifact() for doc in (supplied or {}).values()]}


def _link(url: str) -> str:
    """`url` as Markdown a report can carry: an autolink when it is one,
    else a code span, so a model-supplied URL cannot break the list."""
    url = " ".join(str(url).split())
    if re.fullmatch(r"https?://[^\s<>`]+", url):
        return f"<{url}>"
    return f"`{url.replace('`', '')}`"


def section(sources) -> str:
    """The report's section, or "" when the run wanted nothing.

    The CITED ones, each with why and who cites it, because a claim's source
    is what a reader can act on; the rest as a count that points at the
    file, because 111 failed fetches no claim relied on is a log, not a
    report section."""
    sources = list(sources or ())
    if not sources:
        return ""
    cited = [s for s in sources if s.get("cited_by")]
    others = len(sources) - len(cited)
    lines = [HEADING, ""]
    if cited:
        lines += ["Claims in this report cite these sources, and the run "
                  "could not read them in full.", ""]
        for s in cited:
            lines.append(f"- {_link(s['url'])}: {s['reason']}. Cited by "
                         f"{', '.join(s['cited_by'])}.")
        if others:
            lines += ["", f"{others} other source{'' if others == 1 else 's'}"
                      f" the run could not read, which no claim cites, "
                      f"{'is' if others == 1 else 'are'} listed in "
                      f"`{FILENAME}`."]
    else:
        lines.append(f"The run could not read {others} source"
                     f"{'' if others == 1 else 's'}, and no claim cites "
                     f"{'it' if others == 1 else 'them'}. "
                     f"{'It is' if others == 1 else 'They are'} listed in "
                     f"`{FILENAME}`.")
    return "\n".join(lines) + "\n"


def with_section(report: str, sources) -> str:
    """`report` with the section after it, once. A resumed run reaches this
    call again with the section already on a stored report."""
    text = section(sources)
    if not text:
        return report
    body = (report or "").rstrip()
    if body.endswith(text.rstrip()):
        return report
    return f"{body}\n\n{text}" if body else text


def summary(sources) -> str | None:
    """The run's closing line about them, or None."""
    sources = list(sources or ())
    if not sources:
        return None
    cited = [s for s in sources if s.get("cited_by")]
    claims = {c for s in cited for c in s["cited_by"]}
    line = f"Sources: the run could not read {len(sources)}"
    if cited:
        line += f", {len(cited)} of them cited by {len(claims)} claim(s)"
    return f"{line}; listed in {FILENAME}."


# ---------------------------------------------------------------------------
# ---- The question (WT5) ---------------------------------------------------
# ---------------------------------------------------------------------------

def listed(sources, point: str) -> list[dict]:
    """What the question at `point` names. After the draft, everything not
    obtained so far; after grounding, the sources claims cite."""
    sources = list(sources or ())
    if point == AFTER_GROUNDING:
        return [s for s in sources if s.get("cited_by")]
    return sources


def folder(run_id) -> str:
    return os.path.join(config.OUTPUT_DIR, str(run_id), SUPPLIED_DIR)


def prepare_folder(run_id) -> str | None:
    """Make `output/<run_id>/supplied/` and return it, or None when the
    filesystem refuses -- a run must not die over a folder for a question
    it may never ask."""
    path = folder(run_id)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not make the folder for supplied sources "
                       "(%s): %s", path, exc)
        return None
    return path


def question(sources, point: str, path: str) -> dict:
    """The SOURCES request's payload: the list grouped by host, and where to
    put a file. Every string here is what both shells print."""
    by_host = defaultdict(list)
    for s in sources:
        by_host[_host(s["url"])].append(s)
    if point == AFTER_GROUNDING:
        intro = ("Claims cite these sources, and grounding could not read "
                 "them in full. The claims that cite them can be grounded "
                 "again against what you supply.")
    else:
        intro = ("The first draft was written without these sources. Pass 1 "
                 "can write it again with what you supply.")
    lines = [intro, ""]
    for host in sorted(by_host, key=lambda h: (-len(by_host[h]), h)):
        group = by_host[host]
        lines.append(f"{host} ({len(group)})")
        for s in group:
            cited = (f"; cited by {', '.join(s['cited_by'])}"
                     if s.get("cited_by") else "")
            lines.append(f"  {' '.join(str(s['url']).split())} -- "
                         f"{s['reason']}{cited}")
    lines += ["", "To supply one, save it (PDF, HTML or text, any name) in:",
              f"  {path}", "then choose the first answer below."]
    return {
        "title": (f"The run could not read {len(sources)} source"
                  f"{'' if len(sources) == 1 else 's'}"),
        "body": "\n".join(lines),
        "point": point,
        "folder": path,
        "count": len(sources),
    }


def pending_note(supplied, obtained, note: str) -> str:
    """What the redraft or the re-grounding is told, or "" when the answer
    brought nothing it could use."""
    if not (supplied or obtained or note):
        return ""
    lines = ["", "", "--- Since the first attempt ---",
             "Some sources could not be read the first time. The user has "
             "since answered:"]
    if supplied:
        lines += ["", "Files the user supplied for this run. Read them with "
                      "`read_source`, by name, a page at a time, and cite "
                      "one as `supplied/<name>`:"]
        lines += [f"- {doc.name} ({doc.total:,} characters)"
                  for doc in supplied]
    if obtained:
        lines += ["", "Sources that failed before and answer now, with "
                      "`fetch_url`:"]
        lines += [f"- {url}" for url in obtained]
    if note:
        lines += ["", f"The user's note: {' '.join(note.split())}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ---- The supplied folder (WT3) --------------------------------------------
# ---------------------------------------------------------------------------

@dataclass
class Supplied:
    """One file the harness read from supplied/, redacted and bounded."""
    name: str
    text: str
    sha256: str
    total: int
    kind: str

    def artifact(self) -> dict:
        return {"name": self.name, "kind": self.kind, "sha256": self.sha256,
                "chars": len(self.text), "total_chars": self.total}


def _limit(suffix: str) -> int:
    megabytes = (config.FETCH_MAX_PDF_MB if suffix in PDF_SUFFIXES
                 else config.FETCH_MAX_HTML_MB)
    return int(round(megabytes * 1_000_000))


def _read_regular(path: str, limit: int):
    """The bytes of `path` if it is a regular file no larger than `limit`,
    else a reason. Never follows a link: a link planted in the folder would
    otherwise hand the harness any file on the machine to read for the
    model."""
    try:
        before = os.lstat(path)
    except OSError as exc:
        return None, f"could not be read ({exc.strerror or exc})"
    if not stat.S_ISREG(before.st_mode):
        return None, "not a regular file (a link or a folder)"
    if before.st_size > limit:
        return None, (f"{before.st_size:,} bytes, larger than the "
                      f"{limit:,}-byte limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
        os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        return None, f"could not be read ({exc.strerror or exc})"
    try:
        after = os.fstat(fd)
        if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev):
            return None, "changed while it was being read"
        with os.fdopen(fd, "rb") as handle:
            fd = None
            data = handle.read(limit + 1)
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > limit:
        return None, f"larger than the {limit:,}-byte limit"
    return data, None


def _extract(suffix: str, data: bytes):
    """(kind, text, total) or (None, reason, 0). `total` is the whole
    document's length when the text is a window of it (a PDF), else 0."""
    if suffix in PDF_SUFFIXES:
        # The killable child fetch_url reads a PDF in: a supplied file is
        # untrusted content exactly as a fetched one is.
        from tools import isolation
        from tools.builtin import _pdf_text
        result = isolation.run_isolated(
            _pdf_text.pdf_window,
            {"data": _pdf_text.encode(data), "start": 0,
             "limit": SUPPLIED_CHARS},
            config.TOOL_COMPUTE_TIMEOUT_S, tool_name="read_source")
        if "error" in result:
            return None, f"the PDF could not be read: {result['error']}", 0
        if result.get("no_text"):
            return None, "the PDF has no text layer (a scan?)", 0
        return "pdf", result.get("text") or "", int(result.get("total") or 0)
    if suffix not in HTML_SUFFIXES and suffix not in TEXT_SUFFIXES:
        return None, "not a PDF, HTML or text file", 0
    text = data.decode("utf-8-sig", errors="replace")
    if suffix in HTML_SUFFIXES:
        from tools.builtin import _page_text
        return "html", _page_text.html_document(
            text, "", lambda url: None).text, 0
    return "text", text, 0


def load_supplied(path: str | None, known: dict) -> tuple[list, list]:
    """(new, skipped): every file in `path` not already read in this form.

    `known` is the run's name -> Supplied map; a file whose bytes are
    unchanged is not read again, and one that changed is read anew under
    the same name."""
    new, skipped = [], []
    if not path:
        return new, skipped
    try:
        names = sorted(entry.name for entry in os.scandir(path))
    except OSError:
        return new, skipped
    for name in names:
        if name.startswith("."):
            continue
        if len(new) + len(known) >= MAX_SUPPLIED_FILES:
            skipped.append({"name": name, "why": (
                f"more than {MAX_SUPPLIED_FILES} files were supplied")})
            continue
        suffix = os.path.splitext(name)[1].lower()
        data, why = _read_regular(os.path.join(path, name), _limit(suffix))
        if data is None:
            skipped.append({"name": name, "why": why})
            continue
        digest = hashlib.sha256(data).hexdigest()
        if name in known and known[name].sha256 == digest:
            continue
        kind, text, total = _extract(suffix, data)
        if kind is None:
            skipped.append({"name": name, "why": text})
            continue
        clean = redact_output_text(text).strip()
        if not clean:
            skipped.append({"name": name, "why": "no text in it"})
            continue
        new.append(Supplied(name=name, text=clean[:SUPPLIED_CHARS],
                            sha256=digest, total=max(total, len(clean)),
                            kind=kind))
    return new, skipped
