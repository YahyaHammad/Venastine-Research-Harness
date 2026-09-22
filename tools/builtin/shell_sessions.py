"""
tools/builtin/shell_sessions.py

ROADMAP_v3 §49, slice 1. The five tools over background and monitor
sessions (SS11): two that start one and three that act on this
conversation's own.

WHAT A START IS. A `shell` command that keeps running after its call has
been answered. It is classified, approved and routed exactly as `shell`
would be -- the approval check IS `shell._shell_approval_check`, which the
registry requires by identity (SS16), and the TOCTOU guard is shell's own
`fallback_changed_refusal`. Nothing about where a command may run is decided
in this file. What is added is lifetime: a timeout the agent picks, clamped
once by the manager (SS13), and a wake when the session reports.

WHERE THE SESSIONS LIVE. `core.shell_sessions.sessions`, the one manager;
this module is its tool face and owns no state. The owning thread is the
injected `memory`'s, never a param -- a model that could name the thread
could reach another conversation's sessions.

WHERE A START IS NOT OFFERED. Anywhere nothing will wake the run when a
session reports (SS17): a research pass, or any run outside a chat turn or a
subagent. All five tools are hidden there by `available_check`, since
advertising a tool the model cannot usefully call is the D24 defect, and a
start is also refused before any approval prompt (`refusal_check`) and again
in the handler.
"""

from __future__ import annotations

from typing import Optional

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
)

import config
from core import shell_sessions as core_sessions
from security.sandbox import SandboxUnavailable
from tools.builtin import shell

BACKGROUND = "shell_background"
MONITOR = "shell_monitor"

_KINDS = {BACKGROUND: core_sessions.KIND_BACKGROUND,
          MONITOR: core_sessions.KIND_MONITOR}


def available() -> bool:
    """Whether this run can be woken when a session reports (SS17)."""
    return core_sessions.has_consumer()


# ---------------------------------------------------------------------------
# ---- Schemas ----------------------------------------------------------------
# ---------------------------------------------------------------------------


_TIMEOUT_DESCRIPTION = (
    "The longest this session may run, in seconds. Pick what the work needs: "
    "while it runs the user's prompt is blocked. Above "
    f"{config.SHELL_SESSION_TIMEOUT_CAP_S} it is capped at "
    f"{config.SHELL_SESSION_TIMEOUT_CAP_S} and the result says so. When it is "
    "reached the command is stopped and you are woken.")


class _StartParams(BaseModel):
    command: str = Field(
        ..., min_length=1,
        description=("The shell command to start. It runs where `shell` would "
                     "run it and is approved the same way; read `ran_on` in "
                     "the result."))
    rationale: str = Field(
        default="",
        description=shell.ShellParams.model_fields["rationale"].description)
    timeout_s: StrictInt = Field(..., ge=1, description=_TIMEOUT_DESCRIPTION)
    requires_network: StrictBool = Field(
        default=False,
        description=shell.ShellParams.model_fields[
            "requires_network"].description)


class BackgroundParams(_StartParams):
    pass


class MonitorParams(_StartParams):
    pattern: str = Field(
        ..., min_length=1,
        description=("An RE2 regular expression searched for in each line of "
                     "output; not anchored, so use ^ and $ to anchor. No "
                     "backreferences or lookaround. It is matched against the "
                     "raw output, and what you are shown is redacted."))


class OutputParams(BaseModel):
    session: str = Field(..., min_length=1,
                         description="The session id, such as \"s1\".")
    offset: StrictInt = Field(
        default=0, ge=0,
        description=("The character to start from. Use `next_offset` from the "
                     "previous page."))
    limit: Optional[StrictInt] = Field(
        default=None, ge=1,
        description=(f"How many characters to return; at most and by default "
                     f"{config.MAX_READ_CHARS}."))


class KillParams(BaseModel):
    session: str = Field(..., min_length=1,
                         description="The session id, such as \"s1\".")


_SHARED = (
    f"At most {config.SHELL_SESSION_MAX_LIVE} sessions run at once, shared with "
    "any subagents, and the user's prompt is blocked while one of yours runs. "
    "Read output with shell_output and stop a session with shell_kill.")


def _schema(name: str, description: str, model, required) -> dict:
    input_schema = model.model_json_schema()
    # §42 (RA4), shell's rule: the rationale is ADVERTISED as required and
    # tolerated when absent, because approval is obtained before the params
    # are validated and a hard requirement would spend a human's yes on a
    # call that then fails.
    input_schema["required"] = list(required)
    return {"name": name, "description": description,
            "input_schema": input_schema}


BACKGROUND_TOOL_SCHEMA = _schema(
    BACKGROUND,
    "Start a shell command that keeps running after this call returns -- a "
    "test suite, a build, a server you need up while you work. The call "
    "returns at once with a session id; when the command finishes or reaches "
    "its timeout you are woken with its exit code and the end of its output, "
    "so do not poll it. " + _SHARED,
    BackgroundParams,
    ("command", "rationale", "timeout_s", "requires_network"))

MONITOR_TOOL_SCHEMA = _schema(
    MONITOR,
    "Start a shell command in the background and be woken each time a line of "
    "its output matches `pattern` -- when a log says ERROR, a server says it "
    "is listening, a test fails -- and again when it finishes. Matches that "
    "arrive while you are working are delivered together. " + _SHARED,
    MonitorParams,
    ("command", "pattern", "rationale", "timeout_s", "requires_network"))

SESSIONS_TOOL_SCHEMA = {
    "name": "shell_sessions",
    "description": (
        "List this conversation's background and monitor sessions: id, kind, "
        "command, status, elapsed time, timeout, exit code, how much output "
        "each has written and how many lines matched. Finished sessions stay "
        "listed for a while."),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

OUTPUT_TOOL_SCHEMA = _schema(
    "shell_output",
    "Read one of this conversation's background sessions' output by character "
    f"offset. Only its first {config.SHELL_SESSION_OUTPUT_HEAD_CHARS} and most "
    f"recent {config.SHELL_SESSION_OUTPUT_TAIL_CHARS} characters are kept; a "
    "page that reaches the dropped middle says where the gap is and continues "
    "past it. Output is not kept after the harness exits.",
    OutputParams, ("session",))

KILL_TOOL_SCHEMA = _schema(
    "shell_kill",
    "Stop one of this conversation's background sessions. Returns its final "
    "status, the end of its output and any matched lines you had not yet "
    "been shown. You are not woken for a session you stopped yourself.",
    KillParams, ("session",))


# ---------------------------------------------------------------------------
# ---- Before approval ----------------------------------------------------------
# ---------------------------------------------------------------------------


def _invalid(name: str, model, params) -> Optional[str]:
    """Why these params would not validate, in one line, or None."""
    if not isinstance(params, dict):
        return f"Invalid arguments for {name}: expected an object."
    try:
        model(**params)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'arguments'}: "
            f"{err['msg']}" for err in e.errors())
        return f"Invalid arguments for {name}: {problems}."
    return None


def _start_refusal(name: str, model):
    def refusal(params: dict, context=None) -> Optional[str]:
        """§32 A7: a start that will be refused is not asked about. Bad
        arguments, a bad pattern, nothing to wake, or the cap."""
        invalid = _invalid(name, model, params)
        if invalid:
            return invalid
        return core_sessions.sessions.start_refusal(
            _KINDS[name], params.get("pattern"))
    return refusal


background_refusal = _start_refusal(BACKGROUND, BackgroundParams)
monitor_refusal = _start_refusal(MONITOR, MonitorParams)


def _start_notice(name: str):
    def notice(params: dict, context=None) -> str:
        """Shell's notice -- where it runs -- plus what a session adds: how
        long it may keep running, and for a monitor what wakes the agent."""
        text = shell._shell_approval_notice(params, context)
        requested = params.get("timeout_s")
        if isinstance(requested, int) and not isinstance(requested, bool) \
                and requested >= 1:
            timeout, capped = core_sessions.SessionManager.effective_timeout(
                requested)
            text += (f" Keeps running after this call returns, for up to "
                     f"{timeout}s"
                     + (f" (asked for {requested}s, capped)" if capped else "")
                     + ".")
        pattern = params.get("pattern")
        if name == MONITOR and isinstance(pattern, str):
            text += f" Wakes the agent on every output line matching {pattern!r}."
        return text
    return notice


background_notice = _start_notice(BACKGROUND)
monitor_notice = _start_notice(MONITOR)


# ---------------------------------------------------------------------------
# ---- Handlers -------------------------------------------------------------------
# ---------------------------------------------------------------------------


def _outside(name: str) -> dict:
    # Reachable only when dispatched outside a run: todo_write's reasoning.
    return {"error": f"{name} is only available inside a conversation."}


def _start(name: str, model, params: dict, memory, call_id) -> dict:
    invalid = _invalid(name, model, params)
    if invalid:
        return {"error": invalid}
    if memory is None:
        return _outside(name)
    parsed = model(**params)
    # ONE probe, read through `shell` so the approval check and this call
    # see the same answer (§40 keeps the posture fixed; the probe is cached).
    docker_up = shell.is_docker_available()
    refusal = shell.fallback_changed_refusal(parsed.command, docker_up)
    if refusal is not None:
        return {"error": refusal}
    try:
        return core_sessions.sessions.start(
            kind=_KINDS[name], command=parsed.command,
            # §28: the SAME classification the approval check made --
            # the §50 declaration included, which `start_sandboxed`
            # already passes into the container argv as `profile.network`
            # (NW4), so the flag reaches a session with no other change.
            profile=shell.classify_command(
                parsed.command, config.WORKSPACE_DIR,
                requires_network=parsed.requires_network),
            docker_available=docker_up,
            requested_timeout_s=parsed.timeout_s,
            owner_thread=memory.thread_id,
            workspace_dir=config.WORKSPACE_DIR,
            call_id=call_id or "",
            # Display only (§42, RA2): shown in the session's view, never
            # read to decide anything.
            rationale=parsed.rationale,
            pattern=getattr(parsed, "pattern", None))
    except (core_sessions.SessionRefused, SandboxUnavailable) as e:
        return {"error": str(e)}


def background_run(params: dict, memory=None, call_id=None) -> dict:
    return _start(BACKGROUND, BackgroundParams, params, memory, call_id)


def monitor_run(params: dict, memory=None, call_id=None) -> dict:
    return _start(MONITOR, MonitorParams, params, memory, call_id)


def sessions_run(params: dict, memory=None) -> dict:
    if memory is None:
        return _outside("shell_sessions")
    listed = core_sessions.sessions.list_for(memory.thread_id)
    if not listed:
        return {"sessions": [],
                "note": "This conversation has no background sessions."}
    return {"sessions": listed}


def output_run(params: dict, memory=None) -> dict:
    invalid = _invalid("shell_output", OutputParams, params)
    if invalid:
        return {"error": invalid}
    if memory is None:
        return _outside("shell_output")
    parsed = OutputParams(**params)
    limit = min(parsed.limit or config.MAX_READ_CHARS, config.MAX_READ_CHARS)
    return core_sessions.sessions.output(parsed.session, memory.thread_id,
                                         offset=parsed.offset, limit=limit)


def kill_run(params: dict, memory=None) -> dict:
    invalid = _invalid("shell_kill", KillParams, params)
    if invalid:
        return {"error": invalid}
    if memory is None:
        return _outside("shell_kill")
    parsed = KillParams(**params)
    return core_sessions.sessions.kill(parsed.session,
                                       owner_thread=memory.thread_id,
                                       reason=core_sessions.KILL_MODEL)
