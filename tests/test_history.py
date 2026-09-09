"""
test_history.py

Batch 63's `tui/history.py`, driven directly.

NO PILOT, which is the reason `PromptHistory` takes the box's current text
as a parameter instead of watching the widget for keystrokes. The rule
this file exists to pin -- that an edit made to a recalled prompt is never
destroyed -- is a race between a keystroke and a posted `TextArea.Changed`
if it is tested through a Pilot, and plain arithmetic here.

Every case simulates the BOX, because that is what the parameter is: the
value handed in is whatever the previous call put on screen, and the whole
state machine turns on the two of them differing.

The painted result -- that ctrl+up reaches this, that the cursor lands at
the end, and that the footer entries stop being grey -- is `test_tui.py`'s
job.
"""

from tui import history
from tui.history import HISTORY_LIMIT, PromptHistory


def _fill(*prompts) -> PromptHistory:
    """A history holding `prompts`, oldest first."""
    recalled = PromptHistory()
    for prompt in prompts:
        recalled.remember(prompt)
    return recalled


def _walk(recalled, presses, box=""):
    """Press a mover `presses` times, feeding back what it hands out.

    The feedback IS the test subject: a real box shows what it was last
    given, so anything else would be measuring a state machine nobody
    drives.
    """
    seen = []
    for _ in range(presses):
        got = recalled.previous(box)
        seen.append(got)
        if got is not None:
            box = got
    return seen, box


class TestNothingToRecallIsNotAnError:
    """An empty history answers None rather than raising or inventing.

    None is what tells `_recall` to leave the box exactly as it was, and
    it has to be the answer on two different occasions -- no history at
    all, and a browse that has reached the oldest entry -- because in
    both the correct behaviour is to do nothing.
    """

    def test_an_empty_history_offers_nothing(self):
        recalled = PromptHistory()
        assert recalled.previous("") is None
        assert recalled.following("") is None
        assert len(recalled) == 0
        assert not recalled.browsing

    def test_an_empty_history_leaves_a_draft_alone(self):
        """The draft is not captured by a press that goes nowhere."""
        recalled = PromptHistory()
        assert recalled.previous("half a sentence") is None
        recalled.remember("something else")
        # If the failed press had started a browse, this would hand back
        # "half a sentence" instead of the real entry.
        assert recalled.previous("half a sentence") == "something else"

    def test_forward_does_nothing_on_a_box_being_typed_into(self):
        """ctrl+down is inert unless a browse is in progress.

        There is nothing newer than what the user is already looking at,
        and answering with an entry would be walking them backwards under
        a key that says forward.
        """
        recalled = _fill("one", "two")
        assert recalled.following("a fresh draft") is None
        assert recalled.following("") is None


class TestWalkingBackThroughWhatWasSent:
    """The walk itself: newest first, stopping at the oldest."""

    def test_the_newest_comes_back_first(self):
        recalled = _fill("older", "newer")
        assert recalled.previous("") == "newer"

    def test_each_press_goes_one_further_back(self):
        recalled = _fill("first", "second", "third")
        seen, _ = _walk(recalled, 3)
        assert seen == ["third", "second", "first"]

    def test_the_walk_stops_at_the_oldest_rather_than_wrapping(self):
        """The suggestion panel wraps; this must not.

        That panel's list is a MENU, where wrapping is how you reach the
        far end quickly. This is a walk backwards through time, and
        arriving at the start of it should not deposit the reader at the
        end -- particularly since the key is held down to get there.
        """
        recalled = _fill("first", "second")
        seen, box = _walk(recalled, 4)
        assert seen == ["second", "first", None, None]
        assert box == "first", (
            f"the box holds {box!r}; a press at the oldest entry has to "
            f"leave it exactly as it was")

    def test_the_draft_comes_back_past_the_newest(self):
        recalled = _fill("sent")
        assert recalled.previous("half-typed") == "sent"
        assert recalled.following("sent") == "half-typed"

    def test_forward_is_inert_once_the_draft_is_back(self):
        """Past the draft there is nothing, and the browse has ended."""
        recalled = _fill("sent")
        recalled.previous("half-typed")
        assert recalled.following("sent") == "half-typed"
        assert not recalled.browsing
        assert recalled.following("half-typed") is None

    def test_the_walk_turns_round_in_the_middle(self):
        recalled = _fill("a", "b", "c")
        assert recalled.previous("draft") == "c"
        assert recalled.previous("c") == "b"
        assert recalled.following("b") == "c"
        assert recalled.following("c") == "draft"

    def test_an_empty_draft_comes_back_as_an_empty_box(self):
        """The commonest case: recall from an empty box, then leave.

        The box must end up EMPTY rather than holding the oldest thing
        the reader passed on the way, which is what a state machine that
        forgot to record an empty draft would do.
        """
        recalled = _fill("one", "two")
        assert recalled.previous("") == "two"
        assert recalled.following("two") == ""


class TestAnEditIsNeverDiscarded:
    """The rule the `current` parameter exists for.

    Recall a prompt, change it, press previous again: the change is not
    walked past and lost. It becomes the draft, so the same key that
    reached it can hand it back.
    """

    def test_typing_over_a_recalled_prompt_starts_a_fresh_walk(self):
        recalled = _fill("one", "two", "three")
        assert recalled.previous("") == "three"
        assert recalled.previous("three") == "two"
        # The user edits what is on screen.
        assert recalled.previous("two, but corrected") == "three", (
            "an edited box has to start a NEW walk from the newest entry; "
            "continuing the old one would step over the edit")

    def test_the_edit_becomes_the_draft(self):
        recalled = _fill("one", "two")
        recalled.previous("")
        assert recalled.previous("two, but corrected") == "two"
        assert recalled.following("two") == "two, but corrected", (
            "the edit was not kept as the draft, so pressing forward "
            "cannot give it back and the typing is gone")

    def test_an_edit_before_any_recall_is_the_draft(self):
        recalled = _fill("sent")
        assert recalled.previous("a long message I typed") == "sent"
        assert recalled.following("sent") == "a long message I typed"

    def test_retyping_an_entry_exactly_is_indistinguishable(self):
        """The honest limit of comparing strings, stated rather than hidden.

        A reader who deletes a recalled prompt and types it back
        character for character continues the old walk instead of
        starting a new one. Nothing is lost -- the two strings are the
        same -- so the cost is that the draft they had before is not
        restored. Position is not knowable from the text, and the
        alternative is watching keystrokes across an async hop.
        """
        recalled = _fill("one", "two")
        assert recalled.previous("draft") == "two"
        assert recalled.previous("two") == "one"


class TestWhatGetsRemembered:
    """Slash commands included, consecutive repeats collapsed."""

    def test_a_slash_command_is_recalled_verbatim(self):
        """The worst retyping in the shell, and the reason for `remember`
        sitting at the one funnel both kinds of prompt pass through."""
        recalled = _fill("/research --attended --grant shell,fetch_url")
        assert recalled.previous("") == (
            "/research --attended --grant shell,fetch_url")

    def test_the_same_prompt_twice_running_is_one_entry(self):
        """Sending a thing twice is one thing to walk back past."""
        recalled = _fill("again", "again", "again")
        assert len(recalled) == 1
        assert recalled.previous("") == "again"
        assert recalled.previous("again") is None

    def test_a_repeat_with_something_between_is_kept(self):
        """The order is the record of what happened.

        Collapsing non-adjacent repeats would make the walk a set rather
        than a history, and the reader's mental index -- "three back" --
        would stop matching what they did.
        """
        recalled = _fill("a", "b", "a")
        assert len(recalled) == 3
        seen, _ = _walk(recalled, 3)
        assert seen == ["a", "b", "a"]

    def test_an_empty_prompt_is_not_remembered(self):
        recalled = PromptHistory()
        recalled.remember("")
        assert len(recalled) == 0

    def test_remembering_ends_a_walk_in_progress(self):
        """Sending something puts the reader back at the live draft.

        Otherwise the browse position would survive the send, and the
        next ctrl+down would hand back a draft from before the prompt
        that was just submitted.
        """
        recalled = _fill("one", "two")
        recalled.previous("")
        recalled.remember("three")
        assert not recalled.browsing
        assert recalled.following("two") is None
        assert recalled.previous("") == "three"

    def test_the_oldest_fall_off_the_end(self):
        recalled = _fill(*[f"prompt {n}" for n in range(HISTORY_LIMIT + 10)])
        assert len(recalled) == HISTORY_LIMIT
        seen, _ = _walk(recalled, HISTORY_LIMIT + 1)
        assert seen[0] == f"prompt {HISTORY_LIMIT + 9}"
        assert seen[HISTORY_LIMIT - 1] == "prompt 10", (
            "the cap dropped from the wrong end -- the OLDEST go, or "
            "recall stops working the moment the cap is reached")
        assert seen[HISTORY_LIMIT] is None


class TestItStaysPure:
    """`tui/markdown.py`, `tui/diffs.py` and `tui/meters.py`'s rule.

    A module `tui/app.py` imports for arithmetic must not reach the
    harness, or the arithmetic stops being testable without one.
    """

    def test_it_imports_nothing_from_core_or_textual(self):
        import inspect
        source = inspect.getsource(history)
        for forbidden in ("import core", "from core", "import textual",
                          "from textual", "import storage"):
            assert forbidden not in source, (
                f"tui/history.py contains {forbidden!r}; it is the pure "
                f"half and app.py is the half that touches widgets")

    def test_two_sessions_do_not_share_a_list(self):
        """The class default on `VenastineApp` is None, not an instance.

        A mutable built at class level would be shared by every app in a
        test session, and the suite builds dozens.
        """
        first, second = PromptHistory(), PromptHistory()
        first.remember("mine")
        assert len(second) == 0
