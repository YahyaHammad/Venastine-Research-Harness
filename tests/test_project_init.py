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
from types import SimpleNamespace

import pytest

from tests.conftest import settle
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


def _hand_back_reversed(monkeypatch, project):
    """Make the filesystem answer in the worst order it plausibly could.

    Both sort assertions below are about `sorted()` and neither can see it
    while the OS hands back sorted names anyway -- which NTFS does, and
    which is what made the tests these replace vacuous (#98). Reversing at
    the source is the only way the sort is the thing under test.
    """
    root = os.path.realpath(str(project))
    real_listdir, real_walk = os.listdir, os.walk

    def _listdir(path=".", *a, **kw):
        out = real_listdir(path, *a, **kw)
        try:
            here = os.path.realpath(path)
        except (TypeError, ValueError):
            return out
        return list(reversed(sorted(out))) if here == root else out

    def _walk(top, *a, **kw):
        for dirpath, dirs, files in real_walk(top, *a, **kw):
            # `dirs` in place: os.walk descends whatever the caller leaves
            # in it, and _tree prunes by assigning to the slice.
            dirs[:] = list(reversed(sorted(dirs)))
            yield dirpath, dirs, list(reversed(sorted(files)))

    monkeypatch.setattr(os, "listdir", _listdir)
    monkeypatch.setattr(os, "walk", _walk)


def _fail_write_of(monkeypatch, filename):
    """Make one write fail the way a real one does (#95).

    Patched at `open` rather than at `write_run` or `dispatch`, so the
    whole chain runs: OSError -> write_run's error dict -> _write's
    InitError. Patching higher up would test the handler against a failure
    shape the handler was already written for.
    """
    import builtins

    real_open = builtins.open

    def _open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if (isinstance(path, (str, os.PathLike))
                and os.path.basename(os.fspath(path)) == filename
                and "w" in mode):
            raise OSError(28, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _open)


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

    def test_read_refuses_env_files_with_a_suffix(self, project):
        """Issue #58. `_is_readable_doc` tests `name.startswith(".env")`, not
        equality, and the existing credential test only uses `.env` exactly --
        so narrowing it to `==` would keep that test green while exposing
        `.env.local`, `.env.production` and every other conventional variant.
        The `.txt` one matters most: its extension is in _DOC_EXTENSIONS, so
        the prefix rule is the ONLY thing refusing it."""
        for name in (".env.local", ".env.production", ".env.txt"):
            (project / name).write_text("GITHUB_TOKEN=ghp_1\n", encoding="utf-8")
            result = project_docs.read_run({"path": name})
            assert "error" in result, f"{name} was readable"
            assert "ghp_1" not in str(result)

    def test_read_refuses_a_denied_segment_at_any_depth(self, project):
        """Issue #58. `_DENIED_SEGMENTS` is checked against EVERY path
        segment, and the existing test only puts `.venastine` first -- so a
        check that looked at `parts[0]` alone would stay green while a
        document nested under a denied directory became readable."""
        nested = project / "docs" / ".venastine"
        nested.mkdir(parents=True)
        (nested / "notes.md").write_text("# private\n", encoding="utf-8")
        assert "error" in project_docs.read_run(
            {"path": "docs/.venastine/notes.md"})

        deep = project / "sub" / "node_modules" / "pkg"
        deep.mkdir(parents=True)
        (deep / "README.md").write_text("# vendored\n", encoding="utf-8")
        assert "error" in project_docs.read_run(
            {"path": "sub/node_modules/pkg/README.md"})

    @pytest.mark.parametrize("name", [
        "setup.py", "setup.cfg", "Gemfile", "pom.xml", "build.gradle",
    ])
    def test_the_five_build_manifests_are_readable_at_the_root(
            self, project, name):
        """#94. These were on neither list while pyproject.toml,
        package.json, Cargo.toml, go.mod, Makefile and Dockerfile were on
        the exact-name one -- an incomplete enumeration, not a judgement.
        requirements.txt got in by accident, through `.txt`."""
        (project / name).write_text("# a build file\n", encoding="utf-8")
        result = project_docs.read_run({"path": name})
        assert "error" not in result
        assert "a build file" in result["content"]

    @pytest.mark.parametrize("name", [
        "setup.py", "setup.cfg", "Gemfile", "pom.xml", "build.gradle",
    ])
    def test_a_nested_copy_of_a_build_manifest_is_refused(
            self, project, name):
        """The tightening that ships WITH the widening. Every other entry
        in _DOC_FILENAMES matches on basename at any depth, which is right
        for packages/foo/package.json in a monorepo and wrong here: a
        vendored tree is where unreviewed third-party source lives, and it
        is the one place these five are not this project's own statement
        about itself."""
        vendor = project / "src" / "vendor"
        vendor.mkdir(parents=True)
        (vendor / name).write_text("# somebody else's\n", encoding="utf-8")
        result = project_docs.read_run({"path": f"src/vendor/{name}"})
        assert "error" in result
        assert "somebody else" not in str(result)

    @pytest.mark.parametrize("name", [
        "main.py", "config.py", "credentials.py", "app.py", "conftest.py",
    ])
    def test_adding_setup_py_did_not_add_python(self, project, name):
        """The control on the widening, and the question that decided its
        shape: the five are added BY EXACT NAME, so `.py` never enters
        _DOC_EXTENSIONS and this is five filenames rather than a class of
        file. A suffix entry would have turned read_project_doc into `read`
        with no approval gate, for every run in the harness."""
        (project / name).write_text("SECRET = 1\n", encoding="utf-8")
        result = project_docs.read_run({"path": name})
        assert "error" in result
        assert "SECRET" not in str(result)
        assert ".py" not in project_docs._DOC_EXTENSIONS

    def test_read_still_allows_an_ordinary_nested_document(self, project):
        """Discriminates the two above from a rule that refuses everything
        nested."""
        docs = project / "docs" / "design"
        docs.mkdir(parents=True)
        (docs / "NOTES.md").write_text("# fine\n", encoding="utf-8")
        result = project_docs.read_run({"path": "docs/design/NOTES.md"})
        assert "error" not in result
        assert "# fine" in result["content"]

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

    @pytest.mark.parametrize("kind", ["software", "research"])
    def test_the_index_is_a_pure_function_of_the_kind(self, kind, tmp_path):
        """I11, and the defect that found it: an index that varied with what
        already existed rewrote CONTEXT.md on every subsequent /init and
        labelled files /init had just authored as pre-existing.

        REWRITTEN in batch 14 (#98). The old version asserted
        `render_index("software") == render_index("software")` and then
        that every name in the set appeared. Neither assertion pinned the
        property in the name. The first is true of any deterministic
        function of anything -- and was true of the reverted design I11
        exists because of, since two calls in one expression see the same
        filesystem. The second catches a MISSING name and cannot see an
        extra one, so building the index from `all_document_names()` --
        putting all seven research documents into a software project's hub
        -- was green on all 1406.

        This asserts the exact line set, which catches both directions, and
        varies the filesystem between the two calls, which is what "pure"
        was supposed to mean."""
        index = doc_sets.render_index(kind)
        links = [ln for ln in index.splitlines() if ln.startswith("- [")]

        expected = [f"- [{n}](../{n}) — {doc_sets._STUBS[n][0]}"
                    for n in doc_sets.document_set(kind)]
        assert links == expected

        other = "research" if kind == "software" else "software"
        for name in doc_sets.document_set(other):
            if name not in doc_sets.document_set(kind):
                assert f"[{name}](../{name})" not in index

        # Purity against a filesystem that changed, not against itself.
        for name in doc_sets.document_set(kind):
            (tmp_path / name).write_text("# already here\n", encoding="utf-8")
        assert doc_sets.render_index(kind) == index

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

    def test_every_advertised_path_is_one_the_tool_accepts(self, project):
        """#94, and the sentence the manifest prints over its listing:
        "Paths below are exactly what read_project_doc expects." It was
        not. The listing came from a parallel test in manifest.py and the
        tool's set from project_docs.py, and nothing kept them in step --
        advertised 15, refused 6, driven against the real read_run.

        Each refusal costs one of the initializer's twelve steps, spent
        learning something the manifest could have said, and the model has
        no way to know the listing is unreliable."""
        for name in ("Gemfile", "pom.xml", "build.gradle", "setup.py",
                     "setup.cfg", "Makefile", "Dockerfile", "docs.rst"):
            (project / name).write_text("# x\n", encoding="utf-8")

        advertised = [ln[2:].split(" (")[0]
                      for ln in manifest.build_manifest(str(project)).splitlines()
                      if ln.startswith("- ") and " (" in ln]
        assert advertised, "the fixture must produce a listing to check"

        refused = [n for n in advertised
                   if "error" in project_docs.read_run({"path": n})]
        assert refused == []

    def test_nothing_readable_is_hidden_from_the_listing(self, project):
        """The other direction, which is the one nobody would notice. `.txt`
        is in _DOC_EXTENSIONS and was not in the manifest's extension test,
        so EVERY .txt document in a project was readable and invisible; and
        license / licence / changelog / notice reached the listing only if
        they happened to carry a .md extension."""
        for name in ("notes.txt", "LICENSE", "CHANGELOG", "NOTICE"):
            (project / name).write_text("# x\n", encoding="utf-8")

        advertised = [ln[2:].split(" (")[0]
                      for ln in manifest.build_manifest(str(project)).splitlines()
                      if ln.startswith("- ") and " (" in ln]
        for name in ("notes.txt", "LICENSE", "CHANGELOG", "NOTICE"):
            assert name in advertised

    def test_a_readme_the_tool_cannot_read_is_not_advertised(self, project):
        """`readme.bin` was listed because the manifest accepted any name
        starting with `readme` whatever its extension. It is fixed here
        rather than by widening the allowlist: nothing needs a .bin
        readable, and the defect was the listing's."""
        (project / "readme.bin").write_bytes(b"\x00\x01binary")
        text = manifest.build_manifest(str(project))
        listing = text.split("## Readable documents")[1].split("## Layout")[0]
        assert "readme.bin" not in listing
        # Still in the LAYOUT, which is not an advertisement: the tree says
        # what the project contains and the listing says what can be read.
        # Conflating those two is the whole of this finding.
        assert "readme.bin" in text

    def test_the_facts_block_names_the_manifests_it_found(self, project):
        """The evidence propose_kind acted on, shown to the agent as well
        as to the user (#96). A Dockerfile appears here and in no stack
        line, which is the distinction the proposal now turns on."""
        (project / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        text = manifest.build_manifest(str(project))
        assert "Code manifests present:" in text
        assert "Dockerfile" in text.split("## Readable documents")[0]

    def test_the_manifest_hides_credential_files(self, project):
        (project / "providers.json").write_text('{"k": "sk-1"}',
                                                encoding="utf-8")
        assert "providers.json" not in manifest.build_manifest(str(project))

    def test_the_manifest_is_stable_across_runs(self, project, monkeypatch):
        """Byte-identical input is what makes /init's output diffable.

        REWRITTEN in batch 14 (#98). The old version compared two calls in
        one process against an unchanged directory, so `os.listdir` handed
        back the same order both times whatever `sorted()` did -- removing
        it was green. "Across runs" means across processes and machines and
        after unrelated files come and go, which is exactly when listdir's
        order moves, and none of that is reachable from one expression.

        Sorted order IS checkable in one process, and it is the mechanism
        the property rests on."""
        for name in ("zebra.md", "alpha.md", "middle.md"):
            (project / name).write_text("# x\n", encoding="utf-8")

        _hand_back_reversed(monkeypatch, project)

        listed = [ln[2:].split(" (")[0]
                  for ln in manifest.build_manifest(str(project)).splitlines()
                  if ln.startswith("- ") and " (" in ln]
        assert listed == sorted(listed)
        assert {"alpha.md", "middle.md", "zebra.md"} <= set(listed)

    def test_the_layout_is_sorted_too(self, project, monkeypatch):
        """`_tree` sorts twice -- directories and files -- and the same
        argument applies to both: an unsorted walk produces a different
        manifest for the same project on a different machine, and every
        /init after the first shows a diff nobody caused.

        This one needed the reversing helper twice over. Written first
        against three ordinary filenames, it SURVIVED the mutation that
        deletes the sort, because os.walk hands back an already-sorted list
        on this filesystem -- an input that cannot discriminate, which is
        the exact defect #98 filed against the test this replaces. The
        finding's own pass caught it in the fix for the finding."""
        pkg = project / "pkg"
        pkg.mkdir()
        for name in ("zebra.py", "alpha.py", "middle.py"):
            (pkg / name).write_text("x\n", encoding="utf-8")
        _hand_back_reversed(monkeypatch, project)

        lines = manifest.build_manifest(str(project)).splitlines()
        entries = [ln.strip().split("/")[-1] for ln in lines
                   if ln.strip().startswith("pkg/")
                   and ln.strip().endswith(".py")]
        assert entries == sorted(entries)
        assert len(entries) == 3, "the fixture must produce entries to sort"

    def test_make_test_is_claimed_only_when_the_target_exists(
            self, tmp_path):
        """I10: a fact in a committed document is read as established, and
        a wrong one is worse than an absent one. A Makefile is a build, not
        a test suite, so `make test` is claimed off the target line and
        nothing else."""
        root = tmp_path / "proj"
        root.mkdir()
        make = root / "Makefile"

        make.write_text("all:\n\tcc -o app main.c\n", encoding="utf-8")
        assert "test_command" not in manifest.detect_facts(str(root))

        make.write_text("all:\n\tcc -o app main.c\n\ntest:\n\t./app -t\n",
                        encoding="utf-8")
        assert manifest.detect_facts(str(root))["test_command"] == "make test"

    def test_a_tests_target_is_not_a_test_target(self, tmp_path):
        """The control on the line above. A target name is exact, and
        matching `test` as a prefix would claim `make test` for a Makefile
        whose only target is `tests`."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "Makefile").write_text("tests:\n\t./run\n", encoding="utf-8")
        assert "test_command" not in manifest.detect_facts(str(root))

    def test_rspec_is_claimed_only_with_a_spec_directory(self, tmp_path):
        """Ruby's runner is a choice, not a given -- minitest and rspec are
        both ordinary, so a bare Gemfile names no command."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "Gemfile").write_text("source 'x'\n", encoding="utf-8")
        assert "test_command" not in manifest.detect_facts(str(root))

        (root / "spec").mkdir()
        facts = manifest.detect_facts(str(root))
        assert facts["test_command"] == "bundle exec rspec"

    def test_the_gradle_wrapper_is_preferred_when_the_project_ships_one(
            self, tmp_path):
        """Shipping a wrapper is a statement about which Gradle to use, and
        running a different one is how a build that passes in CI fails on a
        laptop."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
        assert manifest.detect_facts(str(root))["test_command"] == "gradle test"

        (root / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        assert (manifest.detect_facts(str(root))["test_command"]
                == "./gradlew test")

    def test_package_json_still_wins_over_pytest(self, tmp_path):
        """Precedence the table must not quietly reorder: a command read
        out of the project beats a default, and package.json's scripts.test
        beats pytest because a project holding both has decided
        something."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        (root / "package.json").write_text('{"scripts": {"test": "jest"}}\n',
                                           encoding="utf-8")
        assert manifest.detect_facts(str(root))["test_command"] == "npm test"

    def test_one_stack_is_named_once_however_many_files_say_it(
            self, tmp_path):
        """Four of the twelve filenames say Python. The stack line is for a
        human reading a confirmation prompt."""
        root = tmp_path / "proj"
        root.mkdir()
        for name in ("pyproject.toml", "setup.py", "setup.cfg",
                     "requirements.txt"):
            (root / name).write_text("x\n", encoding="utf-8")
        facts = manifest.detect_facts(str(root))
        assert facts["stack"] == "Python"
        assert len(facts["code_manifests"]) == 4

    def test_the_manifest_list_is_in_table_order_not_set_order(
            self, tmp_path):
        """`code_manifests` is printed to the user behind a proposal, so it
        is a list in a fixed order. It was a set, which prints in whatever
        order the interpreter felt like that run."""
        root = tmp_path / "proj"
        root.mkdir()
        for name in ("Dockerfile", "go.mod", "pyproject.toml"):
            (root / name).write_text("x\n", encoding="utf-8")
        found = manifest.detect_facts(str(root))["code_manifests"]
        assert found == ["pyproject.toml", "go.mod", "Dockerfile"]

    def test_the_walk_prunes_the_directories_that_would_flood_it(
            self, project):
        """#98: nothing pinned the prune, and dropping `_PRUNED` outright
        was green on all 1406. Its own comment states the stake --
        node_modules "can be tens of thousands of files, which turns a
        listing into a denial of service on the context window"."""
        for name in ("node_modules", "__pycache__", ".venv", "dist"):
            d = project / name
            d.mkdir()
            (d / "junk.py").write_text("x\n", encoding="utf-8")

        text = manifest.build_manifest(str(project))
        for name in ("node_modules", "__pycache__", ".venv", "dist"):
            assert name not in text

    def test_the_walk_prunes_dotted_directories_too(self, project):
        """The second half of the same line, and the one that matters most:
        `.venastine` holds mcp.json and settings.json, and neither filename
        is covered by `_is_secret`. With the dotfile filter gone they reach
        the model in the listing. Dropping `_PRUNED` and dropping
        `startswith(".")` are two mutations, so they get two tests."""
        secrets = project / ".secretstuff"
        secrets.mkdir()
        (secrets / "notes.md").write_text("# private\n", encoding="utf-8")

        text = manifest.build_manifest(str(project))
        assert ".secretstuff" not in text
        assert "private" not in text

    def test_a_directory_that_should_be_walked_still_is(self, project):
        """The control. A prune that pruned everything would satisfy both
        tests above while making the layout useless."""
        pkg = project / "widgets"
        pkg.mkdir()
        (pkg / "core.py").write_text("x\n", encoding="utf-8")
        assert "widgets" in manifest.build_manifest(str(project))

    def test_a_heading_is_read_from_the_head_of_the_file(self, project):
        """#98. `_first_heading` reads 4 KB, and its docstring names the
        cost it is avoiding: this runs over every root document before the
        agent has chosen anything, and DEVLOG.md is 226 KB in this repo
        alone -- about 1 MB of reads per manifest build if the guard goes.
        Changing `f.read(4096)` to `f.read()` was green.

        A heading past the cap is not found, which is the observable
        consequence of the bound and the only way to pin it from outside.

        The padding is BLANK LINES, and that is the whole test. Written
        first with 5000 x's it survived the mutation, because
        `_first_heading` returns on the first non-empty line it sees -- so
        the heading below was unreachable whether the read was capped or
        not, and the assertion held for a reason having nothing to do with
        the cap. #98's own shape, in #98's own fix."""
        big = project / "BIG.md"
        big.write_text("\n" * 5000 + "# Buried\n", encoding="utf-8")
        assert "Buried" not in manifest.build_manifest(str(project))

        small = project / "SMALL.md"
        small.write_text("# Found\n\nbody\n", encoding="utf-8")
        assert "Found" in manifest.build_manifest(str(project))

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

    @pytest.mark.parametrize("filename,stack", [
        ("pom.xml", "Java/Maven"),
        ("build.gradle", "Java/Gradle"),
        ("Gemfile", "Ruby"),
        ("pyproject.toml", "Python"),
        ("setup.py", "Python"),
        ("setup.cfg", "Python"),
        ("requirements.txt", "Python"),
        ("package.json", "Node/JavaScript"),
        ("Cargo.toml", "Rust"),
        ("go.mod", "Go"),
    ])
    def test_every_manifest_that_names_a_stack_proposes_software(
            self, tmp_path, filename, stack):
        """#96. `propose_kind` branched on the four stacks `detect_facts`
        happened to set, not on the twelve filenames the manifest walks
        for, so Java, Ruby and Gradle projects were proposed as RESEARCH."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / filename).write_text("x\n", encoding="utf-8")
        kind, reason = generator.propose_kind(str(root))
        assert kind == doc_sets.SOFTWARE
        assert stack in reason
        assert manifest.detect_facts(str(root))["stack"] == stack

    @pytest.mark.parametrize("filename", ["Makefile", "Dockerfile"])
    def test_a_build_file_proposes_software_with_no_stack_claimed(
            self, tmp_path, filename):
        """The other half of #96, and the reason the fix is not "give every
        manifest a stack". A Makefile is C, Go, LaTeX or this repository; a
        Dockerfile says nothing about what is inside the image. Both are
        strong evidence of a codebase and none at all about the language,
        so the proposal is software and `stack` stays absent."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / filename).write_text("x\n", encoding="utf-8")
        kind, reason = generator.propose_kind(str(root))
        assert kind == doc_sets.SOFTWARE
        assert filename in reason
        assert "stack" not in manifest.detect_facts(str(root))

    def test_the_reason_names_the_files_it_found(self, tmp_path):
        """I13 returns the reason so the confirmation prompt can show its
        working, and for this heuristic the filename IS the working. The
        old reason named a stack the user then had to take on trust."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        (root / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        _kind, reason = generator.propose_kind(str(root))
        assert "pom.xml" in reason and "Dockerfile" in reason

    def test_the_wrong_reason_is_not_printed_for_a_project_that_has_one(
            self, tmp_path):
        """The control on the sentence itself. The defect was not only the
        kind: the printed reason SAID no code manifests were found while
        the manifest listed one, which is the half a user could act on."""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "Gemfile").write_text("source 'x'\n", encoding="utf-8")
        _kind, reason = generator.propose_kind(str(root))
        assert "no code manifests found" not in reason
        assert "Gemfile" in manifest.build_manifest(str(root))

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

    def test_the_run_inherits_the_configured_spend_cap(self, project,
                                                       fake_agent):
        """#4: there is no separate /init ceiling any more. Omitting the
        kwarg lets the wrapper resolve settings.json max_token_budget --
        uncapped by default -- exactly as every other path does."""
        _generate()
        assert "max_total_tokens" not in fake_agent[0]

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

    def test_a_write_that_fails_names_the_files_it_already_created(
            self, project, monkeypatch, fake_agent):
        """#95. `written` was built up and thrown away on the failure path,
        so a user read "Could not write TECHNICAL_DEBT.md" with no way to
        tell whether nothing had happened or most of it had.

        The failure injected here is the REACHABLE one: `write_run` turns
        any OSError into an error dict and `_write` raises `InitError` for
        it. The only handler was `except ToolCallDenied`, which a normal
        /init cannot reach because consent is granted a few lines above."""
        _fail_write_of(monkeypatch, "TECHNICAL_DEBT.md")

        with pytest.raises(generator.InitError) as exc:
            _generate()

        message = str(exc.value)
        assert "TECHNICAL_DEBT.md" in message
        assert "CONTEXT.md" in message
        assert "ARCHITECTURE.md" in message
        # and the count is the count, not the whole document set
        created = [n for n in doc_sets.document_set("software")
                   if (project / n).exists()]
        assert f"Wrote {len(created) + 1} files before that" in message

    def test_a_failure_before_anything_was_written_says_so(
            self, project, monkeypatch, fake_agent):
        """The control on the line above: a message listing files must not
        appear when there are none, and "Nothing was written" is a
        different sentence from a list of length zero."""
        _fail_write_of(monkeypatch, "CONTEXT.md")

        with pytest.raises(generator.InitError) as exc:
            _generate()
        assert "Nothing was written." in str(exc.value)

    def test_a_partial_write_leaves_a_trusted_project_trusted(
            self, project, monkeypatch, fake_agent):
        """The half with teeth. The partial write has already moved the D17
        content hash, so a project the user trusted a moment ago is
        untrusted -- exactly the state I6 exists to prevent, reached by the
        route I6 was not looking at. Driven before the fix: True -> False,
        and nothing said so."""
        root = os.path.realpath(str(project))
        workspace_trust.grant_trust(root)
        assert workspace_trust.is_trusted(root) is True

        _fail_write_of(monkeypatch, "TECHNICAL_DEBT.md")
        with pytest.raises(generator.InitError) as exc:
            _generate()

        assert workspace_trust.is_trusted(root) is True
        assert "trust re-granted" in str(exc.value)

    def test_a_partial_write_does_not_launder_an_untrusted_project(
            self, project, monkeypatch, fake_agent):
        """I6's other half, and the control that stops the fix above from
        becoming a hole. An untrusted project's .venastine/ may already
        hold agents, skills and an mcp.json that arrived with a clone;
        granting trust as a side effect of a FAILED /init would launder all
        of it in through a door nobody is watching.

        The clone is simulated rather than assumed: is_trusted() answers
        True for a project with no .venastine/ at all ("nothing to trust"),
        so an agent definition has to be there for the question to mean
        anything."""
        root = os.path.realpath(str(project))
        agents = project / ".venastine" / "agents"
        agents.mkdir(parents=True)
        (agents / "arrived_with_the_repo.md").write_text(
            "---\nname: x\n---\nbody\n", encoding="utf-8")
        assert workspace_trust.is_trusted(root) is False

        _fail_write_of(monkeypatch, "TECHNICAL_DEBT.md")
        with pytest.raises(generator.InitError):
            _generate()

        assert workspace_trust.is_trusted(root) is False

    def test_a_partial_write_is_not_rolled_back(self, project, monkeypatch,
                                                fake_agent):
        """U7, recorded as a test because the alternative is tempting.
        Rolling back would delete documents out of someone's project on an
        error path, and it would be a lie about the one file that matters:
        CONTEXT.md's previous content was overwritten at the first step and
        cannot be restored."""
        _fail_write_of(monkeypatch, "TECHNICAL_DEBT.md")
        with pytest.raises(generator.InitError):
            _generate()
        assert (project / "ARCHITECTURE.md").exists()
        assert not (project / "TECHNICAL_DEBT.md").exists()


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

@pytest.mark.asyncio
async def test_the_tui_asks_the_kind_then_confirms_then_writes(
        project, fake_agent, monkeypatch):
    from tui.app import VenastineApp
    from tui.screens import ConfirmScreen, ProjectKindScreen
    from project_init.tui_commands import _cmd_init

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _cmd_init(app, "")

        assert await settle(
            pilot, lambda: isinstance(app.screen, ProjectKindScreen)), \
            "the project-kind modal never opened"
        app.screen.dismiss("software")

        assert await settle(
            pilot, lambda: isinstance(app.screen, ConfirmScreen)), \
            "the confirmation never opened"
        app.screen.dismiss(True)

        assert await settle(
            pilot,
            lambda: (project / ".venastine" / "CONTEXT.md").exists()), \
            "the worker never wrote CONTEXT.md"
        assert await settle(pilot, lambda: app._busy is False)


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

        assert await settle(
            pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("escape")

        assert await settle(pilot, lambda: app._busy is False), \
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

        assert await settle(
            pilot, lambda: isinstance(app.screen, ProjectKindScreen))
        await pilot.press("escape")

        assert await settle(pilot, lambda: app._busy is False)
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
        assert await settle(
            pilot, lambda: isinstance(app.screen, ProjectKindScreen))
        # Captured and DISMISSED before asserting. A failed assertion with
        # the modal still up leaves the worker parked on its queue and
        # blocks run_test()'s teardown, so the regression presents as a
        # stalled suite rather than a red test (BREAKING_CHANGES §14).
        # Releasing first costs nothing and makes this one fail properly.
        proposal, reason, blank = (app.screen._proposal, app.screen._reason,
                                   app.screen._blank)
        app.screen.dismiss(None)
        assert await settle(pilot, lambda: app._busy is False)

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
        assert await settle(
            pilot, lambda: isinstance(app.screen, ConfirmScreen))
        body, label = app.screen._body, app.screen._confirm_label
        app.screen.dismiss(False)
        assert await settle(pilot, lambda: app._busy is False)

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
        assert await settle(
            pilot, lambda: isinstance(app.screen, ConfirmScreen)), \
            "--research should go straight to the confirmation"
        app.screen.dismiss(True)
        assert await settle(pilot, lambda: (project / "FINDINGS.md").exists())


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

        assert await settle(
            pilot, lambda: isinstance(app.screen, ProjectKindScreen))
        app.screen.dismiss(False)  # what the shutdown release puts

        assert await settle(pilot, lambda: app._busy is False)
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


class TestTheCliInitGoesDownTheChannel:
    """§29 (N5), audit #7. §23 slice 1 said generate()'s confirm and
    choose_kind are "a shell-agnostic API the CLI implements too" and
    moved the TUI onto the response channel. The CLI is the half that was
    never moved, and kept two guards written by hand.
    """

    @staticmethod
    def _run(monkeypatch, tmp_path, lines, interactive=True):
        """Drive run_init_command, capturing what generate() was handed."""
        import main
        from project_init import generator
        from tests.conftest import FakeStdinReader

        seen = {}

        def _fake_generate(**kwargs):
            seen.update(kwargs)
            return {"text": "did nothing", "written": []}

        reader = FakeStdinReader(lines)
        monkeypatch.setattr(main, "_stdin_reader", lambda: reader)
        monkeypatch.setattr(generator, "generate", _fake_generate)
        monkeypatch.setattr(main.sys.stdin, "isatty", lambda: interactive)

        args = SimpleNamespace(research_project=False, software_project=False)
        code = main.run_init_command(args, "ANTHROPIC", "m")
        return code, seen, reader

    def test_confirm_is_asked_through_the_channel(self, monkeypatch, tmp_path):
        code, seen, reader = self._run(monkeypatch, tmp_path, ["y"])
        assert code == 0
        assert seen["confirm"]("ARCHITECTURE.md, CONTEXT.md") is True
        assert reader.prompts, "nothing was ever put to the user"

    def test_choose_kind_is_asked_through_the_channel(self, monkeypatch,
                                                     tmp_path):
        _, seen, _ = self._run(monkeypatch, tmp_path, ["research"])
        assert seen["choose_kind"]("software", "it has a setup.py",
                                   False) == "research"

    def test_the_prompts_carry_no_deadline(self, monkeypatch, tmp_path):
        """N2. There is no run waiting on this answer, so a deadline
        would only discard the answer of someone who stepped away
        mid-decision -- and it must not claim one it does not have."""
        _, seen, reader = self._run(monkeypatch, tmp_path, ["y"])
        seen["confirm"]("some files")
        assert reader.timeouts == [None]
        assert "s)" not in reader.prompts[0], reader.prompts[0]

    def test_a_pipe_passes_no_confirm_route_at_all(self, monkeypatch,
                                                   tmp_path):
        """The trap in the migration. generate() reads confirm=None as
        "report what you WOULD write without writing it" (I5/V6) -- which
        is not the same answer as a channel that declines. Handing it a
        channel on a pipe turns a dry run into a refusal.
        """
        _, seen, _ = self._run(monkeypatch, tmp_path, [], interactive=False)
        assert seen["confirm"] is None
        assert seen["choose_kind"] is None


# ===========================================================================
# ---- §31 (H7), #55: read_project_doc reads a page, not the file -----------
# ===========================================================================


class TestReadingOnePageCostsOnePage:
    """This was `text = f.read()` followed by
    `text[offset:offset + INIT_READ_CHARS]`, so returning 20,000 characters
    of a 30 MB document cost 60 MB of peak traced memory -- and cost it
    AGAIN on every page, because each call re-read the whole file to slice a
    different part of it.

    Not fixed with read_run's getsize guard, deliberately: refusing a large
    document is not what this tool is for. I7 chose INIT_READ_CHARS so that
    /init could page through exactly such a document, and a size check would
    have removed the capability instead of the cost.
    """

    def _long_doc(self, project, chars):
        path = project / "LONG.md"
        path.write_text("z" * chars, encoding="utf-8")
        return path

    def test_the_whole_file_is_not_held_to_return_one_page(self, project):
        import tracemalloc

        self._long_doc(project, 4_000_000)

        tracemalloc.start()
        try:
            result = project_docs.read_run({"path": "LONG.md"})
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert len(result["content"]) == config.INIT_READ_CHARS
        assert peak < 1_000_000, (
            f"{peak} bytes held to return {config.INIT_READ_CHARS} "
            "characters -- the whole file is still being read")

    def test_a_LATER_page_does_not_re_read_the_prefix_into_memory(self,
                                                                  project):
        """The half a size check would not have fixed. A paging loop used to
        pay for the entire document on every call, so page 100 cost the same
        as page 1 and both cost the whole file."""
        import tracemalloc

        self._long_doc(project, 4_000_000)

        tracemalloc.start()
        try:
            result = project_docs.read_run({"path": "LONG.md",
                                            "offset": 3_000_000})
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert len(result["content"]) == config.INIT_READ_CHARS
        assert peak < 1_000_000, (
            f"{peak} bytes held to return a page from offset 3,000,000")

    def test_paging_still_reaches_the_same_text(self, project):
        """The behaviour, not the cost. A bound that returned the wrong
        characters would pass both tests above."""
        body = "".join(str(i % 10) for i in range(50_000))
        (project / "LONG.md").write_text(body, encoding="utf-8")

        first = project_docs.read_run({"path": "LONG.md"})
        offset = int(first["message"].split("offset=")[1].split()[0].strip("."))
        second = project_docs.read_run({"path": "LONG.md", "offset": offset})

        assert first["content"] == body[:config.INIT_READ_CHARS]
        assert second["content"] == body[offset:offset + config.INIT_READ_CHARS]
        assert first["content"] + second["content"] == body[:offset +
                                                            len(second["content"])]

    def test_truncated_is_true_while_there_is_more_and_false_at_the_end(
            self, project):
        """`truncated` used to be computed from len(text), which is the read
        that was removed. It is answered now by looking one character past
        the page -- so the two ends of the document have to be checked
        separately, because a bound that always says True would satisfy the
        paging test above."""
        body = "y" * (config.INIT_READ_CHARS + 10)
        (project / "LONG.md").write_text(body, encoding="utf-8")

        first = project_docs.read_run({"path": "LONG.md"})
        last = project_docs.read_run({"path": "LONG.md",
                                      "offset": config.INIT_READ_CHARS})

        assert first["truncated"] is True
        assert last["truncated"] is False
        assert last["content"] == "y" * 10

    def test_a_document_shorter_than_one_page_is_returned_whole(self,
                                                                project):
        (project / "SHORT.md").write_text("# tiny\n", encoding="utf-8")
        result = project_docs.read_run({"path": "SHORT.md"})

        assert result["content"] == "# tiny\n"
        assert result["truncated"] is False
        assert "message" not in result

    def test_an_offset_past_the_end_is_empty_rather_than_an_error(self,
                                                                  project):
        (project / "SHORT.md").write_text("# tiny\n", encoding="utf-8")
        result = project_docs.read_run({"path": "SHORT.md", "offset": 999999})

        assert result["content"] == ""
        assert result["truncated"] is False
