"""
test_usage_view.py

ROADMAP_v3 §97 (UF4): what `/usage` shows, driven directly.

`core/usage_view.py` is pure, so every case here is a dict in and lines
out. That the loop feeds it and both shells print it is tested beside
the loop (`test_spend_and_size.py`), the TUI (`test_tui.py`) and the CLI
(`test_cli.py`); this file holds the one rule that matters most: a count
the provider did not send is never shown as a number.
"""

from core.usage_view import UsageTally, report_lines


def _tally(*calls):
    tally = UsageTally()
    for usage, unreported in calls:
        tally.add(usage, unreported)
    return tally


class TestOnlyReportedCountsAreAdded:

    def test_each_row_sums_its_own_reports(self):
        tally = _tally(({"input_tokens": 100, "output_tokens": 5,
                         "cache_read_tokens": 60}, ()),
                       ({"input_tokens": 200, "output_tokens": 7,
                         "cache_read_tokens": 90}, ()))
        assert tally.calls == 2
        assert tally.totals == {"input_tokens": 300, "output_tokens": 12,
                                "cache_read_tokens": 150}

    def test_a_coerced_zero_is_not_a_report(self):
        """`ModelResponse.usage_unreported` names the counts D21 made a 0
        of. They are in the dict and must not be added or counted."""
        tally = _tally(({"input_tokens": 0, "output_tokens": 0},
                        ("input_tokens", "output_tokens")))
        assert tally.calls == 1
        assert tally.totals == {} and tally.reported == {}

    def test_a_reported_zero_is_a_report(self):
        tally = _tally(({"input_tokens": 0, "output_tokens": 0}, ()))
        assert tally.reported == {"input_tokens": 1, "output_tokens": 1}

    def test_what_is_not_a_count_is_not_added(self):
        tally = _tally(({"input_tokens": True, "output_tokens": -3,
                         "cache_read_tokens": "12"}, ()))
        assert tally.totals == {}

    def test_a_key_outside_the_four_rows_is_ignored(self):
        tally = _tally(({"input_tokens": 4, "reasoning_tokens": 9}, ()))
        assert tally.totals == {"input_tokens": 4}

    def test_a_missing_usage_dict_is_a_call_with_nothing_reported(self):
        tally = _tally((None, ()))
        assert tally.calls == 1 and tally.totals == {}

    def test_merge_adds_calls_totals_and_reports(self):
        first = _tally(({"input_tokens": 10, "output_tokens": 1}, ()))
        second = _tally(({"input_tokens": 20, "output_tokens": 2,
                          "cache_write_tokens": 5}, ()))
        first.merge(second)
        assert first.calls == 2
        assert first.totals == {"input_tokens": 30, "output_tokens": 3,
                                "cache_write_tokens": 5}
        assert first.reported["cache_write_tokens"] == 1

    def test_merging_nothing_changes_nothing(self):
        """A response from a path that sets no tally carries None."""
        tally = _tally(({"input_tokens": 10}, ()))
        tally.merge(None)
        tally.merge(object())
        assert tally.calls == 1 and tally.totals == {"input_tokens": 10}


class TestTheReport:

    def test_no_calls_says_so(self):
        assert report_lines(UsageTally()) == [
            "usage · this thread since it was opened · no model calls yet"]

    def test_no_tally_at_all_reads_as_no_calls(self):
        assert report_lines(None) == report_lines(UsageTally())

    def test_the_four_rows_in_order_with_their_names(self):
        lines = report_lines(_tally(
            ({"input_tokens": 412_880, "output_tokens": 18_207,
              "cache_read_tokens": 301_440, "cache_write_tokens": 2_048},
             ())))
        assert lines == [
            "usage · this thread since it was opened · 1 call",
            "  input        412,880",
            "  cache read   301,440",
            "  cache write    2,048",
            "  output        18,207",
            "  cache read and cache write are part of input",
        ]

    def test_a_row_nobody_reported_says_not_reported(self):
        lines = report_lines(_tally(
            ({"input_tokens": 50, "output_tokens": 5}, ())))
        assert "  cache read   not reported" in lines
        assert "  cache write  not reported" in lines
        # And no footnote about cache rows that have no figure.
        assert not any("part of input" in line for line in lines)

    def test_a_row_reported_on_some_calls_says_on_how_many(self):
        lines = report_lines(_tally(
            ({"input_tokens": 50, "output_tokens": 5,
              "cache_read_tokens": 30}, ()),
            ({"input_tokens": 70, "output_tokens": 6}, ()),
            ({"input_tokens": 90, "output_tokens": 7,
              "cache_read_tokens": 40}, ())))
        assert lines[0] == "usage · this thread since it was opened · 3 calls"
        # Right-aligned to the widest figure, so the column reads down.
        assert "  cache read    70   (reported on 2 of 3 calls)" in lines
        assert "  input        210" in lines

    def test_a_provider_that_reports_nothing_shows_no_zeros(self):
        """THE RULE. A provider that streams no usage has its counts
        coerced to 0 for the loop; the report must not print them."""
        lines = report_lines(_tally(
            ({"input_tokens": 0, "output_tokens": 0},
             ("input_tokens", "output_tokens"))))
        assert lines[1:] == ["  input        not reported",
                             "  cache read   not reported",
                             "  cache write  not reported",
                             "  output       not reported"]
