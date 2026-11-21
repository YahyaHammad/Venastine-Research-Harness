"""
core/usage_view.py

What `/usage` shows, for either shell (ROADMAP_v3 §97, UF4-UF9).

FOUR ROWS, EACH THE PROVIDER'S OWN COUNT. Input, cache read, cache write
and output, summed over model calls since the thread was opened -- the
span the TUI's usage line already gives `billed`. Nothing here is
estimated, priced or carried across a restart: a figure the provider did
not send is not a zero.

BY SOURCE, THEN BY MODEL (UF6, UF7). A thread's calls are only part of
what it spends. Its subagents at any depth, the research passes of a
`/research` run started while it was on screen, and the harness's own runs
for it (compaction, the summaries `/summary` and `/ref` make, the research
review, `/init`) each run on a thread of their own, and models are billed
at different rates, so a sum across them would be a number nobody is
billed. Each source is a section and each (provider, model) a block in it.

ONE LEDGER, ENTERED, NOT PASSED (UF9). Every model call is recorded once,
in `RunAgentLoop._run`, into whichever `UsageLedger` the shell entered with
`counting()`. The ledger and the current source ride a ContextVar, the
route `agent_activity`'s span already takes, so a run inside a tool call,
on the parallel tool pool or on a background session's thread finds the
ledger of the thread that started it: each of those takes
`copy_context()` with it. A run started where no shell is counting --
`--mode research`, a test calling the loop directly -- is recorded
nowhere, because nothing would read it.

"NOT REPORTED" IS A ROW'S ANSWER, NOT A ZERO. `core/client.py` puts a
cache count in a response's usage only when the provider sent one, and
names on `ModelResponse.usage_unreported` the input or output count that
`_stream_usage` had to coerce to 0 for the loop's arithmetic (D21). A
call that did not report a row is not counted for it, and a row reported
on some calls says on how many: the batch-61 rule that the completion line
makes no token claim where none was measured, applied to four figures.

PURE: no `core/` import, no storage, no clock. The thread kind that marks
a research pass is the loop's to read and hand over (`research=`).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar

#: The rows, in the order `/usage` prints them, as (usage key, label).
#: The keys are `ModelResponse.usage`'s; the labels are the owner's
#: choice (§97 UF4).
ROWS = (
    ("input_tokens", "input"),
    ("cache_read_tokens", "cache read"),
    ("cache_write_tokens", "cache write"),
    ("output_tokens", "output"),
)

#: The two rows that are PART OF input on every provider since §97 UF5:
#: `core/client.py` adds them into input where a provider reports them
#: apart, so the report says so rather than inviting a sum.
CACHE_KEYS = ("cache_read_tokens", "cache_write_tokens")

#: Where a call came from (UF6). THREAD is the thread's own run, the span
#: `billed` covers; the others are what it started.
THREAD = "thread"
SUBAGENTS = "subagents"
RESEARCH = "research"
HARNESS = "harness"

#: The sections, in the order `/usage` prints them, as (source, heading).
SOURCES = (
    (THREAD, "this thread"),
    (SUBAGENTS, "subagents"),
    (RESEARCH, "research passes"),
    (HARNESS, "harness runs"),
)

_KNOWN = frozenset(source for source, _heading in SOURCES)

HEAD = "usage · this thread since it was opened"

#: (ledger, source) for the run in progress, or None where no shell is
#: counting. `source` is None until something the thread started sets it.
_SCOPE: ContextVar = ContextVar("venastine_usage_scope", default=None)


def _count(value):
    """`value` as a token count, or None. `_token_count`'s rule: a bool is
    not a count and neither is a negative number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


class UsageTally:
    """Provider-reported counts over a run of model calls.

    `calls` is every call recorded, reported or not, because "reported on
    3 of 5 calls" needs both numbers. `totals` and `reported` hold a key
    only once some call reported it, so an absent key IS "not reported".
    """

    def __init__(self):
        self.calls = 0
        self.totals: dict = {}
        self.reported: dict = {}

    def add(self, usage, unreported=()) -> None:
        """One model call's usage dict, and the keys in it that the
        provider did not send (`ModelResponse.usage_unreported`)."""
        self.calls += 1
        usage = usage if isinstance(usage, dict) else {}
        for key, _label in ROWS:
            if key in unreported:
                continue
            value = _count(usage.get(key))
            if value is None:
                continue
            self.totals[key] = self.totals.get(key, 0) + value
            self.reported[key] = self.reported.get(key, 0) + 1

    def merge(self, other) -> None:
        """Another tally's calls, added to this one's. A None or a
        non-tally is nothing to add."""
        if not isinstance(other, UsageTally):
            return
        self.calls += other.calls
        for key, value in other.totals.items():
            self.totals[key] = self.totals.get(key, 0) + value
        for key, value in other.reported.items():
            self.reported[key] = self.reported.get(key, 0) + value


class UsageLedger:
    """Every call a thread caused, as a tally per (source, provider, model).

    LOCKED, because the writers are not one thread: a background subagent
    records from its session's thread and the parallel tool pool from its
    workers while the turn that started them records from its own. A
    reader gets a copy (`snapshot`), never the live tallies.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tallies: dict = {}
        self._runs: dict = {}

    def add(self, source, provider, model, usage, unreported=()) -> None:
        key = (source, str(provider or ""), str(model or ""))
        with self._lock:
            tally = self._tallies.get(key)
            if tally is None:
                tally = self._tallies[key] = UsageTally()
            tally.add(usage, unreported)

    def count_run(self, source) -> None:
        """One more run of `source` started, for the heading's "1 run"."""
        with self._lock:
            self._runs[source] = self._runs.get(source, 0) + 1

    @property
    def calls(self) -> int:
        with self._lock:
            return sum(tally.calls for tally in self._tallies.values())

    def snapshot(self):
        """({source: [((provider, model), tally copy), ...]}, {source: runs}).

        Blocks keep the order their model was first used in."""
        with self._lock:
            sections: dict = {}
            for (source, provider, model), tally in self._tallies.items():
                copy = UsageTally()
                copy.merge(tally)
                sections.setdefault(source, []).append(((provider, model),
                                                        copy))
            return sections, dict(self._runs)


@contextmanager
def counting(ledger):
    """A shell's work for one thread: calls under it go to `ledger`.

    Entered by the TUI round every thread worker it starts and by the CLI
    round every line its chat loop handles. Anything but a ledger counts
    nothing."""
    token = _SCOPE.set((ledger, None) if isinstance(ledger, UsageLedger)
                       else None)
    try:
        yield ledger
    finally:
        _SCOPE.reset(token)


@contextmanager
def started(source):
    """A run the current one starts, counted under `source` (UF8).

    A harness run is the harness's wherever it runs -- a subagent's
    compaction is compaction. Otherwise the first source set stands, so
    a subagent's own subagents are subagents, and a pass's subagent is a
    subagent, since research is told apart by its thread and never set
    here. Usable as a decorator; each call enters afresh."""
    if source not in _KNOWN:
        raise ValueError(f"not a usage source: {source!r}")
    scope = _SCOPE.get()
    if scope is None:
        yield
        return
    ledger, current = scope
    chosen = source if (source == HARNESS or current is None) else current
    token = _SCOPE.set((ledger, chosen))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def record(provider, model, usage, unreported=(), *, research=False) -> None:
    """One model call, into the ledger being counted, if any.

    `research` is whether the run's thread is a research pass; it decides
    the source only where nothing the thread started has set one."""
    scope = _SCOPE.get()
    if scope is None:
        return
    ledger, source = scope
    if source is None:
        source = RESEARCH if research else THREAD
    ledger.add(source, provider, model, usage, unreported)


def count_run(source) -> None:
    """One run of `source` started, on the ledger being counted, if any."""
    scope = _SCOPE.get()
    if scope is not None:
        scope[0].count_run(source)


def _calls(n: int) -> str:
    return "1 call" if n == 1 else f"{n:,} calls"


def _model_label(provider: str, model: str) -> str:
    return f"{provider} {model}".strip() or "model not named"


def _rows(tally, indent: str) -> list:
    """A tally's four rows, right-aligned to the block's widest figure."""
    lines = []
    figures = {key: format(tally.totals[key], ",")
               for key, _label in ROWS if tally.reported.get(key)}
    width = max((len(text) for text in figures.values()), default=0)
    label_width = max(len(label) for _key, label in ROWS)
    for key, label in ROWS:
        name = label.ljust(label_width)
        if key not in figures:
            lines.append(f"{indent}{name}  not reported")
            continue
        line = f"{indent}{name}  {figures[key].rjust(width)}"
        seen = tally.reported[key]
        if seen < tally.calls:
            line += f"   (reported on {seen:,} of {tally.calls:,} calls)"
        lines.append(line)
    return lines


def report_lines(ledger) -> list:
    """The lines `/usage` prints, in either shell."""
    if not isinstance(ledger, UsageLedger):
        ledger = UsageLedger()
    sections, runs = ledger.snapshot()
    total = sum(tally.calls for blocks in sections.values()
                for _model, tally in blocks)
    if not total:
        return [HEAD + " · no model calls yet"]
    lines = [f"{HEAD} · {_calls(total)}"]
    cached = False
    for source, heading in SOURCES:
        blocks = sections.get(source)
        if not blocks:
            continue
        calls = sum(tally.calls for _model, tally in blocks)
        started_runs = runs.get(source, 0)
        prefix = ""
        if started_runs:
            prefix = ("1 run, " if started_runs == 1
                      else f"{started_runs:,} runs, ")
        lines.append(f"{heading} · {prefix}{_calls(calls)}")
        for (provider, model), tally in blocks:
            label = _model_label(provider, model)
            if len(blocks) > 1:
                label += f" · {_calls(tally.calls)}"
            lines.append(f"  {label}")
            lines.extend(_rows(tally, "    "))
            cached = cached or any(tally.reported.get(key)
                                   for key in CACHE_KEYS)
    if cached:
        lines.append("  cache read and cache write are part of input")
    return lines
