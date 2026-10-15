"""
ROADMAP_v3 §76: an image a tool reads reaches a model that can see it.

`view_image` reads an image off disk and attaches it to its own result. The
bytes do not ride in the result dict -- they go to the memory, into the same
row as the result (IM3) -- so this file is about the five places that have to
agree on that: the tool, the memory and its column, the advertisement gate,
the per-provider translation, and the compactor's size proxy.

Real-SQLite round trips live in test_storage_e2e.py, which owns the one
sqlmodel swap; these are the pure halves.
"""

import base64
import dataclasses
import json
import logging
import os
from unittest.mock import patch

import pytest

import config
import storage
from core import client as client_mod
from core import compaction
from core import loop as loop_mod
from core import memory as memory_mod
from core.client import _messages_for_provider, accepts_images
from tools.builtin import file_ops
from tools.context import RunInfo
from tools.registry import registry

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
IMAGE = {"media_type": "image/png", "data": base64.b64encode(PNG).decode()}


class _Memory:
    """What view_image needs from a memory: somewhere to attach."""

    def __init__(self):
        self.attached = []

    def attach_image(self, tool_call_id, media_type, data):
        self.attached.append((tool_call_id, media_type, data))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    monkeypatch.setattr(config, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", os.path.realpath(ws))
    return ws


def _put(workspace, name, data):
    path = os.path.join(workspace, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return name


def _enable_view_image(monkeypatch):
    """`view_image` ships DISABLED (IM1), and is_tool_allowed refuses it
    long before any gate -- so a test about the gate has to raise the
    permission, or it passes while proving nothing (AGENTS.md, Testing)."""
    real = config.ToolPermissions
    monkeypatch.setattr(config, "ToolPermissions",
                        lambda: dataclasses.replace(real(), view_image=True))


# ---------------------------------------------------------------------------
# ---- The tool (IM1, IM3, IM7) ----------------------------------------------
# ---------------------------------------------------------------------------

class TestTheToolAttachesTheImageAndReturnsText:

    def _run(self, path, memory=None, run=None, call_id="call_1"):
        return file_ops.view_image_run(
            {"path": path},
            memory=_Memory() if memory is None else memory,
            call_id=call_id,
            parent_run=RunInfo(model="m", provider_name="P", images=True)
            if run is None else run)

    def test_the_bytes_go_to_the_memory_and_the_result_is_a_sentence(
            self, workspace):
        memory = _Memory()
        result = self._run(_put(workspace, "shot.png", PNG), memory=memory)
        assert memory.attached == [
            ("call_1", "image/png", base64.b64encode(PNG).decode())]
        # The result is what passes the redactor, the event and the
        # transcript: it names the file and carries no payload.
        assert result == {"content": "Attached shot.png (image/png, 40 bytes)."}

    @pytest.mark.parametrize("data, media_type", [
        (PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"),
        (WEBP, "image/webp"), (b"GIF87a" + b"\x00" * 8, "image/gif")])
    def test_the_format_is_read_from_the_bytes(self, workspace, data,
                                               media_type):
        memory = _Memory()
        self._run(_put(workspace, "img.bin", data), memory=memory)
        assert memory.attached[0][1] == media_type

    def test_a_misnamed_file_is_the_format_its_bytes_say(self, workspace):
        """Declaring the extension's type for a PNG named .jpg is a request
        the provider rejects (IM7)."""
        memory = _Memory()
        self._run(_put(workspace, "really_a.jpg", PNG), memory=memory)
        assert memory.attached[0][1] == "image/png"

    @pytest.mark.parametrize("data", [b"", b"plain text", b"%PDF-1.7\n",
                                      b"RIFF\x00\x00\x00\x00WAVEfmt "])
    def test_anything_else_is_refused_and_nothing_is_attached(
            self, workspace, data):
        memory = _Memory()
        result = self._run(_put(workspace, "x.png", data), memory=memory)
        assert "not a PNG, JPEG, GIF or WEBP" in result["error"]
        assert memory.attached == []

    def test_an_image_over_the_cap_is_refused(self, workspace, monkeypatch):
        monkeypatch.setattr(config, "MAX_IMAGE_BYTES", len(PNG) - 1)
        memory = _Memory()
        result = self._run(_put(workspace, "big.png", PNG), memory=memory)
        assert "exceeds the maximum attachable size" in result["error"]
        assert memory.attached == []

    def test_an_image_at_the_cap_is_attached(self, workspace, monkeypatch):
        """The boundary, so the refusal above is not an off-by-one that
        refuses everything."""
        monkeypatch.setattr(config, "MAX_IMAGE_BYTES", len(PNG))
        memory = _Memory()
        self._run(_put(workspace, "edge.png", PNG), memory=memory)
        assert len(memory.attached) == 1

    @pytest.mark.parametrize("run", [
        None, RunInfo(model="m", provider_name="P", images=False)])
    def test_a_run_that_cannot_deliver_an_image_refuses(self, workspace, run):
        """IM6's backstop, for a call made to a tool the run did not offer.
        None is a dispatch with no run at all."""
        memory = _Memory()
        path = _put(workspace, "shot.png", PNG)
        result = file_ops.view_image_run(
            {"path": path}, memory=memory, call_id="c", parent_run=run)
        assert "not available in this run" in result["error"]
        assert memory.attached == []

    def test_no_memory_is_refused_rather_than_raising(self, workspace):
        path = _put(workspace, "shot.png", PNG)
        result = file_ops.view_image_run(
            {"path": path}, memory=None, call_id="c",
            parent_run=RunInfo(model="m", provider_name="P", images=True))
        assert "no conversation" in result["error"]

    def test_the_protected_segment_is_refused(self, workspace):
        """`read`'s hard deny, re-tested in the handler as all the file
        tools re-test it."""
        memory = _Memory()
        _put(workspace, os.path.join(".venastine", "a.png"), PNG)
        result = self._run(os.path.join(".venastine", "a.png"),
                           memory=memory)
        assert "Refused" in result["error"]
        assert memory.attached == []
        assert registry.refusal_reason(
            "view_image", {"path": ".venastine/a.png"}, None)

    def test_a_missing_file_is_named(self, workspace):
        assert "No such file" in self._run("gone.png")["error"]


class TestTheToolIsRegisteredLikeRead:

    def test_it_ships_disabled_in_both_tables(self):
        """IM1: enabled apart from `read`, but off by default like it."""
        assert config.ToolPermissions().view_image is False
        assert config.ToolApprovals().view_image is False

    def test_it_carries_reads_path_rules(self):
        spec = registry._tools["view_image"]
        assert spec.approval_check is file_ops._file_approval_check
        assert spec.returns_images is True
        assert registry._tools["read"].returns_images is False

    def test_outside_the_workspace_asks(self, workspace):
        assert registry.approval_needed(
            "view_image", {"path": "/etc/passwd"}, None) is True
        assert registry.approval_needed(
            "view_image", {"path": "in.png"}, None) is False

    def test_dispatch_attaches_and_the_result_carries_no_payload(
            self, workspace, monkeypatch):
        """Through the REAL dispatch: the permission, the refusal check,
        the injection and the output redactor all live, the handler not
        stubbed."""
        _enable_view_image(monkeypatch)
        memory = _Memory()
        result = registry.dispatch(
            "view_image", {"path": _put(workspace, "s.png", PNG)},
            parent_run=RunInfo(model="m", provider_name="P", images=True),
            memory=memory, call_id="call_7")
        assert memory.attached[0][:2] == ("call_7", "image/png")
        assert IMAGE["data"] not in json.dumps(result)


# ---------------------------------------------------------------------------
# ---- Memory and the column (IM2, IM3) --------------------------------------
# ---------------------------------------------------------------------------

class TestTheImageIsWrittenWithItsResult:

    def _memory(self):
        memory = memory_mod.ConversationMemory.__new__(
            memory_mod.ConversationMemory)
        memory.thread_id = "t1"
        memory._messages = []
        memory._pending_images = {}
        return memory

    def test_an_attached_image_lands_in_the_same_row_as_its_result(self):
        memory = self._memory()
        memory.attach_image("call_1", "image/png", IMAGE["data"])
        with patch.object(memory_mod, "save_message") as save:
            memory.add_tool_result("call_1", {"content": "Attached s.png"})
        save.assert_called_once_with(
            "t1", role="tool", content="{'content': 'Attached s.png'}",
            tool_call_id="call_1", images=[IMAGE])
        assert memory.messages[-1]["images"] == [IMAGE]
        assert memory._pending_images == {}

    def test_a_result_with_nothing_attached_is_the_shape_it_always_was(self):
        memory = self._memory()
        with patch.object(memory_mod, "save_message") as save:
            memory.add_tool_result("call_1", {"content": "x"})
        assert save.call_args.kwargs["images"] is None
        assert memory.messages[-1] == {
            "role": "tool", "tool_call_id": "call_1",
            "content": "{'content': 'x'}"}

    def test_an_image_waits_for_its_own_call(self):
        """Keyed by call id: a result for another call does not take it."""
        memory = self._memory()
        memory.attach_image("call_2", "image/png", IMAGE["data"])
        with patch.object(memory_mod, "save_message"):
            memory.add_tool_result("call_1", {"content": "x"})
        assert "images" not in memory.messages[-1]
        assert memory._pending_images == {"call_2": [IMAGE]}


class TestStorageReconstruction:

    def _row(self, images):
        return {"role": "tool", "content": json.dumps("Attached s.png"),
                "name": None, "tool_call_id": "call_1", "images": images}

    def test_the_images_come_back_only_when_the_row_has_them(self):
        assert storage._to_neutral(self._row(json.dumps([IMAGE]))) == {
            "role": "tool", "tool_call_id": "call_1",
            "content": "Attached s.png", "images": [IMAGE]}
        assert storage._to_neutral(self._row(None)) == {
            "role": "tool", "tool_call_id": "call_1",
            "content": "Attached s.png"}

    @pytest.mark.parametrize("raw", ["not json", "{}", '"a string"', "[]",
                                     json.dumps([{"media_type": "image/png"}]),
                                     json.dumps([{"data": "x"}])])
    def test_an_unusable_record_costs_the_image_not_the_thread(self, raw):
        assert "images" not in storage._to_neutral(self._row(raw))

    def test_one_unusable_entry_costs_that_entry(self):
        raw = json.dumps([IMAGE, {"media_type": "", "data": "x"}])
        assert storage._to_neutral(self._row(raw))["images"] == [IMAGE]


# ---------------------------------------------------------------------------
# ---- Who is offered the tool (IM4, IM6) ------------------------------------
# ---------------------------------------------------------------------------

class TestAcceptsImagesIsOptInAndStrict:

    @pytest.mark.parametrize("entry, expected", [
        ({"accepts_images": True}, True),
        ({"accepts_images": False}, False),
        ({"accepts_images": "true"}, False),
        ({"accepts_images": 1}, False),
        ({}, False)])
    def test_only_a_json_true_opts_in(self, monkeypatch, entry, expected):
        """`bool("false")` is True, so the test is `is True` (A29)."""
        monkeypatch.setattr(client_mod, "load_provider_data",
                            lambda: {"OPENAI": entry})
        assert accepts_images("OPENAI") is expected

    def test_an_unknown_provider_takes_no_images(self, monkeypatch):
        monkeypatch.setattr(client_mod, "load_provider_data", lambda: {})
        assert accepts_images("NOPE") is False

    def test_google_is_deferred_and_says_so_once(self, monkeypatch, caplog):
        monkeypatch.setattr(client_mod, "load_provider_data",
                            lambda: {"GOOGLE": {"accepts_images": True}})
        monkeypatch.setattr(client_mod, "_images_ignored_warned", set())
        with caplog.at_level(logging.WARNING, logger="core.client"):
            assert accepts_images("GOOGLE") is False
            assert accepts_images("GOOGLE") is False
        assert [r.getMessage() for r in caplog.records].count(
            caplog.records[0].getMessage()) == 1
        assert "GOOGLE" in caplog.records[0].getMessage()


class TestTheRunDecidesOnce:

    class _M:
        def __init__(self, kind):
            self.kind = kind

    @pytest.mark.parametrize("kind, accepts, expected", [
        (storage.THREAD_KIND_CHAT, True, True),
        (storage.THREAD_KIND_SUBAGENT, True, True),
        (storage.THREAD_KIND_CHAT, False, False),
        (storage.THREAD_KIND_RESEARCH_PASS, True, False)])
    def test_a_research_pass_never_takes_images(self, monkeypatch, kind,
                                               accepts, expected):
        monkeypatch.setattr(loop_mod, "accepts_images", lambda p: accepts)
        assert loop_mod._images_deliverable("P", self._M(kind)) is expected


class TestTheSchemaListFollowsTheRun:

    def _names(self, **kwargs):
        return {s["name"] for s in registry.schemas(None, **kwargs)}

    def test_it_is_offered_only_where_an_image_can_arrive(self, monkeypatch):
        _enable_view_image(monkeypatch)
        assert "view_image" in self._names(images=True)
        assert "view_image" not in self._names(images=False)
        # The default is the safe answer: a caller that never heard of
        # images hides the tool rather than offering an unseeable result.
        assert "view_image" not in self._names()

    def test_every_other_tool_is_offered_exactly_as_before(self, monkeypatch):
        _enable_view_image(monkeypatch)
        assert self._names(images=True) - self._names(images=False) == {
            "view_image"}

    def test_a_disabled_tool_is_not_reported_as_image_hidden(self):
        """Shipped config: the tool is off, so there is nothing to explain."""
        assert "view_image" not in registry.image_hidden(None)

    def test_an_enabled_tool_left_out_is_named(self, monkeypatch):
        _enable_view_image(monkeypatch)
        assert registry.image_hidden(None) == ["view_image"]


# ---------------------------------------------------------------------------
# ---- The wire (IM4, IM5) ---------------------------------------------------
# ---------------------------------------------------------------------------

def _turn(*results):
    """An assistant turn calling one tool per result, then the results."""
    calls = [{"id": cid, "name": "view_image", "input": {"path": "s.png"}}
             for cid, _ in results]
    rows = [{"role": "user", "content": "look"},
            {"role": "assistant", "text": "", "tool_calls": calls}]
    for cid, images in results:
        row = {"role": "tool", "tool_call_id": cid, "content": "Attached."}
        if images:
            row["images"] = images
        rows.append(row)
    return rows


class TestAnthropicCarriesTheImageInsideItsResult:

    def test_delivered_as_an_image_block_after_the_text(self):
        wire = _messages_for_provider(
            "ANTHROPIC", _turn(("c1", [IMAGE])), images=True)
        assert wire[-1]["content"][0]["content"] == [
            {"type": "text", "text": "Attached."},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": IMAGE["data"]}}]

    def test_an_empty_text_sends_no_empty_text_block(self):
        rows = _turn(("c1", [IMAGE]))
        rows[-1]["content"] = ""
        wire = _messages_for_provider("ANTHROPIC", rows, images=True)
        assert [b["type"] for b in wire[-1]["content"][0]["content"]] == [
            "image"]

    def test_not_delivered_is_a_note_and_no_bytes(self):
        wire = _messages_for_provider(
            "ANTHROPIC", _turn(("c1", [IMAGE])), images=False)
        content = wire[-1]["content"][0]["content"]
        assert content.startswith("Attached.\n[1 image(s) attached")
        assert "not sent" in content
        assert IMAGE["data"] not in json.dumps(wire)


class TestOpenAICarriesTheImageAfterTheBatch:

    def test_one_harness_message_after_every_tool_message(self):
        """A tool message takes text only, and every tool message must
        follow the assistant turn directly -- so the images go after the
        WHOLE batch, even when only the first result carried one."""
        wire = _messages_for_provider(
            "OPENAI", _turn(("c1", [IMAGE]), ("c2", None)), images=True)
        assert [m["role"] for m in wire] == [
            "user", "assistant", "tool", "tool", "user"]
        assert "follow these tool results" in wire[2]["content"]
        assert wire[3]["content"] == "Attached."
        parts = wire[4]["content"]
        assert parts[0]["type"] == "text"
        assert "c1" in parts[0]["text"] and "Harness" in parts[0]["text"]
        assert parts[1] == {"type": "image_url", "image_url": {
            "url": f"data:image/png;base64,{IMAGE['data']}"}}

    def test_two_results_with_images_share_one_message(self):
        wire = _messages_for_provider(
            "OPENAI", _turn(("c1", [IMAGE]), ("c2", [IMAGE])), images=True)
        assert [m["role"] for m in wire].count("user") == 2
        parts = wire[-1]["content"]
        assert [p["type"] for p in parts] == [
            "text", "image_url", "text", "image_url"]

    def test_the_message_lands_before_the_next_turn(self):
        rows = _turn(("c1", [IMAGE])) + [
            {"role": "assistant", "text": "A cat.", "tool_calls": []},
            {"role": "user", "content": "thanks"}]
        wire = _messages_for_provider("OPENAI", rows, images=True)
        assert [m["role"] for m in wire] == [
            "user", "assistant", "tool", "user", "assistant", "user"]
        assert isinstance(wire[3]["content"], list)

    def test_not_delivered_is_a_note_and_no_extra_message(self):
        wire = _messages_for_provider(
            "OPENAI", _turn(("c1", [IMAGE])), images=False)
        assert [m["role"] for m in wire] == ["user", "assistant", "tool"]
        assert "not sent" in wire[-1]["content"]
        assert IMAGE["data"] not in json.dumps(wire)


class TestGoogleIsDeferred:

    def test_google_gets_the_note_even_when_asked_to_deliver(self):
        wire = _messages_for_provider(
            "GOOGLE", _turn(("c1", [IMAGE])), images=True)
        response = wire[-1].parts[0].function_response.response
        assert "not sent" in response["result"]
        assert IMAGE["data"] not in str(response)


class TestARowWithoutImagesIsUnchanged:

    @pytest.mark.parametrize("provider", ["ANTHROPIC", "OPENAI"])
    def test_the_flag_changes_nothing_for_a_text_result(self, provider):
        rows = _turn(("c1", None))
        assert (_messages_for_provider(provider, rows, images=True)
                == _messages_for_provider(provider, rows, images=False))


class TestTheCallReadsTheFlag:

    def test_call_model_stream_delivers_to_a_flagged_provider(
            self, monkeypatch):
        captured = {}

        def create(**kw):
            captured.update(kw)
            return iter([])

        fake = type("C", (), {})()
        fake.chat = type("Ch", (), {})()
        fake.chat.completions = type("Co", (), {"create": staticmethod(create)})()
        monkeypatch.setattr(client_mod, "load_provider_data",
                            lambda: {"OPENAI": {"accepts_images": True}})
        list(client_mod.call_model_stream(
            fake, "OPENAI", "gpt-test", _turn(("c1", [IMAGE])), "", []))
        assert captured["messages"][-1]["content"][1]["type"] == "image_url"


# ---------------------------------------------------------------------------
# ---- The size proxy (IM8) --------------------------------------------------
# ---------------------------------------------------------------------------

class TestAnImageWeighsAFixedAmount:

    def test_a_message_counts_its_images_and_not_their_length(self):
        text_only = [{"role": "tool", "content": "Attached."}]
        with_image = [dict(text_only[0], images=[IMAGE, IMAGE])]
        assert (compaction._chars(with_image) - compaction._chars(text_only)
                == 2 * compaction.IMAGE_TOKEN_ESTIMATE
                * compaction.CHARS_PER_TOKEN)

    def test_an_archive_row_counts_its_stored_images(self):
        row = {"content": json.dumps("Attached."),
               "images": json.dumps([IMAGE])}
        assert compaction._row_chars(row) == (
            len(row["content"])
            + compaction.IMAGE_TOKEN_ESTIMATE * compaction.CHARS_PER_TOKEN)

    def test_a_row_without_images_is_its_content(self):
        assert compaction._row_chars({"content": "abc", "images": None}) == 3
        assert compaction._row_chars({"content": "abc"}) == 3

    def test_the_summarizer_never_sees_the_bytes(self):
        text = compaction._as_text(
            [{"role": "tool", "content": "Attached s.png", "images": [IMAGE]}])
        assert IMAGE["data"] not in text
        assert "Attached s.png" in text


# ---------------------------------------------------------------------------
# ---- Through the loop (IM3, IM6) -------------------------------------------
# ---------------------------------------------------------------------------

class TestTheLoopCarriesTheRunsAnswer:
    """The pieces above, joined where production joins them: `_run_steps`
    builds RunInfo, hands `images` to the schema list, and dispatches through
    the REAL registry with the memory and call id injected. Only the model
    call is faked, so the gate, the injection and the handler are live."""

    def _drive(self, monkeypatch, kind, responses, accepts=True):
        from core.loop import RunAgentLoop, run_to_completion
        from tests.conftest import FakeMemory, make_stream_sequence

        _enable_view_image(monkeypatch)
        monkeypatch.setattr(loop_mod, "accepts_images", lambda p: accepts)
        seen = []
        stream = make_stream_sequence(*responses)

        def fake_call(client, provider, model, messages, system_prompt,
                      tool_schemas, *args, **kwargs):
            seen.append({s["name"] for s in tool_schemas})
            return stream()

        monkeypatch.setattr(loop_mod, "call_model_stream", fake_call)
        memory = FakeMemory(kind=kind)
        run_to_completion(RunAgentLoop._run(
            memory=memory, system_prompt="s", provider_name="ANTHROPIC",
            model="m", context=None, max_steps=3, max_total_tokens=None,
            response_channel=object()))
        return memory, seen

    def test_a_chat_on_an_image_provider_is_offered_the_tool(
            self, monkeypatch):
        from tests.conftest import make_model_response
        _, seen = self._drive(monkeypatch, storage.THREAD_KIND_CHAT,
                              [make_model_response(text="ok")])
        assert "view_image" in seen[0]

    @pytest.mark.parametrize("kind, accepts", [
        (storage.THREAD_KIND_RESEARCH_PASS, True),
        (storage.THREAD_KIND_CHAT, False)])
    def test_a_run_that_cannot_deliver_is_not(self, monkeypatch, kind,
                                              accepts):
        from tests.conftest import make_model_response
        _, seen = self._drive(monkeypatch, kind,
                              [make_model_response(text="ok")], accepts)
        assert "view_image" not in seen[0]
        assert "web_search" in seen[0]

    def test_a_call_attaches_to_the_turns_memory_under_its_own_id(
            self, monkeypatch, workspace):
        from tests.conftest import make_model_response
        _put(workspace, "s.png", PNG)
        memory, _ = self._drive(monkeypatch, storage.THREAD_KIND_CHAT, [
            make_model_response(tool_calls=[{
                "id": "call_9", "name": "view_image",
                "input": {"path": "s.png"}}]),
            make_model_response(text="A picture.")])
        assert memory.attached_images == [
            ("call_9", "image/png", IMAGE["data"])]
        (call_id, result), = memory.tool_results
        assert call_id == "call_9"
        assert IMAGE["data"] not in json.dumps(result)
