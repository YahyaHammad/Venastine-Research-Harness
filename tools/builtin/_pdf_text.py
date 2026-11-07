"""
tools/builtin/_pdf_text.py

A PDF's text, read in the killable child `tools/isolation.py` runs (batch
240, the owner's choice of both the library and the process).

pypdf, NOT markitdown[pdf], AND THE CHOICE WAS MEASURED. Three arXiv papers
-- Attention (one column), BERT (two columns, ACL layout) and the 77-page
Llama 2 report -- each checked for whole running sentences:

  * pypdf 6.21.4: every probe sentence intact on Attention and BERT; 0.8%
    of words run together on Llama 2; 1.2 s, 0.5 s and 4.2 s. 4.1 MB, no
    dependencies.
  * markitdown 0.1.9 [pdf]: words run together on every paper (22% on
    Llama 2), and BERT's two columns came out as a Markdown table of word
    fragments. 188 MB of dependencies, onnxruntime and numpy among them.

IN A CHILD PROCESS, because PDF parsers have a long history of hostile files
that make them spin or exhaust memory, and a thread cannot be stopped (the
argument `tools/isolation.py` makes at length). The child is under
`tool_compute_timeout_s` and, on POSIX, the CPU and address-space rlimits.

pypdf IS IMPORTED AT MODULE TOP, on purpose. The child imports this module
BEFORE a shared hosted deployment confines it, and the import is what needs
the filesystem; importing inside the handler would run after the walls go
up. The parent imports this module only when a PDF arrives.

REDACTED HERE, BEFORE THE WINDOW IS CUT (§52's RB1). The child sees the
whole text exactly once, and a credential straddling the edge of the window
it returns would be invisible to a redactor run on the window afterwards.
Positions are therefore positions in redacted text, the one space
`fetch_url` speaks (RB3).

WHAT CROSSES BACK. Only the window asked for, never the whole text:
`tools/isolation` refuses a result over 4 MB, and a long report's text is
larger than the window `fetch_url` holds anyway.
"""

from __future__ import annotations

import base64
import io
import re

from pypdf import PdfReader

from safety.policy_enforcement import redact_output_text
from tools.builtin._page_text import text_digest

#: Ligatures a PDF's fonts encode as one glyph. Expanded, so "ﬁnetune"
#: is "finetune" to anything that searches the text.
_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
              "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}

# A word broken across lines -- "representa-" / "tion" -- joined back up.
# Only before a lower-case letter: "Rad-" / "ford" is a name split by the
# column, while "BERT-" / "Base" is a real hyphen.
_LINE_HYPHEN = re.compile(r"(\w)-\n(?=[a-z])")
_SPACES = re.compile(r"[ \t]+")


def _clean(text: str) -> str:
    for glyph, letters in _LIGATURES.items():
        text = text.replace(glyph, letters)
    text = _LINE_HYPHEN.sub(r"\1", text)
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pdf_window(params: dict) -> dict:
    """The child's handler: `{"data": base64, "start": int, "limit": int}`.

    Returns `{"text", "total", "digest", "pages", "title", "redacted"}` --
    the window `[start, start + limit)` of the whole document's text, with
    `[page N]` before each page so a claim can cite one -- or `{"error"}`.
    """
    try:
        reader = PdfReader(io.BytesIO(base64.b64decode(params["data"])))
        if reader.is_encrypted and not reader.decrypt(""):
            return {"error": "this PDF is encrypted with a password."}
        parts = []
        for number, page in enumerate(reader.pages, start=1):
            parts.append(f"[page {number}]\n{_clean(page.extract_text() or '')}")
    except Exception as exc:  # noqa: BLE001 -- a hostile file raises anything
        return {"error": f"this PDF could not be read ({type(exc).__name__}: "
                         f"{str(exc)[:200]})."}
    whole = "\n\n".join(parts)
    clean = redact_output_text(whole)
    start = max(int(params.get("start", 0)), 0)
    limit = max(int(params.get("limit", 0)), 0)
    title = None
    try:
        meta = reader.metadata
        if meta is not None and isinstance(meta.title, str) and meta.title.strip():
            title = meta.title.strip()[:300]
    except Exception:  # noqa: BLE001 -- metadata is optional and often broken
        title = None
    return {
        "text": clean[start:start + limit],
        "total": len(clean),
        "digest": text_digest(clean),
        "pages": len(parts),
        "title": title,
        "redacted": clean != whole,
        # A scanned PDF has pages and no text layer: say so rather than
        # return a document of page markers.
        "no_text": not any(part.split("\n", 1)[-1].strip() for part in parts),
    }


def encode(data: bytes) -> str:
    """The PDF's bytes as the child's JSON request carries them."""
    return base64.b64encode(data).decode("ascii")
