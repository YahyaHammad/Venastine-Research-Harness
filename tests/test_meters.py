"""
test_meters.py

Batch 61's `tui/meters.py`, driven directly.

NO PILOT AND NO CLOCK, which is the whole reason `TurnMeter` takes `now`
as a parameter instead of reading one. A live timer tested through a real
0.4s tick is a test that either sleeps or flakes; every case here hands the
meter the seconds it wants and asserts on arithmetic. The painted result --
that these strings reach the prompt's border, and that they move while the
commit cap is withholding a table -- is `test_tui.py`'s job, because that
is a wiring question and this is not.
"""

import pytest

from tui import meters
from tui.meters import TurnMeter


# ---------------------------------------------------------------------------
# ---- Formatting -----------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheFiguresAreReadableAtEveryScale:
    """Two formatters, and each is coarser than the last on purpose."""

    @pytest.mark.parametrize("seconds,expected", [
        (0.0, "0.0s"),
        (0.04, "0.0s"),
        (12.44, "12.4s"),
        (59.9, "59.9s"),
        (60.0, "1m00s"),
        (61.5, "1m01s"),
        (3599.0, "59m59s"),
        (3600.0, "1h00m"),
        (7380.0, "2h03m"),
    ])
    def test_a_turn_reads_in_tenths_under_a_minute_and_whole_seconds_above(
            self, seconds, expected):
        assert meters._duration(seconds) == expected

    @pytest.mark.parametrize("seconds,expected", [
        (0.0, "up 0s"),
        (59.9, "up 59s"),
        (60.0, "up 1m"),
        (119.0, "up 1m"),
        (3599.0, "up 59m"),
        (3600.0, "up 1h00m"),
        (7380.0, "up 2h03m"),
    ])
    def test_uptime_is_coarser_than_the_turn_clock(self, seconds, expected):
        """Deliberately no tenths.

        A figure carrying tenths changes on every tick, so the change-guard
        in app.py would repaint the border twice a second forever to report
        something nobody is watching. Minutes change a minute apart.
        """
        assert meters._uptime(seconds) == expected


# ---------------------------------------------------------------------------
# ---- The turn clock -------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheClockMeasuresTheHarnessAndNotTheHuman:

    def test_elapsed_is_wall_clock_when_nothing_blocks(self):
        m = TurnMeter()
        m.begin_session(100.0)
        m.start(100.0)
        assert m.elapsed(112.5) == pytest.approx(12.5)

    def test_a_modal_does_not_count_toward_the_turn(self):
        """The decision this feature was asked to make: a 30-second
        permission answer is the user's time, and counting it makes the
        model look slow and the throughput figure collapse."""
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.set_blocked(True, 2.0)
        m.set_blocked(False, 32.0)          # thirty seconds reading a modal
        assert m.elapsed(40.0) == pytest.approx(10.0)

    def test_time_inside_an_open_modal_is_excluded_before_it_closes(self):
        """The pause has to apply WHILE the modal is up, not only once it
        dismisses -- otherwise the figure climbs for thirty seconds and
        then jumps backwards, which reads as a bug."""
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.set_blocked(True, 5.0)
        assert m.elapsed(25.0) == pytest.approx(5.0)

    def test_blocking_is_idempotent_in_both_directions(self):
        """Two routes drive this -- the tick's screen-stack read and the
        permission path's direct call -- and they are allowed to overlap.
        Idempotence is what makes them ONE piece of state rather than two
        that have to agree."""
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.set_blocked(True, 2.0)
        m.set_blocked(True, 3.0)            # the second observer
        m.set_blocked(False, 12.0)
        m.set_blocked(False, 13.0)          # and its release
        assert m.elapsed(20.0) == pytest.approx(10.0)

    def test_a_second_start_does_not_restart_a_running_turn(self):
        """`_busy = True` is assigned more than once on at least one path.
        Resetting the clock there would silently under-report every turn
        that took it."""
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.start(5.0)
        assert m.elapsed(10.0) == pytest.approx(10.0)

    def test_stop_returns_the_active_seconds_and_ends_the_turn(self):
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.set_blocked(True, 1.0)
        m.set_blocked(False, 6.0)
        assert m.stop(11.0) == pytest.approx(6.0)
        assert m.running is False
        assert m.elapsed(99.0) == 0.0

    def test_stop_returns_none_when_no_turn_was_running(self):
        """NONE, not 0.0. `_busy` is cleared on paths where it was never
        set, and the caller has to tell "nothing ran" from "it was
        instant" -- one of those gets a transcript line and the other must
        not."""
        assert TurnMeter().stop(10.0) is None

    def test_a_backwards_clock_reports_zero_rather_than_raising(self):
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(10.0)
        assert m.elapsed(4.0) == 0.0

    def test_beginning_the_session_twice_keeps_the_first_answer(self):
        m = TurnMeter()
        m.begin_session(100.0)
        m.begin_session(500.0)
        assert m.subtitle(160.0) == "up 1m"


# ---------------------------------------------------------------------------
# ---- The live estimate ----------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheLiveRateIsAnEstimateAndSaysSo:

    def test_the_rate_divides_characters_by_the_active_seconds(self):
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.add_output_chars(400)             # 100 tokens at CHARS_PER_TOKEN
        assert m.rate(10.0) == pytest.approx(10.0)

    def test_the_rate_ignores_time_spent_waiting_on_a_human(self):
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.add_output_chars(400)
        m.set_blocked(True, 10.0)
        m.set_blocked(False, 40.0)
        # Ten active seconds, not forty.
        assert m.rate(40.0) == pytest.approx(10.0)

    def test_no_rate_before_the_floor_because_it_would_be_noise(self):
        """The first delta of a turn divided by a hundredth of a second
        renders thousands of tokens a second and then collapses, which
        reads as a bug rather than as a measurement settling."""
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.add_output_chars(40)
        assert m.rate(0.01) is None
        assert m.rate(meters.RATE_FLOOR_S) is not None

    def test_no_rate_before_any_output_has_arrived(self):
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        assert m.rate(5.0) is None

    def test_characters_arriving_outside_a_turn_are_not_counted(self):
        m = TurnMeter()
        m.begin_session(0.0)
        m.add_output_chars(4000)
        m.start(0.0)
        assert m.rate(10.0) is None

    def test_each_turn_starts_its_own_count(self):
        """A rate carrying the previous turn's characters would make the
        second turn of a session look twice as fast as it was."""
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.add_output_chars(4000)
        m.stop(10.0)
        m.start(10.0)
        m.add_output_chars(400)
        assert m.rate(20.0) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# ---- What lands on the border ---------------------------------------------
# ---------------------------------------------------------------------------

class TestTheSubtitleSaysOnlyWhatItKnows:

    def test_nothing_at_all_before_the_session_is_on_screen(self):
        """None rather than "", so a bare-built app renders the border it
        always rendered."""
        assert TurnMeter().subtitle(10.0) is None

    def test_an_idle_session_shows_uptime_alone(self):
        m = TurnMeter()
        m.begin_session(0.0)
        assert m.subtitle(300.0) == "up 5m"

    def test_a_running_turn_shows_all_three(self):
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(288.0)
        m.add_output_chars(4 * 34 * 12)
        assert m.subtitle(300.0) == "12.0s  ~34 tok/s  up 5m"

    def test_the_rate_is_absent_until_it_means_something(self):
        """Elapsed and uptime still show. A turn that has produced nothing
        yet is exactly when a reader most wants to see the seconds
        moving."""
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(295.0)
        assert m.subtitle(300.0) == "5.0s  up 5m"

    def test_the_estimate_is_marked_as_one(self):
        """The tilde is CHARS_PER_TOKEN, admitted. The completion line
        carries no tilde because it divides the provider's own count."""
        m = TurnMeter()
        m.begin_session(0.0)
        m.start(0.0)
        m.add_output_chars(400)
        assert "~" in m.subtitle(10.0)


# ---------------------------------------------------------------------------
# ---- The line under the answer --------------------------------------------
# ---------------------------------------------------------------------------

class TestTheCompletionLineNeverInventsACount:

    def test_the_time_alone_when_the_provider_reports_no_usage(self):
        """Sixteen of the nineteen configured providers report no usage on
        a streaming call (D21). A zero there would say the model wrote
        nothing, which is a claim, not an absence."""
        assert TurnMeter().completion(12.4, None) == "took 12.4s"

    def test_a_zero_count_is_treated_as_no_count(self):
        assert TurnMeter().completion(12.4, 0) == "took 12.4s"

    def test_the_exact_count_and_rate_when_the_provider_reports_them(self):
        assert TurnMeter().completion(12.0, 418) == \
            "took 12.0s · 418 tokens out · 35 tok/s"

    def test_the_count_is_grouped_for_reading(self):
        assert "12,345 tokens out" in TurnMeter().completion(60.0, 12345)

    def test_no_rate_below_the_floor_but_the_count_survives(self):
        """An instant turn still has a real token count; only the division
        is refused."""
        line = TurnMeter().completion(0.1, 40)
        assert line == "took 0.1s · 40 tokens out"

    def test_the_completion_rate_carries_no_tilde(self):
        """It is a different instrument from the live figure: the
        provider's own count over the measured time, not characters over a
        constant."""
        assert "~" not in TurnMeter().completion(12.0, 418)
