"""
test_config_update.py

Batch 86. Putting an edited `config.yaml` back after npm has overwritten it.

MEASURED FIRST, then written. `npm pack` + two installs into a scratch
prefix: a version bump replaces `config.yaml` outright -- an edited
`max_tokens: 31337` came back as `16000` and the new version's
`max_iterations` default arrived with it. A same-version `npm install`, even
with `--force`, is a no-op and re-extracts nothing, which is why the
reinstall branch here is described as a RE-EXTRACT (`npm ci`, a cleared
cache) rather than as any `npm install`.

THE LIVE `config.yaml` IS NEVER WRITTEN HERE, the same rule
`test_config_edit.py` states: every test runs against a copy in `tmp_path`
with BOTH `config_schema.CONFIG_PATH` and `config_update.LIVE` redirected at
it, and `test_the_two_modules_agree_about_where_the_file_is` is what notices
a redirect that only moved one of them.
"""

import json
import os
import shutil
import types

import pytest

import config_edit
import config_schema
import config_update

# ---------------------------------------------------------------------------
# ---- A scratch install ----------------------------------------------------
# ---------------------------------------------------------------------------

def _read(path) -> str:
    return open(path, encoding="utf-8", newline="").read()


def _write(path, text) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _set_scalar(path, key, value) -> None:
    """Rewrite one scalar line in place, leaving every other byte alone.

    Line-based rather than through ruamel on purpose: the pristine file in
    these tests has to stay byte-exact apart from the line being changed, or
    `test_only_your_own_lines_differ` would be measuring the helper.
    """
    lines = _read(path).split("\n")
    if "." in key:
        table, leaf = key.split(".", 1)
        start = next(i for i, line in enumerate(lines)
                     if line.startswith(f"{table}:"))
        index = next(i for i, line in enumerate(lines[start + 1:], start + 1)
                     if line.strip().startswith(f"{leaf}:"))
        indent = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
        lines[index] = f"{indent}{leaf}: {value}"
    else:
        index = next(i for i, line in enumerate(lines)
                     if line.startswith(f"{key}:"))
        lines[index] = f"{key}: {value}"
    _write(path, "\n".join(lines))


def _set_any(path, key, value) -> None:
    """Set any value, container included, through the round-trip parser."""
    yaml = config_edit._round_trip()
    with open(path, encoding="utf-8") as handle:
        tree = yaml.load(handle)
    if "." in key:
        table, leaf = key.split(".", 1)
        tree[table][leaf] = value
    else:
        tree[key] = value
    with open(path, "w", encoding="utf-8", newline="") as handle:
        yaml.dump(tree, handle)


def _drop(path, key) -> None:
    """Remove a top-level key from a state file, spanning its block."""
    lines = _read(path).split("\n")
    start = next(i for i, line in enumerate(lines)
                 if line.startswith(f"{key}:"))
    end = start + 1
    while end < len(lines) and (not lines[end] or lines[end][:1] in " \t#"):
        end += 1
    _write(path, "\n".join(lines[:start] + lines[end:]))


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A scratch npm install, with its own state directory.

    `root` holds `config.yaml` and a `package.json` naming the version, the
    way a package directory does; `state` is `~/.config/venastine/
    config-state` moved somewhere a test may write.
    """
    root = tmp_path / "package"
    root.mkdir()
    live = root / "config.yaml"
    shutil.copyfile(config_schema.CONFIG_PATH, live)
    (root / "package.json").write_text(
        json.dumps({"version": "0.2.0"}), encoding="utf-8")

    monkeypatch.setattr(config_schema, "CONFIG_PATH", str(live))
    monkeypatch.setattr(config_update, "LIVE", str(live))
    config_edit.forget_document()

    box = types.SimpleNamespace(
        root=root, live=live, state=tmp_path / "state",
        shipped=tmp_path / "state" / config_update.SHIPPED_FILE,
        yours=tmp_path / "state" / config_update.YOURS_FILE,
        version=tmp_path / "state" / config_update.VERSION_FILE)
    box.run = lambda **kw: config_update.reconcile(
        state=str(box.state), live=str(box.live), **kw)
    box.bump = lambda v: (root / "package.json").write_text(
        json.dumps({"version": v}), encoding="utf-8")
    yield box
    config_edit.forget_document()


def _adopted(box, **kw):
    """Adopt on a fresh install, so a test can start from a known baseline."""
    box.run(first_run=True, **kw)
    return _read(box.live)


# ---------------------------------------------------------------------------
# ---- Where the file is ----------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_two_modules_agree_about_where_the_file_is():
    """`config_update.LIVE` restates `config_schema.CONFIG_PATH`'s
    expression rather than importing it, because importing that module costs
    294ms of pydantic and ruamel on a path that runs at every launch and
    does nothing on almost all of them (measured: 79ms bare, 373ms with the
    import). Two copies of one fact need this."""
    assert config_update.LIVE == config_schema.CONFIG_PATH


def test_the_state_directory_is_named_once():
    assert config_update.state_dir().endswith(config_update.STATE_DIRNAME)
    assert ".config" in config_update.state_dir()


# ---------------------------------------------------------------------------
# ---- The diff -------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestWhatCountsAsYourEdit:
    """`deviations()` is the whole merge in one function: everything it
    misses is a setting the next update throws away."""

    def _docs(self, install, edits):
        shipped = config_schema.read_document(str(install.live))
        yours = config_schema.read_document(str(install.live))
        for key, value in edits.items():
            if "." in key:
                table, leaf = key.split(".", 1)
                yours[table][leaf] = value
            else:
                yours[key] = value
        return yours, shipped

    def test_a_changed_scalar_is_a_deviation(self, install):
        yours, shipped = self._docs(install, {"max_tokens": 31337})
        assert config_update.deviations(yours, shipped) == {
            "max_tokens": 31337}

    def test_a_table_leaf_is_addressed_by_its_dotted_name(self, install):
        """`shell` appears in BOTH tables, so an undotted name could not say
        which one moved -- the same reason `/config`'s catalogue prefixes
        them."""
        yours, shipped = self._docs(install, {"tool_permissions.shell": True})
        assert config_update.deviations(yours, shipped) == {
            "tool_permissions.shell": True}

    def test_a_container_is_one_value(self, install):
        """Not merged entry by entry, and the reason is that a deletion the
        user made and an entry the new version added are the same shape."""
        yours, shipped = self._docs(
            install, {"models_rejecting_sampling_params": ["only-this-one"]})
        found = config_update.deviations(yours, shipped)
        assert found == {"models_rejecting_sampling_params": ["only-this-one"]}

    def test_an_untouched_file_deviates_in_nothing(self, install):
        yours, shipped = self._docs(install, {})
        assert config_update.deviations(yours, shipped) == {}

    def test_with_no_baseline_every_value_is_yours(self, install):
        """The adoption fallback. An install that predates this module has
        no pristine copy on record, so nothing can say which of its values
        were chosen -- and keeping all of them costs one update's worth of
        changed defaults, where guessing costs a setting."""
        yours, _ = self._docs(install, {})
        everything = config_update.deviations(yours, None)
        assert everything["max_tokens"] == yours["max_tokens"]
        assert "tool_permissions.shell" in everything
        assert len(everything) == len(config_update.leaves(yours))


# ---------------------------------------------------------------------------
# ---- The update ------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestAnUpdateKeepsYourSettings:
    """The batch's reason to exist. Measured against npm's real behaviour:
    a version bump replaces `config.yaml` and everything in it."""

    def _updated(self, install, edits, shipped_edits=(), drop=()):
        """Settle at 0.2.0, edit, then arrive at a 0.3.0 pristine file."""
        _adopted(install)
        for key, value in edits.items():
            _set_any(str(install.yours), key, value)
        for key, value in dict(shipped_edits).items():
            _set_scalar(str(install.live), key, value)
        for key in drop:
            _drop(str(install.shipped), key)
        install.bump("0.3.0")
        pristine = _read(install.live)
        return install.run(), pristine

    def test_your_value_survives_and_an_untouched_default_moves(self, install):
        """Both halves in one test, because either alone is the wrong
        design: a merge that keeps everything never delivers a better
        default, and one that keeps nothing is what npm already does."""
        report, _ = self._updated(
            install, {"max_tokens": 31337},
            shipped_edits={"max_iterations": 55})
        assert report.action == "updated"
        after = config_schema.read_document(str(install.live))
        assert after["max_tokens"] == 31337, "the user's value was lost"
        assert after["max_iterations"] == 55, "the new default never arrived"

    def test_a_key_the_new_version_adds_arrives_at_its_default(self, install):
        """No code does this: the merge starts FROM the new file, so a key
        the old baseline never had is simply already there."""
        report, _ = self._updated(install, {"max_tokens": 31337},
                                  drop=["max_pipeline_retries"])
        assert "max_pipeline_retries" in report.added
        after = config_schema.read_document(str(install.live))
        assert after["max_pipeline_retries"] == config_schema.current(
        ).max_pipeline_retries

    def test_every_comment_survives(self, install):
        """Roughly 600 of the 845 lines are the comments that say what each
        value does and which keys are AUTHORITY."""
        _, pristine = self._updated(install, {"max_tokens": 31337})
        assert _read(install.live).count("#") == pristine.count("#")

    def test_only_your_own_lines_differ(self, install):
        _, pristine = self._updated(
            install, {"max_tokens": 31337, "subagent_max_depth": 3})
        changed = [(a, b) for a, b in zip(pristine.split("\n"),
                                          _read(install.live).split("\n"))
                   if a != b]
        assert len(changed) == 2, changed
        assert all("31337" in b or "subagent_max_depth: 3" in b
                   for _, b in changed)

    def test_a_crlf_working_copy_stays_crlf(self, install):
        """`config_edit.read_text` carries the convention out and back. A
        write that normalised it would report a one-line change as 845."""
        _write(install.live, _read(install.live).replace("\n", "\r\n"))
        self._updated(install, {"max_tokens": 31337})
        raw = _read(install.live)
        assert "\r\n" in raw and "\n" not in raw.replace("\r\n", "")

    def test_the_state_is_re_recorded_for_the_next_update(self, install):
        """Without this the merge runs again at every launch, against a
        baseline one version stale."""
        self._updated(install, {"max_tokens": 31337})
        assert _read(install.version) == "0.3.0"
        assert config_schema.read_document(
            str(install.yours))["max_tokens"] == 31337
        assert config_schema.read_document(
            str(install.shipped))["max_tokens"] != 31337, (
            "shipped.yaml must be the PRISTINE file, not the merged one")

    def test_the_record_is_left_where_it_can_be_read_again(self, install):
        self._updated(install, {"max_tokens": 31337})
        record = _read(install.state / config_update.REPORT_FILE)
        assert "max_tokens" in record


class TestWhatTheReportHasToSay:

    def _updated(self, install, edits, **kw):
        return TestAnUpdateKeepsYourSettings()._updated(install, edits, **kw)

    def test_an_authority_key_is_restored_and_named(self, install):
        """Restored like any other key -- the file surviving an update is
        the status quo, and an update must neither silently loosen nor
        silently tighten the posture -- but NAMED, because the harness is
        about to run under it."""
        report, _ = self._updated(install, {"tool_permissions.shell": True})
        assert config_schema.read_document(
            str(install.live))["tool_permissions"]["shell"] is True
        assert ("tool_permissions.shell", True, True) in report.restored
        assert any("AUTHORITY" in line for line in report.lines())

    def test_a_default_that_moved_under_you_is_flagged(self, install):
        """Your value wins, and you are told the shipped one changed too --
        which is the only way to notice that a default you had deliberately
        overridden has caught up with you."""
        report, _ = self._updated(install, {"max_iterations": 40},
                                  shipped_edits={"max_iterations": 55})
        assert config_schema.read_document(
            str(install.live))["max_iterations"] == 40
        assert "max_iterations" in report.overridden
        assert any("changed its default too" in line
                   for line in report.lines())

    def test_a_container_changed_on_both_sides_keeps_yours(self, install):
        report, _ = self._updated(
            install, {"models_rejecting_sampling_params": ["yours-only"]})
        assert config_schema.read_document(str(install.live))[
            "models_rejecting_sampling_params"] == ["yours-only"]
        assert any(name == "models_rejecting_sampling_params"
                   for name, _, _ in report.restored)

    def test_restoring_a_container_keeps_the_text_after_it(self, install):
        """FOUND BY RUNNING IT, and it cost six comment lines. ruamel anchors
        the prose after a block sequence to that sequence's LAST INDEX, not
        to the key in the parent mapping -- so replacing the node deleted the
        `Critic and embedder routing` banner and the AUTHORITY note beneath
        it from the user's file, silently, while every scalar restored
        cleanly. Mutating the node in place does not help; measured."""
        _, pristine = self._updated(
            install, {"models_rejecting_sampling_params": ["yours-only"]})
        after = _read(install.live)
        assert after.count("#") == pristine.count("#")
        assert "# Critic and embedder routing" in after
        assert "AUTHORITY -- names a provider every claim is sent to" in after

    def test_an_update_that_carried_nothing_says_nothing(self, install):
        """Most people never change a setting, and announcing "0 of them
        merged back in" at every launch after every update teaches people to
        ignore the one time it matters."""
        _adopted(install)
        install.bump("0.3.0")
        report = install.run()
        assert report.action == "updated"
        assert report.lines() == []

    def test_nothing_is_printed_when_there_was_nothing_to_do(self, install):
        _adopted(install)
        assert config_update.reconcile(
            state=str(install.state), live=str(install.live)).lines() == []


class TestAValueTheNewVersionWillNotTake:
    """Per-key, not all-or-nothing: one dead key must cost the user that
    key rather than every edit they ever made."""

    def _updated(self, install, edits, **kw):
        return TestAnUpdateKeepsYourSettings()._updated(install, edits, **kw)

    def test_a_key_this_version_no_longer_has_is_dropped(self, install):
        """Unknown keys are refused at startup, so writing one back would
        break the launch this exists to protect."""
        _adopted(install)
        for path in (install.yours, install.shipped):
            _write(path, _read(path) + "\nlegacy_key: 3\n")
        _set_scalar(str(install.yours), "legacy_key", 9)
        _set_scalar(str(install.yours), "max_tokens", 31337)
        install.bump("0.3.0")
        report = install.run()
        assert ("legacy_key", 9, "not a setting in this version") in \
            report.dropped
        after = config_schema.read_document(str(install.live))
        assert "legacy_key" not in after
        assert after["max_tokens"] == 31337, "one dead key cost a live one"

    def test_a_value_this_version_rejects_is_dropped(self, install):
        """`max_iterations: 0` is refused by the schema -- the D24-shaped
        defect that range checks exist to make impossible."""
        report, _ = self._updated(
            install, {"max_iterations": 0, "max_tokens": 31337})
        assert ("max_iterations", 0, "rejected by this version") in \
            report.dropped
        after = config_schema.read_document(str(install.live))
        assert after["max_tokens"] == 31337
        assert after["max_iterations"] == config_schema.current(
        ).max_iterations

    def test_a_read_only_install_still_judges_values_honestly(
            self, install, monkeypatch):
        """A global npm prefix owned by root is an ordinary install, and the
        candidate cannot be written beside `config.yaml` there. Without the
        fallback every value comes back "rejected by this version" -- which
        is a lie about the schema, and it hides the honest failure, the
        write, behind a wall of wrong ones."""
        import tempfile
        real = tempfile.NamedTemporaryFile

        def refuse_the_install_directory(*args, **kwargs):
            if kwargs.get("dir"):
                raise OSError(13, "Permission denied")
            return real(*args, **kwargs)

        monkeypatch.setattr(tempfile, "NamedTemporaryFile",
                            refuse_the_install_directory)
        report, _ = self._updated(install, {"max_tokens": 31337})
        assert not report.dropped
        assert config_schema.read_document(
            str(install.live))["max_tokens"] == 31337

    def test_what_was_written_still_starts_the_harness(self, install):
        """The point of dropping rather than aborting: the file left behind
        has to load, or the merge has broken the launch it was protecting."""
        self._updated(install, {"max_iterations": 0, "max_tokens": 31337})
        assert config_schema.load(path=str(install.live)).max_tokens == 31337


# ---------------------------------------------------------------------------
# ---- Adoption -------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheFirstTimeThisRuns:

    def test_a_fresh_install_records_the_pristine_copy(self, install):
        report = install.run(first_run=True)
        assert report.action == "adopted"
        assert install.shipped.exists() and install.yours.exists()
        assert _read(install.version) == "0.2.0"

    def test_an_install_already_running_records_no_baseline(self, install):
        """The file in front of us may already carry edits, and recording it
        as pristine would make those edits the baseline and lose them at the
        very next update. The launcher knows which case this is from its own
        venv stamp."""
        _set_scalar(str(install.live), "max_tokens", 31337)
        report = install.run(first_run=False)
        assert report.action == "adopted"
        assert install.yours.exists()
        assert not install.shipped.exists()
        assert report.lines(), "the user is not told their file is untracked"

    def test_the_two_way_fallback_keeps_everything(self, install):
        """One update where a changed default does not reach this user, and
        then it self-heals: the merge records a real baseline on its way
        out."""
        _set_scalar(str(install.live), "max_tokens", 31337)
        install.run(first_run=False)
        install.bump("0.3.0")
        install.run()
        assert config_schema.read_document(
            str(install.live))["max_tokens"] == 31337
        assert install.shipped.exists(), "the fallback did not self-heal"

    def test_adoption_does_not_write_the_config(self, install):
        before = _read(install.live)
        install.run(first_run=True)
        assert _read(install.live) == before


# ---------------------------------------------------------------------------
# ---- The ordinary launch --------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheLaunchThatChangesNothing:

    def test_the_config_is_not_written(self, install):
        before = _adopted(install)
        install.run()
        assert _read(install.live) == before

    def test_a_hand_edit_reaches_the_mirror(self, install):
        """Between sessions someone edits the installed file directly --
        which README.md tells them to do for `ensemble_models` and every
        other container. The mirror is how that edit survives the update."""
        _adopted(install)
        _set_scalar(str(install.live), "max_tokens", 31337)
        assert install.run().action == "mirrored"
        assert config_schema.read_document(
            str(install.yours))["max_tokens"] == 31337

    def test_the_mirror_is_not_refreshed_before_the_version_check(
            self, install):
        """THE TRAP THIS WHOLE MODULE CAN FAIL ON. Copy `config.yaml` over
        `yours.yaml` first and the mirror is overwritten with the file npm
        just replaced -- the edits are gone before anything looks at them,
        and every other test here still passes."""
        _adopted(install)
        _set_scalar(str(install.yours), "max_tokens", 31337)
        install.bump("0.3.0")
        install.run()
        assert config_schema.read_document(
            str(install.live))["max_tokens"] == 31337


class TestAReExtractedPackage:
    """`npm ci`, a cleared cache, a deleted `node_modules`. Measured: a
    same-version `npm install` -- `--force` included -- re-extracts nothing,
    so this is not reachable through an ordinary update."""

    def test_the_settings_come_back_without_a_version_change(self, install):
        _adopted(install)
        _set_scalar(str(install.yours), "max_tokens", 31337)
        report = install.run()
        assert report.action == "reinstalled"
        assert config_schema.read_document(
            str(install.live))["max_tokens"] == 31337


# ---------------------------------------------------------------------------
# ---- Nothing here may stop a launch ---------------------------------------
# ---------------------------------------------------------------------------

class TestTheLaunchSurvivesAnything:
    """A configuration-history feature must not be able to stop the harness
    from starting. Every one of these is a report, never an exception."""

    def test_a_checkout_is_left_alone(self, install):
        """git is already managing this file. A contributor running the
        launcher to test it must not have their tracked `config.yaml`
        rewritten from a mirror."""
        (install.root / ".git").mkdir()
        assert install.run(first_run=True).action == "skipped"
        assert not install.state.exists()

    def test_a_yours_file_that_no_longer_parses_is_reported(self, install):
        _adopted(install)
        _write(install.yours, "max_tokens: [unclosed\n")
        install.bump("0.3.0")
        report = install.run()
        assert report.action == "failed"
        assert report.lines(), "a failure with nothing said is a silent one"

    def test_an_unwritable_config_is_reported(self, install, monkeypatch):
        _adopted(install)
        _set_scalar(str(install.yours), "max_tokens", 31337)
        install.bump("0.3.0")

        def refuse(*args, **kwargs):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(config_edit, "write", refuse)
        report = install.run()
        assert report.action == "failed"
        assert config_schema.read_document(
            str(install.live))["max_tokens"] != 31337
        assert _read(install.yours) != _read(install.live), (
            "a failed merge must not overwrite the mirror it still needs")

    def test_a_missing_config_is_not_an_error(self, install):
        os.unlink(install.live)
        assert install.run().action == "skipped"

    def test_main_returns_zero_whatever_happened(self, monkeypatch, capsys):
        monkeypatch.setattr(config_update, "reconcile",
                            lambda **kw: config_update.Report(
                                action="failed", note="anything at all"))
        assert config_update.main([]) == 0
        assert "could not restore" in capsys.readouterr().out

    def test_a_report_line_survives_a_cp1252_console(self, capsys):
        """Patch scripts in this repo have crashed on exactly this, and a
        report names VALUES the user wrote -- a URL, a model name."""
        config_update._say("kept: critic_model = 'café-model → x'")
        assert capsys.readouterr().out.strip()
