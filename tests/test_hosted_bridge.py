"""
test_hosted_bridge.py

ROADMAP_v3 §79 (HD1-HD3, HD7-HD9, HD11): the bridge under deploy/hosted/ --
the process a Space runs, which builds the sessions' environment, starts one
harness per browser tab and relays it.

Nothing here imports gradio or spaces: app.py is the only module that does,
and everything it wires together is reachable without it. The relay is
driven over a real pipe against tests/_bridge_child.py, a scripted stand-in
for `main.py --tui` that speaks the same framing, so a session's whole life
-- spawn, handshake, both directions, the tab closing, the directory going
-- runs in a second rather than starting a harness.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.hosted import session_env, single_user, supervisor, web

CHILD = Path(__file__).resolve().parent / "_bridge_child.py"


def _lock(tmp_path: Path, text: str = "x==1 --hash=sha256:00\n") -> Path:
    lock = tmp_path / "session.lock"
    lock.write_text(text, encoding="utf-8")
    return lock


class _FakeUv:
    """Records uv's invocations; `venv` creates the interpreter file."""

    def __init__(self, fail_on=None, stderr="boom"):
        self.calls = []
        self.fail_on = fail_on
        self.stderr = stderr

    def __call__(self, args, env=None, capture_output=True, text=True,
                 check=False):
        self.calls.append((args, env))
        verb = args[3]
        if verb == self.fail_on:
            return type("R", (), {"returncode": 2, "stderr": self.stderr,
                                  "stdout": ""})()
        if verb == "venv":
            python = session_env._python_in(Path(args[-1]))
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()


class TestTheSessionEnvironment:
    def test_a_new_lock_builds_the_environment_with_hashes_and_no_builds(
            self, tmp_path):
        lock, uv = _lock(tmp_path), _FakeUv()
        python = session_env.ensure(lock, tmp_path / "env", run=uv,
                                    base_env={"PATH": "/bin"})
        assert python == session_env._python_in(tmp_path / "env")
        venv, install = (args for args, _ in uv.calls)
        assert venv[3] == "venv"
        assert install[3:5] == ["pip", "install"]
        # Every file checked against the lock, and no sdist built: nothing
        # PyPI serves runs code during a container-start install.
        assert "--require-hashes" in install and "--no-build" in install
        assert install[install.index("-r") + 1] == str(lock)
        marker = (tmp_path / "env" / session_env.MARKER).read_text(
            encoding="utf-8").strip()
        assert marker == hashlib.sha256(lock.read_bytes()).hexdigest()

    def test_the_same_lock_reuses_it(self, tmp_path):
        lock = _lock(tmp_path)
        session_env.ensure(lock, tmp_path / "env", run=_FakeUv(), base_env={})
        again = _FakeUv()
        session_env.ensure(lock, tmp_path / "env", run=again, base_env={})
        assert again.calls == []

    def test_a_changed_lock_rebuilds_it(self, tmp_path):
        lock = _lock(tmp_path)
        session_env.ensure(lock, tmp_path / "env", run=_FakeUv(), base_env={})
        lock.write_text("x==2 --hash=sha256:11\n", encoding="utf-8")
        again = _FakeUv()
        session_env.ensure(lock, tmp_path / "env", run=again, base_env={})
        assert [args[3] for args, _ in again.calls] == ["venv", "pip"]

    def test_a_failed_install_says_why_and_leaves_no_marker(self, tmp_path):
        """The Space's log is where an operator looks; uv's words go there,
        and the next start tries again rather than trusting a half-built
        environment."""
        uv = _FakeUv(fail_on="pip", stderr="hash mismatch for x")
        with pytest.raises(session_env.SessionEnvError, match="hash mismatch"):
            session_env.ensure(_lock(tmp_path), tmp_path / "env", run=uv,
                               base_env={})
        assert not (tmp_path / "env" / session_env.MARKER).exists()

    def test_uv_gets_none_of_the_deployments_secrets(self, tmp_path):
        uv = _FakeUv()
        base = {"PATH": "/bin", "HOME": "/home/user", "HF_TOKEN": "hf_x",
                "OAUTH_CLIENT_SECRET": "s", "VENASTINE_PROVIDERS_JSON": "{}",
                "MODELSCOPE_API_KEY": "ms-x", "PYTHONPATH": "/evil"}
        session_env.ensure(_lock(tmp_path), tmp_path / "env", run=uv,
                           base_env=base)
        for _, env in uv.calls:
            assert set(env) == {"PATH", "HOME"}

    def test_where_it_lives(self, tmp_path):
        assert session_env.default_dir({"VENASTINE_SESSION_ENV": "/x/env"}) \
            == Path("/x/env")
        assert session_env.default_dir({"HOME": str(tmp_path)}, euid=1000) \
            == tmp_path / ".venastine-session-env"

    def test_as_root_it_lives_where_nobody_can_import_from(self, tmp_path):
        """Measured on the Space: HOME was /root at mode 700, and a math
        child dropped to `nobody` could not import SymPy from beneath it."""
        assert session_env.default_dir({"HOME": "/root"}, euid=0) \
            == session_env.ROOT_DEFAULT_DIR
        assert session_env.default_dir({"VENASTINE_SESSION_ENV": "/x/env"},
                                       euid=0) == Path("/x/env")


class TestTheConfinementVerdict:
    """HD11: the bridge runs security/confine.py's self-test and hands every
    session the verdict; anything short of a clean pass is "" -- which a
    shared session reads as "refuse the math tools"."""

    def test_a_clean_pass_is_verified(self, monkeypatch, tmp_path):
        from deploy.hosted import isolation_report
        from security import confine, hosted

        monkeypatch.setattr(confine, "supported", lambda: None)
        verdict = isolation_report.verify_confinement(
            tmp_path / "python", tmp_path,
            verify=lambda python, root: {"ok": True, "checks": {}})
        assert verdict == hosted.CONFINEMENT_VERIFIED

    @pytest.mark.parametrize("verify", [
        lambda python, root: {"ok": False, "reason": "allowed: ['fork']"},
        lambda python, root: {},
        lambda python, root: 1 / 0,
    ])
    def test_anything_else_is_not(self, monkeypatch, tmp_path, verify):
        from deploy.hosted import isolation_report
        from security import confine

        monkeypatch.setattr(confine, "supported", lambda: None)
        assert isolation_report.verify_confinement(
            tmp_path / "python", tmp_path, verify=verify) == ""

    def test_no_session_interpreter_or_an_unsupported_host_is_not(
            self, monkeypatch, tmp_path):
        from deploy.hosted import isolation_report
        from security import confine

        ran = []
        verify = lambda python, root: ran.append(1) or {"ok": True}  # noqa: E731
        assert isolation_report.verify_confinement(None, tmp_path,
                                                   verify=verify) == ""
        monkeypatch.setattr(confine, "supported", lambda: "not x86_64")
        assert isolation_report.verify_confinement(
            tmp_path / "python", tmp_path, verify=verify) == ""
        assert ran == []

    def test_the_verdict_reaches_every_session_and_nothing_else_sets_it(
            self, tmp_path):
        from deploy.hosted.supervisor import SessionPaths, session_environment

        paths = SessionPaths.under(tmp_path)
        base = {"PATH": "/bin", "VENASTINE_CONFINEMENT": "verified"}
        assert "VENASTINE_CONFINEMENT" not in session_environment(
            base, paths, "shared")
        assert session_environment(base, paths, "shared",
                                   confinement="verified")[
            "VENASTINE_CONFINEMENT"] == "verified"
        bridge = web.build_bridge(env={}, root=tmp_path, confinement="verified")
        assert bridge.supervisor.confinement == "verified"

    def test_the_report_states_facts_without_raising(self, tmp_path, caplog):
        from deploy.hosted import isolation_report

        facts = isolation_report.facts(None, tmp_path)
        assert {"kernel", "uid", "capabilities", "landlock_abi",
                "app_root"} <= set(facts)
        with caplog.at_level("INFO"):
            isolation_report.log(None, tmp_path)
        assert "isolation: kernel:" in caplog.text


def _request(xff=None, peer="10.0.0.5"):
    headers = {"x-request-id": "r1"}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return type("Request", (), {
        "headers": headers,
        "client": type("Client", (), {"host": peer})()})()


class TestTheAddressTheRateLimitCounts:
    def test_the_proxys_entry_not_the_clients(self):
        """Measured on the Space (2026-10-20): Hugging Face's proxy APPENDS
        the real address, so the first entry is whatever the client sent."""
        assert web.Bridge._client(_request("198.51.100.23, 203.0.113.9")) \
            == "203.0.113.9"
        assert web.Bridge._client(_request("203.0.113.9")) == "203.0.113.9"

    def test_without_the_header_it_is_the_peer(self):
        assert web.Bridge._client(_request()) == "10.0.0.5"
        assert web.Bridge._client(_request(" , ")) == "10.0.0.5"

    def test_a_forged_first_entry_cannot_dodge_the_limit(self, tmp_path):
        bridge = web.build_bridge(env={}, root=tmp_path)
        issued = [bridge.issue_ticket(bridge._client(_request(
            f"198.51.100.{n}, 203.0.113.9"))) for n in range(30)]
        assert issued.count(None) == 30 - bridge.tickets_per_minute

    def test_the_proxy_shape_names_kinds_never_addresses(self, tmp_path,
                                                         caplog):
        bridge = web.build_bridge(env={}, root=tmp_path)
        with caplog.at_level("INFO"):
            for _ in range(web.PROXY_SHAPES_LOGGED + 2):
                bridge.note_proxy_shape(_request("198.51.100.23, 8.8.8.8"))
        lines = [r.message for r in caplog.records
                 if r.message.startswith("proxy shape")]
        assert len(lines) == web.PROXY_SHAPES_LOGGED
        assert "['probe', 'public']" in lines[0]
        assert "peer private" in lines[0]
        assert "x-request-id" in lines[0]
        assert "8.8.8.8" not in caplog.text
        assert "198.51.100.23" not in caplog.text
        assert "10.0.0.5" not in caplog.text


class TestTheBridgeRunsSessionsOnThatEnvironment:
    def test_a_session_interpreter_becomes_the_command(self, tmp_path):
        python = tmp_path / "env" / "bin" / "python"
        bridge = web.build_bridge(env={}, root=tmp_path, python=python)
        assert bridge.supervisor.command == [
            str(python), str(tmp_path / "main.py"), "--tui"]

    def test_without_one_sessions_run_on_this_interpreter(self, tmp_path):
        bridge = web.build_bridge(env={}, root=tmp_path)
        assert bridge.supervisor.command is None

    def test_a_built_bundle_is_the_only_place_a_lock_exists(self):
        """app.py builds the environment only when the lock is there, so a
        checkout -- and this suite -- never runs uv by accident."""
        assert not session_env.LOCK.exists(), (
            "deploy/hosted/session.lock is generated into a BUNDLE by "
            "scripts/build_demo_bundle.py; one in the checkout makes a local "
            "app.py run build an environment from a stale lock")
        assert os.path.basename(session_env.LOCK) == "session.lock"


# ---------------------------------------------------------------------------
# The relay, against tests/_bridge_child.py
# ---------------------------------------------------------------------------

def _base_env() -> dict:
    """What a Python child needs to start here, and nothing else."""
    return {name: os.environ[name] for name in ("PATH", "SYSTEMROOT")
            if name in os.environ}


def _supervisor(tmp_path, mode="echo", *, max_sessions=4, startup_s=30):
    limits = supervisor.Limits(max_sessions=max_sessions, idle_s=600,
                               lifetime_s=600, memory_mb=2048, cpu_s=120,
                               startup_s=startup_s)
    return supervisor.Supervisor(
        limits, "shared", session_root=tmp_path / "sessions",
        base_env=_base_env(), command=[sys.executable, str(CHILD), mode],
        tick_s=0.05)


class _FakeSocket:
    """The four methods the supervisor relays through; the browser's side
    is the inbox, and None in it is the tab closing."""

    def __init__(self) -> None:
        self.binary: list = []
        self.texts: list = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send_bytes(self, data: bytes) -> None:
        self.binary.append(data)

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def receive_text(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise supervisor.SocketClosed()
        return item

    async def close(self) -> None:
        self.closed = True

    def screen(self) -> bytes:
        return b"".join(self.binary)


async def _until(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


class TestTheFraming:
    def test_a_packet_split_across_reads_is_still_one_packet(self):
        """A pipe hands over whatever it has; one byte at a time is the
        worst case of the ordinary one."""
        data = (supervisor.encode_packet(b"D", b"hello")
                + supervisor.encode_meta({"type": "exit"}))
        reader = supervisor.PacketReader()
        packets = []
        for index in range(len(data)):
            packets += reader.feed(data[index:index + 1])
        assert packets == [(b"D", b"hello"), (b"M", b'{"type": "exit"}')]


class TestTheChildsEnvironment:
    def test_it_carries_nothing_of_the_deployments(self, tmp_path):
        """HD3: built from an allowlist, never copied. Every name below is
        one a Space or an operator could hold."""
        base = {"PATH": "/bin", "HF_TOKEN": "hf_x", "OAUTH_CLIENT_ID": "c",
                "OAUTH_CLIENT_SECRET": "s", "MODELSCOPE_API_KEY": "m",
                "SPACE_ID": "a/b", "SPACE_HOST": "x.hf.space",
                "VENASTINE_PROVIDERS_JSON": "{}", "OPENAI_API_KEY": "sk",
                "AWS_SECRET_ACCESS_KEY": "a", "PYTHONPATH": "/evil",
                "VENASTINE_SINGLE_USER": "1", "VENASTINE_CONFINEMENT": "verified"}
        env = supervisor.session_environment(
            base, supervisor.SessionPaths.under(tmp_path), "shared")
        assert set(env) & set(base) == {"PATH"}
        assert "PYTHONPATH" not in env
        assert env["VENASTINE_HOSTED"] == "shared"
        for name in ("HOME", "TMPDIR", "APP_DB_PATH", "AGENT_LOG_FILE",
                     "AGENT_WORKSPACE", "AGENT_OUTPUT_DIR",
                     "AGENT_PROVIDERS_FILE", "AGENT_SECRETS_FILE",
                     "AGENT_ENV_FILE"):
            assert Path(env[name]).is_relative_to(tmp_path), name

    def test_the_session_directories_are_private(self, tmp_path):
        paths = supervisor.SessionPaths.under(tmp_path / "s")
        if os.name == "posix":
            for directory in (paths.home, paths.workspace, paths.tmp,
                              paths.logs):
                assert stat.S_IMODE(directory.stat().st_mode) == 0o700


class TestASession:
    @pytest.mark.asyncio
    async def test_it_relays_both_ways_and_leaves_nothing(self, tmp_path):
        sup = _supervisor(tmp_path)
        sock = _FakeSocket()
        task = asyncio.ensure_future(sup.open(sock, 100, 30))
        assert await _until(lambda: b"hello" in sock.screen()), \
            "the preamble before __GANGLION__ was not skipped"
        root = next(iter(sup.sessions.values())).paths.root
        assert root.is_dir()

        sock.inbox.put_nowait(json.dumps(["stdin", "abc"]))
        # Clamped to what a terminal can be, whatever the page claims.
        sock.inbox.put_nowait(json.dumps(["resize", {"width": 9999,
                                                     "height": 1}]))
        sock.inbox.put_nowait(json.dumps(["ping", "p1"]))
        sock.inbox.put_nowait("not json")
        assert await _until(lambda: b"echo:abc" in sock.screen()
                            and b"size:500x6" in sock.screen())
        assert await _until(lambda: json.dumps(["pong", "p1"]) in sock.texts)

        sock.inbox.put_nowait(None)                       # the tab closes
        session = await asyncio.wait_for(task, 30)
        assert session.ended_because == "the tab closed"
        assert session.process.returncode == 0, "it was not asked to quit"
        assert not root.exists()
        assert sup.sessions == {} and sock.closed

        # Only a web address reaches window.open() in the page.
        opened = [json.loads(t) for t in sock.texts if "open_url" in t]
        assert opened == [["open_url", {"url": "https://example.org/x",
                                        "new_tab": True}]]

    @pytest.mark.asyncio
    async def test_one_that_never_starts_says_why_in_the_log(self, tmp_path,
                                                            caplog):
        """Before any visitor typed anything, so the deployment's log may
        quote its stderr; the reader of the tab is told it did not start."""
        sup = _supervisor(tmp_path, "no-handshake")
        sock = _FakeSocket()
        with caplog.at_level(logging.WARNING, logger="deploy.hosted.supervisor"):
            session = await asyncio.wait_for(sup.open(sock), 30)
        assert session.ended_because == "the session did not start"
        assert "did not start" in caplog.text
        assert "boom: the harness could not import its config" in caplog.text
        assert b"[the session did not start]" in sock.screen()
        assert not session.paths.root.exists()

    @pytest.mark.asyncio
    async def test_a_full_house_refuses_before_spawning_anything(self,
                                                                 tmp_path):
        sup = _supervisor(tmp_path, max_sessions=1)
        sup.sessions["taken"] = object()
        with pytest.raises(supervisor.CapacityError):
            await sup.open(_FakeSocket())
        assert not (tmp_path / "sessions").exists()

    def test_the_providers_seed_is_an_object_written_once_0600(self, tmp_path,
                                                               caplog):
        """HD7, single-user only: the operator's secret becomes the session's
        providers.json -- and a malformed one is reported, never written."""
        sup = _supervisor(tmp_path)
        paths = supervisor.SessionPaths.under(tmp_path / "s")
        sup.providers_seed = '{"OPENROUTER": {"API_KEY": "k"}}'
        sup._seed_providers(paths)
        target = paths.root / "providers.json"
        assert json.loads(target.read_text(encoding="utf-8")) \
            == {"OPENROUTER": {"API_KEY": "k"}}
        if os.name == "posix":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
        sup.providers_seed = '{"OTHER": {}}'
        sup._seed_providers(paths)
        assert "OTHER" not in target.read_text(encoding="utf-8")
        for index, bad in enumerate(("not json", "[1, 2]")):
            fresh = supervisor.SessionPaths.under(tmp_path / f"bad{index}")
            sup.providers_seed = bad
            with caplog.at_level(logging.WARNING):
                sup._seed_providers(fresh)
            assert not (fresh.root / "providers.json").exists()
        assert "VENASTINE_PROVIDERS_JSON is not" in caplog.text

    @pytest.mark.asyncio
    async def test_a_delivered_file_streams_under_its_name_alone(self,
                                                                 tmp_path):
        from textual._binary_encode import dump

        sup = _supervisor(tmp_path)
        session = supervisor.Session(
            sup, supervisor.SessionPaths.under(tmp_path / "s"), keep_dir=False)
        other = supervisor.Session(
            sup, supervisor.SessionPaths.under(tmp_path / "o"), keep_dir=False)
        sock = _FakeSocket()
        meta = json.dumps({"type": "deliver_file_start", "key": "k1",
                           "path": '../../etc/pass"wd;x.txt',
                           "mime_type": "text/plain", "encoding": "utf-8"})
        assert await session._on_meta(sock, meta.encode()) is False
        assert sup.downloads["k1"].name == "passwdx.txt"
        assert json.dumps(["deliver_file_start", "k1"]) in sock.texts
        # Another session cannot write into this one's download.
        other._on_packed(dump(["deliver_chunk", "k1", "not theirs"]))
        session._on_packed(dump(["deliver_chunk", "k1", "line one"]))
        session._on_packed(dump(["deliver_chunk", "k1", b""]))
        session._on_packed(b"not a packet")
        chunks = [chunk async for chunk in sup.download("k1")]
        assert chunks == [b"line one"]
        assert "k1" not in sup.downloads


class TestWhoMayOpenASession:
    def test_a_websocket_must_come_from_this_page(self, tmp_path):
        bridge = web.build_bridge(env={"SPACE_HOST": "demo.hf.space"},
                                  root=tmp_path)
        assert not bridge.origin_allowed({})
        assert not bridge.origin_allowed({"origin": "null"})
        assert not bridge.origin_allowed({"origin": "https://evil.example"})
        assert bridge.origin_allowed({"origin": "https://demo.hf.space"})
        assert bridge.origin_allowed({"origin": "http://localhost:7860",
                                      "host": "localhost:7860"})

    def test_a_ticket_opens_once_and_not_late(self, tmp_path, monkeypatch):
        bridge = web.build_bridge(env={}, root=tmp_path)
        now = [1000.0]
        monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: now[0]))
        first, second = bridge.issue_ticket("a"), bridge.issue_ticket("a")
        assert bridge.consume_ticket(first)
        assert not bridge.consume_ticket(first)
        now[0] += web.TICKET_TTL_S + 1
        assert not bridge.consume_ticket(second)
        assert not bridge.consume_ticket("forged")


class TestTheSingleUserGate:
    """HD7: honoured only while no stranger can reach the app, and refused
    whenever that cannot be decided."""

    @pytest.mark.parametrize("answer, allowed", [
        ((200, f"<p>{single_user.HEALTH_MARKER}</p>"), False),
        ((302, ""), True),
        ((404, "Not found"), True),
        ((200, "a sign-in page"), True),
    ])
    def test_it_runs_only_while_no_stranger_can_reach_it(self, answer, allowed):
        gate = single_user.ReachabilityGate("https://x.hf.space",
                                            get=lambda url: answer)
        assert gate.allowed() is allowed

    def test_no_url_or_no_answer_refuses(self):
        def unreachable(url):
            raise OSError("no route")

        assert not single_user.ReachabilityGate(None).allowed()
        gate = single_user.ReachabilityGate("https://x", get=unreachable)
        assert not gate.allowed()
        assert "could not tell" in gate.reason

    def test_the_answer_is_rechecked_every_minute(self):
        calls, now = [], [0.0]
        gate = single_user.ReachabilityGate(
            "https://x", get=lambda url: calls.append(url) or (404, ""),
            clock=lambda: now[0])
        gate.allowed()
        gate.allowed()
        now[0] += single_user.CACHE_S
        gate.allowed()
        assert calls == ["https://x/healthz"] * 2

    def test_the_unlock_writes_config_yaml_once(self, tmp_path, monkeypatch):
        import config_edit
        import config_schema

        target = tmp_path / "config.yaml"
        shutil.copyfile(config_schema.CONFIG_PATH, target)
        monkeypatch.setattr(config_schema, "CONFIG_PATH", str(target))
        assert single_user.unlock_config() == list(single_user.UNLOCKED)
        for name in single_user.UNLOCKED:
            assert config_edit.file_value(name) is True, name
        assert single_user.unlock_config() == []


class TestTheRoutes:
    """The bridge's routes through Starlette's own TestClient, with the
    stand-in child behind the websocket."""

    def _client(self, bridge):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        return TestClient(Starlette(routes=bridge.routes()))

    def test_end_to_end(self, tmp_path):
        from starlette.websockets import WebSocketDisconnect

        bridge = web.build_bridge(env={}, root=tmp_path)
        bridge.supervisor = _supervisor(tmp_path)
        with self._client(bridge) as client:
            landing = client.get("/")
            assert landing.status_code == 200
            assert landing.headers["x-content-type-options"] == "nosniff"
            assert client.get("/healthz").json() == {
                "app": single_user.HEALTH_MARKER, "ok": True}
            assert client.get("/download/nope").status_code == 404
            page = client.get("/terminal")
            ticket = re.search(r'data-ticket="([^"]+)"', page.text).group(1)

            with pytest.raises(WebSocketDisconnect) as refused:
                with client.websocket_connect(
                        f"/ws/{ticket}",
                        headers={"origin": "https://evil.example"}) as ws:
                    ws.receive_bytes()
            assert refused.value.code == 4403

            with client.websocket_connect(
                    f"/ws/{ticket}?width=100&height=30",
                    headers={"origin": "http://testserver"}) as ws:
                assert ws.receive_bytes() == b"hello"
                ws.send_text(json.dumps(["stdin", "hi"]))
                received = b""
                while b"echo:hi" not in received:
                    message = ws.receive()
                    received += message.get("bytes") or b""

            with pytest.raises(WebSocketDisconnect) as reused:
                with client.websocket_connect(
                        f"/ws/{ticket}",
                        headers={"origin": "http://testserver"}) as ws:
                    ws.receive_bytes()
            assert reused.value.code == 4401

            # Closing the socket CANCELS the handler under the TestClient;
            # the stop is shielded, so it finishes while the client's loop
            # is still running -- which is why this waits inside the `with`.
            sessions = tmp_path / "sessions"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and (
                    bridge.supervisor.sessions or any(sessions.iterdir())):
                time.sleep(0.05)
            left = sorted(str(p.relative_to(sessions))
                          for p in sessions.rglob("*"))
        assert bridge.supervisor.sessions == {}
        assert left == [], left

    @pytest.mark.parametrize("answer, status", [
        ((200, single_user.HEALTH_MARKER), 403),
        ((404, ""), 200),
    ])
    def test_single_user_on_a_public_deployment_is_refused(self, tmp_path,
                                                           answer, status):
        unlocked = []
        gate = single_user.ReachabilityGate("https://x.hf.space",
                                            get=lambda url: answer)
        bridge = web.Bridge(_supervisor(tmp_path), single_user_requested=True,
                            gate=gate, unlock=lambda: unlocked.append(1) or [])
        with self._client(bridge) as client:
            first = client.get("/terminal")
            client.get("/terminal")
        assert first.status_code == status
        if status == 403:
            assert "answers anyone" in first.text
            assert unlocked == []
        else:
            assert unlocked == [1], "config.yaml is unlocked once, not per tab"
