"""
test_fake_storage_mirror.py

Issue #123. `tests/conftest.py`'s `FakeStorage` carries two reads whose
docstrings say, in capitals, that they mirror `storage.py` -- and nothing
compared them to `storage.py`. CLAUDE.md states the same thing as a project
convention:

    Verify against production code, not the test double. ... If you change
    storage.py's reconstruction, change the fake identically.

Unit 15 mutated the fake and the full suite stayed green in both directions:

    _reconstruct's assistant branch emitting "tool_calls": []  -> 1406 passed
    _split_at returning len(rows) for an unfound watermark     -> 1406 passed

The second is the sharper one, because production's `_split_at` has the
identical property, the identical reasoning in its docstring, AND a test
(`test_storage_reads.py`) -- so the rule was enforced on one side of the
mirror and merely stated on the other.

This file is the comparison. It turns a convention somebody has to remember
into a check, which is the same move `test_docs_consistency.py` made for the
test count and `test_pilot_wait.py` made for the pilot helpers.

WHY BOTH SIDES ARE CALLED DIRECTLY. `storage._to_neutral` and
`storage._split_at` are pure functions -- no Session, no database, no
dependence on the fake `sqlmodel`. So this compares the two implementations
rather than two behaviours observed through a database, and it costs
microseconds.

THE ONE DIFFERENCE THAT IS NOT A DIVERGENCE: production reads a row whose
`content` is a JSON *string* (that is what the column holds); the fake holds
already-decoded values, because nothing serialises on its write path. Every
case below is therefore built once as a logical row and encoded for each
side, so the test compares reconstruction and not serialisation.
"""

import json
from uuid import uuid4

import pytest

import storage
from tests.conftest import FakeStorage


# ---------------------------------------------------------------------------
# ---- Rows that cover every branch of both implementations ------------------
# ---------------------------------------------------------------------------

def _logical_rows():
    """One row per branch, plus the combinations that have bitten before.

    The assistant-with-tool_calls row is the one the resume-shape bug lived
    in (DEVLOG §3/§4): the fake and the fix diverged and nothing flagged it.
    """
    return [
        # plain user turn
        {"role": "user", "content": "first question",
         "name": None, "tool_call_id": None},
        # assistant, no tool calls -- the "text only" shape
        {"role": "assistant", "content": {"text": "an answer", "tool_calls": []},
         "name": None, "tool_call_id": None},
        # assistant WITH tool calls -- the shape #123's mutation emptied
        {"role": "assistant",
         "content": {"text": "", "tool_calls": [
             {"id": "call_1", "name": "get_time", "input": {}},
             {"id": "call_2", "name": "web_search", "input": {"query": "x"}},
         ]},
         "name": None, "tool_call_id": None},
        # tool result -- carries tool_call_id from the COLUMN, not the payload
        {"role": "tool", "content": "2026-08-15T00:00:00Z",
         "name": "get_time", "tool_call_id": "call_1"},
        # a named user row: `name` is appended by both, for any role
        {"role": "user", "content": "named turn",
         "name": "someone", "tool_call_id": None},
        # a user row carrying a tool_call_id: the `else` branch's conditional
        # copy, which is the half of the canonical bug that survived longest
        {"role": "user", "content": "carries an id",
         "name": None, "tool_call_id": "call_2"},
        # an assistant row with neither key present in the decoded payload,
        # exercising both .get() defaults
        {"role": "assistant", "content": {},
         "name": None, "tool_call_id": None},
    ]


def _for_production(logical):
    """storage._to_neutral's input: content is the JSON string the column
    holds, and every key is present (SQL columns are never absent)."""
    return {
        "role": logical["role"],
        "content": json.dumps(logical["content"]),
        "name": logical["name"],
        "tool_call_id": logical["tool_call_id"],
    }


def _for_fake(logical, row_id=None, pinned=False):
    """FakeStorage._reconstruct's input: content already decoded, plus the
    id/pinned fields the fake's slicing helpers read."""
    return {
        "id": row_id if row_id is not None else uuid4(),
        "role": logical["role"],
        "content": logical["content"],
        "name": logical["name"],
        "tool_call_id": logical["tool_call_id"],
        "pinned": pinned,
    }


# ---------------------------------------------------------------------------
# ---- 1. The reconstruction --------------------------------------------------
# ---------------------------------------------------------------------------

class TestReconstructionMirrorsProduction:

    @pytest.mark.parametrize("logical", _logical_rows(),
                             ids=lambda r: f"{r['role']}"
                                           f"{'+name' if r['name'] else ''}"
                                           f"{'+tcid' if r['tool_call_id'] else ''}")
    def test_one_row_reconstructs_identically(self, logical):
        """THE CHECK #123 ASKS FOR, per branch rather than in aggregate, so a
        failure names the shape that diverged instead of just saying the
        lists differ."""
        real = storage._to_neutral(_for_production(logical))
        fake = FakeStorage()._reconstruct([_for_fake(logical)])[0]
        assert fake == real, (
            f"FakeStorage._reconstruct and storage._to_neutral disagree for a "
            f"{logical['role']!r} row.\n  production: {real}\n  fake:       {fake}\n"
            "The fake's docstring says it mirrors storage.py; CLAUDE.md makes "
            "that a project convention. Change both or neither.")

    def test_a_whole_thread_reconstructs_identically(self):
        """Aggregate as well as per-row: ORDER and LENGTH are part of the
        contract, and a per-row loop cannot see either."""
        logical = _logical_rows()
        real = [storage._to_neutral(_for_production(r)) for r in logical]
        fake = FakeStorage()._reconstruct([_for_fake(r) for r in logical])
        assert fake == real

    def test_the_assistant_branch_carries_the_tool_calls_through(self):
        """Named for the mutation that survived: emitting a constant [] for
        tool_calls left all 1406 tests green. The parametrised case above
        covers it; this one exists so the failure says WHAT was lost, since
        this is the divergence that actually shipped once."""
        logical = _logical_rows()[2]
        fake = FakeStorage()._reconstruct([_for_fake(logical)])[0]
        assert fake["tool_calls"] == logical["content"]["tool_calls"]
        assert [c["id"] for c in fake["tool_calls"]] == ["call_1", "call_2"]


# ---------------------------------------------------------------------------
# ---- 2. The watermark split -------------------------------------------------
# ---------------------------------------------------------------------------

class TestSplitAtMirrorsProduction:
    """`_split_at`'s unfound-watermark answer is 0, and the 0 is the whole
    point: the caller shows the WHOLE thread rather than silently hiding a
    prefix on the strength of an id it could not find. Returning len(rows)
    would hide everything, and it left the suite green."""

    def _rows(self):
        ids = [uuid4() for _ in range(4)]
        logical = _logical_rows()[:4]
        return ids, [_for_fake(r, row_id=i) for r, i in zip(logical, ids)]

    def test_none_is_zero_on_both_sides(self):
        ids, rows = self._rows()
        assert FakeStorage()._split_at(rows, None) == storage._split_at(rows, None) == 0

    @pytest.mark.parametrize("index", [0, 1, 2, 3])
    def test_a_known_watermark_agrees(self, index):
        ids, rows = self._rows()
        assert (FakeStorage()._split_at(rows, ids[index])
                == storage._split_at(rows, ids[index])
                == index + 1)

    def test_an_unknown_watermark_is_zero_on_both_sides(self):
        """The mutation that survived. `len(rows)` here means an id the
        thread does not contain hides the entire thread."""
        ids, rows = self._rows()
        stranger = uuid4()
        fake = FakeStorage()._split_at(rows, stranger)
        real = storage._split_at(rows, stranger)
        assert fake == real == 0, (
            "An unrecognised watermark must resolve to 0 -- show the whole "
            "thread -- on both sides of the mirror. Anything else hides a "
            "prefix because of an id that could not be found.")

    def test_an_empty_thread_is_zero_on_both_sides(self):
        assert FakeStorage()._split_at([], uuid4()) == storage._split_at([], uuid4()) == 0
