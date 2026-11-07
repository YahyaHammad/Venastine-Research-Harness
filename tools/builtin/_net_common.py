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

THE THIRD THING IS THE NETWORK METER (ROADMAP_v3 §53, batch 239), and it is
here for IO2's reason: this is the one module every network-backed tool
already imports, `core/reasoning/scholar.py` included, so a second home
would be a second place to forget. Three meters -- tools, the provider API
and MCP over HTTP -- each with its own ceiling over one rolling window,
counted into a small machine-wide SQLite ledger. Its section, at the end of
this file, says what is counted exactly, what is an estimate, and what
nothing here can see.

WHAT IS DELIBERATELY *NOT* SHARED. Each tool keeps its OWN instance, so the
two key spaces stay separate -- a `TTLCache` shared between them would let
an arXiv query and a web query collide on an equal key string, which is a
correctness bug rather than a tidiness one. Their `@field_validator`s
(`strip_keywords`, `strip_query`) are also left alone: they are three lines
each, they differ in the message the model is shown, and folding them into
a factory would make the params class harder to read to save two lines.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import cache
from typing import Any, Optional

import config
from safety.policy_enforcement import redact_output_text
from security import protected_paths

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


# ===========================================================================
# ---- The network meter (ROADMAP_v3 §53, batch 239) ------------------------
# ===========================================================================
#
# UNLIMITED BY DEFAULT, AND THEN NOTHING HERE DOES ANYTHING (IO1). Every
# ceiling in config.yaml ships at 0. A meter with no ceiling admits every
# call, counts nothing and never opens the ledger, so a harness nobody has
# configured behaves exactly as it did before this section existed and
# writes no file for it.
#
# WHAT IS COUNTED, AND HOW EXACTLY (IO3, IO4). A request and its response as
# HTTP/1.1 frames them: the request line, the headers, the body, the status
# line, the response headers and the body AS IT CAME OFF THE WIRE --
# compressed, and without chunk framing. Measured (batch 239) against a
# local server: a 50,000-byte page served gzipped counts as its 143 wire
# bytes, not its 50,000 decoded ones, because that is what an allowance is
# charged. Every redirect hop is counted, its body too.
#
# WHAT IS NOT (IO5), so a ceiling is never read as more than it is: TLS and
# TCP overhead, HTTP/2's header compression, bytes the kernel had already
# received when a read stopped early, sign-in and token-refresh traffic
# (`/connect` and the ChatGPT plan's refresh), a `stdio` MCP server's own
# sockets, and everything a shell command does. `web_search` is an
# ESTIMATE (IO15), kept apart so every figure that includes it can say so.

#: The three meters, in the order every listing shows them (IO1).
METERS = ("tools", "provider", "mcp")

#: What a person reads for each, in a refusal and in `/network`.
METER_LABELS = {"tools": "tool", "provider": "provider API", "mcp": "MCP"}

#: A ceiling is in DECIMAL megabytes (IO9), which is how a data allowance
#: is sold and how a carrier counts it.
MEGABYTE = 1_000_000

#: The ledger is written at most this often (IO12), and only when something
#: was counted. It is also how stale another process's figures may get
#: before this one reads the file again.
FLUSH_INTERVAL_S = 60.0

#: The ledger holds at most this many buckets per meter (IO10). A bucket is
#: the window divided by this, never narrower than MIN_BUCKET_S, so the
#: file's size is bounded by the bucket count and not by the traffic: 50 MB
#: a day and 50 GB a day both add to one integer per bucket.
LEDGER_BUCKETS = 1440
MIN_BUCKET_S = 60

LEDGER_FILENAME = "network.db"

#: IO15, amending IO3, the owner's choice. What one `web_search` costs on
#: the wire, per result requested. Measured (batch 239) through a counting
#: CONNECT proxy on two live queries: 85,746 bytes for five results and
#: 206,637 for ten, against 1,967 and 2,445 bytes of rows returned -- so
#: IO3's "the size of what it returned" would have undercounted the tool
#: by 44x and 85x. The higher ratio, rounded up, so the estimate leans
#: high rather than low. Re-measure when `ddgs` moves: which backends it
#: queries decides this number.
WEB_SEARCH_BYTES_PER_RESULT = 21_000

#: How long a ledger write waits on another process's lock. Short, because
#: a writer holds it for one tiny transaction.
_BUSY_TIMEOUT_S = 2.0

#: The WAL file is cut back to this after a checkpoint, so it cannot keep
#: the size of the busiest minute it ever saw.
_JOURNAL_LIMIT_BYTES = 256 * 1024

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS usage ("
    " meter TEXT NOT NULL,"
    " bucket INTEGER NOT NULL,"
    " bytes INTEGER NOT NULL,"
    " estimated INTEGER NOT NULL DEFAULT 0,"
    " PRIMARY KEY (meter, bucket)) WITHOUT ROWID")

# An ADD, not a replace: two processes flushing the same bucket must both
# count. This is why the ledger is SQLite rather than a JSON file written
# atomically -- an atomic replace of a read-modify-write still loses the
# other writer's increment.
_UPSERT = (
    "INSERT INTO usage (meter, bucket, bytes, estimated) VALUES (?, ?, ?, ?)"
    " ON CONFLICT (meter, bucket) DO UPDATE SET"
    " bytes = bytes + excluded.bytes,"
    " estimated = estimated + excluded.estimated")


def window_s() -> float:
    """The rolling window, in seconds, read at call time (IO7, IO8)."""
    return float(config.NETWORK_WINDOW_HOURS) * 3600.0


def bucket_width_s() -> int:
    """How wide one ledger bucket is for the current window (IO10)."""
    return max(MIN_BUCKET_S, int(window_s() // LEDGER_BUCKETS))


class NetworkLimitReached(Exception):
    """A call refused because its meter has no room for it (IO6).

    RAISED, NOT RETURNED, so that one boundary per consumer decides what it
    becomes, and no caller has to remember to check: `registry.dispatch`
    turns it into the tool result the model reads, `core/provider_errors`
    makes it a failure that is never retried and is described in these
    words even under an SDK that wrapped it, and `mcp_client` returns it in
    its one error shape. `reason` is the sentence all three show.
    """

    def __init__(self, reason: str, meter: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.meter = meter


@dataclass(frozen=True)
class MeterState:
    """One meter, as `/network`, the sidebar and a refusal read it."""

    name: str
    label: str
    ceiling: int                 # bytes; 0 means this meter is off
    used: int                    # bytes in the window, estimate included
    estimated: int               # the part of `used` that is an estimate
    window_s: float
    room_at: Optional[float]     # when room returns, if the meter is full
    ledger_note: Optional[str]   # why the ledger is not in use, if it is not

    @property
    def enabled(self) -> bool:
        return self.ceiling > 0

    @property
    def full(self) -> bool:
        return self.enabled and self.used >= self.ceiling


def format_bytes(n: int) -> str:
    """`12.3 MB` -- decimal units, to match the ceiling's (IO9)."""
    if n < 1000:
        return f"{n} B"
    if n < MEGABYTE:
        return f"{n / 1000:.1f} kB"
    if n < 1000 * MEGABYTE:
        return f"{n / MEGABYTE:.1f} MB"
    return f"{n / (1000 * MEGABYTE):.2f} GB"


def format_bytes_short(n: int) -> str:
    """`12M` -- the sidebar's spelling, which has 22 columns to work in."""
    if n < 1000:
        return f"{n}"
    if n < MEGABYTE:
        return f"{n / 1000:.0f}k"
    if n < 10 * MEGABYTE:
        return f"{n / MEGABYTE:.1f}M"
    if n < 1000 * MEGABYTE:
        return f"{n / MEGABYTE:.0f}M"
    return f"{n / (1000 * MEGABYTE):.1f}G"


def describe_window(seconds: float) -> str:
    """`24 h`, or `7 days` for a whole number of days past two."""
    hours = seconds / 3600.0
    if hours >= 48 and hours % 24 == 0:
        return f"{hours / 24:g} days"
    return f"{hours:g} h"


def _clock(at: float) -> str:
    """A local time, with the date only when it is not today."""
    when = time.localtime(at)
    if time.strftime("%Y-%m-%d", when) == time.strftime("%Y-%m-%d"):
        return time.strftime("%H:%M", when)
    return time.strftime("%Y-%m-%d %H:%M", when)


def _room_at(buckets: list, used: int, ceiling: int, outgoing: int,
             window: float, width: int) -> Optional[float]:
    """When a request sending `outgoing` bytes would next be admitted.

    The oldest bytes leave first, so walk the buckets oldest to newest and
    find the first whose departure brings the meter under the line. None
    when the request is larger than the whole ceiling, which no wait fixes.
    """
    need = max(outgoing, 0)
    if need > ceiling:
        return None
    remaining = used
    for bucket, nbytes, _ in buckets:
        remaining -= nbytes
        if remaining < ceiling and remaining + need <= ceiling:
            return bucket + width + window
    return None


class Ledger:
    """The machine-wide record the meters count into (IO10, IO12).

    ONE FILE FOR EVERY HARNESS PROCESS, `~/.config/venastine/network.db`,
    because IO7 rejects a reset per run and a restart is a run: a counter
    that lived in memory would be emptied by quitting, which is the
    short-run loophole IO7 names. Every subagent shares it for the same
    reason -- they run in this process and count into this instance.

    COUNTS, NEVER TRAFFIC, AND BOUNDED BY TIME RATHER THAN BY VOLUME. A row
    is (meter, bucket start, bytes, estimated bytes). Rows older than the
    window are deleted on every write and SQLite reuses the freed pages, so
    the file stops at about `LEDGER_BUCKETS` rows per meter whatever the
    traffic was.

    WRITTEN AT MOST ONCE A MINUTE, AND NEVER WHILE IDLE (IO12, the owner's
    concern about write wear). Counts gather in memory; the first one after
    a flush starts a one-shot timer, and the flush it fires is one
    transaction of a few rows. With nothing counted there is no timer and no
    write, and with every meter off there is no file at all. A refusal
    decision reads memory, never the disk; the file is read again only when
    the last read is more than a minute old.

    THE COSTS, STATED: another process's traffic can be up to a minute late
    here, and a crash loses at most the last minute of counts -- an
    UNDER-count, which the gap register records. A file that cannot be
    read or written does not turn the meter off: this process's own counts
    still enforce, and `note` says the ledger is not in use.
    """

    def __init__(self, path: Optional[str] = None, clock=time.time,
                 timers: bool = True) -> None:
        self._path = path
        self._clock = clock
        self._timers = timers
        # (meter, bucket) -> [bytes, estimated]. `_stored` is what the file
        # said at the last read, every process's flushed counts included;
        # `_pending` is this process's, not yet written. Disjoint by
        # construction: a flush moves pending into the file and reads the
        # file back, so nothing is ever in both.
        self._stored: dict = {}
        self._pending: dict = {}
        self._read_at: Optional[float] = None
        self._timer: Optional[threading.Timer] = None
        self._note: Optional[str] = None
        # NOT reentrant, for TTLCache's reason: every `_locked` method is
        # called with it held and must not take it again.
        self._lock = threading.Lock()

    # -- reading ---------------------------------------------------------

    def now(self) -> float:
        return self._clock()

    def path(self) -> str:
        """Resolved at CALL time, so a redirected HOME is honoured."""
        return self._path or os.path.join(
            protected_paths.user_config_dir(), LEDGER_FILENAME)

    @property
    def note(self) -> Optional[str]:
        return self._note

    def usage(self, meter: str, now: Optional[float] = None) -> list:
        """`[(bucket, bytes, estimated)]` inside the window, oldest first.

        A bucket STRADDLING the window's old edge is counted whole, so the
        figure errs high by at most one bucket (1/1440 of the window) and
        never low -- a ceiling should refuse a moment early rather than
        admit a moment late.
        """
        now = self._clock() if now is None else now
        with self._lock:
            if self._read_at is None or now - self._read_at >= FLUSH_INTERVAL_S:
                self._refresh_locked(now)
            edge = now - window_s() - bucket_width_s()
            merged: dict = {}
            for source in (self._stored, self._pending):
                for (name, bucket), (nbytes, estimated) in source.items():
                    if name == meter and bucket > edge:
                        slot = merged.setdefault(bucket, [0, 0])
                        slot[0] += nbytes
                        slot[1] += estimated
        return sorted((bucket, n, e) for bucket, (n, e) in merged.items())

    # -- counting --------------------------------------------------------

    def add(self, meter: str, nbytes: int, estimated: bool = False,
            now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        width = bucket_width_s()
        bucket = int(now // width * width)
        with self._lock:
            slot = self._pending.setdefault((meter, bucket), [0, 0])
            slot[0] += nbytes
            if estimated:
                slot[1] += nbytes
            self._schedule_locked()

    def flush(self) -> None:
        """Write what this process counted, then read every process's back.

        Called by the timer, at exit, and by tests. A failure keeps the
        counts pending for the next attempt and says why, once.
        """
        with self._lock:
            self._timer = None
            if not self._pending:
                return
            now = self._clock()
            try:
                self._write_locked(now)
            except (sqlite3.Error, OSError) as exc:
                self._fail_locked("written", exc)
                # Bounded even while the file is failing: a bucket that has
                # left the window is no use to anyone.
                edge = now - window_s() - 2 * bucket_width_s()
                self._pending = {key: value
                                 for key, value in self._pending.items()
                                 if key[1] > edge}

    def cancel(self) -> None:
        """Stop a scheduled flush. For tests, which replace the ledger."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # -- the file --------------------------------------------------------

    def _schedule_locked(self) -> None:
        if self._timers and self._timer is None:
            timer = threading.Timer(FLUSH_INTERVAL_S, self.flush)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _connect(self, create: bool) -> Optional[sqlite3.Connection]:
        path = self.path()
        if not create and not os.path.exists(path):
            return None
        if create:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        # Autocommit, so the one transaction below is spelled out rather
        # than opened implicitly by the first statement.
        db = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_S,
                             isolation_level=None)
        try:
            if create:
                # WAL with synchronous=NORMAL: a commit appends to the log
                # and does not wait on fsync; only a checkpoint does. The
                # last connection to close checkpoints and removes the log.
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                db.execute(f"PRAGMA journal_size_limit={_JOURNAL_LIMIT_BYTES}")
                db.execute(_SCHEMA)
        except BaseException:
            db.close()
            raise
        return db

    def _select_locked(self, db: sqlite3.Connection, edge: float) -> dict:
        rows = db.execute(
            "SELECT meter, bucket, bytes, estimated FROM usage"
            " WHERE bucket > ?", (int(edge),)).fetchall()
        return {(meter, bucket): [nbytes, estimated]
                for meter, bucket, nbytes, estimated in rows}

    def _refresh_locked(self, now: float) -> None:
        # Stamped even when the read fails, so a broken file is tried once
        # a minute rather than on every call.
        self._read_at = now
        edge = now - window_s() - bucket_width_s()
        try:
            db = self._connect(create=False)
            if db is None:
                self._stored = {}
                return
            try:
                self._stored = self._select_locked(db, edge)
            finally:
                db.close()
        except (sqlite3.Error, OSError) as exc:
            self._fail_locked("read", exc)

    def _write_locked(self, now: float) -> None:
        width = bucket_width_s()
        # Two buckets of margin past the window's edge: the straddling one
        # `usage` still counts, and one more so a clock that has moved on
        # between processes cannot delete a row another still reads.
        horizon = int(now - window_s()) - 2 * width
        rows = [(meter, bucket, nbytes, estimated)
                for (meter, bucket), (nbytes, estimated)
                in self._pending.items() if bucket > horizon]
        db = self._connect(create=True)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.executemany(_UPSERT, rows)
                db.execute("DELETE FROM usage WHERE bucket <= ?", (horizon,))
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise
            stored = self._select_locked(db, now - window_s() - width)
        finally:
            db.close()
        self._pending.clear()
        self._stored = stored
        self._read_at = now
        if self._note is not None:
            logger.warning("The network ledger at %s is in use again.",
                           self.path())
            self._note = None

    def _fail_locked(self, verb: str, exc: BaseException) -> None:
        note = (f"the ledger at {self.path()} could not be {verb} "
                f"({type(exc).__name__}: {exc}); counting this process only")
        if note != self._note:
            logger.warning("Network meter: %s.", note)
        self._note = note


#: The one ledger every meter in this process counts into. Replaced, never
#: mutated, by the tests that turn a meter on (tests/conftest.py).
LEDGER = Ledger()


def _ledger() -> Ledger:
    """The CURRENT ledger, looked up at call time so a test's swap is seen."""
    return LEDGER


@atexit.register
def _flush_at_exit() -> None:
    """The last minute's counts, written as the process leaves (IO12)."""
    with contextlib.suppress(Exception):
        _ledger().flush()


class Meter:
    """One of the three (IO1): a ceiling, and the arithmetic against it.

    THE CEILING IS READ AT CALL TIME from `config`, like every other
    setting a test may monkeypatch; nothing here caches it.

    ADMISSION, NEVER TRUNCATION (IO6, IO11). `admit` is asked before a call
    starts, with the size of what the call will SEND when that is known,
    and refuses when the meter is full or the send would pass the ceiling.
    A response already in flight is never cut: it completes and is counted,
    so a meter can end a window over its ceiling by at most one response --
    and a half-read body is the spliced-document failure PG5 exists to
    prevent.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.label = METER_LABELS[name]
        # Whether the last decision was a refusal, so a run of them warns
        # once (the transcript line IO6 owes) and not once per call.
        self._refusing = False
        self._lock = threading.Lock()

    def ceiling(self) -> int:
        megabytes = getattr(config, f"NETWORK_LIMIT_{self.name.upper()}_MB", 0)
        # Rounded, not truncated: 2.01 MB is 2,009,999.9999999998 bytes in
        # binary floating point, and a ceiling must not shrink in the
        # conversion.
        return int(round(float(megabytes or 0) * MEGABYTE))

    def enabled(self) -> bool:
        return self.ceiling() > 0

    def _measure(self) -> tuple:
        ceiling = self.ceiling()
        window = window_s()
        if not ceiling:
            return MeterState(self.name, self.label, 0, 0, 0, window,
                              None, None), []
        ledger = _ledger()
        buckets = ledger.usage(self.name)
        used = sum(nbytes for _, nbytes, _ in buckets)
        estimated = sum(part for _, _, part in buckets)
        room = (_room_at(buckets, used, ceiling, 0, window, bucket_width_s())
                if used >= ceiling else None)
        return MeterState(self.name, self.label, ceiling, used, estimated,
                          window, room, ledger.note), buckets

    def state(self) -> MeterState:
        return self._measure()[0]

    def admit(self, outgoing: int = 0) -> None:
        """Refuse, by raising NetworkLimitReached, or return and admit."""
        if not self.enabled():
            return
        state, buckets = self._measure()
        if state.used < state.ceiling and state.used + outgoing <= state.ceiling:
            with self._lock:
                self._refusing = False
            return
        reason = self._refusal(state, buckets, outgoing)
        with self._lock:
            first, self._refusing = not self._refusing, True
        if first:
            logger.warning("Network limit: %s", reason)
        raise NetworkLimitReached(reason, self.name)

    def record(self, nbytes: int, estimated: bool = False) -> None:
        if nbytes > 0 and self.enabled():
            _ledger().add(self.name, int(nbytes), estimated)

    def _refusal(self, state: MeterState, buckets: list, outgoing: int) -> str:
        ceiling, window = state.ceiling, describe_window(state.window_s)
        if outgoing > ceiling:
            text = (f"The {self.label} network limit cannot admit this "
                    f"request: it sends {format_bytes(outgoing)}, more than "
                    f"the whole {format_bytes(ceiling)} allowed per {window}.")
        elif state.used >= ceiling:
            text = (f"The {self.label} network limit is reached: "
                    f"{format_bytes(state.used)} of {format_bytes(ceiling)} "
                    f"used in the last {window}.")
        else:
            text = (f"The {self.label} network limit would be passed: this "
                    f"request sends {format_bytes(outgoing)} and "
                    f"{format_bytes(ceiling - state.used)} of "
                    f"{format_bytes(ceiling)} is left for the last {window}.")
        if state.estimated:
            text += (f" {format_bytes(state.estimated)} of the use is an "
                     f"estimate (web_search).")
        room = _room_at(buckets, state.used, ceiling, outgoing,
                        state.window_s, bucket_width_s())
        if room is not None:
            text += f" Room returns at about {_clock(room)}."
        text += (f" The ceiling is network_limit_{self.name}_mb in "
                 f"config.yaml.")
        if self.name != "provider":
            # The model reads this one. The limit is the user's, and the
            # shell is where an agent would reach next (gap register).
            text += " It is the user's limit: do not work around it."
        return text


TOOLS = Meter("tools")
PROVIDER = Meter("provider")
MCP = Meter("mcp")
_BY_NAME = {meter.name: meter for meter in (TOOLS, PROVIDER, MCP)}


def meter(name: str) -> Meter:
    return _BY_NAME[name]


def states() -> list:
    """Every meter's state, in METERS order."""
    return [_BY_NAME[name].state() for name in METERS]


# ---- Sizing an exchange ----------------------------------------------------
#
# DUCK-TYPED over httpx and httpx2, which share these attribute names: the
# tools' own calls are httpx, the provider SDKs and MCP are httpx2. Total
# over a wrong shape -- every accessor is guarded, and a part that cannot
# be read counts as zero -- because sizing a request must never be the
# reason it fails.

_HTTP11_REQUEST_TAIL = len(" HTTP/1.1\r\n")


def _headers_bytes(headers) -> int:
    raw = getattr(headers, "raw", None)
    if raw is not None:
        pairs = list(raw)
    elif hasattr(headers, "items"):
        pairs = list(headers.items())
    else:
        return 0
    total = 2                                   # the blank line
    for key, value in pairs:
        total += len(key) + len(value) + 4      # ": " and CRLF
    return total


def request_bytes(request) -> int:
    """One request as HTTP/1.1 frames it: line, headers and body."""
    if request is None:
        return 0
    try:
        target = getattr(request.url, "raw_path", b"/")
        total = len(request.method) + 1 + len(target) + _HTTP11_REQUEST_TAIL
        total += _headers_bytes(request.headers)
        length = request.headers.get("content-length")
        if length is not None:
            total += int(length)
        else:
            with contextlib.suppress(Exception):
                total += len(request.content)
        return total
    except Exception:  # noqa: BLE001 -- see the section comment
        return 0


def response_head_bytes(response) -> int:
    """The status line and headers. The body is counted as it is read."""
    try:
        reason = getattr(response, "reason_phrase", "") or ""
        line = len("HTTP/1.1 000 \r\n") + len(reason)
        return line + _headers_bytes(response.headers)
    except Exception:  # noqa: BLE001
        return 0


def _request_of(response):
    """The request a response answered, or None.

    A PROPERTY THAT RAISES rather than an attribute that may be missing:
    httpx's `Response.request` raises RuntimeError when no request was set,
    so `getattr` with a default does not cover it.
    """
    try:
        return response.request
    except Exception:  # noqa: BLE001 -- see the section comment
        return None


def exchange_bytes(response) -> int:
    """Every hop of a response the CALLER read: request, head and body.

    `num_bytes_downloaded` is httpx's count of body bytes as received --
    before decompression, after chunk framing -- which is the wire figure
    (measured, above). `history` holds the redirect hops httpx followed.
    """
    total = 0
    for hop in [*getattr(response, "history", ()), response]:
        total += request_bytes(_request_of(hop))
        total += response_head_bytes(hop)
        total += int(getattr(hop, "num_bytes_downloaded", 0) or 0)
    return total


# ---- The tools' calls (IO3) ------------------------------------------------
#
# `httpx` is imported INSIDE each call, for two reasons: scholar's lookup
# imports it lazily so a run with `scholar_lookup` off never loads it, and
# the suite installs a fake `httpx` module these must reach (the root
# conftest). The module function is looked up per call, so a test's fake is
# what runs.

@contextlib.contextmanager
def stream(method: str, url: str, **kwargs):
    """`httpx.stream`, admitted and counted against the tool meter."""
    import httpx

    TOOLS.admit()
    with httpx.stream(method, url, **kwargs) as response:
        try:
            yield response
        finally:
            if TOOLS.enabled():
                TOOLS.record(exchange_bytes(response))


def get(url: str, **kwargs):
    """`httpx.get`, admitted and counted against the tool meter."""
    import httpx

    TOOLS.admit()
    response = httpx.get(url, **kwargs)
    if TOOLS.enabled():
        TOOLS.record(exchange_bytes(response))
    return response


def record_web_search(results_requested: int) -> None:
    """IO15's estimate for one search, which has no client to count."""
    if TOOLS.enabled():
        TOOLS.record(max(int(results_requested), 1)
                     * WEB_SEARCH_BYTES_PER_RESULT, estimated=True)


# ---- The provider SDKs and MCP (IO4) ---------------------------------------
#
# EVENT HOOKS ON AN httpx2 CLIENT, not a wrapped transport, and both halves
# of the reason were measured (batch 239, httpx2 2.16.0). With HTTPS_PROXY
# set and no transport passed, the client MOUNTS a proxy transport of its
# own and a proxied request goes through that, which a wrapper on the
# default transport never sees. Pass a transport instead, and the client
# drops the environment's proxy altogether -- a meter that silently breaks
# the user's proxy. Hooks run for every hop, whatever carries it. Measured
# the same day against all three SDKs: anthropic, openai and google-genai
# each call both hooks for an ordinary request, a stream and (google) an
# embedding.
#
# THE BODY IS COUNTED BY REPLACING `response.stream` in the response hook,
# which runs before anything reads the body, with a stream that counts each
# raw chunk as it passes.

@cache
def _stream_classes() -> tuple:
    """The two counting streams, built on first use so `httpx2` is only
    imported by a process that has a meter on."""
    import httpx2

    class CountingStream(httpx2.SyncByteStream):
        def __init__(self, inner, meter: Meter) -> None:
            self._inner = inner
            self._meter = meter

        def __iter__(self):
            for chunk in self._inner:
                self._meter.record(len(chunk))
                yield chunk

        def close(self) -> None:
            self._inner.close()

    class AsyncCountingStream(httpx2.AsyncByteStream):
        def __init__(self, inner, meter: Meter) -> None:
            self._inner = inner
            self._meter = meter

        async def __aiter__(self):
            async for chunk in self._inner:
                self._meter.record(len(chunk))
                yield chunk

        async def aclose(self) -> None:
            await self._inner.aclose()

    return CountingStream, AsyncCountingStream


def provider_event_hooks() -> dict:
    """Hooks for a provider SDK's client: admit, then count (IO4, IO11).

    THE REQUEST IS SIZED EXACTLY BEFORE IT IS SENT, so an upload that would
    pass the ceiling is refused whole. The provider's upload is the big
    one -- every step re-sends the thread -- which is why this meter
    admits on the outgoing size and not only on what is already used.
    """
    counting, _ = _stream_classes()
    meter = PROVIDER

    def on_request(request) -> None:
        outgoing = request_bytes(request)
        meter.admit(outgoing)
        meter.record(outgoing)

    def on_response(response) -> None:
        meter.record(response_head_bytes(response))
        response.stream = counting(response.stream, meter)

    return {"request": [on_request], "response": [on_response]}


def mcp_event_hooks() -> dict:
    """Hooks for an MCP server's async client: count, never refuse.

    MCP's admission is `mcp_client.call_tool`'s, not this hook's. A refusal
    raised here would surface inside the SDK's background writer task,
    where it reads as a timeout rather than as a reason; refused at the
    call, it is the one error shape `call_tool` already returns.
    """
    _, counting = _stream_classes()
    meter = MCP

    async def on_request(request) -> None:
        meter.record(request_bytes(request))

    async def on_response(response) -> None:
        meter.record(response_head_bytes(response))
        response.stream = counting(response.stream, meter)

    return {"request": [on_request], "response": [on_response]}


def mcp_call_bytes(params: Any) -> int:
    """What a tool call will send, near enough to admit on."""
    try:
        return len(json.dumps(params, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


# ---- What a person reads (IO13) --------------------------------------------

#: IO5's list, said once and shown by `/network` whatever the meters say.
NOT_COUNTED = (
    "Not counted: shell commands (containers, WSL, SSH), stdio MCP servers, "
    "sign-in and token-refresh traffic, and TLS/TCP overhead. web_search is "
    "an estimate, about 21 kB per result asked for.")

#: The sidebar's names, which have 22 columns to share.
SHORT_LABELS = {"tools": "tools", "provider": "api", "mcp": "mcp"}


def network_report() -> list:
    """`/network`'s lines, the same in both shells (ROADMAP_v3 §89's rule:
    the shells bind handlers, and what they print is decided here)."""
    rows = states()
    window = describe_window(window_s())
    if not any(row.enabled for row in rows):
        return [
            "No network limit is set: every meter is off and nothing is "
            "counted.",
            "To set one, give network_limit_tools_mb, "
            "network_limit_provider_mb or network_limit_mcp_mb a value in "
            f"config.yaml (decimal MB per {window}, network_window_hours).",
            NOT_COUNTED,
        ]
    width = max(len(row.label) for row in rows)
    lines = [f"Network use over the last {window}:"]
    for row in rows:
        if not row.enabled:
            lines.append(f"  {row.label:<{width}}  off")
            continue
        line = (f"  {row.label:<{width}}  {format_bytes(row.used)} of "
                f"{format_bytes(row.ceiling)}")
        if row.estimated:
            line += f" ({format_bytes(row.estimated)} of it estimated)"
        if row.full:
            line += " -- full"
            if row.room_at is not None:
                line += f", room returns at about {_clock(row.room_at)}"
        lines.append(line)
    note = next((row.ledger_note for row in rows if row.ledger_note), None)
    if note:
        lines.append(f"Ledger: {note}.")
    else:
        lines.append(f"Ledger: {_ledger().path()}, shared by every harness "
                     "process and written at most once a minute.")
    lines.append(NOT_COUNTED)
    return lines


def sidebar_text(rows: list) -> Optional[str]:
    """The sidebar line for these states, or None while every meter is off.

    `~` marks a figure that includes an estimate, IO3's rule that the
    weaker number says so wherever it is shown.
    """
    on = [row for row in rows if row.enabled]
    if not on:
        return None
    parts = []
    for row in on:
        mark = "~" if row.estimated else ""
        full = " full" if row.full else ""
        parts.append(f"{SHORT_LABELS[row.name]} {mark}"
                     f"{format_bytes_short(row.used)}/"
                     f"{format_bytes_short(row.ceiling)}{full}")
    return ("net · " + " · ".join(parts)
            + f" · per {describe_window(on[0].window_s)}")
