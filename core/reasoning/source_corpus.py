"""
core/reasoning/source_corpus.py

What a claim was actually grounded IN, kept for the length of one run.

ROADMAP_v2 §45 (SQ2). Pass 3a's `sources` list has always been URLs and two
model-invented floats. The text those URLs served reached the model, was
stringified into the pass thread, and was then unreachable: nothing in
`03_grounding.json` could say what a page had said, so `similarity_score`
could not be checked against anything and a quote could not be checked at
all. `fetch_url.py`'s own comment already describes the artifact this
builds ("output_writer builds each run's sources/ directory from it") --
that directory has existed and been empty since §12.

WHY A SECOND SINK RATHER THAN A CHANGE TO _translate. The orchestrator
already sees every tool result and deliberately forwards only `ok` plus an
error string to the UI, because a successful result body is not
observability. That boundary is unchanged. This is a different consumer
with a different need, attached beside it -- the corpus is read by the
scoring stage and written to the run directory, and neither is a
transcript.

REDACTED ON ENTRY, THOUGH IT ARRIVES REDACTED. `registry.dispatch` runs
every result through `check_output_policy` before the loop emits it, so
what lands here has already been through `redact_output_text`. It is run
again anyway, and for `_translate`'s stated reason: one rule owned at one
boundary -- nothing is stored by this module unredacted -- beats depending
on a guarantee made two layers away that a later refactor could quietly
drop. The second pass is a no-op on already-clean text.

THIS MODULE OWNS URL IDENTITY for the pipeline. Two spellings of one arXiv
paper are one source, and a model that cites `/abs/2005.14165v3` after
fetching `/abs/2005.14165` must not be told its own citation has no text.
`scholar.py` resolves the same URLs and imports `arxiv_id_from_url` from
here rather than re-deriving it -- a second copy of "what counts as the
same paper" is how the corpus and the scholarly lookup come to disagree
about one source in one artifact.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from safety.policy_enforcement import redact_output_text

logger = logging.getLogger(__name__)

# Bounds, not tuning. A research pass calls fetch_url a bounded number of
# times (§31 budgets), so these are a backstop against a pathological run
# rather than a knob anyone is expected to turn: the corpus is held in
# memory for the length of a run and then written to disk.
#
# MAX_DOCUMENT_CHARS is deliberately well ABOVE fetch_url's own
# MAX_CONTENT_CHARS (5000). It bounds this module against a future tool
# that returns more; it is not a second truncation of the tools that exist,
# because a cap that bites in normal operation would silently shorten the
# text a similarity score is computed from.
MAX_DOCUMENTS = 200
MAX_DOCUMENT_CHARS = 20_000

# Below this, a "document" is a title and a fragment -- a web_search
# snippet for a result nobody fetched. Kept, because a 300-char snippet is
# still the only text some cited URLs ever have, and scoring a claim
# against it is better than reporting no evidence at all. The constant
# exists to reject the empty and near-empty case, not the short one.
MIN_DOCUMENT_CHARS = 16

_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
# `2005.14165`, `2005.14165v3`, and the pre-2007 `math.GT/0309136` form --
# whose archive prefix contains a SLASH, which is why the id group cannot
# simply exclude one. A path with any further segment is not an arXiv id.
_ARXIV_PATH_RE = re.compile(
    r"^/(?:abs|pdf)/"
    r"(?P<id>(?:[a-z-]+(?:\.[A-Za-z]{2})?/)?[^/?#]+?)"
    r"(?:v\d+)?(?:\.pdf)?$",
    re.IGNORECASE,
)


@dataclass
class SourceDocument:
    """One URL's best available text, and where it came from.

    `tool` and `retrieved_at` are provenance rather than decoration: a
    similarity score computed from a 300-char search snippet and one
    computed from a fetched page are not the same measurement, and a
    reader of the artifact has to be able to tell which they are looking
    at.
    """

    url: str
    text: str
    title: str = ""
    tool: str = ""
    retrieved_at: str = ""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def as_artifact(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "tool": self.tool,
            "retrieved_at": self.retrieved_at,
            "sha256": self.sha256,
            "chars": len(self.text),
            "text": self.text,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def arxiv_id_from_url(url: str) -> str | None:
    """The bare arXiv id in `url`, version stripped, or None.

    Version stripped because a version is not a different paper for any
    purpose this pipeline has: the corpus keys on it, and OpenAlex's DOI
    for `2005.14165v3` is `10.48550/arXiv.2005.14165`.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.hostname is None or parts.hostname.lower() not in _ARXIV_HOSTS:
        return None
    match = _ARXIV_PATH_RE.match(parts.path)
    return match.group("id") if match else None


def normalize_url(url: str) -> str:
    """The key two spellings of one source have to share.

    Case-folds the scheme and host, drops the fragment, drops a default
    port, drops a bare trailing slash, and collapses every arXiv `abs`/`pdf`
    spelling onto one canonical `abs` URL. The QUERY IS KEPT: `?id=7` is a
    different page on most sites, and dropping it would merge sources that
    say different things.

    An unparseable URL is returned stripped rather than raising. This runs
    on model-supplied strings; a malformed one should fail to match a
    document, not fail the run.
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    arxiv_id = arxiv_id_from_url(raw)
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"

    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    host = (parts.hostname or "").lower()
    if not host:
        return raw
    if parts.port and not (
        (parts.scheme == "http" and parts.port == 80)
        or (parts.scheme == "https" and parts.port == 443)
    ):
        host = f"{host}:{parts.port}"

    path = parts.path
    if path == "/":
        path = ""

    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


@dataclass
class SourceCorpus:
    """Every source text one run retrieved, keyed by normalized URL.

    Run-scoped and passed by reference, the way `run.granted_calls` is
    (§25): one object means the corpus is already complete at the moment
    the scoring stage reads it, rather than being assembled at the end by a
    run that reached the end.

    THE LONGEST TEXT WINS. One URL can arrive three times -- as a 300-char
    web_search snippet, as a 600-char arXiv abstract, and as 5000 chars
    from fetch_url -- and the question every consumer asks is "what is the
    most this run knows about this page". Last-write-wins would make the
    answer depend on the order the model happened to call its tools in.
    """

    documents: dict = field(default_factory=dict)
    dropped: int = 0

    # -- ingest ----------------------------------------------------------

    def add(self, tool_name: str, result) -> int:
        """Absorb one tool result. Returns how many documents it added or
        improved.

        Anything unrecognised is ignored silently: this is attached to
        EVERY tool result a research pass produces, and a `todo_write`
        return value is not a defect worth a log line. A recognised tool
        whose result is the error shape is also ignored -- `arxiv.py` and
        `web_search.py` both return errors rather than raising, so an
        error dict is an ordinary occurrence here.
        """
        if not isinstance(result, dict) or "error" in result:
            return 0
        if tool_name == "fetch_url":
            return self._add_fetch_url(result)
        if tool_name == "arxiv_search":
            return self._add_arxiv(result)
        if tool_name == "web_search":
            return self._add_web_search(result)
        return 0

    def _add_fetch_url(self, result: dict) -> int:
        # `url` is the URL that ANSWERED, after redirects -- which is what
        # fetch_url.py:89-95 says the grounding pass attributes the text
        # to, and therefore what the model will cite.
        return self._store(
            url=result.get("url"),
            text=result.get("content"),
            title="",
            tool="fetch_url",
        )

    def _add_arxiv(self, result: dict) -> int:
        added = 0
        for entry in result.get("results") or ():
            if not isinstance(entry, dict):
                continue
            arxiv_id = (entry.get("arxiv_id") or "").strip()
            if not arxiv_id:
                continue
            title = (entry.get("title") or "").strip()
            summary = (entry.get("summary") or "").strip()
            # Title AND abstract, joined. The title carries the paper's
            # subject in the compressed form a claim is usually phrased in,
            # and a 600-char abstract that opens on background prose can
            # otherwise match a claim about the paper's own topic poorly.
            text = "\n\n".join(p for p in (title, summary) if p)
            added += self._store(
                url=f"https://arxiv.org/abs/{arxiv_id}",
                text=text,
                title=title,
                tool="arxiv_search",
            )
        return added

    def _add_web_search(self, result: dict) -> int:
        added = 0
        for entry in result.get("results") or ():
            if not isinstance(entry, dict):
                continue
            title = (entry.get("title") or "").strip()
            snippet = (entry.get("snippet") or "").strip()
            text = "\n\n".join(p for p in (title, snippet) if p)
            added += self._store(
                url=entry.get("url"),
                text=text,
                title=title,
                tool="web_search",
            )
        return added

    def _store(self, url, text, title: str, tool: str) -> int:
        if not isinstance(url, str) or not isinstance(text, str):
            return 0
        key = normalize_url(url)
        if not key:
            return 0

        cleaned = redact_output_text(text).strip()
        if len(cleaned) < MIN_DOCUMENT_CHARS:
            return 0
        cleaned = cleaned[:MAX_DOCUMENT_CHARS]

        existing = self.documents.get(key)
        if existing is not None and len(existing.text) >= len(cleaned):
            return 0
        if existing is None and len(self.documents) >= MAX_DOCUMENTS:
            # Counted rather than logged per drop: a run that hits this is
            # hitting it repeatedly, and the count is what the trace line
            # at the end of the stage reports.
            self.dropped += 1
            return 0

        self.documents[key] = SourceDocument(
            url=key, text=cleaned, title=title or (existing.title if existing else ""),
            tool=tool, retrieved_at=_now(),
        )
        return 1

    # -- read ------------------------------------------------------------

    def get(self, url: str):
        """The document held for `url`, or None. Normalizes first, so a
        caller passing the model's spelling gets the fetched page."""
        return self.documents.get(normalize_url(url))

    def text_for(self, url: str) -> str:
        document = self.get(url)
        return document.text if document is not None else ""

    def __len__(self) -> int:
        return len(self.documents)

    def __bool__(self) -> bool:
        # Explicit, because __len__ alone would make an empty corpus
        # falsy and every `if corpus:` guard in the orchestrator would
        # then mean "if anything was fetched" rather than "if a corpus is
        # being collected at all". Those are different questions and the
        # first one is never the one being asked.
        return True

    def artifact_entries(self) -> list[dict]:
        """Every document, newest spelling of each URL, sorted for a
        stable diff between runs."""
        return [self.documents[key].as_artifact()
                for key in sorted(self.documents)]
