"""
ROADMAP_v3 §49, slice 2: interactive shell sessions (SS25-SS34).

A shell held open with a pty that the agent types into, whose state
survives across its own turns.

NO CONTAINERS HERE. The backend is conftest's `interactive_starter`, whose
fake shell answers a completed line with the prompt token exactly where a
real one does -- so what is measured is the PROTOCOL the manager speaks,
not Docker. What the real pty actually emits was measured separately
(batch 100's probes) and the bytes it produced are replayed through the
stripper below, so SS27 is pinned against observed output rather than
against a hand-written approximation of it.

Every wait is bounded, so a regression fails instead of hanging.
"""

import pytest

import config
from core import ansi
from core import shell_sessions as ss
from core.session_wake import KIND_FOR_TOOL, TOOL_FOR_KIND
from security import sandbox
from security.capability import CommandProfile
from security.sandbox import SandboxUnavailable
from tests.conftest import set_posture
from tools.builtin import shell
from tools.builtin import shell_sessions as tools
from tools.registry import registry

WAIT = 5.0

# Bytes the real pty produced, captured by batch 100's probe. Kept verbatim,
# including the CRLF, because the point of replaying them is that they were
# not invented here.
MEASURED = {
    "colour": "\x1b[1;32mgreen\x1b[0m and plain\r\n",
    "cursor": "aaa\x1b[2Dbb\r\n",
    "erase": "working\x1b[2K\rdone\r\n",
    "osc_title": "\x1b]0;a title\x07after\r\n",
    "plain": "hello\r\n",
}
EXPECTED = {
    "colour": "green and plain\n",
    "cursor": "aaabb\n",
    "erase": "working\rdone\n",
    "osc_title": "after\n",
    "plain": "hello\n",
}


def _profile(tier="SANDBOXED", network=False):
    return CommandProfile(tier=tier, measured=True, escapes_workspace=False,
                          writes=True, runs_code=True, network=network,
                          reason="test")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _until(predicate, timeout=WAIT):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(config, "SHELL_SESSION_TIMEOUT_CAP_S", 3600)
    monkeypatch.setattr(config, "SHELL_SESSION_IDLE_TIMEOUT_S", 600)
    monkeypatch.setattr(config, "SHELL_SESSION_MAX_LIVE", 4)
    monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 10)
    monkeypatch.setattr(config, "SHELL_SESSION_OUTPUT_HEAD_CHARS", 10_000)
    monkeypatch.setattr(config, "SHELL_SESSION_OUTPUT_TAIL_CHARS", 190_000)
    monkeypatch.setattr(config, "TEARDOWN_BUDGET_S", 5)
    # The real interval is a third of a second, picked from a measured 50 ms
    # round trip. Shortened here so a file of send tests costs milliseconds;
    # the ONE test that cares about its value sets it back itself.
    monkeypatch.setattr(ss, "_INPUT_QUIET_S", 0.05)


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    return str(tmp_path)


@pytest.fixture
def runtime_up(monkeypatch):
    """The approval questions below reach `containment_for`, which probes
    for a runtime. Answered here so the gate's answer is the gate's and not
    the machine's."""
    monkeypatch.setattr(shell, "is_docker_available", lambda: True)


@pytest.fixture
def manager(interactive_starter):
    """A real clock, because `send` waits on wall time."""
    import time
    return ss.SessionManager(starter=interactive_starter,
                             clock=time.monotonic, poll_s=0.01)


@pytest.fixture
def frozen(interactive_starter, monkeypatch):
    """A clock the test moves, for the lifetime rules.

    With time stopped, a settle interval can never elapse, so it is zero
    here -- these tests are about when a session DIES, and none of them
    reads `ended`.
    """
    monkeypatch.setattr(ss, "_INPUT_QUIET_S", 0.0)
    clock = Clock()
    m = ss.SessionManager(starter=interactive_starter, clock=clock,
                          poll_s=0.01)
    return m, clock


def _open(manager, owner="t1", command="python3", timeout=600, wait_s=2,
          call_id="c1"):
    with ss.consuming():
        return manager.start(kind=ss.KIND_INTERACTIVE, command=command,
                             profile=_profile(), docker_available=True,
                             requested_timeout_s=timeout, owner_thread=owner,
                             workspace_dir="/ws", call_id=call_id,
                             rationale="because", open_wait_s=wait_s)


def _background(manager, owner="t1"):
    with ss.consuming():
        return manager.start(kind=ss.KIND_BACKGROUND, command="pytest",
                             profile=_profile(), docker_available=True,
                             requested_timeout_s=600, owner_thread=owner,
                             workspace_dir="/ws", call_id="cb",
                             rationale="because")


def _process(starter, index=-1):
    return starter.started[index]["process"]


# ---------------------------------------------------------------------------
# ---- The container it opens -------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheContainerArgvIsTheMeasuredOne:
    """SS28, SS33, SS34. Every clause below was measured through this very
    builder before it was written, so a change that drops one is a change
    to something that was observed to be necessary."""

    @pytest.fixture
    def argv(self, tmp_path):
        return sandbox._docker_argv(
            "the agent's text", str(tmp_path), False, False, "docker",
            "sess", session_timeout_s=90, prompt_token="VENabc12345>")

    def test_stdin_is_kept_open(self, argv):
        assert "-i" in argv
        # NOT -t: the pty is made inside the container, so the client never
        # needs a real terminal and docker never mangles the stream.
        assert "-t" not in argv

    def test_the_pty_is_made_inside_the_container(self, argv):
        inner = argv[-1]
        assert inner.startswith("script -qfec ")
        assert inner.endswith(" /dev/null")

    def test_the_echo_is_off_and_the_pty_is_sized(self, argv):
        """SS33. The pty is 0x0 without this, and a 200-character line
        echoes back as readline's horizontal-scroll marker."""
        inner = argv[-1]
        assert "stty -echo rows 50 cols 1000" in inner
        assert "+o emacs" in inner and "+o vi" in inner

    def test_the_prompt_is_the_token_it_was_given(self, argv):
        assert "PS1='VENabc12345>'" in argv[-1]

    def test_no_user_rc_file_reaches_it(self, argv):
        assert "--norc --noprofile" in argv[-1]

    def test_the_agents_text_is_not_in_the_argv_at_all(self, argv):
        """SS32: it is TYPED into the shell once it is up. A command placed
        here would be the thing holding the pty, and the prompt would never
        come back."""
        assert not any("the agent's text" in part for part in argv)

    def test_it_is_still_a_session_argv(self, argv):
        assert "--sig-proxy=false" in argv
        assert "timeout" in argv and "90" in argv

    def test_network_follows_the_profile(self, tmp_path):
        off = sandbox._docker_argv("", str(tmp_path), False, False, "docker",
                                   "s", session_timeout_s=90,
                                   prompt_token="VENa1>")
        on = sandbox._docker_argv("", str(tmp_path), True, False, "docker",
                                  "s", session_timeout_s=90,
                                  prompt_token="VENa1>")
        assert "--network" in off and "none" in off
        assert "--network" not in on


class TestOnlyAMintedTokenIsEmbedded:

    def test_a_minted_token_is_safe_by_construction(self):
        for _ in range(20):
            token = sandbox.new_prompt_token()
            assert token.startswith("VEN") and token.endswith(">")
            assert all(c.isalnum() or c == ">" for c in token)

    @pytest.mark.parametrize("bad", ["", "a'b", 'q"q', "x; rm -rf /",
                                     "a b", "$(id)", "back\\slash"])
    def test_anything_else_is_refused(self, bad):
        """The token is embedded in a shell string. It is never a model's
        value, and a check that says so is worth more than an escape that
        would hide the day it stopped being true."""
        with pytest.raises(ValueError):
            sandbox._interactive_command(bad)


class TestTheContainerRouteOrNothing:
    """SS28. Every other route is refused with the reason rather than
    served differently."""

    def test_no_runtime_is_refused(self, tmp_path):
        with pytest.raises(SandboxUnavailable) as caught:
            sandbox.start_interactive(str(tmp_path), profile=_profile(),
                                      docker_available=False, timeout_s=60,
                                      prompt_token="VENa1>")
        assert "container runtime" in str(caught.value)

    def test_the_refusal_says_why_there_is_no_fallback(self, tmp_path):
        with pytest.raises(SandboxUnavailable) as caught:
            sandbox.start_interactive(str(tmp_path), profile=_profile(),
                                      docker_available=False, timeout_s=60,
                                      prompt_token="VENa1>")
        message = str(caught.value)
        assert "pty" in message or "prompt" in message
        assert "shell_background" in message

    def test_it_asks_for_a_stdin_PIPE(self, tmp_path, monkeypatch):
        """The one thing every other test here cannot see, because they all
        run against a fake backend that was HANDED a process.

        `_popen_session` defaults to DEVNULL -- that is right for the two
        kinds that are never typed into, and fatal for this one, which
        would come up with nothing to write to and no error saying so.
        """
        seen = {}

        def spy(args, **kwargs):
            seen.update(kwargs)
            seen["args"] = args
            raise FileNotFoundError("stop here; the argv is all we wanted")

        monkeypatch.setattr(sandbox, "_popen_session", spy)
        monkeypatch.setattr(sandbox, "known_runtime", lambda: "docker")
        monkeypatch.setattr(sandbox, "_log_image_identity", lambda _r: None)

        with pytest.raises(SandboxUnavailable):
            sandbox.start_interactive(str(tmp_path), profile=_profile(),
                                      docker_available=True, timeout_s=60,
                                      prompt_token="VENa1>")

        # Read off the module under test rather than imported here: it is
        # the same constant `_popen_session` itself would pass, and it
        # keeps a `subprocess` import out of a test that runs no command.
        assert seen["stdin"] is sandbox.subprocess.PIPE
        assert "-i" in seen["args"]

    def test_and_popen_session_actually_opens_one(self):
        """The other half, and it has to be a REAL process: the test above
        spies on `_popen_session`, so a mutation INSIDE it survives that
        assertion completely. Found by the mutation pass, which is what it
        is for.
        """
        import sys as _sys

        with_pipe = sandbox._popen_session(
            [_sys.executable, "-c", "pass"], stdin=sandbox.subprocess.PIPE)
        try:
            assert with_pipe.stdin is not None
        finally:
            with_pipe.kill()

        default = sandbox._popen_session([_sys.executable, "-c", "pass"])
        try:
            # Still DEVNULL when nothing asks, which is what the two kinds
            # that are never typed into rely on (§29, N1).
            assert default.stdin is None
        finally:
            default.kill()

    def test_a_host_read_is_refused_although_a_runtime_exists(self, tmp_path):
        """The route, not the runtime: HOST_READ never reaches a container,
        so it can never be an interactive session either."""
        with pytest.raises(SandboxUnavailable):
            sandbox.start_interactive(
                str(tmp_path), profile=_profile(tier=sandbox.HOST_READ),
                docker_available=True, timeout_s=60, prompt_token="VENa1>")


# ---------------------------------------------------------------------------
# ---- What the pty's output goes through (SS27) -------------------------------
# ---------------------------------------------------------------------------


class TestTheStripperCarriesAcrossReads:
    """A 128 KB burst arrived in 74 chunks whose boundaries fell wherever
    the kernel put them, so a sequence split across two reads is the
    ordinary case. Every split below is one of those."""

    @pytest.mark.parametrize("name", sorted(MEASURED))
    def test_measured_pty_output_whole(self, name):
        assert ansi.AnsiStripper().feed(MEASURED[name]) == EXPECTED[name]

    @pytest.mark.parametrize("name", sorted(MEASURED))
    def test_measured_pty_output_split_at_every_boundary(self, name):
        """The same bytes, cut in two at every possible point. One stripper
        instance per split, because the carry is what is under test."""
        raw, want = MEASURED[name], EXPECTED[name]
        for cut in range(len(raw) + 1):
            stripper = ansi.AnsiStripper()
            got = stripper.feed(raw[:cut]) + stripper.feed(raw[cut:])
            assert got + stripper.flush() == want, f"{name} cut at {cut}"

    def test_a_crlf_split_across_two_reads_is_one_newline(self):
        stripper = ansi.AnsiStripper()
        assert stripper.feed("done\r") == "done"
        assert stripper.feed("\nnext") == "\nnext"

    def test_a_lone_cr_is_left_where_it_was(self):
        """Making `working\\rdone` read as `done` is emulation, and SS27
        declines to emulate: what arrived is what is shown."""
        assert ansi.AnsiStripper().feed("working\rdone") == "working\rdone"

    def test_an_unterminated_sequence_is_text_after_all(self):
        stripper = ansi.AnsiStripper()
        assert stripper.feed("a\x1b[3") == "a"
        assert stripper.flush() == "\x1b[3"

    def test_a_runaway_carry_is_released_rather_than_held_forever(self):
        stripper = ansi.AnsiStripper()
        out = stripper.feed("\x1b]" + "x" * (ansi._MAX_CARRY + 10))
        assert len(out) > ansi._MAX_CARRY
        assert stripper.feed("done") == "done"

    def test_text_with_no_sequences_is_untouched(self):
        assert ansi.AnsiStripper().feed("plain text") == "plain text"


class TestThePromptIsMachineryNotOutput:
    """SS34. The token is a thing this process minted; the agent asked for
    a command's result and that is not part of it."""

    def test_the_token_never_reaches_the_reader(self):
        splitter = ansi.PromptSplitter("VENa1>")
        assert splitter.feed("hi\nVENa1>") == "hi\n"

    def test_the_count_rises_once_per_prompt(self):
        splitter = ansi.PromptSplitter("VENa1>")
        splitter.feed("VENa1>")
        assert splitter.ready_count == 1 and splitter.at_prompt
        splitter.feed("out\nVENa1>")
        assert splitter.ready_count == 2 and splitter.at_prompt

    def test_output_after_it_means_the_shell_is_busy_again(self):
        splitter = ansi.PromptSplitter("VENa1>")
        splitter.feed("VENa1>")
        splitter.feed("more")
        assert splitter.at_prompt is False
        assert splitter.ready_count == 1

    def test_a_token_split_across_two_reads_still_counts(self):
        splitter = ansi.PromptSplitter("VENa1>")
        assert splitter.feed("hi\nVEN") == "hi\n"
        assert splitter.feed("a1>") == ""
        assert splitter.ready_count == 1 and splitter.at_prompt

    def test_a_command_that_prints_it_leaves_no_false_ready(self):
        """The one way the token could lie. It raises the count, and the
        output that follows clears `at_prompt`, so nothing stands."""
        splitter = ansi.PromptSplitter("VENa1>")
        splitter.feed("VENa1> and more output\n")
        assert splitter.at_prompt is False

    def test_both_transforms_run_in_the_one_order_they_can(self):
        """Escapes first: a sequence between the prompt and the end of the
        stream would otherwise hide it."""
        stream = ansi.PtyStream("VENa1>")
        assert stream.feed("hi\r\n\x1b[0mVENa1>") == "hi\n"
        assert stream.ready_count == 1 and stream.at_prompt


# ---------------------------------------------------------------------------
# ---- Opening ----------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestOpeningTypesTheFirstLine:

    def test_the_command_is_typed_in_with_a_newline(self, manager,
                                                    interactive_starter):
        _open(manager, command="python3")
        assert _process(interactive_starter).stdin.sent == "python3\n"

    def test_nothing_is_typed_before_the_shell_prompts(
            self, manager, interactive_starter, monkeypatch):
        """The terminal driver echoes what reaches it before `stty -echo`
        has run, and that window is the container's whole startup -- so
        input written the instant Popen returns would be echoed EVERY
        time, not rarely."""
        interactive_starter.prompt_on_start = False
        monkeypatch.setattr(ss, "_OPEN_BOUND_S", 0.3)

        with pytest.raises(ss.SessionRefused):
            _open(manager, command="python3")

        assert _process(interactive_starter).stdin.sent == ""

    def test_a_shell_that_never_prompts_is_refused_with_the_reason(
            self, manager, interactive_starter, monkeypatch):
        interactive_starter.prompt_on_start = False
        monkeypatch.setattr(ss, "_OPEN_BOUND_S", 0.3)

        with pytest.raises(ss.SessionRefused) as caught:
            _open(manager)

        assert "prompt" in str(caught.value)
        assert "script" in str(caught.value)

    def test_the_session_it_killed_is_not_left_running(
            self, manager, interactive_starter, monkeypatch):
        interactive_starter.prompt_on_start = False
        monkeypatch.setattr(ss, "_OPEN_BOUND_S", 0.3)

        with pytest.raises(ss.SessionRefused):
            _open(manager)

        assert _until(lambda: _process(interactive_starter).kills >= 1)

    def test_the_result_says_what_an_interactive_session_is(self, manager):
        result = _open(manager)
        assert result["kind"] == ss.KIND_INTERACTIVE
        note = result["note"]
        assert "shell_input" in note
        assert "does NOT block the user" in note
        assert "vim" in note          # SS27's limit, stated not discovered

    def test_the_opening_output_comes_back_with_the_open(self, manager,
                                                         interactive_starter):
        """A REPL's banner is the first thing worth seeing, and a separate
        call to fetch it would be a poll."""
        process = None

        result = _open(manager, command="python3")
        process = _process(interactive_starter)
        assert process is not None
        assert result["ended"] in {"ready", "quiet", "bound"}
        assert "ended_meaning" in result

    def test_the_token_is_minted_per_session(self, manager,
                                             interactive_starter):
        _open(manager, call_id="c1")
        _open(manager, call_id="c2")
        first, second = (s["prompt_token"] for s in
                         interactive_starter.started[-2:])
        assert first and second and first != second


# ---------------------------------------------------------------------------
# ---- Sending (SS26) ----------------------------------------------------------
# ---------------------------------------------------------------------------


class TestEverySendIsBounded:
    """Four bounds, each of which has to be reachable -- a send that could
    not return is the wedge the gap register named."""

    def test_ready_when_the_prompt_comes_back(self, manager):
        opened = _open(manager)
        result = manager.send(opened["session"], "t1", text="echo hi",
                              wait_s=WAIT)
        assert result["ended"] == "ready"

    def test_bound_when_the_prompt_does_not(self, manager,
                                            interactive_starter):
        interactive_starter.auto_prompt = False
        opened = _open(manager, wait_s=1)
        result = manager.send(opened["session"], "t1", text="sleep 60",
                              wait_s=1)
        assert result["ended"] == "bound"

    def test_quiet_when_output_stops_without_a_prompt(
            self, manager, interactive_starter, monkeypatch):
        interactive_starter.auto_prompt = False
        opened = _open(manager, wait_s=1)
        process = _process(interactive_starter)

        def answer(_data, p=process):
            p.write(">>> ")
        process.stdin.on_write = answer

        result = manager.send(opened["session"], "t1", text="python3",
                              wait_s=WAIT)
        assert result["ended"] == "quiet"
        assert ">>> " in result["text"]

    def test_quiet_needs_output_to_have_arrived(self, manager,
                                                interactive_starter):
        """Otherwise a command that prints nothing while it works -- which
        is most of them, now that the echo is off -- would report `quiet`
        and read as finished."""
        interactive_starter.auto_prompt = False
        opened = _open(manager, wait_s=1)
        result = manager.send(opened["session"], "t1", text="sleep 60",
                              wait_s=0.4)
        assert result["ended"] == "bound"

    def test_matched_on_a_pattern(self, manager, interactive_starter):
        interactive_starter.auto_prompt = False
        opened = _open(manager, wait_s=1)
        process = _process(interactive_starter)

        def answer(_data, p=process):
            p.write("line1\nline2\n")
        process.stdin.on_write = answer

        result = manager.send(opened["session"], "t1", text="run",
                              wait_s=WAIT, wait_for="line2")
        assert result["ended"] == "matched"

    def test_quiet_never_pre_empts_a_pattern(self, manager,
                                             interactive_starter,
                                             monkeypatch):
        """Measured: `wait_for="line2"` against a loop printing a line a
        second returned `quiet` after line1 -- answering a question nobody
        asked, and leaving the loop running to desynchronize every later
        call."""
        import threading
        interactive_starter.auto_prompt = False
        opened = _open(manager, wait_s=1)
        process = _process(interactive_starter)

        def answer(_data, p=process):
            p.write("line1\n")
            threading.Timer(0.4, lambda: p.write("line2\n")).start()
        process.stdin.on_write = answer

        result = manager.send(opened["session"], "t1", text="run",
                              wait_s=WAIT, wait_for="line2")
        assert result["ended"] == "matched"

    def test_a_pattern_matches_a_line_that_arrived_with_ansi_in_it(
            self, manager, interactive_starter):
        """Stripping precedes every match (SS27), so a pattern is written
        against what the program said and not against how it coloured it."""
        interactive_starter.auto_prompt = False
        opened = _open(manager, wait_s=1)
        process = _process(interactive_starter)

        def answer(_data, p=process):
            p.write("\x1b[1;31mFAILED\x1b[0m here\r\n")
        process.stdin.on_write = answer

        result = manager.send(opened["session"], "t1", text="run",
                              wait_s=WAIT, wait_for="^FAILED here$")
        assert result["ended"] == "matched"

    def test_ended_when_the_session_itself_stops(self, manager,
                                                 interactive_starter):
        interactive_starter.auto_prompt = False
        opened = _open(manager, wait_s=1)
        process = _process(interactive_starter)

        def answer(_data, p=process):
            p.exit(0)
        process.stdin.on_write = answer

        result = manager.send(opened["session"], "t1", text="exit",
                              wait_s=WAIT)
        assert result["ended"] == "ended"

    def test_ready_waits_for_the_prompt_to_SETTLE(self, manager,
                                                  interactive_starter,
                                                  monkeypatch):
        """A prompt merely PASSED THROUGH on the way to a queued line is not
        the answer to the line just sent.

        Measured: a line typed while another command was still running was
        reported `ready` the instant the EARLIER command's prompt arrived,
        handing back that command's tail as this one's answer. The settle
        is what separates them -- a passed-through prompt is followed by
        output within milliseconds and so never settles.

        This is the one test that needs the interval at something like its
        real size, since what it asserts is that the wait outlasts a gap.
        """
        import threading
        monkeypatch.setattr(ss, "_INPUT_QUIET_S", 0.5)
        interactive_starter.auto_prompt = False
        opened = _open(manager, wait_s=1)
        process = _process(interactive_starter)

        def answer(_data, p=process):
            p.prompt()                       # the earlier command's prompt
            threading.Timer(
                0.1, lambda: (p.write("late\n"), p.prompt())).start()
        process.stdin.on_write = answer

        result = manager.send(opened["session"], "t1", text="echo late",
                              wait_s=WAIT)

        assert result["ended"] == "ready"
        assert "late" in result["text"]

    def test_every_answer_carries_what_it_means(self, manager):
        """The agent has to act on the difference, and only `ready` says a
        command finished."""
        opened = _open(manager)
        result = manager.send(opened["session"], "t1", text="echo hi",
                              wait_s=WAIT)
        assert result["ended_meaning"] == ss._ENDED_MEANING[result["ended"]]
        assert set(ss._ENDED_MEANING) == {"ready", "matched", "quiet",
                                          "bound", "ended"}

    def test_an_invalid_pattern_is_refused_before_anything_is_typed(
            self, manager, interactive_starter):
        opened = _open(manager)
        before = _process(interactive_starter).stdin.sent

        result = manager.send(opened["session"], "t1", text="x",
                              wait_s=WAIT, wait_for="(unclosed")

        assert "Invalid pattern" in result["error"]
        assert _process(interactive_starter).stdin.sent == before


class TestWhatReachesThePty:

    def test_a_newline_is_added(self, manager, interactive_starter):
        """A line the shell never sees because its terminator was forgotten
        looks exactly like a command that produced no output."""
        opened = _open(manager, command="pwd")
        process = _process(interactive_starter)
        process.stdin.chunks.clear()

        manager.send(opened["session"], "t1", text="echo hi", wait_s=WAIT)

        assert process.stdin.sent == "echo hi\n"

    def test_an_existing_newline_is_not_doubled(self, manager,
                                                interactive_starter):
        opened = _open(manager, command="pwd")
        process = _process(interactive_starter)
        process.stdin.chunks.clear()

        manager.send(opened["session"], "t1", text="echo hi\n", wait_s=WAIT)

        assert process.stdin.sent == "echo hi\n"

    def test_a_control_sends_its_character_and_no_newline(
            self, manager, interactive_starter):
        opened = _open(manager, command="pwd")
        process = _process(interactive_starter)
        process.stdin.chunks.clear()

        manager.send(opened["session"], "t1", control="interrupt", wait_s=1)

        assert process.stdin.sent == "\x03"

    def test_eof_is_the_other_control(self, manager, interactive_starter):
        opened = _open(manager, command="pwd")
        process = _process(interactive_starter)
        process.stdin.chunks.clear()

        manager.send(opened["session"], "t1", control="eof", wait_s=1)

        assert process.stdin.sent == "\x04"

    def test_an_unknown_control_is_refused(self, manager,
                                           interactive_starter):
        opened = _open(manager)
        process = _process(interactive_starter)
        process.stdin.chunks.clear()

        result = manager.send(opened["session"], "t1", control="explode",
                              wait_s=1)

        assert "Unknown control" in result["error"]
        assert process.stdin.sent == ""

    def test_waiting_with_no_text_writes_nothing(self, manager,
                                                 interactive_starter):
        opened = _open(manager, command="pwd")
        process = _process(interactive_starter)
        process.stdin.chunks.clear()

        manager.send(opened["session"], "t1", wait_s=0.2)

        assert process.stdin.sent == ""

    def test_a_dead_pipe_is_reported_rather_than_raised(
            self, manager, interactive_starter):
        opened = _open(manager)
        _process(interactive_starter).stdin.broken = True

        result = manager.send(opened["session"], "t1", text="x", wait_s=1)

        assert "Could not reach" in result["error"]


class TestALineTooLongIsRefusedNotTruncated:
    """Measured: a pty in canonical mode takes 4095 bytes on one line and
    silently drops the rest, and the shell then runs a TRUNCATED command
    and reports success. A command a human approved must not be edited by
    a terminal driver on its way in."""

    def test_over_the_limit_is_refused(self, manager):
        opened = _open(manager)
        result = manager.send(opened["session"], "t1",
                              text="echo " + "q" * 5000, wait_s=1)
        assert "refused" in result["error"]
        assert str(ss._PTY_LINE_LIMIT) in result["error"]

    def test_the_refusal_says_what_to_do_instead(self, manager):
        opened = _open(manager)
        result = manager.send(opened["session"], "t1", text="q" * 5000,
                              wait_s=1)
        assert "file" in result["error"]

    def test_nothing_is_typed(self, manager, interactive_starter):
        opened = _open(manager)
        process = _process(interactive_starter)
        process.stdin.chunks.clear()

        manager.send(opened["session"], "t1", text="q" * 5000, wait_s=1)

        assert process.stdin.sent == ""

    def test_the_limit_is_counted_in_BYTES(self, manager):
        """A 4000-character line of non-ASCII is not a 4000-byte line, and
        the driver counts bytes."""
        opened = _open(manager)
        result = manager.send(opened["session"], "t1",
                              text="é" * (ss.MAX_INPUT_BYTES // 2),
                              wait_s=1)
        assert "error" in result

    def test_just_under_the_limit_is_allowed(self, manager):
        opened = _open(manager)
        result = manager.send(opened["session"], "t1",
                              text="x" * (ss.MAX_INPUT_BYTES - 1), wait_s=WAIT)
        assert "error" not in result


class TestOnlyAnInteractiveSessionTakesInput:

    def test_a_background_session_is_refused(self, manager):
        started = _background(manager)
        result = manager.send(started["session"], "t1", text="x", wait_s=1)
        assert "cannot be typed into" in result["error"]

    def test_an_unknown_session_is_refused(self, manager):
        assert "No session" in manager.send("s99", "t1", text="x",
                                            wait_s=1)["error"]

    def test_another_thread_cannot_reach_it(self, manager):
        opened = _open(manager, owner="t1")
        result = manager.send(opened["session"], "t2", text="x", wait_s=1)
        assert "No session" in result["error"]

    def test_a_finished_session_is_refused(self, manager,
                                           interactive_starter):
        opened = _open(manager)
        _process(interactive_starter).exit(0)
        assert _until(lambda: manager.row(opened["session"]).state
                      not in ss.LIVE_STATES)

        result = manager.send(opened["session"], "t1", text="x", wait_s=1)

        assert "already finished" in result["error"]


# ---------------------------------------------------------------------------
# ---- Lifetime (SS25, SS31) ---------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheTwoCountsAreNotTheSameQuestion:
    """SS25. One list, two questions: what is RUNNING, which is every kind,
    and what the user's prompt is waiting on, which is not."""

    def test_it_counts_toward_the_cap(self, manager):
        _open(manager)
        assert manager.live_for("t1") == 1

    def test_it_does_not_block_the_users_prompt(self, manager):
        _open(manager)
        assert manager.blocks("t1") is False

    def test_a_background_session_still_blocks(self, manager):
        _background(manager)
        assert manager.blocks("t1") is True

    def test_and_still_blocks_with_an_interactive_one_beside_it(self,
                                                                manager):
        _background(manager)
        _open(manager)
        assert manager.blocks("t1") is True

    def test_the_cap_refuses_a_fifth_session_of_any_kind(self, manager,
                                                         monkeypatch):
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_LIVE", 2)
        _open(manager, call_id="c1")
        _open(manager, call_id="c2")

        with pytest.raises(ss.SessionRefused):
            _open(manager, call_id="c3")

    def test_wait_for_wake_does_not_wait_for_one(self, manager):
        """Load bearing: this loop keeps a turn alive while something is
        still running, and an idle shell would hold the turn open exactly
        as a hung read would.

        The claim is about TIME, not about the answer. With an interactive
        session counted, this returns None too -- just `timeout` seconds
        later, having held the turn open the whole while. Asserting only on
        the return value passes either way, which is how the mutation that
        removed the kinds survived the first pass.
        """
        import time
        _open(manager)

        started = time.monotonic()
        assert manager.wait_for_wake("t1", timeout=3.0) is None

        assert time.monotonic() - started < 1.0

    def test_the_kinds_counted_are_named_once(self):
        assert ss.BLOCKING_KINDS == {ss.KIND_BACKGROUND, ss.KIND_MONITOR}
        assert ss.KIND_INTERACTIVE not in ss.BLOCKING_KINDS


class TestItsEndIsHeldNotWokenFor:
    """SS31. With no prompt block, a wake would be a turn starting
    underneath the user's cursor -- and there is nothing in it the agent is
    waiting on, since every send was already answered when it was made."""

    def test_an_exit_is_held(self, manager, interactive_starter):
        opened = _open(manager)
        _process(interactive_starter).exit(0)

        assert _until(lambda: manager.take_held("t1") or
                      manager.row(opened["session"]).state == ss.EXITED)
        assert _until(lambda: manager.row(opened["session"]).state
                      not in ss.LIVE_STATES)

    def test_nothing_is_queued_to_wake_the_agent(self, manager,
                                                 interactive_starter):
        opened = _open(manager)
        _process(interactive_starter).exit(0)
        assert _until(lambda: manager.row(opened["session"]).state
                      not in ss.LIVE_STATES)

        assert manager.take_wake("t1") is None
        assert [e.session_id for e in manager.take_held("t1")] == [
            opened["session"]]

    def test_a_background_session_still_wakes(self, manager,
                                              interactive_starter):
        """The contrast is the point: held is interactive's rule, not a
        change to how sessions report."""
        started = _background(manager)
        _process(interactive_starter).exit(0)

        assert _until(lambda: manager.take_wake("t1") is not None
                      or manager.row(started["session"]).state
                      not in ss.LIVE_STATES)


class TestItDiesOfBeingIgnored:
    """SS25. A shell nobody is typing into holds a container for nothing,
    and because it does not block the user's prompt there is no other
    pressure to close it."""

    def test_the_idle_timeout_closes_it(self, frozen, monkeypatch):
        manager, clock = frozen
        monkeypatch.setattr(config, "SHELL_SESSION_IDLE_TIMEOUT_S", 60)
        opened = _open(manager, wait_s=1)

        clock.now += 61

        assert _until(lambda: manager.row(opened["session"]).state
                      not in ss.LIVE_STATES)
        assert manager.row(opened["session"]).state == ss.KILLED

    def test_it_says_why_it_closed(self, frozen, monkeypatch):
        manager, clock = frozen
        monkeypatch.setattr(config, "SHELL_SESSION_IDLE_TIMEOUT_S", 60)
        opened = _open(manager, wait_s=1)

        clock.now += 61
        assert _until(lambda: manager.row(opened["session"]).state
                      not in ss.LIVE_STATES)

        held = manager.take_held("t1")
        assert [e.kill_reason for e in held] == [ss.KILL_IDLE]

    def test_typing_resets_the_idle_clock(self, frozen, monkeypatch):
        """Measured from the INPUT, not the output: a program printing to
        itself forever is still one nobody is using."""
        manager, clock = frozen
        monkeypatch.setattr(config, "SHELL_SESSION_IDLE_TIMEOUT_S", 60)
        opened = _open(manager, wait_s=1)
        session = manager._sessions[opened["session"]]
        clock.now += 30

        manager.send(opened["session"], "t1", text="still here", wait_s=1)

        assert session.last_input_mono == clock.now

    def test_a_background_session_has_no_idle_timeout(self, frozen,
                                                      monkeypatch):
        """It is waiting on work, which is the opposite of idle."""
        manager, clock = frozen
        monkeypatch.setattr(config, "SHELL_SESSION_IDLE_TIMEOUT_S", 60)
        started = _background(manager)

        clock.now += 300

        assert manager.row(started["session"]).state in ss.LIVE_STATES

    def test_the_wall_clock_cap_still_applies(self, frozen, monkeypatch):
        manager, clock = frozen
        monkeypatch.setattr(config, "SHELL_SESSION_IDLE_TIMEOUT_S", 100_000)
        opened = _open(manager, timeout=60, wait_s=1)

        clock.now += 61

        assert _until(lambda: manager.row(opened["session"]).state
                      not in ss.LIVE_STATES)
        assert manager.row(opened["session"]).state == ss.TIMED_OUT


# ---------------------------------------------------------------------------
# ---- Approval (SS29, SS30, SS32) ---------------------------------------------
# ---------------------------------------------------------------------------


class TestTypingIsCoveredByTheOpen:

    def test_an_ordinary_line_asks_nobody(self, monkeypatch, workspace,
                                          runtime_up):
        set_posture(monkeypatch, shell_approval_mode="tiered")
        assert registry.approval_needed(
            "shell_input", {"session": "s1", "text": "echo hi"}) is False

    def test_a_protected_segment_asks_anyway(self, monkeypatch, workspace,
                                             runtime_up):
        """`_shell_approval_check`'s step 2 applied one layer along: the
        same check, the same always-ASK polarity, and never a deny."""
        set_posture(monkeypatch, shell_approval_mode="tiered")
        assert registry.approval_needed(
            "shell_input",
            {"session": "s1", "text": "cat .venastine/settings.json"}) is True

    def test_a_control_asks_nobody_even_under_always(self, monkeypatch,
                                                    workspace, runtime_up):
        """SS11 ships `shell_kill` ungated -- destroying the whole session
        needs no human -- so interrupting one command inside it cannot need
        one, and a gate on the escape hatch fires exactly when it cannot be
        answered."""
        set_posture(monkeypatch, shell_approval_mode="always")
        assert tools.input_approval_check(
            "shell_input", {"session": "s1", "control": "interrupt"}) is False

    def test_always_still_asks_about_a_typed_line(self, monkeypatch,
                                                  workspace, runtime_up):
        set_posture(monkeypatch, shell_approval_mode="always")
        assert tools.input_approval_check(
            "shell_input", {"session": "s1", "text": "echo hi"}) is True

    def test_never_asks_about_nothing(self, monkeypatch, workspace,
                                      runtime_up):
        set_posture(monkeypatch, shell_approval_mode="never")
        assert tools.input_approval_check(
            "shell_input",
            {"session": "s1", "text": "cat .venastine/settings.json"}) is False

    def test_the_notice_names_the_segment_that_made_it_ask(self):
        notice = tools.input_notice({"session": "s1",
                                     "text": "cat .venastine/settings.json"})
        assert ".venastine" in notice

    def test_the_notice_says_the_session_was_already_approved(self):
        notice = tools.input_notice({"session": "s1", "text": "echo hi"})
        assert "already" in notice


class TestOpeningIsApprovedAsASession:
    """SS29, SS32. What is granted is a shell, not its first line."""

    def test_the_profile_is_stated_not_derived(self):
        profile = shell.interactive_profile(False)
        assert profile.runs_code and profile.writes
        assert profile.measured and not profile.escapes_workspace
        assert profile.tier == sandbox.SANDBOXED

    def test_a_declared_network_lifts_the_tier(self):
        assert shell.interactive_profile(True).tier == sandbox.SANDBOXED_NET
        assert shell.interactive_profile(True).network is True

    def test_the_prompt_says_the_yes_covers_the_session(self, workspace,
                                                        runtime_up):
        notice = tools.interactive_notice(
            {"command": "pwd", "rationale": "r", "timeout_s": 60})
        assert "APPROVES EVERYTHING" in notice
        assert "pwd" in notice

    def test_the_prompt_says_how_long_it_may_stay_open(self, workspace,
                                                       runtime_up):
        notice = tools.interactive_notice(
            {"command": "pwd", "rationale": "r", "timeout_s": 60})
        assert "60s" in notice
        assert str(config.SHELL_SESSION_IDLE_TIMEOUT_S) in notice

    def test_a_declared_network_is_on_the_prompt(self, workspace,
                                                 runtime_up):
        notice = tools.interactive_notice(
            {"command": "pwd", "rationale": "r", "timeout_s": 60,
             "requires_network": True})
        assert "network" in notice

    @pytest.mark.parametrize("mode", ["tiered", "contained"])
    def test_it_asks_whatever_the_first_line_is(self, mode, monkeypatch,
                                                workspace, runtime_up):
        set_posture(monkeypatch, shell_approval_mode=mode)
        for command in ("pwd", "ls", "python x.py", "curl http://x"):
            assert shell._interactive_approval_check(
                "shell_interactive", {"command": command}) is True

    def test_never_is_still_an_opt_out_of_every_prompt(self, monkeypatch,
                                                       workspace,
                                                       runtime_up):
        set_posture(monkeypatch, shell_approval_mode="never")
        assert shell._interactive_approval_check(
            "shell_interactive", {"command": "rm -rf /"}) is False


class TestTheDisplaySurfacesKnowTheKind:

    def test_the_kind_maps_to_the_tool_that_started_it(self):
        assert TOOL_FOR_KIND[ss.KIND_INTERACTIVE] == "shell_interactive"

    def test_and_back_again_from_one_literal(self):
        """Two literals would be two places for a new kind to be
        half-added."""
        assert KIND_FOR_TOOL["shell_interactive"] == ss.KIND_INTERACTIVE
        assert KIND_FOR_TOOL == {v: k for k, v in TOOL_FOR_KIND.items()}

    def test_only_the_open_arms_a_clickable_line(self):
        """Arming every input line would put one session on twenty lines,
        which is why `shell_output` and `shell_kill` are not armed either."""
        assert registry.opens("shell_interactive") == ("session",)
        assert registry.opens("shell_input") == ()
