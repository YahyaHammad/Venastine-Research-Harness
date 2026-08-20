"""
test_tool_budgets.py

ROADMAP_v2 §31 (H1-H3, H8-H9). What a tool call is allowed to cost.

Before §31, `dispatch` was `result = spec.handler(params, **injected)` --
no timeout, no alarm, no signal machinery of any kind. Every bound in the
tool layer was per-tool and voluntary: three network tools each set their
own REQUEST_TIMEOUT_S, `shell` inherited the sandbox's, MCP tools got a
two-layer clock inside mcp_client. The six math tools, the only ones that
can burn a core indefinitely, had none -- measured in #57 as a
`symbolic_math series order=100000` that never returns, and a
`discrete_math combinations n=1e7 r=1e6` likewise.

TWO THINGS ARE BEING PINNED HERE, AND THEY FAIL DIFFERENTLY.

  * The DECLARATION (H1). Every statically registered tool says what a
    call to it costs, and omission is fatal at import. This is
    grant_policy's shape one question over, for grant_policy's reason: an
    unwrapped tool looks, from every layer above, exactly like a tool
    that is bounded somewhere else.

  * The MECHANISM (H2). A compute tool is dispatched into a killable
    subprocess. The distinction that matters is that the CPU comes back:
    a thread watchdog can only stop WAITING, because sympy has no
    interruption points to cancel at, so a "timed out" call would keep a
    core busy until the process exits.
    test_a_timed_out_call_is_STOPPED_not_merely_abandoned is what
    separates the two designs, and it is the reason this is a process
    rather than a thread -- with its own positive control beside it,
    because "the file stopped growing" is also what a child that never
    started would produce.

WHY DISPATCH AND NOT THE TOOL. `module.run(params)` stays an ordinary
in-process call; only `registry.dispatch` isolates. That keeps the 123
tests in test_math_tools.py -- which call `run` directly and never
dispatch -- measuring the maths rather than the transport, and it keeps
the seam at the one place a handler is ever invoked in production
(`tools/registry.py`, the only `spec.handler(` call site in the tree).
"""

import json
import os
import re
import subprocess
import sys
import time

import pytest

import config
from tools import isolation
from tools.base import (
    BUDGET_COMPUTE, BUDGET_HUMAN, BUDGET_IO, BUDGETS, GRANT_ANYWHERE,
    ToolSpec, assert_budget_declared,
)
from tools.registry import registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spec(name, budget=BUDGET_IO, handler=None):
    return ToolSpec(name, {}, handler or (lambda params: {"ok": True}),
                    grant_policy=GRANT_ANYWHERE, budget=budget)


# ===========================================================================
# ---- H1: the declaration, and that omitting it is fatal -------------------
# ===========================================================================

class TestEveryToolSaysWhatACallCosts:

    def test_every_statically_registered_tool_declares_a_budget(self):
        """The property the import-time assert exists to hold. Stated as a
        test as well so a failure names the tool rather than only killing
        collection."""
        undeclared = [name for name, spec in registry._tools.items()
                      if not name.startswith("mcp__") and spec.budget is None]
        assert undeclared == [], (
            f"{undeclared} are registered without saying what a call to "
            "them is allowed to cost (ROADMAP_v2 §31, H1)")

    def test_every_declared_budget_is_one_of_the_three_values(self):
        for name, spec in registry._tools.items():
            if name.startswith("mcp__"):
                continue
            assert spec.budget in BUDGETS, f"{name}={spec.budget!r}"

    def test_the_six_math_tools_are_the_compute_class(self):
        """#57's population, named. If a seventh pure-compute tool is added
        and declared io, it silently loses its bound -- which is the whole
        failure this section exists to make impossible to reach quietly."""
        compute = {name for name, spec in registry._tools.items()
                   if spec.budget == BUDGET_COMPUTE}
        assert compute == {"discrete_math", "symbolic_math", "linear_algebra",
                           "probability_stats", "geometry", "logic"}

    def test_the_two_tools_that_wait_on_a_person_are_the_human_class(self):
        human = {name for name, spec in registry._tools.items()
                 if spec.budget == BUDGET_HUMAN}
        assert human == {"ask_user", "spawn_subagent"}

    def test_an_undeclared_tool_is_fatal(self):
        bare = ToolSpec("bare", {}, lambda params: {},
                        grant_policy=GRANT_ANYWHERE)
        with pytest.raises(RuntimeError, match="undeclared=\\['bare'\\]"):
            assert_budget_declared({"bare": bare}, {"bare": bare})

    def test_a_misspelled_budget_is_fatal_rather_than_ignored(self):
        """R13's own lesson, quoted in its docstring: a misspelled value
        would drop the tool out of the mechanism *while looking answered*,
        which is worse than an omission."""
        typo = ToolSpec("typo", {}, lambda params: {},
                        grant_policy=GRANT_ANYWHERE, budget="computee")
        with pytest.raises(RuntimeError, match="invalid=.*computee"):
            assert_budget_declared({"typo": typo}, {"typo": typo})

    def test_a_compute_tool_that_declares_an_injection_is_fatal(self):
        """Not a style rule. BUDGET_COMPUTE routes the call through a
        process boundary and nothing in _INJECTABLE_PARAMS survives one --
        a live ConversationMemory does not pickle. Caught at import rather
        than at the first call."""
        def handler(params, memory=None):
            return {}

        spec = ToolSpec("c", {}, handler, grant_policy=GRANT_ANYWHERE,
                        budget=BUDGET_COMPUTE)
        with pytest.raises(RuntimeError, match="compute-with-injection"):
            assert_budget_declared({"c": spec}, {"c": spec},
                                   {"c": ("memory",)})

    def test_the_same_tool_declared_io_with_an_injection_is_fine(self):
        """The control for the test above: it is the PAIRING that is
        rejected, not either half. `pin`, `remember` and `todo_write` are
        all io and all inject."""
        def handler(params, memory=None):
            return {}

        spec = ToolSpec("i", {}, handler, grant_policy=GRANT_ANYWHERE,
                        budget=BUDGET_IO)
        assert_budget_declared({"i": spec}, {"i": spec}, {"i": ("memory",)})

    def test_a_dynamic_mcp_name_is_exempt(self):
        """Named at connection time, so no static declaration can exist --
        R13's exemption, unchanged."""
        spec = ToolSpec("mcp__s__t", {}, lambda params: {})
        assert_budget_declared({"mcp__s__t": spec}, {"mcp__s__t": spec})

    def test_the_mcp_registration_declares_one_anyway(self):
        """Exempt is not the same as absent. mcp_client's own two-layer
        clock IS the io bound, and registration.py says so rather than
        leaning on the exemption -- the same trade it already makes for
        grant_policy."""
        source = open(os.path.join(ROOT, "mcp_client", "registration.py"),
                      encoding="utf-8").read()
        assert "budget=BUDGET_IO" in source

    def test_the_assert_runs_at_import_not_only_in_this_file(self):
        """N4's rule, and B11's: a missing declaration must be loud at CI,
        not a runtime warning nobody reaches. The call site is in
        registry.py beside R13's, after the static registrations and
        before any dynamic one."""
        source = open(os.path.join(ROOT, "tools", "registry.py"),
                      encoding="utf-8").read()
        assert re.search(r"^assert_budget_declared\(", source, re.M), (
            "assert_budget_declared is not called at registry import")


# ===========================================================================
# ---- H2/H3/H9: the mechanism ----------------------------------------------
# ===========================================================================

_SLOW = {"operation": "series", "expression": "exp(x)", "point": "0",
         "order": 100000}


class TestAComputeToolIsBoundedByAClockThatCanStopIt:

    def test_a_runaway_returns_an_error_instead_of_never_returning(self,
                                                                   monkeypatch):
        """#57's headline case. Before §31 this call did not return at all;
        the probe in the issue killed it externally at 8s."""
        monkeypatch.setattr(config, "TOOL_COMPUTE_TIMEOUT_S", 3)
        started = time.time()
        result = registry.dispatch("symbolic_math", dict(_SLOW))
        elapsed = time.time() - started

        assert "error" in result
        assert elapsed < 30, "the budget did not bound the call"

    def test_the_error_names_the_tool_and_the_budget(self, monkeypatch):
        """A model that cannot tell "this is too big" from "this broke"
        retries the same call. B2 settled the same point for shape
        errors."""
        monkeypatch.setattr(config, "TOOL_COMPUTE_TIMEOUT_S", 3)
        result = registry.dispatch("symbolic_math", dict(_SLOW))

        assert "symbolic_math" in result["error"]
        assert "3" in result["error"]

    def test_a_child_killed_by_its_own_cpu_limit_reports_the_limit(self):
        """H3's inner layer kills the CHILD, so subprocess.run RETURNS
        with a negative returncode and never raises TimeoutExpired --
        the parent's timeout branch does not run at all.

        This was live for a day: on Linux the answer was
        "symbolic_math did not return a usable result (exit -9)" while
        Windows, which has no rlimit and therefore only the wall clock,
        gave the actionable one. The better-protected platform gave the
        worse message. CI caught it; the container run that would have
        caught it first had not been run yet.

        Driven through _interpret directly so it holds on both
        platforms -- the signal itself is POSIX-only, but the parent's
        handling of what it reports is not.
        """
        result = isolation._interpret("symbolic_math", "", "", -9, 15)

        assert "exceeded its 15s limit" in result["error"]
        assert "smaller" in result["error"]

    def test_both_bounds_give_the_SAME_sentence(self):
        """The property, rather than two copies of a string. Whichever
        layer fires, a model reading the result has to be able to act on
        it the same way."""
        by_signal = isolation._interpret("symbolic_math", "", "", -9, 15)
        by_clock = isolation._limit_message("symbolic_math", 15)

        assert by_signal["error"] == by_clock

    def test_a_crashed_child_is_NOT_reported_as_a_limit(self):
        """The control, and the reason this is a returncode check rather
        than a catch-all. A child that exits non-zero WITHOUT a signal
        has a bug, not a budget problem, and telling the model to try
        smaller numbers would send it round a loop that cannot end."""
        result = isolation._interpret("symbolic_math", "", "boom", 1, 15)

        assert "did not return a usable result" in result["error"]
        assert "smaller" not in result["error"]

    def test_an_ordinary_call_still_returns_its_result(self):
        """The cost of the boundary is one interpreter start, measured at
        ~0.34s against a harness whose every step is a model call."""
        result = registry.dispatch("discrete_math",
                                   {"operation": "gcd", "a": 462, "b": 1071})
        assert result == {"result": "21", "operation": "gcd"}

    def test_a_tool_error_crosses_the_boundary_unchanged(self):
        """The child returns the tool's own error dict, not a transport
        failure. A tool that rejects its input must look identical
        isolated and not."""
        from tools.builtin import discrete_math

        direct = discrete_math.run({"operation": "mod_inverse", "base": 2,
                                    "modulus": 4})
        dispatched = registry.dispatch("discrete_math",
                                       {"operation": "mod_inverse",
                                        "base": 2, "modulus": 4})
        assert dispatched == direct
        assert "error" in dispatched

    def test_a_timed_out_call_is_STOPPED_not_merely_abandoned(self, tmp_path):
        """THE TEST THAT CHOSE THE DESIGN, and the one a thread watchdog
        would fail.

        Every other test in this class passes under a thread watchdog: it
        returns an error at the budget and the caller carries on. What a
        thread cannot do is stop the work -- CPython offers no interruption
        point inside `sympy.binomial` to cancel at -- so the core stays
        busy until the process exits and the harness reports a timeout
        that did not happen.

        Measured by evidence the work leaves behind rather than by asking
        the mechanism whether it worked: the child appends to a file every
        50ms, and the file must stop growing once the call returns.

        NOT measured with os.times(): its children_user/children_system
        fields are always 0.0 on Windows, so that assertion would be
        trivially true on the platform this is usually run on -- a test
        that passes because its input is already zero, which is the exact
        vacuity class #15 records.
        """
        from tests import _isolation_probe

        ticks = tmp_path / "ticks"
        ticks.write_text("", encoding="utf-8")

        result = isolation.run_isolated(
            _isolation_probe.tick_forever,
            {"path": str(ticks), "interval": 0.05}, 2.0,
            tool_name="probe")

        assert "error" in result and "2s limit" in result["error"]

        settled = ticks.stat().st_size
        assert settled > 0, "the child never ran, so nothing was measured"
        time.sleep(1.5)
        assert ticks.stat().st_size == settled, (
            "the child kept working after the call returned -- it was "
            "abandoned, not stopped")

    def test_the_probe_would_notice_a_child_that_kept_going(self, tmp_path):
        """Guards the guard. The test above asserts that a file STOPPED
        growing, which is exactly what it would assert if the child had
        never started. This is the positive control: the same handler,
        observed while it is still running."""
        from tests import _isolation_probe

        ticks = tmp_path / "ticks"
        ticks.write_text("", encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, "-m", "tools.isolation"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            cwd=isolation._package_root(), env=isolation._child_env())
        proc.stdin.write(json.dumps({
            "module": "tests._isolation_probe", "function": "tick_forever",
            "params": {"path": str(ticks), "interval": 0.05}}))
        proc.stdin.close()
        try:
            time.sleep(1.0)
            first = ticks.stat().st_size
            time.sleep(1.0)
            assert ticks.stat().st_size > first, (
                "an unkilled child did not keep growing the file, so the "
                "test above is asserting nothing")
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_the_child_does_not_inherit_the_environment(self, monkeypatch):
        """A math tool needs no credentials, and the child is a fresh
        interpreter with a minimal env -- sandbox._scrubbed_env's argument,
        applied to the other subprocess this project spawns.

        The variables are SET here first. An earlier version asserted that
        "ANTHROPIC_API_KEY" was absent from the child env without setting
        it in the parent, so it was absent before `_child_env` did
        anything -- true of a function that copies os.environ wholesale,
        and true of no function at all. A test whose input is already zero
        is the vacuity class this batch recorded for os.times(), reached a
        second way in the same file.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SENTINEL")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-SENTINEL")
        monkeypatch.setenv("VENASTINE_UNRELATED", "also-not-wanted")

        env = isolation._child_env()

        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "VENASTINE_UNRELATED" not in env
        assert "SENTINEL" not in "".join(env.values())
        assert "PYTHONPATH" in env, (
            "the child cannot import its own tool without this")

    def test_the_child_is_located_from_the_module_not_the_cwd(self):
        """dispatch has no guarantee about the working directory -- the TUI
        runs from wherever it was launched, and a subagent inherits
        nothing. Resolving the child from __file__ is what makes the
        mechanism independent of that."""
        assert os.path.isabs(isolation._package_root())
        assert os.path.isdir(os.path.join(isolation._package_root(), "tools"))

    def test_an_unparseable_child_response_is_an_error_not_a_crash(self):
        """dispatch's containment covers a raising handler; this covers a
        child that says something the parent cannot read."""
        result = isolation._interpret("discrete_math", "not json at all",
                                      "", 0, 15)
        assert "error" in result
        assert "discrete_math" in result["error"]

    def test_the_child_stdout_is_bounded_on_read(self):
        """#55's own lesson applied to the mechanism that fixes it: bound
        the read, do not slice afterwards.

        The payload is a WELL-FORMED marked result that is merely too
        large. An earlier version sent unmarked filler, which reaches the
        "did not return a usable result" branch and produces an error
        dict whether the size check runs or not -- two paths agreeing on
        one input, so deleting the check left the test green.
        """
        assert isolation.MAX_CHILD_OUTPUT_BYTES > 0
        payload = json.dumps(
            {"result": "y" * (isolation.MAX_CHILD_OUTPUT_BYTES + 1000)})
        oversized = isolation.MARKER + payload + "\n"
        assert len(oversized) > isolation.MAX_CHILD_OUTPUT_BYTES

        result = isolation._interpret("discrete_math", oversized, "", 0, 15)

        assert "error" in result, (
            "an oversized but perfectly parseable result was returned "
            "verbatim -- the size check is not doing anything")
        assert "more output than can be returned" in result["error"]

    def test_a_marked_result_UNDER_the_cap_is_returned_verbatim(self):
        """The control for the test above. A check that rejected every
        marked result would satisfy it."""
        payload = json.dumps({"result": "21", "operation": "gcd"})
        out = isolation._interpret(
            "discrete_math", isolation.MARKER + payload + "\n", "", 0, 15)
        assert out == {"result": "21", "operation": "gcd"}


class TestTheHumanAndIoClassesAreNotWrapped:

    def test_ask_user_is_not_bounded_by_the_compute_budget(self, monkeypatch):
        """H8's regression. ask_user blocks on a person for up to
        ATTENDED_APPROVAL_TIMEOUT_S (600s); a 15s compute clock over it
        would answer the question on the user's behalf."""
        monkeypatch.setattr(config, "TOOL_COMPUTE_TIMEOUT_S", 1)

        slept = []

        def slow_channel(*args, **kwargs):
            time.sleep(2)
            slept.append(True)
            return None

        monkeypatch.setattr("core.interaction.ask", slow_channel)
        result = registry.dispatch(
            "ask_user", {"question": "which?"},
            response_channel=object())

        assert slept, "the call was cut short by the compute budget"
        assert "error" in result  # nobody answered -- ask_user's own message

    def test_an_io_tool_runs_in_process(self, monkeypatch):
        """`pin`, `remember` and `todo_write` inject a live object. If the
        io class were ever routed through the isolator they would fail at
        the first call, not at import."""
        calls = []
        monkeypatch.setattr(isolation, "run_isolated",
                            lambda *a, **k: calls.append(a) or {})
        registry.dispatch("get_time", {})
        assert calls == []


class TestTheBudgetIsOneNumberInOnePlace:

    def test_the_constant_exists_and_is_positive(self):
        assert isinstance(config.TOOL_COMPUTE_TIMEOUT_S, (int, float))
        assert config.TOOL_COMPUTE_TIMEOUT_S > 0

    def test_it_leaves_room_for_the_slowest_legitimate_call(self):
        """`series order=1000` is 3.44s measured. A budget at or below that
        would refuse work the tool is for, which is how a bound stops being
        a bound and starts being a bug report."""
        assert config.TOOL_COMPUTE_TIMEOUT_S >= 10

    def test_dispatch_reads_the_constant_rather_than_a_literal(self,
                                                               monkeypatch):
        """A second copy of the number is a second thing to revise."""
        monkeypatch.setattr(config, "TOOL_COMPUTE_TIMEOUT_S", 3)
        started = time.time()
        registry.dispatch("symbolic_math", dict(_SLOW))
        assert time.time() - started < 30


@pytest.mark.skipif(sys.platform == "win32",
                    reason="rlimit is POSIX-only; H3 records the asymmetry")
class TestTheUnixSecondLayer:

    def test_the_child_carries_cpu_and_memory_limits(self):
        """H3: mcp_client's documented two-layer shape. The wall clock is
        the backstop for 'the answer is never coming'; RLIMIT_CPU is what
        actually stops the work, and sooner. Windows has neither rlimit
        nor a cheap equivalent, so it runs on the clock alone -- recorded
        rather than hidden."""
        assert isolation._resource_limits() is not None

    def test_a_memory_bomb_is_stopped_by_the_rlimit(self):
        """Allocation costs time roughly proportional to size (~39 MB/s
        measured), so the wall clock is already a loose memory bound. This
        makes it a hard one where the platform allows."""
        child = subprocess.run(
            [sys.executable, "-c",
             "import resource, sys\n"
             "resource.setrlimit(resource.RLIMIT_AS, (200_000_000,) * 2)\n"
             "try:\n"
             "    x = bytearray(500_000_000)\n"
             "    print('ALLOCATED')\n"
             "except MemoryError:\n"
             "    print('REFUSED')\n"],
            capture_output=True, text=True, timeout=30)
        assert "REFUSED" in child.stdout


class TestTheIsolatorRoundTrip:

    def test_every_compute_tool_survives_the_boundary(self):
        """One representative call per compute tool, through dispatch. The
        params are JSON on the way in and the result is JSON on the way
        back, so a tool returning something unserialisable would fail
        here rather than in production."""
        probes = [
            ("discrete_math", {"operation": "gcd", "a": 12, "b": 18}),
            ("symbolic_math", {"operation": "factor",
                               "expression": "x**2 - 4"}),
            ("linear_algebra", {"operation": "transpose",
                                "matrix_a": [["1", "2"]]}),
            ("probability_stats", {"operation": "describe",
                                   "data": [1.0, 2.0, 3.0]}),
            ("geometry", {"operation": "distance",
                          "points": [["0", "0"], ["3", "4"]]}),
            ("logic", {"operation": "tautology", "expression": "a | ~a"}),
        ]
        for tool, params in probes:
            result = registry.dispatch(tool, params)
            assert "error" not in result, f"{tool}: {result}"
            assert json.loads(json.dumps(result)) == result
