"""
test_project_init.py

ROADMAP_v2 §24: `/init` — scaffolding a project's documentation set.

WHAT IS WORTH ASSERTING HERE. Most of §24 is assembly: an agent run through
the existing loop, a diff from difflib, a dispatch through the existing
registry. The tests are about the properties that assembly can silently lose:

  I1   the write goes through the permission layer, and the tool cannot be
       aimed at a path of the caller's choosing
  I2   reads are confined to the project and to documentation
  I5   no way to confirm ⇒ nothing is written (§25's V6, applied again)
  I6   an untrusted project is not laundered into a trusted one, and
       CONTEXT.md is never special-cased out of the D17 hash
  I10  stubs are templates; nothing invents project content
  I11  the index is generated, and is a pure function of the kind
  I12  an existing document is left exactly as it was
  I13  a blank folder is asked about rather than guessed at

Plus the §27 lesson one section on: the initializer is a SIXTH thread source
and has to say so, or it fills the picker with threads nobody will resume.
"""

import os

import pytest

import config
from core import config_loader, workspace_trust
from project_init import doc_sets, generator, manifest
from tools.builtin import project_docs


class _Resp:
    """A ModelResponse stand-in — the loop is faked at run_agent_conversation."""

    def __init__(self, text=""):
        self.text = text
        self.tool_calls = []
        self.thread_id = None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """The D17 trust store resolves at call time from HOME, so every test
    here gets its own. Without this, granting trust in a test writes into
    the developer's real ~/.config/venastine."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    yield
    config_loader.reset()


@pytest.fixture
def project(tmp_path):
    """A small but real Python project, initialized as the loader sees it."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "requirements.txt").write_text("pytest==8.0\n", encoding="utf-8")
    (root / "README.md").write_text("# Widget\n\nA widget factory.\n",
                                    encoding="utf-8")
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "tests").mkdir()
    config_loader.initialize(str(root))
    return root


@pytest.fixture
def fake_agent(monkeypatch):
    """Fake the one model call. Returns the list of kwargs it was given, so
    a test can assert on the budget, the step cap and the thread kind."""
    from core.loop import RunAgentLoop

    calls = []

    def _run(**kwargs):
        calls.append(kwargs)
        return _Resp("# Widget\n\nA widget factory.\n")

    monkeypatch.setattr(RunAgentLoop, "run_agent_conversation",
                        staticmethod(_run))
    return calls


def _generate(**overrides):
    kwargs = {
        "model": "m",
        "provider_name": "P",
        "confirm": lambda _summary: True,
        "notify": lambda _text: None,
        "choose_kind": lambda proposal, _reason, _blank: proposal,
    }
    kwargs.update(overrides)
    return generator.generate(**kwargs)


# ---------------------------------------------------------------------------
# ---- The two scoped tools --------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheScopedTools:

    def test_both_are_declared_so_the_import_check_passes(self):
        """D24. The check runs at import of tools.registry; if either field
        were missing, importing it here would already have raised."""
        from tools.registry import registry
        assert "read_project_doc" in registry._tools
        assert "write_project_doc" in registry._tools
        assert config.ToolPermissions().read_project_doc is True
        assert config.ToolPermissions().write_project_doc is True

    def test_the_write_is_gated_and_the_read_is_not(self):
        """I1/I2. Writing into someone's project is the side that needs a
        human; reading its README at their explicit request is not."""
        assert config.ToolApprovals().write_project_doc is True
        assert config.ToolApprovals().read_project_doc is False

    def test_write_takes_a_name_not_a_path(self, project):
        """I1. The schema has no path parameter at all, which is what makes
        'cannot be aimed elsewhere' structural rather than validated."""
        properties = project_docs.WRITE_TOOL_SCHEMA["input_schema"]["properties"]
        assert set(properties) == {"name", "content"}

    def test_write_refuses_an_unknown_document_name(self, project):
        result = project_docs.write_run(
            {"name": "../../etc/passwd", "content": "x"})
        assert "error" in result
        assert not os.path.exists(os.path.join(str(project), "..", "etc"))

    def test_write_refuses_a_name_that_is_a_path_into_the_project(self, project):
        """The allowlist is by NAME, so a relative path that would land
        somewhere legal is still not one of the project's documents."""
        result = project_docs.write_run(
            {"name": "src/ARCHITECTURE.md", "content": "x"})
        assert "error" in result

    def test_context_goes_to_venastine_and_the_rest_to_the_root(self, project):
        """I9. The hub is the only file §14's loader reads, and it reads it
        from .venastine/ only."""
        root = str(project)
        assert project_docs.doc_path(root, "CONTEXT.md") == os.path.join(
            workspace_trust.venastine_dir(root), "CONTEXT.md")
        assert project_docs.doc_path(root, "ARCHITECTURE.md") == os.path.join(
            root, "ARCHITECTURE.md")

    def test_read_is_confined_to_the_project(self, project, tmp_path):
        outside = tmp_path / "secret.md"
        outside.write_text("# secrets\n", encoding="utf-8")
        result = project_docs.read_run({"path": "../secret.md"})
        assert "error" in result
        assert "outside" in result["error"]

    def test_read_follows_symlinks_before_deciding(self, project, tmp_path):
        """realpath BEFORE the containment test. A link that looks contained
        and resolves outside is the case the ordering exists for."""
        outside = tmp_path / "secret.md"
        outside.write_text("# secrets\n", encoding="utf-8")
        link = project / "innocent.md"
        try:
            os.symlink(str(outside), str(link))
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("symlinks unavailable here")
        result = project_docs.read_run({"path": "innocent.md"})
        assert "error" in result
        assert "secrets" not in str(result)

    def test_read_refuses_non_documents(self, project):
        (project / "settings.py").write_text("SECRET = 1\n", encoding="utf-8")
        result = project_docs.read_run({"path": "settings.py"})
        assert "error" in result
        assert "SECRET" not in str(result)

    def test_read_refuses_the_credential_files(self, project):
        for name, body in (("providers.json", '{"ANTHROPIC": {"key": "sk-1"}}'),
                           (".env", "GITHUB_TOKEN=ghp_1\n")):
            (project / name).write_text(body, encoding="utf-8")
            assert "error" in project_docs.read_run({"path": name})

    def test_read_refuses_inside_venastine(self, project):
        venastine = project / ".venastine"
        venastine.mkdir()
        (venastine / "notes.md").write_text("# private\n", encoding="utf-8")
        result = project_docs.read_run({"path": ".venastine/notes.md"})
        assert "error" in result

    def test_read_caps_below_the_general_read_tool(self, project):
        """I7. MAX_READ_CHARS would be re-billed on every later step by a
        meter that re-counts the whole prompt (TECHNICAL_DEBT item 9)."""
        assert config.INIT_READ_CHARS < config.MAX_READ_CHARS
        (project / "BIG.md").write_text("x" * (config.INIT_READ_CHARS + 500),
                                        encoding="utf-8")
        result = project_docs.read_run({"path": "BIG.md"})
        assert result["truncated"] is True
        assert len(result["content"]) == config.INIT_READ_CHARS
        assert f"offset={config.INIT_READ_CHARS}" in result["message"]

    def test_read_resumes_from_an_offset(self, project):
        (project / "BIG.md").write_text("a" * config.INIT_READ_CHARS + "TAIL",
                                        encoding="utf-8")
        result = project_docs.read_run(
            {"path": "BIG.md", "offset": config.INIT_READ_CHARS})
        assert result["content"] == "TAIL"
        assert result["truncated"] is False


# ---------------------------------------------------------------------------
# ---- The document sets -----------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheDocumentSets:

    def test_the_two_sets_share_only_the_standards_document(self):
        shared = set(doc_sets.document_set("software")) & set(
            doc_sets.document_set("research"))
        assert shared == {"DOCUMENTATION_STANDARDS.md"}

    def test_the_allowlist_is_the_union_plus_the_hub(self):
        """A project that started as research and grew a codebase must be
        able to gain an ARCHITECTURE.md without being re-classified first."""
        names = doc_sets.all_document_names()
        assert "CONTEXT.md" in names
        assert set(doc_sets.document_set("software")) <= names
        assert set(doc_sets.document_set("research")) <= names

    def test_an_unknown_kind_raises_rather_than_defaulting(self):
        with pytest.raises(ValueError):
            doc_sets.document_set("webapp")

    def test_every_document_has_a_stub_template(self):
        for kind in doc_sets.PROJECT_KINDS:
            for name in doc_sets.document_set(kind):
                assert doc_sets.render_stub(name)

    def test_a_stub_says_what_belongs_and_what_does_not(self):
        stub = doc_sets.render_stub("ARCHITECTURE.md")
        assert "**What belongs here:**" in stub
        assert "**What does NOT:**" in stub
        assert "_Not yet written._" in stub

    def test_a_stub_carries_only_facts_it_was_given(self):
        """I10. Nothing here invents project content."""
        bare = doc_sets.render_stub("TEST_WRITING.md")
        assert "What `/init` established" not in bare
        withfacts = doc_sets.render_stub(
            "TEST_WRITING.md", {"test_command": "pytest -q"})
        assert "pytest -q" in withfacts

    def test_the_index_is_a_pure_function_of_the_kind(self):
        """I11, and the defect that found it: an index that varied with what
        already existed rewrote CONTEXT.md on every subsequent /init and
        labelled files /init had just authored as pre-existing."""
        assert doc_sets.render_index("software") == doc_sets.render_index(
            "software")
        index = doc_sets.render_index("software")
        for name in doc_sets.document_set("software"):
            assert f"[{name}](../{name})" in index

    def test_the_index_links_out_of_venastine(self):
        """CONTEXT.md lives one level down, so a bare ./NAME.md would be a
        broken link in the one file every agent reads."""
        assert "](../ARCHITECTURE.md)" in doc_sets.render_index("software")


# ---------------------------------------------------------------------------
# ---- Discovery -------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestDiscovery:

    def test_facts_come_off_manifests(self, project):
        facts = manifest.detect_facts(str(project))
        assert facts["stack"] == "Python"
        assert facts["test_command"] == "pytest"
        assert facts["test_dirs"] == ["tests"]
        assert "main.py" in facts["entry_points"]

    def test_the_manifest_lists_documents_with_sizes(self, project):
        text = manifest.build_manifest(str(project))
        assert "README.md" in text
        assert "— Widget" in text  # the document's first heading
        assert "bytes" in text

    def test_the_manifest_hides_credential_files(self, project):
        (project / "providers.json").write_text('{"k": "sk-1"}',
                                                encoding="utf-8")
        assert "providers.json" not in manifest.build_manifest(str(project))

    def test_the_manifest_is_stable_across_runs(self, project):
        """Byte-identical input is what makes /init's output diffable."""
        assert manifest.build_manifest(str(project)) == manifest.build_manifest(
            str(project))

    def test_a_blank_folder_is_recognised(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / ".git").mkdir()
        assert manifest.is_blank(str(empty)) is True

    def test_a_project_with_files_is_not_blank(self, project):
        assert manifest.is_blank(str(project)) is False

    def test_software_is_proposed_from_a_code_manifest(self, project):
        kind, reason = generator.propose_kind(str(project))
        assert kind == doc_sets.SOFTWARE
        assert "Python" in reason

    def test_research_is_proposed_without_one(self, tmp_path):
        root = tmp_path / "study"
        root.mkdir()
        (root / "notes.md").write_text("# Study\n", encoding="utf-8")
        kind, _reason = generator.propose_kind(str(root))
        assert kind == doc_sets.RESEARCH


# ---------------------------------------------------------------------------
# ---- Generating ------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestGenerating:

    def test_it_writes_the_hub_and_the_set(self, project, fake_agent):
        notice = _generate()
        assert (project / ".venastine" / "CONTEXT.md").exists()
        for name in doc_sets.document_set("software"):
            assert (project / name).exists(), name
        assert "CONTEXT.md" in notice["text"]

    def test_the_hub_carries_the_generated_index(self, project, fake_agent):
        _generate()
        body = (project / ".venastine" / "CONTEXT.md").read_text(
            encoding="utf-8")
        assert "## Project documentation" in body
        assert "](../ARCHITECTURE.md)" in body

    def test_the_manifest_reaches_the_agent_as_the_task(self, project,
                                                        fake_agent):
        """I3, and §19's K6 by implication: the listing is the USER MESSAGE.
        In the system prompt it would travel into all ten research passes."""
        _generate()
        assert "README.md" in fake_agent[0]["user_goal"]
        assert "README.md" not in (fake_agent[0]["system_prompt"] or "")

    def test_an_existing_context_is_given_to_the_agent_to_revise(
            self, project, fake_agent):
        """I4. Hand edits survive by design, not by the user rejecting a
        diff and re-applying them."""
        venastine = project / ".venastine"
        venastine.mkdir()
        (venastine / "CONTEXT.md").write_text(
            "# Widget\n\nHAND WRITTEN NOTE.\n", encoding="utf-8")
        _generate()
        assert "HAND WRITTEN NOTE." in fake_agent[0]["user_goal"]

    def test_the_run_is_labelled_as_a_subagent_thread(self, project,
                                                      fake_agent):
        """§27: the initializer is a sixth thread source. Unlabelled, it
        clutters the picker with threads nobody will resume."""
        from storage import THREAD_KIND_SUBAGENT
        _generate()
        assert fake_agent[0]["thread_kind"] == THREAD_KIND_SUBAGENT

    def test_the_run_gets_its_own_budget_not_the_chat_one(self, project,
                                                          fake_agent):
        """I7 / §26's reasoning: a tool-heavy run on MAX_TOKEN_BUDGET is cut
        off by a meter that re-counts the whole prompt every step."""
        _generate()
        assert fake_agent[0]["max_total_tokens"] == config.INIT_TOKEN_BUDGET
        assert config.INIT_TOKEN_BUDGET > config.MAX_TOKEN_BUDGET

    def test_the_agent_can_only_read(self, project, fake_agent):
        """The initializer writes nothing itself (I8) — the shell does."""
        _generate()
        allowed = fake_agent[0]["context"].allowed_tools
        assert set(allowed) == {"read_project_doc"}

    def test_an_existing_document_is_left_exactly_as_it_was(self, project,
                                                            fake_agent):
        (project / "ARCHITECTURE.md").write_text("HAND WRITTEN\n",
                                                 encoding="utf-8")
        notice = _generate()
        assert (project / "ARCHITECTURE.md").read_text(
            encoding="utf-8") == "HAND WRITTEN\n"
        assert "Left alone" in notice["text"]

    def test_a_second_run_changes_nothing(self, project, fake_agent):
        """I11's property, end to end: /init is idempotent."""
        _generate()
        before = (project / ".venastine" / "CONTEXT.md").read_text(
            encoding="utf-8")
        notice = _generate()
        assert "Nothing to do" in notice["text"]
        assert (project / ".venastine" / "CONTEXT.md").read_text(
            encoding="utf-8") == before

    def test_the_research_set_is_scaffolded_when_chosen(self, project,
                                                        fake_agent):
        _generate(kind=doc_sets.RESEARCH)
        assert (project / "FINDINGS.md").exists()
        assert not (project / "ARCHITECTURE.md").exists()

    def test_an_empty_agent_response_writes_nothing(self, project, monkeypatch):
        from core.loop import RunAgentLoop
        monkeypatch.setattr(RunAgentLoop, "run_agent_conversation",
                            staticmethod(lambda **_kw: _Resp("")))
        with pytest.raises(generator.InitError):
            _generate()
        assert not (project / ".venastine" / "CONTEXT.md").exists()

    def test_a_missing_initializer_agent_is_reported_not_crashed(
            self, project, monkeypatch):
        from agents.manager import manager
        monkeypatch.setattr(manager, "get", lambda _name: None)
        with pytest.raises(generator.InitError):
            _generate()


# ---------------------------------------------------------------------------
# ---- Consent ---------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestConsent:

    def test_no_confirm_route_writes_nothing(self, project, fake_agent):
        """I5 / §25's V6: the inability to ask is not permission to
        proceed. This is the sixth place that rule applies."""
        notice = _generate(confirm=None)
        assert not (project / ".venastine" / "CONTEXT.md").exists()
        assert not (project / "ARCHITECTURE.md").exists()
        assert "no way to confirm" in notice["text"]

    def test_declining_writes_nothing(self, project, fake_agent):
        notice = _generate(confirm=lambda _s: False)
        assert not (project / ".venastine" / "CONTEXT.md").exists()
        assert "Declined" in notice["text"]

    def test_the_diff_is_shown_before_the_question(self, project, fake_agent):
        """AC2: regeneration must not silently overwrite. The user sees the
        change before being asked, not a filename."""
        order = []
        _generate(notify=lambda text: order.append(("notify", text)),
                  confirm=lambda summary: (order.append(("confirm", summary)),
                                           True)[1])
        kinds = [k for k, _ in order]
        assert kinds.index("confirm") == len(kinds) - 1
        assert any("+# Widget" in text for k, text in order if k == "notify")

    def test_the_consent_summary_names_every_file(self, project, fake_agent):
        summaries = []
        _generate(confirm=lambda summary: (summaries.append(summary), True)[1])
        assert "ARCHITECTURE.md" in summaries[0]
        assert "CONTEXT.md" in summaries[0]

    def test_one_consent_covers_the_whole_command(self, project, fake_agent):
        """write_project_doc is approval-gated, so a naive implementation
        prompts eight times — which is the shape of consent that gets
        clicked through."""
        calls = []
        _generate(confirm=lambda summary: (calls.append(summary), True)[1])
        assert len(calls) == 1

    def test_the_write_goes_through_the_registry(self, project, monkeypatch,
                                                 fake_agent):
        """AC3/I1. Not open() from the handler — dispatch means the approval
        gate, check_input_policy and output redaction all still apply."""
        from tools import registry as registry_mod

        seen = []
        real = registry_mod.registry.dispatch

        def _spy(tool_name, params, **kwargs):
            seen.append((tool_name, kwargs.get("approval_callback") is not None))
            return real(tool_name, params, **kwargs)

        monkeypatch.setattr(registry_mod.registry, "dispatch", _spy)
        _generate()
        assert seen, "nothing was dispatched — the write bypassed the registry"
        assert {name for name, _ in seen} == {"write_project_doc"}
        assert all(has_callback for _name, has_callback in seen)

    def test_a_denied_write_is_reported_not_swallowed(self, project,
                                                      monkeypatch, fake_agent):
        from tools import registry as registry_mod
        from tools.registry import ToolCallDenied

        def _deny(*_a, **_kw):
            raise ToolCallDenied("write_project_doc is disabled by policy")

        monkeypatch.setattr(registry_mod.registry, "dispatch", _deny)
        with pytest.raises(generator.InitError):
            _generate()


# ---------------------------------------------------------------------------
# ---- Project kind ----------------------------------------------------------
# ---------------------------------------------------------------------------

class TestChoosingTheKind:

    def test_an_established_project_gets_a_proposal_and_a_reason(
            self, project, fake_agent):
        seen = []
        _generate(choose_kind=lambda proposal, reason, blank: (
            seen.append((proposal, reason, blank)), proposal)[1])
        proposal, reason, blank = seen[0]
        assert proposal == doc_sets.SOFTWARE
        assert reason
        assert blank is False

    def test_a_blank_folder_is_asked_with_no_proposal(self, tmp_path,
                                                      fake_agent):
        """I13. There is nothing to infer from, so a proposal would be a
        guess wearing a finding's clothes."""
        root = tmp_path / "blank"
        root.mkdir()
        config_loader.initialize(str(root))
        seen = []
        _generate(choose_kind=lambda proposal, reason, blank: (
            seen.append((proposal, blank)), doc_sets.RESEARCH)[1])
        assert seen[0] == (None, True)
        assert (root / "FINDINGS.md").exists()

    def test_cancelling_the_kind_question_writes_nothing(self, project,
                                                         fake_agent):
        notice = _generate(choose_kind=lambda *_a: None)
        assert "Cancelled" in notice["text"]
        assert not (project / ".venastine" / "CONTEXT.md").exists()

    def test_no_way_to_ask_and_no_flag_refuses(self, project, fake_agent):
        with pytest.raises(generator.InitError):
            _generate(choose_kind=None)

    def test_an_explicit_kind_skips_the_question(self, project, fake_agent):
        asked = []
        _generate(kind=doc_sets.SOFTWARE,
                  choose_kind=lambda *_a: asked.append(1))
        assert asked == []

    def test_an_unknown_explicit_kind_is_refused(self, project, fake_agent):
        with pytest.raises(generator.InitError):
            _generate(kind="webapp")


# ---------------------------------------------------------------------------
# ---- Workspace trust -------------------------------------------------------
# ---------------------------------------------------------------------------

class TestWorkspaceTrust:

    def test_a_trusted_project_stays_trusted(self, project, fake_agent):
        venastine = project / ".venastine"
        venastine.mkdir()
        (venastine / "CONTEXT.md").write_text("old\n", encoding="utf-8")
        workspace_trust.grant_trust(str(project))
        assert workspace_trust.is_trusted(str(project))

        notice = _generate()

        assert workspace_trust.is_trusted(str(project))
        assert "re-granted" in notice["text"]

    def test_an_untrusted_project_is_not_laundered(self, project, fake_agent):
        """I6. `.venastine/` may already hold agents, skills and an mcp.json
        that arrived with a repo someone cloned. Granting trust as a side
        effect of /init would wave all of it through."""
        venastine = project / ".venastine"
        venastine.mkdir()
        (venastine / "mcp.json").write_text('{"mcpServers": {}}',
                                            encoding="utf-8")
        config_loader.initialize(str(project))
        assert not workspace_trust.is_trusted(str(project))

        notice = _generate()

        assert not workspace_trust.is_trusted(str(project))
        assert "not trusted" in notice["text"]

    def test_context_md_still_counts_towards_the_hash(self, project,
                                                      fake_agent):
        """The other half of I6: no special-casing. If CONTEXT.md were
        excluded from the hash, /init would need no re-grant at all — and
        D17 would have a hole shaped exactly like this command."""
        venastine = project / ".venastine"
        venastine.mkdir()
        (venastine / "CONTEXT.md").write_text("old\n", encoding="utf-8")
        before = workspace_trust._content_hash(str(project))

        _generate()

        assert workspace_trust._content_hash(str(project)) != before


# ---------------------------------------------------------------------------
# ---- The TUI half ----------------------------------------------------------
# ---------------------------------------------------------------------------
#
# Driven through Textual's pilot rather than by calling the handler, for
# test_tui.py's reason: a handler called by hand proves the function works,
# not that the app wires it up. Both modals are asserted through the WORKER'S
# OUTCOME rather than through the modal rendering -- a modal that appears and
# drops its answer leaves the worker blocked on a queue forever, which
# presents as a hang and not as a failure.

async def _settle(pilot, predicate, tries: int = 200):
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause()
    return False


@pytest.mark.asyncio
async def test_the_tui_asks_the_kind_then_confirms_then_writes(
        project, fake_agent, monkeypatch):
    from tui.app import VenastineApp
    from tui.screens import ConfirmScreen, ProjectKindScreen
    from project_init.tui_commands import _cmd_init

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_init(app, "")

        assert await _settle(
            pilot, lambda: isinstance(app.screen, ProjectKindScreen)), \
            "the project-kind modal never opened"
        app.screen.dismiss("software")

        assert await _settle(
            pilot, lambda: isinstance(app.screen, ConfirmScreen)), \
            "the confirmation never opened"
        app.screen.dismiss(True)

        assert await _settle(
            pilot,
            lambda: (project / ".venastine" / "CONTEXT.md").exists()), \
            "the worker never wrote CONTEXT.md"
        assert await _settle(pilot, lambda: app._busy is False)


@pytest.mark.asyncio
async def test_dismissing_the_tui_confirmation_writes_nothing(
        project, fake_agent):
    """Escape on the confirmation must decline, not hang. The worker is
    parked on a queue; a dismissal carrying no value never unblocks it."""
    from tui.app import VenastineApp
    from tui.screens import ConfirmScreen, ProjectKindScreen
    from project_init.tui_commands import _cmd_init

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_init(app, "--software")

        assert await _settle(
            pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("escape")

        assert await _settle(pilot, lambda: app._busy is False), \
            "the worker never came back — the dismissal carried no value"
        assert not (project / ".venastine" / "CONTEXT.md").exists()
        assert not (project / "ARCHITECTURE.md").exists()
        assert not isinstance(app.screen, ProjectKindScreen)


@pytest.mark.asyncio
async def test_cancelling_the_kind_modal_never_reaches_the_confirmation(
        project, fake_agent):
    from tui.app import VenastineApp
    from tui.screens import ConfirmScreen, ProjectKindScreen
    from project_init.tui_commands import _cmd_init

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_init(app, "")

        assert await _settle(
            pilot, lambda: isinstance(app.screen, ProjectKindScreen))
        await pilot.press("escape")

        assert await _settle(pilot, lambda: app._busy is False)
        assert not isinstance(app.screen, ConfirmScreen)
        assert not (project / ".venastine" / "CONTEXT.md").exists()


@pytest.mark.asyncio
async def test_the_kind_modal_is_told_what_was_proposed(project, fake_agent):
    """§23 moved these arguments into a Request payload dict, so a typo in
    a key now silently shows an empty modal instead of failing at a
    signature. The pilot above only dismisses the screen; this asserts the
    screen was given something to render."""
    from tui.app import VenastineApp
    from tui.screens import ProjectKindScreen
    from project_init.tui_commands import _cmd_init

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_init(app, "")
        assert await _settle(
            pilot, lambda: isinstance(app.screen, ProjectKindScreen))
        # Captured and DISMISSED before asserting. A failed assertion with
        # the modal still up leaves the worker parked on its queue and
        # blocks run_test()'s teardown, so the regression presents as a
        # stalled suite rather than a red test (BREAKING_CHANGES §14).
        # Releasing first costs nothing and makes this one fail properly.
        proposal, reason, blank = (app.screen._proposal, app.screen._reason,
                                   app.screen._blank)
        app.screen.dismiss(None)
        assert await _settle(pilot, lambda: app._busy is False)

        assert proposal == doc_sets.SOFTWARE
        assert "Python" in reason
        assert blank is False


@pytest.mark.asyncio
async def test_the_confirmation_is_told_what_it_is_confirming(project,
                                                              fake_agent):
    """The other half: the diff summary travels in the payload too."""
    from tui.app import VenastineApp
    from tui.screens import ConfirmScreen
    from project_init.tui_commands import _cmd_init

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_init(app, "--software")
        assert await _settle(
            pilot, lambda: isinstance(app.screen, ConfirmScreen))
        body, label = app.screen._body, app.screen._confirm_label
        app.screen.dismiss(False)
        assert await _settle(pilot, lambda: app._busy is False)

        assert "CONTEXT.md" in body
        assert label == "Write"


@pytest.mark.asyncio
async def test_an_explicit_flag_skips_the_kind_modal(project, fake_agent):
    from tui.app import VenastineApp
    from tui.screens import ConfirmScreen
    from project_init.tui_commands import _cmd_init

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_init(app, "--research")
        assert await _settle(
            pilot, lambda: isinstance(app.screen, ConfirmScreen)), \
            "--research should go straight to the confirmation"
        app.screen.dismiss(True)
        assert await _settle(pilot, lambda: (project / "FINDINGS.md").exists())


def test_an_unknown_flag_is_reported(project):
    """Registered and reachable, and a typo says so rather than becoming a
    chat turn — the registry's rule for unknown commands, applied to args."""
    from tui.commands import registry as commands

    assert commands.get("init") is not None

    class _T:
        def __init__(self):
            self.errors = []

        def write_error(self, text):
            self.errors.append(text)

    class _App:
        _busy = False

        def __init__(self):
            self._transcript = _T()

    from project_init.tui_commands import _cmd_init
    app = _App()
    _cmd_init(app, "--webapp")
    assert app._transcript.errors and "webapp" in app._transcript.errors[0]


@pytest.mark.asyncio
async def test_an_unrecognised_kind_answer_cancels(project, fake_agent):
    """The fail-safe decode, exercised by the value that actually reaches
    it. Escape dismisses with None, which cancels whether or not the guard
    is there — so that path proves nothing. `_release_permission_channel`
    puts a bare FALSE on app shutdown, and without the guard that False is
    returned as if it were a project kind.

    Same rule ask_review_blocking follows, and the reason it is a rule: a
    dismissal path added later must cancel rather than scaffold a document
    set nobody chose.
    """
    from tui.app import VenastineApp
    from tui.screens import ConfirmScreen, ProjectKindScreen
    from project_init.tui_commands import _cmd_init

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_init(app, "")

        assert await _settle(
            pilot, lambda: isinstance(app.screen, ProjectKindScreen))
        app.screen.dismiss(False)  # what the shutdown release puts

        assert await _settle(pilot, lambda: app._busy is False)
        assert not isinstance(app.screen, ConfirmScreen)
        assert not (project / ".venastine" / "CONTEXT.md").exists()

        # Nothing is written either way: generate() refuses an unrecognised
        # kind downstream, so asserting on files passes with or without the
        # guard. What the guard buys is WHICH outcome -- a clean
        # cancellation, rather than an error quoting an internal value the
        # user neither chose nor typed.
        entries = app._transcript._entries
        assert any(role == "system" and "Cancelled" in text
                   for role, text in entries), entries
        assert not any(role == "error" for role, _text in entries), entries
