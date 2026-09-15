"""
core/shell_sessions.py

ROADMAP_v3 §49, slice 1. Background and monitor shell sessions: commands
that keep running after the tool call that started them has been answered,
and the one object that knows about all of them.

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
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

import config
from core import agent_activity
from core.line_pattern import compile_pattern, validate

logger = logging.getLogger(__name__)

KIND_BACKGROUND = "background"
KIND_MONITOR = "monitor"

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
    return sandbox.start_sandboxed(*args, **kwargs)


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
        yours = [s.id for s in live if s.owner_thread == str(owner_thread)]
        mine = (f" Yours: {', '.join(yours)}; stop one with shell_kill, or "
                f"wait for one to finish." if yours else
                " None of them is this conversation's, so wait for one to "
                "finish.")
        return (f"{len(live)} background sessions are already running, which "
                f"is the limit, shared with any subagents.{mine}")

    def start(self, *, kind: str, command: str, profile, docker_available: bool,
              requested_timeout_s: int, owner_thread, workspace_dir: str,
              call_id: str = "", rationale: str = "",
              pattern: Optional[str] = None) -> dict:
        """Start a session and return what the agent is told. Raises
        SessionRefused with the reason, or the backend's SandboxUnavailable."""
        refusal = self.start_refusal(kind, pattern, owner_thread)
        if refusal:
            raise SessionRefused(refusal)
        timeout_s, capped = self.effective_timeout(requested_timeout_s)
        matcher = compile_pattern(pattern) if kind == KIND_MONITOR else None
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
                started_wall=self._wall_clock())
            self._sessions[session.id] = session
        try:
            process = self._starter(command, workspace_dir, profile=profile,
                                    docker_available=docker_available,
                                    timeout_s=timeout_s)
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
        return self._started_result(session)

    def _started_result(self, session: _Session) -> dict:
        cap = int(config.SHELL_SESSION_TIMEOUT_CAP_S)
        woken = ("it finishes, times out, or a line matches your pattern"
                 if session.kind == KIND_MONITOR else
                 "it finishes or times out")
        note = (f"Runs in the background. You will be woken when {woken}; do "
                f"not poll it.")
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
        code = None
        while True:
            remaining = deadline - self._clock()
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
                session.output.append(text)
                if session.matcher is not None:
                    pending = self._match_complete_lines(session, pending + text)
        except (OSError, ValueError):
            logger.debug("session %s output stream closed", session.id)
        text = decoder.decode(b"", final=True)
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
            hold = (inbox.suspended
                    or (session.kill_reason == KILL_USER
                        and session.owner_depth == 0))
            (inbox.held if hold else inbox.events).append(event)
            self._prune_locked()
            self._changed.notify_all()
            rows = self._rows_locked()
        self._post(rows, None if hold else session.owner_thread)

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

    def _live_locked(self, owner_thread) -> int:
        owner = str(owner_thread)
        return sum(1 for s in self._sessions.values()
                   if s.owner_thread == owner and s.state in LIVE_STATES)

    def live_for(self, owner_thread) -> int:
        with self._lock:
            return self._live_locked(owner_thread)

    def blocks(self, owner_thread) -> bool:
        """Whether this thread's input is blocked (SS2): it has live sessions
        and waking has not been suspended by the consecutive-wake limit."""
        with self._lock:
            inbox = self._inboxes.get(str(owner_thread))
            return (self._live_locked(owner_thread) > 0
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
                if (self._closing or (inbox and inbox.suspended)
                        or not self._live_locked(owner_thread)):
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
        session = self._sessions.get(str(session_id))
        if session is None:
            return None
        if owner_thread is not None and session.owner_thread != str(owner_thread):
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

    def kill(self, session_id: str, *, owner_thread=None,
             reason: str = KILL_MODEL) -> dict:
        """Stop a session and report its terminal state. *owner_thread* None
        is the user's kill, which may name any session."""
        with self._lock:
            session = self._owned_locked(session_id, owner_thread)
            if session is None:
                return {"error": f"No session {session_id} in this "
                                 f"conversation."}
            if session.state not in LIVE_STATES:
                return {"session": session.id, "status": session.state,
                        "return_code": session.return_code,
                        "note": "already finished"}
            if not session.kill_reason:
                session.kill_reason = reason
            process = session.process
        if process is not None:
            process.kill()
        with self._changed:
            self._changed.wait_for(lambda: session.state not in LIVE_STATES,
                                   timeout=_KILL_SETTLE_S)
            return {"session": session.id, "status": session.state,
                    "return_code": session.return_code}

    def kill_owned(self, owner_thread, reason: str) -> list[str]:
        """Kill every live session this thread owns -- a subagent that raised
        (KILL_OWNER_FAILED) or ran out of wakes (KILL_WAKE_LIMIT)."""
        with self._lock:
            ids = [s.id for s in self._sessions.values()
                   if s.owner_thread == str(owner_thread)
                   and s.state in LIVE_STATES]
        for session_id in ids:
            self.kill(session_id, reason=reason)
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
