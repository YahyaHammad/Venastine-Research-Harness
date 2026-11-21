"""
test_usage_view.py

ROADMAP_v3 §97 (UF4-UF9): what `/usage` shows, driven directly.

`core/usage_view.py` is pure, so every case here is dicts in and lines
out, or a ContextVar entered and read back. That the loop records into it,
that a spawn, a pass and a harness run each land in their own section, and
that both shells print it is tested beside the loop
(`test_spend_and_size.py`). This file holds the rules: a count the
provider did not send is never shown as a number, a sum is never taken
across models, and a call is counted under the source that started it.
"""

import threading
from contextvars import copy_context

import pytest

from core import usage_view
from core.usage_view import (
    HARNESS,
    RESEARCH,
    SUBAGENTS,
    THREAD,
    UsageLedger,
    UsageTally,
    counting,
    record,
    report_lines,
    started,
)


def _tally(*calls):
    tally = UsageTally()
    for usage, unreported in calls:
        tally.add(usage, unreported)
    return tally


def _ledger(*calls):
    """(source, provider, model, usage, unreported) per call."""
    ledger = UsageLedger()
    for source, provider, model, usage, unreported in calls:
        ledger.add(source, provider, model, usage, unreported)
    return ledger


def _one(usage, unreported=(), source=THREAD, provider="ANTHROPIC",
         model="model-a"):
    return _ledger((source, provider, model, usage, unreported))


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
        tally = _tally(({"input_tokens": 10}, ()))
        tally.merge(None)
        tally.merge(object())
        assert tally.calls == 1 and tally.totals == {"input_tokens": 10}


class TestTheLedgerKeepsModelsApart:
    """UF7. Models are billed at different rates, so a block is one
    (provider, model) and nothing adds across two of them."""

    def test_one_model_is_one_tally_however_many_calls(self):
        ledger = _ledger(
            (THREAD, "ANTHROPIC", "model-a", {"input_tokens": 10}, ()),
            (THREAD, "ANTHROPIC", "model-a", {"input_tokens": 20}, ()))
        sections, _runs = ledger.snapshot()
        [(pair, tally)] = sections[THREAD]
        assert pair == ("ANTHROPIC", "model-a")
        assert tally.calls == 2 and tally.totals == {"input_tokens": 30}

    def test_the_same_model_name_on_two_providers_is_two_blocks(self):
        """The provider is half the price list: one model id served by
        two endpoints is billed by each at its own rate."""
        ledger = _ledger(
            (THREAD, "OPENROUTER", "model-a", {"input_tokens": 10}, ()),
            (THREAD, "LOCAL", "model-a", {"input_tokens": 20}, ()))
        sections, _runs = ledger.snapshot()
        assert [pair for pair, _ in sections[THREAD]] == [
            ("OPENROUTER", "model-a"), ("LOCAL", "model-a")]

    def test_the_same_model_in_two_sources_is_counted_in_each(self):
        ledger = _ledger(
            (THREAD, "ANTHROPIC", "model-a", {"input_tokens": 10}, ()),
            (SUBAGENTS, "ANTHROPIC", "model-a", {"input_tokens": 20}, ()))
        sections, _runs = ledger.snapshot()
        assert sections[THREAD][0][1].totals == {"input_tokens": 10}
        assert sections[SUBAGENTS][0][1].totals == {"input_tokens": 20}

    def test_a_snapshot_is_a_copy(self):
        """A reader gets copies: a background child recording while
        /usage prints must not change the figures mid-report."""
        ledger = _one({"input_tokens": 10})
        sections, runs = ledger.snapshot()
        sections[THREAD][0][1].add({"input_tokens": 99})
        runs[RESEARCH] = 5
        again, again_runs = ledger.snapshot()
        assert again[THREAD][0][1].totals == {"input_tokens": 10}
        assert again_runs == {}

    def test_calls_counts_every_block(self):
        ledger = _ledger(
            (THREAD, "A", "m", None, ()),
            (RESEARCH, "B", "n", None, ()),
            (HARNESS, "A", "m", None, ()))
        assert ledger.calls == 3

    def test_concurrent_writers_lose_nothing(self):
        """Background children and the parallel tool pool write from
        their own threads. Every add lands."""
        ledger = UsageLedger()
        go = threading.Barrier(8)

        def write(n):
            go.wait()
            for _ in range(500):
                ledger.add(SUBAGENTS, "A", f"m{n % 2}",
                           {"input_tokens": 1}, ())

        workers = [threading.Thread(target=write, args=(n,))
                   for n in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        sections, _runs = ledger.snapshot()
        assert ledger.calls == 4000
        assert sum(t.totals["input_tokens"]
                   for _pair, t in sections[SUBAGENTS]) == 4000


class TestTheScope:
    """UF8 and UF9. A call is recorded only where a shell is counting,
    and under the source of what started it."""

    def test_nothing_is_recorded_where_no_shell_is_counting(self):
        """`--mode research`, a test calling the loop: nobody reads it."""
        record("A", "m", {"input_tokens": 5})
        usage_view.count_run(RESEARCH)   # and this does not raise

    def test_a_threads_own_call_is_this_thread(self):
        ledger = UsageLedger()
        with counting(ledger):
            record("A", "m", {"input_tokens": 5})
        assert list(ledger.snapshot()[0]) == [THREAD]

    def test_a_research_pass_is_told_apart_by_its_thread(self):
        ledger = UsageLedger()
        with counting(ledger):
            record("A", "m", {"input_tokens": 5}, research=True)
        assert list(ledger.snapshot()[0]) == [RESEARCH]

    def test_what_a_thread_starts_takes_the_started_source(self):
        ledger = UsageLedger()
        with counting(ledger), started(SUBAGENTS):
            record("A", "m", {"input_tokens": 5})
        assert list(ledger.snapshot()[0]) == [SUBAGENTS]

    def test_a_subagents_own_subagent_is_still_a_subagent(self):
        ledger = UsageLedger()
        with counting(ledger), started(SUBAGENTS), started(SUBAGENTS):
            record("A", "m", {"input_tokens": 5})
        assert list(ledger.snapshot()[0]) == [SUBAGENTS]

    def test_a_subagents_compaction_is_the_harnesss(self):
        """Harness runs go under their own heading wherever they run --
        the owner was told compaction is told apart from subagents."""
        ledger = UsageLedger()
        with counting(ledger), started(SUBAGENTS), started(HARNESS):
            record("A", "m", {"input_tokens": 5})
        assert list(ledger.snapshot()[0]) == [HARNESS]

    def test_a_subagent_a_harness_run_starts_is_the_harnesss(self):
        ledger = UsageLedger()
        with counting(ledger), started(HARNESS), started(SUBAGENTS):
            record("A", "m", {"input_tokens": 5})
        assert list(ledger.snapshot()[0]) == [HARNESS]

    def test_a_subagent_a_pass_spawns_is_a_subagent(self):
        """Research is read from the thread, never set, so a pass's own
        spawn is a subagent like any other."""
        ledger = UsageLedger()
        with counting(ledger), started(SUBAGENTS):
            record("A", "m", {"input_tokens": 5}, research=True)
        assert list(ledger.snapshot()[0]) == [SUBAGENTS]

    def test_leaving_a_source_restores_the_one_before(self):
        ledger = UsageLedger()
        with counting(ledger):
            with started(SUBAGENTS):
                record("A", "m", {"input_tokens": 1})
            record("A", "m", {"input_tokens": 2})
        sections, _runs = ledger.snapshot()
        assert sections[THREAD][0][1].totals == {"input_tokens": 2}
        assert sections[SUBAGENTS][0][1].totals == {"input_tokens": 1}

    def test_a_raise_inside_a_source_restores_it_too(self):
        ledger = UsageLedger()
        with counting(ledger):
            with pytest.raises(RuntimeError), started(SUBAGENTS):
                raise RuntimeError("child failed")
            record("A", "m", {"input_tokens": 2})
        assert list(ledger.snapshot()[0]) == [THREAD]

    def test_started_works_as_a_decorator_on_every_call(self):
        @started(HARNESS)
        def compactor():
            record("A", "m", {"input_tokens": 1})

        ledger = UsageLedger()
        with counting(ledger):
            compactor()
            compactor()
        sections, _runs = ledger.snapshot()
        assert sections[HARNESS][0][1].calls == 2

    def test_an_unknown_source_is_refused(self):
        with pytest.raises(ValueError):
            with started("plugins"):
                pass

    def test_counting_nothing_counts_nothing(self):
        with counting(None):
            record("A", "m", {"input_tokens": 1})

    def test_a_run_count_lands_on_the_ledger_being_counted(self):
        ledger = UsageLedger()
        with counting(ledger):
            usage_view.count_run(RESEARCH)
        assert ledger.snapshot()[1] == {RESEARCH: 1}

    def test_a_copied_context_carries_the_ledger_to_another_thread(self):
        """The route a background child and the parallel tool pool take:
        `copy_context()` at the spawn, so the session's thread records
        into the ledger of the thread that spawned it."""
        ledger = UsageLedger()
        with counting(ledger), started(SUBAGENTS):
            context = copy_context()
        worker = threading.Thread(
            target=context.run,
            args=(record, "A", "m", {"input_tokens": 3}))
        worker.start()
        worker.join()
        assert list(ledger.snapshot()[0]) == [SUBAGENTS]

    def test_a_bare_thread_does_not_inherit_it(self):
        """Why the TUI enters the ledger in `run_worker` rather than
        once on the UI thread: a thread starts with an empty context."""
        ledger = UsageLedger()
        with counting(ledger):
            worker = threading.Thread(
                target=record, args=("A", "m", {"input_tokens": 3}))
            worker.start()
            worker.join()
        assert ledger.calls == 0


class TestTheReport:

    def test_no_calls_says_so(self):
        assert report_lines(UsageLedger()) == [
            "usage · this thread since it was opened · no model calls yet"]

    def test_no_ledger_at_all_reads_as_no_calls(self):
        assert report_lines(None) == report_lines(UsageLedger())

    def test_one_model_on_this_thread(self):
        lines = report_lines(_one(
            {"input_tokens": 412_880, "output_tokens": 18_207,
             "cache_read_tokens": 301_440, "cache_write_tokens": 2_048}))
        assert lines == [
            "usage · this thread since it was opened · 1 call",
            "this thread · 1 call",
            "  ANTHROPIC model-a",
            "    input        412,880",
            "    cache read   301,440",
            "    cache write    2,048",
            "    output        18,207",
            "  cache read and cache write are part of input",
        ]

    def test_the_owners_layout_by_source_then_model(self):
        """UF7, the preview the owner picked: a section per source, a
        block per model, the research heading counting its runs."""
        ledger = _ledger(
            (THREAD, "PROVIDER_A", "model-a",
             {"input_tokens": 120_400, "output_tokens": 8_112}, ()),
            (SUBAGENTS, "PROVIDER_A", "model-b",
             {"input_tokens": 61_020, "output_tokens": 900}, ()),
            (RESEARCH, "PROVIDER_A", "model-a",
             {"input_tokens": 412_880, "output_tokens": 18_000}, ()),
            (RESEARCH, "PROVIDER_A", "model-a",
             {"input_tokens": 1, "output_tokens": 1}, ()))
        ledger.count_run(RESEARCH)
        lines = report_lines(ledger)
        assert lines[0] == "usage · this thread since it was opened · 4 calls"
        assert [line for line in lines if not line.startswith(" ")] == [
            lines[0],
            "this thread · 1 call",
            "subagents · 1 call",
            "research passes · 1 run, 2 calls",
        ]
        assert lines[lines.index("subagents · 1 call") + 1] == (
            "  PROVIDER_A model-b")
        assert "    input        412,881" in lines

    def test_sections_keep_their_order_whatever_was_used_first(self):
        ledger = _ledger(
            (HARNESS, "A", "m", {"input_tokens": 1}, ()),
            (RESEARCH, "A", "m", {"input_tokens": 1}, ()),
            (SUBAGENTS, "A", "m", {"input_tokens": 1}, ()),
            (THREAD, "A", "m", {"input_tokens": 1}, ()))
        heads = [line for line in report_lines(ledger)[1:]
                 if not line.startswith(" ")]
        assert heads == ["this thread · 1 call", "subagents · 1 call",
                         "research passes · 1 call", "harness runs · 1 call"]

    def test_a_section_with_two_models_says_how_many_calls_each(self):
        ledger = _ledger(
            (THREAD, "A", "model-a", {"input_tokens": 5}, ()),
            (THREAD, "A", "model-a", {"input_tokens": 5}, ()),
            (THREAD, "B", "model-b", {"input_tokens": 7}, ()))
        lines = report_lines(ledger)
        assert "  A model-a · 2 calls" in lines
        assert "  B model-b · 1 call" in lines

    def test_no_row_sums_two_models(self):
        """THE RULE UF7 adds. Two models' inputs never appear as one
        figure, at any level of the report."""
        ledger = _ledger(
            (THREAD, "A", "model-a", {"input_tokens": 300}, ()),
            (THREAD, "B", "model-b", {"input_tokens": 400}, ()))
        assert not any("700" in line for line in report_lines(ledger))

    def test_a_model_nobody_named_is_still_a_block(self):
        lines = report_lines(_one({"input_tokens": 1}, provider="",
                                  model=""))
        assert "  model not named" in lines

    def test_a_row_nobody_reported_says_not_reported(self):
        lines = report_lines(_one({"input_tokens": 50, "output_tokens": 5}))
        assert "    cache read   not reported" in lines
        assert "    cache write  not reported" in lines
        assert not any("part of input" in line for line in lines)

    def test_the_cache_note_is_written_once(self):
        ledger = _ledger(
            (THREAD, "A", "m", {"input_tokens": 9, "cache_read_tokens": 4}, ()),
            (HARNESS, "A", "m", {"input_tokens": 9, "cache_read_tokens": 4}, ()))
        lines = report_lines(ledger)
        assert lines.count("  cache read and cache write are part of input") == 1
        assert lines[-1] == "  cache read and cache write are part of input"

    def test_a_row_reported_on_some_calls_says_on_how_many(self):
        ledger = UsageLedger()
        for usage in ({"input_tokens": 50, "output_tokens": 5,
                       "cache_read_tokens": 30},
                      {"input_tokens": 70, "output_tokens": 6},
                      {"input_tokens": 90, "output_tokens": 7,
                       "cache_read_tokens": 40}):
            ledger.add(THREAD, "A", "m", usage, ())
        lines = report_lines(ledger)
        assert lines[0] == "usage · this thread since it was opened · 3 calls"
        # Right-aligned to the widest figure, so the column reads down.
        assert "    cache read    70   (reported on 2 of 3 calls)" in lines
        assert "    input        210" in lines

    def test_reported_on_counts_the_blocks_own_calls(self):
        """A block's "of M calls" is that model's, not the section's."""
        ledger = _ledger(
            (THREAD, "A", "model-a", {"input_tokens": 5,
                                      "cache_read_tokens": 1}, ()),
            (THREAD, "A", "model-a", {"input_tokens": 5}, ()),
            (THREAD, "B", "model-b", {"input_tokens": 5}, ()))
        lines = report_lines(ledger)
        # Aligned to the block's widest figure, input's 10.
        assert "    cache read    1   (reported on 1 of 2 calls)" in lines

    def test_a_provider_that_reports_nothing_shows_no_zeros(self):
        """THE RULE. A provider that streams no usage has its counts
        coerced to 0 for the loop; the report must not print them."""
        lines = report_lines(_one({"input_tokens": 0, "output_tokens": 0},
                                  ("input_tokens", "output_tokens")))
        assert lines[3:] == ["    input        not reported",
                             "    cache read   not reported",
                             "    cache write  not reported",
                             "    output       not reported"]

    def test_several_research_runs_are_counted(self):
        ledger = _one({"input_tokens": 1}, source=RESEARCH)
        ledger.count_run(RESEARCH)
        ledger.count_run(RESEARCH)
        assert "research passes · 2 runs, 1 call" in report_lines(ledger)
