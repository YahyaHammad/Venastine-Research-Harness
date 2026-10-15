"""
ROADMAP_v3 §49 (SS8): core/line_pattern.py, the one engine a model-written
monitor pattern ever reaches.

The property that matters is not that patterns match -- any engine does
that -- but that a HOSTILE one cannot stall the harness, cannot print over
the TUI, and cannot quietly be served by a different engine under the same
module name.
"""

import ast
import importlib.metadata
import os
import time

import pytest

from core import line_pattern
from core.line_pattern import PatternError, compile_pattern, validate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestMatching:

    def test_a_line_that_contains_the_pattern_matches(self):
        pattern = compile_pattern("tick [23]")
        assert pattern.search("12:00 tick 2 of 3")
        assert not pattern.search("tick 9")

    def test_the_source_is_kept_for_the_result_the_agent_reads(self):
        assert compile_pattern("FAILED").source == "FAILED"

    def test_non_ascii_text_matches(self):
        assert compile_pattern("café").search("un café noir")


class TestWhatIsRefused:

    @pytest.mark.parametrize("pattern, said", [
        ("(a", "missing )"),
        (r"(\w)\1", "invalid escape"),
        (r"a(?=b)", "invalid perl operator"),
    ])
    def test_a_pattern_re2_will_not_compile_is_refused_with_its_reason(
            self, pattern, said):
        """A backreference and a lookaround are how a regex backtracks, and
        RE2 refuses both at compile time -- so the agent is told why rather
        than the harness finding out at the first match."""
        reason = validate(pattern)
        assert reason is not None and said in reason
        with pytest.raises(PatternError):
            compile_pattern(pattern)

    @pytest.mark.parametrize("pattern", ["", None, 7, ["a"]])
    def test_an_empty_or_non_string_pattern_is_refused(self, pattern):
        """The value arrives from a tool call's params unvalidated."""
        assert validate(pattern) is not None

    def test_a_pattern_past_the_memory_bound_is_refused(self):
        """Measured against the 8 MiB bound: the same alternation repeated
        {50} times compiles, and {1000} times does not."""
        huge = "(" + "a|" * 5000 + "b){1000}"
        assert "too large" in validate(huge)

    def test_a_valid_pattern_validates(self):
        assert validate(r"^ERROR\b.*timeout") is None


class TestAHostilePatternCannotStallTheHarness:

    def test_a_backtracking_pattern_on_a_long_line_stays_fast(self):
        """Under `re` this input takes ~7 s and holds the GIL throughout,
        freezing every thread (measured at 25 characters: 1.9 s). RE2 is
        linear, so the bound is generous and the input is far longer."""
        pattern = compile_pattern("(a+)+$")
        line = "a" * 200_000 + "b"
        started = time.monotonic()
        assert not pattern.search(line)
        assert time.monotonic() - started < 1.0

    def test_a_refused_pattern_writes_nothing_to_stderr(self, capfd):
        """RE2 logs compile errors to the process's stderr through Abseil
        unless told not to, and stderr under Textual paints over the
        screen. capfd reads the file descriptor, which is where C++ writes."""
        assert validate("(a") is not None
        assert capfd.readouterr().err == ""


class TestTheEngineIsTheOneItClaimsToBe:

    def test_a_re2_module_from_another_distribution_is_refused(
            self, monkeypatch):
        """Another package ships a `re2` that falls back to Python's `re` --
        the freeze this module exists to prevent, under a safe-looking
        name."""
        def missing(name):
            raise importlib.metadata.PackageNotFoundError(name)
        monkeypatch.setattr(line_pattern.importlib.metadata, "distribution",
                            missing)
        with pytest.raises(ImportError, match="google-re2"):
            line_pattern._assert_provenance()

    def test_a_re2_module_at_another_path_is_refused(self, monkeypatch):
        monkeypatch.setattr(line_pattern.re2, "__file__",
                            os.path.join(ROOT, "re2", "__init__.py"))
        with pytest.raises(ImportError, match="google-re2"):
            line_pattern._assert_provenance()

    def test_the_installed_engine_passes(self):
        line_pattern._assert_provenance()

    def test_only_this_module_imports_re2(self):
        """One importer, so the three guards cannot be bypassed by a second
        caller that compiles with RE2's defaults."""
        importers = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames
                           if d not in {".git", "tests", "node_modules",
                                        "__pycache__", ".venv", "venv"}]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as f:
                    try:
                        tree = ast.parse(f.read())
                    except SyntaxError:
                        continue
                for node in ast.walk(tree):
                    names = ([a.name for a in node.names]
                             if isinstance(node, ast.Import) else
                             [node.module or ""]
                             if isinstance(node, ast.ImportFrom) else [])
                    if any(n == "re2" or n.startswith("re2.") for n in names):
                        importers.append(os.path.relpath(path, ROOT))
        assert importers == [os.path.join("core", "line_pattern.py")]
