"""
core/live_runs.py

What a run is doing right now, for a person watching it (ROADMAP_v3 §108,
PW2 and PW3).

A RUN'S ARCHIVE HOLDS FINISHED STEPS ONLY. D20 persists an assistant turn
once its model call returns, so a pass thinking for ten minutes, or a
subagent streaming its first answer, has written nothing a viewer can read.
Measured in batch 282 on the real loop: a pass that streamed 40,000
characters of thinking and 3,000 of text had one row in its thread, its
task, until the call ended. This module is the other half: per thread, in
memory, what the step in progress has streamed, when its last byte came,
which tools are running, and the provider's token counts so far.

WRITTEN BY THE LOOP AND NOTHING ELSE. `RunAgentLoop._run` opens a record for
every run, and `_run_steps` writes at moments it already has: a call going
out, each delta, a retraction, a step persisted, a group of tool calls
starting and settling.

THE CONVERSATION'S OWN RUN KEEPS NO TEXT (ROADMAP_v3 §102, MW3). Its stream
is already the transcript, which is why §108's PW2 left it out. Since batch
284 it is recorded PHASE-ONLY -- waiting, streaming, tools, and when its last
byte came -- because a chat turn waiting on a silent model looked the same as
one being answered: the shells read this to say `waiting 4m12s`. Its deltas
move its phase and keep nothing, and the record goes when the turn ends.

READ BY A SHELL ON ITS OWN CLOCK. The TUI's sidebar and viewer read a
snapshot each tick; the CLI's ticker reads one every few seconds. Nothing
here posts, wakes or calls back, so a run never waits on a reader.

NOT A CHANNEL INTO ANY MODEL. D6 returns a subagent's distilled answer to its
parent and nothing else, and that is unchanged: this is read by display code
only, as a session's output buffer is (SS14). `agent_activity` still carries
lifecycle only.

BOUNDED. A step keeps the last `STEP_CHARS_KEPT` characters of its text and
of its thinking and says how many came before them. An ended run keeps its
figures, never its text, among the last `ENDED_KEPT`, so a corrective retry
of the same pass thread (§99), which is a second run on that thread, carries
on counting its tokens instead of starting again from zero.

PURE: no `core/` import, no storage. The loop hands over the thread id and
kind it already holds.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass

#: The most of one step's text, and of its thinking, kept for the live view
#: (PW2). The view shows the step in progress; the archive has every step
#: that finished, so this bounds one call's output and nothing else.
STEP_CHARS_KEPT = 200_000

#: Ended runs whose figures are kept, newest last. A run is revived from
#: here when its thread runs again, which is what a corrective retry is.
ENDED_KEPT = 32

#: What a run is doing (PW3). WAITING: a model call is out and no byte of
#: it has arrived, or the loop is between a step and the next call.
#: STREAMING: bytes are arriving; `phase_for` is the time since the last.
#: TOOLS: a group of tool calls is running. ENDED: the run is over.
WAITING = "waiting"
STREAMING = "streaming"
TOOLS = "tools"
ENDED = "ended"


def duration(seconds) -> str:
    """A whole-second duration in few columns: `45s`, `6m40s`, `1h02m`.

    WHOLE SECONDS, unlike the completion line's `took 12.3s`: these figures
    are redrawn every tick in the sidebar and printed by the CLI as it
    waits, and a tenth that changes ten times a second is noise there.
    Shared by both shells, so `waiting 1m05s` is spelled one way."""
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


#: A stream silent this long is said to be, on a chat turn's border
#: (ROADMAP_v3 §102, MW3): `last byte 14s`. Below it a stream is simply
#: arriving, which the transcript already shows, and a few seconds between
#: chunks is ordinary.
QUIET_SHOWN_S = 10.0


def model_wait(snap) -> str:
    """What a chat turn is waiting on the model for, in §108's words, or
    "" when it is not (§102, MW3, the owner's choice of wording).

    `waiting 4m12s` while a call is out and nothing of it has arrived, or
    the loop is between a step and the next call; `last byte 14s` once a
    stream has been silent for QUIET_SHOWN_S. Nothing while tools run --
    they are on screen -- or once the run is over."""
    if snap is None or not snap.live:
        return ""
    if snap.phase == WAITING:
        return f"waiting {duration(snap.phase_for)}"
    if snap.phase == STREAMING and snap.phase_for >= QUIET_SHOWN_S:
        return f"last byte {duration(snap.phase_for)}"
    return ""


def _count(value):
    """`value` as a token count, or None: `usage_view._count`'s rule, a
    bool is not a count and neither is a negative number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


class _Stream:
    """One kind of streamed text in the step in progress.

    CHUNKS, NOT ONE STRING. A delta is a few characters, and appending each
    to a string held on an object copies the whole string every time: two
    hundred thousand characters in four-character deltas is billions of
    bytes copied. A deque of chunks costs one append per delta, and a
    reader joins once per tick."""

    __slots__ = ("chunks", "kept", "total")

    def __init__(self) -> None:
        self.chunks: deque = deque()
        self.kept = 0
        self.total = 0

    def add(self, delta: str) -> None:
        self.chunks.append(delta)
        self.kept += len(delta)
        self.total += len(delta)
        over = self.kept - STEP_CHARS_KEPT
        while over > 0 and self.chunks:
            first = self.chunks[0]
            if len(first) <= over:
                self.chunks.popleft()
                self.kept -= len(first)
                over -= len(first)
            else:
                self.chunks[0] = first[over:]
                self.kept -= over
                over = 0

    def since(self, offset: int) -> tuple[int, str]:
        """(start, text): what is kept from absolute offset `offset` on.

        `start` is where the text begins, which is later than `offset` when
        the cap dropped what the reader had not seen yet -- the reader says
        so rather than presenting the text as continuous."""
        first = self.total - self.kept
        start = max(int(offset or 0), first)
        if start >= self.total:
            return self.total, ""
        text = "".join(self.chunks)
        return start, text[start - first:]


class _Run:
    """One run's record. Mutated under the registry's lock only."""

    def __init__(self, key: str, kind, name, now: float,
                 keeps_text: bool = True) -> None:
        self.key = key
        self.kind = kind
        self.name = name
        # False for a phase-only run (§102): see the module docstring.
        self.keeps_text = keeps_text
        self.live = True
        self.begun = now
        self.phase = WAITING
        self.since = now
        self.last_byte = None
        self.tools: tuple = ()
        # Bumped whenever what the view drew of the step in progress stops
        # being true: the step was persisted (the archive has it now), it
        # was retracted, or the run started again.
        self.step = 0
        self.text = _Stream()
        self.thinking = _Stream()
        self.calls = 0
        self.tokens_in = None
        self.tokens_out = None

    def new_step(self) -> None:
        self.step += 1
        self.text = _Stream()
        self.thinking = _Stream()


@dataclass(frozen=True)
class Snapshot:
    """A run as it stood when read. Frozen: it crosses to a reader's thread.

    `phase_for` is seconds in the current phase: waiting for a first byte,
    since the last byte while streaming, or running the tools in `tools`.
    `tokens_in` and `tokens_out` are None until a call has reported them.
    The text fields are filled only by a read that asked for them."""

    thread_id: str
    kind: object
    name: object
    live: bool
    phase: str
    phase_for: float
    tools: tuple
    step: int
    calls: int
    tokens_in: object
    tokens_out: object
    begun: float = 0.0
    # The registry's clock when this was read, so a reader can say when
    # the phase began (`read_at - phase_for`) without a clock of its own.
    read_at: float = 0.0
    text: str = ""
    text_start: int = 0
    text_total: int = 0
    thinking: str = ""
    thinking_start: int = 0
    thinking_total: int = 0


class _Watch:
    """The loop's handle on one run's record, so `_run_steps` writes
    without passing a thread id to every call. The null handle answers
    every write with nothing, for a run nobody watches."""

    __slots__ = ("_registry", "_key")

    def __init__(self, registry, key) -> None:
        self._registry = registry
        self._key = key

    def call_started(self) -> None:
        if self._key is not None:
            self._registry.call_started(self._key)

    def delta(self, text=None, thinking=None) -> None:
        if self._key is not None:
            self._registry.delta(self._key, text=text, thinking=thinking)

    def retract(self) -> None:
        if self._key is not None:
            self._registry.retract(self._key)

    def step_done(self, usage=None, unreported=()) -> None:
        if self._key is not None:
            self._registry.step_done(self._key, usage, unreported)

    def tools_started(self, names) -> None:
        if self._key is not None:
            self._registry.tools_started(self._key, names)

    def tools_done(self) -> None:
        if self._key is not None:
            self._registry.tools_done(self._key)


class Registry:
    """Every watched run in this process, by thread id.

    PROCESS-WIDE, like `shell_sessions.sessions`: a background subagent
    writes from its session's thread, a pass from the research worker's, a
    parallel tool's child from the pool's, and the shells read from theirs.
    One lock; every write is a few assignments under it."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._live: dict = {}
        self._ended: OrderedDict = OrderedDict()

    # -- the loop's half ------------------------------------------------

    def begin(self, thread_id, kind=None, name=None,
              keeps_text: bool = True) -> None:
        """A run on `thread_id` began. A thread that ran before in this
        process carries on from its figures (its tokens and calls), with a
        new step, so a reader drawing the old one draws again.

        `keeps_text` False records the run's phase and figures only (§102):
        its deltas move the phase and are not kept, and it is forgotten
        when it ends rather than kept among the ended."""
        if thread_id is None:
            return
        key = str(thread_id)
        now = self._clock()
        with self._lock:
            run = self._live.get(key) or self._ended.pop(key, None)
            if run is None:
                run = _Run(key, kind, name, now, keeps_text=keeps_text)
            else:
                run.keeps_text = keeps_text
                run.live = True
                run.phase, run.since, run.last_byte = WAITING, now, None
                run.tools = ()
                run.new_step()
                if kind is not None:
                    run.kind = kind
                if name is not None:
                    run.name = name
            # Re-inserted, so `latest` finds the run begun most recently.
            self._live.pop(key, None)
            self._live[key] = run

    def end(self, thread_id) -> None:
        """That run is over. Its text goes; its figures are kept a while."""
        if thread_id is None:
            return
        key = str(thread_id)
        with self._lock:
            run = self._live.pop(key, None)
            if run is None or not run.keeps_text:
                # A phase-only run has nothing worth keeping once it is
                # over, and must not push a pass's figures out of the
                # ended runs a corrective retry carries on from (§99).
                return
            run.live = False
            run.phase, run.since = ENDED, self._clock()
            run.tools = ()
            run.new_step()
            self._ended[key] = run
            while len(self._ended) > ENDED_KEPT:
                self._ended.popitem(last=False)

    def call_started(self, key: str) -> None:
        with self._lock:
            run = self._live.get(key)
            if run is not None:
                run.phase, run.since, run.last_byte = (
                    WAITING, self._clock(), None)
                run.tools = ()

    def delta(self, key: str, text=None, thinking=None) -> None:
        with self._lock:
            run = self._live.get(key)
            if run is None:
                return
            if text and run.keeps_text:
                run.text.add(text)
            if thinking and run.keeps_text:
                run.thinking.add(thinking)
            if text or thinking:
                run.phase, run.last_byte = STREAMING, self._clock()

    def retract(self, key: str) -> None:
        """What was streamed for this call is being taken back (batch 113);
        the view takes its rows back too. The call is retried, so the run
        is waiting again."""
        with self._lock:
            run = self._live.get(key)
            if run is not None:
                run.new_step()
                run.phase, run.since, run.last_byte = (
                    WAITING, self._clock(), None)

    def step_done(self, key: str, usage=None, unreported=()) -> None:
        """A call finished and its assistant turn is in the archive: the
        step in progress is over, and its usage is counted. The run waits
        until the loop says what it does next."""
        with self._lock:
            run = self._live.get(key)
            if run is None:
                return
            run.calls += 1
            usage = usage if isinstance(usage, dict) else {}
            for field, attr in (("input_tokens", "tokens_in"),
                                ("output_tokens", "tokens_out")):
                if field in unreported:
                    continue
                value = _count(usage.get(field))
                if value is not None:
                    setattr(run, attr, (getattr(run, attr) or 0) + value)
            run.new_step()
            run.phase, run.since, run.last_byte = (
                WAITING, self._clock(), None)

    def tools_started(self, key: str, names) -> None:
        with self._lock:
            run = self._live.get(key)
            if run is not None:
                run.phase, run.since = TOOLS, self._clock()
                run.tools = tuple(str(name) for name in names if name)

    def tools_done(self, key: str) -> None:
        with self._lock:
            run = self._live.get(key)
            if run is not None:
                run.phase, run.since, run.last_byte = (
                    WAITING, self._clock(), None)
                run.tools = ()

    @contextmanager
    def watching(self, memory, exclude=(), phase_only=()):
        """Bracket one run of `memory`'s thread: begin, then end however
        the run ends -- including a generator closed part way, which is how
        a quitting shell abandons one. A thread whose kind is in `exclude`
        is not recorded at all; one whose kind is in `phase_only` is
        recorded without its text (§102), which is how the loop records
        the conversation."""
        thread_id = getattr(memory, "thread_id", None)
        kind = getattr(memory, "kind", None)
        if thread_id is None or kind in exclude:
            yield
            return
        self.begin(thread_id, kind=kind,
                   name=getattr(memory, "agent_name", None),
                   keeps_text=kind not in phase_only)
        try:
            yield
        finally:
            self.end(thread_id)

    def watch(self, memory) -> _Watch:
        """The loop's handle on `memory`'s run, or the null handle when its
        thread is not being recorded."""
        thread_id = getattr(memory, "thread_id", None)
        key = None if thread_id is None else str(thread_id)
        with self._lock:
            if key not in self._live:
                key = None
        return _Watch(self, key)

    # -- the shells' half -----------------------------------------------

    def read(self, thread_id, text_from=None, thinking_from=None):
        """A snapshot of that run, or None for a thread never recorded or
        long forgotten. Text is included only when its offset is given:
        the sidebar reads figures every tick and needs none of it."""
        if thread_id is None:
            return None
        key = str(thread_id)
        with self._lock:
            run = self._live.get(key) or self._ended.get(key)
            if run is None:
                return None
            return self._snapshot(run, text_from, thinking_from)

    def latest(self, kind):
        """The live run of `kind` begun most recently, or None. The CLI's
        ticker asks this for the research pass it is waiting on."""
        with self._lock:
            for run in reversed(list(self._live.values())):
                if run.kind == kind:
                    return self._snapshot(run, None, None)
        return None

    def _snapshot(self, run, text_from, thinking_from) -> Snapshot:
        now = self._clock()
        if run.phase == STREAMING and run.last_byte is not None:
            phase_for = now - run.last_byte
        else:
            phase_for = now - run.since
        extra = {}
        if text_from is not None:
            start, text = run.text.since(text_from)
            extra.update(text=text, text_start=start,
                         text_total=run.text.total)
        if thinking_from is not None:
            start, text = run.thinking.since(thinking_from)
            extra.update(thinking=text, thinking_start=start,
                         thinking_total=run.thinking.total)
        return Snapshot(
            thread_id=run.key, kind=run.kind, name=run.name, live=run.live,
            phase=run.phase, phase_for=max(0.0, phase_for), tools=run.tools,
            step=run.step, calls=run.calls, tokens_in=run.tokens_in,
            tokens_out=run.tokens_out, begun=run.begun, read_at=now,
            **extra)


#: The process's registry.
runs = Registry()
