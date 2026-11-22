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

A RESEARCH RUN'S OWN TOTALS (ROADMAP_v3 §108, PW5). The ledger above is a
thread's and lives as long as the shell's view of it; a run's passes are
one section of it, summed. A run also keeps its own `RunUsage`, by pass and
then by model, entered by the pipeline the way a shell enters its ledger
and recorded into at the same one place. Its dict form is what the run's
record stores, after every pass, so trace.md, the research panel and
`/usage --run` read one shape, and a figure outlives the process that
counted it. UF7 holds there too: no figure adds two models.

PURE: no storage and no clock. The two `core/` imports are pure as well:
`live_runs.duration`, so a time is spelled one way in both shells, and
`ansi`, for a stored query shown back. The thread kind that marks a
research pass is the loop's to read and hand over (`research=`).
"""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from core import ansi
from core.live_runs import duration

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

#: (run usage, pass label, this run of the pass) for the research run being
#: tallied, or None (§108 PW5). The label is None between passes; the
#: third slot is a `UsageLedger` of the pass's current run alone, for its
#: trace line, or None between passes.
_RUN: ContextVar = ContextVar("venastine_run_usage", default=None)


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
    """One model call, into the ledger being counted, if any, and into the
    research run being tallied, if any (§108 PW5).

    `research` is whether the run's thread is a research pass; it decides
    the source only where nothing the thread started has set one. The two
    are independent: `--mode research` counts no thread, and still keeps
    its run's totals."""
    scope = _SCOPE.get()
    if scope is not None:
        ledger, source = scope
        if source is None:
            source = RESEARCH if research else THREAD
        ledger.add(source, provider, model, usage, unreported)
    tallied = _RUN.get()
    if tallied is not None:
        run_usage, label, this_run = tallied
        label = label or OUTSIDE_A_PASS
        run_usage.add(label, provider, model, usage, unreported)
        if this_run is not None:
            this_run.add(label, provider, model, usage, unreported)


def count_run(source) -> None:
    """One run of `source` started, on the ledger being counted, if any."""
    scope = _SCOPE.get()
    if scope is not None:
        scope[0].count_run(source)


#: What `/usage` takes (ROADMAP_v3 §108, PW5, the owner's form): nothing
#: for this thread, `--run` alone for a picker of stored runs -- a listing
#: in the CLI -- and `--run <id>` for one, the shape of `/research
#: --resume`.
ARGS = "[--run [run-id]]"

#: `parse_args`' answer for a bare `--run`.
RUN_PICKER = "picker"


def parse_args(args):
    """None for a bare `/usage`, RUN_PICKER for `--run` alone, the id as
    typed for `--run <id>`. Anything else raises ValueError, with what
    the command takes: a typo should not print this thread's report as
    though it had been understood."""
    text = (args or "").strip()
    if not text:
        return None
    head, _sep, rest = text.partition(" ")
    rest = rest.strip()
    if head != "--run" or " " in rest:
        raise ValueError(f"/usage takes {ARGS}")
    return rest or RUN_PICKER


def _calls(n: int) -> str:
    return "1 call" if n == 1 else f"{n:,} calls"


#: `1 call`, `11 calls`: the research panel's foot says it as `/usage`
#: does (§108 PW5).
calls_text = _calls


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


# ---------------------------------------------------------------------------
# ---- ROADMAP_v3 §108 (PW5): one research run's totals ---------------------
# ---------------------------------------------------------------------------

#: The label for a run's call made while no pass was running. The review
#: stage names its own, so this is whatever is left; a run that shows it
#: made a call nothing here expected.
OUTSIDE_A_PASS = "outside a pass"

#: The version of the stored dict. A dict with another version, or none,
#: is a run whose totals this build cannot read: "not recorded".
RUN_USAGE_VERSION = 1


class _PassFigures:
    """One label's runs, wall time and tallies. Mutated under RunUsage's
    lock only."""

    __slots__ = ("runs", "seconds", "tallies")

    def __init__(self) -> None:
        self.runs = 0
        self.seconds = 0.0
        self.tallies: dict = {}


class RunUsage:
    """One research run's calls, by pass and then by (provider, model).

    A PASS IS ITS LABEL, so a pass run more than once -- 6a and 6b once per
    round, Pass 1 once per ensemble candidate, Final synthesis again after
    a review -- is one entry that says how many runs it took. Everything
    a pass caused counts under it: its own calls, its corrective retries,
    its backstop compaction and its subagents, whose runs take the label
    with the rest of the context.

    LOCKED: a pass's subagents record from the parallel tool pool and from
    background sessions' threads while the pipeline runs on its own.

    `base_seconds` is the wall time the run's record already had when this
    process took it up -- a resume's earlier part -- so the time between a
    failure and its resume is never counted."""

    def __init__(self, base_seconds=0.0) -> None:
        self._lock = threading.Lock()
        self._passes: dict = {}
        self.base_seconds = _seconds(base_seconds)

    def _figures(self, label) -> _PassFigures:
        figures = self._passes.get(label)
        if figures is None:
            figures = self._passes[label] = _PassFigures()
        return figures

    def add(self, label, provider, model, usage, unreported=()) -> None:
        key = (str(provider or ""), str(model or ""))
        with self._lock:
            tallies = self._figures(str(label)).tallies
            tally = tallies.get(key)
            if tally is None:
                tally = tallies[key] = UsageTally()
            tally.add(usage, unreported)

    def ran(self, label, seconds) -> None:
        """One more run of `label`, `seconds` of wall time long."""
        with self._lock:
            figures = self._figures(str(label))
            figures.runs += 1
            figures.seconds += _seconds(seconds)

    def to_dict(self, seconds) -> dict:
        """The stored form, with the run's wall time `seconds` so far."""
        with self._lock:
            return {
                "version": RUN_USAGE_VERSION,
                "seconds": round(_seconds(seconds), 1),
                "passes": [
                    {"pass": label, "runs": figures.runs,
                     "seconds": round(figures.seconds, 1),
                     "models": [_block_dict(provider, model, tally)
                                for (provider, model), tally
                                in figures.tallies.items()]}
                    for label, figures in self._passes.items()],
            }

    @classmethod
    def from_dict(cls, data) -> RunUsage:
        """A stored run's figures to carry on from: a resume adds to what
        the run had. Anything unreadable is a run that kept none."""
        usage = cls()
        if not readable(data):
            return usage
        usage.base_seconds = _seconds(data.get("seconds"))
        for label, runs, seconds, blocks in passes(data):
            figures = usage._figures(label)
            figures.runs += runs
            figures.seconds += seconds
            for key, tally in blocks:
                held = figures.tallies.get(key)
                if held is None:
                    figures.tallies[key] = tally
                else:
                    held.merge(tally)
        return usage


def _seconds(value) -> float:
    """`value` as a wall time: a non-negative number, or 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


def _block_dict(provider, model, tally) -> dict:
    return {"provider": provider, "model": model, "calls": tally.calls,
            "totals": dict(tally.totals), "reported": dict(tally.reported)}


def _blocks(models) -> list:
    """[((provider, model), UsageTally)] from a stored entry's `models`,
    keeping only counts `_count` accepts and rows `ROWS` names. A total
    with no count of the calls that reported it cannot be qualified, so
    it is not kept, and neither is a count with no total."""
    blocks = []
    for block in models or ():
        if not isinstance(block, dict):
            continue
        tally = UsageTally()
        tally.calls = _count(block.get("calls")) or 0
        totals = block.get("totals")
        reported = block.get("reported")
        if isinstance(totals, dict) and isinstance(reported, dict):
            for key, _label in ROWS:
                value = _count(totals.get(key))
                seen = _count(reported.get(key))
                if value is not None and seen:
                    tally.totals[key] = value
                    tally.reported[key] = seen
        key = (str(block.get("provider") or ""), str(block.get("model") or ""))
        blocks.append((key, tally))
    return blocks


def readable(data) -> bool:
    """Whether `data` is a run's totals this build reads. A run stored
    before §108 PW5 has `{}`."""
    return isinstance(data, dict) and data.get("version") == RUN_USAGE_VERSION


def enter_run(run_usage):
    """A research run starts tallying into `run_usage`; returns what to
    hand `leave` when it stops.

    A SET AND A RESTORE, NOT A CONTEXT MANAGER WITH A TOKEN. The pipeline is
    a generator, so the run spans its yields, and a generator abandoned by
    a quitting shell can be closed from a context other than the one that
    entered it, where `ContextVar.reset` raises. Restoring the value read
    on entry cannot."""
    previous = _RUN.get()
    _RUN.set((run_usage, None, None) if isinstance(run_usage, RunUsage)
             else None)
    return previous


def enter_pass(label):
    """A pass of the run being tallied starts: its calls, and its
    children's, count under `label`. Returns (what to hand `leave`, the
    ledger of this run of the pass alone), or (None, None) where no run is
    being tallied -- a pass driven on its own, as most tests drive one."""
    current = _RUN.get()
    if current is None:
        return None, None
    this_run = UsageLedger()
    _RUN.set((current[0], str(label), this_run))
    return current, this_run


def leave(previous) -> None:
    """Undo `enter_run` or `enter_pass`."""
    _RUN.set(previous)


def tallied():
    """The RunUsage being tallied into, or None."""
    current = _RUN.get()
    return None if current is None else current[0]


def _merged(blocks) -> list:
    """[((provider, model), tally)] summed per model, in the order each
    model was first used. Never across models (UF7)."""
    merged: dict = {}
    for key, tally in blocks:
        held = merged.get(key)
        if held is None:
            held = merged[key] = UsageTally()
        held.merge(tally)
    return list(merged.items())


def passes(data) -> list:
    """[(label, runs, seconds, [((provider, model), tally), ...])] from a
    run's stored totals, in the order the passes first ran."""
    if not readable(data):
        return []
    rows = []
    for entry in data.get("passes") or ():
        if isinstance(entry, dict):
            rows.append((str(entry.get("pass")), _count(entry.get("runs")) or 0,
                         _seconds(entry.get("seconds")),
                         _blocks(entry.get("models"))))
    return rows


def run_totals(data):
    """(calls, seconds, [((provider, model), tally), ...]) for the whole
    run, one block per model."""
    blocks = _merged(block for _label, _runs, _secs, pass_blocks
                     in passes(data) for block in pass_blocks)
    calls = sum(tally.calls for _key, tally in blocks)
    seconds = _seconds(data.get("seconds")) if readable(data) else 0.0
    return calls, seconds, blocks


def _brief(tally) -> str:
    """A tally as one clause: `12,000 in (4,000 cache read), 800 out`.

    A row not reported says so; a row reported on some calls says on how
    many, as `/usage`'s rows do."""
    def partial(key) -> str:
        seen = tally.reported.get(key, 0)
        return (f" on {seen:,} of {tally.calls:,} calls"
                if seen < tally.calls else "")

    if tally.reported.get("input_tokens"):
        text = f"{tally.totals['input_tokens']:,} in" + partial("input_tokens")
        cached = [f"{tally.totals[key]:,} {label}" for key, label in ROWS
                  if key in CACHE_KEYS and tally.reported.get(key)]
        if cached:
            text += f" ({', '.join(cached)})"
    else:
        text = "input not reported"
    if tally.reported.get("output_tokens"):
        text += (f", {tally.totals['output_tokens']:,} out"
                 + partial("output_tokens"))
    else:
        text += ", output not reported"
    return text


def _models_clause(blocks) -> str:
    return "; ".join(f"{_model_label(provider, model)}: {_brief(tally)}"
                     for (provider, model), tally in blocks)


def pass_line(label, ledger, seconds, *, how="") -> str:
    """The trace line one run of a pass ends with (§108 PW5):
    `Pass 2 used 1 call, 12s; ANTHROPIC model-a: 12,000 in (4,000 cache
    read), 800 out.` `ledger` holds that run alone; `how` is what ended a
    run that did not finish (`failed`, `was stopped`).

    `used`, NOT THE OWNER'S PREVIEW'S COLON. A line that begins `Pass 2:`
    is the pass's RESULT to every reader of a trace by prefix: the resume
    of a run recorded before §98 finds its stage that way (`Pass 6b: ` is
    one of its prefixes), and so does every test that asks what a pass
    reported. Measured in batch 283: the colon form broke eight of them."""
    blocks = []
    if isinstance(ledger, UsageLedger):
        sections, _runs = ledger.snapshot()
        blocks = [block for section in sections.values()
                  for block in section]
    blocks = _merged(blocks)
    calls = sum(tally.calls for _key, tally in blocks)
    head = f"{label} used {_calls(calls)}, {duration(seconds)}"
    if how:
        head += f" before it {how}"
    if not blocks:
        return head + "."
    return f"{head}; {_models_clause(blocks)}."


#: A trace line `pass_line` or `run_total_lines` wrote: a label, ` used `,
#: a call count; or `Run total` and a colon or a comma.
_USAGE_LINE = re.compile(r"^(?:[^:;]+ used [\d,]+ calls?, |Run total[:,] )")


def is_usage_line(line) -> bool:
    """Whether a trace line is one of the run's figures (§108 PW5), for a
    reader that wants the run's account without its bill -- the reviewer,
    whose model has no use for token counts."""
    return isinstance(line, str) and bool(_USAGE_LINE.match(line))


def run_total_lines(data) -> list:
    """The trace lines a run ends with: its calls and time, then each
    model's figures -- on the one line when it used one model, a line per
    model when it used more (UF7: no figure adds two)."""
    if not readable(data):
        return []
    calls, seconds, blocks = run_totals(data)
    took = duration(seconds)
    if not blocks:
        return [f"Run total: no model calls, {took}."]
    if len(blocks) == 1:
        return [f"Run total: {_calls(calls)}, {took}; "
                f"{_models_clause(blocks)}."]
    lines = [f"Run total: {_calls(calls)}, {took}, on {len(blocks)} models."]
    for (provider, model), tally in blocks:
        lines.append(f"Run total, {_model_label(provider, model)}: "
                     f"{_calls(tally.calls)}, {_brief(tally)}.")
    return lines


#: How `/usage --run` names a stored run's state. 'running' is a run in
#: progress or one a quit abandoned, and the record cannot tell which --
#: `--resume`'s listing says the same.
_STATES = {
    "running": "running or stopped, as of its last saved pass",
    "failed": "failed",
    "complete": "complete",
}


def run_report_lines(record) -> list:
    """What `/usage --run` prints for one stored run, in either shell: the
    run, then each pass, then the run's total, each as `/usage`'s four
    rows per model (§108 PW5)."""
    record = record if isinstance(record, dict) else {}
    short = str(record.get("id") or "")[:8]
    status = record.get("status")
    state = _STATES.get(status, str(status or "state unknown"))
    head = f"usage · research run {short} · {state}"
    query = " ".join(ansi.for_display(str(record.get("user_query") or ""))
                     .split())
    if len(query) > 70:
        query = query[:69] + "…"
    asked = [f"  {query}"] if query else []
    data = record.get("usage")
    if not readable(data):
        return [f"{head} · not recorded: the run was stored before runs "
                f"kept their totals"] + asked
    calls, seconds, total_blocks = run_totals(data)
    lines = [f"{head} · {_calls(calls)} · {duration(seconds)}"] + asked
    cached = False

    def add_blocks(blocks) -> None:
        nonlocal cached
        for (provider, model), tally in blocks:
            label = _model_label(provider, model)
            if len(blocks) > 1:
                label += f" · {_calls(tally.calls)}"
            lines.append(f"  {label}")
            lines.extend(_rows(tally, "    "))
            cached = cached or any(tally.reported.get(key)
                                   for key in CACHE_KEYS)

    for label, runs, pass_seconds, blocks in passes(data):
        pass_calls = sum(tally.calls for _key, tally in blocks)
        ran = f"{runs:,} runs, " if runs > 1 else ""
        lines.append(f"{label} · {ran}{_calls(pass_calls)} · "
                     f"{duration(pass_seconds)}")
        add_blocks(blocks)
    if total_blocks:
        lines.append(f"run total · {_calls(calls)} · {duration(seconds)}")
        add_blocks(total_blocks)
    if cached:
        lines.append("  cache read and cache write are part of input")
    return lines
