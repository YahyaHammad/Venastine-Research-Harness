"""
test_net_common.py

`tools/builtin/_net_common.py` -- the TTL cache the three network-backed
built-in tools share. It had NO direct tests before ROADMAP_v3 §52: only a
conftest fixture that clears it between tests, added in batch 105 when
paging made a leaked entry able to make an assertion pass for the wrong
reason.

§52 (RB5, RB6) gave it a second bound. The TTL was always about FRESHNESS
and expired only in `get`, so an entry nothing read again was never dropped
-- measured in batch 106, 100 `fetch_url` fetches held 6.3 MB and the cache
released none of it. A sweep on write makes the TTL mean something for
memory too, and `MAX_ENTRIES` makes the bound absolute rather than dependent
on whether anyone happens to read a key.

WHAT WOULD MAKE THIS FILE VACUOUS:

  * Testing the sweep with `time.sleep`. A 300-second TTL cannot be waited
    out, and sleeping for a shortened one makes the suite slow and flaky for
    no gain. Time is injected, and the entries are aged by rewriting their
    stored timestamps -- the same thing the passage of time would do.
  * Asserting only that the cache holds `max_entries` items. That passes
    whichever item was thrown away. WHICH one is evicted is the decision
    (RB6, oldest first), and evicting the newest would be actively harmful:
    the freshest entry is the one a caller is about to read.
  * Testing the class and not its callers. The cap only bounds anything if
    the three real caches actually have one, so the last class asserts that
    against the modules themselves.
"""

import time

import pytest

from tools.builtin import arxiv, fetch_url, web_search
from tools.builtin._net_common import MAX_ENTRIES, TTLCache

TTL = 300.0


def age(cache, key, seconds):
    """Rewrite one entry's timestamp, as the passage of time would.

    Reaches into `_entries` deliberately: the alternative is injecting a
    clock into a nine-line class, which is more machinery than the thing
    being tested.
    """
    stored_at, value = cache._entries[key]
    cache._entries[key] = (stored_at - seconds, value)


# ===========================================================================
# ---- What did not change --------------------------------------------------
# ===========================================================================

class TestTheOriginalContract:
    """Two other tools depend on this and neither asked for §52."""

    def test_a_stored_value_comes_back(self):
        cache = TTLCache(TTL)
        cache.set("k", ["a", "b"])
        assert cache.get("k") == ["a", "b"]

    def test_a_missing_key_is_none(self):
        assert TTLCache(TTL).get("nope") is None

    def test_reading_an_expired_entry_drops_it(self):
        """The load-bearing half of expire-on-read: it is what makes a
        repeated query after the TTL a real request again rather than a
        permanent miss against a growing dict. The sweep is ADDITIONAL, and
        a cache that only swept would keep serving a stale entry until
        something else happened to write."""
        cache = TTLCache(TTL)
        cache.set("k", "v")
        age(cache, "k", TTL + 1)

        assert cache.get("k") is None
        assert "k" not in cache._entries

    def test_an_entry_inside_the_ttl_survives_a_read(self):
        cache = TTLCache(TTL)
        cache.set("k", "v")
        age(cache, "k", TTL - 1)

        assert cache.get("k") == "v"

    def test_clear_drops_everything(self):
        cache = TTLCache(TTL)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert cache._entries == {}

    def test_re_storing_a_key_replaces_its_value(self):
        cache = TTLCache(TTL)
        cache.set("k", "old")
        cache.set("k", "new")
        assert cache.get("k") == "new"
        assert len(cache._entries) == 1


# ===========================================================================
# ---- RB5: the sweep on write ----------------------------------------------
# ===========================================================================

class TestTheSweepOnWrite:

    def test_writing_drops_an_expired_entry_nobody_read(self):
        """The defect in one test. Before §52 this entry stayed resident
        until something read that exact key, which for a page nobody paged
        was never."""
        cache = TTLCache(TTL)
        cache.set("stale", "x" * 1000)
        age(cache, "stale", TTL + 1)

        cache.set("fresh", "y")

        assert "stale" not in cache._entries
        assert cache.get("fresh") == "y"

    def test_it_drops_every_expired_entry_and_not_just_one(self):
        cache = TTLCache(TTL)
        for i in range(10):
            cache.set(f"k{i}", i)
        for i in range(0, 10, 2):
            age(cache, f"k{i}", TTL + 1)

        cache.set("trigger", "t")

        assert sorted(cache._entries) == [
            "k1", "k3", "k5", "k7", "k9", "trigger"]

    def test_it_keeps_everything_still_inside_the_ttl(self):
        cache = TTLCache(TTL)
        for i in range(5):
            cache.set(f"k{i}", i)
            age(cache, f"k{i}", TTL - 1)

        cache.set("trigger", "t")

        assert len(cache._entries) == 6

    def test_reading_does_not_sweep(self):
        """Deliberate, and worth pinning: `get` still touches exactly the
        one key it was asked for. A read that swept would make the cost of a
        cache hit depend on how many other entries happen to be stale."""
        cache = TTLCache(TTL)
        cache.set("a", 1)
        cache.set("b", 2)
        age(cache, "a", TTL + 1)
        age(cache, "b", TTL + 1)

        assert cache.get("a") is None
        assert "b" in cache._entries, "reading 'a' swept 'b' as well"


# ===========================================================================
# ---- RB6: the entry cap ---------------------------------------------------
# ===========================================================================

class TestTheEntryCap:

    def test_the_cap_holds(self):
        cache = TTLCache(TTL, max_entries=4)
        for i in range(20):
            cache.set(f"k{i}", i)

        assert len(cache._entries) == 4

    def test_the_OLDEST_entry_is_the_one_evicted(self):
        """Not just that something was dropped. Evicting the newest would
        keep the cap and destroy the point -- the freshest entry is the one
        a caller is most likely to ask for next."""
        cache = TTLCache(TTL, max_entries=3)
        for name in ("first", "second", "third"):
            cache.set(name, name)

        cache.set("fourth", "fourth")

        assert sorted(cache._entries) == ["fourth", "second", "third"]
        assert cache.get("first") is None

    def test_re_storing_a_key_makes_it_the_newest(self):
        """The `pop` before the assignment. Without it a re-stored key keeps
        its original insertion position, so the entry just refreshed is the
        next one thrown away -- which for `fetch_url` means a re-fetched
        document losing its held body to the fetch after it."""
        cache = TTLCache(TTL, max_entries=3)
        for name in ("a", "b", "c"):
            cache.set(name, name)

        cache.set("a", "a-again")     # a is now the NEWEST, not the oldest
        cache.set("d", "d")           # evicts one

        assert cache.get("a") == "a-again", "the refreshed entry was evicted"
        assert cache.get("b") is None, "b was the oldest and should have gone"

    def test_a_full_cache_of_stale_entries_makes_room_for_free(self):
        cache = TTLCache(TTL, max_entries=3)
        for name in ("old1", "old2", "old3"):
            cache.set(name, name)
            age(cache, name, TTL + 1)

        cache.set("live", "live")

        assert sorted(cache._entries) == ["live"]

    def test_a_stale_entry_yields_its_slot_before_a_live_one_is_evicted(self):
        """The sweep runs BEFORE the cap, and this is the arrangement that
        can tell. The test above cannot: with every entry stale, evicting
        first takes a stale one and sweeping second removes the rest, so
        both orders end up in the same place.

        Here the OLDEST entry is live and a younger one is stale. Sweeping
        first frees the slot the stale entry held, the cap never fires, and
        the live entry survives. Evicting first would take `live_old`
        because it is oldest, then sweep the stale one anyway -- discarding
        something live to make room that was already there.
        """
        cache = TTLCache(TTL, max_entries=2)
        cache.set("live_old", 1)
        cache.set("stale", 2)
        age(cache, "stale", TTL + 1)

        cache.set("new", 3)

        assert sorted(cache._entries) == ["live_old", "new"]

    def test_a_cap_of_one_holds_only_the_newest(self):
        cache = TTLCache(TTL, max_entries=1)
        cache.set("a", 1)
        cache.set("b", 2)

        assert list(cache._entries) == ["b"]

    def test_the_cap_does_not_fire_below_itself(self):
        """The boundary. A cache holding exactly `max_entries` has evicted
        nothing -- an off-by-one here would silently throw away a live entry
        on every write in a full cache."""
        cache = TTLCache(TTL, max_entries=5)
        for i in range(5):
            cache.set(f"k{i}", i)

        assert len(cache._entries) == 5
        assert cache.get("k0") == 0


# ===========================================================================
# ---- The callers, since a cap nobody passes bounds nothing ----------------
# ===========================================================================

class TestTheThreeRealCachesAreBounded:

    @pytest.mark.parametrize("module,attr", [
        (fetch_url, "_body_cache"),
        (arxiv, "_cache"),
        (web_search, "_cache"),
    ], ids=["fetch_url", "arxiv", "web_search"])
    def test_it_has_a_cap(self, module, attr):
        cache = getattr(module, attr)
        assert cache.max_entries == MAX_ENTRIES
        assert cache.max_entries > 0

    def test_the_cap_is_above_what_one_pass_can_reach(self):
        """RB6's derivation, as an assertion rather than a comment. A pass
        makes at most `MAX_ITERATIONS` tool calls and a redirecting
        `fetch_url` stores two keys per body, so 2 * MAX_ITERATIONS is the
        most one pass can put in. An eviction inside a pass would turn a
        page-2 request into a re-fetch, which is §51's spliced document --
        trading a bounded memory cost for an unbounded correctness one."""
        import config
        assert MAX_ENTRIES > 2 * config.MAX_ITERATIONS

    def test_a_body_cache_of_capped_pages_stays_bounded(self):
        """End to end on the caller that made this necessary, in the units
        that matter. `MAX_CONTENT_BYTES` bounds one entry and this bounds
        the count, so the product is the ceiling -- and PG6's refusal to
        raise the byte cap is what makes the first half true."""
        cache = fetch_url._body_cache
        cache.clear()
        body = "x" * fetch_url.MAX_CONTENT_BYTES
        for i in range(MAX_ENTRIES + 50):
            cache.set(f"https://example.com/{i}", (f"u{i}", body, False))

        assert len(cache._entries) == MAX_ENTRIES
        ceiling = MAX_ENTRIES * fetch_url.MAX_CONTENT_BYTES
        assert ceiling < 16 * 1024 * 1024
        cache.clear()


# ===========================================================================
# ---- That the class is still usable as the other two tools use it ---------
# ===========================================================================

class TestTheOtherTwoToolsAreUnaffected:
    """`arxiv` and `web_search` hold result lists, not bodies, and neither
    asked for any of this. Their observable behaviour has to be what it was.
    """

    def test_a_repeated_query_is_served_from_the_cache(self):
        cache = TTLCache(TTL)
        cache.set("quantum computing|None|5|relevance", [{"title": "a"}])

        assert cache.get("quantum computing|None|5|relevance") == [
            {"title": "a"}]

    def test_a_different_key_is_a_miss(self):
        cache = TTLCache(TTL)
        cache.set("a|1", ["x"])
        assert cache.get("a|2") is None

    def test_the_clock_is_the_real_one(self):
        """No injected time in production: an entry stored now is live now.
        Cheap, and it catches a refactor that made `set` stamp entries with
        something other than `time.time()` -- which every test above would
        survive, since they all age relative to whatever was stored."""
        cache = TTLCache(TTL)
        before = time.time()
        cache.set("k", "v")
        stored_at, _ = cache._entries["k"]

        assert before <= stored_at <= time.time()
