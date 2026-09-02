"""
test_question_tool.py

ROADMAP_v2 §23 slice 2, ACs 2 and 3: the `ask_user` tool.

WHAT IS WORTH ASSERTING. `core/interaction.py`'s decoder is already tested
in test_interaction.py, so these tests are about the three things that
decoder cannot see:

  * the tool DENIES with an explanation when there is nobody to ask (AC2),
    rather than hanging, raising, or reporting success
  * the four affordances survive the trip THROUGH THE REAL REGISTRY --
    registration, the schema, and dispatch's injection of
    `response_channel`. §23 slice 1 lost four mutations to tests that
    patched exactly this wiring, so at least one test here must not
  * both shells can actually put the question, because a kind a shell does
    not render silently decodes to "nobody answered" (D12)

The tool is UNGATED (J12), which is a claim about what the model can see:
`schemas(callable_only=True)` must still advertise it with no channel
present, or the deny-cleanly path is unreachable because the model never
learns the tool exists.
"""

import threading

import pytest

from core import interaction
from core.interaction import QUESTION, Request, ResponseChannel
from tools.builtin import ask_user


def _channel(answer):
    return ResponseChannel(ask=lambda _request: answer)


def _capturing():
    """A channel that records the Request it was given."""
    seen = []

    def ask(request):
        seen.append(request)
        return {"options": [], "text": "noted"}

    return ResponseChannel(ask=ask), seen


# ---------------------------------------------------------------------------
# ---- AC2: no way to ask means denied, with a reason ------------------------
# ---------------------------------------------------------------------------

class TestHeadlessDeniesAndSaysWhy:

    def test_no_channel_is_an_error_result(self):
        """The spec: "with no response channel the call is DENIED, and the
        model receives an error result it can work around"."""
        result = ask_user.run({"question": "which?"}, response_channel=None)
        assert "error" in result
        assert "result" not in result

    def test_the_error_says_what_to_do_instead(self):
        """"and say why" (AC2). The reader is a model mid-run that must now
        decide for itself -- an error naming only the condition would leave
        it re-calling the tool."""
        result = ask_user.run({"question": "which?"}, response_channel=None)
        message = result["error"].lower()
        assert "nobody" in message or "cannot reach" in message
        assert "assumption" in message or "yourself" in message

    def test_it_does_not_raise(self):
        """A raising tool would be contained by dispatch anyway, but the
        message the model gets would be a traceback string rather than
        something written for it."""
        assert isinstance(
            ask_user.run({"question": "q"}, response_channel=None), dict)

    def test_the_tool_is_still_ADVERTISED_with_no_channel(self):
        """J12, and the reason gating it was rejected. §13 does not merely
        deny a gated tool where nothing can ask -- it stops advertising it,
        so a gated ask_user would be invisible rather than deniable and the
        model could not "work around" anything.

        Asserted against the real registry's headless view, because this is
        a claim about the schema list the model actually receives.
        """
        from tools.registry import registry

        advertised = [s["name"] for s in registry.schemas(callable_only=True)]
        assert "ask_user" in advertised


# ---------------------------------------------------------------------------
# ---- The four affordances -------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheFourAffordances:

    def test_a_chosen_option_reaches_the_model(self):
        result = ask_user.run(
            {"question": "which?", "options": ["red", "blue"]},
            response_channel=_channel({"options": ["red"]}))
        assert result["result"]["chosen"] == ["red"]

    def test_multi_select_is_passed_through_to_the_shell(self):
        """The shell decides how to render it, so what matters is that the
        flag arrives on the payload."""
        channel, seen = _capturing()
        ask_user.run({"question": "which?", "options": ["a", "b"],
                      "multi_select": True}, response_channel=channel)
        assert seen[0].payload["multi_select"] is True

    def test_free_text_reaches_the_model(self):
        result = ask_user.run(
            {"question": "which?"},
            response_channel=_channel({"text": "neither, do X"}))
        assert result["result"]["text"] == "neither, do X"

    def test_allow_text_defaults_to_TRUE(self):
        """A write-your-own answer is one of the four affordances, so the
        absence of the key is not the absence of the affordance."""
        channel, seen = _capturing()
        ask_user.run({"question": "q"}, response_channel=channel)
        assert seen[0].payload["allow_text"] is True

    def test_allow_text_can_be_turned_off(self):
        channel, seen = _capturing()
        # Options supplied (#114): allow_text=False without any would now
        # be refused as unanswerable before reaching the payload at all.
        ask_user.run({"question": "q", "options": ["a"], "allow_text": False},
                     response_channel=channel)
        assert seen[0].payload["allow_text"] is False

    def test_the_deferral_is_reported_as_a_result_not_an_error(self):
        """The spec's escape "returns control to the conversation instead of
        forcing a choice". It is something the user DID, so it is a result;
        and it tells the model not to re-ask, or the escape becomes a loop.
        """
        result = ask_user.run(
            {"question": "which?"}, response_channel=_channel({"defer": True}))
        assert "result" in result
        assert "error" not in result
        assert "discuss" in result["result"].lower()
        assert "again" in result["result"].lower()

    def test_a_dismissal_is_reported_DIFFERENTLY_from_a_deferral(self):
        """Two things that both mean "no choice was made" and call for
        different next moves. Collapsing them is what makes the modal's
        Discuss button pointless."""
        deferred = ask_user.run({"question": "q"},
                                response_channel=_channel({"defer": True}))
        dismissed = ask_user.run({"question": "q"},
                                 response_channel=_channel(None))
        assert "result" in deferred and "error" in dismissed

    def test_a_blank_submission_is_neither(self):
        """Someone who was present and said nothing. Not the no-channel
        error -- there was a person -- and not a deferral."""
        result = ask_user.run(
            {"question": "q"},
            response_channel=_channel({"options": [], "text": ""}))
        assert "result" in result
        assert "ask again" in result["result"].lower()


# ---------------------------------------------------------------------------
# ---- The option cap -------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheOptionCap:

    def test_four_options_are_accepted(self):
        result = ask_user.run(
            {"question": "q", "options": ["a", "b", "c", "d"]},
            response_channel=_channel({"options": ["a"]}))
        assert "result" in result

    def test_five_options_are_REFUSED_not_truncated(self):
        """Truncating would show the user a different question from the one
        the model asked, and the model would then read the answer as a
        response to the whole list. M15's rule with the model in the naming
        role: never silently drop something somebody named."""
        channel, seen = _capturing()
        result = ask_user.run(
            {"question": "q", "options": ["a", "b", "c", "d", "e"]},
            response_channel=channel)
        assert "error" in result
        assert seen == [], "the question was put despite being over the cap"

    def test_the_cap_error_names_the_limit_and_the_count(self):
        result = ask_user.run(
            {"question": "q", "options": list("abcdef")},
            response_channel=_channel({"options": []}))
        assert str(ask_user.MAX_OPTIONS) in result["error"]
        assert "6" in result["error"]

    def test_an_option_at_the_length_cap_is_accepted(self):
        result = ask_user.run(
            {"question": "q", "options": ["x" * ask_user.MAX_OPTION_CHARS]},
            response_channel=_channel({"options": []}))
        assert "result" in result

    def test_an_over_long_option_is_REFUSED_not_truncated(self):
        """Batch 49, and the same rule as the count cap directly above --
        which is the point, because the modal had been truncating by
        accident. An option button was `width: auto`, so a label wider
        than the dialog was CUT at its edge: two options differing only
        past the cut rendered as the same visible sentence, and choosing
        between them was choosing at random. Fixing the width made long
        options WRAP, and this keeps them short enough to wrap twice
        rather than scroll.

        Refused rather than shortened for M15's reason: an option the
        tool trimmed is not the option the model offered, and the answer
        would come back as though it were.
        """
        channel, seen = _capturing()
        result = ask_user.run(
            {"question": "q",
             "options": ["a", "x" * (ask_user.MAX_OPTION_CHARS + 1)]},
            response_channel=channel)
        assert "error" in result
        assert seen == [], "the question was put despite an unshowable option"

    def test_the_length_error_names_the_limit_the_length_and_the_option(self):
        """A correctable error names what to correct. The offending option
        is quoted because a model given four options and a bare number
        cannot tell which one to shorten."""
        over = "unique-marker " + "y" * ask_user.MAX_OPTION_CHARS
        result = ask_user.run(
            {"question": "q", "options": ["a", over]},
            response_channel=_channel({"options": []}))
        assert str(ask_user.MAX_OPTION_CHARS) in result["error"]
        assert str(len(over)) in result["error"]
        assert "unique-marker" in result["error"]

    def test_the_schema_states_the_length_cap(self):
        """A limit the model is only told about by being refused costs a
        turn every time. `MAX_OPTIONS` is in the schema text for the same
        reason, asserted the same way."""
        options = ask_user.TOOL_SCHEMA["input_schema"]["properties"]["options"]
        assert str(ask_user.MAX_OPTION_CHARS) in options["description"]



# ---------------------------------------------------------------------------
# ---- Malformed input from the model ---------------------------------------
# ---------------------------------------------------------------------------

class TestBadParams:

    @pytest.mark.parametrize("params", [{}, {"question": ""},
                                        {"question": None},
                                        {"question": 3}])
    def test_a_missing_or_wrong_typed_question_is_an_error(self, params):
        """params.get, never params[]: no provider validates tool inputs
        against the schema, and a bare KeyError escapes the loop's
        ToolCallDenied handler and takes down the whole turn."""
        assert "error" in ask_user.run(params, response_channel=_channel(None))

    def test_wrong_typed_options_is_an_error(self):
        assert "error" in ask_user.run(
            {"question": "q", "options": "red"},
            response_channel=_channel(None))

    def test_no_params_at_all_does_not_raise(self):
        assert "error" in ask_user.run({}, response_channel=None)

    def test_no_options_and_no_text_box_is_refused(self):
        """#114: the one combination with no way to reply. The modal
        would render a single "Discuss instead" button, so its defer
        would mean "that was my only button", not "let's talk about it"
        -- collapsing the distinction the two answers exist to keep."""
        result = ask_user.run(
            {"question": "Which database?",
             "options": [], "allow_text": False},
            response_channel=_channel(None))
        assert result["error"] == (
            "ask_user needs something to answer with: give options, or "
            "leave allow_text true so the user can write a reply.")

    def test_empty_options_with_the_default_allow_text_proceeds(self):
        """allow_text defaults TRUE, so absence of the key is not
        absence of the affordance: an open question is legitimate and
        must reach the channel."""
        channel, seen = _capturing()
        answer = ask_user.run({"question": "What next?"},
                              response_channel=channel)
        assert "error" not in answer

    def test_options_without_allow_text_still_proceeds(self):
        channel, seen = _capturing()
        answer = ask_user.run(
            {"question": "Pick one.", "options": ["a"],
             "allow_text": False},
            response_channel=channel)
        assert "error" not in answer

    def test_the_shape_refusal_wins_over_an_unreachable_channel(self):
        """Ordering (#114): shape validation completes before the
        channel check, so the model is told what to FIX first."""
        result = ask_user.run({"question": "q", "options": [],
                               "allow_text": False},
                              response_channel=None)
        assert "something to answer with" in result["error"]


# ---------------------------------------------------------------------------
# ---- The wiring, asserted for real ----------------------------------------
# ---------------------------------------------------------------------------

class TestItIsActuallyWired:
    """Nothing patched. §23 slice 1's lesson: four mutations survived
    because the test that would have caught them mocked the very wiring
    they broke. Anything a test patches, some other test has to assert.
    """

    def test_it_is_registered_under_its_own_name(self):
        from tools.registry import registry

        assert "ask_user" in registry._tools
        assert registry._tools["ask_user"].schema["name"] == "ask_user"

    def test_the_registry_knows_to_inject_the_channel(self):
        """The injection is by signature inspection, so a handler renamed
        away from `response_channel` would silently stop being asked --
        and the tool would then deny every call as headless."""
        from tools.registry import registry

        assert "response_channel" in registry._injectable["ask_user"]

    def test_dispatch_actually_hands_the_channel_over(self):
        """End to end through the real dispatch: no mock of the registry, no
        mock of permissions. If this passes and the tool still denies, the
        injection is broken."""
        from tools.registry import registry

        channel, seen = _capturing()
        result = registry.dispatch(
            "ask_user", {"question": "real?"}, response_channel=channel)
        assert seen, "dispatch did not inject the response channel"
        assert seen[0].kind == QUESTION
        assert result["result"]["text"] == "noted"

    def test_dispatch_with_no_channel_denies_through_the_real_path(self):
        from tools.registry import registry

        result = registry.dispatch("ask_user", {"question": "real?"})
        assert "error" in result

    def test_it_is_ungated_and_allowed(self):
        """AC3/D24 in the direction that matters: both fields exist (the
        import-time check enforces that) and they say ungated."""
        from security.permissions import is_tool_allowed, requires_approval

        assert is_tool_allowed("ask_user") is True
        assert requires_approval("ask_user", {}) is False

    def test_both_config_dataclasses_declare_it(self):
        """D24's check raises at import if either is missing, so this is
        really asserting that the raise would be about the right names."""
        import config

        assert hasattr(config.ToolPermissions(), "ask_user")
        assert hasattr(config.ToolApprovals(), "ask_user")

    def test_the_schema_advertises_the_cap_it_enforces(self):
        """A description promising more options than run() accepts would
        make the model produce a call that always errors."""
        options = (ask_user.TOOL_SCHEMA["input_schema"]["properties"]
                   ["options"]["description"])
        assert str(ask_user.MAX_OPTIONS) in options


# ---------------------------------------------------------------------------
# ---- The shells ------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheTuiModal:
    """Driven through the pilot. Capture, dismiss, settle, then assert --
    asserting with the modal up leaves the worker parked and stalls the
    suite rather than failing it.
    """

    @staticmethod
    def _ask_on_a_thread(app, payload):
        answer = {}

        def worker():
            answer["value"] = app.ask_question_blocking(payload)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return answer

    @pytest.mark.asyncio
    async def test_choosing_an_option_returns_it(self):
        from tests.conftest import settle
        from tui.app import VenastineApp
        from tui.screens import QuestionScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            answer = self._ask_on_a_thread(
                app, {"question": "which?", "options": ["red", "blue"]})
            assert await settle(
                pilot, lambda: isinstance(app.screen, QuestionScreen)), \
                "the question modal never opened"
            app.screen.dismiss({"options": ["blue"], "text": ""})
            assert await settle(pilot, lambda: "value" in answer), \
                "the worker never came back"
        assert answer["value"]["options"] == ["blue"]

    @pytest.mark.asyncio
    async def test_enter_in_the_answer_box_sends_what_was_typed(self):
        """Batch 49. Textual posts `Input.Submitted` and QuestionScreen
        handled nothing, so the one affordance whose entire purpose is
        being typed into had no way to send what was typed -- and the
        Answer button it needed instead was, until this batch, the child
        clipped off the bottom of the dialog. Tab still reached it, which
        is why this was invisible rather than fatal.

        Asserted through the same dict the button produces, because the
        handler routes through the same `_answer` -- one shape of answer,
        one place that builds it.
        """
        from tests.conftest import settle
        from tui.app import VenastineApp
        from tui.screens import QuestionScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            answer = self._ask_on_a_thread(
                app, {"question": "where?", "options": ["red", "blue"]})
            assert await settle(
                pilot, lambda: isinstance(app.screen, QuestionScreen)), \
                "the question modal never opened"
            app.screen.query_one("#question-text").value = "somewhere else"
            app.screen.query_one("#question-text").focus()
            await pilot.pause()
            await pilot.press("enter")
            assert await settle(pilot, lambda: "value" in answer), \
                "Enter in the answer box sent nothing"
        assert answer["value"]["text"] == "somewhere else"
        assert answer["value"]["options"] == []

    @pytest.mark.asyncio
    async def test_escape_is_no_answer_not_a_deferral(self):
        """The distinction the Discuss button exists for. Escape must not
        decode into the thing the user could have chosen deliberately."""
        from tests.conftest import settle
        from tui.app import VenastineApp
        from tui.screens import QuestionScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            answer = self._ask_on_a_thread(app, {"question": "which?"})
            assert await settle(
                pilot, lambda: isinstance(app.screen, QuestionScreen))
            await pilot.press("escape")
            assert await settle(pilot, lambda: "value" in answer), \
                "escape left the worker parked"
        assert answer["value"] is None

    @pytest.mark.asyncio
    async def test_the_discuss_button_defers(self):
        from tests.conftest import settle
        from tui.app import VenastineApp
        from tui.screens import QuestionScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            answer = self._ask_on_a_thread(app, {"question": "which?"})
            assert await settle(
                pilot, lambda: isinstance(app.screen, QuestionScreen))
            await pilot.click("#question-defer")
            assert await settle(pilot, lambda: "value" in answer)
        assert answer["value"] == {"defer": True}

    @pytest.mark.asyncio
    async def test_a_typed_answer_comes_back(self):
        from tests.conftest import settle
        from tui.app import VenastineApp
        from tui.screens import QuestionScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            answer = self._ask_on_a_thread(app, {"question": "open?"})
            assert await settle(
                pilot, lambda: isinstance(app.screen, QuestionScreen))
            app.screen.query_one("#question-text").value = "my own words"
            await pilot.click("#question-ok")
            assert await settle(pilot, lambda: "value" in answer)
        assert answer["value"]["text"] == "my own words"

    @pytest.mark.asyncio
    async def test_the_kind_reaches_the_modal_through_the_channel(self):
        """The branch in `_ask_blocking`. Without it the kind falls through
        to None and every question decodes as unanswered -- which is what
        CONFIRM and CHOICE do on the CLI today."""
        from tests.conftest import settle
        from tui.app import VenastineApp
        from tui.screens import QuestionScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            channel = app.response_channel()
            answer = {}

            def worker():
                answer["value"] = interaction.ask(channel, Request(
                    kind=QUESTION,
                    payload={"question": "via the channel?",
                             "options": ["yes"]}))

            threading.Thread(target=worker, daemon=True).start()
            assert await settle(
                pilot, lambda: isinstance(app.screen, QuestionScreen)), \
                "the response channel did not route QUESTION to a modal"
            app.screen.dismiss({"options": ["yes"], "text": ""})
            assert await settle(pilot, lambda: "value" in answer)
        assert answer["value"]["options"] == ["yes"]


class TestTheCliRenderer:
    """D12: a kind the CLI cannot render falls through `ask` to None, which
    decode turns into the declining default -- so the tool would report
    "the user did not answer" on every CLI run while looking wired up.
    CONFIRM and CHOICE are in exactly that state today.

    `_stdin_reader()` is the seam: one reader per process, so patching the
    factory is how a test supplies a typed line.
    """

    @staticmethod
    def _answer(typed, payload):
        """Put one question to the CLI channel and return what it decoded."""
        import unittest.mock as mock

        import main

        class _Reader:
            def ask(self, _prompt, _timeout):
                return typed

        with mock.patch.object(main, "_stdin_reader", return_value=_Reader()):
            channel = main.build_attended_provider()
        return interaction.ask(channel, Request(kind=QUESTION,
                                                payload=payload))

    _OPTIONS = {"question": "which?", "options": ["red", "blue"]}

    def test_a_number_picks_that_option(self):
        """Asserted on the decoded answer, not on what was printed: a
        renderer that draws the question and drops the answer looks
        perfectly right in a terminal."""
        answer = self._answer("2", self._OPTIONS)
        assert answer["options"] == ["blue"]

    def test_several_numbers_pick_several_when_multi_select(self):
        payload = dict(self._OPTIONS, multi_select=True)
        answer = self._answer("1 2", payload)
        assert answer["options"] == ["red", "blue"]

    def test_free_text_comes_back_as_text(self):
        answer = self._answer("neither, do X instead", self._OPTIONS)
        assert answer["text"] == "neither, do X instead"
        assert answer["options"] == []

    def test_a_qualified_number_is_read_as_TEXT(self):
        """"2 but only if X" is a sentence, not a selection. Half-parsing it
        as option 2 would drop the condition the user attached to it, which
        is the part they bothered to type."""
        answer = self._answer("2 but only if X", self._OPTIONS)
        assert answer["options"] == []
        assert "only if X" in answer["text"]

    def test_d_defers(self):
        answer = self._answer("d", self._OPTIONS)
        assert answer["defer"] is True

    def test_a_blank_line_is_no_answer(self):
        assert self._answer("", self._OPTIONS) is None

    def test_a_timeout_is_no_answer(self):
        """_StdinReader.ask returns None on timeout."""
        assert self._answer(None, self._OPTIONS) is None

    def test_an_out_of_range_number_does_not_invent_an_option(self):
        """J3 again, from the shell's side. `9` names nothing, and the
        answer must not become the nearest option."""
        answer = self._answer("9", self._OPTIONS)
        assert answer["options"] == []
