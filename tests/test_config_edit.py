"""
test_config_edit.py

Batch 84. The catalogue behind `/config`, the round-trip writer under it,
and the relaunch that applies a write.

THE LIVE `config.yaml` IS NEVER WRITTEN HERE. Every test that writes
redirects `config_schema.CONFIG_PATH` at a copy in tmp_path first, and
`test_the_shipped_file_is_untouched` is what notices a slip: it holds every
scalar in the live file against the model this process bound at import, and
those two disagree only if something in the run wrote the real file.
"""

import os
import shutil

import pytest

import config_edit
import config_schema
import main


def _copy_config(tmp_path, monkeypatch):
    """Point the module at a throwaway copy and hand back its path."""
    target = tmp_path / "config.yaml"
    shutil.copyfile(config_schema.CONFIG_PATH, target)
    monkeypatch.setattr(config_schema, "CONFIG_PATH", str(target))
    return target


def _read(path) -> str:
    return open(path, encoding="utf-8", newline="").read()


class TestTheCatalogueCoversTheFile:
    """What `/config` offers has to be what `config.yaml` holds.

    A name missing here is a value nobody can reach from the shell; a name
    here that the file does not have is a row that fails when chosen.
    """

    def test_every_key_is_addressable_exactly_once(self):
        names = [row.name for row in config_edit.catalogue()]
        assert len(names) == len(set(names)), "a name is offered twice"
        fields = set(config_schema.HarnessConfig.model_fields)
        top = {name for name in names if "." not in name}
        assert top == fields - set(config_edit.TABLES), (
            "the catalogue and the schema disagree about the top-level keys")

    def test_the_two_tool_tables_are_expanded_not_listed(self):
        """The 23 tool names appear in BOTH tables, which is the whole
        reason the rows are prefixed. `shell` alone would be ambiguous in a
        way nothing downstream could resolve."""
        names = {row.name for row in config_edit.catalogue()}
        for table in config_edit.TABLES:
            assert table not in names
        tools = list(config_schema.ToolPermissionsModel.model_fields)
        assert len(tools) == 23
        for tool in tools:
            assert f"tool_permissions.{tool}" in names
            assert f"tool_approvals.{tool}" in names

    def test_the_order_is_the_files_order(self):
        """Not alphabetical. The schema declares fields in the order the
        document writes them, under the same section banners, so browsing
        the panel walks the file rather than interleaving the sandbox with
        the scholar lookup."""
        top = [row.name for row in config_edit.catalogue()
               if "." not in row.name]
        expected = [name for name in config_schema.HarnessConfig.model_fields
                    if name not in config_edit.TABLES]
        assert top == expected

    def test_every_authority_key_has_a_sentence(self):
        """The gate is a modal that says what the key PERMITS, so a key
        without a sentence is a key with no working gate. Both directions:
        a sentence for something that is not an authority key would be a
        warning shown where it does not belong."""
        assert (set(config_edit.AUTHORITY_EFFECT)
                == set(config_schema.HARNESS_AUTHORITY_KEYS))

    def test_the_authority_rows_are_the_authority_keys(self):
        marked = {row.name for row in config_edit.catalogue() if row.authority}
        expected = {key for key in config_schema.HARNESS_AUTHORITY_KEYS
                    if key not in config_edit.TABLES}
        leaves = {name for name in marked if "." in name}
        assert len(leaves) == 46, "both tool tables must be gated whole"
        assert marked - leaves == expected

    def test_every_settings_override_names_something_real(self):
        """The one table in this module that is hand-written, so it is the
        one that can drift. A stale entry makes `/config` report the wrong
        winner, quietly, which is worse than not reporting one."""
        from core.config_loader import (
            _KNOWN_SETTINGS,
            _NESTED_SETTINGS,
        )

        fields = set(config_schema.HarnessConfig.model_fields)
        for field, path in config_edit.settings_overrides().items():
            assert field in fields, f"{field} is not a config.yaml key"
            section, _, key = path.partition(".")
            if key:
                assert section in _NESTED_SETTINGS, path
                assert key in _NESTED_SETTINGS[section], path
            else:
                assert section in _KNOWN_SETTINGS, path

    def test_the_env_overrides_are_marked(self):
        marked = {row.name: row.outranked_by for row in config_edit.catalogue()
                  if row.outranked_by and row.outranked_by.startswith("$")}
        expected = {key.lower(): f"${variable}"
                    for key, variable in config_schema.ENV_OVERRIDES.items()}
        assert marked == expected


class TestTheDescriptionsComeFromTheSchema:
    """Generated, not written down. 133 rows and one function, so a range
    that changes in `config_schema.py` changes what `/config` offers with
    no second copy to update."""

    def test_a_closed_vocabulary_lists_its_words(self):
        assert config_edit.find("shell_approval_mode").values == (
            "always | tiered | never")
        assert config_edit.find("compaction_strategy").values == (
            "rederive | chain")

    def test_a_bool_is_true_or_false(self):
        assert config_edit.find("ensemble_mode").values == "true | false"
        assert config_edit.find("tool_permissions.shell").values == (
            "true | false")

    def test_the_ranges_are_read_off_the_constraints(self):
        assert config_edit.find("max_tokens").values == (
            "whole number, above 0")
        assert config_edit.find("max_injected_memories").values == (
            "whole number, 0 or more")
        assert config_edit.find("compaction_trigger_fraction").values == (
            "number from 0 to 1")

    def test_a_container_says_so_and_is_not_settable(self):
        row = config_edit.find("domain_authority_suffixes")
        assert row.kind == "container"
        assert not row.settable

    def test_the_two_model_pairs_take_the_critic_grammar(self):
        for name in ("critic_model", "embedder_model"):
            row = config_edit.find(name)
            assert row.kind == "pair"
            assert row.settable


class TestTypedTextBecomesAValue:
    def test_a_string_field_takes_its_text_verbatim(self):
        """The reason this is not one `yaml.load` call. The shipped image
        tag has a colon in it, and a parser asked to guess would return a
        mapping for something nobody typed as one."""
        row = config_edit.find("sandbox_docker_image")
        assert config_edit.parse_value(row, "python:3.13-slim") == (
            "python:3.13-slim")
        url = config_edit.find("scholar_api_url")
        assert config_edit.parse_value(url, "https://x.example/works?a=1") == (
            "https://x.example/works?a=1")

    def test_numbers_and_bools_parse_as_yaml(self):
        assert config_edit.parse_value(
            config_edit.find("max_tokens"), "18000") == 18000
        assert config_edit.parse_value(
            config_edit.find("compaction_trigger_fraction"), "0.5") == 0.5
        assert config_edit.parse_value(
            config_edit.find("ensemble_mode"), "true") is True

    def test_quotes_are_how_an_empty_string_is_typed(self):
        """`scholar_mailto` ships as `''`, so putting it back has to be
        expressible. Under the verbatim rule alone, `""` would set the
        two-character string `""` and the key meant to carry an email
        address would carry a pair of quotes."""
        row = config_edit.find("scholar_mailto")
        assert config_edit.parse_value(row, '""') == ""
        assert config_edit.parse_value(row, "''") == ""
        assert config_edit.parse_value(row, '"a@b.org"') == "a@b.org"
        # And a value that merely CONTAINS a quote is left alone.
        assert config_edit.parse_value(row, 'a"b') == 'a"b'

    def test_off_clears_an_optional(self):
        row = config_edit.find("default_effort")
        assert config_edit.parse_value(row, "off") is None
        assert config_edit.parse_value(row, "null") is None
        assert config_edit.parse_value(row, "high") == "high"

    def test_an_empty_value_says_what_the_key_takes(self):
        with pytest.raises(ValueError, match="whole number, above 0"):
            config_edit.parse_value(config_edit.find("max_tokens"), "   ")


class TestTheRoundTripIsLossless:
    """The write path's whole claim. `config.yaml` is 845 lines of which
    most are the comments saying what each value does and which keys are
    AUTHORITY; a writer that reformatted them would make every change
    unreviewable."""

    def test_loading_and_dumping_reproduces_the_file(self):
        text, _ = config_edit.read_text()
        assert config_edit._dump(config_edit.document()) == text

    def test_setting_every_leaf_to_its_own_value_changes_nothing(self):
        """The path-resolution check, over the WHOLE catalogue rather than
        a sample. A leaf written to the wrong place shows up here as a
        diff, including all 46 of the tool booleans whose names collide
        across the two tables."""
        text, _ = config_edit.read_text()
        tree = config_edit.document()
        touched = 0
        for row in config_edit.catalogue():
            if not row.settable or row.kind == "pair":
                continue
            if "." in row.name:
                table, leaf = row.name.split(".", 1)
                tree[table][leaf] = tree[table][leaf]
            else:
                tree[row.name] = tree[row.name]
            touched += 1
        assert touched == 114, f"{touched} settable scalars, expected 114"
        assert config_edit._dump(tree) == text

    def test_a_one_value_change_is_a_one_line_diff(self, tmp_path,
                                                   monkeypatch):
        _copy_config(tmp_path, monkeypatch)
        proposal = config_edit.propose("max_tokens", 18000)
        assert len(proposal.lines) == 1
        number, before, after = proposal.lines[0]
        assert before == "max_tokens: 16000"
        assert after == "max_tokens: 18000"

    def test_the_comments_survive_a_write(self, tmp_path, monkeypatch):
        target = _copy_config(tmp_path, monkeypatch)
        before = _read(target)
        proposal = config_edit.propose("max_tokens", 18000)
        config_edit.write(proposal.text, proposal.newline)
        after = _read(target)
        assert after.count("# AUTHORITY --") == before.count("# AUTHORITY --")
        assert after.count("#") == before.count("#")
        assert len(after.split("\n")) == len(before.split("\n"))
        assert "max_tokens: 18000" in after

    def test_a_crlf_checkout_stays_crlf(self, tmp_path, monkeypatch):
        """`core.autocrlf=true` is how this repo is developed, so a
        worktree copy can be CRLF even where the index is LF. ruamel emits
        LF whatever it read, so a writer that did not put the convention
        back would rewrite all 845 lines to report a one-line change."""
        target = _copy_config(tmp_path, monkeypatch)
        text = _read(target)
        open(target, "w", encoding="utf-8", newline="").write(
            text.replace("\n", "\r\n"))

        proposal = config_edit.propose("max_tokens", 18000)
        assert proposal.newline == "\r\n"
        config_edit.write(proposal.text, proposal.newline)
        written = _read(target)
        assert "\r\n" in written
        assert "\n" not in written.replace("\r\n", "")
        assert len(proposal.lines) == 1


class TestAChangeIsValidatedBeforeItIsWritten:
    def test_a_bad_value_is_refused_naming_the_key(self, tmp_path,
                                                   monkeypatch):
        _copy_config(tmp_path, monkeypatch)
        cases = [
            ("max_tokens", -5, "max_tokens"),
            ("max_iterations", 0, "max_iterations"),
            ("shell_approval_mode", "yolo", "shell_approval_mode"),
            ("compaction_trigger_fraction", float("nan"),
             "compaction_trigger_fraction"),
            ("compaction_trigger_fraction", 5.0,
             "compaction_trigger_fraction"),
            ("tool_permissions.shell", "yes", "tool_permissions.shell"),
            ("model_name", 123, "model_name"),
            ("sandbox_memory_mb", 0, "sandbox_memory_mb"),
        ]
        for name, value, named in cases:
            with pytest.raises(ValueError) as caught:
                config_edit.propose(name, value)
            assert named in str(caught.value), (
                f"the refusal for {name}={value!r} does not name the key")

    def test_nothing_is_written_when_a_value_is_refused(self, tmp_path,
                                                        monkeypatch):
        target = _copy_config(tmp_path, monkeypatch)
        before = _read(target)
        with pytest.raises(ValueError):
            config_edit.propose("max_tokens", -5)
        assert _read(target) == before

    def test_no_temp_file_is_left_behind(self, tmp_path, monkeypatch):
        _copy_config(tmp_path, monkeypatch)
        config_edit.propose("max_tokens", 18000)
        with pytest.raises(ValueError):
            config_edit.propose("max_tokens", -5)
        leftovers = [name for name in os.listdir(tmp_path)
                     if name != "config.yaml"]
        assert leftovers == []

    def test_the_whole_document_is_validated_not_the_one_field(
            self, tmp_path, monkeypatch):
        """A change is judged against the file it lands in. Break another
        key by hand and even a perfectly good `max_tokens` is refused --
        which is what stops `/config` from certifying a document that
        cannot start."""
        target = _copy_config(tmp_path, monkeypatch)
        text = _read(target)
        open(target, "w", encoding="utf-8", newline="").write(
            text.replace("max_iterations: 50", "max_iterations: 0"))
        with pytest.raises(ValueError, match="max_iterations"):
            config_edit.propose("max_tokens", 18000)

    def test_an_unknown_key_is_refused(self):
        with pytest.raises(ValueError, match="not a key in config.yaml"):
            config_edit.propose("no_such_key", 1)

    def test_a_container_is_refused_with_a_pointer(self):
        with pytest.raises(ValueError, match="edited in config.yaml"):
            config_edit.propose("domain_authority_suffixes", {})

    def test_setting_a_value_to_what_it_already_is_changes_no_lines(
            self, tmp_path, monkeypatch):
        _copy_config(tmp_path, monkeypatch)
        assert config_edit.propose("max_tokens", 16000).lines == []


class TestTheFileAndTheSessionAreTwoThings:
    """Batch 85. `/config` wrote against the file and reported against the
    model bound at import, so the second change to a key in one session
    named the value it had at LAUNCH as the one being replaced.

    Nothing written was ever wrong. Only the reporting drifted, and only
    once someone did the thing `/config` exists for.
    """

    def test_a_write_is_visible_to_the_next_catalogue(self, tmp_path,
                                                      monkeypatch):
        _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        proposal = config_edit.propose("max_tokens", 18000)
        config_edit.write(proposal.text, proposal.newline)

        row = config_edit.find("max_tokens")
        assert row.in_file == 18000
        assert row.in_session == 16000
        assert row.pending
        assert "now 18000" in row.summary
        assert "pending restart" in row.summary

    def test_the_second_edit_reports_the_first_as_its_before(self, tmp_path,
                                                             monkeypatch):
        """The defect, stated as a test. Two changes to one key in one
        session: the second is replacing the first, not the launch value."""
        _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        first = config_edit.propose("max_tokens", 18000)
        assert first.before == 16000
        config_edit.write(first.text, first.newline)

        second = config_edit.propose("max_tokens", 20000)
        assert second.before == 18000, (
            "the second write reported the launch value as the one it was "
            "replacing, which is what batch 85 fixed")
        assert second.after == 20000

    def test_changes_accumulate_across_keys(self, tmp_path, monkeypatch):
        """The flow this round came from: several values, one restart."""
        _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        for name, value in (("max_tokens", 18000), ("max_iterations", 60),
                            ("tool_approvals.read", True)):
            proposal = config_edit.propose(name, value)
            config_edit.write(proposal.text, proposal.newline)

        pending = {row.name: (row.in_session, row.in_file)
                   for row in config_edit.pending_changes()}
        assert pending == {
            "max_tokens": (16000, 18000),
            "max_iterations": (50, 60),
            "tool_approvals.read": (False, True)}
        # And the document still validates as a whole, which is what makes
        # one restart at the end safe rather than hopeful.
        config_schema.load(path=str(tmp_path / "config.yaml"))

    def test_a_container_is_never_pending(self):
        """The raw document hands back the list YAML spells where the model
        holds a tuple or a frozenset, so all sixteen differ by construction
        -- and none of them can be written from here anyway. Without this
        every container row read `pending restart` from launch."""
        containers = [row for row in config_edit.catalogue()
                      if row.kind == "container"]
        assert len(containers) == 17
        assert not [row.name for row in containers if row.pending]

    def test_an_environment_override_is_not_pending(self, monkeypatch):
        """A difference explained by a variable is not a change waiting on a
        restart: the variable won when the file was read and will win again
        next time. Built directly rather than through the catalogue, because
        the alternative is re-loading the schema's global cache under a
        changed environment and leaving it there."""
        row = config_edit.KeyRow(
            name="model_name", kind="scalar", values="text",
            in_file="from-the-file", in_session="from-the-variable",
            authority=False, outranked_by="$AGENT_MODEL")

        monkeypatch.setenv("AGENT_MODEL", "from-the-variable")
        assert not row.pending

        monkeypatch.delenv("AGENT_MODEL")
        assert row.pending, (
            "with the variable unset the two were equal at launch, so a "
            "difference now is a write waiting on a restart")

    def test_pending_lists_nothing_on_an_untouched_file(self):
        assert config_edit.pending_changes() == []


class TestTheDocumentIsCached:
    """`catalogue()` reads the file on every keystroke the panel sees, and
    parsing 845 lines costs ~70ms against the 0.7ms everything else in that
    path costs."""

    def _count_reads(self, monkeypatch):
        real = config_edit.read_text
        calls = []

        def counting():
            calls.append(1)
            return real()

        monkeypatch.setattr(config_edit, "read_text", counting)
        return calls

    def test_it_is_parsed_once_for_many_lookups(self, tmp_path, monkeypatch):
        _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        calls = self._count_reads(monkeypatch)
        for _ in range(6):
            config_edit.catalogue()
        assert len(calls) == 1, (
            f"the document was read {len(calls)} times for six catalogue "
            f"builds; the panel builds one per keystroke")

    def test_a_write_invalidates_it(self, tmp_path, monkeypatch):
        """Not left to the stamp: `max_tokens: 16000` and `18000` are the
        same SIZE, so mtime would be carrying the whole comparison and two
        writes inside one filesystem tick would be invisible."""
        _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        assert config_edit.find("max_tokens").in_file == 16000
        proposal = config_edit.propose("max_tokens", 18000)
        config_edit.write(proposal.text, proposal.newline)
        assert config_edit.find("max_tokens").in_file == 18000

    def test_an_edit_from_outside_is_picked_up(self, tmp_path, monkeypatch):
        """The case `write()` cannot see: another window, while the harness
        is up. The replacement is the same length as the original, so this
        is the mtime half of the stamp on its own -- and mtime is set
        explicitly so the assertion does not depend on clock resolution."""
        target = _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        assert config_edit.find("max_tokens").in_file == 16000

        text = _read(target).replace("max_tokens: 16000", "max_tokens: 12345")
        assert len(text) == len(_read(target))
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        stamp = os.stat(target).st_mtime_ns + 1_000_000_000
        os.utime(target, ns=(stamp, stamp))

        assert config_edit.find("max_tokens").in_file == 12345


class TestExplainSaysWhatTheValueIsSubjectTo:
    def test_an_authority_key_says_what_it_permits(self):
        lines = " ".join(config_edit.explain("shell_approval_mode"))
        assert "AUTHORITY" in lines
        assert "without asking" in lines
        assert "needs you" in lines.lower() or "asks first" in lines

    def test_an_environment_override_is_named(self, monkeypatch):
        monkeypatch.delenv("AGENT_MODEL", raising=False)
        assert any("AGENT_MODEL would override" in line
                   for line in config_edit.explain("model_name"))

    def test_a_set_environment_variable_says_it_is_winning(self,
                                                           monkeypatch):
        """The one case where the value in force and the value in the file
        are different things, so `/config` has to say both or mislead."""
        monkeypatch.setenv("AGENT_MODEL", "some-other-model")
        config_schema.load(force=True)
        try:
            lines = " ".join(config_edit.explain("model_name"))
            assert "AGENT_MODEL is set" in lines
            assert "config.yaml says" in lines
        finally:
            monkeypatch.delenv("AGENT_MODEL", raising=False)
            config_schema.load(force=True)

    def test_a_settings_key_that_outranks_is_named(self):
        lines = " ".join(config_edit.explain("compaction_trigger_tokens"))
        assert "settings.json" in lines

    def test_a_container_lists_its_entries_capped(self):
        lines = config_edit.explain("domain_authority_suffixes")
        assert any("and 93 more" in line for line in lines)
        assert any("CONFIG_ARCHITECTURE.md" in line for line in lines)


class TestTheRelaunch:
    """`main.relaunch_argv` is pure so that what a restart consists of can
    be asserted without replacing the test process to find out."""

    def _request(self, **kwargs):
        return config_edit.RestartRequest(key="max_tokens", **kwargs)

    def test_the_thread_on_screen_replaces_any_that_was_passed(self):
        request = self._request(thread_id="new-id")
        assert main.relaunch_argv(request, ["main.py", "--tui"])[2:] == [
            "--tui", "--thread", "new-id"]
        assert main.relaunch_argv(
            request, ["main.py", "--tui", "--thread", "old-id"])[2:] == [
            "--tui", "--thread", "new-id"]

    def test_the_equals_spelling_is_dropped_too(self):
        """argparse takes `--thread=X` as readily as `--thread X`, so a
        relaunch that handled one spelling would pass both threads."""
        request = self._request(thread_id="new-id")
        rest = main.relaunch_argv(
            request, ["main.py", "--tui", "--thread=old-id"])[2:]
        assert rest.count("--thread") == 1
        assert "--thread=old-id" not in rest

    def test_other_arguments_are_carried_verbatim(self):
        request = self._request(thread_id="t")
        rest = main.relaunch_argv(
            request, ["main.py", "--tui", "--effort", "high"])[2:]
        assert rest[:3] == ["--tui", "--effort", "high"]

    def test_the_pair_is_named_only_when_the_launch_pinned_it(self):
        """Unpinned, the remembered choice in tui/preferences.py answers
        at the next launch exactly as it did at this one; writing a flag
        the user never typed would outrank it."""
        bare = main.relaunch_argv(
            self._request(thread_id="t"), ["main.py", "--tui"])
        assert "--model" not in bare and "--provider" not in bare

        pinned = main.relaunch_argv(
            self._request(thread_id="t", provider="ANTHROPIC",
                          model="claude-opus-5"),
            ["main.py", "--tui", "--model", "stale"])
        assert pinned[2:] == ["--tui", "--thread", "t",
                              "--provider", "ANTHROPIC",
                              "--model", "claude-opus-5"]

    def test_a_session_with_no_thread_asks_for_none(self):
        assert "--thread" not in main.relaunch_argv(
            self._request(), ["main.py", "--tui"])

    def test_windows_waits_rather_than_orphaning_the_terminal(self,
                                                              mocker):
        """Windows has no exec: the C runtime emulates it by starting a
        new process and exiting, which gives the replacement a different
        pid. The npm launcher waits on THIS one with inherited stdio, so
        its wait would return and the shell prompt would come back over a
        terminal the replacement is still drawing in."""
        mocker.patch.object(os, "name", "nt")
        call = mocker.patch("subprocess.call", return_value=7)
        execv = mocker.patch.object(os, "execv")
        assert main.replace_process(["py", "main.py"]) == 7
        call.assert_called_once_with(["py", "main.py"])
        execv.assert_not_called()

    def test_posix_replaces_the_process(self, mocker):
        mocker.patch.object(os, "name", "posix")
        execv = mocker.patch.object(os, "execv")
        call = mocker.patch("subprocess.call")
        with pytest.raises(AssertionError):
            main.replace_process(["python", "main.py"])
        execv.assert_called_once_with("python", ["python", "main.py"])
        call.assert_not_called()


def test_the_shipped_file_is_untouched():
    """The guard on every test above. `config_edit.write` writes the real
    `config.yaml` unless the path was redirected, and a test that forgot
    would otherwise change the developer's checkout and still pass.

    Held against the LOADED model rather than against a number typed here:
    the model was bound at import, so a stray write during this session
    makes the two disagree whatever the value was, and no expectation in
    this file goes stale the day a default legitimately changes.
    """
    config_edit.forget_document()
    for row in config_edit.catalogue():
        assert not row.pending, (
            f"config.yaml's {row.name} no longer matches the model this "
            f"process loaded, so something in this run wrote the real "
            f"file: it says {row.in_file!r} against {row.in_session!r}")
