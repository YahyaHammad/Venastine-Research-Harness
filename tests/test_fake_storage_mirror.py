"""
test_fake_storage_mirror.py

Issue #123. `tests/conftest.py`'s `FakeStorage` carries two reads whose
docstrings say, in capitals, that they mirror `storage.py` -- and nothing
compared them to `storage.py`. AGENTS.md states the same thing as a project
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
        # ROADMAP_v3 §49 (SS5): a user row the HARNESS wrote -- the mark
        # travels in its own column, and only when present
        {"role": "user", "content": "[harness] s1 exited with code 0.",
         "name": None, "tool_call_id": None,
         "harness": {"kind": "session_wake",
                     "sessions": [{"id": "s1", "call_id": "c1",
                                   "shape": "exited"}]}},
        # ROADMAP_v3 §81 (IM2): a tool row with images in their own column,
        # one entry unusable -- both sides drop that entry and keep the rest
        {"role": "tool", "content": "Attached s.png",
         "name": None, "tool_call_id": "call_3",
         "images": [{"media_type": "image/png", "data": "iVBORw0KGgo="},
                    {"media_type": "", "data": "x"}]},
    ]


def _for_production(logical):
    """storage._to_neutral's input: content is the JSON string the column
    holds, and every key is present (SQL columns are never absent)."""
    harness = logical.get("harness")
    images = logical.get("images")
    return {
        "role": logical["role"],
        "content": json.dumps(logical["content"]),
        "name": logical["name"],
        "tool_call_id": logical["tool_call_id"],
        "harness": json.dumps(harness) if harness else None,
        "images": json.dumps(images) if images else None,
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
        "harness": logical.get("harness"),
        "images": logical.get("images"),
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
            "The fake's docstring says it mirrors storage.py; AGENTS.md makes "
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


# ---------------------------------------------------------------------------
# ---- 3. The pinned span and the turn opener (#231, batch 216) --------------
# ---------------------------------------------------------------------------
#
# The two transcriptions of a published algorithm the fake carried with
# nothing comparing them: `pinned_through`'s keep-set (#88's tool-result
# re-inclusion) and batch 120's turn opener. `pinned_through` reads the
# database, so batch 216 split its index computation into the pure
# `storage._pinned_indices`, and the fake's copy into
# `FakeStorage._pinned_indices`, and these compare the two -- the shape
# `_to_neutral` and `_split_at` already had. Rows need only `role` and
# `pinned`; the neutral rows only `tool_call_id` and `tool_calls`, so both
# sides are handed the same ones and the comparison is of the algorithm.

def _turns(*specs):
    """`(role, pinned, tool_call_ids)` -> (rows, neutral). An assistant's
    ids are the calls it makes; a tool row's single id is the one it
    answers."""
    rows, neutral = [], []
    for role, pinned, ids in specs:
        rows.append({"role": role, "pinned": pinned})
        message = {"role": role, "content": ""}
        if role == "assistant" and ids:
            message["tool_calls"] = [{"id": i, "name": "t", "input": {}}
                                     for i in ids]
        if role == "tool":
            message["tool_call_id"] = ids[0]
        neutral.append(message)
    return rows, neutral


_PINNED_CASES = {
    # A pinned assistant call whose result was not pinned: the result is
    # re-included, or the view carries an unanswered tool_use (#88).
    "re-includes the result": [
        ("user", False, ()), ("assistant", True, ("a",)),
        ("tool", False, ("a",)), ("assistant", False, ()),
        ("user", False, ())],
    # Two calls, one result pinned already and one not.
    "only what is missing": [
        ("user", False, ()), ("assistant", True, ("a", "b")),
        ("tool", True, ("a",)), ("tool", False, ("b",)),
        ("user", False, ())],
    # A result for a call that was not pinned stays out.
    "not an unrelated result": [
        ("user", True, ()), ("assistant", False, ("a",)),
        ("tool", False, ("a",)), ("user", False, ())],
    # A watermark inside a turn: the turn's request comes back verbatim.
    "the opener of a cut turn": [
        ("user", False, ()), ("assistant", False, ()),
        ("user", False, ()), ("assistant", False, ("a",)),
        ("tool", False, ("a",)), ("assistant", False, ())],
    # The opener already pinned: one copy, in archive order.
    "a pinned opener once": [
        ("user", True, ()), ("assistant", False, ("a",)),
        ("tool", False, ("a",)), ("assistant", False, ())],
    "nothing pinned, turn-aligned": [
        ("user", False, ()), ("assistant", False, ()),
        ("user", False, ()), ("assistant", False, ())],
}


class TestThePinnedSpanMirrorsProduction:

    @pytest.mark.parametrize("name", sorted(_PINNED_CASES))
    @pytest.mark.parametrize("cut", [0, 1, 2, 3, 4, 5, 6])
    def test_the_indices_agree(self, name, cut):
        rows, neutral = _turns(*_PINNED_CASES[name])
        span, part = rows[:cut], neutral[:cut]
        real = storage._pinned_indices(rows, span, part)
        assert FakeStorage()._pinned_indices(rows, span, part) == real
        assert real == sorted(set(real)), "archive order, each row once"

    def test_the_comparison_can_see_the_re_inclusion(self):
        """Reach, not a constant: the case #88 is about must come out
        with the result in it, on the production side."""
        rows, neutral = _turns(*_PINNED_CASES["re-includes the result"])
        assert storage._pinned_indices(rows, rows[:4], neutral[:4]) == [1, 2]

    def test_the_comparison_can_see_the_opener(self):
        rows, neutral = _turns(*_PINNED_CASES["the opener of a cut turn"])
        assert storage._pinned_indices(rows, rows[:4], neutral[:4]) == [2]


class TestTheTurnOpenerMirrorsProduction:
    """`_turn_opener_index` is pure on both sides, so it is compared
    directly: turn-aligned, mid-turn, an empty span, a span that is every
    row, and a kept prefix with no user row at all."""

    @pytest.mark.parametrize("roles, cut, expected", [
        (("user", "assistant", "user", "assistant"), 2, []),
        (("user", "assistant", "assistant"), 2, [0]),
        (("user", "assistant", "tool", "assistant"), 3, [0]),
        (("user", "assistant"), 0, []),
        (("user", "assistant"), 2, []),
        (("assistant", "tool", "assistant"), 2, []),
    ])
    def test_the_index_agrees(self, roles, cut, expected):
        rows = [{"role": role, "pinned": False} for role in roles]
        span = rows[:cut]
        real = storage._turn_opener_index(rows, span)
        assert FakeStorage()._turn_opener_index(rows, span) == real == expected
