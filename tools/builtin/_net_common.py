"""
tools/builtin/_net_common.py

What the two network-backed search tools share. `_math_common.py`'s
precedent, one directory over: a private module the built-in tools import
from, never registered and never advertised.

TWO THINGS NOW. The first is the in-process response cache: `web_search.py`
and `arxiv.py` each carried a `_cache_get` / `_cache_set` pair that was
identical apart from the type of the value being stored, plus a
`CACHE_TTL_S = 300` each, plus a bare dict each. Three copies of an expiry
rule is how one of them comes to be measured in minutes.

The second is `ThirdPartyText` (batch 156, ROADMAP_v3 §73), which is where
the two search tools now bound and redact the strings they did not write.
It is here rather than in either tool for the reason the cache is: both
were doing the same thing to the same kind of value, and the one that got
it wrong got it wrong in the half the other had already fixed.

There are THREE callers now -- `fetch_url.py` joined them in ROADMAP_v3 §51 --
and the third is why §52 gave this class a size bound as well as a time one.

WHAT IS DELIBERATELY *NOT* SHARED. Each tool keeps its OWN instance, so the
two key spaces stay separate -- a `TTLCache` shared between them would let
an arXiv query and a web query collide on an equal key string, which is a
correctness bug rather than a tidiness one. Their `@field_validator`s
(`strip_keywords`, `strip_query`) are also left alone: they are three lines
each, they differ in the message the model is shown, and folding them into
a factory would make the params class harder to read to save two lines.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from safety.policy_enforcement import redact_output_text

logger = logging.getLogger(__name__)

# ROADMAP_v3 §52 (RB6). How many entries any one of these may hold.
#
# Derived rather than picked: `config.MAX_ITERATIONS` is 50, so one pass can
# make at most 50 tool calls, and a `fetch_url` whose URL redirects stores
# TWO keys for one body (the requested spelling and the answering one). 100
# keys is therefore the most a single pass can reach, and this sits above it
# so that the cap bounds a PROCESS -- which runs many passes and many runs --
# without ever biting inside one.
#
# That "without ever biting" is the requirement, not a nicety. A `fetch_url`
# page-2 request that misses degrades to a re-fetch, which is exactly the
# spliced-document failure §51 measured and built PG5 to prevent, so an
# eviction that fires in ordinary use would trade a bounded memory cost for
# an unbounded correctness one.
#
# In entries and not in bytes, because a generic cache cannot measure the
# size of what a caller hands it. For the caller that made this necessary the
# two are interchangeable anyway: `fetch_url`'s MAX_CONTENT_BYTES holds each
# body to ~64 KB whatever the encoding, so PG6's refusal to raise that cap is
# what turns this count into a memory bound at all.
MAX_ENTRIES = 128


# ROADMAP_v3 §73 (A58). How long a URL this harness is willing to SHOW the
# model may be.
#
# DERIVED, NOT PICKED, and the derivation is the argument for dropping an
# over-long URL rather than cutting it: `fetch_url` already bounds the URL
# the model may ASK for, at `max_length=400` on `FetchURLParams.url`.
# Measured (batch 156), a 401-character URL comes back from fetch_url's own
# gate as an "Invalid arguments" refusal while a 400-character one is
# accepted. So a longer URL is one THIS HARNESS refuses to take, and
# offering it in a search result is offering a route to nowhere.
#
# ONE DEFINITION: `fetch_url` reads it from here rather than spelling 400
# again. The two were the same number in two files, which is the state a
# pair of numbers is in just before it stops being a pair.
MAX_URL_CHARS = 400


class ThirdPartyText:
    """One tool call's worth of strings nobody here wrote, REDACTED and
    THEN cut.

    ORDER IS THE WHOLE POINT, and it is RB1's point restated for the two
    tools RB1 did not reach. `fetch_url` redacts its whole body above both
    the hold and the slice, because a pattern only matches what it can see
    whole; `web_search` and `arxiv_search` cut first and let
    `registry.dispatch`'s `check_output_policy` redact the piece that was
    kept. A fixed-length credential straddling such a cut is then invisible
    to both halves -- the kept prefix is not the token and the dropped
    suffix is gone. Measured (batch 156) at all three of their cut points:
    a `ghp_` token survived post-hoc redaction at 36 of the 39 offsets it
    could straddle, and redacting the whole value first caught every one.

    TWO KINDS OF VALUE, because a cut means different things to them (A58).
    `text()` cuts and flags: half an abstract is still an abstract, and PG4
    says the flag is owed. `url()` DROPS and flags: half a URL is not a
    URL, it is a fetchable-looking string that is not the resource, and
    `pdf_url` and arXiv's `full_*_url` pair exist precisely to be the route
    the model takes to recover what was cut. A broken route offered as a
    route is worse than an absent one, which the flag makes visible.

    TOTAL OVER WHATEVER THE PROVIDER EMITTED, for the reason `tools/base.py`
    gives about tool arguments: a key that is present with a wrong-typed
    value is not an absent key, and `r.get("title", "(untitled)")` returns
    None for `{"title": null}` rather than the default. Both accessors
    answer None for a non-string instead of raising, and the caller decides
    what an absent value means -- which is a decision the two tools make
    differently and should (A61).

    ONE WARNING PER CALL, not per value (#49, RB2). Redacting here means
    `check_output_policy` finds nothing left to alter, so the obligation to
    say that something WAS altered moves here with the redaction; and a
    feed with twenty-five authors must not produce twenty-five warning
    lines. The count is carried on the instance and `report()` spends it.
    The match is never echoed, for the reason `check_output_policy` and
    `fetch_url._redacted` both state: this function exists because
    something credential-shaped was found, so quoting it to say so would
    put it in app.log and every sink a WARNING reaches.
    """

    def __init__(self, tool: str) -> None:
        self._tool = tool
        self._redacted = 0

    def text(self, value: Any, cap: int) -> tuple:
        """`(text, truncated)` -- `value` redacted and THEN cut to `cap`.

        `(None, False)` for anything that is not a string, so a caller can
        tell "the provider did not give me this" from "it gave me an empty
        one" and choose its own substitute.

        THE FLAG COUNTS REDACTED CHARACTERS, which is RB3's consequence
        arriving here: redaction can SHRINK a value past the cap, and when
        it does there is nothing further the cut is withholding. A flag
        measured on the raw length would claim there is.
        """
        if not isinstance(value, str):
            return None, False
        clean = self._clean(value)
        return clean[:cap], len(clean) > cap

    def url(self, value: Any, cap: int = MAX_URL_CHARS) -> tuple:
        """`(url, withheld)` -- a URL past `cap` is DROPPED, not cut (A58).

        `(None, False)` for a non-string or an empty one: nothing was
        withheld, there was nothing there. `(None, True)` says a URL was
        found and is not being shown, which is the fact the flag carries.
        """
        if not isinstance(value, str) or not value:
            return None, False
        clean = self._clean(value)
        if len(clean) > cap:
            return None, True
        return clean, False

    def _clean(self, value: str) -> str:
        clean = redact_output_text(value)
        if clean != value:
            self._redacted += 1
        return clean

    def report(self) -> None:
        """The one warning this call owes (#49), or silence."""
        if self._redacted:
            logger.warning(
                "Output policy altered %s result: credentials replaced in "
                "%d value(s) before they were cut.",
                self._tool, self._redacted)


class TTLCache:
    """A tiny time-boxed cache, keyed by whatever string a caller builds.

    TWO BOUNDS, ANSWERING DIFFERENT QUESTIONS. The TTL is about FRESHNESS: a
    stale entry is dropped as it is read, which is what makes a repeated
    query after the TTL a real request again rather than a permanent miss
    against a growing dict. `max_entries` is about SIZE, and it is the only
    thing that bounds a cache nothing reads a second time.

    THE SWEEP ON WRITE IS §52 (RB5), AND THE REASON THIS DOCSTRING USED TO
    GIVE FOR NOT HAVING ONE IS NOW FALSE. It said a sweep "would be machinery
    for a problem neither tool has" -- true of two callers holding result
    lists, and not of the third, which holds bodies. Measured (batch 106):
    100 fetches of a capped page held 6.3 MB and not one entry was ever
    dropped, because expiry ran only in `get` and nothing read those keys
    again. Expiring on read alone means the TTL bounds staleness and nothing
    whatever bounds memory.

    READ IS DELIBERATELY UNCHANGED. `get` still expires the one key it
    touches, because that path is what makes a post-TTL query a real request
    again; the sweep is additional, not a replacement, and a cache that only
    swept would keep serving a stale entry until someone else wrote.
    """

    def __init__(self, ttl_s: float, max_entries: int = MAX_ENTRIES) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._entries: dict[str, tuple[float, Any]] = {}
        # LOCKED BECAUSE THE REGISTRY ALREADY SAYS IT IS (batch 117).
        # `spawn_subagent` is the one tool with `parallel=True`, and the
        # comment beside that field claims "everything the child's run
        # touches was made concurrency-safe first -- the budget's lock, the
        # serialised question, the per-future authorization". The child
        # reaches this cache through `arxiv_search` and `fetch_url`, and it
        # was the one item on that list without a lock.
        #
        # Single dict operations are atomic under CPython, so a corrupt
        # dict was never the risk. `set` is a read-modify-write -- pop,
        # assign, sweep, then evict while over the cap -- so two children
        # storing at once could lose one entry and evict in an order
        # neither asked for. Mirrors `GrantBudget.take()`, which is the
        # pattern this module should have followed from the start.
        #
        # NOT reentrant, and `_sweep` is the reason: it is called from
        # `set` with the lock already held, so it must never take it.
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            stored_at, value = entry
            if time.time() - stored_at > self.ttl_s:
                self._entries.pop(key, None)
                return None
            return value

    def _sweep(self, now: float) -> None:
        """Drop everything already expired. Write-side only (§52, RB5).

        CALLED WITH `self._lock` HELD, and it must not take it: the lock is
        a plain `Lock`, so re-entering would deadlock the caller.
        """
        stale = [key for key, (stored_at, _) in self._entries.items()
                 if now - stored_at > self.ttl_s]
        for key in stale:
            del self._entries[key]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._set_locked(key, value)

    def _set_locked(self, key: str, value: Any) -> None:
        now = time.time()
        # POPPED FIRST so a re-store moves the key to the END of the dict.
        # Assigning to an existing key keeps its original insertion position,
        # which would make the freshest entry the next one evicted -- and the
        # freshest entry is the one a caller is most likely to read next.
        self._entries.pop(key, None)
        self._entries[key] = (now, value)
        self._sweep(now)
        # Insertion order IS age order, since every entry is stamped as it is
        # written and the TTL is uniform. So the oldest key is simply the
        # first one, and no second ordering has to be kept consistent.
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)))

    def clear(self) -> None:
        """Drop everything. For tests, which must not inherit a previous
        test's answers -- the same reason conftest isolates the stores."""
        with self._lock:
            self._entries.clear()
