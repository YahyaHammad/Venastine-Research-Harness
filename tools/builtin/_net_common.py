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


class TTLCache:
    """A tiny time-boxed cache, keyed by whatever string a caller builds.

    Expiry is checked on READ rather than swept: these caches hold a
    handful of entries for the length of a research run, so a sweep would
    be machinery for a problem neither tool has.

    A stale entry is dropped as it is read, which is what makes a repeated
    query after the TTL a real request again rather than a permanent miss
    against a growing dict.
    """

    def __init__(self, ttl_s: float) -> None:
        self.ttl_s = ttl_s
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

    def set(self, key: str, value: Any) -> None:
        self._entries[key] = (time.time(), value)

    def clear(self) -> None:
        """Drop everything. For tests, which must not inherit a previous
        test's answers -- the same reason conftest isolates the stores."""
        self._entries.clear()
