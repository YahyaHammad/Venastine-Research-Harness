"""
test_storage_reads.py

ROADMAP_v2 §21: storage.py's watermark and pinned reads -- the queries the
derived "what the agent sees" view is assembled from.

These monkeypatch `_ordered_rows`, which is deliberately the one function
that touches a Session. Everything above it is slicing and filtering over
an ordered list, and that is where §21's actual rules live (M4's turn
boundaries, M9's pinned re-inclusion, AC2's exclusion from the summary).
Testing them through a database would exercise SQLAlchemy, not the rules.

The suite's fake `sqlmodel` (see the root conftest.py) makes a real Session
read return nothing anyway, so this is the seam that exists rather than a
seam invented for testing.
"""

import json
from uuid import UUID, uuid4

import pytest

import storage


# ---------------------------------------------------------------------------
# ---- A canned thread -------------------------------------------------------
# ---------------------------------------------------------------------------

def _row(role, content, pinned=False, tool_call_id=None, name=None):
    return {
        "id": uuid4(), "role": role, "content": json.dumps(content),
        "name": name, "tool_call_id": tool_call_id, "pinned": pinned,
    }


@pytest.fixture
def thread(monkeypatch):
    """Two complete turns plus the start of a third. Turn 2's assistant
    message carries a tool call and its result, which is the pairing M4
    forbids a fold boundary from splitting."""
    rows = [
        _row("user", "first question"),                                  # 0
        _row("assistant", {"text": "first answer", "tool_calls": []}),   # 1
        _row("user", "second question"),                                 # 2
        _row("assistant", {"text": "", "tool_calls": [                   # 3
            {"id": "tc1", "name": "web_search", "input": {"q": "x"}}]}),
        _row("tool", "search results", tool_call_id="tc1"),              # 4
        _row("assistant", {"text": "second answer", "tool_calls": []}),  # 5
        _row("user", "third question"),                                  # 6
    ]
    monkeypatch.setattr(storage, "_ordered_rows", lambda thread_id: rows)
    return rows


THREAD_ID = UUID("11111111-2222-3333-4444-555555555555")


# ---------------------------------------------------------------------------
# ---- The watermark ---------------------------------------------------------
# ---------------------------------------------------------------------------

def test_no_watermark_returns_the_whole_thread(thread):
    """Every pre-§21 caller passes no watermark, and their behaviour has
    to be untouched until compaction first fires on a thread."""
    history = storage.get_session_history(THREAD_ID)

    assert len(history) == len(thread)
    assert history[0] == {"role": "user", "content": "first question"}


def test_a_watermark_returns_only_the_tail_after_it(thread):
    """'covers_up_to' means inclusive: the named row is represented by the
    summary, so the tail starts after it."""
    history = storage.get_session_history(
        THREAD_ID, after_message_id=thread[2]["id"])

    assert [h["role"] for h in history] == ["assistant", "tool", "assistant", "user"]


def test_an_unrecognised_watermark_shows_everything(thread):
    """The safe direction. A watermark naming a row this thread does not
    have means something is wrong with the checkpoint -- showing the whole
    conversation costs tokens, while silently hiding a prefix on the
    strength of an id we could not find loses context the model needs and
    reports nothing."""
    history = storage.get_session_history(THREAD_ID, after_message_id=uuid4())

    assert len(history) == len(thread)


def test_archive_history_ignores_compaction_entirely(thread, monkeypatch):
    """AC1, on a thread that HAS been compacted -- which is the only state
    where the guarantee can fail.

    Comparing it against an uncompacted get_session_history() proves
    nothing: the two agree trivially. The failure mode worth pinning is
    archive_history growing the checkpoint lookup its neighbours have, so
    the read that exists to be complete quietly starts honouring a
    watermark.
    """
    checkpoint = type("_C", (), {"covers_up_to_message_id": thread[5]["id"]})()
    monkeypatch.setattr(storage, "latest_checkpoint", lambda tid: checkpoint)

    assert len(storage.archive_history(THREAD_ID)) == len(thread)
    # ...while the compacted view of that same thread really is shorter,
    # so the assertion above is about archive_history and not about the
    # checkpoint being ignored everywhere.
    assert len(storage.get_session_history(
        THREAD_ID, after_message_id=checkpoint.covers_up_to_message_id)) == 1


# ---------------------------------------------------------------------------
# ---- Pinning ---------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_summary_span_excludes_pinned_rows(thread):
    """AC2: a pinned message is never included in what the compactor is
    asked to summarize."""
    thread[1]["pinned"] = True

    span = storage.history_through(THREAD_ID, thread[5]["id"])

    assert [s["role"] for s in span] == [
        "user", "user", "assistant", "tool", "assistant"]
    assert all(s.get("text") != "first answer" for s in span)


def test_pinned_rows_inside_the_span_come_back_verbatim(thread):
    """M9. The summary covers a contiguous span but skips pinned rows, and
    one `covers_up_to_message_id` cannot say 'everything through K except
    row 1' -- so the view has to re-add them or they are simply gone."""
    thread[1]["pinned"] = True

    kept = storage.pinned_through(THREAD_ID, thread[5]["id"])

    assert kept == [{"role": "assistant", "text": "first answer", "tool_calls": []}]


def test_a_pin_after_the_watermark_is_not_re_added(thread):
    """It is already in the uncompacted tail. Returning it here too would
    put the same message in the prompt twice, growing the context that
    compaction just ran to shrink."""
    thread[6]["pinned"] = True

    assert storage.pinned_through(THREAD_ID, thread[5]["id"]) == []


def test_chaining_summarizes_only_what_followed_the_last_checkpoint(thread):
    """M2's chain strategy. Passing both bounds is what makes it cheap --
    rederive passes no lower bound and re-reads the whole span."""
    span = storage.history_through(
        THREAD_ID, thread[5]["id"], after_message_id=thread[1]["id"])

    assert [s["role"] for s in span] == [
        "user", "assistant", "tool", "assistant"]


# ---------------------------------------------------------------------------
# ---- Turn boundaries -------------------------------------------------------
# ---------------------------------------------------------------------------

def test_turn_starts_are_the_user_messages(thread):
    """M4/M5 both need this: a turn begins where a user message does, and
    a fold boundary anywhere else can separate an assistant message from
    its tool results."""
    assert storage.turn_start_ids(THREAD_ID) == [
        thread[0]["id"], thread[2]["id"], thread[6]["id"]]


def test_message_ids_from_includes_the_named_row(thread):
    """pin(last_n) resolves to a turn start and pins from there to the
    end. Excluding the named row would pin a turn without the question
    that prompted it."""
    ids = storage.message_ids_from(THREAD_ID, thread[2]["id"])

    assert ids == [r["id"] for r in thread[2:]]


def test_message_ids_from_an_unknown_row_is_empty(thread):
    """Same safe direction as the watermark: pin nothing rather than
    guess at a range."""
    assert storage.message_ids_from(THREAD_ID, uuid4()) == []


# ---------------------------------------------------------------------------
# ---- The neutral shape -----------------------------------------------------
# ---------------------------------------------------------------------------

def test_every_role_reconstructs_to_its_neutral_shape(thread):
    """One reconstruction point feeds every read above, so this pins the
    shape for all of them at once. tests/conftest.py's FakeStorage mirrors
    it -- if this changes, that changes identically."""
    history = storage.get_session_history(THREAD_ID)

    assert history[0] == {"role": "user", "content": "first question"}
    assert history[3] == {
        "role": "assistant", "text": "",
        "tool_calls": [{"id": "tc1", "name": "web_search", "input": {"q": "x"}}],
    }
    assert history[4] == {
        "role": "tool", "tool_call_id": "tc1", "content": "search results"}
