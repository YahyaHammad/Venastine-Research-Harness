"""
test_copyright_year.py

ROADMAP_v3 §80 (batch 169): scripts/bump_copyright_year.py, which
copyright-year.yml runs at 23:59 UTC on December 31 and again on January 1.
The owner chose a range (2026-2027) and asked never to have to remember it,
so these pin the two runs agreeing, a second run changing nothing, LICENSE
and NOTICE moving together, and the year never going backwards.
"""

import datetime
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "bump_copyright_year", ROOT / "scripts" / "bump_copyright_year.py")
year = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(year)

UTC = datetime.UTC


def _at(*args, tz=UTC):
    return datetime.datetime(*args, tzinfo=tz)


def _files(tmp_path, license_line="   Copyright 2026 Yahya Hammad",
           notice_line="Copyright 2026 Yahya Hammad", newline="\n",
           notice_newline="\n"):
    (tmp_path / "LICENSE").write_bytes(newline.join([
        "   2. Grant of Copyright License. Subject to the terms",
        "", license_line, "", "   Licensed under the Apache License"])
        .encode() + newline.encode())
    (tmp_path / "NOTICE").write_bytes(notice_newline.join([
        "Venastine Research Harness", notice_line, "",
        "Licensed under the Apache License, Version 2.0."]).encode()
        + notice_newline.encode())
    return tmp_path


class TestTheYear:
    def test_the_run_on_time_writes_next_year(self):
        assert year.target_year(_at(2026, 12, 31, 23, 59)) == 2027

    def test_a_run_delayed_past_midnight_writes_the_same(self):
        assert year.target_year(_at(2027, 1, 1, 0, 40)) == 2027
        assert year.target_year(_at(2027, 1, 1, 6, 7)) == 2027

    def test_any_other_day_is_this_year(self):
        assert year.target_year(_at(2026, 12, 30, 23, 59)) == 2026
        assert year.target_year(_at(2026, 6, 1)) == 2026

    def test_the_day_is_utcs_not_the_machines(self):
        """Cases where the two disagree: 20:00 on Dec 30 in UTC-5 is
        already Dec 31 in UTC, and 08:00 on Dec 31 in UTC+9 is still
        Dec 30. (The first version of this test picked times where local
        and UTC gave the same year, and the mutation pass said so.)"""
        minus5 = datetime.timezone(datetime.timedelta(hours=-5))
        assert year.target_year(_at(2026, 12, 30, 20, 0, tz=minus5)) == 2027
        plus9 = datetime.timezone(datetime.timedelta(hours=9))
        assert year.target_year(_at(2026, 12, 31, 8, 0, tz=plus9)) == 2026


class TestTheRewrite:
    def test_a_single_year_becomes_a_range_in_both_files(self, tmp_path):
        _files(tmp_path)
        old, new = year.bump(tmp_path, _at(2026, 12, 31, 23, 59))
        assert (old, new) == ("Copyright 2026 Yahya Hammad",
                              "Copyright 2026-2027 Yahya Hammad")
        assert "\n   Copyright 2026-2027 Yahya Hammad\n" in \
            (tmp_path / "LICENSE").read_text()
        assert "\nCopyright 2026-2027 Yahya Hammad\n" in \
            (tmp_path / "NOTICE").read_text()

    def test_a_range_keeps_its_first_year(self, tmp_path):
        _files(tmp_path, "   Copyright 2026-2027 Yahya Hammad",
               "Copyright 2026-2027 Yahya Hammad")
        assert year.bump(tmp_path, _at(2028, 1, 1, 6, 7))[1] == \
            "Copyright 2026-2028 Yahya Hammad"

    def test_a_second_run_changes_nothing(self, tmp_path):
        _files(tmp_path)
        year.bump(tmp_path, _at(2026, 12, 31, 23, 59))
        before = [(tmp_path / n).read_bytes() for n in year.FILES]
        old, new = year.bump(tmp_path, _at(2027, 1, 1, 6, 7))
        assert old == new == "Copyright 2026-2027 Yahya Hammad"
        assert [(tmp_path / n).read_bytes() for n in year.FILES] == before

    def test_it_never_moves_a_year_backwards(self, tmp_path):
        _files(tmp_path, "   Copyright 2026-2030 Yahya Hammad",
               "Copyright 2026-2030 Yahya Hammad")
        old, new = year.bump(tmp_path, _at(2027, 1, 1))
        assert old == new
        assert "2026-2030" in (tmp_path / "NOTICE").read_text()

    def test_the_year_already_current_is_left_alone(self, tmp_path):
        _files(tmp_path)
        assert year.bump(tmp_path, _at(2026, 7, 4))[1] == \
            "Copyright 2026 Yahya Hammad"

    def test_each_files_line_endings_survive(self, tmp_path):
        """LICENSE is CRLF in a Windows worktree and NOTICE LF -- the
        mix the first run against the real files failed on, reading the
        CR into LICENSE's holder so the two files 'disagreed'."""
        _files(tmp_path, newline="\r\n", notice_newline="\n")
        year.bump(tmp_path, _at(2026, 12, 31, 23, 59))
        data = (tmp_path / "LICENSE").read_bytes()
        assert b"   Copyright 2026-2027 Yahya Hammad\r\n" in data
        assert b"\n" not in data.replace(b"\r\n", b"")
        notice = (tmp_path / "NOTICE").read_bytes()
        assert b"\nCopyright 2026-2027 Yahya Hammad\n" in notice
        assert b"\r" not in notice

    def test_check_writes_nothing(self, tmp_path):
        _files(tmp_path)
        before = (tmp_path / "LICENSE").read_bytes()
        old, new = year.bump(tmp_path, _at(2026, 12, 31, 23, 59), write=False)
        assert new.endswith("2026-2027 Yahya Hammad")
        assert (tmp_path / "LICENSE").read_bytes() == before

    def test_files_that_disagree_are_refused_not_guessed(self, tmp_path):
        _files(tmp_path, notice_line="Copyright 2025 Yahya Hammad")
        with pytest.raises(year.YearError, match="disagree"):
            year.bump(tmp_path, _at(2026, 12, 31, 23, 59))

    @pytest.mark.parametrize("license_line", [
        "   Copyright Yahya Hammad",
        "   Copyright 2026 Yahya Hammad\n   Copyright 2026 Someone Else",
    ])
    def test_anything_but_one_line_per_file_is_refused(self, tmp_path,
                                                       license_line):
        _files(tmp_path, license_line)
        with pytest.raises(year.YearError, match="copyright lines, not one"):
            year.bump(tmp_path, _at(2026, 12, 31, 23, 59))
        assert "2027" not in (tmp_path / "NOTICE").read_text()

    def test_the_licence_sentences_are_not_notices(self, tmp_path):
        """'2. Grant of Copyright License' and the Apache boilerplate say
        Copyright too; only a line that starts with it and a year counts."""
        _files(tmp_path)
        assert len(list(year.LINE.finditer(
            (tmp_path / "LICENSE").read_text()))) == 1


class TestTheRun:
    def test_it_tells_the_workflow_whether_to_commit(self, tmp_path,
                                                    monkeypatch, capsys):
        _files(tmp_path)
        monkeypatch.setattr(year, "ROOT", tmp_path)
        monkeypatch.setattr(year, "bump", lambda write: (
            "Copyright 2026 Yahya Hammad",
            "Copyright 2026-2027 Yahya Hammad"))
        out = tmp_path / "out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        assert year.main([]) == 0
        assert out.read_text() == "changed=true\n"
        assert "-> Copyright 2026-2027" in capsys.readouterr().out

    def test_a_refusal_exits_non_zero(self, monkeypatch, capsys):
        def refuse(write):
            raise year.YearError("LICENSE has 0 copyright lines, not one")

        monkeypatch.setattr(year, "bump", refuse)
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        assert year.main(["--check"]) == 1
        assert "not one" in capsys.readouterr().err

    def test_the_repositorys_own_files_are_in_a_shape_it_can_bump(self):
        old, new = year.bump(ROOT, _at(2026, 12, 31, 23, 59), write=False)
        assert old.startswith("Copyright 20") and old.endswith("Yahya Hammad")
        assert new.endswith("-2027 Yahya Hammad") or new == old
