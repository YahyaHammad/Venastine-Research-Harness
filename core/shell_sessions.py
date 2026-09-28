"""
core/shell_sessions.py

ROADMAP_v3 §49, slices 1 and 2. Shell sessions: commands that keep running
after the tool call that started them has been answered, and the one object
that knows about all of them.

THREE KINDS. `background` and `monitor` are slice 1's -- started, watched,
reported when they end. `interactive` is slice 2's: a shell held open with
a pty that the agent TYPES INTO, whose state survives across its own turns.
It differs in four places and nowhere else: its stdin is a pipe, its output
passes through `core/ansi.py` first, it does not block the user's prompt
(SS25), and its end is HELD rather than woken for (SS31).

WHY IN core/. The TUI, the CLI and a sleeping subagent all consume what a
session produces, and D12 keeps the CLI a permanent fallback -- so the
lifecycle cannot live in tui/, and it is not a tool's to own either: three
tools read and write the same sessions. Nothing here imports storage or a
widget.

WHAT THIS OWNS: lifecycle (start, supervise, time out, kill, close); the
process-wide cap (SS12); each session's output, head and tail, in memory
only; monitor matching on the live stream (SS8); and each owning thread's
INBOX of results waiting to wake it (SS2, SS5, SS7, SS19, SS20).

WHAT IT DOES NOT: write rows or build the wake text (core/session_wake.py
does both, through the real output policy), or decide to start a turn. The
consumers -- the TUI, the CLI chat loop, a subagent asleep in its spawn --
take what is waiting and decide.

THREADS, NOT ASYNCIO (NA10). Each session has a SUPERVISOR (waits for the
process, kills it at its timeout, finalises the state) and a READER (reads
merged output, feeds the buffer, matches lines). A monitor pattern is matched
on the reader thread and outside every lock; re2 releases the GIL.

LOCKING, NA16's SHAPE. One lock guards the session table, the inboxes and
the closing flag, with a Condition over it for waiters. Each output buffer
has its own lock, and the order is always manager then buffer. The sink is
called with a SNAPSHOT and OUTSIDE the lock, so a sink that reads back into
the manager cannot deadlock it and the UI thread never holds a list a
supervisor is still writing.
"""

from __future__ import annotations

import codecs
import contextvars
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

import config
from core import agent_activity
from core.ansi import PtyStream
from core.line_pattern import compile_pattern, validate

logger = logging.getLogger(__name__)

KIND_BACKGROUND = "background"
KIND_MONITOR = "monitor"
KIND_INTERACTIVE = "interactive"

# The kinds whose being live BLOCKS the user's prompt (SS2). Not the same
# question as which kinds occupy the 4-live cap, which is all of them: the
# cap is about resources, which an idle shell does hold, and the block is
# about whether the agent is waiting on work it cannot proceed without,
# which for an idle REPL it is not (SS25). One list, two questions, and
# `_live_locked` takes the kinds so neither caller can drift into the
# other's answer.
BLOCKING_KINDS = frozenset({KIND_BACKGROUND, KIND_MONITOR})

STARTING = "starting"
RUNNING = "running"
EXITED = "exited"
TIMED_OUT = "timed_out"
KILLED = "killed"
FAILED = "failed"
LIVE_STATES = frozenset({STARTING, RUNNING})

# The one wake shape that is not a terminal state: a monitor line matched and
# the session is still running (SS7). The other shapes are the states above.
SHAPE_MATCHED = "matched"

KILL_MODEL = "model"
KILL_USER = "user"
KILL_QUIT = "quit"
KILL_OWNER_FAILED = "owner_failed"
KILL_WAKE_LIMIT = "wake_limit"
KILL_IDLE = "idle"
KILL_OPEN_FAILED = "open_failed"


class _AnyOwner:
    """The type of ANY_OWNER, for a legible repr in a traceback."""

    __slots__ = ()

    def __repr__(self) -> str:                     # pragma: no cover - repr
        return "ANY_OWNER"


#: "Do not check ownership" -- the AUTHORITY OF THE USER OR THE HARNESS, said
#: out loud (TECHNICAL_DEBT 29, batch 110).
#:
#: This used to be spelled `None`, which is the value a thread id takes when
#: something forgot to supply one. `_owned_locked` read it as "any session
#: matches" and `list_for` eleven lines below read the same value as "no
#: session matches" -- one value, two opposite readings, in two functions
#: that answer the same question. Both are now the second reading, and a
#: caller that means to skip the check has to say so.
#:
#: Nothing reachable from the tools can pass this: `shell_input`,
#: `shell_output` and `shell_kill` all hand over `memory.thread_id`, and the
#: whole ownership story in tools/builtin/shell_sessions.py is that a model
#: which could NAME a thread could reach another conversation's sessions.
#: That story rested on `memory.thread_id` never being None, which is a
#: property of another module and was asserted nowhere.
ANY_OWNER = _AnyOwner()

# What an interactive session may be typed at, and how it is waited on.
# The control names are the agent's vocabulary; the bytes are the pty's.
CONTROLS = {"interrupt": "\x03", "eof": "\x04"}

# Measured, batch 100. A pty in canonical mode accepts 4095 bytes on one
# line and SILENTLY DROPS the rest -- the shell then runs a truncated
# command and reports success. So a longer input is REFUSED here rather
# than half-executed: a command the user approved must not be edited by a
# terminal driver on its way in.
MAX_INPUT_BYTES = 4000

# What each of SS26's four bounds MEANS, carried in the result beside the
# name. The distinction the agent has to act on is that only `ready` says a
# command finished -- `quiet` is silence, which a running command produces
# just as well as a finished one, and that ambiguity is why SS34 exists.
_ENDED_MEANING = {
    "ready": ("the shell returned to its prompt, so the command finished; "
              "anything you sent has run"),
    "matched": ("a line matched wait_for; the command may still be running, "
                "so send again with no text to keep reading"),
    "quiet": ("output stopped arriving, which does NOT mean the command "
              "finished -- send again with no text to keep waiting"),
    "bound": ("wait_s ran out without the shell coming back to its prompt. "
              "Whatever you sent is still running and its output is still "
              "being collected -- send again with no text to keep reading, "
              "or control \"interrupt\" to stop it"),
    "ended": "the session itself ended while waiting",
}

# A pty in canonical mode holds this many bytes on one line, measured.
_PTY_LINE_LIMIT = 4095
# How much new output one poll pulls back to run a wait_for pattern over.
_MATCH_SLICE = 65_536

# The round trip through the container was measured at about 50 ms, so a
# third of a second of silence is quiet rather than slow.
_INPUT_QUIET_S = 0.3
_INPUT_POLL_S = 0.02
# How long a shell gets to reach its first prompt before the open fails.
# Measured at 0.43 s; this is that with room for a cold image.
_OPEN_BOUND_S = 30.0

MAX_MATCHED_LINES = 50
_MAX_LINE_CHARS = 16_000
_READ_CHUNK = 65_536
_FINISHED_KEPT = 20
_KILL_SETTLE_S = 15


class SessionRefused(Exception):
    """A start the manager will not make. The message is what the agent is
    told, so it says what to do next."""


# ---------------------------------------------------------------------------
# ---- Who can be woken (SS17) -----------------------------------------------
# ---------------------------------------------------------------------------

# Set around a run that something will WAKE when its sessions finish: the TUI
# chat turn, the CLI chat loop, a subagent run. A ContextVar rather than a
# registry of thread ids, because the question is asked by a tool's
# refusal_check, which is handed params and a ToolContext and never learns
# which thread it runs on -- and a ContextVar follows the run into the NA10
# pool, which submits through copy_context().
_CONSUMING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "shell_session_consumer", default=False)


@contextmanager
def consuming():
    """Mark the code inside as a run whose sessions will be woken for."""
    token = _CONSUMING.set(True)
    try:
        yield
    finally:
        _CONSUMING.reset(token)


def has_consumer() -> bool:
    """Whether the current run can be woken. A research pass cannot, and
    under `shell_approval_mode: never` nothing else would stop it starting a
    session that no one would ever report (ROADMAP_v3 §49, SS17)."""
    return _CONSUMING.get()


# ---------------------------------------------------------------------------
# ---- Output ------------------------------------------------------------------
# ---------------------------------------------------------------------------


class OutputBuffer:
    """A session's output: the first `head_chars` and the most recent
    `tail_chars`, in memory only (SS12). Offsets are ABSOLUTE character
    positions in everything the program wrote, so a page that falls in the
    dropped middle can say exactly which span is gone."""

    def __init__(self, head_chars: int, tail_chars: int) -> None:
        self._head_chars = head_chars
        self._tail_chars = tail_chars
        self._lock = threading.Lock()
        self._head: list[str] = []
        self._head_len = 0
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self.total = 0

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self.total += len(text)
            room = self._head_chars - self._head_len
            if room > 0:
                part = text[:room]
                self._head.append(part)
                self._head_len += len(part)
                text = text[room:]
            if not text:
                return
            self._tail.append(text)
            self._tail_len += len(text)
            excess = self._tail_len - self._tail_chars
            while excess > 0 and self._tail:
                first = self._tail[0]
                if len(first) <= excess:
                    self._tail.popleft()
                    self._tail_len -= len(first)
                    excess -= len(first)
                else:
                    self._tail[0] = first[excess:]
                    self._tail_len -= excess
                    excess = 0

    def _parts(self) -> tuple[str, str, int]:
        with self._lock:
            return "".join(self._head), "".join(self._tail), self.total

    def page(self, offset: int, limit: int) -> dict:
        """Up to *limit* characters from *offset*, never across the gap."""
        head, tail, total = self._parts()
        tail_start = total - len(tail)
        gap = ({"from": len(head), "to": tail_start}
               if tail_start > len(head) else None)
        offset = max(0, int(offset))
        page: dict = {"total_chars": total}
        if offset < len(head):
            text = head[offset:offset + limit]
            end = offset + len(text)
            next_offset = tail_start if (gap and end >= len(head)) else end
            if gap and end >= len(head):
                page["gap"] = gap
        elif gap and offset < tail_start:
            page["gap"] = gap
            text = tail[:limit]
            offset = tail_start
            next_offset = tail_start + len(text)
        else:
            start = offset - tail_start
            text = tail[start:start + limit]
            next_offset = offset + len(text)
        page.update(offset=offset, text=text, next_offset=next_offset,
                    more=next_offset < total)
        return page

    def last(self, chars: int) -> tuple[str, bool]:
        """The most recent *chars* characters kept, and whether anything the
        program wrote before them is missing from what is returned."""
        head, tail, total = self._parts()
        kept = tail if total - len(tail) > len(head) else head + tail
        text = kept[-chars:] if chars > 0 else ""
        return text, len(text) < total


# ---------------------------------------------------------------------------
# ---- Records ---------------------------------------------------------------
# ---------------------------------------------------------------------------


@dataclass
class WakeEvent:
    """One session's contribution to one wake. Coalesced per session: a
    monitor that matched ten lines before the wake was taken is ONE event
    with ten lines, and a finish folds any untaken matches into itself."""

    session_id: str
    call_id: str
    kind: str
    command: str
    shape: str
    lines: list[str] = field(default_factory=list)
    match_count: int = 0
    return_code: Optional[int] = None
    elapsed_s: float = 0.0
    ran_on: str = ""
    tier: str = ""
    kill_reason: str = ""
    output_tail: str = ""
    output_truncated: bool = False
    # What a monitor was watching for, and the timeout it ran under -- so a
    # wake can say "matched `FAILED`" and "stopped at its 600s timeout"
    # without asking the manager for a session it may have pruned.
    pattern: Optional[str] = None
    timeout_s: int = 0


@dataclass(frozen=True)
class SessionRow:
    """A snapshot of one session, for a panel or a view. Frozen and copied,
    never the live object (NA16)."""

    id: str
    kind: str
    command: str
    rationale: str
    pattern: Optional[str]
    owner_thread: str
    owner_agent: Optional[str]
    owner_depth: int
    owner_span_id: Optional[str]
    call_id: str
    state: str
    started_wall: float
    timeout_s: int
    match_count: int
    output_chars: int
    ran_on: str
    tier: str
    return_code: Optional[int]


@dataclass
class _Session:
    id: str
    kind: str
    command: str
    rationale: str
    pattern: Optional[str]
    owner_thread: str
    owner_agent: Optional[str]
    owner_depth: int
    owner_span_id: Optional[str]
    call_id: str
    requested_timeout_s: int
    timeout_s: int
    capped: bool
    output: OutputBuffer
    matcher: object = None
    started_mono: float = 0.0
    started_wall: float = 0.0
    finished_mono: Optional[float] = None
    state: str = STARTING
    process: object = None
    ran_on: str = ""
    tier: str = ""
    return_code: Optional[int] = None
    kill_reason: str = ""
    timed_out: bool = False
    match_count: int = 0
    # Interactive only. `pty` strips escape sequences and takes the
    # prompt token back out (SS27, SS34); `last_input_mono` is what the
    # idle deadline is measured from (SS25).
    prompt_token: str = ""
    pty: object = None
    last_input_mono: float = 0.0
    # The agent's own kill takes the finish as its RESULT instead of a wake
    # (`kill`). Claimed and released under the manager lock, which `_finish`
    # also holds when it reads the claim.
    kill_report_claimed: bool = False
    final_event: Optional[WakeEvent] = None

    def row(self) -> SessionRow:
        return SessionRow(
            id=self.id, kind=self.kind, command=self.command,
            rationale=self.rationale, pattern=self.pattern,
            owner_thread=self.owner_thread, owner_agent=self.owner_agent,
            owner_depth=self.owner_depth, owner_span_id=self.owner_span_id,
            call_id=self.call_id, state=self.state,
            started_wall=self.started_wall, timeout_s=self.timeout_s,
            match_count=self.match_count, output_chars=self.output.total,
            ran_on=self.ran_on, tier=self.tier, return_code=self.return_code)


@dataclass
class _Inbox:
    events: list[WakeEvent] = field(default_factory=list)
    held: list[WakeEvent] = field(default_factory=list)
    consecutive_wakes: int = 0
    suspended: bool = False


class SessionActivity:
    """The sink a shell supplies. The base class is the null sink.

    `changed` receives every session row after anything moved; `wake_ready`
    says a thread has results waiting. Both are called OUTSIDE the manager's
    lock, and a raise inside either is contained: display machinery must not
    fail the session it describes (core/agent_activity.py's rule)."""

    def changed(self, rows: list[SessionRow]) -> None:
        pass

    def wake_ready(self, thread_id: str) -> None:
        pass


NULL_SINK = SessionActivity()


# ---------------------------------------------------------------------------
# ---- The manager -------------------------------------------------------------
# ---------------------------------------------------------------------------


def _default_starter(*args, **kwargs):
    from security import sandbox
    # An interactive session is the one shape `start_sandboxed` cannot
    # serve: it needs a stdin pipe, the pty wrapper, and the container
    # route or nothing (SS28). The kwarg is only ever passed for that
    # kind, so a slice-1 fake starter sees the call it always saw.
    if kwargs.get("prompt_token"):
        kwargs.pop("command", None)
        args = args[1:]          # the command is typed in, not run
        return sandbox.start_interactive(*args, **kwargs)
    return sandbox.start_sandboxed(*args, **kwargs)



def _new_prompt_token() -> str:
    """One interactive session's prompt token (SS34).

    Imported lazily for `_default_starter`'s reason: core must not import
    the sandbox at module scope. Minted THERE rather than here because the
    token and the shell string that embeds it are one decision, and the
    character set is what makes that embedding safe.
    """
    from security import sandbox
    return sandbox.new_prompt_token()


class SessionManager:
    """Every background and monitor session in this process."""

    def __init__(self, starter: Optional[Callable] = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 poll_s: float = 0.5) -> None:
        self._starter = starter or _default_starter
        self._clock = clock
        self._wall_clock = wall_clock
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._sessions: dict[str, _Session] = {}
        self._inboxes: dict[str, _Inbox] = {}
        self._ids = itertools.count(1)
        self._closing = False
        self._sink: SessionActivity = NULL_SINK

    # -- configuration ---------------------------------------------------------

    def set_sink(self, sink: Optional[SessionActivity]) -> None:
        self._sink = sink or NULL_SINK

    @property
    def sink(self) -> SessionActivity:
        """Who is being told, so a shell can drop ITS OWN registration
        without dropping a later one's. The manager is a module
        singleton and a shell is not: an app tearing down unconditionally
        would silence whatever mounted after it."""
        return self._sink

    @staticmethod
    def effective_timeout(requested: int) -> tuple[int, bool]:
        """(the timeout a session runs with, whether the cap applied) --
        THE clamp (SS13). Nothing else compares against the cap."""
        cap = int(config.SHELL_SESSION_TIMEOUT_CAP_S)
        requested = int(requested)
        return (cap, True) if requested > cap else (requested, False)

    # -- starting --------------------------------------------------------------

    def start_refusal(self, kind: str, pattern: Optional[str] = None,
                      owner_thread=None) -> Optional[str]:
        """Why a start would be refused, or None. Asked BEFORE approval by
        the tools' refusal_check (§32 A7) and again inside `start`."""
        if not has_consumer():
            return ("Background sessions can only be started from a chat "
                    "turn or a subagent -- something that is woken when they "
                    "finish. This run cannot be.")
        if kind == KIND_MONITOR:
            reason = validate(pattern)
            if reason is not None:
                return (f"Invalid pattern: {reason}. Patterns use RE2 syntax: "
                        f"no backreferences or lookaround.")
        with self._lock:
            return self._capacity_refusal_locked(owner_thread)

    def _capacity_refusal_locked(self, owner_thread) -> Optional[str]:
        if self._closing:
            return "The harness is quitting; no new sessions can start."
        cap = int(config.SHELL_SESSION_MAX_LIVE)
        live = [s for s in self._sessions.values() if s.state in LIVE_STATES]
        if len(live) < cap:
            return None
        # With no owner -- the tools' pre-approval check, which is handed
        # params and a context and never learns the thread -- say only what
        # is true of every caller.
        yours = [s.id for s in live if s.owner_thread == str(owner_thread)]
        if owner_thread is None:
            mine = (" List yours with shell_sessions and stop one with "
                    "shell_kill, or wait for one to finish.")
        elif yours:
            mine = (f" Yours: {', '.join(yours)}; stop one with shell_kill, "
                    f"or wait for one to finish.")
        else:
            mine = (" None of them is this conversation's, so wait for one "
                    "to finish.")
        return (f"{len(live)} background sessions are already running, which "
                f"is the limit, shared with any subagents.{mine}")

    def start(self, *, kind: str, command: str, profile, docker_available: bool,
              requested_timeout_s: int, owner_thread, workspace_dir: str,
              call_id: str = "", rationale: str = "",
              pattern: Optional[str] = None,
              open_wait_s: float = 10.0,
              backend: str = "container",
              needs_root: bool = False) -> dict:
        """Start a session and return what the agent is told. Raises
        SessionRefused with the reason, or the backend's SandboxUnavailable."""
        refusal = self.start_refusal(kind, pattern, owner_thread)
        if refusal:
            raise SessionRefused(refusal)
        timeout_s, capped = self.effective_timeout(requested_timeout_s)
        matcher = compile_pattern(pattern) if kind == KIND_MONITOR else None
        interactive = kind == KIND_INTERACTIVE
        token = _new_prompt_token() if interactive else ""
        span = agent_activity.current()
        with self._lock:
            refusal = self._capacity_refusal_locked(owner_thread)
            if refusal:
                raise SessionRefused(refusal)
            session = _Session(
                id=f"s{next(self._ids)}", kind=kind, command=command,
                rationale=rationale or "", pattern=pattern,
                owner_thread=str(owner_thread),
                owner_agent=span.name if span else None,
                owner_depth=span.depth if span else 0,
                owner_span_id=span.id if span else None,
                call_id=call_id or "",
                requested_timeout_s=int(requested_timeout_s),
                timeout_s=timeout_s, capped=capped,
                output=OutputBuffer(int(config.SHELL_SESSION_OUTPUT_HEAD_CHARS),
                                    int(config.SHELL_SESSION_OUTPUT_TAIL_CHARS)),
                matcher=matcher, started_mono=self._clock(),
                started_wall=self._wall_clock(),
                prompt_token=token,
                pty=PtyStream(token) if interactive else None,
                last_input_mono=self._clock())
            self._sessions[session.id] = session
        # ROADMAP_v3 §49 slice 3. Passed to the backend, not stored on the
        # session: what the session RECORDS about where it ran is
        # `ran_on`, read back off the process below, so a request and an
        # outcome cannot disagree in the record. Interactive never carries
        # one -- `start_interactive` takes no backend from this layer.
        #
        # Passed ONLY when it is not the default, which is `prompt_token`'s
        # rule and keeps its property: a starter written against slice 1 --
        # every fake in the suite -- sees the call it has always seen, and
        # one that wants to observe the backend opts in by accepting it.
        # "container" and "no backend given" are the same request, so there
        # is nothing a caller can express that this drops.
        extra = {"prompt_token": token} if interactive else {}
        if not interactive and backend != "container":
            extra["backend"] = backend
        # Slice 5b, on `backend`'s own terms: passed to the backend, not
        # stored on the session, and only when it is true -- so a slice-1
        # fake starter still sees the call it has always seen.
        if not interactive and needs_root:
            extra["needs_root"] = True
        try:
            process = self._starter(command, workspace_dir, profile=profile,
                                    docker_available=docker_available,
                                    timeout_s=timeout_s, **extra)
        except Exception:
            with self._lock:
                session.state = FAILED
                session.finished_mono = self._clock()
                rows = self._rows_locked()
            self._post(rows)
            raise
        with self._lock:
            session.process = process
            session.ran_on = getattr(process, "ran_on", "")
            session.tier = getattr(process, "tier", "") or getattr(
                profile, "tier", "")
            session.state = RUNNING
            killed_early = bool(session.kill_reason)
            rows = self._rows_locked()
        threading.Thread(target=self._supervise, args=(session,),
                         name=f"shell-session-{session.id}",
                         daemon=True).start()
        if killed_early:
            process.kill()
        self._post(rows)
        if interactive:
            return self._open_interactive(session, command, open_wait_s)
        return self._started_result(session)

    def _open_interactive(self, session: _Session, command: str,
                          wait_s: float) -> dict:
        """Wait for the shell's first prompt, then TYPE the opening command.

        WAITED FOR, not fired off. The terminal driver echoes what reaches
        it before `stty -echo` has run, and that window is the container's
        whole startup -- so input written the instant Popen returns would
        be echoed back EVERY time, not rarely. Waiting for the prompt also
        turns a shell that never comes up into a clear refusal instead of a
        session that accepts input nothing will ever read.

        TYPED, not handed to the container (SS32). The text is classified
        and approved exactly as `shell` would classify it, and then it
        lands in the shell that was measured here -- rather than becoming
        the container's command, where whatever it started would be the
        thing holding the pty and the prompt token would never come back.
        """
        if not self._await_prompt(session, _OPEN_BOUND_S):
            self.kill(session.id, owner_thread=session.owner_thread,
                      reason=KILL_OPEN_FAILED)
            raise SessionRefused(
                "The shell did not reach a prompt within "
                f"{int(_OPEN_BOUND_S)}s and was stopped. The container "
                "image must provide bash and util-linux's `script`.")
        result = self._started_result(session)
        if command:
            sent = self.send(session.id, session.owner_thread,
                             text=command, wait_s=wait_s)
            for key in ("text", "ended", "ended_meaning", "next_offset",
                        "total_chars", "more", "gap"):
                if key in sent:
                    result[key] = sent[key]
        return result

    def _await_prompt(self, session: _Session, bound_s: float) -> bool:
        """Whether the shell reached its prompt inside *bound_s*."""
        deadline = self._clock() + bound_s
        while self._clock() < deadline:
            pty = session.pty
            if pty is not None and pty.ready_count > 0 and pty.at_prompt:
                return True
            if session.state not in LIVE_STATES:
                return False
            time.sleep(_INPUT_POLL_S)
        return False

    def _started_result(self, session: _Session) -> dict:
        cap = int(config.SHELL_SESSION_TIMEOUT_CAP_S)
        if session.kind == KIND_INTERACTIVE:
            idle = int(config.SHELL_SESSION_IDLE_TIMEOUT_S)
            note = (
                "An interactive shell is open. Send it input with "
                "shell_input; what you do in it -- the working directory, "
                "variables, a REPL you started -- survives between your "
                "turns. It does NOT block the user, and nothing wakes you "
                f"when it ends: it closes after {idle}s with no input, or "
                f"at its {session.timeout_s}s cap, whichever comes first. "
                "Full-screen programs (vim, htop, less) are not supported.")
        else:
            woken = ("it finishes, times out, or a line matches your pattern"
                     if session.kind == KIND_MONITOR else
                     "it finishes or times out")
            note = (f"Runs in the background. You will be woken when "
                    f"{woken}; do not poll it.")
        if session.capped:
            note += (f" You asked for {session.requested_timeout_s}s, which is "
                     f"above the {cap}s cap, so it runs for {cap}s.")
        result = {"session": session.id, "kind": session.kind,
                  "status": RUNNING, "command": session.command,
                  "timeout_s": session.timeout_s,
                  "timeout_capped": session.capped, "timeout_cap_s": cap,
                  "ran_on": session.ran_on, "tier": session.tier,
                  "note": note}
        if session.kind == KIND_MONITOR:
            result["pattern"] = session.pattern
        return result

    # -- the two threads -------------------------------------------------------

    def _supervise(self, session: _Session) -> None:
        reader = threading.Thread(target=self._read, args=(session,),
                                  name=f"shell-session-{session.id}-reader",
                                  daemon=True)
        reader.start()
        process = session.process
        deadline = session.started_mono + session.timeout_s
        # SS25. An interactive session also dies of being IGNORED. Its
        # wall-clock cap is the same one every session has, but a shell
        # nobody is typing into is holding a container for nothing, and
        # because it does not block the user's prompt there is no other
        # pressure to close it.
        idle_s = (int(config.SHELL_SESSION_IDLE_TIMEOUT_S)
                  if session.kind == KIND_INTERACTIVE else 0)
        code = None
        while True:
            remaining = deadline - self._clock()
            if idle_s:
                idle_left = session.last_input_mono + idle_s - self._clock()
                if idle_left <= 0:
                    with self._lock:
                        if not session.kill_reason:
                            session.kill_reason = KILL_IDLE
                    process.kill()
                    break
                remaining = min(remaining, idle_left)
            if remaining <= 0:
                with self._lock:
                    if not session.kill_reason:
                        session.timed_out = True
                process.kill()
                break
            code = process.wait(min(remaining, self._poll_s))
            if code is not None:
                break
        if code is None:
            code = process.wait(_KILL_SETTLE_S)
        reader.join(timeout=10)
        self._finish(session, code)

    def _read(self, session: _Session) -> None:
        stream = session.process.stdout
        read = getattr(stream, "read1", None) or stream.read
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        try:
            while True:
                chunk = read(_READ_CHUNK)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if session.pty is not None:
                    # BEFORE the buffer and before every match (SS27), so
                    # nothing downstream ever holds a version of this text
                    # with the escape sequences still in it.
                    text = session.pty.feed(text)
                session.output.append(text)
                if session.matcher is not None:
                    pending = self._match_complete_lines(session, pending + text)
        except (OSError, ValueError):
            logger.debug("session %s output stream closed", session.id)
        text = decoder.decode(b"", final=True)
        if session.pty is not None:
            # An unterminated escape sequence at the end of the stream was
            # never a sequence, and held-back text is owed to the reader.
            text = session.pty.feed(text) + session.pty.flush()
        session.output.append(text)
        if session.matcher is not None:
            pending = self._match_complete_lines(session, pending + text)
            if pending:
                self._match(session, pending)

    def _match_complete_lines(self, session: _Session, text: str) -> str:
        *lines, pending = text.split("\n")
        for line in lines:
            self._match(session, line.rstrip("\r"))
        while len(pending) > _MAX_LINE_CHARS:
            self._match(session, pending[:_MAX_LINE_CHARS])
            pending = pending[_MAX_LINE_CHARS:]
        return pending

    def _match(self, session: _Session, line: str) -> None:
        # Outside every lock: re2 releases the GIL, and a long line must not
        # hold up a kill or a snapshot.
        if not session.matcher.search(line):
            return
        with self._lock:
            session.match_count += 1
            inbox = self._inboxes.setdefault(session.owner_thread, _Inbox())
            target = inbox.held if inbox.suspended else inbox.events
            event = next((e for e in target if e.session_id == session.id
                          and e.shape == SHAPE_MATCHED), None)
            if event is None:
                event = self._event_locked(session, SHAPE_MATCHED)
                target.append(event)
            event.match_count += 1
            if len(event.lines) < MAX_MATCHED_LINES:
                event.lines.append(line)
            wake = not inbox.suspended
            self._changed.notify_all()
            rows = self._rows_locked()
        self._post(rows, session.owner_thread if wake else None)

    def _finish(self, session: _Session, code: Optional[int]) -> None:
        tail, truncated = session.output.last(int(config.MAX_READ_CHARS))
        with self._lock:
            if session.kill_reason:
                session.state = KILLED
            elif session.timed_out:
                session.state = TIMED_OUT
            else:
                session.state = EXITED
            session.return_code = code
            session.finished_mono = self._clock()
            inbox = self._inboxes.setdefault(session.owner_thread, _Inbox())
            event = self._event_locked(session, session.state)
            event.output_tail = tail
            event.output_truncated = truncated
            for queue in (inbox.events, inbox.held):
                for matched in [e for e in queue
                                if e.session_id == session.id
                                and e.shape == SHAPE_MATCHED]:
                    event.lines.extend(matched.lines)
                    event.match_count += matched.match_count
                    queue.remove(matched)
            del event.lines[MAX_MATCHED_LINES:]
            # SS19. A kill the USER made between turns does not wake the main
            # conversation -- it arrives with their next message. A
            # subagent's session still wakes the subagent, which has no next
            # message.
            # The agent's own kill takes this event as its RESULT, so it is
            # queued for nobody: `kill` is still waiting for it, and a wake
            # turn telling the agent about a session it just stopped is a
            # model call nobody needed. Read under the lock `kill` releases
            # the claim under, so a session that outlives the wait is
            # reported the ordinary way instead of not at all.
            # SS31. An interactive session's end is HELD whoever ended it.
            # It does not block the user's prompt (SS25), so a wake for one
            # would be a turn starting underneath the user's cursor -- and
            # there is nothing in it the agent is waiting on, since every
            # send was already answered when it was made.
            claimed = session.kill_report_claimed
            hold = not claimed and (
                inbox.suspended
                or session.kind == KIND_INTERACTIVE
                or (session.kill_reason == KILL_USER
                    and session.owner_depth == 0))
            if claimed:
                session.final_event = event
            else:
                (inbox.held if hold else inbox.events).append(event)
            self._prune_locked()
            self._changed.notify_all()
            rows = self._rows_locked()
        self._post(rows, None if (hold or claimed) else session.owner_thread)

    def _event_locked(self, session: _Session, shape: str) -> WakeEvent:
        return WakeEvent(
            session_id=session.id, call_id=session.call_id, kind=session.kind,
            command=session.command, shape=shape,
            return_code=session.return_code,
            elapsed_s=round(self._clock() - session.started_mono, 1),
            ran_on=session.ran_on, tier=session.tier,
            kill_reason=session.kill_reason, pattern=session.pattern,
            timeout_s=session.timeout_s)

    def _prune_locked(self) -> None:
        finished = sorted((s for s in self._sessions.values()
                           if s.state not in LIVE_STATES),
                          key=lambda s: s.finished_mono or 0.0)
        for old in finished[:-_FINISHED_KEPT]:
            del self._sessions[old.id]

    # -- what the consumers ask --------------------------------------------------

    def _live_locked(self, owner_thread, kinds=None) -> int:
        """How many of this thread's sessions are live, of *kinds*.

        The kinds are a parameter because two different questions are
        asked of this one list and they have different answers (SS25):
        what is RUNNING, which is every kind, and what the user's prompt
        is waiting on, which is not. Defaulting to all of them keeps the
        broad question the cheap one to ask.
        """
        owner = str(owner_thread)
        return sum(1 for s in self._sessions.values()
                   if s.owner_thread == owner and s.state in LIVE_STATES
                   and (kinds is None or s.kind in kinds))

    def live_for(self, owner_thread) -> int:
        with self._lock:
            return self._live_locked(owner_thread)

    def blocks(self, owner_thread) -> bool:
        """Whether this thread's input is blocked (SS2): it has a live
        BLOCKING session and waking has not been suspended by the
        consecutive-wake limit.

        An interactive session is deliberately not counted (SS25): the
        block exists because the agent is waiting on work the user cannot
        usefully interrupt, and an open shell is not that.
        """
        with self._lock:
            inbox = self._inboxes.get(str(owner_thread))
            return (self._live_locked(owner_thread, BLOCKING_KINDS) > 0
                    and not (inbox and inbox.suspended))

    def suspended(self, owner_thread) -> bool:
        with self._lock:
            inbox = self._inboxes.get(str(owner_thread))
            return bool(inbox and inbox.suspended)

    def pending(self, owner_thread) -> bool:
        with self._lock:
            inbox = self._inboxes.get(str(owner_thread))
            return bool(inbox and inbox.events and not inbox.suspended)

    def _take_locked(self, owner_thread) -> tuple[Optional[list[WakeEvent]], bool]:
        """(the batch to wake with, whether waking was just suspended)."""
        inbox = self._inboxes.get(str(owner_thread))
        if inbox is None or not inbox.events or inbox.suspended:
            return None, False
        if inbox.consecutive_wakes >= int(
                config.SHELL_SESSION_MAX_CONSECUTIVE_WAKES):
            inbox.suspended = True
            inbox.held.extend(inbox.events)
            inbox.events = []
            self._changed.notify_all()
            return None, True
        batch, inbox.events = inbox.events, []
        inbox.consecutive_wakes += 1
        return batch, False

    def take_wake(self, owner_thread) -> Optional[list[WakeEvent]]:
        """Everything waiting to wake this thread, taken atomically -- or None
        when nothing is, or when the consecutive-wake limit just stopped
        waking (SS2), in which case the results move to `held`."""
        with self._lock:
            batch, suspended_now = self._take_locked(owner_thread)
            rows = self._rows_locked() if suspended_now else None
        if rows is not None:
            self._post(rows)
        return batch

    def wait_for_wake(self, owner_thread,
                      timeout: Optional[float] = None
                      ) -> Optional[list[WakeEvent]]:
        """Block until there is a batch to wake this thread with. None when
        there is nothing left to wait for: no live session and nothing
        pending, waking suspended, the harness quitting, or *timeout* gone.
        Waits in `poll_s` slices, so a KeyboardInterrupt still lands."""
        deadline = None if timeout is None else self._clock() + timeout
        rows = None
        with self._changed:
            while True:
                batch, suspended_now = self._take_locked(owner_thread)
                if batch is not None:
                    return batch
                if suspended_now:
                    rows = self._rows_locked()
                    break
                inbox = self._inboxes.get(str(owner_thread))
                # BLOCKING_KINDS for SS25's reason, and here it is load
                # bearing: this loop keeps a turn alive while something is
                # still running, and an idle interactive shell would hold
                # the turn open exactly as a hung read would.
                if (self._closing or (inbox and inbox.suspended)
                        or not self._live_locked(owner_thread,
                                                 BLOCKING_KINDS)):
                    break
                if deadline is not None and self._clock() >= deadline:
                    break
                self._changed.wait(self._poll_s)
        if rows is not None:
            self._post(rows)
        return None

    def note_user_input(self, owner_thread) -> None:
        """A message from the user resets the consecutive-wake count and
        resumes waking (SS2). Held results stay held until `take_held`."""
        with self._lock:
            inbox = self._inboxes.setdefault(str(owner_thread), _Inbox())
            inbox.consecutive_wakes = 0
            inbox.suspended = False
            rows = self._rows_locked()
        self._post(rows)

    def take_held(self, owner_thread) -> list[WakeEvent]:
        """Results held for the user's next message: past the wake limit, or
        from a kill the user made (SS2, SS19)."""
        with self._lock:
            inbox = self._inboxes.get(str(owner_thread))
            if inbox is None or not inbox.held:
                return []
            held, inbox.held = inbox.held, []
            return held

    # -- what the tools ask --------------------------------------------------------

    def _owned_locked(self, session_id: str, owner_thread) -> Optional[_Session]:
        """This thread's session of that id, or None.

        `ANY_OWNER` is the only way past the check, and it is a value a
        caller has to name (see its comment above). Everything else is
        compared, `None` INCLUDED -- a missing thread id owns nothing, which
        is the same answer `list_for` gives it one function down.
        """
        session = self._sessions.get(str(session_id))
        if session is None:
            return None
        if owner_thread is ANY_OWNER:
            return session
        if owner_thread is None or session.owner_thread != str(owner_thread):
            return None
        return session

    def list_for(self, owner_thread) -> list[dict]:
        now = self._clock()
        with self._lock:
            sessions = [s for s in self._sessions.values()
                        if s.owner_thread == str(owner_thread)]
            return [{"session": s.id, "kind": s.kind, "command": s.command,
                     "status": s.state,
                     "elapsed_s": round((s.finished_mono or now)
                                        - s.started_mono, 1),
                     "timeout_s": s.timeout_s, "return_code": s.return_code,
                     "output_chars": s.output.total,
                     "match_count": s.match_count}
                    for s in sessions]

    def output(self, session_id: str, owner_thread, offset: int = 0,
               limit: Optional[int] = None) -> dict:
        limit = int(limit or config.MAX_READ_CHARS)
        with self._lock:
            session = self._owned_locked(session_id, owner_thread)
            if session is None:
                return {"error": f"No session {session_id} in this "
                                 f"conversation."}
            state = session.state
            buffer = session.output
        page = buffer.page(offset, limit)
        page.update(session=str(session_id), status=state)
        return page

    def send(self, session_id: str, owner_thread, *, text: str = "",
             control: str = "", wait_s: float = 10.0,
             wait_for: Optional[str] = None) -> dict:
        """Type into an interactive session and wait for it, BOUNDED (SS26).

        Returns everything the session wrote since this call began, and
        `ended`, naming which of the four bounds ended the wait. Every one of
        them returns: a send cannot wait forever, because the turn it is in
        cannot either -- that is the whole reason the gap register called a
        hung read a wedge.

        An empty *text* with no *control* writes nothing and only waits,
        which is how a caller reads more of something still running.
        """
        if control and control not in CONTROLS:
            return {"error": f"Unknown control {control!r}. Use one of: "
                             f"{', '.join(sorted(CONTROLS))}."}
        matcher = None
        if wait_for:
            reason = validate(wait_for)
            if reason is not None:
                return {"error": f"Invalid pattern: {reason}. Patterns use "
                                 f"RE2 syntax: no backreferences or "
                                 f"lookaround."}
            matcher = compile_pattern(wait_for)
        if control:
            payload = CONTROLS[control]
        elif text:
            # The newline is added rather than demanded: a line the shell
            # never sees because its terminator was forgotten looks exactly
            # like a command that produced no output.
            payload = text if text.endswith("\n") else text + "\n"
        else:
            payload = ""
        size = len(payload.encode("utf-8"))
        if size > MAX_INPUT_BYTES:
            return {"error": (
                f"That input is {size} bytes. A pty accepts "
                f"{_PTY_LINE_LIMIT} on one line and silently drops the rest, "
                f"so the shell would run a TRUNCATED command and report "
                f"success -- it is refused here instead. Write it to a file "
                f"with `write` and run the file.")}
        with self._lock:
            session = self._owned_locked(session_id, owner_thread)
            if session is None:
                return {"error": f"No session {session_id} in this "
                                 f"conversation."}
            if session.kind != KIND_INTERACTIVE:
                return {"error": f"Session {session.id} is a {session.kind} "
                                 f"session and cannot be typed into. Only an "
                                 f"interactive session takes input."}
            if session.state not in LIVE_STATES:
                return {"error": f"Session {session.id} has already finished "
                                 f"({session.state})."}
            process, pty = session.process, session.pty
            mark = session.output.total
            # ONE rule: a prompt that arrives AFTER this write, with the
            # stream then at rest on it. Three cheaper rules were measured
            # first and each was wrong somewhere.
            #
            # `at_prompt` alone says the stream ENDS with a prompt, which is
            # not the same as the shell being idle -- with the echo off
            # (SS33) a command produces nothing at all when it starts, so
            # the last prompt stands on the stream for as long as a silent
            # command runs. Measured: a wait issued while `sleep 2` ran read
            # that stale prompt and returned `ready` before the command had
            # finished. Nothing observable distinguishes the two; only this
            # side knows a line was written.
            #
            # Counting two prompts when the shell looked busy fixed the
            # queued case and broke leaving a REPL, where `exit()` is eaten
            # by python and one prompt follows. Those two are identical from
            # out here, and telling them apart means emulating the terminal.
            #
            # So: always the NEXT prompt, and always let it settle. A prompt
            # that is merely passed through on the way to running a queued
            # line is followed by that line's output within milliseconds, so
            # it never settles; the prompt that ends the work does. The cost
            # is the quiet interval on every send, which is a third of a
            # second against a model round trip.
            ready_target = pty.ready_count + 1
            # Reset the idle clock on the INPUT, not on the output: a session
            # printing to itself forever is still one nobody is using (SS25).
            session.last_input_mono = self._clock()
        if payload:
            stdin = getattr(process, "stdin", None)
            if stdin is None:
                return {"error": f"Session {session.id} has no input stream."}
            try:
                stdin.write(payload.encode("utf-8"))
                stdin.flush()
            except (OSError, ValueError) as e:
                return {"error": f"Could not reach session {session.id}: {e}."}
        return self._wait_after_input(session, mark, ready_target, wait_s,
                                      matcher)

    def _wait_after_input(self, session: _Session, mark: int,
                          ready_target: int, wait_s: float, matcher) -> dict:
        """SS26's four bounds, in the order they can be believed.

        `ready` is checked first because it is the only one that is a FACT
        about the shell rather than an observation about silence, and it
        ends a wait for a pattern that is never coming.
        """
        pty = session.pty
        deadline = self._clock() + max(0.0, float(wait_s))
        last_total, last_change = mark, self._clock()
        searched, carry = mark, ""
        while True:
            total = session.output.total
            if (pty.ready_count >= ready_target and pty.at_prompt
                    and self._clock() - last_change >= _INPUT_QUIET_S):
                ended = "ready"
                break
            if matcher is not None and total > searched:
                page = session.output.page(searched, _MATCH_SLICE)
                searched = int(page.get("next_offset", searched))
                *lines, carry = (carry + page.get("text", "")).split("\n")
                if len(carry) > _MAX_LINE_CHARS:
                    lines.append(carry[:_MAX_LINE_CHARS])
                    carry = carry[_MAX_LINE_CHARS:]
                if any(matcher.search(line.rstrip("\r")) for line in lines):
                    ended = "matched"
                    break
            now = self._clock()
            if total != last_total:
                last_total, last_change = total, now
            elif (matcher is None and total > mark
                    and now - last_change >= _INPUT_QUIET_S):
                # Two conditions, each measured into existence.
                #
                # Only once something HAS arrived: a command that prints
                # nothing while it works -- which is most of them, now that
                # the echo is off -- would otherwise report `quiet` after a
                # third of a second and read as finished.
                #
                # And NEVER when a pattern was given. A caller passing
                # `wait_for` has said what it is waiting for, and silence is
                # not it: measured, `wait_for="line2"` against a loop
                # printing a line a second returned `quiet` after line1 --
                # answering a question nobody asked, and leaving the loop
                # running to desynchronize every later call.
                ended = "quiet"
                break
            if session.state not in LIVE_STATES:
                ended = "ended"
                break
            if now >= deadline:
                ended = "bound"
                break
            time.sleep(_INPUT_POLL_S)
        page = session.output.page(mark, int(config.MAX_READ_CHARS))
        page.update(session=session.id, status=session.state, ended=ended,
                    ended_meaning=_ENDED_MEANING[ended])
        return page

    def kill(self, session_id: str, *, owner_thread,
             reason: str = KILL_MODEL) -> dict:
        """Stop a session and report its terminal state. *owner_thread*
        `ANY_OWNER` is the user's or the harness's kill, which may name any
        session; a thread id is the model's, which may name only its own.

        REQUIRED, with no default, since batch 110. The default was `None`
        and `None` meant "skip the ownership check", so the safe call was
        the one a caller had to remember to make. There are four call sites
        and each of them now says which authority it is exercising.

        A kill the MODEL makes is its own report: this returns what the wake
        would have said -- the final state, the end of the output, and any
        matched lines nobody had taken yet -- and no wake follows, because
        the agent is the one that asked. The claim belongs to the call that
        SET the reason, so a second killer of the same session does not take
        a report out from under the first, and it is given back if the
        session outlives the wait, which puts the finish back on the
        ordinary wake path rather than dropping it."""
        with self._lock:
            session = self._owned_locked(session_id, owner_thread)
            if session is None:
                return {"error": f"No session {session_id} in this "
                                 f"conversation."}
            if session.state not in LIVE_STATES:
                return {"session": session.id, "status": session.state,
                        "return_code": session.return_code,
                        "note": "already finished"}
            claimed = False
            if not session.kill_reason:
                session.kill_reason = reason
                claimed = reason == KILL_MODEL
                session.kill_report_claimed = claimed
            process = session.process
        if process is not None:
            process.kill()
        with self._changed:
            self._changed.wait_for(lambda: session.state not in LIVE_STATES,
                                   timeout=_KILL_SETTLE_S)
            if claimed and session.state in LIVE_STATES:
                session.kill_report_claimed = False
            elif claimed and session.final_event is not None:
                return self._kill_report(session.final_event)
            return {"session": session.id, "status": session.state,
                    "return_code": session.return_code}

    @staticmethod
    def _kill_report(event: WakeEvent) -> dict:
        """What the agent is told about the session it just stopped."""
        report = {"session": event.session_id, "status": event.shape,
                  "return_code": event.return_code,
                  "elapsed_s": event.elapsed_s, "ran_on": event.ran_on,
                  "output_tail": event.output_tail,
                  "output_truncated": event.output_truncated,
                  "note": "Stopped. This is its final report; you will not "
                          "be woken for it."}
        if event.match_count:
            report["unreported_matches"] = {"count": event.match_count,
                                            "lines": list(event.lines)}
        return report

    def kill_owned(self, owner_thread, reason: str) -> list[str]:
        """Kill every live session this thread owns -- a subagent that ran
        out of wakes (KILL_WAKE_LIMIT).

        `ANY_OWNER` below because the ownership question was already asked,
        two lines up and by this function: re-asking it per id would either
        repeat the filter or, if the thread were None, quietly widen it."""
        with self._lock:
            ids = [s.id for s in self._sessions.values()
                   if s.owner_thread == str(owner_thread)
                   and s.state in LIVE_STATES]
        for session_id in ids:
            self.kill(session_id, owner_thread=ANY_OWNER, reason=reason)
        return ids

    def kill_by_span(self, owner_span_id: str, reason: str) -> list[str]:
        """Kill every live session started inside one agent-shaped run.

        The failure path's answer to a question the thread id cannot be
        asked: a subagent whose run RAISES never returns its thread, and
        `agent_activity`'s span is frozen and carries no address (the
        binding belongs to the sink). The span id is on the session from
        the moment it started, so what a failed run left behind is
        identifiable even though its conversation is not.

        `ANY_OWNER` for the same reason as `kill_owned`, and here it is the
        point rather than an economy: this sweep exists precisely because
        the thread id is not available, so there is nothing to check by."""
        if not owner_span_id:
            return []
        with self._lock:
            ids = [s.id for s in self._sessions.values()
                   if s.owner_span_id == owner_span_id
                   and s.state in LIVE_STATES]
        for session_id in ids:
            self.kill(session_id, owner_thread=ANY_OWNER, reason=reason)
        return ids

    # -- snapshots and shutdown ----------------------------------------------------

    def _rows_locked(self) -> list[SessionRow]:
        return [s.row() for s in self._sessions.values()]

    def rows(self) -> list[SessionRow]:
        with self._lock:
            return self._rows_locked()

    def live_rows(self) -> list[SessionRow]:
        with self._lock:
            return [s.row() for s in self._sessions.values()
                    if s.state in LIVE_STATES]

    def row(self, session_id: str) -> Optional[SessionRow]:
        with self._lock:
            session = self._sessions.get(str(session_id))
            return session.row() if session else None

    def buffer(self, session_id: str) -> Optional[OutputBuffer]:
        """A session's output buffer, for a shell's read-only view."""
        with self._lock:
            session = self._sessions.get(str(session_id))
            return session.output if session else None

    def close(self, reason: str = KILL_QUIT,
              budget_s: Optional[float] = None) -> dict[str, list[SessionRow]]:
        """Kill every live session, within one shared budget, and stop
        accepting new ones. Returns {owner thread: the rows killed}, for the
        caller to record (SS6). Idempotent: a second call finds nothing live.
        Releases every waiter."""
        budget = float(config.TEARDOWN_BUDGET_S if budget_s is None
                       else budget_s)
        with self._lock:
            self._closing = True
            live = [s for s in self._sessions.values()
                    if s.state in LIVE_STATES]
            for session in live:
                if not session.kill_reason:
                    session.kill_reason = reason
            self._changed.notify_all()
        killers = [threading.Thread(target=s.process.kill, daemon=True)
                   for s in live if s.process is not None]
        for killer in killers:
            killer.start()
        deadline = self._clock() + budget
        for killer in killers:
            killer.join(max(0.0, deadline - self._clock()))
        with self._changed:
            self._changed.wait_for(
                lambda: all(s.state not in LIVE_STATES for s in live),
                timeout=max(0.0, deadline - self._clock()))
            closed: dict[str, list[SessionRow]] = {}
            for session in live:
                closed.setdefault(session.owner_thread, []).append(
                    session.row())
            rows = self._rows_locked()
        self._post(rows)
        return closed

    @property
    def closing(self) -> bool:
        return self._closing

    def _post(self, rows: Optional[list[SessionRow]],
              wake_thread: Optional[str] = None) -> None:
        sink = self._sink
        if rows is not None:
            try:
                sink.changed(list(rows))
            except Exception:                   # noqa: BLE001 -- contained
                logger.exception("session sink raised on changed")
        if wake_thread is not None:
            try:
                sink.wake_ready(wake_thread)
            except Exception:                   # noqa: BLE001 -- contained
                logger.exception("session sink raised on wake_ready")


sessions = SessionManager()
"""The process's one session manager."""
