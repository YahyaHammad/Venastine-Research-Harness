"""
core/usage_view.py

What `/usage` shows, for either shell (ROADMAP_v3 §97, UF4 and UF5).

FOUR ROWS, EACH THE PROVIDER'S OWN COUNT. Input, cache read, cache write
and output, summed over this thread's model calls since it was opened --
the span the TUI's usage line already gives `billed`
(`ConversationMemory.record_billed`), so the two figures cannot disagree
about what they cover. Nothing here is estimated, priced or carried
across a restart: a figure the provider did not send is not a zero.

"NOT REPORTED" IS A ROW'S ANSWER, NOT A ZERO. `core/client.py` puts a
cache count in a response's usage only when the provider sent one, and
names on `ModelResponse.usage_unreported` the input or output count that
`_stream_usage` had to coerce to 0 for the loop's arithmetic (D21). A
call that did not report a row is not counted for it, and a row reported
on some calls says on how many: the batch-61 rule that the completion line
makes no token claim where none was measured, applied to four figures.

PURE: no `core/` import, no storage, no clock. `ConversationMemory` holds
a `UsageTally` for the TUI, which keeps one memory across turns; the CLI
builds a memory per turn, so `RunAgentLoop.run_agent_conversation` and
`wake_conversation` hand theirs back on the response and the CLI's
`ChatSession` adds them up. Both shells then print `report_lines`.
"""

from __future__ import annotations

#: The rows, in the order `/usage` prints them, as (usage key, label).
#: The keys are `ModelResponse.usage`'s; the labels are the owner's
#: choice (§97 UF4).
ROWS = (
    ("input_tokens", "input"),
    ("cache_read_tokens", "cache read"),
    ("cache_write_tokens", "cache write"),
    ("output_tokens", "output"),
)

#: The two rows that are PART OF input on every provider since §97 UF5:
#: `core/client.py` adds them into input where a provider reports them
#: apart, so the report says so rather than inviting a sum.
CACHE_KEYS = ("cache_read_tokens", "cache_write_tokens")


def _count(value):
    """`value` as a token count, or None. `_token_count`'s rule: a bool is
    not a count and neither is a negative number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


class UsageTally:
    """Provider-reported counts over a run of model calls.

    `calls` is every call recorded, reported or not, because "reported on
    3 of 5 calls" needs both numbers. `totals` and `reported` hold a key
    only once some call reported it, so an absent key IS "not reported".
    """

    def __init__(self):
        self.calls = 0
        self.totals: dict = {}
        self.reported: dict = {}

    def add(self, usage, unreported=()) -> None:
        """One model call's usage dict, and the keys in it that the
        provider did not send (`ModelResponse.usage_unreported`)."""
        self.calls += 1
        usage = usage if isinstance(usage, dict) else {}
        for key, _label in ROWS:
            if key in unreported:
                continue
            value = _count(usage.get(key))
            if value is None:
                continue
            self.totals[key] = self.totals.get(key, 0) + value
            self.reported[key] = self.reported.get(key, 0) + 1

    def merge(self, other) -> None:
        """Another tally's calls, added to this one's. A None or a
        non-tally is nothing to add: a response from a path that does not
        set one (a test double, an older caller) carries none."""
        if not isinstance(other, UsageTally):
            return
        self.calls += other.calls
        for key, value in other.totals.items():
            self.totals[key] = self.totals.get(key, 0) + value
        for key, value in other.reported.items():
            self.reported[key] = self.reported.get(key, 0) + value


def report_lines(tally) -> list:
    """The lines `/usage` prints, in either shell."""
    if not isinstance(tally, UsageTally):
        tally = UsageTally()
    head = "usage · this thread since it was opened"
    if not tally.calls:
        return [head + " · no model calls yet"]
    calls = "1 call" if tally.calls == 1 else f"{tally.calls:,} calls"
    lines = [f"{head} · {calls}"]
    figures = {key: format(tally.totals[key], ",")
               for key, _label in ROWS if tally.reported.get(key)}
    width = max((len(text) for text in figures.values()), default=0)
    label_width = max(len(label) for _key, label in ROWS)
    for key, label in ROWS:
        name = label.ljust(label_width)
        if key not in figures:
            lines.append(f"  {name}  not reported")
            continue
        line = f"  {name}  {figures[key].rjust(width)}"
        seen = tally.reported[key]
        if seen < tally.calls:
            line += f"   (reported on {seen:,} of {tally.calls:,} calls)"
        lines.append(line)
    if any(key in figures for key in CACHE_KEYS):
        lines.append("  cache read and cache write are part of input")
    return lines
