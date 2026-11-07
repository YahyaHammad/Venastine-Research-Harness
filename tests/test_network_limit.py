"""
tests/test_network_limit.py

ROADMAP_v3 §53, batch 239: a rolling bound on what the harness pulls and
pushes. Three meters -- tools, the provider API, MCP over HTTP -- each with
its own ceiling over one window, counted into a machine-wide SQLite ledger,
and admitted before a call rather than cut during one.

What is pinned here, in the order a reader meets it:

  * the LEDGER (IO10, IO12): no file until something is counted, counts
    that survive a restart, two processes that both count, rows pruned
    past the window and bounded however much traffic there was, and a
    broken file that does not switch the meter off;
  * the METER (IO6, IO9, IO11): off by default, admission on what is
    used AND on what a request will send, one warning per run of
    refusals, and room returning as the oldest bytes leave the window;
  * the CALLERS: each tool's refusal reaches the model as the meter's own
    sentence and is never retried, web_search is charged IO15's measured
    estimate, and the provider and MCP hooks count wire bytes exactly;
  * what a person READS (IO13): `/network` in both shells and the sidebar
    line, which is hidden while every meter is off.

The wire measurements that justify the counting (gzip counted as sent, a
redirect hop's body counted, the SDKs' hook behaviour) were made against a
real local server and the real SDKs in batch 239; here they are pinned on
real httpx2 objects with a streamed body, which is the shape a real
transport produces.
"""

import gzip
import logging
import sqlite3
import time

import httpx2
import pytest

import config
from core import provider_errors
from tools.builtin import _net_common
from tools.builtin._net_common import (
    LEDGER_BUCKETS,
    MEGABYTE,
    Ledger,
    NetworkLimitReached,
)

HOUR = 3600.0


class Clock:
    """A wall clock the test moves by hand (IO7 is wall-clock time)."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ledger(tmp_path, clock, monkeypatch):
    """A timerless ledger on a fake clock, installed as the module's."""
    led = Ledger(path=str(tmp_path / "net" / "network.db"), clock=clock,
                 timers=False)
    monkeypatch.setattr(_net_common, "LEDGER", led)
    return led


def _limits(monkeypatch, tools=0.0, provider=0.0, mcp=0.0, hours=24.0):
    monkeypatch.setattr(config, "NETWORK_WINDOW_HOURS", hours)
    monkeypatch.setattr(config, "NETWORK_LIMIT_TOOLS_MB", tools)
    monkeypatch.setattr(config, "NETWORK_LIMIT_PROVIDER_MB", provider)
    monkeypatch.setattr(config, "NETWORK_LIMIT_MCP_MB", mcp)


def _rows(path):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT meter, bucket, bytes, estimated FROM usage "
            "ORDER BY meter, bucket").fetchall()


# ---------------------------------------------------------------------------
# ---- The shipped state -----------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheShippedState:
    def test_every_ceiling_ships_at_zero_over_a_24_hour_window(self):
        """IO1 and IO8: unlimited by default, and the owner's 24 hours."""
        import config_schema

        shipped = config_schema.load()
        assert shipped.network_window_hours == 24
        assert shipped.network_limit_tools_mb == 0
        assert shipped.network_limit_provider_mb == 0
        assert shipped.network_limit_mcp_mb == 0

    def test_off_means_nothing_is_counted_and_no_file_exists(
            self, ledger, monkeypatch):
        _limits(monkeypatch)
        for meter in (_net_common.TOOLS, _net_common.PROVIDER, _net_common.MCP):
            meter.admit(10 * MEGABYTE)
            meter.record(10 * MEGABYTE)
        ledger.flush()
        assert not (ledger.path() and __import__("os").path.exists(ledger.path()))
        assert all(not state.enabled for state in _net_common.states())

    @pytest.mark.parametrize("key, bad", [
        ("network_limit_tools_mb", -1),
        ("network_limit_provider_mb", True),
        ("network_window_hours", 0),
        ("network_window_hours", ".nan"),
    ])
    def test_the_schema_refuses_a_ceiling_or_window_that_cannot_work(
            self, key, bad):
        import config_schema

        data = config_schema.read_document()
        data[key] = float("nan") if bad == ".nan" else bad
        with pytest.raises(Exception):
            config_schema.HarnessConfig.model_validate(data)

    def test_the_ceilings_are_not_authority_keys(self):
        """A ceiling decides how much may be SPENT, which is ensemble_mode's
        side of the line, not what may run."""
        import config_schema

        assert not {
            "network_window_hours", "network_limit_tools_mb",
            "network_limit_provider_mb", "network_limit_mcp_mb",
        } & config_schema.HARNESS_AUTHORITY_KEYS


# ---------------------------------------------------------------------------
# ---- The ledger (IO10, IO12) -------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheLedger:
    def test_counting_alone_writes_nothing_until_a_flush(
            self, ledger, monkeypatch):
        import os

        _limits(monkeypatch, tools=1)
        ledger.add("tools", 500)
        assert not os.path.exists(ledger.path())
        assert ledger.usage("tools")[0][1] == 500
        ledger.flush()
        assert os.path.exists(ledger.path())

    def test_an_idle_flush_opens_nothing(self, ledger, monkeypatch):
        import os

        _limits(monkeypatch, tools=1)
        ledger.flush()
        assert not os.path.exists(os.path.dirname(ledger.path()))

    def test_counts_survive_a_restart(self, tmp_path, clock, monkeypatch):
        """IO7: a restart is a run, and a reset per run is the loophole."""
        _limits(monkeypatch, tools=1)
        path = str(tmp_path / "network.db")
        first = Ledger(path=path, clock=clock, timers=False)
        first.add("tools", 1234)
        first.flush()

        second = Ledger(path=path, clock=clock, timers=False)
        assert sum(n for _, n, _ in second.usage("tools")) == 1234

    def test_two_processes_flushing_one_bucket_both_count(
            self, tmp_path, clock, monkeypatch):
        """The reason this is SQLite: an atomic replace of a read-modify-
        write would keep only the last writer's figure."""
        _limits(monkeypatch, provider=1)
        path = str(tmp_path / "network.db")
        a = Ledger(path=path, clock=clock, timers=False)
        b = Ledger(path=path, clock=clock, timers=False)
        a.add("provider", 100)
        b.add("provider", 250)
        a.flush()
        b.flush()
        assert _rows(path) == [("provider", int(clock.now // 60 * 60), 350, 0)]
        assert sum(n for _, n, _ in a.usage("provider", clock.now + 61)) == 350

    def test_another_process_is_seen_after_the_flush_interval(
            self, tmp_path, clock, monkeypatch):
        """IO12's cost, stated: up to a minute late, and no later."""
        _limits(monkeypatch, tools=1)
        path = str(tmp_path / "network.db")
        here = Ledger(path=path, clock=clock, timers=False)
        there = Ledger(path=path, clock=clock, timers=False)
        assert here.usage("tools") == []        # stamps the read
        there.add("tools", 700)
        there.flush()
        assert here.usage("tools") == []        # memory, not the disk
        clock.advance(_net_common.FLUSH_INTERVAL_S)
        assert sum(n for _, n, _ in here.usage("tools")) == 700

    def test_rows_past_the_window_are_deleted_on_a_flush(
            self, ledger, clock, monkeypatch):
        _limits(monkeypatch, tools=1, hours=1)
        ledger.add("tools", 10)
        ledger.flush()
        clock.advance(2 * HOUR)
        ledger.add("tools", 20)
        ledger.flush()
        rows = _rows(ledger.path())
        assert [nbytes for _, _, nbytes, _ in rows] == [20]

    def test_the_file_is_bounded_by_time_not_by_traffic(
            self, ledger, clock, monkeypatch):
        """The owner's concern: 50 MB a day must not become a growing file.
        A full day and a half of traffic every minute, then the rows."""
        _limits(monkeypatch, tools=1, provider=1)
        for _ in range(int(36 * 60)):
            ledger.add("tools", 50 * MEGABYTE)
            ledger.add("provider", 1)
            clock.advance(60)
            ledger.flush()
        rows = _rows(ledger.path())
        per_meter = {m: sum(1 for r in rows if r[0] == m)
                     for m in ("tools", "provider")}
        assert all(count <= LEDGER_BUCKETS + 3 for count in per_meter.values()), (
            per_meter)

    @pytest.mark.parametrize("hours, width", [
        (24, 60), (1, 60), (0.01, 60), (24 * 30, 1800), (24 * 7, 420),
    ])
    def test_a_bucket_is_the_window_over_1440_and_never_under_a_minute(
            self, monkeypatch, hours, width):
        _limits(monkeypatch, hours=hours)
        assert _net_common.bucket_width_s() == width

    def test_a_bucket_straddling_the_old_edge_still_counts(
            self, ledger, clock, monkeypatch):
        """It errs high, never low: refused a moment early rather than
        admitted a moment late."""
        _limits(monkeypatch, tools=1, hours=1)
        ledger.add("tools", 99)
        clock.advance(HOUR + 30)
        assert sum(n for _, n, _ in ledger.usage("tools")) == 99
        clock.advance(60)
        assert ledger.usage("tools") == []

    def test_a_broken_file_still_enforces_this_process_and_says_so(
            self, tmp_path, clock, monkeypatch, caplog):
        _limits(monkeypatch, tools=0.001)          # 1,000 bytes
        path = tmp_path / "network.db"
        path.write_bytes(b"this is not a database" * 100)
        led = Ledger(path=str(path), clock=clock, timers=False)
        monkeypatch.setattr(_net_common, "LEDGER", led)

        with caplog.at_level(logging.WARNING):
            _net_common.TOOLS.record(2000)
            led.flush()
            led.flush()                            # same failure: no repeat
        assert led.note and "counting this process only" in led.note
        assert sum("could not be written" in r.getMessage()
                   for r in caplog.records) == 1
        with pytest.raises(NetworkLimitReached):
            _net_common.TOOLS.admit()
        assert _net_common.TOOLS.state().ledger_note == led.note

    def test_one_timer_per_flush_and_none_while_idle(self, tmp_path, monkeypatch):
        started = []

        class FakeTimer:
            def __init__(self, interval, fn):
                self.interval, self.fn, self.daemon = interval, fn, False
                started.append(self)

            def start(self):
                pass

            def cancel(self):
                pass

        monkeypatch.setattr(_net_common.threading, "Timer", FakeTimer)
        _limits(monkeypatch, tools=1)
        led = Ledger(path=str(tmp_path / "n.db"), timers=True)
        assert started == []
        led.add("tools", 1)
        led.add("tools", 1)
        assert len(started) == 1
        assert started[0].interval == _net_common.FLUSH_INTERVAL_S
        assert started[0].daemon is True
        started[0].fn()                            # the flush fires
        assert len(started) == 1
        led.add("tools", 1)
        assert len(started) == 2


# ---------------------------------------------------------------------------
# ---- The meter (IO6, IO9, IO11) ----------------------------------------------
# ---------------------------------------------------------------------------

class TestTheMeter:
    def test_a_ceiling_is_decimal_megabytes_read_at_call_time(
            self, ledger, monkeypatch):
        _limits(monkeypatch, tools=2.5)
        assert _net_common.TOOLS.ceiling() == 2_500_000
        # 2.01 * 1,000,000 is 2,009,999.9999999998 in binary floating point.
        monkeypatch.setattr(config, "NETWORK_LIMIT_TOOLS_MB", 2.01)
        assert _net_common.TOOLS.ceiling() == 2_010_000  # rounded, not cut
        monkeypatch.setattr(config, "NETWORK_LIMIT_TOOLS_MB", 0)
        assert not _net_common.TOOLS.enabled()

    def test_under_the_ceiling_is_admitted_and_at_it_is_refused(
            self, ledger, monkeypatch):
        _limits(monkeypatch, tools=0.001)
        meter = _net_common.TOOLS
        meter.record(999)
        meter.admit()
        meter.record(1)
        with pytest.raises(NetworkLimitReached) as caught:
            meter.admit()
        reason = caught.value.reason
        assert caught.value.meter == "tools"
        assert "The tool network limit is reached" in reason
        assert "1.0 kB of 1.0 kB used in the last 24 h" in reason
        assert "network_limit_tools_mb in config.yaml" in reason
        assert "Room returns at about" in reason
        assert "do not work around it" in reason

    def test_an_upload_that_would_pass_the_ceiling_is_refused_before_it_is_sent(
            self, ledger, monkeypatch):
        _limits(monkeypatch, provider=0.001)
        meter = _net_common.PROVIDER
        meter.record(400)
        meter.admit(600)
        with pytest.raises(NetworkLimitReached) as caught:
            meter.admit(601)
        assert "would be passed" in caught.value.reason
        assert "sends 601 B and 600 B of 1.0 kB is left" in caught.value.reason
        # The model never reads a provider refusal; the user does.
        assert "work around" not in caught.value.reason

    def test_a_request_bigger_than_the_whole_ceiling_has_no_room_time(
            self, ledger, monkeypatch):
        _limits(monkeypatch, provider=0.001)
        with pytest.raises(NetworkLimitReached) as caught:
            _net_common.PROVIDER.admit(5000)
        assert "cannot admit this request" in caught.value.reason
        assert "Room returns" not in caught.value.reason

    def test_room_returns_as_the_oldest_bytes_leave_the_window(
            self, ledger, clock, monkeypatch):
        _limits(monkeypatch, tools=0.001, hours=1)
        meter = _net_common.TOOLS
        meter.record(600)
        clock.advance(600)
        meter.record(600)
        state = meter.state()
        assert state.full
        expected = int((clock.now - 600) // 60 * 60) + 60 + HOUR
        assert state.room_at == expected
        clock.now = state.room_at - 1
        with pytest.raises(NetworkLimitReached):
            meter.admit()
        clock.now = state.room_at
        meter.admit()

    def test_a_run_of_refusals_warns_once_and_again_after_an_admission(
            self, ledger, clock, monkeypatch, caplog):
        _limits(monkeypatch, tools=0.001, hours=1)
        meter = _net_common.TOOLS
        meter.record(1000)
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                with pytest.raises(NetworkLimitReached):
                    meter.admit()
            clock.advance(2 * HOUR)
            meter.admit()
            meter.record(1000)
            with pytest.raises(NetworkLimitReached):
                meter.admit()
        warned = [r for r in caplog.records
                  if r.getMessage().startswith("Network limit:")]
        assert len(warned) == 2

    def test_the_meters_are_separate(self, ledger, monkeypatch):
        """IO1: a full tool meter does not stop a model call."""
        _limits(monkeypatch, tools=0.001, provider=0.001)
        _net_common.TOOLS.record(5000)
        with pytest.raises(NetworkLimitReached):
            _net_common.TOOLS.admit()
        _net_common.PROVIDER.admit(10)

    def test_an_estimate_is_kept_apart_and_named(self, ledger, monkeypatch):
        _limits(monkeypatch, tools=1)
        _net_common.TOOLS.record(300)
        _net_common.record_web_search(5)
        state = _net_common.TOOLS.state()
        assert state.estimated == 5 * _net_common.WEB_SEARCH_BYTES_PER_RESULT
        assert state.used == state.estimated + 300


# ---------------------------------------------------------------------------
# ---- Sizing an exchange exactly (IO3, IO4) ---------------------------------
# ---------------------------------------------------------------------------

class _Chunks(httpx2.SyncByteStream):
    """A body that arrives in pieces, as a real transport's does -- a
    `content=` response is read at construction and would count nothing."""

    def __init__(self, data: bytes, size: int = 700) -> None:
        self._data, self._size = data, size

    def __iter__(self):
        for start in range(0, len(self._data), self._size):
            yield self._data[start:start + self._size]


def _head_size(status_line: str, headers: list) -> int:
    return (len(status_line) + 2
            + sum(len(k) + len(v) + 4 for k, v in headers) + 2)


class TestSizingAnExchange:
    def test_a_request_is_its_line_headers_and_body(self):
        request = httpx2.Request("POST", "https://api.example/v1/x?a=1",
                                 headers={"x-k": "v"}, content=b"z" * 1234)
        raw = request.headers.raw
        expected = (len("POST /v1/x?a=1 HTTP/1.1\r\n")
                    + sum(len(k) + len(v) + 4 for k, v in raw) + 2 + 1234)
        assert _net_common.request_bytes(request) == expected

    def test_a_gzipped_body_counts_as_the_bytes_that_were_sent(self):
        payload = b"abcdefghij" * 5000
        wire = gzip.compress(payload)

        def handler(request):
            return httpx2.Response(
                200, headers={"content-encoding": "gzip"}, stream=_Chunks(wire))

        with httpx2.Client(transport=httpx2.MockTransport(handler)) as http:
            response = http.get("https://h.example/page")
        assert response.content == payload
        assert response.num_bytes_downloaded == len(wire)
        total = _net_common.exchange_bytes(response)
        assert total == (_net_common.request_bytes(response.request)
                         + _net_common.response_head_bytes(response)
                         + len(wire))

    def test_every_redirect_hop_is_counted(self):
        def handler(request):
            if request.url.path == "/a":
                return httpx2.Response(302, headers={"location": "/b"},
                                       stream=_Chunks(b"moved"))
            return httpx2.Response(200, stream=_Chunks(b"x" * 1000))

        with httpx2.Client(transport=httpx2.MockTransport(handler),
                           follow_redirects=True) as http:
            response = http.get("https://h.example/a")
        hop = response.history[0]
        assert _net_common.exchange_bytes(response) == (
            _net_common.request_bytes(hop.request)
            + _net_common.response_head_bytes(hop) + 5
            + _net_common.request_bytes(response.request)
            + _net_common.response_head_bytes(response) + 1000)

    def test_a_shape_it_cannot_read_counts_zero_and_never_raises(self):
        class Odd:
            history = ()

            @property
            def request(self):
                raise RuntimeError("The request instance has not been set")

        assert _net_common.exchange_bytes(Odd()) == 0
        assert _net_common.request_bytes(object()) == 0


# ---------------------------------------------------------------------------
# ---- The provider and MCP hooks (IO4, IO11) ----------------------------------
# ---------------------------------------------------------------------------

class TestTheProviderHooks:
    def _client(self, handler):
        return httpx2.Client(transport=httpx2.MockTransport(handler),
                             event_hooks=_net_common.provider_event_hooks())

    def test_a_call_is_counted_both_ways_at_the_wire(self, ledger, monkeypatch):
        _limits(monkeypatch, provider=1)
        body = b"data: {}\n\n" * 300

        def handler(request):
            return httpx2.Response(200, stream=_Chunks(body, 64))

        with self._client(handler) as http:
            response = http.post("https://api.example/v1/messages",
                                 content=b"q" * 4000)
            response.read()
        used = _net_common.PROVIDER.state().used
        assert used == (_net_common.request_bytes(response.request)
                        + _net_common.response_head_bytes(response) + len(body))

    def test_a_refused_request_never_reaches_the_transport(
            self, ledger, monkeypatch):
        _limits(monkeypatch, provider=0.001)
        sent = []

        def handler(request):
            sent.append(request)
            return httpx2.Response(200)

        with self._client(handler) as http, pytest.raises(NetworkLimitReached):
            http.post("https://api.example/v1/messages", content=b"q" * 2000)
        assert sent == []
        assert _net_common.PROVIDER.state().used == 0

    @pytest.mark.asyncio
    async def test_the_mcp_hooks_count_and_never_refuse(self, ledger, monkeypatch):
        _limits(monkeypatch, mcp=0.000001)          # one byte
        _net_common.MCP.record(50)

        class AsyncChunks(httpx2.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(10):
                    yield b"e" * 100

        def handler(request):
            return httpx2.Response(200, stream=AsyncChunks())

        async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler),
                event_hooks=_net_common.mcp_event_hooks()) as http:
            response = await http.post("https://mcp.example/mcp", content=b"{}")
            await response.aread()
        assert _net_common.MCP.state().used == (
            50 + _net_common.request_bytes(response.request)
            + _net_common.response_head_bytes(response) + 1000)


class TestProviderErrorsReadTheChain:
    """Measured: anthropic wraps a refusal raised in its client as
    APIConnectionError("Connection error."), after two retries of its own."""

    @staticmethod
    def _wrapped():
        class APIConnectionError(Exception):
            pass

        refusal = NetworkLimitReached("The provider API network limit is "
                                      "reached: 5.0 MB of 5.0 MB.", "provider")
        try:
            try:
                raise refusal
            except NetworkLimitReached as inner:
                raise APIConnectionError("Connection error.") from inner
        except APIConnectionError as outer:
            return outer, refusal

    def test_a_wrapped_refusal_is_not_transient(self):
        outer, _ = self._wrapped()
        assert provider_errors.is_transient(outer) is False

    def test_a_wrapped_refusal_is_described_as_itself(self):
        outer, refusal = self._wrapped()
        assert provider_errors.describe(outer) == (
            "NetworkLimitReached: " + refusal.reason)

    def test_an_ordinary_connection_error_is_still_transient(self):
        class APIConnectionError(Exception):
            pass

        assert provider_errors.is_transient(APIConnectionError("boom"))

    def test_a_cyclic_chain_ends(self):
        exc = ValueError("loop")
        exc.__context__ = exc
        assert provider_errors.network_refusal(exc) is None


class TestTheProviderClients:
    @pytest.fixture(autouse=True)
    def fresh(self):
        from core import client

        for name in ("OPENAI_LIKE", "ANTHROPIC", "GOOGLE"):
            client.forget_client(name)
        yield
        for name in ("OPENAI_LIKE", "ANTHROPIC", "GOOGLE"):
            client.forget_client(name)

    def test_off_builds_the_client_it_always_built(self, monkeypatch):
        import sys

        from core import client

        _limits(monkeypatch)
        made = []
        monkeypatch.setattr(sys.modules["openai"], "DefaultHttpxClient",
                            lambda **kw: made.append(kw) or "http-client",
                            raising=False)
        assert client._metered("openai") == {}
        assert made == []
        monkeypatch.setattr(client, "load_provider_data", lambda: {
            "OPENAI_LIKE": {"API_KEY": "k", "API_URL": "http://x/v1",
                            "is_v1_compatible": True}})
        built = client.api_initialization("OPENAI_LIKE")
        assert built.kwargs == {"base_url": "http://x/v1", "api_key": "k"}

    @pytest.mark.parametrize("sdk", ["openai", "anthropic"])
    def test_on_uses_the_sdks_own_default_client_with_the_hooks(
            self, monkeypatch, sdk):
        import sys

        from core import client

        _limits(monkeypatch, provider=1)
        made = []
        monkeypatch.setattr(sys.modules[sdk], "DefaultHttpxClient",
                            lambda **kw: made.append(kw) or "http-client",
                            raising=False)
        assert client._metered(sdk) == {"http_client": "http-client"}
        hooks = made[0]["event_hooks"]
        assert len(hooks["request"]) == len(hooks["response"]) == 1

    def test_on_gives_google_an_httpx2_client_that_follows_redirects(
            self, monkeypatch):
        import sys

        from core import client

        _limits(monkeypatch, provider=1)
        monkeypatch.setattr(sys.modules["google.genai.types"], "HttpOptions",
                            lambda **kw: kw, raising=False)
        options = client._metered("google")["http_options"]
        http = options["httpx_client"]
        try:
            assert isinstance(http, httpx2.Client)
            assert http.follow_redirects is True
            assert http.event_hooks["request"]
        finally:
            http.close()

    def test_on_reaches_the_constructor(self, monkeypatch):
        from core import client

        _limits(monkeypatch, provider=1)
        monkeypatch.setattr(client, "_metered",
                            lambda sdk: {"http_client": f"metered-{sdk}"})
        monkeypatch.setattr(client, "load_provider_data", lambda: {
            "OPENAI_LIKE": {"API_KEY": "k", "API_URL": "http://x/v1",
                            "is_v1_compatible": True}})
        built = client.api_initialization("OPENAI_LIKE")
        assert built.kwargs["http_client"] == "metered-openai"


# ---------------------------------------------------------------------------
# ---- The tools (IO3, IO6, IO15) --------------------------------------------
# ---------------------------------------------------------------------------

def _fill_tools(monkeypatch):
    _limits(monkeypatch, tools=0.001)
    _net_common.TOOLS.record(5000)


@pytest.mark.usefixtures("public_dns")
class TestTheTools:
    """`fetch_url`'s host is cleared by a lookup before the meter is asked
    (batch 243), so without `public_dns` the policy check refused the URL
    offline and the meter was never reached."""

    def test_fetch_url_is_refused_before_any_request(self, monkeypatch, http):
        from tools.builtin import fetch_url

        _fill_tools(monkeypatch)
        with pytest.raises(NetworkLimitReached):
            fetch_url.run({"url": "https://example.com/page"})
        assert http.requests == []

    def test_dispatch_hands_the_model_the_meters_own_sentence(
            self, monkeypatch, http, caplog):
        from tools.registry import registry

        _fill_tools(monkeypatch)
        with caplog.at_level(logging.WARNING):
            result = registry.dispatch(
                "fetch_url", {"url": "https://example.com/page"})
        assert result["error"].startswith("The tool network limit is reached")
        assert "failed:" not in result["error"]
        # A refusal is not a crash: no traceback is logged for it.
        assert not any(r.exc_info for r in caplog.records)

    def test_web_search_is_charged_the_measured_estimate(self, monkeypatch):
        from tools.builtin import web_search

        _limits(monkeypatch, tools=10)
        web_search._call_ddgs("q", 5)
        web_search._call_ddgs("q", 10, page=2)
        state = _net_common.TOOLS.state()
        assert state.used == state.estimated == (
            15 * _net_common.WEB_SEARCH_BYTES_PER_RESULT)

    def test_web_search_does_not_retry_a_refusal(self, monkeypatch):
        from tools.builtin import web_search

        _fill_tools(monkeypatch)
        calls = []
        monkeypatch.setattr(web_search, "DDGS",
                            lambda **kw: calls.append(kw))
        with pytest.raises(NetworkLimitReached):
            web_search.run({"query": "anything"})
        assert calls == []

    def test_arxiv_lets_a_refusal_through_its_retry_loop(self, monkeypatch, http):
        from tools.builtin import arxiv

        _fill_tools(monkeypatch)
        with pytest.raises(NetworkLimitReached):
            arxiv.run({"keywords": "graphs"})
        assert http.requests == []

    def test_the_scholar_lookup_stops_at_a_refusal_rather_than_retrying(
            self, monkeypatch):
        from core.reasoning import scholar

        monkeypatch.setattr(config, "SCHOLAR_MAX_RETRIES", 3)
        tried = []

        def refuse(path, params):
            tried.append(path)
            raise NetworkLimitReached("The tool network limit is reached.",
                                      "tools")

        lookup = scholar.ScholarLookup(enabled=True, fetch=refuse)
        assert lookup._get("works/x", {}) is None
        assert tried == ["works/x"]
        assert lookup.failed_reason.startswith("NetworkLimitReached")

    def test_the_scholar_transport_goes_through_the_meter(self, monkeypatch):
        from core.reasoning import scholar

        _fill_tools(monkeypatch)
        lookup = scholar.ScholarLookup(enabled=True)
        with pytest.raises(NetworkLimitReached):
            lookup._http_get("works/x", {})


# ---------------------------------------------------------------------------
# ---- MCP's admission (IO11) ------------------------------------------------
# ---------------------------------------------------------------------------

class TestMcpAdmission:
    def _client(self, transport):
        from mcp_client import config as mcp_config
        from mcp_client.client import MCPClient

        cfg = mcp_config.ServerConfig(
            name="srv", tier="user", path="<test>", transport=transport,
            url="https://mcp.example/mcp" if transport != "stdio" else None,
            command="unused" if transport == "stdio" else None)
        manager = MCPClient({"srv": cfg})
        manager.clients["srv"] = object()
        return manager

    @pytest.mark.parametrize("transport", ["http", "sse"])
    def test_a_full_meter_refuses_an_http_server_call_before_it_is_sent(
            self, monkeypatch, transport):
        from mcp_client import client as mcp_module

        _limits(monkeypatch, mcp=0.001)
        _net_common.MCP.record(1000)
        monkeypatch.setattr(mcp_module, "_ensure_loop", lambda: pytest.fail(
            "a refused call must not reach the event loop"))
        result = self._client(transport).call_tool("srv", "t", {"a": 1})
        assert result["error"].startswith("The MCP network limit is reached")

    def test_a_stdio_server_is_not_asked(self, monkeypatch):
        """IO5: a stdio server's traffic is its own process's."""
        from mcp_client import client as mcp_module

        _limits(monkeypatch, mcp=0.001)
        _net_common.MCP.record(1000)
        assert _net_common.MCP.state().full

        class Reached(Exception):
            pass

        def loop():
            raise Reached

        monkeypatch.setattr(mcp_module, "_ensure_loop", loop)
        with pytest.raises(Reached):
            self._client("stdio").call_tool("srv", "t", {})

    def test_an_http_server_gets_a_metered_client_only_while_the_meter_is_on(
            self, monkeypatch):
        from mcp_client import client as mcp_module

        made = []
        monkeypatch.setattr(mcp_module, "streamable_http_client",
                            lambda url, **kw: made.append(kw))
        manager = self._client("http")
        _limits(monkeypatch)
        manager._transport_for(manager.server_configs["srv"])
        _limits(monkeypatch, mcp=1)
        manager._transport_for(manager.server_configs["srv"])
        assert made[0]["http_client"] is None
        metered = made[1]["http_client"]
        assert isinstance(metered, httpx2.AsyncClient)
        assert metered.event_hooks["request"]

    def test_an_sse_server_gets_the_metered_factory(self, monkeypatch):
        from mcp_client import client as mcp_module

        made = []
        monkeypatch.setattr(mcp_module, "sse_client",
                            lambda url, **kw: made.append(kw))
        _limits(monkeypatch, mcp=1)
        manager = self._client("sse")
        manager._transport_for(manager.server_configs["srv"])
        assert made[0]["httpx_client_factory"] is mcp_module._metered_mcp_client


# ---------------------------------------------------------------------------
# ---- What a person reads (IO13) ----------------------------------------------
# ---------------------------------------------------------------------------

class TestNetworkReport:
    def test_with_every_meter_off_it_says_how_to_set_one(self, monkeypatch):
        _limits(monkeypatch)
        lines = _net_common.network_report()
        assert lines[0].startswith("No network limit is set")
        assert "network_limit_tools_mb" in lines[1]
        assert lines[-1] == _net_common.NOT_COUNTED

    def test_with_a_meter_on_it_lists_all_three(self, ledger, monkeypatch):
        _limits(monkeypatch, tools=0.001, provider=50)
        _net_common.TOOLS.record(400)
        _net_common.record_web_search(1)
        lines = _net_common.network_report()
        assert lines[0] == "Network use over the last 24 h:"
        tool, api, mcp = lines[1:4]
        assert "tool" in tool and "of 1.0 kB" in tool
        assert "21.0 kB of it estimated" in tool
        assert "full, room returns at about" in tool
        assert api.split() == ["provider", "API", "0", "B", "of", "50.0", "MB"]
        assert mcp.split() == ["MCP", "off"]
        assert ledger.path() in lines[4]
        assert lines[-1] == _net_common.NOT_COUNTED

    def test_the_not_counted_list_names_what_io5_requires(self):
        text = _net_common.NOT_COUNTED
        for thing in ("shell commands", "stdio MCP", "sign-in", "TLS",
                      "web_search is an estimate"):
            assert thing in text

    def test_config_yaml_writes_the_same_limits_down(self):
        import config_schema

        with open(config_schema.CONFIG_PATH, encoding="utf-8") as handle:
            text = handle.read()
        section = text[text.index("# Network limits"):
                       text.index("network_limit_mcp_mb:")]
        for thing in ("shell commands", '"stdio"', "sign-in", "TLS",
                      "estimate", "network.db", "once a minute"):
            assert thing in section, thing

    def test_the_cli_prints_the_same_lines(self, monkeypatch, capsys):
        import cli_commands

        _limits(monkeypatch)
        cli_commands._cmd_network(None, "")
        assert capsys.readouterr().out.splitlines() == (
            _net_common.network_report())

    def test_the_catalog_has_it_in_both_shells_within_the_panel(self):
        from core import commands

        spec = commands.lookup("network")
        assert spec.in_shell(commands.TUI) and spec.in_shell(commands.CLI)
        assert len(f"/{spec.name} ● {spec.summary}") <= 50


class TestTheSidebarText:
    def test_none_while_every_meter_is_off(self, monkeypatch):
        _limits(monkeypatch)
        assert _net_common.sidebar_text(_net_common.states()) is None

    def test_each_meter_that_is_on_with_an_estimate_marked(
            self, ledger, monkeypatch):
        _limits(monkeypatch, tools=500, provider=1000)
        _net_common.record_web_search(5)
        _net_common.PROVIDER.record(312 * MEGABYTE)
        assert _net_common.sidebar_text(_net_common.states()) == (
            "net · tools ~105k/500M · api 312M/1.0G · per 24 h")

    def test_a_full_meter_says_so(self, ledger, monkeypatch):
        _limits(monkeypatch, mcp=0.001)
        _net_common.MCP.record(2000)
        assert _net_common.sidebar_text(_net_common.states()) == (
            "net · mcp 2k/1k full · per 24 h")


class TestTheSidebarLine:
    @pytest.mark.asyncio
    async def test_hidden_by_default_and_shown_and_coloured_when_full(
            self, ledger, monkeypatch):
        from tests.conftest import pump
        from tui.app import VenastineApp
        from tui.widgets import NetworkLine

        _limits(monkeypatch)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            line = app.query_one("#network-line", NetworkLine)
            assert line.display is False

            _limits(monkeypatch, tools=0.001)
            _net_common.TOOLS.record(2000)
            app.refresh_network_line(force=True)
            await pilot.pause()
            assert line.display is True
            assert line.content.plain == "net · tools 2k/1k full · per 24 h"

    @pytest.mark.asyncio
    async def test_network_in_the_tui_writes_the_report(self, monkeypatch):
        from tests.conftest import pump
        from tui.app import VenastineApp

        _limits(monkeypatch)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        written = []
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            monkeypatch.setattr(app._transcript, "write_system",
                                written.append)
            from tui.app import _cmd_network

            _cmd_network(app, "")
        assert written == _net_common.network_report()

    def test_the_refresh_is_throttled_unless_forced(self, monkeypatch):
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        drawn = []

        class Line:
            def update_meters(self, states):
                drawn.append(states)

        app._network_widget = Line()
        app.refresh_network_line(force=True)
        app.refresh_network_line()
        app.refresh_network_line()
        app.refresh_network_line(force=True)
        assert len(drawn) == 2
        app._network_drawn_at = time.monotonic() - 1.0
        app.refresh_network_line()
        assert len(drawn) == 3
