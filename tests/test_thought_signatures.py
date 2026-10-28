"""
test_thought_signatures.py

ROADMAP_v3 §82 (GS1-GS4): Gemini 3's thought signatures, read off the stream
and handed back. Every model a new Google account can call is a Gemini 3
model -- measured in batch 172, gemini-2.5-flash answers 404 "no longer
available to new users" -- and Gemini 3 refuses a request whose current turn
holds an unsigned function call. The old pin could not hold a signature at
all, so every GOOGLE tool loop failed on its second request.

These run against the root conftest's fake google.genai, whose Part keeps
the keyword it is given. tests/test_sdk_conformance.py drives the same two
paths through the REAL pinned types.
"""

import base64
from types import SimpleNamespace

import pytest

from core.client import (
    _GOOGLE_FOREIGN_SIGNATURE,
    _messages_for_provider,
    call_model_stream,
)
from core.replay import _reasoning_text

SIG = b"\x01\x02signature"
SIG_B64 = base64.b64encode(SIG).decode("ascii")


def _block(call_id, signature):
    return {"type": "thought_signature", "call_id": call_id,
            "signature": signature}


def _record(model="gemini-x", provider="GOOGLE", blocks=None):
    if blocks is None:
        blocks = [_block("c1", SIG_B64), _block("c2", None)]
    return {"provider": provider, "model": model, "blocks": blocks}


def _turn(record, calls=("c1", "c2"), text=""):
    """One user ask, one assistant turn calling `calls`, and their results."""
    assistant = {"role": "assistant", "text": text, "tool_calls": [
        {"id": c, "name": "probe", "input": {}} for c in calls]}
    if record is not None:
        assistant["thinking"] = record
    return ([{"role": "user", "content": "go"}, assistant]
            + [{"role": "tool", "tool_call_id": c, "content": "ok"}
               for c in calls])


def _signatures(messages, model="gemini-x", turn=1):
    content = _messages_for_provider("GOOGLE", messages, model)[turn]
    assert content.role == "model"
    return [p.thought_signature for p in content.parts
            if p.function_call is not None]


# ---------------------------------------------------------------------------
# ---- A turn this model wrote goes back exactly as it wrote it (GS2) --------
# ---------------------------------------------------------------------------

class TestATurnGoesBackAsTheModelWroteIt:

    def test_each_call_gets_its_own_signature_and_nothing_is_added(self):
        """Gemini 3 signs only the first call of a parallel batch, so the
        second goes back unsigned -- adding one would not be its own."""
        assert _signatures(_turn(_record())) == [SIG, None]

    def test_a_text_part_carries_no_signature(self):
        content = _messages_for_provider(
            "GOOGLE", _turn(_record(), text="Looking."), "gemini-x")[1]
        assert content.parts[0].text == "Looking."
        assert content.parts[0].thought_signature is None

    def test_a_model_that_signed_nothing_gets_nothing(self):
        """A model that does not think signs no call. Its record says so,
        and that is why a call with no signature is still recorded: it keeps
        "this model attached nothing" apart from "nothing is known"."""
        record = _record(blocks=[_block("c1", None), _block("c2", None)])
        assert _signatures(_turn(record)) == [None, None]


# ---------------------------------------------------------------------------
# ---- A turn it did not write gets the placeholder, first call only (GS3) ---
# ---------------------------------------------------------------------------

class TestATurnThisModelDidNotWriteGetsThePlaceholder:

    @pytest.mark.parametrize("record", [
        None,                           # before §82, or no record at all
        _record(model="gemini-y"),      # another Google model
        _record(provider="ANTHROPIC"),  # another provider
    ], ids=["no-record", "other-model", "other-provider"])
    def test_on_the_first_call_only(self, record):
        assert _signatures(_turn(record)) == [_GOOGLE_FOREIGN_SIGNATURE, None]

    def test_no_model_reads_as_foreign(self):
        assert _signatures(_turn(_record()), model=None) == [
            _GOOGLE_FOREIGN_SIGNATURE, None]

    def test_an_unreadable_signature_costs_that_call_alone(self):
        """`_decode_thinking`'s rule: an entry that cannot be read costs the
        entry. The call still validates, on the placeholder."""
        record = _record(blocks=[_block("c1", "not base64!"),
                                 _block("c2", None)])
        assert _signatures(_turn(record)) == [_GOOGLE_FOREIGN_SIGNATURE, None]

    def test_a_first_call_missing_from_its_record_gets_the_placeholder(self):
        record = _record(blocks=[_block("c2", None)])
        assert _signatures(_turn(record)) == [_GOOGLE_FOREIGN_SIGNATURE, None]

    def test_only_signature_blocks_are_read_as_signatures(self):
        record = _record(blocks=[{"type": "thinking", "thinking": "hm",
                                  "call_id": "c1", "signature": SIG_B64}])
        assert _signatures(_turn(record)) == [_GOOGLE_FOREIGN_SIGNATURE, None]

    def test_each_assistant_turn_is_judged_on_its_own(self):
        """A /model switch mid-thread leaves turns from two models. Each
        goes back on its own record, never on its neighbour's."""
        other = base64.b64encode(b"gemini-y's own").decode("ascii")
        first = _turn(_record(model="gemini-y", blocks=[_block("a1", other)]),
                      calls=("a1",))
        second = _turn(_record(blocks=[_block("c1", SIG_B64)]), calls=("c1",))
        messages = first + second[1:]
        assert _signatures(messages, turn=1) == [_GOOGLE_FOREIGN_SIGNATURE]
        assert _signatures(messages, turn=3) == [SIG]


# ---------------------------------------------------------------------------
# ---- The stream keeps what each call carried (GS2) --------------------------
# ---------------------------------------------------------------------------

def _drain(parts, model="gemini-x"):
    chunk = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
        usage_metadata=SimpleNamespace(prompt_token_count=3,
                                       candidates_token_count=2))
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content_stream=lambda **kw: iter([chunk])))
    final = None
    for token in call_model_stream(client, "GOOGLE", model, [], "sys", []):
        if token.final_response is not None:
            final = token.final_response
    return final


def _call_part(call_id, signature=None):
    return SimpleNamespace(text=None, thought_signature=signature,
                           function_call=SimpleNamespace(
                               id=call_id, name="probe", args={}))


class TestTheStreamKeepsWhatEachCallCarried:

    @pytest.fixture(autouse=True)
    def _no_providers(self, monkeypatch):
        monkeypatch.setattr("core.client.load_provider_data", lambda: {})

    def test_signed_and_unsigned_calls_are_both_recorded(self):
        final = _drain([_call_part("c1", SIG), _call_part("c2")])
        assert final.thinking == _record()

    def test_the_record_names_the_model_that_wrote_it(self):
        final = _drain([_call_part("c1", SIG)], model="gemini-z")
        assert (final.thinking["provider"], final.thinking["model"]) == (
            "GOOGLE", "gemini-z")

    def test_a_turn_without_calls_records_nothing(self):
        final = _drain([SimpleNamespace(text="hi", function_call=None,
                                        thought_signature=b"text-sig")])
        assert final.thinking is None

    def test_a_content_with_no_parts_is_not_an_error(self):
        """Content.parts is Optional on the pinned type."""
        assert _drain(None).tool_calls == []

    def test_what_is_recorded_goes_back_unchanged(self):
        final = _drain([_call_part("c1", SIG), _call_part("c2")])
        messages = _turn(final.thinking)
        assert _signatures(messages) == [SIG, None]


# ---------------------------------------------------------------------------
# ---- Nothing else sees a signature ------------------------------------------
# ---------------------------------------------------------------------------

class TestNothingElseSeesASignature:

    def test_replay_renders_no_text_for_it(self):
        assert _reasoning_text(_record()) == ""

    def test_anthropic_never_receives_a_google_record(self):
        wire = _messages_for_provider("ANTHROPIC", _turn(_record()),
                                      "gemini-x")
        assert [block["type"] for block in wire[1]["content"]] == [
            "tool_use", "tool_use"]

    def test_an_echoing_compatible_provider_never_receives_it(self):
        wire = _messages_for_provider("OPENROUTER", _turn(_record()),
                                      "gemini-x", echo_reasoning=True)
        assert "reasoning_content" not in wire[1]
