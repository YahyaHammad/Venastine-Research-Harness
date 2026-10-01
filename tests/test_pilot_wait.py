"""
test_pilot_wait.py

TECHNICAL_DEBT 8: tests for `settle` and `pump` themselves.

WHY A TEST UTILITY GETS ITS OWN TESTS. `settle` had five copies across the
suite, five different budgets, two different orderings, and no test of any
kind -- so the two bugs in it were invisible to 1246 passing tests. It is
also the one thing here whose failure mode is a FLAKE, and the flake did
not reproduce once across ~10 full-suite runs in the container this was
fixed in. "It stopped failing" is therefore not available as evidence, and
these tests are what stands in for it: they assert the helper's contract
directly, so a regression is a named red test rather than a rare one.

The pilot is REAL throughout, never a double. The helper's whole contract
is about how it drives `Pilot.pause`, and a fake pause with a plausible
sleep in it would be a model of the thing whose behaviour was the bug --
exactly the "verify against production code, not the test double" rule in
AGENTS.md, with `pause` in the production role.
"""

import asyncio
import inspect
import time

import pytest
from textual._wait import wait_for_idle
from textual.app import App
from textual.widgets import Static

from tests.conftest import pump, settle

#: What ONE `pilot.pause()` can cost, read from textual rather than
#: restated. Batch 116 restated it as "1000ms" and set two ceilings from
#: that number; batch 116's own correction is that `Pilot.pause` is
#: `_wait_for_screen(timeout=30.0)` followed by `wait_for_idle(0)`, so the
#: cap is the idle half ALONE. Reading the signature means a textual
#: release that moves the cap moves these ceilings with it, and a textual
#: release that renames the parameter fails here loudly instead of leaving
#: a stale constant behind.
#:
#: `textual._wait` is private, and that is accepted: `settle`'s own
#: docstring already names this module, so the coupling exists either way.
PAUSE_CAP = inspect.signature(wait_for_idle).parameters["max_sleep"].default

#: Slack for the swing BETWEEN two consecutive pauses, which is the one
#: thing a calibration cannot predict: `settle`'s docstring measures 21ms
#: idle against 1021ms with a busy sibling, so the calibration pause and
#: the measured pause can land 46x apart in either direction.
PAUSE_SLACK_S = 0.5


async def _one_pause(pilot) -> float:
    """How long one bare `pilot.pause()` costs on THIS machine, now.

    The ceilings below are relative to this rather than to a constant,
    because the constant is textual's and this repo got it wrong once by
    writing it down.
    """
    started = time.monotonic()
    await pilot.pause()
    return time.monotonic() - started


class _Tiny(App):
    """The smallest app a Pilot can drive. `settle` never touches the DOM."""

    def compose(self):
        yield Static("x")


class _Counting:
    """A real Pilot with a pause counter -- delegation, not a fake.

    Counting is the only way to assert the quiescing pause happened: it has
    no observable effect on the predicate (that is the point of it), so the
    call itself is the behaviour under test.
    """

    def __init__(self, pilot):
        self._pilot = pilot
        self.pauses = 0

    async def pause(self, delay=None):
        self.pauses += 1
        await self._pilot.pause(delay)


# ---------------------------------------------------------------------------
# ---- settle: the quiesce (defect 2) ---------------------------------------
# ---------------------------------------------------------------------------

class TestTheQuiesce:

    @pytest.mark.asyncio
    async def test_an_already_true_predicate_still_pauses_once(self):
        """The load-bearing assertion of the whole fix.

        A predicate that is true on entry must NOT return without giving
        the loop a turn. `isinstance(app.screen, SomeModal)` goes true
        while the worker is still inside `call_from_thread(push_screen)`
        and has not reached `channel.get()`; returning immediately lets a
        test join that thread from the event-loop thread and deadlock.

        Asserted on the pause COUNT rather than on any state, because a
        quiesce that had an observable effect on the predicate would not
        be a quiesce.

        THIS TEST IS THE ONLY PIN. Deleting the quiesce leaves
        tests/test_tui.py green 43/43 under 4x CPU load, because the bare
        `pause()` used for polling already masks the need. The pairing that
        actually fails is fast polling WITHOUT the quiesce -- measured 3/3
        on `test_quitting_during_an_attended_research_prompt_releases_the_
        worker`. So the deadlock is real and the line prevents it, but no
        TUI test can currently demonstrate that, which is exactly why the
        helper needed tests of its own.
        """
        async with _Tiny().run_test() as pilot:
            counting = _Counting(pilot)
            assert await settle(counting, lambda: True) is True
            assert counting.pauses == 1, \
                "the quiescing pause did not run -- see defect 2"

    @pytest.mark.asyncio
    async def test_the_quiesce_also_runs_after_a_real_wait(self):
        """Not just on the already-true shortcut: the true path is one
        path, and a `return True` placed before the pause would pass the
        test above only if that test were the shortcut case."""
        async with _Tiny().run_test() as pilot:
            counting = _Counting(pilot)
            flips = [False, False, True]
            await settle(counting, lambda: flips.pop(0) if flips else True)
            # two polling pauses for the two False answers, one quiesce.
            assert counting.pauses == 3

    @pytest.mark.asyncio
    async def test_no_quiesce_on_the_timeout_path(self):
        """A timeout returns False and the caller asserts on it. Pausing
        again would only delay a failure that has already been decided."""
        async with _Tiny().run_test() as pilot:
            counting = _Counting(pilot)
            assert await settle(counting, lambda: False, timeout=0.2) is False
            before = counting.pauses
            await asyncio.sleep(0)
            assert counting.pauses == before


# ---------------------------------------------------------------------------
# ---- settle: wall clock, not a pump count (defect 1) ----------------------
# ---------------------------------------------------------------------------

class TestItWaitsOnTime:

    @pytest.mark.asyncio
    async def test_a_never_true_predicate_gives_up_after_the_timeout(self):
        async with _Tiny().run_test() as pilot:
            started = time.monotonic()
            assert await settle(pilot, lambda: False, timeout=0.5) is False
            elapsed = time.monotonic() - started
        assert elapsed >= 0.5, f"gave up early after {elapsed:.3f}s"
        # < 2.0 rather than < 3.0 so this discriminates too: the old
        # 120-pump helper takes ~2.5s here on an idle machine regardless of
        # the timeout asked for, because a count cannot honour one. The
        # ceiling allows one full `pause()` of overshoot (capped at 1000ms
        # under load) on top of the 0.5s deadline.
        assert elapsed < 2.0, f"overshot the deadline at {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_it_outlasts_the_old_pump_budget(self):
        """THE mutation-prover for defect 1, and the reason this test is
        allowed to be slow.

        The helper this replaced pumped 120 times. `pilot.pause()` floors
        at `wait_for_idle`'s 20ms tick, so on a machine with nothing
        burning CPU in-process that budget is ~2.5s (measured: 2.535s) --
        never the "microseconds" TECHNICAL_DEBT 8 claimed. A predicate
        that flips at 3.0s therefore separates the two implementations:
        the old one returns False, a wall-clock deadline returns True.

        Swap the deadline back for `for _ in range(120)` and this fails.

        Note for anyone re-running that mutation: check it on an IDLE
        machine. Under CPU load each pause stretches toward its 1000ms
        cap, so 120 pumps becomes ~122s and the old helper would pass
        this too. The 46x swing is the bug; it also hides the bug.
        """
        async with _Tiny().run_test() as pilot:
            flip_at = time.monotonic() + 3.0
            assert await settle(
                pilot, lambda: time.monotonic() >= flip_at, timeout=10.0
            ) is True, "gave up before 3s -- budgeting pumps, not time?"

    @pytest.mark.asyncio
    async def test_a_passing_wait_costs_only_what_it_needs(self):
        """A generous timeout is not a slow suite: the deadline bounds
        failures, and a satisfied predicate returns at once. This is why
        raising the ceiling from ~2.5s to 10s did not change the suite's
        runtime.

        THE CEILING IS MEASURED, NOT WRITTEN DOWN, and the difference is
        two CI failures' worth of lesson. A satisfied predicate costs
        exactly one `pilot.pause()` -- `settle` checks, pauses to quiesce,
        returns -- so the ceiling is "one pause plus slack", and the only
        honest way to spend one pause is to time one.

        It was `< 1.0` until 2026-10-01, when CI read 1.010s: `wait_for_idle`
        reaches `max_sleep=1` and then overshoots by one 20ms
        `SLEEP_GRANULARITY` tick before it checks, so 1.0 was below the cap
        of the thing being timed. Batch 116 replaced it with a flat 2.0 and
        called that "one full capped pause plus margin" -- which was still
        wrong, because `Pilot.pause` is `_wait_for_screen(timeout=30.0)`
        AND `wait_for_idle(0)`, and 1000ms is only the second half.

        So: no absolute. `PAUSE_CAP` comes from textual's signature and the
        rest is measured here. Still 20x clear of the 30s timeout this
        exists to prove is not paid, which is what keeps it a test.
        """
        async with _Tiny().run_test() as pilot:
            one_pause = await _one_pause(pilot)

            started = time.monotonic()
            await settle(pilot, lambda: True, timeout=30.0)
            elapsed = time.monotonic() - started

        budget = one_pause + PAUSE_CAP + PAUSE_SLACK_S
        assert budget < 30.0 / 2, (
            "the budget stopped discriminating against the timeout: "
            "%.3fs against a 30.0s deadline" % budget)
        assert elapsed < budget, (
            "paid for the timeout it did not need: %.3fs against a budget "
            "of %.3fs (one measured pause %.3fs + cap %.3fs + slack %.1fs)"
            % (elapsed, budget, one_pause, PAUSE_CAP, PAUSE_SLACK_S))


# ---------------------------------------------------------------------------
# ---- pump: a different job ------------------------------------------------
# ---------------------------------------------------------------------------

class TestPump:

    @pytest.mark.asyncio
    async def test_it_pumps_exactly_what_it_was_asked_for(self):
        async with _Tiny().run_test() as pilot:
            counting = _Counting(pilot)
            await pump(counting, 7)
            assert counting.pauses == 7

    @pytest.mark.asyncio
    async def test_it_stays_a_count_and_not_a_deadline(self):
        """`pump` backs NEGATIVE assertions -- "prove X did not happen" --
        so there is no predicate and nothing to wait for. Giving it a
        timeout instead would make each of those sites pay the full
        deadline and prove nothing extra. Pinned on the runtime, because
        that is the cost that would regress.

        THREE PAUSES, SO THREE CAPS. This one never failed; it was found by
        grepping for the root cause of the one above that did, and it was
        the MORE exposed of the two -- `pump(3)` is three `pilot.pause()`
        calls, worst case ~3.0s, against a ceiling that was 2.0. Batch 116
        raised it to a flat 4.0 on the same wrong arithmetic corrected
        above; it is relative now for the same reason.

        Still discriminates against the regression it names: a `pump` that
        grew a deadline would pay `settle`'s 10s default.
        """
        pumped = 3
        async with _Tiny().run_test() as pilot:
            one_pause = await _one_pause(pilot)

            started = time.monotonic()
            await pump(pilot, pumped)
            elapsed = time.monotonic() - started

        budget = one_pause + pumped * PAUSE_CAP + PAUSE_SLACK_S
        assert budget < 10.0 / 2, (
            "the budget stopped discriminating against a grown deadline: "
            "%.3fs against settle's 10.0s default" % budget)
        assert elapsed < budget, (
            "pump(%d) took %.3fs against a budget of %.3fs -- has it grown "
            "a deadline?" % (pumped, elapsed, budget))


# ---------------------------------------------------------------------------
# ---- The stall-vs-fail fixture --------------------------------------------
# ---------------------------------------------------------------------------

def test_the_approval_timeout_is_shrunk_for_tests():
    """`shrink_approval_timeout` has no other pin, by its nature.

    Deleting it breaks no assertion anywhere: it does not change an
    outcome, only how long a broken modal path takes to get there --
    600s in production, which under pytest is a stall rather than a
    failure. Nothing green turns red when it goes, so the fixture being
    in effect is asserted directly.

    `tui/app.py` reads this at call time, so patching the module
    attribute is enough to reach `_blocking_modal`'s `channel.get`.
    """
    import config
    assert config.ATTENDED_APPROVAL_TIMEOUT_S <= 10, (
        "an unanswered modal will stall the suite instead of failing it -- "
        "is the shrink_approval_timeout fixture still autouse?")


# ---------------------------------------------------------------------------
# ---- No sixth copy --------------------------------------------------------
# ---------------------------------------------------------------------------

def test_no_test_module_defines_its_own_settle():
    """The duplication was the reason the bugs survived 1246 tests.

    Five implementations existed -- budgets 40/60/120/120/200 and two
    incompatible orderings -- so a fix to the one TECHNICAL_DEBT 8 named
    by filename reached one of them. Worse, the copy in
    test_shell_compaction.py was deliberate and its docstring said so:
    importing the canonical one would have coupled this file's timing to a
    known-broken helper. That reasoning was sound while the helper was
    broken and is exactly wrong now, which is the sort of comment that
    outlives its premise unless something checks.

    A grep test rather than a convention, because a convention is what
    produced five copies.
    """
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("tests").glob("test_*.py")):
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.startswith(("async def _settle", "def _settle",
                                "async def settle", "def settle")):
                offenders.append(f"{path}:{number}")
    assert not offenders, (
        "a local wait helper is back -- import `settle` from tests/conftest.py "
        f"instead: {offenders}")
