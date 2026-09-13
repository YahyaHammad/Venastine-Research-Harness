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
import tempfile

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
        assert top == fields, (
            "the catalogue and the schema disagree about the top-level keys")

    def test_the_two_tool_tables_are_expanded_AND_listed(self):
        """The 23 tool names appear in `tool_permissions`; all but `shell`
        appear in `tool_approvals`, which is the whole reason the rows are
        prefixed. `shell` alone would be ambiguous in a way nothing
        downstream could resolve -- and `shell`'s approval lives solely in
        `shell_approval_mode`, so it has no approvals leaf at all.

        Batch 87 gave the tables a row of their own as well. Expanding them
        and emitting nothing for the table meant `/config tool_permissions`
        answered "is not a key in config.yaml" -- false three times over:
        it is a key, a top-level block in the file, and one of the nine
        AUTHORITY keys."""
        rows = {row.name: row for row in config_edit.catalogue()}
        for table in config_edit.TABLES:
            assert table in rows, f"{table} cannot be asked about"
            assert rows[table].kind == "container"
            assert not rows[table].settable, "a whole table is not settable"
            assert rows[table].authority
        names = set(rows)
        tools = list(config_schema.ToolPermissionsModel.model_fields)
        assert len(tools) == 23
        for tool in tools:
            assert f"tool_permissions.{tool}" in names
            if tool == "shell":
                assert f"tool_approvals.{tool}" not in names
            else:
                assert f"tool_approvals.{tool}" in names

    def test_the_order_is_the_files_order(self):
        """Not alphabetical. The schema declares fields in the order the
        document writes them, under the same section banners, so browsing
        the panel walks the file rather than interleaving the sandbox with
        the scholar lookup."""
        top = [row.name for row in config_edit.catalogue()
               if "." not in row.name]
        expected = list(config_schema.HarnessConfig.model_fields)
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
        expected = set(config_schema.HARNESS_AUTHORITY_KEYS)
        leaves = {name for name in marked if "." in name}
        assert len(leaves) == 45, (
            "tool_permissions holds all 23 tools and tool_approvals holds "
            "all but shell, whose approval lives solely in "
            "shell_approval_mode")
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
        marked = {row.name: row.environment_variable
                  for row in config_edit.catalogue()
                  if row.environment_variable}
        expected = {key.lower(): variable
                    for key, variable in config_schema.ENV_OVERRIDES.items()}
        assert marked == expected

    def test_a_key_with_two_masters_names_both(self):
        """Batch 87. `model_name` loses to `$AGENT_MODEL` AND to
        `settings.json default_model`, and an `if/elif` reported only the
        first -- so the tier that arrives with a directory you cloned was
        the one never mentioned."""
        row = config_edit.find("model_name")
        assert row.environment_variable == "AGENT_MODEL"
        assert row.settings_key == "default_model"
        assert "$AGENT_MODEL" in row.outranked_by
        assert "settings.json default_model" in row.outranked_by


class TestTheDescriptionsComeFromTheSchema:
    """Generated, not written down. 132 rows and one function, so a range
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
        diff, including all 45 of the tool booleans whose names collide
        across the two tables (`tool_approvals` deliberately has no
        `shell`)."""
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
        assert touched == 113, f"{touched} settable scalars, expected 113"
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


class TestTheWriteKeepsTheProseAroundIt:
    """Batch 87. Rule 2 says the write preserves everything it did not
    change, and for the two pair keys it did not."""

    def test_setting_and_clearing_a_pair_keeps_every_comment(
            self, tmp_path, monkeypatch):
        """MEASURED, and it deleted an AUTHORITY warning. `/config
        critic_model OPENAI gpt-4o` then `/config critic_model off` removed
        `embedder_model`'s three-line `# AUTHORITY -- names a provider that
        receives claim text` block from the user's file. ruamel keeps the
        text following a scalar in the parent's comment slot 2 and moves it
        to slot 3 when the value becomes a block; going back it writes a
        scalar and slot 3 is never emitted."""
        target = _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        before = _read(target)

        pair = config_edit.propose(
            "critic_model", {"provider_name": "OPENAI", "model": "gpt-4o"})
        config_edit.write(pair.text, pair.newline)
        assert _read(target).count("#") == before.count("#")

        back = config_edit.propose("critic_model", None)
        config_edit.write(back.text, back.newline)
        after = _read(target)
        assert after.count("#") == before.count("#")
        assert "# AUTHORITY -- names a provider that receives claim text" in \
            after
        assert after == before, "the round trip is not byte-exact"

    def test_a_container_keeps_the_section_that_follows_it(
            self, tmp_path, monkeypatch):
        """The other half, from batch 86: prose after a block SEQUENCE is
        anchored to the sequence's last index, not to the parent."""
        target = _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        before = _read(target)
        tree = config_edit.document(fresh=True)
        config_edit.place(tree, "models_rejecting_sampling_params", ["only"])
        after = config_edit._dump(tree)
        assert after.count("#") == before.count("#")
        assert "# Critic and embedder routing" in after


class TestWhatChangedIsReportedHonestly:

    def test_a_line_count_change_reports_only_what_moved(
            self, tmp_path, monkeypatch):
        """`zip(old, new)` mis-pairs every line after an edit that grows the
        file: measured at 707 of 847 lines for this one change, and it
        truncated at the shorter document so a removal under-reported."""
        _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        grown = config_edit.propose(
            "critic_model", {"provider_name": "OPENAI", "model": "gpt-4o"})
        assert len(grown.lines) == 3, grown.lines

    def test_a_one_line_change_is_one_line(self, tmp_path, monkeypatch):
        _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        one = config_edit.propose("max_tokens", 18000)
        assert [(n, a, b) for n, a, b in one.lines] == [
            (49, "max_tokens: 16000", "max_tokens: 18000")]


class TestTheTablesCanBeAskedAbout:
    """`/config tool_permissions` used to answer "is not a key in
    config.yaml" -- false three times over."""

    def test_a_table_is_found_and_explained(self):
        row = config_edit.find("tool_permissions")
        assert row is not None
        assert "23 tools" in row.values
        lines = " ".join(config_edit.explain("tool_permissions"))
        assert "not a key" not in lines
        assert "AUTHORITY" in lines

    def test_a_table_is_not_settable_from_here(self):
        assert not config_edit.find("tool_approvals").settable


class TestWhatOutranksAKeyIsReportedWhole:

    def test_a_settings_only_key_names_its_path(self):
        """`main.py:resolve_review` states `research.subagent_review >
        config.SUBAGENT_REVIEW`, and the table was missing the entry."""
        row = config_edit.find("subagent_review")
        assert row.settings_key == "research.subagent_review"
        assert any("settings.json" in line
                   for line in config_edit.explain("subagent_review"))

    def test_one_vocabulary_for_the_words_that_mean_nothing(self):
        """`tui/app._config_set` carried its own copy WITH `auto` while this
        one was without, so one word cleared one key and was refused on
        another."""
        assert "auto" in config_edit.NULL_WORDS
        for word in ("null", "none", "off", "clear"):
            assert word in config_edit.NULL_WORDS


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


class TestTheGrammarReadsTypesNotPhrasing:
    """Batch 88. `parse_value` recovered two facts by reading the DISPLAY
    string back -- `values.startswith("text")` and `"null" in values` -- so
    a rendering detail decided how a field was parsed."""

    def test_the_facts_come_off_the_annotation(self):
        text_field = config_edit.find("sandbox_docker_image")
        assert text_field.takes_text and not text_field.nullable
        plain_text = config_edit.find("scholar_mailto")
        assert plain_text.takes_text and not plain_text.nullable
        nullable_text = config_edit.find("default_effort")
        assert nullable_text.takes_text and nullable_text.nullable
        number = config_edit.find("max_tokens")
        assert not number.takes_text and not number.nullable

    def test_a_display_string_starting_with_text_does_not_make_it_text(self):
        """The trap the old branch was one schema change away from: a field
        whose rendered `values` happens to begin with `text` matched
        `values.startswith("text")` and silently stopped being parsed as
        YAML, while the panel went on offering it a closed vocabulary.

        The input has to be something the two branches DISAGREE about, or
        the test passes under both: `5` is the string "5" taken verbatim and
        the integer 5 parsed."""
        row = config_edit.KeyRow(
            name="retries", kind="scalar", values="text | 5",
            in_file=5, in_session=5, authority=False)
        assert config_edit.parse_value(row, "5") == 5

    def test_a_display_string_mentioning_null_does_not_make_it_nullable(self):
        row = config_edit.KeyRow(
            name="mode", kind="scalar", values="text, or null-ish",
            in_file="a", in_session="a", authority=False)
        assert config_edit.parse_value(row, "off") == "off"

    def test_a_null_word_clears_only_a_nullable_field(self):
        assert config_edit.parse_value(
            config_edit.find("default_effort"), "off") is None
        image = config_edit.find("sandbox_docker_image")
        assert config_edit.parse_value(image, "off") == "off"


class TestAKeyIsFoundHoweverItIsTyped:
    """Batch 88. `config.py` publishes these names as `MAX_TOKENS` and the
    docs quote both spellings; two rows differing only in case cannot exist,
    because the schema's field names are the source of them."""

    def test_an_uppercase_name_resolves(self):
        assert config_edit.find("MAX_TOKENS").name == "max_tokens"
        assert config_edit.find("Tool_Permissions.Shell").name == (
            "tool_permissions.shell")

    def test_the_panel_completes_the_same_rows(self):
        assert ([row.name for row in config_edit.matching("MAX_")]
                == [row.name for row in config_edit.matching("max_")])


class TestAnUnwritableInstallIsAnOrdinaryInstall:
    """A global npm prefix owned by root. `/config` used to take the app
    down there rather than say the file could not be written."""

    def test_validation_falls_back_when_the_install_is_read_only(
            self, tmp_path, monkeypatch):
        _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        real = tempfile.mkstemp

        def refuse_the_install_directory(*args, **kwargs):
            if kwargs.get("dir"):
                raise OSError(13, "Permission denied")
            return real(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", refuse_the_install_directory)
        assert config_edit.propose("max_tokens", 18000).after == 18000

    def test_a_failed_write_leaves_no_rubble(self, tmp_path, monkeypatch):
        """The old fixed `config.yaml.tmp` survived a failed os.replace --
        on Windows, opening the temp succeeds where replacing a read-only
        target does not -- and sat in the install tree forever."""
        target = _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            config_edit.write("max_tokens: 1\n")
        leftovers = [p for p in os.listdir(tmp_path)
                     if p != os.path.basename(str(target))]
        assert leftovers == [], leftovers


def _boom(*args, **kwargs):
    raise OSError(13, "Permission denied")


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
        assert len(containers) == 19, (
            "17 value containers plus the two permission tables, which "
            "batch 87 gave rows of their own")
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
            authority=False, environment_variable="AGENT_MODEL")

        monkeypatch.setenv("AGENT_MODEL", "from-the-variable")
        assert not row.pending

        monkeypatch.delenv("AGENT_MODEL")
        assert row.pending, (
            "with the variable unset the two were equal at launch, so a "
            "difference now is a write waiting on a restart")

    def test_pending_lists_nothing_on_an_untouched_file(self):
        assert config_edit.pending_changes() == []


class TestTheLoaderCacheDoesNotLeakBetweenTests:
    """Batch 87. These two run in definition order, and the second is what
    the `restore_config_schema_cache` fixture in conftest exists for.

    The symptom, measured: `pytest tests/test_config_loader.py
    tests/test_config_edit.py` failed two of batch 85's tests, because a
    loader test re-read the live document with `APP_DB_PATH` set and
    monkeypatch then removed the variable without touching the cache. The
    file said `app.db`, the session said `from-the-shell.db`, nothing was
    overriding either, so `db_path` read as a pending restart. The full
    suite passed only because collection is alphabetical.
    """

    def test_one_leaves_an_environment_override_in_the_cache(self,
                                                             monkeypatch):
        monkeypatch.setenv("APP_DB_PATH", "from-the-shell.db")
        assert config_schema.load(force=True).db_path == "from-the-shell.db"

    def test_two_still_sees_the_document_this_process_started_on(self):
        assert config_schema.current().db_path != "from-the-shell.db"
        assert not [row for row in config_edit.pending_changes()
                    if row.name == "db_path"]


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


class TestRememberedPairIsSurfaced:
    """The `/model` memory and the `/config model_name` row are two tiers:
    the remembered whole-pair from ui_preferences.json and the harness
    default from config.yaml. The panel used to show only the file value
    as `now`, so a session running a remembered model read as running the
    file's -- while the remembered tier wins short of a --provider/--model
    flag."""

    pair = ("OPENROUTER", "nex-agi/nex-n2.5-pro:free")

    def test_the_panel_names_the_remembered_pair(self):
        row = config_edit.find("model_name", self.pair)
        assert (f"remembered {self.pair[0]} | {self.pair[1]}"
                in row.summary)
        assert "now " in row.summary  # the file value stays first

    def test_other_rows_carry_no_remembered_pair(self):
        row = config_edit.find("max_tokens", self.pair)
        assert row.remembered is None
        assert "remembered" not in row.summary

    def test_nothing_is_shown_when_nothing_is_remembered(self):
        row = config_edit.find("model_name")
        assert row.remembered is None
        assert "remembered" not in row.summary

    def test_explain_names_the_running_pair_when_it_is_in_force(self):
        lines = " ".join(config_edit.explain(
            "model_name", remembered=self.pair, session_pair=self.pair))
        assert f"running {self.pair[0]} | {self.pair[1]}" in lines
        assert "remembered with /model" in lines
        assert "still wins at the next launch" in lines

    def test_explain_says_when_a_remembered_pair_is_dormant(self):
        lines = " ".join(config_edit.explain(
            "model_name", remembered=self.pair,
            session_pair=("ANTHROPIC", "claude-sonnet-5")))
        assert "stored, but this session is not running it" in lines

    def test_explain_says_flags_outrank_a_remembered_pair(self):
        lines = " ".join(config_edit.explain(
            "model_name", remembered=self.pair, cli_pinned=True,
            session_pair=self.pair))
        assert "stored, but this session is not running it" in lines

    def test_explain_names_a_flag_pinned_session(self):
        lines = " ".join(config_edit.explain(
            "model_name", cli_pinned=True,
            session_pair=("OPENAI", "gpt-5.1")))
        assert "pinned with --provider/--model" in lines
        assert "running OPENAI | gpt-5.1" in lines

    def test_an_environment_claim_yields_to_an_active_remembered_pair(
            self, monkeypatch):
        monkeypatch.setenv("AGENT_MODEL", "from-the-variable")
        lines = " ".join(config_edit.explain(
            "model_name", remembered=self.pair, session_pair=self.pair))
        assert "outranks it this session" in lines
        assert "what this session is using" not in lines

    def test_an_environment_claim_stands_without_one(self, monkeypatch):
        monkeypatch.setenv("AGENT_MODEL", "from-the-variable")
        lines = " ".join(config_edit.explain("model_name"))
        assert "what this session is using" in lines

    def test_a_pending_write_names_the_running_pair(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_MODEL", raising=False)
        _copy_config(tmp_path, monkeypatch)
        config_edit.forget_document()
        proposal = config_edit.propose("model_name", "other-model")
        config_edit.write(proposal.text, proposal.newline)
        lines = " ".join(config_edit.explain(
            "model_name", session_pair=self.pair))
        assert ("still running OPENROUTER | "
                "nex-agi/nex-n2.5-pro:free" in lines)


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
