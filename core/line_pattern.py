"""
core/line_pattern.py

The one place a model-supplied pattern is compiled and matched (ROADMAP_v3
§49, SS8). A monitor session wakes the agent when a line of a program's
output matches a pattern the MODEL wrote -- two untrusted inputs meeting
inside the harness's own process, on a thread beside the TUI's.

WHY NOT `re`. Measured on CPython 3.13 (batch 93): `re.match(r"(a+)+$",
"a" * 25 + "b")` took 1.88 s, and a sibling thread's ticker stalled for
1.98 s of it. `re` holds the GIL for the whole match, so a backtracking
pattern freezes EVERY thread, and each extra character doubles the time. No
per-line budget enforced from inside the same process can bound that.

re2 matches in time linear in the input, releases the GIL while it runs,
and refuses the constructs that make backtracking possible: a backreference
or a lookaround is a compile error, and the error is what the agent is told.

THREE GUARDS, each measured against google-re2 1.1.20251105:

  provenance   another package ships a module named `re2` that falls back to
               `re` for what it cannot handle, which would bring the freeze
               back under a name that looks safe. The module must be the one
               the `google-re2` distribution installed, or this module
               refuses to import. Checked by locating that distribution's
               own `re2/__init__.py` (0.6 ms), not by
               `packages_distributions()`, which scans every installed
               package (593 ms, on a path every launch takes).
  log_errors   OFF. re2's C++ side reports every compile error on stderr
               through Abseil, and anything written to stderr while Textual
               is up paints over the screen.
  max_mem      set explicitly to re2's own 8 MiB default, so the bound on
               what one compiled pattern may cost is a decision rather than
               a library default that can move. A pattern past it is refused
               ("pattern too large").

Imports nothing first-party, so a session backend or a tool can use it
without an import cycle.
"""

from __future__ import annotations

import importlib.metadata
import os

import re2

_DISTRIBUTION = "google-re2"
_MAX_MEM = 8 << 20


class PatternError(ValueError):
    """A pattern re2 will not compile, carrying re2's own reason."""


def _assert_provenance() -> None:
    """Refuse a `re2` module that the `google-re2` distribution did not
    install. Raises ImportError, because a module that cannot vouch for its
    engine should fail where it is imported rather than at the first match."""
    try:
        dist = importlib.metadata.distribution(_DISTRIBUTION)
        expected = os.path.realpath(str(dist.locate_file("re2/__init__.py")))
    except importlib.metadata.PackageNotFoundError:
        expected = None
    actual = os.path.realpath(getattr(re2, "__file__", "") or "")
    if expected is None or expected != actual:
        raise ImportError(
            f"core/line_pattern.py needs the `re2` module from the "
            f"{_DISTRIBUTION} distribution, and the one imported is "
            f"{actual or 'unknown'}. Another package's `re2` can fall back "
            f"to Python's `re`, which freezes every thread on a backtracking "
            f"pattern. Install it with: pip install {_DISTRIBUTION}")


_assert_provenance()


def _options():
    options = re2.Options()
    options.log_errors = False
    options.max_mem = _MAX_MEM
    return options


def _reason(error: Exception) -> str:
    """re2's message as text. The wrapper raises it as bytes."""
    detail = error.args[0] if error.args else ""
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", "replace")
    return str(detail) or "the pattern could not be compiled"


class LinePattern:
    """A compiled monitor pattern. `search` is the only question asked."""

    __slots__ = ("source", "_compiled")

    def __init__(self, source: str, compiled) -> None:
        self.source = source
        self._compiled = compiled

    def search(self, line: str) -> bool:
        """Whether the pattern occurs anywhere in *line*."""
        return self._compiled.search(line) is not None


def compile_pattern(pattern) -> LinePattern:
    """Compile *pattern* for line matching, or raise PatternError.

    Total over whatever the model sent: the value arrives from a tool call's
    params, so a non-string is refused here rather than raising a TypeError
    out of re2.
    """
    if not isinstance(pattern, str) or not pattern:
        raise PatternError("a pattern must be a non-empty string")
    try:
        return LinePattern(pattern, re2.compile(pattern, _options()))
    except re2.error as e:
        raise PatternError(_reason(e)) from None


def validate(pattern) -> str | None:
    """Why *pattern* cannot be a monitor pattern, or None when it can."""
    try:
        compile_pattern(pattern)
    except PatternError as e:
        return str(e)
    return None
