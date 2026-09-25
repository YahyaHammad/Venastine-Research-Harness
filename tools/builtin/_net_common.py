"""
tools/builtin/_net_common.py

What the two network-backed search tools share. `_math_common.py`'s
precedent, one directory over: a private module the built-in tools import
from, never registered and never advertised.

TODAY THAT IS ONE THING: the in-process response cache. `web_search.py` and
`arxiv.py` each carried a `_cache_get` / `_cache_set` pair that was
identical apart from the type of the value being stored, plus a
`CACHE_TTL_S = 300` each, plus a bare dict each. Three copies of an expiry
rule is how one of them comes to be measured in minutes.

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

import time
from typing import Any, Optional

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

    def get(self, key: str) -> Optional[Any]:
        entry = self._entries.get(key)
        if not entry:
            return None
        stored_at, value = entry
        if time.time() - stored_at > self.ttl_s:
            self._entries.pop(key, None)
            return None
        return value

    def _sweep(self, now: float) -> None:
        """Drop everything already expired. Write-side only (§52, RB5)."""
        stale = [key for key, (stored_at, _) in self._entries.items()
                 if now - stored_at > self.ttl_s]
        for key in stale:
            del self._entries[key]

    def set(self, key: str, value: Any) -> None:
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
        self._entries.clear()
