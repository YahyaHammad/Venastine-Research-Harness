"""
tools/builtin/shell_sessions.py

ROADMAP_v3 §49, slices 1 and 2. The seven tools over shell sessions
(SS11): three that start one, one that types into an interactive one,
and three that act on this conversation's own.

WHAT A START IS. A `shell` command that keeps running after its call has
been answered. It is classified, approved and routed exactly as `shell`
would be -- the approval check IS `shell._shell_approval_check`, which the
registry requires by identity (SS16), and the TOCTOU guard is shell's own
`fallback_changed_refusal`. Nothing about where a command may run is decided
in this file. What is added is lifetime: a timeout the agent picks, clamped
once by the manager (SS13), and a wake when the session reports.

EXCEPT THE INTERACTIVE ONE (slice 2). `shell_interactive` opens a shell and
keeps it open, so the yes it obtains covers every line typed into it
afterwards -- which means the question cannot be asked about its first
line. Its gate is `shell._interactive_approval_check`, held by identity in
the same place and for the same reason, and the profile that routes it is
STATED rather than read off text (SS32). `shell_input` then asks nobody,
except for a line naming a protected segment (SS29) and never for a control
key (SS30).

WHERE THE SESSIONS LIVE. `core.shell_sessions.sessions`, the one manager;
this module is its tool face and owns no state. The owning thread is the
injected `memory`'s, never a param -- a model that could name the thread
could reach another conversation's sessions.

WHERE A START IS NOT OFFERED. Anywhere nothing will wake the run when a
session reports (SS17): a research pass, or any run outside a chat turn or a
subagent. All seven tools are hidden there by `available_check`, since
advertising a tool the model cannot usefully call is the D24 defect, and a
start is also refused before any approval prompt (`refusal_check`) and again
in the handler.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
)

import config
from core import shell_sessions as core_sessions
from security import capability, posture
from security.sandbox import SandboxUnavailable
from tools.builtin import shell

BACKGROUND = "shell_background"
MONITOR = "shell_monitor"
INTERACTIVE = "shell_interactive"
INPUT = "shell_input"

_KINDS = {BACKGROUND: core_sessions.KIND_BACKGROUND,
          MONITOR: core_sessions.KIND_MONITOR,
          INTERACTIVE: core_sessions.KIND_INTERACTIVE}


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


def _backend_field():
    """The `backend` field, on the two start tools that take one.

    NOT on `_StartParams`, which `InteractiveParams` also inherits:
    `start_interactive` refuses WSL and SSH by name (WSL's stream shape
    there is unmeasured; SSH's was measured and is WORSE -- a remote pty
    merges stderr into stdout and CRLFs every line), and a parameter
    whose only legal value is the default is one the model will spend a
    call discovering. A tool advertises what it can do.
    """
    return Field(
        default="container",
        description=shell.ShellParams.model_fields["backend"].description)


class BackgroundParams(_StartParams):
    backend: Literal["container", "wsl", "ssh"] = _backend_field()


class MonitorParams(_StartParams):
    pattern: str = Field(
        ..., min_length=1,
        description=("An RE2 regular expression searched for in each line of "
                     "output; not anchored, so use ^ and $ to anchor. No "
                     "backreferences or lookaround. It is matched against the "
                     "raw output, and what you are shown is redacted."))
    backend: Literal["container", "wsl", "ssh"] = _backend_field()


_WAIT_DESCRIPTION = (
    "How long to wait for output, in seconds, before returning anyway. "
    "The call ALWAYS returns by then; the session keeps running and you "
    "can read more with another shell_input carrying no text.")


class InteractiveParams(_StartParams):
    command: str = Field(
        ..., min_length=1,
        description=(
            "The first line to type into the shell -- `python3` for a "
            "Python REPL, `psql ...` for a database client, or `pwd` if "
            "you just want a shell to work in. Approving the session "
            "approves everything you type into it afterwards, so this "
            "line is a starting point and not a limit."))
    wait_s: StrictInt = Field(default=10, ge=1, le=120,
                              description=_WAIT_DESCRIPTION)


class InputParams(BaseModel):
    session: str = Field(..., min_length=1,
                         description="The session id, such as \"s1\".")
    text: str = Field(
        default="",
        description=(
            "The line to type. A newline is added for you. Leave it out "
            "to type nothing and just read more output -- which is what "
            "to do when a previous call came back `quiet` or `bound`."))
    control: Optional[Literal["interrupt", "eof"]] = Field(
        default=None,
        description=(
            "Send a control key instead of text: \"interrupt\" is Ctrl-C, "
            "which stops whatever is running without closing the "
            "session, and \"eof\" is Ctrl-D. Use interrupt when a "
            "command will not finish or is waiting for input you cannot "
            "give."))
    wait_s: StrictInt = Field(default=10, ge=1, le=120,
                              description=_WAIT_DESCRIPTION)
    wait_for: Optional[str] = Field(
        default=None,
        description=(
            "An RE2 regular expression. Return as soon as a line of "
            "output matches it, instead of waiting for the shell to come "
            "back to its prompt. Useful when a program prints a prompt of "
            "its own and then waits for you."))


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

_INTERACTIVE_SHARED = (
    f"At most {config.SHELL_SESSION_MAX_LIVE} sessions run at once, shared "
    "with any subagents. Unlike a background command an open shell does "
    "NOT block the user, and nothing wakes you when it closes -- so close "
    "it with shell_kill when you are done. It closes itself after "
    f"{config.SHELL_SESSION_IDLE_TIMEOUT_S}s with nothing typed into it.")

INTERACTIVE_TOOL_SCHEMA = _schema(
    INTERACTIVE,
    "Open a shell inside the container and KEEP IT OPEN, so you can type "
    "into it and read the reply: a Python or node REPL, a database "
    "client, an ssh-less debugging session, anything where the next "
    "command depends on what the last one printed. What you do in it "
    "persists -- the working directory, environment variables, an "
    "interpreter's state -- across your own turns. Send input with "
    "shell_input. Programs that paint the whole screen (vim, htop, less) "
    "are NOT supported: use shell for those or run them non-interactively. "
    + _INTERACTIVE_SHARED,
    InteractiveParams,
    ("command", "rationale", "timeout_s", "requires_network"))

INPUT_TOOL_SCHEMA = _schema(
    INPUT,
    "Type a line into an interactive session and read what comes back. "
    "The call always returns: it ends as soon as the shell comes back to "
    "its prompt, or a line matches `wait_for`, or output goes quiet, or "
    "`wait_s` runs out -- and `ended` says WHICH, which you need, because "
    "only \"ready\" means the command finished. Silence does not: a "
    "command that takes a minute is silent for a minute. When you get "
    "anything else, call again with no text to keep reading. Do NOT send a "
    "new command before you get \"ready\": the terminal queues it behind "
    "the one still running, and the reply you get back may be the earlier "
    "command's.",
    InputParams, ("session",))

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
interactive_refusal = _start_refusal(INTERACTIVE, InteractiveParams)


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


def interactive_notice(params: dict, context=None) -> str:
    """Shell's interactive notice -- which says the yes covers the whole
    session (SS29) -- plus how long the shell may live.

    Not `_start_notice`: that one describes a COMMAND, and describing a
    session's first line as though it were the thing being approved is
    exactly what `_interactive_approval_check` refuses to do.
    """
    text = shell._interactive_approval_notice(params, context)
    requested = params.get("timeout_s")
    if isinstance(requested, int) and not isinstance(requested, bool) \
            and requested >= 1:
        timeout, capped = core_sessions.SessionManager.effective_timeout(
            requested)
        text += (f" Stays open for up to {timeout}s"
                 + (f" (asked for {requested}s, capped)" if capped else "")
                 + f", or {config.SHELL_SESSION_IDLE_TIMEOUT_S}s with "
                 f"nothing typed into it.")
    return text


def input_refusal(params: dict, context=None) -> Optional[str]:
    """§32 A7: a send that will be refused is not asked about."""
    return _invalid(INPUT, InputParams, params)


def input_approval_check(tool_name: str, params: dict) -> bool:
    """Whether typing this line needs a human yes (SS29, SS30).

    A CONTROL key is ungated first, above the mode, for SS11's reason:
    `shell_kill` destroys the whole session and asks nobody, so
    interrupting one command inside it cannot need more. It is also the
    only escape from a command that will not finish, and a gate on the
    escape hatch is a gate that fires exactly when it cannot be answered.

    Otherwise the open call's approval covers this line (SS29) -- except
    that a line naming a protected path segment asks anyway, which is
    `_shell_approval_check`'s step 2 applied one layer along: the same
    check, the same always-ASK polarity, and the same reason for never
    being a deny (G2 -- this does not parse the text).
    """
    if isinstance(params, dict) and params.get("control"):
        return False
    active = posture.current()
    mode = capability.validate_mode(active.shell_approval_mode,
                                    "config.SHELL_APPROVAL_MODE")
    if mode == capability.ALWAYS:
        return True
    if mode == capability.NEVER:
        return False
    text = params.get("text", "") if isinstance(params, dict) else ""
    return shell._command_touches_protected(text) is not None


def input_notice(params: dict, context=None) -> str:
    text = params.get("text", "") if isinstance(params, dict) else ""
    segment = shell._command_touches_protected(text)
    where = (f" It names {segment}, which is protected -- the session's "
             f"own approval does not cover that." if segment else "")
    return (f"Types into an interactive shell this conversation already "
            f"opened.{where}")


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
    # SS37/SS40/SS54, before the probe and before anything is classified:
    # a session that asked for WSL or SSH and cannot have it is refused by
    # name. `shell_interactive` has no `backend` field, so this is the
    # container for it and the refusal that DOES apply to it comes from
    # `start_interactive` -- which is the one that knows why.
    backend = getattr(parsed, "backend", shell.BACKEND_CONTAINER)
    refusal = shell.uncontained_refusal(parsed.command, backend)
    if refusal is not None:
        return {"error": refusal}
    # See `shell.run` for why an uncontained call does not probe: the guard
    # the probe feeds is about the insecure fallback, which it cannot take.
    docker_up = (True if backend != shell.BACKEND_CONTAINER
                 else shell.is_docker_available())
    if name == INTERACTIVE:
        # SS32: what is approved is a SHELL, not the first line, so the
        # profile that routes it says so rather than being read off text
        # whose later lines are not there yet. It is also what the gate
        # asked about -- `_interactive_approval_check` builds this same
        # profile -- so §28's rule that the two agree still holds.
        profile = shell.interactive_profile(parsed.requires_network)
        # No `fallback_changed_refusal`: its subject is a command that
        # would silently move to the insecure fallback, and SS28 gives an
        # interactive session no route to move to. `start_interactive`
        # refuses a missing runtime outright, and says what to install --
        # where this would have said "retry, you will be asked", which
        # for this kind is not true.
    else:
        profile = shell.classify_command(
            parsed.command, config.WORKSPACE_DIR,
            requires_network=parsed.requires_network)
        if backend != shell.BACKEND_WSL:
            refusal = shell.fallback_changed_refusal(
                parsed.command, docker_up)
            if refusal is not None:
                return {"error": refusal}
    try:
        return core_sessions.sessions.start(
            kind=_KINDS[name], command=parsed.command,
            # §28: the SAME profile the approval check used -- the §50
            # declaration included, which `start_sandboxed` already passes
            # into the container argv as `profile.network` (NW4), so the
            # flag reaches a session with no other change.
            profile=profile,
            docker_available=docker_up,
            backend=backend,
            requested_timeout_s=parsed.timeout_s,
            owner_thread=memory.thread_id,
            workspace_dir=config.WORKSPACE_DIR,
            call_id=call_id or "",
            # Display only (§42, RA2): shown in the session's view, never
            # read to decide anything.
            rationale=parsed.rationale,
            pattern=getattr(parsed, "pattern", None),
            open_wait_s=getattr(parsed, "wait_s", 10))
    except (core_sessions.SessionRefused, SandboxUnavailable) as e:
        return {"error": str(e)}


def background_run(params: dict, memory=None, call_id=None) -> dict:
    return _start(BACKGROUND, BackgroundParams, params, memory, call_id)


def monitor_run(params: dict, memory=None, call_id=None) -> dict:
    return _start(MONITOR, MonitorParams, params, memory, call_id)


def interactive_run(params: dict, memory=None, call_id=None) -> dict:
    return _start(INTERACTIVE, InteractiveParams, params, memory, call_id)


def input_run(params: dict, memory=None) -> dict:
    invalid = _invalid(INPUT, InputParams, params)
    if invalid:
        return {"error": invalid}
    if memory is None:
        return _outside(INPUT)
    parsed = InputParams(**params)
    return core_sessions.sessions.send(
        parsed.session, memory.thread_id, text=parsed.text,
        control=parsed.control or "", wait_s=parsed.wait_s,
        wait_for=parsed.wait_for)


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
