"""
test_hosted_bridge.py

ROADMAP_v3 §79 (HD1-HD3, HD7-HD9, HD11, HD13): the bridge under
deploy/hosted/ -- the process a Space runs, which builds the sessions'
environment, starts one harness per browser tab, relays it, and completes
ModelScope's sign-in for it.

Nothing here imports gradio or spaces: app.py is the only module that does,
and everything it wires together is reachable without it -- its own tests
give it a stand-in for `spaces` and never call the part that imports
gradio. The relay is
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

from deploy.hosted import session_env, sign_in, single_user, supervisor, web
from deploy.hosted.sign_in import ModelScopeSignIn

CHILD = Path(__file__).resolve().parent / "_bridge_child.py"

# Runtime-assembled fakes, for test_connect.py's reason.
MS_SECRET = "ms-secret-" + "4f9a" * 6
MS_CN_SECRET = "ms-cn-secret-" + "b3d8" * 6
MS_TOKEN = "ms_oauth_" + "7c1e" * 10


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


def _keyed(forwarded_for="last"):
    return web.Bridge(SimpleNamespace(), forwarded_for=forwarded_for)


class TestTheAddressTheRateLimitCounts:
    def test_the_proxys_entry_not_the_clients(self):
        """Measured on the Space (2026-10-20): Hugging Face's proxy APPENDS
        the real address, so the first entry is whatever the client sent."""
        hf = _keyed("last")
        assert hf._client(_request("198.51.100.23, 203.0.113.9")) \
            == "203.0.113.9"
        assert hf._client(_request("203.0.113.9")) == "203.0.113.9"

    def test_on_modelscope_the_first_entry_is_the_client(self):
        """Measured on the Studio (2026-10-22, twice): ModelScope's proxy
        REPLACES the header -- a TEST-NET-2 entry sent by hand never arrived
        -- and rebuilds it as [client, hop, hop], so the last entry is a
        proxy of ModelScope's, the same for every visitor."""
        modelscope = _keyed("first")
        assert modelscope._client(_request("8.8.8.8, 10.1.0.7, 10.2.0.9")) \
            == "8.8.8.8"

    @pytest.mark.parametrize("forwarded_for", ["first", "last"])
    def test_without_the_header_it_is_the_peer(self, forwarded_for):
        bridge = _keyed(forwarded_for)
        assert bridge._client(_request()) == "10.0.0.5"
        assert bridge._client(_request(" , ")) == "10.0.0.5"

    def test_a_forged_first_entry_cannot_dodge_the_limit(self, tmp_path):
        bridge = web.build_bridge(env={}, root=tmp_path)
        issued = [bridge.issue_ticket(bridge._client(_request(
            f"198.51.100.{n}, 203.0.113.9"))) for n in range(30)]
        assert issued.count(None) == 30 - bridge.tickets_per_minute

    def test_on_modelscope_its_own_hops_do_not_split_one_visitor(self, tmp_path):
        """The hops differ request to request; the visitor does not."""
        (tmp_path / "BUILD_INFO.json").write_text(
            json.dumps({"platform": "modelscope"}), encoding="utf-8")
        bridge = web.build_bridge(env={}, root=tmp_path)
        issued = [bridge.issue_ticket(bridge._client(_request(
            f"8.8.8.8, 10.0.{n}.1, 10.0.{n}.2"))) for n in range(30)]
        assert issued.count(None) == 30 - bridge.tickets_per_minute

    @pytest.mark.parametrize("info, expected", [
        ({"platform": "modelscope"}, "first"),
        ({"platform": "hf"}, "last"),
        ({"platform": "somewhere-unmeasured"}, "last"),
        (None, "last"),
    ])
    def test_the_platform_comes_from_the_bundle(self, tmp_path, info,
                                                expected):
        if info is not None:
            (tmp_path / "BUILD_INFO.json").write_text(json.dumps(info),
                                                      encoding="utf-8")
        assert web.build_bridge(env={}, root=tmp_path).forwarded_for \
            == expected

    def test_any_other_choice_is_refused(self):
        with pytest.raises(ValueError, match="forwarded_for"):
            _keyed("middle")

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


class TestTheRateBooksForget:
    """Batch 191. A per-address limit forgets an address once its newest
    request has left the window. The books used to gain an entry for every
    address they ever counted, which a sender with many addresses could
    grow until the container restarted."""

    @staticmethod
    def _book():
        clock = [1000.0]
        return web._RateBook(clock=lambda: clock[0]), clock

    def test_an_address_past_the_window_is_forgotten(self):
        book, clock = self._book()
        for n in range(5000):
            assert book.allow(f"2001:db8::{n:x}", 10)
        assert len(book) == 5000
        clock[0] += web.RATE_WINDOW_S
        assert book.allow("198.51.100.7", 10)
        assert len(book) == 1

    def test_a_live_limit_survives_the_sweep(self):
        """The sweep reads an address's NEWEST request: one whose oldest
        has left the window but whose newest has not is still counted."""
        book, clock = self._book()
        assert book.allow("203.0.113.9", 2)
        clock[0] += 30
        assert book.allow("203.0.113.9", 2)
        clock[0] += 31  # past the first request and not the second; a sweep
        assert book.allow("203.0.113.9", 2)
        assert not book.allow("203.0.113.9", 2)

    def test_the_sweep_runs_once_a_window_not_on_every_request(self):
        """So a request costs the same however many addresses are
        counted, and a stale address waits at most one more window."""
        book, clock = self._book()
        clock[0] += 10
        book.allow("a", 10)  # t=10
        clock[0] += 51
        book.allow("b", 10)  # t=61: a sweep, and a's newest is 51 s old
        assert len(book) == 2
        clock[0] += 19
        book.allow("c", 10)  # t=80: a is 70 s old, and no sweep is due
        assert len(book) == 3
        clock[0] += 41
        book.allow("d", 10)  # t=121: a sweep; a and b go, c stays
        assert len(book) == 2

    def test_an_address_never_allowed_is_forgotten_too(self):
        book, clock = self._book()
        assert not book.allow("203.0.113.9", 0)
        clock[0] += web.RATE_WINDOW_S
        assert book.allow("198.51.100.7", 1)
        assert len(book) == 1

    def test_both_of_the_bridges_limits_are_such_books(self, tmp_path):
        bridge = web.Bridge(_supervisor(tmp_path))
        assert bridge.issue_ticket("203.0.113.9")
        assert isinstance(bridge._issued, web._RateBook)
        assert len(bridge._issued) == 1
        assert isinstance(bridge._callbacks, web._RateBook)


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
                "VENASTINE_SINGLE_USER": "1", "VENASTINE_CONFINEMENT": "verified",
                sign_in.MODELSCOPE.secret_env: MS_SECRET,
                sign_in.MODELSCOPE.client_id_env: "c",
                sign_in.MODELSCOPE_CN.secret_env: MS_CN_SECRET,
                sign_in.MODELSCOPE_CN.client_id_env: "c",
                "VENASTINE_BRIDGE_SIGN_IN": "modelscope,modelscope-cn"}
        env = supervisor.session_environment(
            base, supervisor.SessionPaths.under(tmp_path), "shared")
        assert set(env) & set(base) == {"PATH"}
        assert MS_SECRET not in env.values()
        assert MS_CN_SECRET not in env.values()
        assert "PYTHONPATH" not in env
        assert env["VENASTINE_HOSTED"] == "shared"
        for name in ("HOME", "TMPDIR", "APP_DB_PATH", "AGENT_LOG_FILE",
                     "AGENT_WORKSPACE", "AGENT_OUTPUT_DIR",
                     "AGENT_PROVIDERS_FILE", "AGENT_SECRETS_FILE",
                     "AGENT_ENV_FILE"):
            assert Path(env[name]).is_relative_to(tmp_path), name

    def test_it_runs_the_hosted_driver_and_names_only_the_offer(self, tmp_path):
        """HD13: the session learns WHICH sign-in its bridge completes,
        never what the bridge needs to complete it."""
        paths = supervisor.SessionPaths.under(tmp_path)
        plain = supervisor.session_environment({}, paths, "shared")
        offered = supervisor.session_environment({}, paths, "shared",
                                                 sign_ins="modelscope")
        assert plain["TEXTUAL_DRIVER"] == "tui.hosted_driver:HostedWebDriver"
        assert "VENASTINE_BRIDGE_SIGN_IN" not in plain
        assert offered["VENASTINE_BRIDGE_SIGN_IN"] == "modelscope"
        assert set(offered) - set(plain) == {"VENASTINE_BRIDGE_SIGN_IN"}

    def test_the_driver_it_names_is_importable_and_a_web_driver(self):
        import importlib

        from textual.drivers.web_driver import WebDriver

        module, _, name = supervisor.HOSTED_DRIVER.partition(":")
        assert issubclass(getattr(importlib.import_module(module), name),
                          WebDriver)

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


class TestStaleSessionsAreSwept:
    """Batch 168. What a killed bridge left of its visitors' sessions goes
    when the next one starts -- and nothing else does."""

    def test_only_leftover_session_directories_go(self, tmp_path):
        root = tmp_path / "tmp"
        stale = root / f"{supervisor.SESSION_PREFIX}abc"
        (stale / "home").mkdir(parents=True)
        (stale / "home" / "app.db").write_text("x", encoding="utf-8")
        kept = root / f"{supervisor.SESSION_PREFIX}kept"
        kept.mkdir()
        a_file = root / f"{supervisor.SESSION_PREFIX}file"
        a_file.write_text("x", encoding="utf-8")
        state = root / "venastine-state"
        state.mkdir()
        assert supervisor.sweep_stale_sessions(root, keep=kept) == 1
        assert not stale.exists()
        assert kept.is_dir() and a_file.is_file() and state.is_dir()

    def test_a_link_is_never_followed(self, tmp_path):
        root = tmp_path / "tmp"
        root.mkdir()
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("x", encoding="utf-8")
        link = root / f"{supervisor.SESSION_PREFIX}link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("this account cannot create a symlink")
        assert supervisor.sweep_stale_sessions(root) == 0
        assert link.is_symlink() and (target / "keep.txt").exists()

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
    def test_another_users_directory_is_left(self, tmp_path, monkeypatch):
        root = tmp_path / "tmp"
        theirs = root / f"{supervisor.SESSION_PREFIX}theirs"
        theirs.mkdir(parents=True)
        real = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real + 1)
        assert supervisor.sweep_stale_sessions(root) == 0
        assert theirs.is_dir()

    def test_a_missing_root_sweeps_nothing(self, tmp_path):
        assert supervisor.sweep_stale_sessions(tmp_path / "absent") == 0

    def test_building_a_bridge_sweeps_nothing(self, tmp_path, monkeypatch):
        """The suite builds bridges in parallel over one temporary
        directory, so only app.py's main() may sweep."""
        import tempfile

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        live = tmp_path / f"{supervisor.SESSION_PREFIX}live"
        live.mkdir()
        bridge = web.build_bridge(env={}, root=tmp_path)
        assert live.is_dir()
        assert bridge.supervisor.session_root \
            == supervisor.default_session_root() == tmp_path


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
        ((302, ""), False),
        ((404, "Not found"), True),
        ((403, '{"Message":"Unauthorized, please login"}'), True),
        ((200, "a sign-in page"), False),
    ])
    def test_it_runs_only_while_no_stranger_can_reach_it(self, answer, allowed):
        gate = single_user.ReachabilityGate("https://x.hf.space",
                                            get=lambda url: answer)
        assert gate.allowed() is allowed

    def test_only_privacy_responses_allow_single_user_mode(self):
        """An error, startup page or unknown reply is not a privacy check.
        Check the whole HTTP status range beside the accepted controls."""
        private = {401, 403, 404}
        for status in range(100, 600):
            gate = single_user.ReachabilityGate(
                "https://x.hf.space", get=lambda url, s=status: (s, ""))
            assert gate.allowed() is (status in private), status
            if status not in private:
                assert f"HTTP {status}" in gate.reason

    def test_a_redirect_is_not_privacy(self):
        """Batch 194, measured: Hugging Face answers plain http with a 301
        to https before it asks whether the Space is private, so an http
        public URL drew the same redirect from a public Space as from a
        private one. Neither platform answers privacy with a redirect: a
        private Space says 404 and a private Studio 403."""
        for status in (301, 302, 303, 307, 308):
            gate = single_user.ReachabilityGate(
                "http://x.hf.space", get=lambda url, s=status: (s, ""))
            assert not gate.allowed(), status
            assert f"HTTP {status}" in gate.reason

    def test_a_public_marker_overrides_a_privacy_status(self):
        for status in (200, 302, 403, 404):
            gate = single_user.ReachabilityGate(
                "https://x.hf.space",
                get=lambda url, s=status: (s, single_user.HEALTH_MARKER))
            assert not gate.allowed(), status
            assert "answers anyone" in gate.reason

    def test_an_indeterminate_answer_can_recover_to_private(self):
        calls, now = [], [0.0]
        answer = [(503, "Starting")]

        def get(url):
            calls.append(url)
            return answer[0]

        gate = single_user.ReachabilityGate("https://x.hf.space", get=get,
                                            clock=lambda: now[0])
        assert not gate.allowed()
        answer[0] = (404, "Not found")
        now[0] = single_user.CACHE_S - 1
        assert not gate.allowed()
        assert len(calls) == 1
        now[0] = single_user.CACHE_S
        assert gate.allowed()
        assert len(calls) == 2

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
            # HD12: a shared demo names where a key CAN go, and every
            # placeholder is filled.
            assert "VENASTINE_SINGLE_USER=1" in landing.text
            assert "Install the harness" in landing.text
            assert "$" not in landing.text
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

    def test_the_landing_page_names_each_modelscope_site_where_it_signs_in(
            self, tmp_path):
        """Batch 168 (HD13). A duplicated Space copies variables but not
        the secret, and there the command could only refuse. Batch 176:
        each site's line on its own, since a deployment may have one app's
        secret and not the other's."""
        bridge = web.Bridge(_supervisor(tmp_path))
        with self._client(bridge) as client:
            without = client.get("/").text
            bridge.supervisor.sign_ins = {"modelscope": _flow()[0]}
            ai_only = client.get("/").text
            bridge.supervisor.sign_ins = {
                "modelscope-cn": _flow(site=sign_in.MODELSCOPE_CN)[0]}
            cn_only = client.get("/").text
        assert '<li id="connect-modelscope" hidden>' in without
        assert '<li id="connect-modelscope-cn" hidden>' in without
        assert '<li id="connect-modelscope">' in ai_only
        assert '<li id="connect-modelscope-cn" hidden>' in ai_only
        assert '<li id="connect-modelscope" hidden>' in cn_only
        assert '<li id="connect-modelscope-cn">' in cn_only
        assert "https://www.modelscope.ai/my/settings/apps" in ai_only
        assert "/connect modelscope-cn" in cn_only
        assert "https://www.modelscope.cn/my/settings/apps" in cn_only
        for page in (without, ai_only, cn_only):
            assert "$" not in page

    def test_a_private_deployment_says_its_keys_go_here(self, tmp_path):
        bridge = web.Bridge(_supervisor(tmp_path), single_user_requested=True)
        with self._client(bridge) as client:
            landing = client.get("/").text
        assert "VENASTINE_PROVIDERS_JSON" in landing
        assert "VENASTINE_SINGLE_USER=1" not in landing
        assert "$" not in landing

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

    def test_startup_errors_never_unlock_or_admit_a_public_session(self,
                                                                 tmp_path):
        """Drive startup, the cached answer and its expiry through the
        bridge: no config write, ticket or websocket session may follow."""
        from starlette.websockets import WebSocketDisconnect

        for status in (200, 301, 429, 500, 502, 503, 504):
            calls, now, unlocked = [], [0.0], []
            answer = [(status, "Starting")]

            def get(url):
                calls.append(url)
                return answer[0]

            gate = single_user.ReachabilityGate(
                "https://x.hf.space", get=get, clock=lambda: now[0])
            bridge = web.Bridge(
                _supervisor(tmp_path), single_user_requested=True, gate=gate,
                unlock=lambda: unlocked.append(1) or [])
            # build_bridge asks before the server listens.
            assert bridge.refusal() is not None, status
            answer[0] = (200, single_user.HEALTH_MARKER)
            with self._client(bridge) as client:
                now[0] = 5.0
                assert client.get("/terminal").status_code == 403
                assert len(calls) == 1, "the cached refusal stays a refusal"
                assert bridge._tickets == {}
                # Even a ticket issued earlier cannot bypass the gate.
                ticket = bridge.issue_ticket("test")
                with pytest.raises(WebSocketDisconnect) as refused:
                    with client.websocket_connect(
                            f"/ws/{ticket}",
                            headers={"origin": "http://testserver"}) as ws:
                        ws.receive_bytes()
                assert refused.value.code == 4403
                now[0] = single_user.CACHE_S
                assert client.get("/terminal").status_code == 403
                assert len(calls) == 2
            assert unlocked == [], status
            assert bridge._tickets == {}
            assert bridge.supervisor.sessions == {}


# ---------------------------------------------------------------------------
# HD13: ModelScope's sign-in, completed by the bridge
# ---------------------------------------------------------------------------

class _Transport:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list = []

    def __call__(self, url, body):
        self.requests.append((url, dict(body)))
        return self.answers.pop(0)


_GRANTED = (200, {"access_token": MS_TOKEN, "scope": "openid api-inference",
                  "expires_in": 86400})


def _endpoints(origin):
    return sign_in.Endpoints(origin, f"{origin}/oauth/authorize",
                             f"{origin}/oauth/token")


def _documents(url):
    """A fake discovery `get`: both sites' documents in the shape they
    publish, and a refusal for anything else."""
    for site in sign_in.SITES:
        if url == f"{site.origin}/.well-known/openid-configuration":
            return {"issuer": site.origin,
                    "authorization_endpoint": f"{site.origin}/oauth/authorize",
                    "token_endpoint": f"{site.origin}/oauth/token",
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_post"]}
    raise sign_in.SignInError(f"could not read {url}: HTTP 404")


def _flow(*answers, site=sign_in.MODELSCOPE, path=None):
    transport = _Transport(*answers)
    return ModelScopeSignIn(site, "client-1", MS_SECRET,
                            f"http://testserver{path or site.callback_path}",
                            endpoints=_endpoints(site.origin),
                            studio_app=path is not None,
                            post=transport), transport


def _state(url: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(url).query)["state"][0]


class TestASessionsSignInRequests:
    """Session._on_meta's half, with no process behind it: what each
    packet asks, and that every flow is keyed by the session's own id."""

    def _session(self, tmp_path, sign_ins, name="s"):
        sup = _supervisor(tmp_path)
        sup.sign_ins = sign_ins
        session = supervisor.Session(
            sup, supervisor.SessionPaths.under(tmp_path / name), keep_dir=True)
        session.sent = []

        async def send_meta(meta):
            session.sent.append(meta)

        session.send_meta = send_meta
        return session

    @staticmethod
    async def _meta(session, meta):
        return await session._on_meta(None, json.dumps(meta).encode())

    @staticmethod
    def _start(provider="modelscope", request="r1"):
        return {"type": "venastine_sign_in", "id": request,
                "provider": provider}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider, named", [
        ("modelscope", "ModelScope"),
        ("modelscope-cn", "ModelScope (modelscope.cn)"),
        ("huggingface", "That"),
        (None, "That"),
    ])
    async def test_a_sign_in_not_offered_here_says_so(self, tmp_path,
                                                      provider, named):
        """Batch 176: the site comes from the packet, and only a site this
        bridge offers is started -- another or a made-up name is refused
        in words that never repeat it."""
        offered = {"modelscope": _flow()[0]} if provider != "modelscope" else {}
        session = self._session(tmp_path, offered)
        start = self._start(provider)
        if provider is None:
            del start["provider"]
        assert await self._meta(session, start) is False
        assert session.sent == [{
            "type": "venastine_sign_in_failed", "id": "r1", "retry": False,
            "reason": f"{named} sign-in is not set up on this deployment."}]

    @pytest.mark.asyncio
    async def test_a_start_answers_with_the_consent_page(self, tmp_path):
        flow, _ = _flow()
        session = self._session(tmp_path, {"modelscope": flow})
        await self._meta(session, self._start())
        (answer,) = session.sent
        assert answer["type"] == "venastine_sign_in_url"
        assert answer["id"] == "r1"
        assert answer["url"].startswith(
            "https://www.modelscope.ai/oauth/authorize?")
        assert flow._by_session[session.id].state == _state(answer["url"])

    @pytest.mark.asyncio
    async def test_the_code_is_traded_and_the_token_goes_back(self, tmp_path):
        flow, transport = _flow(_GRANTED)
        session = self._session(tmp_path, {"modelscope": flow})
        await self._meta(session, self._start())
        page = flow.callback({"code": "code-C",
                              "state": _state(session.sent[0]["url"])})
        await self._meta(session, {"type": "venastine_sign_in_code",
                                   "id": "r1", "code": page.handle})
        await session._exchange
        assert session.sent[-1] == {"type": "venastine_credential",
                                    "id": "r1", "token": MS_TOKEN}
        assert transport.requests[0][1]["code"] == "code-C"

    @pytest.mark.asyncio
    async def test_a_cn_sign_in_goes_to_modelscope_cn_alone(self, tmp_path):
        """Batch 176: the consent page, the code and the trade all belong
        to the site the session asked for, and the other site's app is
        never touched."""
        ai, ai_transport = _flow(_GRANTED)
        cn, cn_transport = _flow(_GRANTED, site=sign_in.MODELSCOPE_CN)
        session = self._session(tmp_path, {"modelscope": ai,
                                           "modelscope-cn": cn})
        await self._meta(session, self._start("modelscope-cn"))
        url = session.sent[0]["url"]
        assert url.startswith("https://www.modelscope.cn/oauth/authorize?")
        page = cn.callback({"code": "code-CN", "state": _state(url)})
        await self._meta(session, {"type": "venastine_sign_in_code",
                                   "id": "r1", "code": page.handle})
        await session._exchange
        assert session.sent[-1] == {"type": "venastine_credential",
                                    "id": "r1", "token": MS_TOKEN}
        (token_url, body), = cn_transport.requests
        assert token_url == "https://www.modelscope.cn/oauth/token"
        assert body["code"] == "code-CN"
        assert ai_transport.requests == [] and ai._by_session == {}

    @pytest.mark.asyncio
    async def test_starting_one_site_drops_the_others_flow(self, tmp_path):
        """A session waits for one sign-in at a time, so a flow left open
        on the other site could only be approved by mistake."""
        ai, _ = _flow()
        cn, _ = _flow(site=sign_in.MODELSCOPE_CN)
        session = self._session(tmp_path, {"modelscope": ai,
                                           "modelscope-cn": cn})
        await self._meta(session, self._start("modelscope"))
        first = _state(session.sent[0]["url"])
        await self._meta(session, self._start("modelscope-cn", "r2"))
        assert session.id not in ai._by_session
        assert session.id in cn._by_session
        assert ai.callback({"code": "c", "state": first}).status == 400

    @pytest.mark.asyncio
    async def test_a_code_with_nothing_started_is_refused(self, tmp_path):
        flow, transport = _flow(_GRANTED)
        session = self._session(tmp_path, {"modelscope": flow})
        await self._meta(session, {"type": "venastine_sign_in_code",
                                   "id": "r1", "code": "anything"})
        assert session.sent == [{
            "type": "venastine_sign_in_failed", "id": "r1", "retry": False,
            "reason": "No sign-in in this session is waiting for a code."}]
        assert session._exchange is None and transport.requests == []

    @pytest.mark.asyncio
    async def test_another_sessions_handle_is_refused(self, tmp_path):
        flow, transport = _flow(_GRANTED)
        owner = self._session(tmp_path, {"modelscope": flow}, "a")
        other = self._session(tmp_path, {"modelscope": flow}, "b")
        await self._meta(owner, self._start())
        page = flow.callback({"code": "code-C",
                              "state": _state(owner.sent[0]["url"])})
        await self._meta(other, self._start(request="r9"))
        await self._meta(other, {"type": "venastine_sign_in_code",
                                 "id": "r9", "code": page.handle})
        assert other.sent[-1]["type"] == "venastine_sign_in_failed"
        assert other._exchange is None and transport.requests == []

    @pytest.mark.asyncio
    async def test_a_refused_trade_is_reported_by_its_code(self, tmp_path,
                                                           caplog):
        caplog.set_level(logging.INFO, logger="deploy.hosted.supervisor")
        flow, _ = _flow((200, {"error": "invalid_grant"}),
                        site=sign_in.MODELSCOPE_CN)
        session = self._session(tmp_path, {"modelscope-cn": flow})
        await self._meta(session, self._start("modelscope-cn"))
        page = flow.callback({"code": "c",
                              "state": _state(session.sent[0]["url"])})
        await self._meta(session, {"type": "venastine_sign_in_code",
                                   "id": "r1", "code": page.handle})
        await session._exchange
        assert session.sent[-1]["type"] == "venastine_sign_in_failed"
        reason = session.sent[-1]["reason"]
        assert "invalid_grant" in reason and "modelscope.cn" in reason
        # The operator sees why, and on which site, by the code alone.
        assert (f"ModelScope (modelscope.cn) sign-in for session "
                f"{session.id} did not finish") in caplog.text
        assert "invalid_grant" in caplog.text
        assert page.handle not in caplog.text and MS_SECRET not in caplog.text

    @pytest.mark.asyncio
    async def test_cancel_and_stop_both_drop_the_flow(self, tmp_path):
        ai, _ = _flow()
        cn, _ = _flow(site=sign_in.MODELSCOPE_CN)
        session = self._session(tmp_path, {"modelscope": ai,
                                           "modelscope-cn": cn})
        # Each with the OTHER site's flow open, so dropping only one site's
        # would leave the flow a cancel or a closed tab was meant to end.
        await self._meta(session, self._start("modelscope"))
        await self._meta(session, {"type": "venastine_sign_in_cancel",
                                   "id": "r1"})
        assert session.id not in ai._by_session
        await self._meta(session, self._start("modelscope-cn", "r2"))
        await session.stop()
        assert session.id not in cn._by_session
        assert session.id not in ai._by_session


class TestTheModelScopeCallback:
    def _client(self, bridge):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        return TestClient(Starlette(routes=bridge.routes()))

    @pytest.mark.parametrize("path", web.CALLBACK_PATHS)
    def test_a_deployment_without_it_has_no_such_page(self, tmp_path, path):
        bridge = web.Bridge(_supervisor(tmp_path))
        with self._client(bridge) as client:
            assert client.get(path).status_code == 404

    def test_a_studios_own_app_is_answered_on_its_own_path(self, tmp_path):
        """Batch 177: the Studio's own app returns to the path ModelScope's
        Studio OAuth docs give a server-side app, and the hand-registered
        app's path is no page while it stands in."""
        flow, _ = _flow(path=sign_in.STUDIO_CALLBACK_PATH)
        bridge = web.Bridge(_supervisor(tmp_path))
        bridge.supervisor.sign_ins = {"modelscope": flow}
        with self._client(bridge) as client:
            assert client.get(sign_in.MODELSCOPE.callback_path).status_code \
                == 404
            page = client.get("/auth/callback", params={
                "code": "c", "state": _state(flow.start("s1", "r1"))})
        assert page.status_code == 200
        assert re.search(r'<p class="handle">[^<]+</p>', page.text)
        assert "ModelScope sign-in" in page.text

    def test_a_forged_state_is_refused_without_repeating_it(self, tmp_path):
        bridge = web.Bridge(_supervisor(tmp_path))
        bridge.supervisor.sign_ins = {"modelscope": _flow()[0]}
        with self._client(bridge) as client:
            page = client.get(sign_in.MODELSCOPE.callback_path,
                              params={"state": "<b>forged</b>", "code": "x"})
        assert page.status_code == 400
        assert "forged" not in page.text and "$" not in page.text
        assert page.headers["cache-control"] == "no-store"

    def test_each_site_is_answered_on_its_own_path_alone(self, tmp_path):
        """Batch 176, the owner's choice of a path per site: a site's code
        reaches only the flows of the site that issued it, so a state from
        one site's consent page is no state at all on the other's path."""
        ai, _ = _flow()
        cn, _ = _flow(site=sign_in.MODELSCOPE_CN)
        bridge = web.Bridge(_supervisor(tmp_path))
        bridge.supervisor.sign_ins = {"modelscope-cn": cn}
        with self._client(bridge) as client:
            assert client.get(sign_in.MODELSCOPE.callback_path).status_code \
                == 404
            bridge.supervisor.sign_ins = {"modelscope": ai,
                                          "modelscope-cn": cn}
            ai_state = _state(ai.start("s1", "r1"))
            crossed = client.get(sign_in.MODELSCOPE_CN.callback_path,
                                 params={"code": "c", "state": ai_state})
            cn_state = _state(cn.start("s2", "r2"))
            page = client.get(sign_in.MODELSCOPE_CN.callback_path,
                              params={"code": "c", "state": cn_state})
        assert crossed.status_code == 400
        assert "/connect modelscope-cn" in crossed.text
        assert ai._by_session["s1"].code is None
        assert page.status_code == 200
        assert re.search(r'<p class="handle">[^<]+</p>', page.text)
        assert "ModelScope (modelscope.cn) sign-in" in page.text

    @pytest.mark.parametrize("info, framed_by", [
        ({"platform": "modelscope"}, "https://www.modelscope.ai"),
        ({"platform": "hf"}, "https://huggingface.co"),
        ({"platform": "somewhere-unmeasured"}, "'none'"),
        (None, "'none'"),
    ])
    def test_only_the_platforms_own_site_may_frame_it(self, tmp_path, info,
                                                      framed_by):
        """Measured on ModelScope: the redirect to the Studio's host came
        back inside www.modelscope.ai's Studio page, and a page no site may
        frame showed the person nothing (HD13)."""
        if info is not None:
            (tmp_path / "BUILD_INFO.json").write_text(json.dumps(info),
                                                      encoding="utf-8")
        bridge = web.build_bridge(env={}, root=tmp_path)
        bridge.supervisor.sign_ins = {"modelscope": _flow()[0]}
        with self._client(bridge) as client:
            csp = client.get(sign_in.MODELSCOPE.callback_path).headers[
                "content-security-policy"]
        assert csp.endswith(f"frame-ancestors {framed_by}")
        assert "default-src 'none'" in csp and "form-action 'none'" in csp

    def test_the_callbacks_from_one_address_are_limited(self, tmp_path):
        bridge = web.Bridge(_supervisor(tmp_path))
        bridge.supervisor.sign_ins = {
            "modelscope": _flow()[0],
            "modelscope-cn": _flow(site=sign_in.MODELSCOPE_CN)[0]}
        paths = [site.callback_path for site in sign_in.SITES]
        with self._client(bridge) as client:
            # One allowance per address across both sites' paths.
            codes = [client.get(paths[i % 2]).status_code
                     for i in range(web.SIGN_IN_CALLBACKS_PER_MINUTE + 1)]
            limited = client.get(sign_in.MODELSCOPE_CN.callback_path)
        assert codes[:-1] == [400] * web.SIGN_IN_CALLBACKS_PER_MINUTE
        assert codes[-1] == 429
        assert "/connect modelscope-cn" in limited.text

    def test_end_to_end_over_a_real_session(self, tmp_path):
        """The whole round, through the routes and a real pipe: the session
        asks, the bridge answers with the consent page, ModelScope's
        redirect gets a handle, the handle typed into the terminal comes
        back to the bridge, and the token goes to that session alone."""
        flow, transport = _flow(_GRANTED)
        cn, cn_transport = _flow(site=sign_in.MODELSCOPE_CN)
        bridge = web.Bridge(_supervisor(tmp_path, "sign-in"))
        bridge.supervisor.sign_ins = {"modelscope-cn": cn, "modelscope": flow}
        with self._client(bridge) as client:
            ticket = re.search(r'data-ticket="([^"]+)"',
                               client.get("/terminal").text).group(1)
            with client.websocket_connect(
                    f"/ws/{ticket}", headers={"origin": "http://testserver"}) as ws:
                screen = b""
                while b"venastine_sign_in_url:" not in screen:
                    screen += ws.receive().get("bytes") or b""
                # What the session was told it may sign in to: the names,
                # in one order, never what they need.
                assert b"offered:modelscope,modelscope-cn|" in screen
                answer = json.loads(re.search(
                    rb"venastine_sign_in_url:(\{.*?\})\|", screen).group(1))
                assert answer["id"] == "req-1"

                page = client.get(sign_in.MODELSCOPE.callback_path, params={
                    "code": "code-C", "state": _state(answer["url"])})
                assert page.status_code == 200
                csp = page.headers["content-security-policy"]
                assert "default-src 'none'" in csp
                assert "frame-ancestors 'none'" in csp
                assert page.headers["referrer-policy"] == "no-referrer"
                assert page.headers["cache-control"] == "no-store"
                assert "code-C" not in page.text
                handle = re.search(r'<p class="handle">([^<]+)</p>',
                                   page.text).group(1)

                ws.send_text(json.dumps(["stdin", f"code:{handle}"]))
                while b"venastine_credential:" not in screen:
                    screen += ws.receive().get("bytes") or b""
                credential = json.loads(re.search(
                    rb"venastine_credential:(\{.*?\})\|", screen).group(1))
            # TestTheRoutes.test_end_to_end's reason: the shielded stop has
            # to finish while the client's loop still runs.
            deadline = time.monotonic() + 30
            while bridge.supervisor.sessions and time.monotonic() < deadline:
                time.sleep(0.05)
        assert bridge.supervisor.sessions == {}
        assert credential == {"id": "req-1", "token": MS_TOKEN}
        (url, body), = transport.requests
        assert url == "https://www.modelscope.ai/oauth/token"
        assert body["code"] == "code-C" and body["client_secret"] == MS_SECRET
        assert body["redirect_uri"] == flow.redirect_uri
        assert cn_transport.requests == []


class TestWhatTheBridgeOffers:
    """build_bridge's part (batch 176): each site whose app id, secret and
    public URL this deployment has, and no other."""

    ENV = {"SPACE_HOST": "x.hf.space",
           sign_in.MODELSCOPE_CN.client_id_env: "cn-app"}

    @pytest.fixture(autouse=True)
    def documents(self, monkeypatch):
        """build_bridge reads each discovery document through the module's
        default `get` (batch 177); here it reads the fake."""
        asked = []
        monkeypatch.setattr(sign_in, "_get_json",
                            lambda url: asked.append(url) or _documents(url))
        return asked

    def test_each_site_with_its_pieces_is_offered(self, tmp_path, documents):
        bridge = web.build_bridge(env=self.ENV, root=tmp_path, sign_in_secrets={
            sign_in.MODELSCOPE.secret_env: MS_SECRET,
            sign_in.MODELSCOPE_CN.secret_env: MS_CN_SECRET})
        assert sorted(documents) == [
            "https://www.modelscope.ai/.well-known/openid-configuration",
            "https://www.modelscope.cn/.well-known/openid-configuration"]
        offered = bridge.supervisor.sign_ins
        assert set(offered) == {"modelscope", "modelscope-cn"}
        assert offered["modelscope-cn"].client_id == "cn-app"
        assert offered["modelscope-cn"].redirect_uri == \
            "https://x.hf.space/oauth/modelscope-cn/callback"
        assert offered["modelscope"].redirect_uri == \
            "https://x.hf.space/oauth/modelscope/callback"

    def test_one_sites_secret_offers_that_site_alone(self, tmp_path):
        bridge = web.build_bridge(env=self.ENV, root=tmp_path, sign_in_secrets={
            sign_in.MODELSCOPE.secret_env: MS_SECRET})
        assert set(bridge.supervisor.sign_ins) == {"modelscope"}

    def test_no_secrets_offer_nothing_and_fetch_nothing(self, tmp_path,
                                                        documents):
        assert web.build_bridge(env=self.ENV, root=tmp_path) \
            .supervisor.sign_ins == {}
        assert documents == []

    def test_a_studio_with_oauth_on_signs_in_through_its_own_app(
            self, tmp_path, documents):
        """Batch 177. The .ai sign-in returns to /auth/callback on the
        Studio's host, and that host is one the bridge answers for. Batch
        178: the provider is injected as plain http, as measured on the
        Studio, and its document is still read over https."""
        env = {"OAUTH_CLIENT_ID": "studio-app",
               "OAUTH_SCOPES": "openid api-inference",
               "OPENID_PROVIDER_URL": "http://www.modelscope.ai",
               "STUDIO_HOST": "owner-demo.ms.fun", "STUDIO_ID": "owner/demo"}
        bridge = web.build_bridge(env=env, root=tmp_path, sign_in_secrets={
            sign_in.STUDIO_SECRET_ENV: MS_SECRET})
        flow = bridge.supervisor.sign_ins["modelscope"]
        assert flow.studio_app is True
        assert flow.redirect_uri == "https://owner-demo.ms.fun/auth/callback"
        assert "owner-demo.ms.fun" in bridge.allowed_hosts
        assert documents == [
            "https://www.modelscope.ai/.well-known/openid-configuration"]


class TestTheBridgeKeepsItsSecret:
    """app.py's part (HD13): the secret leaves the environment before
    anything can inherit it, and the bridge makes itself non-dumpable."""

    @pytest.fixture
    def app_module(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "spaces", SimpleNamespace(
            GPU=lambda **kw: (lambda function: function)))
        from deploy.hosted import app

        return app

    def test_taking_them_removes_every_one(self, app_module):
        """The Studio's own app's secret too (batch 177): ModelScope's
        Studio OAuth injects it into the bridge's environment."""
        environ = {sign_in.MODELSCOPE.secret_env: f" {MS_SECRET}\n",
                   sign_in.MODELSCOPE_CN.secret_env: MS_CN_SECRET,
                   "OAUTH_CLIENT_SECRET": "studio", "OAUTH_CLIENT_ID": "id",
                   "PATH": "/bin"}
        assert app_module.take_secrets(environ) == {
            sign_in.MODELSCOPE.secret_env: MS_SECRET,
            sign_in.MODELSCOPE_CN.secret_env: MS_CN_SECRET,
            "OAUTH_CLIENT_SECRET": "studio"}
        assert environ == {"OAUTH_CLIENT_ID": "id", "PATH": "/bin"}

    def test_one_not_set_maps_to_nothing(self, app_module):
        environ = {sign_in.MODELSCOPE_CN.secret_env: MS_CN_SECRET}
        assert app_module.take_secrets(environ) == {
            sign_in.MODELSCOPE.secret_env: "",
            sign_in.MODELSCOPE_CN.secret_env: MS_CN_SECRET,
            "OAUTH_CLIENT_SECRET": ""}
        assert environ == {}

    def test_a_studio_drops_the_spaces_flag_and_a_space_keeps_it(
            self, app_module):
        """Batch 177. Studio OAuth sets SYSTEM=spaces for Gradio's own
        button, which would start Hugging Face's reload watcher here."""
        studio = {"STUDIO_ID": "owner/demo", "SYSTEM": "spaces",
                  "SPACE_ID": "owner/demo"}
        assert app_module.drop_spaces_flag(studio) is True
        assert studio == {"STUDIO_ID": "owner/demo", "SPACE_ID": "owner/demo"}
        space = {"SYSTEM": "spaces", "SPACE_ID": "owner/demo"}
        assert app_module.drop_spaces_flag(space) is False
        assert space["SYSTEM"] == "spaces"
        assert app_module.drop_spaces_flag({"STUDIO_ID": "owner/demo"}) is False

    def test_main_takes_them_first_and_hands_them_to_the_bridge(
            self, app_module, monkeypatch, tmp_path, caplog):
        import security.hosted
        from deploy.hosted import isolation_report

        def any_secret_left():
            return any(site.secret_env in os.environ for site in sign_in.SITES)

        events = []
        monkeypatch.setenv(sign_in.MODELSCOPE.secret_env, MS_SECRET)
        monkeypatch.setenv(sign_in.MODELSCOPE_CN.secret_env, MS_CN_SECRET)
        monkeypatch.setenv("OAUTH_CLIENT_SECRET", "studio")
        monkeypatch.setenv("STUDIO_ID", "owner/demo")
        monkeypatch.setenv("SYSTEM", "spaces")
        monkeypatch.setattr(security.hosted, "make_undumpable", lambda: events.append(
            ("undumpable", any_secret_left())) or True)
        monkeypatch.setattr(session_env, "LOCK", tmp_path / "absent.lock")
        monkeypatch.setattr(isolation_report, "log", lambda *a: events.append(
            ("report", any_secret_left())))
        monkeypatch.setattr(isolation_report, "verify_confinement",
                            lambda *a: "")
        # Batch 168: a killed predecessor's sessions go before anything
        # listens.
        monkeypatch.setattr(supervisor, "sweep_stale_sessions",
                            lambda: events.append(("sweep",)))
        # Batch 169: which build this is goes in the log, before anything
        # slow, because a private Studio's log is where a deploy is seen.
        monkeypatch.setattr(web, "build_label", lambda root: events.append(
            ("label", root == Path(app_module.__file__).resolve().parents[2]))
            or "build 0123456789ab")

        def build(python, confinement, secrets):
            events.append(("build", secrets, "SYSTEM" in os.environ))
            return SimpleNamespace(launch=lambda **kw: events.append(("launch",)))

        monkeypatch.setattr(app_module, "build", build)
        with caplog.at_level(logging.INFO, logger=app_module.__name__):
            app_module.main()
        assert events == [("undumpable", False), ("label", True), ("sweep",),
                          ("report", False),
                          ("build", {sign_in.MODELSCOPE.secret_env: MS_SECRET,
                                     sign_in.MODELSCOPE_CN.secret_env:
                                         MS_CN_SECRET,
                                     "OAUTH_CLIENT_SECRET": "studio"}, False),
                          ("launch",)]
        assert not any_secret_left() and "OAUTH_CLIENT_SECRET" not in os.environ
        assert "SYSTEM=spaces removed" in caplog.text
        assert "serving build 0123456789ab" in caplog.messages
