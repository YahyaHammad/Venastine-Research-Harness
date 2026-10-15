"""
test_parallel_calls.py

ROADMAP_v2 §47 slice 8 -- several calls of ONE model response running at
the same time, and the seven things that were correct only because nothing
ran alongside anything else.

WHY A FILE OF ITS OWN. The slice's claims are not about one module: the
partition is in `core/loop.py`, the serialised question is in
`core/interaction.py`, the budget's lock is in `core/approval.py`, and the
panel's ordering is in `tui/widgets.py`. What they have in common is the
concurrency, so that is what this file is organised by. The two claims that
cannot live here live where their machinery does -- real SQLite in
`test_storage_e2e.py`, because there can be only one sqlmodel swap, and the
authorization scope in `test_grants.py`, next to the batch-60 tests it
extends.

EVERY CONCURRENCY CLAIM IS DRIVEN WITH REAL THREADS AND A BARRIER, never by
reasoning about interleavings. A test that asserts a lock is needed by
arranging the race in its own head proves nothing about the race the
machine runs; a barrier makes the overlap a precondition of the test
passing, so removing the guard fails rather than merely becoming
theoretically wrong. Where a test needs to observe that two things
overlapped, it says so with a barrier whose timeout IS the assertion.
"""

import threading

import pytest

import config
import core.loop
from core import interaction
from core.approval import GrantBudget
from core.client import StreamToken
from core.interaction import ResponseChannel
from core.loop import RunAgentLoop, _parallel_groups
from tests.conftest import FakeMemory as _Mem
from tests.conftest import make_model_response
from tools.registry import registry

# A barrier that never blocks longer than this. Long enough that a loaded
# machine does not fail spuriously, short enough that a genuinely
# sequential implementation fails in seconds rather than at pytest's own
# timeout -- which would read as a hang rather than as an assertion.
OVERLAP_TIMEOUT_S = 10


class _Call:
    """The three attributes the loop reads off a tool call."""

    def __init__(self, id, name, input=None):
        self.id = id
        self.name = name
        self.input = input or {}
        self.parse_error = None


def _calls(*pairs):
    return [_Call("c%d" % i, name, params)
            for i, (name, params) in enumerate(pairs)]


def _drive(mocker, calls, *, answers=(), channel=True, max_steps=2,
           granted=(), ask_takes=0.0):
    """One turn through the real loop and the real dispatch.

    `calls` is [(name, params), ...]. Returns the events, so a test can ask
    about ordering as well as about outcomes -- the ordering claims (NA11,
    NA15) are about the event STREAM, not about the results alone.

    `ask_takes` holds the fake shell open for that many seconds. Not a
    sleep-until-it-passes: an ask is SLOW by nature -- it is a human looking
    at a modal -- and a test whose ask returns in nanoseconds compresses the
    window a guard exists to protect into less than one GIL quantum. See
    `test_the_same_agent_twice_in_one_response_is_one_question`.
    """
    uses = make_model_response(text="", tool_calls=[
        {"id": "c%d" % i, "name": name, "input": params}
        for i, (name, params) in enumerate(calls)])
    seq = [uses, make_model_response(text="done")]

    def _stream(*a, **kw):
        yield StreamToken(final_response=(
            seq.pop(0) if seq else make_model_response(text="x")))

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("core.loop.effort_for", return_value=None)
    mocker.patch("core.loop.call_model_stream", side_effect=_stream)

    asked, pending = [], list(answers)
    lock = threading.Lock()

    def _ask(request):
        with lock:
            asked.append(request)
            answer = pending.pop(0) if pending else None
        if ask_takes:
            threading.Event().wait(ask_takes)
        return answer

    ch = (ResponseChannel(ask=_ask) if channel else None)
    events = list(RunAgentLoop._run(
        memory=_Mem(), system_prompt="s", provider_name="ANTHROPIC",
        model="m", context=None, max_steps=max_steps,
        response_channel=ch, granted_tools=set(granted)))
    return events, asked


def _results(events):
    return [e.tool_result["result"] for e in events
            if e.tool_result is not None]


def _result_ids(events):
    return [e.tool_result["id"] for e in events if e.tool_result is not None]


def _make_parallel(mocker, *names):
    """Declare `names` parallel for the duration of one test.

    Patched rather than registering a throwaway ToolSpec, because what is
    under test is what the LOOP does with the answer. The declaration
    itself is asserted separately, against the real registry, in
    TestTheDeclaration -- so neither claim rests on the other.
    """
    wanted = set(names)
    real = registry.parallel
    mocker.patch.object(
        registry, "parallel",
        side_effect=lambda name: name in wanted or real(name))


# ---------------------------------------------------------------------------
# ---- The declaration (NA9) ------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheDeclaration:
    """`parallel` is declared on the ToolSpec and read by the loop, which is
    `opens_thread`'s shape one question over (R13)."""

    def test_spawn_subagent_declares_itself_parallel(self):
        assert registry.parallel("spawn_subagent") is True

    @pytest.mark.parametrize(
        "name", ["shell", "web_search", "get_time", "write_project_doc"])
    def test_every_other_tool_stays_sequential(self, name):
        """False is the SAFE answer here, unlike grant_policy where absence
        is fatal at import. A tool nobody has examined for thread safety
        must not become concurrent by having forgotten to say so."""
        assert registry.parallel(name) is False

    def test_an_unknown_tool_is_not_parallel(self):
        """Which is what an mcp__* tool registered at runtime is until it
        says otherwise -- and a stranger's tool must get the sequential
        answer."""
        assert registry.parallel("mcp__probe__whatever") is False

    def test_the_loop_asks_the_registry_rather_than_naming_the_tool(self):
        """The mechanism claim, structurally. A tool name in core/loop.py is
        how two readers come to disagree about which calls are special --
        R13's argument, and the reason grant_scope, request_kind and
        opens_thread are all declared rather than matched.

        THE DOCSTRING IS EXCLUDED, and the first version of this test was
        red because of it: `_parallel_groups` explains itself by naming the
        tool it deliberately does not test for. Prose about a rule is not a
        breach of it, so this reads the CODE -- which is also what a
        mutation would have to change to break the rule.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(core.loop._parallel_groups))
        body = tree.body[0].body
        if (isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        code = "\n".join(ast.unparse(node) for node in body)

        assert "registry.parallel" in code, (
            "the partition stopped asking the registry")
        assert "spawn_subagent" not in code, (
            "core/loop.py names the tool instead of asking about it; two "
            "readers with their own copy of the answer is how they come to "
            "disagree")


# ---------------------------------------------------------------------------
# ---- The partition (NA9) --------------------------------------------------
# ---------------------------------------------------------------------------

class TestThePartition:
    """A response becomes groups before anything runs. Pure function, so
    these are cheap and exhaustive rather than sampled."""

    def test_a_run_of_parallel_calls_becomes_one_group(self):
        groups = _parallel_groups(_calls(
            ("spawn_subagent", {}), ("spawn_subagent", {}),
            ("spawn_subagent", {})))
        assert [len(g) for g in groups] == [3]

    def test_a_sequential_call_is_a_group_of_one(self):
        groups = _parallel_groups(_calls(("get_time", {})))
        assert [len(g) for g in groups] == [1]

    def test_a_lone_parallel_call_is_also_a_group_of_one(self):
        """Which is what routes it down the unchanged path. A tool declaring
        `parallel` does not make a single call concurrent -- there is
        nothing for it to be concurrent WITH."""
        groups = _parallel_groups(_calls(("spawn_subagent", {})))
        assert [len(g) for g in groups] == [1]

    def test_a_sequential_call_keeps_its_position_between_two_parallel_ones(
            self):
        """CONSECUTIVE runs only, and this is the decision rather than an
        implementation detail. Grouping the two spawns either side of a
        `shell` would move the shell relative to them, and NA9 keeps every
        non-parallel call exactly where the model put it."""
        groups = _parallel_groups(_calls(
            ("spawn_subagent", {}), ("shell", {}), ("spawn_subagent", {})))
        assert [len(g) for g in groups] == [1, 1, 1]
        assert [g[0].name for g in groups] == [
            "spawn_subagent", "shell", "spawn_subagent"]

    def test_order_is_preserved_across_groups(self):
        groups = _parallel_groups(_calls(
            ("get_time", {}), ("spawn_subagent", {}), ("spawn_subagent", {}),
            ("get_time", {})))
        assert [len(g) for g in groups] == [1, 2, 1]
        flat = [call.id for g in groups for call in g]
        assert flat == ["c0", "c1", "c2", "c3"]

    def test_no_calls_is_no_groups(self):
        assert _parallel_groups([]) == []


# ---------------------------------------------------------------------------
# ---- They actually run at once (NA9, NA10) --------------------------------
# ---------------------------------------------------------------------------

class TestTheyRunAtOnce:

    def test_three_parallel_calls_overlap(self, mocker):
        """THE POINT OF THE SLICE, and the barrier is the assertion.

        Each handler waits for all three to arrive. Sequentially the first
        never gets its partners, so `barrier.wait` raises BrokenBarrier at
        the timeout and the results carry errors -- which is what makes this
        test fail on a partition that groups nothing, rather than pass
        slowly.
        """
        _make_parallel(mocker, "get_time")
        barrier = threading.Barrier(3, timeout=OVERLAP_TIMEOUT_S)
        seen = []

        def handler(params, **kw):
            barrier.wait()
            seen.append(threading.current_thread().name)
            return {"ok": True}

        mocker.patch.object(registry._tools["get_time"], "handler", handler)

        events, _ = _drive(mocker, [("get_time", {}), ("get_time", {}),
                                    ("get_time", {})])

        assert len(seen) == 3, (
            "three calls did not reach the barrier together; the partition "
            "ran them one after another")
        assert len(set(seen)) == 3, (
            f"expected three distinct threads, saw {seen}")
        assert all("error" not in r for r in _results(events)), _results(events)

    def test_a_lone_call_runs_on_the_calling_thread(self, mocker):
        """The sequential branch takes no thread at all, which is what makes
        "a response with nothing parallel in it is unchanged" structural
        rather than a claim about timing."""
        _make_parallel(mocker, "get_time")
        ran_on = []

        def handler(params, **kw):
            ran_on.append(threading.current_thread())
            return {"ok": True}

        mocker.patch.object(registry._tools["get_time"], "handler", handler)
        _drive(mocker, [("get_time", {})])

        assert ran_on == [threading.current_thread()], (
            "a single call was handed to a worker; the sequential path must "
            "stay on the caller's own thread")

    def test_the_pool_is_bounded_by_the_configured_ceiling(self, mocker):
        """SUBAGENT_MAX_PARALLEL is a ceiling, not a target: more calls than
        it allows run in waves rather than all at once. Driven at 2 so the
        wave boundary is observable with three calls."""
        _make_parallel(mocker, "get_time")
        mocker.patch.object(config, "SUBAGENT_MAX_PARALLEL", 2)
        live, peak = [], []
        lock = threading.Lock()

        def handler(params, **kw):
            with lock:
                live.append(1)
                peak.append(len(live))
            # Long enough that a third worker would have joined by now if
            # the pool let it.
            threading.Event().wait(0.05)
            with lock:
                live.pop()
            return {"ok": True}

        mocker.patch.object(registry._tools["get_time"], "handler", handler)
        _drive(mocker, [("get_time", {})] * 3)

        assert max(peak) <= 2, (
            f"the pool ran {max(peak)} calls at once against a ceiling of 2")


# ---------------------------------------------------------------------------
# ---- Order (NA11) ---------------------------------------------------------
# ---------------------------------------------------------------------------

class TestResultsComeBackInCallOrder:

    def test_results_are_ordered_by_the_model_not_by_completion(self, mocker):
        """`add_tool_result` pairs each result to its `tool_use` by id
        (M4/D20), so a turn assembled in completion order is a thread that
        will not resume.

        The handlers finish in REVERSE, forced with an event chain rather
        than with sleeps: the first call cannot finish until the last one
        has. So a completion-ordered implementation produces exactly the
        reversed list and fails loudly.
        """
        _make_parallel(mocker, "get_time")
        done = {i: threading.Event() for i in range(3)}
        finished = []

        def handler(params, **kw):
            n = params["n"]
            if n < 2:
                # Wait for the call after me.
                assert done[n + 1].wait(OVERLAP_TIMEOUT_S), (
                    "the later call never finished; they did not overlap")
            finished.append(n)
            done[n].set()
            return {"n": n}

        mocker.patch.object(registry._tools["get_time"], "handler", handler)

        events, _ = _drive(mocker, [("get_time", {"n": i}) for i in range(3)])

        assert finished == [2, 1, 0], (
            f"the handlers were meant to finish in reverse; got {finished}")
        assert _result_ids(events) == ["c0", "c1", "c2"], (
            "results were emitted in completion order")
        assert [r["n"] for r in _results(events)] == [0, 1, 2], (
            "each result was paired with the wrong call")

    def test_two_calls_sharing_one_id_still_get_their_own_answers(
            self, mocker):
        """THE ORDER IS THE POSITION, NOT A LOOKUP (batch 76's review).

        The batch collected its outcomes into a dict keyed by `call.id`
        and rebuilt the list by looking each one back up. That id is the
        PROVIDER'S: core/client.py's v1-compatible branch defaults a
        tool-call fragment's id to "" and fills it only if a delta carries
        one, and the branch twenty lines below it accumulates
        `function.name` across deltas because some providers split it --
        so a provider being sloppy about the id is the case that file is
        written for. Two spawns arriving with one id made the dict hand
        every call in the batch the last-finishing worker's outcome: one
        child's answer reported for a different child's call, which is
        what NA11 exists to prevent.

        The adapter defaults a missing id now too, one commit along. Both,
        because this frame should not need a provider to keep a promise
        for its own bookkeeping to hold.
        """
        _make_parallel(mocker, "get_time")

        def handler(params, **kw):
            return {"n": params["n"]}

        mocker.patch.object(registry._tools["get_time"], "handler", handler)

        # Built by hand, because `_drive` numbers the ids and the whole
        # point here is that they are not distinct.
        uses = make_model_response(text="", tool_calls=[
            {"id": "", "name": "get_time", "input": {"n": n}}
            for n in range(2)])
        seq = [uses, make_model_response(text="done")]
        mocker.patch("core.loop.api_initialization", return_value=object())
        mocker.patch("core.loop.effort_for", return_value=None)
        mocker.patch(
            "core.loop.call_model_stream",
            side_effect=lambda *a, **kw: iter([StreamToken(
                final_response=(seq.pop(0) if seq
                                else make_model_response(text="x")))]))

        events = list(RunAgentLoop._run(
            memory=_Mem(), system_prompt="s", provider_name="ANTHROPIC",
            model="m", context=None, max_steps=2, response_channel=None))

        assert [r["n"] for r in _results(events)] == [0, 1], (
            f"got {_results(events)}; each call is owed the answer its own "
            "handler produced, and two of them cannot be told apart by an "
            "id the provider did not give them")

    def test_every_call_is_announced_before_any_of_them_runs(self, mocker):
        """`tool_call_start` for the whole group, in call order, before the
        first handler is entered. A batch announces its membership at once
        rather than in whatever order three threads reach their first line.
        """
        _make_parallel(mocker, "get_time")
        starts_at_first_handler = []
        seen_starts = []
        gate = threading.Event()

        def handler(params, **kw):
            gate.wait(OVERLAP_TIMEOUT_S)
            return {"ok": True}

        mocker.patch.object(registry._tools["get_time"], "handler", handler)

        # Drained by hand, so the events can be inspected as they arrive
        # rather than only in aggregate.
        uses = make_model_response(text="", tool_calls=[
            {"id": "c%d" % i, "name": "get_time", "input": {}}
            for i in range(3)])
        seq = [uses, make_model_response(text="done")]
        mocker.patch("core.loop.api_initialization", return_value=object())
        mocker.patch("core.loop.effort_for", return_value=None)
        mocker.patch(
            "core.loop.call_model_stream",
            side_effect=lambda *a, **kw: iter([StreamToken(
                final_response=(seq.pop(0) if seq
                                else make_model_response(text="x")))]))

        gen = RunAgentLoop._run(
            memory=_Mem(), system_prompt="s", provider_name="ANTHROPIC",
            model="m", context=None, max_steps=2, response_channel=None)
        for event in gen:
            if event.tool_call_start is not None:
                seen_starts.append(event.tool_call_start["id"])
                if len(seen_starts) == 3:
                    starts_at_first_handler = list(seen_starts)
                    gate.set()

        assert starts_at_first_handler == ["c0", "c1", "c2"], (
            f"the group announced itself as {starts_at_first_handler}; all "
            "three starts must arrive in call order before anything runs")


# ---------------------------------------------------------------------------
# ---- Live narration (NA15) ------------------------------------------------
# ---------------------------------------------------------------------------

class TestNarrationIsLive:
    """N8's fifth blocker: "a synchronous generator with no yield point
    inside dispatch()". A worker cannot yield; it puts events on a conduit
    the still-yielding driver drains.
    """

    def test_a_permission_request_arrives_while_a_sibling_is_still_running(
            self, mocker, real_harness_tier):
        """THE TEST THAT STOPS THE CONDUIT DECAYING INTO A DEFERRED YIELD.

        Collecting every event and yielding it after the batch would satisfy
        every other assertion in this file, so this one makes the DRAIN a
        precondition: the silent sibling blocks until the driver has yielded
        the gated call's permission request. A batched implementation
        deadlocks and fails on the timeout rather than passing quietly.

        A PERMISSION REQUEST, specifically, because that is the narration
        that exists mid-call. A notice attached to a tool RESULT cannot be
        live -- it does not exist until the call is over, and NA11 puts it
        beside its own `tool_result` in call order. The first version of
        this test used one and was red for that reason, which is worth
        recording: "narration is live" is a claim about the events a call
        produces WHILE running, and there is exactly one family of those.
        """
        from core import config_loader
        config_loader.initialize(str(real_harness_tier))
        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            return_value=make_model_response(text="child"))
        mocker.patch("memories.manager.manager.visible", return_value=[])
        _make_parallel(mocker, "get_time")

        observed = threading.Event()

        def handler(params, **kw):
            assert observed.wait(OVERLAP_TIMEOUT_S), (
                "the driver never yielded the sibling's permission request "
                "while this call was still running; narration is not live")
            return {"ok": True}

        mocker.patch.object(registry._tools["get_time"], "handler", handler)

        uses = make_model_response(text="", tool_calls=[
            {"id": "c0", "name": "spawn_subagent",
             "input": {"agent_name": "explore", "task": "a"}},
            {"id": "c1", "name": "get_time", "input": {}}])
        seq = [uses, make_model_response(text="done")]
        mocker.patch("core.loop.api_initialization", return_value=object())
        mocker.patch("core.loop.effort_for", return_value=None)
        mocker.patch(
            "core.loop.call_model_stream",
            side_effect=lambda *a, **kw: iter([StreamToken(
                final_response=(seq.pop(0) if seq
                                else make_model_response(text="x")))]))

        results, saw_request = [], False
        for event in RunAgentLoop._run(
                memory=_Mem(), system_prompt="s", provider_name="ANTHROPIC",
                model="m", context=None, max_steps=2,
                response_channel=ResponseChannel(ask=lambda r: set())):
            if event.permission_request is not None:
                saw_request = True
                observed.set()
            if event.tool_result is not None:
                results.append(event.tool_result["result"])

        assert saw_request, "the permission request never reached the stream"
        assert all("error" not in r for r in results), results

    def test_a_result_notice_travels_beside_its_own_result(self, mocker):
        """The other half, and it is an ordering claim rather than a
        liveness one. A notice a tool attaches to its RESULT is emitted by
        `_settle_one`, so it lands immediately before that call's
        `tool_result` and in the model's call order -- never interleaved
        with a sibling's, which would leave a shell unable to tell whose
        notice it was holding.
        """
        _make_parallel(mocker, "get_time")

        def handler(params, **kw):
            return {"ok": params["n"],
                    "notice": {"kind": "probe", "n": params["n"]}}

        mocker.patch.object(registry._tools["get_time"], "handler", handler)

        events, _ = _drive(mocker, [("get_time", {"n": i}) for i in range(3)])

        pairs = [e for e in events
                 if e.notice is not None or e.tool_result is not None]
        shape = [("notice", e.notice.get("n")) if e.notice is not None
                 else ("result", e.tool_result["result"]["ok"])
                 for e in pairs]
        assert shape == [("notice", 0), ("result", 0),
                         ("notice", 1), ("result", 1),
                         ("notice", 2), ("result", 2)], shape
        assert all("notice" not in r for r in _results(events)), (
            "a notice reached the model; it is plumbing for the shell (J10)")


# ---------------------------------------------------------------------------
# ---- A raising sibling (NA14) --------------------------------------------
# ---------------------------------------------------------------------------

class TestARaisingSibling:

    def test_a_raise_becomes_that_calls_error_and_spares_its_siblings(
            self, mocker):
        """NA14, and it is a deliberate behaviour change scoped to a batch.

        `registry.dispatch` is patched here rather than a handler, and that
        is the point rather than a shortcut: dispatch ALREADY converts a
        handler's exception into an error result, so a raising handler never
        reaches the branch under test. What does reach it is a failure
        ABOVE the handler -- an unknown tool's ValueError, or the approval
        machinery itself -- and patching dispatch is how a test produces
        one. AGENTS.md's warning about mocking dispatch is about tests
        asserting a gate is HONOURED; nothing here asks that, and
        `test_grants.py` drives the real gate.
        """
        _make_parallel(mocker, "get_time")

        def dispatch(name, params, **kw):
            if params.get("boom"):
                raise ValueError("no such tool, allegedly")
            return {"ok": params.get("n")}

        mocker.patch.object(registry, "dispatch", side_effect=dispatch)

        events, _ = _drive(mocker, [
            ("get_time", {"n": 0}),
            ("get_time", {"boom": True}),
            ("get_time", {"n": 2})])

        results = _results(events)
        assert _result_ids(events) == ["c0", "c1", "c2"]
        assert results[0] == {"ok": 0}, "a sibling's answer was discarded"
        assert results[2] == {"ok": 2}, "a sibling's answer was discarded"
        assert "error" in results[1], (
            f"the raiser must report an error result; got {results[1]}")
        assert "no such tool" in results[1]["error"]

    def test_the_turn_continues_after_a_raising_sibling(self, mocker):
        """Not just the batch: the loop must go on to the next model call,
        because discarding the turn is what NA14 exists to prevent."""
        _make_parallel(mocker, "get_time")

        def dispatch(name, params, **kw):
            if params.get("boom"):
                raise ValueError("boom")
            return {"ok": True}

        mocker.patch.object(registry, "dispatch", side_effect=dispatch)
        events, _ = _drive(mocker, [("get_time", {"boom": True}),
                                    ("get_time", {})])
        finals = [e for e in events if e.final_response is not None]
        assert finals, "the turn ended without a final response"
        assert finals[-1].stop_reason == "complete"

    def test_a_LONE_raising_call_still_ends_the_turn(self, mocker):
        """THE NEGATIVE, and it is what scopes NA14 to a batch.

        `test_tui.py` AC3 pins that a raising tool must not kill the app,
        and the mechanism it pins is the exception LEAVING `_run`. A
        conversion applied to every call would quietly retire that, so the
        single-call path must still propagate.
        """
        _make_parallel(mocker, "get_time")
        mocker.patch.object(
            registry, "dispatch", side_effect=ValueError("lone boom"))

        with pytest.raises(ValueError, match="lone boom"):
            _drive(mocker, [("get_time", {})])


# ---------------------------------------------------------------------------
# ---- One question at a time (NA12) ---------------------------------------
# ---------------------------------------------------------------------------

class TestQuestionsAreSerialised:

    def test_two_concurrent_asks_never_overlap_in_the_shell(self):
        """`core/interaction.ask` is the one entry point every question goes
        through, and no shell can hold two pending answers -- the TUI keeps
        one `_permission_channel`, the CLI one `_StdinReader`.

        Asserted from INSIDE the shell, which is the only place the overlap
        would be visible: the fake channel records how many callers are in
        its `ask` at once. Real threads and a barrier, so the two genuinely
        arrive together.
        """
        started = threading.Barrier(2, timeout=OVERLAP_TIMEOUT_S)
        live, peak = [], []
        guard = threading.Lock()

        def ask(request):
            with guard:
                live.append(1)
                peak.append(len(live))
            # Held open long enough that a second caller admitted by a
            # broken lock would be seen.
            threading.Event().wait(0.05)
            with guard:
                live.pop()
            return True

        channel = ResponseChannel(ask=ask)

        def caller():
            started.wait()
            interaction.ask(
                channel, interaction.Request(kind=interaction.APPROVAL,
                                             payload={}))

        threads = [threading.Thread(target=caller) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(OVERLAP_TIMEOUT_S * 2)

        assert peak, "neither caller reached the channel"
        assert max(peak) == 1, (
            f"{max(peak)} callers were inside the shell's ask at once; a "
            "shell that can hold one pending answer was handed two")

    def test_the_serialising_lock_is_in_the_one_entry_point(self):
        """Structurally, because the placement is the decision. A lock
        around the TUI's modal would leave the CLI unprotected and would
        have to be written again for the third shell -- this project's
        canonical rule is to fix at the producer."""
        import inspect
        source = inspect.getsource(interaction.ask)
        assert "_ask_lock" in source, (
            "core.interaction.ask no longer serialises; every shell is then "
            "responsible for a hazard none of them handles")

    def test_the_same_agent_twice_in_one_response_is_one_question(
            self, mocker, real_harness_tier):
        """NA12's second lock, and the regression it prevents.

        §25 R11 and §23 J8 make two spawns of one agent in a turn ONE
        question. Concurrently both calls recall the memo before either
        records an answer, so both ask -- the user is asked twice about the
        same agent, which is a regression in the number of consent
        surfaces rather than a race anyone would see as a crash.

        Two spawns of ONE agent, so the parallel partition applies and the
        gate is what has to hold.

        TWO SCAFFOLDS, AND IT TOOK BOTH -- the mutation that removes the
        lock survived each of them alone, measured, and the reason is the
        interesting part.

        The barrier holds both workers at `request_payload`, the call
        immediately before the window opens, so they enter it together.
        Without it the first worker finished its whole gate before the pool
        had started the second, and the memo was hit for reasons that had
        nothing to do with the guard.

        That was still not enough. Instrumenting `recall_signoff` and
        `remember_signoff` under the mutation showed the second worker
        completing recall AND remember before the first worker's recall ran
        at all -- because the whole window is a handful of Python statements
        with no I/O in it, so it fits inside one 5ms GIL quantum whatever
        the barrier does. What was missing is that a real ask is SLOW: it is
        a human reading a modal. `ask_takes` restores that, and it is
        modelling the window rather than tuning around a race.
        """
        from core import config_loader
        config_loader.initialize(str(real_harness_tier))
        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            return_value=make_model_response(text="child"))
        mocker.patch("memories.manager.manager.visible", return_value=[])

        at_the_gate = threading.Barrier(2, timeout=OVERLAP_TIMEOUT_S)
        real_payload = registry.request_payload

        def payload(name, params, context):
            answer = real_payload(name, params, context)
            at_the_gate.wait()
            return answer

        mocker.patch.object(registry, "request_payload", side_effect=payload)

        events, asked = _drive(
            mocker,
            [("spawn_subagent", {"agent_name": "explore", "task": "a"}),
             ("spawn_subagent", {"agent_name": "explore", "task": "b"})],
            answers=[set(), set()], ask_takes=0.1)

        assert len(asked) == 1, (
            f"the same agent twice in one response asked {len(asked)} "
            "times; the sign-off window is not guarded")
        assert all("error" not in r for r in _results(events)), _results(events)

    def test_two_different_agents_are_two_questions(
            self, mocker, real_harness_tier):
        """The control. The gate must not collapse questions about DIFFERENT
        subjects -- J8 rebuilt the memo around (tool, subject) precisely
        because approving a spawn of `a` had silently covered `b`, and a
        lock that over-shared would reintroduce it as a feature.
        """
        from core import config_loader
        config_loader.initialize(str(real_harness_tier))
        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            return_value=make_model_response(text="child"))
        mocker.patch("memories.manager.manager.visible", return_value=[])

        _, asked = _drive(
            mocker,
            [("spawn_subagent", {"agent_name": "explore", "task": "a"}),
             ("spawn_subagent", {"agent_name": "review", "task": "b"})],
            answers=[set(), set()])

        subjects = [r.payload.get("subject") for r in asked]
        assert sorted(s for s in subjects if s) == ["explore", "review"], (
            f"expected one question per agent; got subjects {subjects}")


# ---------------------------------------------------------------------------
# ---- The grant budget's ceiling (hazard 3) -------------------------------
# ---------------------------------------------------------------------------

class TestTheGrantBudgetHolds:

    def test_concurrent_takes_never_exceed_the_limit(self):
        """§25 R6's whole purpose is a bound on how many pre-authorized
        calls run. `take()` is a read-modify-write on an object shared BY
        REFERENCE across passes and children, so two callers reading `used`
        before either writes it both proceed.

        Real threads and a barrier, because the interleaving is the thing
        under test. Every worker starts together and hammers the budget, so
        an unguarded increment overshoots rather than merely being able to.
        """
        workers, limit = 8, 20
        budget = GrantBudget(limit)
        barrier = threading.Barrier(workers, timeout=OVERLAP_TIMEOUT_S)
        taken = []
        guard = threading.Lock()

        def worker():
            barrier.wait()
            mine = 0
            for _ in range(limit):
                if budget.take():
                    mine += 1
            with guard:
                taken.append(mine)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(OVERLAP_TIMEOUT_S * 2)

        assert sum(taken) == limit, (
            f"{sum(taken)} calls were authorized against a ceiling of "
            f"{limit}; the budget overshot")
        assert budget.used == limit
        assert budget.exhausted is True

    def test_the_decision_and_the_increment_are_both_guarded(self):
        """Structurally. Guarding only the increment keeps the overshoot,
        because the decision to spend IS the read -- and a decision made on
        a stale read is the defect."""
        import inspect
        source = inspect.getsource(GrantBudget.take)
        body = source.split("\"\"\"")[-1]
        assert "with self._lock" in body, (
            "GrantBudget.take no longer takes its lock")
        held = body.split("with self._lock")[1]
        assert "self.used >= self.limit" in held and "self.used += 1" in held, (
            "the ceiling test escaped the lock; only the increment is "
            "guarded, which leaves the overshoot exactly as it was")


# ---------------------------------------------------------------------------
# ---- The activity sink (hazard 9) ----------------------------------------
# ---------------------------------------------------------------------------

class TestTheActivitySink:
    """`TuiActivity`'s docstring used to say its stack was "read and written
    only here, under the worker's own serialisation". That was measured and
    true for as long as one agent ran at a time. NA9 made it false.

    Found during the build rather than in the plan's hazard list, and it is
    the sharpest of them: `exit` and `bind` are read-rebuild-assign, so two
    at once each filter the list they read and the second assignment wins --
    RESURRECTING the row the first removed. A row describing a finished run
    then stays on screen for the session, which is exactly the failure
    `span()`'s `finally` is structural to prevent, reached from the other
    side.
    """

    @staticmethod
    def _sink():
        from core.agent_activity import AgentSpan
        from tui.app import TuiActivity

        class _App:
            def __init__(self):
                self.posted = []

            def post_message(self, message):
                # A snapshot per post, so a test can assert the UI thread was
                # never handed a list somebody went on appending to.
                self.posted.append(message.stack)

        app = _App()
        return TuiActivity(app), app, AgentSpan

    def test_concurrent_exits_leave_no_row_behind(self):
        """A LONG STACK AND A SHORT SWITCH INTERVAL, and both are needed.

        The first version of this test used twelve rows at the default
        switch interval and the mutation removing the lock SURVIVED it,
        measured. The reason is worth writing down: the race needs a thread
        to be preempted BETWEEN reading `self._stack` and assigning the
        rebuilt list, and a twelve-element comprehension finishes in about a
        microsecond against a 5ms scheduling quantum -- so the window
        existed and was essentially never landed in. A test whose input
        cannot discriminate is the batch-14 lesson, arrived at from the
        scheduling side.

        So this widens the window (a few hundred rows, long enough to be
        interrupted) and raises the preemption rate (`setswitchinterval`),
        which changes how OFTEN threads are switched and nothing about what
        the code means. The interval is restored in a `finally`, because
        leaving it at a microsecond would slow every later test in the
        session.
        """
        import sys

        sink, _app, AgentSpan = self._sink()
        saved_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for round_no in range(5):
                # Enough rows that rebuilding the list spans a switch.
                filler = [AgentSpan("filler", 2) for _ in range(400)]
                for span in filler:
                    sink.enter(span)
                spans = [AgentSpan("explore", 1) for _ in range(8)]
                for span in spans:
                    sink.enter(span)

                ready = threading.Barrier(len(spans),
                                          timeout=OVERLAP_TIMEOUT_S)

                def leave(span=None):
                    ready.wait()
                    sink.exit(span)

                threads = [threading.Thread(target=leave, kwargs={"span": s})
                           for s in spans]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(OVERLAP_TIMEOUT_S * 2)

                left = [r for r in sink._stack if r.name == "explore"]
                assert left == [], (
                    f"round {round_no}: {len(left)} row(s) survived every "
                    "run ending. A concurrent exit resurrected a row "
                    "another had already removed, and the panel will "
                    "describe a finished run for the rest of the session")
                for span in filler:
                    sink.exit(span)
        finally:
            sys.setswitchinterval(saved_interval)

    def test_concurrent_enters_lose_nothing(self):
        """The mirror: an append onto the list a rebuild already copied is
        dropped, so a RUNNING child would have no row at all."""
        sink, _app, AgentSpan = self._sink()
        spans = [AgentSpan("explore", 1) for _ in range(16)]
        ready = threading.Barrier(len(spans), timeout=OVERLAP_TIMEOUT_S)

        def arrive(span=None):
            ready.wait()
            sink.enter(span)

        threads = [threading.Thread(target=arrive, kwargs={"span": s})
                   for s in spans]
        for t in threads:
            t.start()
        for t in threads:
            t.join(OVERLAP_TIMEOUT_S * 2)

        assert len(sink._stack) == len(spans), (
            f"{len(sink._stack)} of {len(spans)} running runs have a row")
        assert {r.span_id for r in sink._stack} == {s.id for s in spans}

    def test_the_posted_stack_is_a_snapshot(self):
        """The UI thread stores `message.stack` and iterates it. Handing it
        the live list makes the worker a second writer of what it is
        reading."""
        sink, app, AgentSpan = self._sink()
        sink.enter(AgentSpan("explore", 1))
        first = app.posted[-1]
        assert len(first) == 1
        sink.enter(AgentSpan("review", 2))

        assert len(first) == 1, (
            "an earlier post's list grew when the next span arrived; the UI "
            "thread was handed the object the worker keeps appending to")
        assert first is not sink._stack


# ---------------------------------------------------------------------------
# ---- The panel is a tree --------------------------------------------------
# ---------------------------------------------------------------------------

class TestThePanelDrawsATree:
    """Slice 2 built the data model as a tree on purpose against this day, so
    the rendering already indented by depth. What had to change is the
    ORDER: arrival order and lineage order were the same thing for a stack
    and are not for a tree.
    """

    @staticmethod
    def _rows():
        from tui.widgets import AgentRow
        return AgentRow

    def test_interleaved_peers_keep_their_own_children(self):
        """THE DEFECT, and it needs no concurrency to show -- only the
        arrival order concurrency makes possible. Two children of one turn
        each spawning a grandchild can arrive A, B, A's child, B's child;
        drawn in that order, A's child sits under B at a deeper indent and
        the panel has invented a lineage.
        """
        from tui.widgets import lineage_rows
        AgentRow = self._rows()

        a = AgentRow("explore", 1, "a")
        b = AgentRow("review", 1, "b")
        a_child = AgentRow("plan", 2, "a1", parent_id="a")
        b_child = AgentRow("plan", 2, "b1", parent_id="b")

        ordered = lineage_rows([a, b, a_child, b_child])

        assert [r.span_id for r, _level in ordered] == ["a", "a1", "b", "b1"], (
            "a grandchild is not drawn under its own parent")
        assert [level for _r, level in ordered] == [1, 2, 1, 2], (
            "each grandchild sits one column in from its own parent")

    def test_the_PANEL_draws_them_in_lineage_order(self):
        """THROUGH THE WIDGET, and that is the point of having both.

        Every other test in this class calls `lineage_rows` directly, and
        the mutation that reverts `_redraw` to iterating the raw stack
        SURVIVED all of them -- measured. Testing a leaf and calling it the
        chain is one of the four traps `tests/BREAKING_CHANGES.md` records,
        and this is it: a correct helper nothing calls is not a correct
        panel. So this asserts on what the widget actually DREW.
        """
        from tui.widgets import AgentPanel
        AgentRow = self._rows()

        panel = AgentPanel()
        panel.show("plan", [
            AgentRow("explore", 1, "a"),
            AgentRow("review", 1, "b"),
            AgentRow("alpha", 2, "a1", parent_id="a"),
            AgentRow("beta", 2, "b1", parent_id="b"),
        ])

        drawn = [ln for ln in panel.content.plain.splitlines() if ln.strip()]
        # ["agent", "plan", then the four spans]
        names = [ln.strip().lstrip("▸ ").strip() for ln in drawn[2:]]
        indents = [len(ln) - len(ln.lstrip(" ")) for ln in drawn[2:]]

        assert names == ["explore", "alpha", "review", "beta"], (
            f"the panel drew {names}; each grandchild must follow its own "
            "parent, or the indentation names the wrong one")
        assert indents == [2, 4, 2, 4], (
            f"drew at indents {indents}; peers share a column and their "
            "children sit one level in from them")

    def test_peers_at_one_depth_share_a_column(self):
        """Which is the thing that used to be forbidden. `AgentPanel`'s
        docstring said a flat list "would claim a concurrency this harness
        does not have"; NA9 gave it that concurrency."""
        from tui.widgets import lineage_rows
        AgentRow = self._rows()

        rows = lineage_rows([AgentRow("explore", 1, "a"),
                             AgentRow("review", 1, "b")])
        assert [level for _r, level in rows] == [1, 1]
        assert [r.span_id for r, _level in rows] == ["a", "b"], (
            "peers must keep the order they arrived in; there is no other "
            "honest answer for two runs with the same parent")

    def test_a_row_whose_parent_has_gone_is_still_drawn(self):
        """The defensive half. It cannot happen through `span()` -- a child's
        span opens inside its parent's frame, so parents outlive children --
        but dropping such a row would make the panel lie by omission."""
        from tui.widgets import lineage_rows
        AgentRow = self._rows()

        orphan = AgentRow("plan", 2, "x1", parent_id="gone")
        ordered = lineage_rows([AgentRow("explore", 1, "a"), orphan])
        assert orphan in [row for row, _level in ordered]
        assert len(ordered) == 2
        # Its own depth is the only column available, since it has no walk
        # position -- floored at 1, like every other row.
        assert dict((row.span_id, level) for row, level in ordered)["x1"] == 2

    def test_hand_built_rows_that_share_a_defaulted_id_all_survive(self):
        """`AgentRow.span_id` defaults to `""`, so every hand-built row
        shares one. A guard keyed on span ids treats the second such row as
        already drawn and silently DROPS it -- invisible in the app, where
        ids are real, and wrong in every test that builds rows directly,
        which BREAKING_CHANGES.md already records tests here doing. This is
        why the walk keys on row identity."""
        from tui.widgets import lineage_rows
        AgentRow = self._rows()

        rows = [AgentRow("explore", 1), AgentRow("review", 2)]
        assert len(lineage_rows(rows)) == 2, (
            "a row was dropped for sharing a defaulted span_id")

    def test_a_parent_cycle_terminates(self):
        """A row's parent is data, and data can be wrong in a way that turns
        recursion into a wedged UI rather than a visible mistake. Note that
        the mutation removing the guard HANGS rather than fails, which is
        why the mutation harness carries a subprocess timeout."""
        from tui.widgets import lineage_rows
        AgentRow = self._rows()

        rows = [AgentRow("a", 1, "a", parent_id="b"),
                AgentRow("b", 1, "b", parent_id="a")]
        ordered = lineage_rows(rows)
        assert len(ordered) == 2

    def test_the_column_is_the_walk_and_not_the_reported_depth(self):
        """A RESEARCH PASS AND ITS OWN SUBAGENT BOTH REPORT DEPTH 1.

        `stream_deep_research_mode` opens its span at `context_depth + 1`
        and hands the same context down, so a spawn inside the pass
        computes `subagent_depth` 1 as well -- and indenting by that drew a
        child level with its own parent, which is the lineage the panel
        must never invent. `/init`'s initializer is the same shape.

        Fixed by indenting by the walk rather than by the number C3
        bounds, so the pass keeps every level of its spawn budget.
        """
        from tui.widgets import lineage_rows
        AgentRow = self._rows()

        # Exactly what the sink holds during an attended research run:
        # the pass, and a subagent the pass spawned.
        pass_row = AgentRow("Pass 1", 1, "p")
        spawned = AgentRow("explore", 1, "s", parent_id="p")

        ordered = lineage_rows([pass_row, spawned])

        assert [r.span_id for r, _level in ordered] == ["p", "s"]
        assert [level for _r, level in ordered] == [1, 2], (
            "the pass and its own child were drawn in the same column, so "
            "the panel reads them as peers")

    def test_the_PANEL_draws_a_passs_child_one_level_in(self):
        """Through the widget, for `test_the_PANEL_draws_them_in_lineage_
        order`'s reason: a correct helper nothing calls is not a correct
        panel, and this is the pairing that mutation survived once."""
        from tui.widgets import AgentPanel
        AgentRow = self._rows()

        panel = AgentPanel()
        panel.show(None, [AgentRow("Pass 1", 1, "p"),
                          AgentRow("explore", 1, "s", parent_id="p")])

        drawn = [ln for ln in panel.content.plain.splitlines() if ln.strip()]
        indents = [len(ln) - len(ln.lstrip(" ")) for ln in drawn[2:]]

        assert indents == [2, 4], (
            f"drew at indents {indents}; a pass's subagent has to sit one "
            "column in from the pass, however deep either reports itself")

    def test_the_sink_carries_the_parent_onto_the_row(self):
        """The one hop that was missing between batch 67's span and the
        panel: `parent_id` has been on `AgentSpan` since slice 1, and the row
        had no field for it because a stack did not need one."""
        from core.agent_activity import AgentSpan
        from tui.app import TuiActivity

        class _App:
            def post_message(self, message):
                pass

        sink = TuiActivity(_App())
        sink.enter(AgentSpan("explore", 1, "child", parent_id="parent"))
        assert sink._stack[0].parent_id == "parent", (
            "the row lost its lineage on the way to the panel")

# ---------------------------------------------------------------------------
# ---- The modal names the right asker (hazard 2) ---------------------------
# ---------------------------------------------------------------------------

class TestTheQuestionNamesItsAsker:
    """Discharged by batch 71, and the slice-8 plan says to VERIFY it rather
    than rebuild it. Nothing verified it under concurrency, and it is not
    obvious: two unlabelled "Allow shell?" dialogs are unanswerable, so the
    label is what makes a serialised queue of questions usable at all.

    It holds for one reason -- the asking run is read off §47's ContextVar,
    and every worker carries its own copy of the context. A module-level
    "current agent" would have named whichever run asked most recently, which
    is the answer that is wrong rather than missing.
    """

    def test_two_concurrent_askers_are_each_named_correctly(self):
        from core import agent_activity
        from core.loop import _obtain_approval

        seen = {}
        both_open = threading.Barrier(2, timeout=OVERLAP_TIMEOUT_S)
        guard = threading.Lock()

        def ask(request):
            with guard:
                seen[request.payload["asking_agent"]] = request.payload[
                    "asking_depth"]
            return True

        channel = ResponseChannel(ask=ask)

        def asker(name, depth):
            # A real span, opened the way spawn_subagent opens one, so the
            # var is set by the same code path production uses.
            with agent_activity.span(None, name, depth):
                # BOTH SPANS OPEN BEFORE EITHER ASKS, which is what makes
                # this discriminate. The asks themselves cannot overlap --
                # NA12 serialises them, and a barrier inside the shell's
                # `ask` deadlocks against that lock, which is how the first
                # version of this test failed. What has to be simultaneous
                # is the two OPEN SPANS: a module-level "current agent"
                # would then hold whichever was entered last and label both
                # questions with it, while a ContextVar gives each worker
                # its own.
                both_open.wait()
                _obtain_approval(channel, "get_time", {}, None,
                                 request_payload={})

        threads = [threading.Thread(target=asker, args=a)
                   for a in (("explore", 1), ("review", 2))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(OVERLAP_TIMEOUT_S * 2)

        assert seen == {"explore": 1, "review": 2}, (
            f"the two questions were labelled {seen}; each must name the run "
            "that actually asked it, or a serialised queue of questions is "
            "unanswerable")

    def test_the_asker_is_read_from_the_contextvar(self):
        """Structurally, because the mechanism is what makes the above true
        under threads and a passing behaviour test cannot say which mechanism
        produced it."""
        import inspect

        from core.loop import _obtain_approval

        source = inspect.getsource(_obtain_approval)
        assert "agent_activity.current()" in source, (
            "the asking run is no longer read off the ContextVar; whatever "
            "replaced it is shared between workers unless it is per-context")


# ---------------------------------------------------------------------------
# ---- Through the real app -------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheRunningApp:
    """The closest a test gets to the terminal pass this slice is for.

    Everything above drives `_run` or a widget directly. This mounts the
    actual `VenastineApp`, sends it a turn whose response carries two spawns,
    and asserts on what the SIDEBAR was told -- which is the whole chain:
    partition, pool, per-worker span, the sink's lock, the posted snapshot,
    and the app's own handler. A unit test of any one hop cannot say whether
    the hops are connected, which is the leaf-versus-chain trap this section
    has now fallen into four times.
    """

    @pytest.mark.asyncio
    async def test_two_spawns_are_live_as_peers_in_the_sidebar(
            self, mocker, real_harness_tier):
        from core import config_loader
        from tui.app import VenastineApp

        config_loader.initialize(str(real_harness_tier))

        # The CHILD run is stubbed, not the spawn tool: `subagent_tool.run`
        # still executes, so the span it opens and the depth it computes are
        # real. Held open until both children exist, which is what makes
        # "live as peers" observable rather than a matter of timing.
        both_running = threading.Barrier(2, timeout=OVERLAP_TIMEOUT_S)

        def child(*a, **kw):
            both_running.wait()
            return make_model_response(text="child")

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=child)
        mocker.patch("memories.manager.manager.visible", return_value=[])
        mocker.patch.object(
            VenastineApp, "ask_signoff_blocking",
            # Every argument SWALLOWED, for `FakeMemory`'s reason: what this
            # stub is for is "approve the spawn, grant nothing", and a double
            # that lists the signature breaks on each argument added to it --
            # which is exactly what happened when the sign-off learned to
            # name the run doing the asking.
            side_effect=lambda *_a, **_kw: set())

        uses = make_model_response(text="", tool_calls=[
            {"id": "s0", "name": "spawn_subagent",
             "input": {"agent_name": "explore", "task": "a"}},
            {"id": "s1", "name": "spawn_subagent",
             "input": {"agent_name": "explore", "task": "b"}}])
        seq = [uses, make_model_response(text="done")]
        mocker.patch("core.loop.api_initialization", return_value=object())
        mocker.patch("core.loop.effort_for", return_value=None)
        mocker.patch(
            "core.loop.call_model_stream",
            side_effect=lambda *a, **kw: iter([StreamToken(
                final_response=(seq.pop(0) if seq
                                else make_model_response(text="x")))]))

        snapshots = []
        app = VenastineApp("ANTHROPIC", "test-model", {})
        real_handler = VenastineApp.on_agent_stack_changed

        def record(self, message):
            snapshots.append(list(message.stack))
            return real_handler(self, message)

        mocker.patch.object(VenastineApp, "on_agent_stack_changed", record)

        from tests.conftest import settle
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "go"
            await pilot.press("enter")
            assert await settle(
                pilot, lambda: len(snapshots) >= 2 and not app._busy), (
                "the turn never finished; snapshots so far: "
                f"{[[(r.name, r.depth) for r in s] for s in snapshots]}")

        peers = [s for s in snapshots
                 if len([r for r in s if r.depth == 1]) == 2]
        assert peers, (
            "no snapshot ever held two runs at depth 1. The two spawns did "
            f"not reach the sidebar as peers; snapshots were "
            f"{[[(r.name, r.depth) for r in s] for s in snapshots]}")

        live = [r for r in peers[-1] if r.depth == 1]
        assert len({r.span_id for r in live}) == 2, (
            "two rows, one span id -- the peers are not telling themselves "
            "apart, which is what batch 67's identity exists for")
        assert all(r.parent_id is None for r in live), (
            "a depth-1 peer was recorded as a child of something; both are "
            "children of the chat turn, which opens no span")
