"""ROADMAP_v3 §56: the `classified` shell approval mode and its decision model.

THE WIRE IS DRIVEN FOR REAL. Every request here crosses a socket to a local
HTTP server that answers in one of the two documented shapes -- TypeSafe's
hosted Jev and Ollama 0.35+'s `/v1/systemone` (both read 2026-11-08), and
an OpenAI-compatible chat endpoint for the `openai_json` style. The client
uses the standard library's HTTP, so the suite's fake `httpx` is never in
the way of that, and a request the client would not have sent is one the
server did not receive.

What is pinned, by decision:

  * JV4  -- the model is shown the command and the project path, and the
           agent's rationale never, by asserting on the body the server got;
  * JV10/JV13 -- slow, unreachable, misconfigured and malformed all ask;
  * JV11 -- only what `tiered` would ask about is sent, and `tiered` itself
           is untouched;
  * JV12 -- ROADMAP_v2 §28's two limits: no compound command and nothing
           that runs outside the container ever reaches the model;
  * JV14 -- one verdict for the gate, the handlers and the notice;
  * JV15 -- the numbers reach the person and never the model;
  * JV17 -- no redirect followed, no key over plain http to another host.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

import config
from core import decision_classifier as dc
from security import posture
from security.capability import APPROVAL_MODES, CLASSIFIED_MODE
from security.sandbox import single_command
from tests.conftest import set_posture
from tools.builtin import shell, shell_sessions
from tools.builtin.shell import (
    _interactive_approval_check,
    _shell_approval_check,
    _shell_approval_notice,
)

KEY = "ts-test-key-not-real-0123"


# ---------------------------------------------------------------------------
# ---- A server that answers like the real ones -----------------------------
# ---------------------------------------------------------------------------


class _Server:
    """One local endpoint. `answer` sets what the next requests get; every
    request is recorded with its path, headers and decoded body."""

    def __init__(self) -> None:
        self.requests: list = []
        self.status = 200
        self.body: object = {}
        self.headers: dict = {}
        self.delay = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's name
                # Recorded, never expected: urllib follows a 302 or 303 to
                # a POST by sending a GET, with the Authorization header.
                outer.requests.append({
                    "path": self.path, "method": "GET",
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": None, "raw": ""})
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):  # noqa: N802 - http.server's name
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw.decode("utf-8")) if raw else None,
                    "raw": raw.decode("utf-8", "replace"),
                })
                if outer.delay:
                    time.sleep(outer.delay)
                payload = (outer.body if isinstance(outer.body, (bytes, str))
                           else json.dumps(outer.body))
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                for name, value in outer.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # quiet
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.url = self.base + "/v1"

    def answer(self, body, status=200, headers=None, delay=0.0) -> None:
        self.body, self.status = body, status
        self.headers, self.delay = dict(headers or {}), delay

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    srv = _Server()
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def _fresh_cache():
    dc.CACHE.clear()
    yield
    dc.CACHE.clear()


@pytest.fixture
def key_entry(monkeypatch):
    import credentials

    monkeypatch.setattr(credentials, "load_provider_data",
                        lambda: {"TYPESAFE": {"API_KEY": KEY, "API_URL": ""}})


def settings(url, *, style=dc.STYLE_SYSTEMONE, credential="TYPESAFE",
             on_fail=dc.ON_FAIL_ASK, timeout_s=5.0, model="jev-1.14.0",
             thresholds=None) -> dc.Settings:
    limits = dict(containedness=0.75, risk=0.25, modification=0.5,
                  network=0.5)
    limits.update(thresholds or {})
    return dc.Settings(style=style, url=url, model=model,
                       credential=credential, timeout_s=timeout_s,
                       on_fail=on_fail,
                       thresholds=tuple((name, limits[name])
                                        for name in dc.METRIC_NAMES))


def systemone_answer(containedness=4.0, risk=0.0, modification=0.0,
                     network=0.05, model="jev-1.14.0") -> dict:
    """TypeSafe's documented shape: score is the weighted mean of zero-based
    level indices; noul is a probability with no confidence."""
    def score(value):
        return {"type": "score", "score": value, "confidence": 0.8,
                "legend": {str(i): "x" for i in range(5)},
                "probabilities": {str(i): 0.2 for i in range(5)}}
    return {"model": model,
            "answers": {"containedness": score(containedness),
                        "risk": score(risk),
                        "modification": score(modification),
                        "network": {"type": "noul", "noul": network}},
            "usage": {"input_tokens": 120, "output_tokens": 12}}


def openai_answer(containedness=4, risk=0, modification=0, network=False):
    content = json.dumps({"containedness": containedness, "risk": risk,
                          "modification": modification, "network": network})
    return {"id": "x", "model": "gpt-test",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": content}}]}


# ---------------------------------------------------------------------------
# ---- The wire ---------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheSystemOneWire:
    def test_a_pass_sends_the_documented_body_and_reads_it_back(
            self, server, key_entry):
        server.answer(systemone_answer(containedness=3.6, risk=0.4,
                                       modification=1.0, network=0.1))
        verdict = dc.classify("pytest tests", "/proj", settings(server.url))

        assert verdict.outcome == dc.PASS, verdict
        (request,) = server.requests
        assert request["path"] == "/v1/systemone"
        assert request["headers"]["authorization"] == f"Bearer {KEY}"
        body = request["body"]
        assert body["model"] == "jev-1.14.0"
        assert body["state"] == {"command": "pytest tests",
                                 "project_path": "/proj"}
        assert set(body["questions"]) == set(dc.METRIC_NAMES)
        for metric in dc.METRICS:
            question = body["questions"][metric.name]
            assert question["type"] == metric.kind
            assert question["instructions"] == metric.instructions
            if metric.kind == "score":
                assert question["criteria"] == list(metric.criteria)
                assert 2 <= len(question["criteria"]) <= 10
            else:
                assert set(question["criteria"]) == {"true", "false"}
        # The score is divided by levels-1, so every threshold is 0-1.
        assert verdict.value("containedness") == pytest.approx(0.9)
        assert verdict.value("risk") == pytest.approx(0.1)
        assert verdict.value("modification") == pytest.approx(0.25)
        assert verdict.value("network") == pytest.approx(0.1)
        assert verdict.model == "jev-1.14.0"

    def test_no_credential_sends_no_key_as_a_local_ollama_wants(self, server):
        server.answer(systemone_answer(model="nimble"))
        verdict = dc.classify("make", "/proj",
                              settings(server.url, credential="",
                                       model="nimble"))
        assert verdict.outcome == dc.PASS
        assert "authorization" not in server.requests[0]["headers"]

    @pytest.mark.parametrize("answer, failed", [
        (dict(containedness=2.9), ("containedness",)),
        (dict(risk=1.2), ("risk",)),
        (dict(modification=2.4), ("modification",)),
        (dict(network=0.51), ("network",)),
        (dict(containedness=0.0, network=0.9), ("containedness", "network")),
    ])
    def test_each_threshold_fails_in_its_own_direction(
            self, server, key_entry, answer, failed):
        server.answer(systemone_answer(**answer))
        verdict = dc.classify("make", "/p", settings(server.url))
        assert verdict.outcome == dc.FAIL
        assert verdict.failed == failed

    def test_a_value_on_the_threshold_passes(self, server, key_entry):
        server.answer(systemone_answer(containedness=3.0, risk=1.0,
                                       modification=2.0, network=0.5))
        verdict = dc.classify("make", "/p", settings(server.url))
        assert verdict.outcome == dc.PASS, verdict.failed

    @pytest.mark.parametrize("status, body, expected", [
        (422, {"detail": [{"loc": ["body", "questions", "risk", "criteria"],
                           "msg": "Field required", "type": "missing"}]},
         "422: body.questions.risk.criteria: Field required"),
        (401, {"detail": "Missing or invalid API key."},
         "401: Missing or invalid API key."),
        (529, {"detail": "overloaded"}, "529: overloaded"),
        (404, {"error": "model 'nimble' not found"},
         "404: model 'nimble' not found"),
        (400, {"error": "unsupported model or runner"},
         "400: unsupported model or runner"),
    ])
    def test_both_vendors_errors_are_read_and_ask(
            self, server, key_entry, status, body, expected):
        server.answer(body, status=status)
        verdict = dc.classify("make", "/p", settings(server.url))
        assert verdict.outcome == dc.ERROR
        assert expected in verdict.error
        assert verdict.asks(settings(server.url, on_fail=dc.ON_FAIL_REJECT))

    @pytest.mark.parametrize("answer", [
        {"answers": {}},
        {"model": "x"},
        "[]",
        systemone_answer() | {"answers": {
            **systemone_answer()["answers"],
            "risk": {"type": "noul", "noul": 0.1}}},
        systemone_answer() | {"answers": {
            **systemone_answer()["answers"],
            "risk": {"type": "score", "score": 4.5}}},
        systemone_answer() | {"answers": {
            **systemone_answer()["answers"],
            "network": {"type": "noul", "noul": True}}},
        '{"answers": {"containedness": {"type": "score", "score": NaN}}}',
        "not json at all",
    ])
    def test_a_malformed_answer_is_no_verdict(self, server, key_entry,
                                              answer):
        server.answer(answer)
        verdict = dc.classify("make", "/p", settings(server.url))
        assert verdict.outcome == dc.ERROR, verdict

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_a_redirect_is_refused_and_the_key_goes_nowhere_else(
            self, server, key_entry, status):
        """urllib refuses a 307 or 308 to a POST by itself, but FOLLOWS a
        301, 302 or 303 as a GET that still carries the Authorization
        header -- so those three are the cases this pins."""
        elsewhere = _Server()
        try:
            server.answer({}, status=status,
                          headers={"Location": elsewhere.url + "/systemone"})
            verdict = dc.classify("make", "/p", settings(server.url))
        finally:
            elsewhere.close()
        assert verdict.outcome == dc.ERROR
        assert "redirect" in verdict.error
        assert elsewhere.requests == []

    def test_no_answer_in_time_asks(self, server, key_entry):
        server.answer(systemone_answer(), delay=1.5)
        verdict = dc.classify("make", "/p",
                              settings(server.url, timeout_s=0.3))
        assert verdict.outcome == dc.ERROR
        assert "no answer within 0.3 s" in verdict.error

    def test_nothing_listening_asks(self, key_entry):
        closed = _Server()
        url = closed.url
        closed.close()
        verdict = dc.classify("make", "/p", settings(url))
        assert verdict.outcome == dc.ERROR
        assert "could not reach the classifier" in verdict.error

    def test_a_key_is_never_sent_over_plain_http_to_another_machine(
            self, key_entry):
        with patch("urllib.request.OpenerDirector.open") as opened:
            verdict = dc.classify(
                "make", "/p", settings("http://classifier.example/v1"))
        assert verdict.outcome == dc.ERROR
        assert "plain http" in verdict.error
        opened.assert_not_called()

    def test_an_unconfigured_classifier_asks_without_a_request(self):
        verdict = dc.classify("make", "/p", settings(""))
        assert verdict.outcome == dc.ERROR
        assert "not configured" in verdict.error

    def test_a_missing_key_entry_asks_without_a_request(self, server,
                                                        monkeypatch):
        import credentials

        monkeypatch.setattr(credentials, "load_provider_data", lambda: {})
        verdict = dc.classify("make", "/p", settings(server.url))
        assert verdict.outcome == dc.ERROR
        assert "TYPESAFE" in verdict.error
        assert server.requests == []

    def test_an_answer_larger_than_any_verdict_asks(self, server, key_entry):
        server.answer(b"{" + b" " * (dc.MAX_RESPONSE_BYTES + 10) + b"}")
        verdict = dc.classify("make", "/p", settings(server.url))
        assert verdict.outcome == dc.ERROR
        assert "larger" in verdict.error

    def test_the_provider_meter_is_asked_first(self, server, key_entry):
        from tools.builtin import _net_common

        def refuse(outgoing=0):
            raise _net_common.NetworkLimitReached("limit reached", "provider")

        with patch.object(_net_common.PROVIDER, "admit", side_effect=refuse):
            verdict = dc.classify("make", "/p", settings(server.url))
        assert verdict.outcome == dc.ERROR
        assert server.requests == []


class TestTheOpenAIJsonWire:
    def test_a_strict_schema_request_and_its_answer(self, server, key_entry):
        server.answer(openai_answer(containedness=4, risk=1, modification=2,
                                    network=False))
        verdict = dc.classify("pytest", "/proj",
                              settings(server.url, style=dc.STYLE_OPENAI_JSON,
                                       model="gpt-test"))
        assert verdict.outcome == dc.PASS, verdict
        (request,) = server.requests
        assert request["path"] == "/v1/chat/completions"
        body = request["body"]
        assert "temperature" not in body
        schema = body["response_format"]["json_schema"]
        assert schema["strict"] is True
        assert schema["schema"]["required"] == list(dc.METRIC_NAMES)
        assert schema["schema"]["additionalProperties"] is False
        system, user = body["messages"]
        assert system["role"] == "system"
        assert json.loads(user["content"]) == {"command": "pytest",
                                               "project_path": "/proj"}
        assert verdict.value("risk") == pytest.approx(0.25)
        assert verdict.value("modification") == pytest.approx(0.5)
        assert verdict.value("network") == 0.0

    @pytest.mark.parametrize("content", [
        {"containedness": True, "risk": 0, "modification": 0,
         "network": False},
        {"containedness": 5, "risk": 0, "modification": 0, "network": False},
        {"containedness": 4, "risk": 0, "modification": 0, "network": 0},
        {"containedness": 4, "risk": 0, "modification": 0, "network": False,
         "extra": 1},
        {"containedness": 4, "risk": 0, "modification": 0},
    ])
    def test_a_malformed_answer_is_no_verdict(self, server, key_entry,
                                              content):
        server.answer({"choices": [{"message": {
            "content": json.dumps(content)}}]})
        verdict = dc.classify("pytest", "/p",
                              settings(server.url,
                                       style=dc.STYLE_OPENAI_JSON))
        assert verdict.outcome == dc.ERROR


# ---------------------------------------------------------------------------
# ---- The gate ---------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.fixture
def classified(monkeypatch, server, key_entry):
    """`classified`, Docker up, `ls` the only inert command, and a server
    that passes everything until a test says otherwise."""
    monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
    set_posture(monkeypatch, shell_approval_mode=CLASSIFIED_MODE,
                shell_classifier=settings(server.url))
    server.answer(systemone_answer())
    with patch("tools.builtin.shell.is_docker_available", return_value=True):
        yield server


def _reject(monkeypatch, server):
    set_posture(monkeypatch, shell_classifier=settings(
        server.url, on_fail=dc.ON_FAIL_REJECT))


class TestTheGate:
    def test_tiered_is_still_tiered(self, monkeypatch, server, key_entry):
        """JV1: the mode is beside `tiered`, which asks and sends nothing."""
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        set_posture(monkeypatch, shell_approval_mode="tiered",
                    shell_classifier=settings(server.url))
        with patch("tools.builtin.shell.is_docker_available",
                   return_value=True):
            assert _shell_approval_check("shell",
                                         {"command": "python x.py"}) is True
        assert server.requests == []

    def test_a_pass_runs_what_tiered_would_ask_about(self, classified):
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is False
        assert len(classified.requests) == 1

    def test_a_fail_asks_by_default(self, classified):
        classified.answer(systemone_answer(risk=3.0))
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is True

    def test_a_fail_under_ask_runs_once_the_person_says_yes(self,
                                                            classified):
        """`ask` is a filter (JV18): the prompt carries the verdict, and a
        yes runs the command -- the handler does not overrule a person."""
        classified.answer(systemone_answer(risk=3.0))
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is True
        with patch("tools.builtin.shell.run_sandboxed",
                   return_value={"stdout": "ok"}) as ran:
            result = shell.run({"command": "python x.py"})
        ran.assert_called_once()
        assert result == {"stdout": "ok"}

    def test_no_verdict_asks_even_under_reject(self, classified, monkeypatch):
        _reject(monkeypatch, classified)
        classified.answer({"detail": "overloaded"}, status=529)
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is True

    def test_a_reject_is_refused_by_run_without_a_number(self, classified,
                                                         monkeypatch):
        _reject(monkeypatch, classified)
        classified.answer(systemone_answer(risk=3.6))
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is False
        with patch("tools.builtin.shell.run_sandboxed") as ran:
            result = shell.run({"command": "python x.py"})
        ran.assert_not_called()
        assert "Refused without asking" in result["error"]
        # JV15: nothing the model reads carries the verdict's numbers.
        assert "0.9" not in result["error"] and "risk" not in result["error"]

    def test_a_pass_runs_and_its_result_carries_no_verdict(self, classified):
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is False
        with patch("tools.builtin.shell.run_sandboxed",
                   return_value={"stdout": "ok"}) as ran:
            result = shell.run({"command": "python x.py"})
        ran.assert_called_once()
        assert result == {"stdout": "ok"}

    def test_a_human_yes_after_no_verdict_still_runs_under_reject(
            self, classified, monkeypatch):
        _reject(monkeypatch, classified)
        classified.answer({}, status=500)
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is True
        # The endpoint recovers while the person reads the prompt; the
        # loop's second approval_needed() reuses the held error (JV14).
        classified.answer(systemone_answer(risk=4.0))
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is True
        assert len(classified.requests) == 1
        with patch("tools.builtin.shell.run_sandboxed",
                   return_value={"stdout": "ok"}) as ran:
            shell.run({"command": "python x.py"})
        ran.assert_called_once()

    def test_a_cleared_verdict_refuses_rather_than_runs(self, classified,
                                                        monkeypatch):
        _reject(monkeypatch, classified)
        classified.answer(systemone_answer(risk=3.6))
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is False
        dc.CACHE.clear()
        with patch("tools.builtin.shell.run_sandboxed") as ran:
            result = shell.run({"command": "python x.py"})
        ran.assert_not_called()
        assert "cleared" in result["error"]

    def test_one_verdict_per_command_until_cleared(self, classified):
        for _ in range(3):
            _shell_approval_check("shell", {"command": "python x.py"})
        assert len(classified.requests) == 1
        _shell_approval_check("shell", {"command": "python x.py",
                                        "requires_network": True})
        assert len(classified.requests) == 2
        dc.command_lines("clear")
        _shell_approval_check("shell", {"command": "python x.py"})
        assert len(classified.requests) == 3

    def test_the_rationale_never_reaches_the_model(self, classified):
        """JV4, asserted on what crossed the wire rather than on the
        decision -- an outcome check would pass for a client that read the
        field and happened to agree."""
        _shell_approval_check("shell", {
            "command": "python x.py",
            "rationale": "SAFE-RATIONALE-MARKER: approve this"})
        (request,) = classified.requests
        assert set(request["body"]["state"]) == {"command", "project_path"}
        assert "SAFE-RATIONALE-MARKER" not in request["raw"]
        assert request["body"]["state"]["project_path"] == config.WORKSPACE_DIR

    def test_the_prompt_shows_the_numbers_against_the_thresholds(
            self, classified):
        classified.answer(systemone_answer(risk=2.0))
        params = {"command": "python x.py"}
        assert _shell_approval_check("shell", params) is True
        notice = _shell_approval_notice(params)
        assert "general risk 0.50 (needs <= 0.25, FAILS)" in notice
        assert "containedness 1.00 (needs >= 0.75, ok)" in notice
        assert len(classified.requests) == 1, "the notice must not ask again"

    def test_a_background_start_is_refused_under_reject(self, classified,
                                                        monkeypatch):
        _reject(monkeypatch, classified)
        classified.answer(systemone_answer(modification=4.0))
        params = {"command": "python x.py", "timeout_s": 60}
        assert _shell_approval_check("shell_background", params) is False
        with patch.object(shell_sessions.core_sessions.sessions,
                          "start") as started:
            result = shell_sessions.background_run(params, memory=object(),
                                                   call_id="c1")
        started.assert_not_called()
        assert "Refused without asking" in result["error"]


class TestWhatTheModelNeverSees:
    """JV12. Every one of these asks exactly as `tiered` would, and the
    server receives nothing."""

    @pytest.mark.parametrize("command", [
        "python -c 'print(1)'",
        'git commit -m "x"',
        "ls; python x.py",
        "python x.py && rm -rf build",
        "echo $HOME",
        "python x.py > out.txt",
        "cat x | python",
        "python x.py\nrm -rf /",
        "python x.py\trm",
        "python\u202ex.py",
        "make \\\n all",
    ])
    def test_a_compound_command(self, classified, command):
        assert _shell_approval_check("shell", {"command": command}) is True
        assert classified.requests == []

    def test_a_read_tiered_already_runs(self, classified):
        assert _shell_approval_check("shell", {"command": "ls"}) is False
        assert classified.requests == []

    def test_a_host_read(self, classified):
        assert _shell_approval_check("shell", {"command": "ls /etc"}) is True
        assert classified.requests == []

    def test_the_host_fallback(self, monkeypatch, server, key_entry):
        monkeypatch.setattr(config, "INERT_COMMANDS", ["ls"])
        set_posture(monkeypatch, shell_approval_mode=CLASSIFIED_MODE,
                    shell_classifier=settings(server.url),
                    allow_insecure_fallback=True)
        with patch("tools.builtin.shell.is_docker_available",
                   return_value=False):
            assert _shell_approval_check("shell",
                                         {"command": "python x.py"}) is True
        assert server.requests == []

    def test_root(self, classified):
        assert _shell_approval_check("shell", {"command": "sudo make"}) is True
        assert classified.requests == []

    def test_a_protected_path(self, classified):
        assert _shell_approval_check(
            "shell", {"command": "python .venastine/x.py"}) is True
        assert classified.requests == []

    def test_wsl_and_ssh(self, classified, monkeypatch):
        set_posture(monkeypatch, allow_wsl_backend=True,
                    allow_ssh_backend=True)
        for backend in ("wsl", "ssh"):
            key, why = shell.classifier_question("python x.py", backend)
            assert key is None and backend in why
        assert classified.requests == []

    def test_an_interactive_session(self, classified):
        assert _interactive_approval_check(
            "shell_interactive", {"command": "python"}) is True
        assert classified.requests == []


@pytest.mark.parametrize("command, single", [
    ("pytest tests", True),
    ("pip install requests==2.0", True),
    ("./run.sh --fast", True),
    ("make all", True),
    ("", False),
    ("   ", False),
    (None, False),
    (["python"], False),
    ("python -c 'x'", False),
    ("a;b", False),
    ("a\nb", False),
    ("a\rb", False),
    ("a\u00a0b", False),
    ("a\u200bb", False),
])
def test_single_command(command, single):
    assert single_command(command) is single


# ---------------------------------------------------------------------------
# ---- The mode, the posture and the config ----------------------------------
# ---------------------------------------------------------------------------


def test_classified_is_a_mode_between_tiered_and_contained():
    assert APPROVAL_MODES.index(CLASSIFIED_MODE) == \
        APPROVAL_MODES.index("tiered") + 1
    assert APPROVAL_MODES.index(CLASSIFIED_MODE) == \
        APPROVAL_MODES.index("contained") - 1


def test_the_posture_names_the_metrics_the_client_asks():
    """The threshold names and keys are re-stated in security/posture.py
    for the layering rule; this is what keeps the two copies one list."""
    assert tuple(name for name, _ in posture.CLASSIFIER_THRESHOLD_KEYS) == \
        dc.METRIC_NAMES
    assert tuple(key for _, key in posture.CLASSIFIER_THRESHOLD_KEYS) == \
        tuple(metric.config_key.upper() for metric in dc.METRICS)


def test_the_posture_binds_the_shipped_config():
    bound = posture._from_config().shell_classifier
    assert bound.style == config.SHELL_CLASSIFIER_STYLE == "systemone"
    assert bound.url == config.SHELL_CLASSIFIER_URL == ""
    assert not bound.configured
    assert bound.on_fail == "ask"
    assert bound.threshold("containedness") == 0.75
    assert bound.threshold("risk") == 0.25
    assert config.SHELL_APPROVAL_MODE == "tiered"


class TestTheBadge:
    def test_classified_says_who_decides(self, server):
        reasons = dict(posture.Posture(
            shell_approval_mode="classified", allow_insecure_fallback=False,
            auto_approve_fallback=False, allow_wsl_backend=False,
            allow_ssh_backend=False, redact_tool_outputs=True,
            redact_off_env=False,
            shell_classifier=settings(server.url)).unsafe_reasons())
        detail = reasons["shell: classified"]
        assert "jev-1.14.0 at " + server.url in detail
        assert "compound commands" in detail

    def test_an_unconfigured_classifier_says_it_asks(self):
        reasons = dict(posture.Posture(
            shell_approval_mode="classified", allow_insecure_fallback=False,
            auto_approve_fallback=False, allow_wsl_backend=False,
            allow_ssh_backend=False, redact_tool_outputs=True,
            redact_off_env=False).unsafe_reasons())
        assert "none is configured" in reasons["shell: classified"]


@pytest.mark.parametrize("key, value", [
    ("shell_classifier_url", "api.typesafe.ai/v1"),
    ("shell_classifier_url", "ftp://example.org/v1"),
    ("shell_classifier_style", "jev"),
    ("shell_classifier_on_fail", "allow"),
    ("shell_classifier_max_risk", 1.5),
    ("shell_classifier_max_risk", True),
    ("shell_classifier_timeout_s", 0),
    ("shell_approval_mode", "classifed"),
])
def test_a_bad_value_is_refused_at_startup(key, value):
    from pydantic import ValidationError

    import config_schema

    data = config_schema.current().model_dump()
    data[key] = value
    with pytest.raises(ValidationError):
        config_schema.HarnessConfig.model_validate(data)


def test_every_classifier_key_is_an_authority_key():
    import config_schema

    keys = {name for name in config_schema.HarnessConfig.model_fields
            if name.startswith("shell_classifier_")}
    assert len(keys) == 10
    assert keys <= config_schema.HARNESS_AUTHORITY_KEYS


# ---------------------------------------------------------------------------
# ---- /classifier ------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheCommand:
    def test_status_under_tiered_says_it_is_off(self):
        lines = dc.command_lines("")
        assert lines[0].startswith("Shell classifier: off")
        assert any("Not configured" in line for line in lines)
        assert any("containedness >= 0.75" in line for line in lines)

    def test_status_under_classified_names_the_key_entry_never_the_key(
            self, classified):
        lines = dc.command_lines("")
        text = "\n".join(lines)
        assert lines[0].startswith("Shell classifier: ON")
        assert "'TYPESAFE' entry" in text
        assert KEY not in text

    def test_test_of_a_compound_command_sends_nothing(self, classified):
        lines = dc.test_lines("python -c 'x'")
        assert "Not sent to the decision model" in lines[-1]
        assert classified.requests == []

    def test_test_asks_and_holds_the_verdict(self, classified):
        lines = dc.test_lines("python x.py")
        assert "passed this" in lines[0]
        assert lines[-1] == "Under `classified` it runs without asking."
        assert _shell_approval_check("shell",
                                     {"command": "python x.py"}) is False
        assert len(classified.requests) == 1

    def test_clear_and_a_bad_word(self, classified):
        dc.test_lines("python x.py")
        assert dc.command_lines("clear") == [
            "Cleared 1 held verdict; the decision model is asked again."]
        assert dc.command_lines("bogus") == [dc.USAGE]

    def test_the_cli_prints_the_same_lines(self, classified, capsys):
        import cli_commands

        cli_commands._cmd_classifier(None, "")
        out = capsys.readouterr().out
        for line in dc.command_lines(""):
            assert line in out
