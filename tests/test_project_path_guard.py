"""
test_project_path_guard.py

TECHNICAL_DEBT 26. The harness's own install tree may not be the PROJECT.

WHAT THIS IS ABOUT, in one sentence: naming the harness root as the
workspace has been refused since batch 37, but with `AGENT_WORKSPACE`
unset `main()` fell back to `os.getcwd()` -- so launching from a clone
made the harness its own project, and the guard's own rule was enforced
when you said it and skipped when you did not.

WHAT THAT COST, measured in batch 96 from an owner report and reproduced
in batch 107 before anything was changed: `describe_project_content`
returned `['AGENTS.md']` and `is_trusted` was False, so the harness asked
to trust its own agent-context file. Downstream of the same line are D17
trust, the `.venastine/` tier, `/init`'s destination and `UserMemory`'s
project scope -- `/init` scaffolding documentation INTO the harness is the
report that reopened RM6 in the first place.

WHAT WOULD MAKE THIS FILE VACUOUS, stated first:

  * TESTING `check_project()` AND STOPPING THERE. The comparison was never
    the hard part. The defect is WHERE THE CALL SITS: above
    `create_db_and_tables`, so a refused launch leaves no database, and
    above `load_project_config`, so the refusal arrives before the trust
    prompt it exists to prevent. Both orderings are asserted at `main()`,
    and a unit test of the comparison cannot see either.
  * HAVING NO CONTROL. A guard that refused every launch would pass the
    first test in this file. Three controls stand against it, and the
    second is the one that matters: cwd IS the harness root, and an
    explicit `AGENT_WORKSPACE` elsewhere still launches -- which pins that
    the rule reads the PROJECT PATH and not the working directory.
  * WRITING THE HARNESS ROOT AS A LITERAL. A `C:\\...` string handed to
    production code asserts nothing on POSIX (batch 102, again in 104 in
    twenty-two tests). Every path here comes from
    `protected_paths.harness_root()` or `tmp_path`.
  * SPELLING THE PATH THE WAY `realpath` ALREADY RETURNS IT. Then the
    `realpath` call could be deleted and every test would stay green.
    `test_an_unnormalised_spelling_of_the_same_directory_is_refused`
    exists for that mutation alone.

WHY NOT `test_cli.py`'s `startup` FIXTURE: its first act is
`monkeypatch.chdir(tmp_path)`. That is exactly the condition under which
this guard cannot fire, and it is why TECHNICAL_DEBT 26's blast-radius
estimate ("about thirty `main.main([])` calls run from the repo root") was
wrong by a factor of eighteen -- fifteen of the sixteen never reach the
guard at all. The fixture below is deliberately the same shape with that
one line removed.
"""
import os

import pytest

from security import protected_paths


@pytest.fixture
def launch(monkeypatch):
    """`main(argv)` with every side effect stubbed, recording what ran.

    See the module docstring for why this is not `test_cli.py`'s `startup`.
    The returned dict's `order` is what makes the two placement assertions
    possible: a stub that is never called cannot appear in it.
    """
    import main

    reached = {"order": [], "project_path": None}

    def _record(name, result=None):
        def _fn(*a, **k):
            reached["order"].append(name)
            return result
        return _fn

    def _load(path, *a, **k):
        reached["order"].append("load_project_config")
        reached["project_path"] = path
        return {}

    monkeypatch.setattr(main, "configure_logging",
                        _record("configure_logging"))
    monkeypatch.setattr(main, "create_db_and_tables", _record("create_db"))
    monkeypatch.setattr(main, "_classify_legacy_threads", _record("sweep"))
    monkeypatch.setattr(main, "load_project_config", _load)
    monkeypatch.setattr(main, "resolve_runtime_defaults",
                        lambda *a, **k: ("ANTHROPIC", "m"))
    monkeypatch.setattr(main, "setup_mcp", _record("setup_mcp", None))
    monkeypatch.setattr(main, "teardown_mcp", _record("teardown_mcp"))
    monkeypatch.setattr(main, "run_chat", _record("run_chat"))
    monkeypatch.setattr(main, "run_research", _record("run_research"))
    monkeypatch.setattr(main, "run_memory_command", _record("memories", 0))
    monkeypatch.setattr(main, "run_secrets_command", _record("secrets", 0))
    monkeypatch.setattr(main, "run_summary_command", _record("summary", 0))
    monkeypatch.setattr(main, "run_init_command", _record("init", 0))
    return reached


@pytest.fixture
def from_the_install_tree(monkeypatch):
    """A default install, launched from the harness's own directory.

    The condition the guard exists for, and the one no other test file
    reproduces: `AGENT_WORKSPACE` unset, so `WORKSPACE_DIR_EXPLICIT` is
    False and `main()` resolves the project from `os.getcwd()`.
    """
    import config

    root = protected_paths.harness_root()
    monkeypatch.chdir(root)
    monkeypatch.setattr(config, "WORKSPACE_DIR", "./workspace")
    monkeypatch.setattr(config, "WORKSPACE_DIR_EXPLICIT", False)
    return root


class TestTheHarnessIsNotItsOwnProject:

    def test_launching_from_the_install_tree_is_refused(
            self, launch, from_the_install_tree, capsys):
        import main

        assert main.main([]) == 2
        assert "run_chat" not in launch["order"]
        assert protected_paths.harness_root() in capsys.readouterr().err

    def test_the_message_says_what_to_do_instead(
            self, launch, from_the_install_tree, capsys):
        """A workspace refusal is always a configuration error, never
        something the model did -- so it is written for the person who has
        to fix it, which is `check_workspace`'s own rule one function up.

        THREE ROUTES SINCE BATCH 111, where there were two and a trap. The
        message used to close by saying `workspace_dir` in `config.yaml`
        "is not enough on its own" -- accurate, and no use whatever to the
        person reading it, who had just written the directory they meant
        into the file the refusal is about. `workspace_is_project` is the
        third route, so the sentence is about what to do rather than about
        what does not work (TECHNICAL_DEBT 28).
        """
        import main

        rc = main.main([])
        err = capsys.readouterr().err

        assert rc == 2
        assert "AGENT_WORKSPACE" in err
        assert "config.yaml" in err, (
            "the file someone stuck on this has already been editing")
        assert "workspace_is_project" in err, (
            "the route that does not need an environment variable is the "
            "one this refusal exists to hand over")
        assert "./workspace" in err, (
            "working on the harness itself has an answer and it is not "
            "obvious; the message is where someone finds it")
        assert "is not enough" not in err, (
            "the old sentence told the reader their fix had failed and "
            "left them nowhere to go; it is a route now")

    @pytest.mark.parametrize("argv", [
        ["--memories"],
        ["--forget", "some-id"],
        ["--summary"],
        ["--init"],
        ["--secrets", "status"],
    ], ids=["memories", "forget", "summary", "init", "secrets"])
    def test_the_early_exit_commands_are_refused_too(
            self, launch, from_the_install_tree, argv):
        """One rule with no exceptions (owner decision, batch 107).

        Four of the five are project-scoped and refusing them is the whole
        point -- `--memories` from here would list the HARNESS's memories,
        and `--init` scaffolding into the install tree is the original
        report. `--secrets` is the one that is not: it manages the
        user-tier store under `~/.config/venastine` and has no project
        dependency at all. It is refused anyway, because a guard with one
        exception is a guard someone has to remember."""
        import main

        assert main.main(argv) == 2
        assert launch["order"] == ["configure_logging"], launch["order"]


class TestWhereTheRefusalSits:
    """The fix is a position, not a comparison. Both of these fail if the
    call is moved down, and neither can be written against
    `check_project()` itself."""

    def test_no_database_is_created(self, launch, from_the_install_tree):
        """Audit #101's argument, applied to the second refusal as well as
        the first: `--help` used to leave a 77 KB six-table SQLite database
        in whatever directory it ran from. A refused launch should leave
        nothing either, and `create_db_and_tables` runs four lines above
        where the project path used to be resolved."""
        import main

        assert main.main([]) == 2
        assert "create_db" not in launch["order"], (
            "the refusal moved below create_db_and_tables, so a refused "
            "launch now leaves a database behind")

    def test_the_trust_prompt_is_never_reached(
            self, launch, from_the_install_tree):
        """The reported symptom lives inside `load_project_config` ->
        `_ensure_workspace_trust`, which asks the user to trust the
        harness's own AGENTS.md. A refusal printed after that prompt would
        be a refusal after the damage."""
        import main

        assert main.main([]) == 2
        assert "load_project_config" not in launch["order"]
        assert launch["project_path"] is None


class TestWhatMustKeepWorking:
    """Without these, a guard that refused every launch passes everything
    above."""

    def test_an_ordinary_directory_still_launches(
            self, launch, monkeypatch, tmp_path):
        import config
        import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "WORKSPACE_DIR", "./workspace")
        monkeypatch.setattr(config, "WORKSPACE_DIR_EXPLICIT", False)

        assert main.main([]) == 0
        assert "run_chat" in launch["order"]

    def test_a_named_workspace_rescues_a_launch_from_the_install_tree(
            self, launch, from_the_install_tree, monkeypatch, tmp_path):
        """THE control that matters. The working directory is the harness
        and the launch still succeeds, because the rule reads the PROJECT
        PATH. A guard written against `os.getcwd()` passes every other test
        in this file and fails this one."""
        import config
        import main

        project = tmp_path / "some-project"
        project.mkdir()
        monkeypatch.setattr(config, "WORKSPACE_DIR", str(project))
        monkeypatch.setattr(config, "WORKSPACE_DIR_EXPLICIT", True)

        assert main.main([]) == 0
        assert launch["project_path"] == os.path.realpath(str(project))

    def test_the_file_alone_rescues_a_launch_from_the_install_tree(
            self, launch, from_the_install_tree, monkeypatch, tmp_path):
        """The same rescue as above with NO environment variable anywhere
        -- the case TECHNICAL_DEBT 28 was about (batch 111).

        Driven through `config_schema` rather than by patching
        `WORKSPACE_DIR_EXPLICIT` to True, because that is the join under
        test: the sibling test above can pass with `workspace_is_project`
        deleted from the codebase entirely. Here the document says it and
        nothing else does, so what arrives at `main()` had to come through
        `derived_values`.
        """
        import config
        import config_schema
        import main

        project = tmp_path / "declared-in-the-file"
        project.mkdir()
        monkeypatch.delenv("AGENT_WORKSPACE", raising=False)

        declared = config_schema.current().model_copy(update={
            "workspace_dir": str(project), "workspace_is_project": True})
        namespace = config_schema.as_module_namespace(declared)
        monkeypatch.setattr(config, "WORKSPACE_DIR",
                            namespace["WORKSPACE_DIR"])
        monkeypatch.setattr(config, "WORKSPACE_DIR_EXPLICIT",
                            namespace["WORKSPACE_DIR_EXPLICIT"])

        assert main.main([]) == 0
        assert launch["project_path"] == os.path.realpath(str(project))

    def test_the_shipped_workspace_is_still_a_project(
            self, launch, from_the_install_tree, monkeypatch):
        """`<harness>/workspace` is the shipped layout and the developer's
        answer to this refusal. `check_workspace` exempts it through
        `WRITABLE_INSIDE_HARNESS`, and equality rather than containment is
        what keeps it working here."""
        import config
        import main

        inside = os.path.join(from_the_install_tree, "workspace")
        monkeypatch.setattr(config, "WORKSPACE_DIR", inside)
        monkeypatch.setattr(config, "WORKSPACE_DIR_EXPLICIT", True)

        assert main.main([]) == 0
        assert launch["project_path"] == os.path.realpath(inside)


class TestTheComparison:
    """`check_project` itself. Unit-level, and deliberately NOT the whole
    story -- see `TestWhereTheRefusalSits`."""

    def test_the_install_tree_is_refused(self):
        assert protected_paths.check_project(
            protected_paths.harness_root()) is not None

    def test_an_unnormalised_spelling_of_the_same_directory_is_refused(self):
        """`_relation` compares strings, so the realpath is what makes the
        comparison about a DIRECTORY rather than about how it was spelled.
        Portable on purpose: `<root>/workspace/..` resolves to the root on
        both platforms, where a case variant only does on Windows."""
        root = protected_paths.harness_root()
        for spelling in (os.path.join(root, "workspace", ".."),
                         root + os.sep,
                         os.path.join(root, ".")):
            assert protected_paths.check_project(spelling) is not None, spelling

    def test_a_subdirectory_is_not_refused(self):
        """Equality, not containment. `check_workspace` refuses a
        subdirectory of the install tree and this does not, and the
        difference is deliberate: that one is a WRITE boundary, this one
        answers 'is the project the harness'."""
        root = protected_paths.harness_root()
        for name in protected_paths.WRITABLE_INSIDE_HARNESS:
            assert protected_paths.check_project(
                os.path.join(root, name)) is None
        assert protected_paths.check_project(
            os.path.join(root, "security")) is None

    def test_a_sibling_sharing_a_prefix_is_not_refused(self):
        """`_relation`'s `+ os.sep`, from the other side: a bare
        `startswith` makes `<root>-evil` look like the root."""
        assert protected_paths.check_project(
            protected_paths.harness_root() + "-evil") is None

    def test_an_ordinary_directory_is_not_refused(self, tmp_path):
        assert protected_paths.check_project(str(tmp_path)) is None

    def test_it_names_the_running_harness_and_not_a_copy(
            self, monkeypatch, tmp_path):
        """`harness_root()` is derived from `__file__`, so a clone of the
        source is a different tree and working in it is allowed -- the
        property `check_workspace` documents and the one that keeps
        development possible."""
        clone = tmp_path / "a-clone-of-the-source"
        clone.mkdir()
        assert protected_paths.check_project(str(clone)) is None

    def test_the_two_guards_agree_about_the_install_tree(self):
        """The disparity this closes, expressed as a property: naming the
        harness root and falling back to it must get the same answer."""
        root = protected_paths.harness_root()
        assert protected_paths.check_workspace(root) is not None
        assert protected_paths.check_project(root) is not None
