"""
test_tool_failure_lines.py

Batch 266, from the owner's 2026-11-16 report: a failed `fetch_url` was
drawn TWICE in the transcript -- once as the tool's own WARNING, which
`TranscriptLogHandler` routes to the screen, and once as the result line
both shells print for a failed call (`x fetch_url  Could not fetch URL:
...`). Measured on the code as it stood: the same text, line for line,
httpx's "For more information check" included.

What this file holds, by the owner's three answers:

  1. A failure a tool RETURNS as its result logs at INFO: app.log keeps it,
     the screen shows the result line alone. Fifteen sites -- the six the
     first answer named, and the nine a scan of `tools/` found after it.
     The six that logged a traceback (`logger.exception`) keep it in the
     file through `exc_info`.
  2. The CLI's stderr shows WARNING and above, the rule the TUI's
     transcript already had. INFO printed there before, so moving a line to
     INFO alone would have fixed one shell (measured). An explicit
     AGENT_LOG_LEVEL still reaches the terminal.
  3. A guard: no block under `tools/` both logs at WARNING or above and
     returns an error result, and no retry loop does it per attempt.
"""

import ast
import json
import logging
import os
import pathlib
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

import config
from logging_setup import _screen_level, configure_logging

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# ---- Drivers: one per site, each reaching the failure through the tool ----
# ---------------------------------------------------------------------------

def _fetch_url(monkeypatch, tmp_path):
    from tools.builtin import _net_common, fetch_url

    @contextmanager
    def refused(*_args, **_kwargs):
        raise httpx.HTTPError("connection reset")
        yield  # pragma: no cover -- a generator, never reached

    monkeypatch.setattr(_net_common, "stream", refused)
    monkeypatch.setattr(fetch_url, "is_url_permitted", lambda _u: None)
    return fetch_url.run({"url": "https://example.com/paper.pdf"})


def _web_search(monkeypatch, tmp_path):
    from tools.builtin import web_search

    web_search._cache.clear()

    def down(*_a, **_k):
        raise RuntimeError("the provider is down")

    monkeypatch.setattr(web_search, "_call_ddgs", down)
    return web_search.run({"query": "batch 266 search"})


def _arxiv_retries(monkeypatch, tmp_path):
    from tools.builtin import arxiv

    arxiv._cache.clear()

    def no_route(*_a, **_k):
        raise httpx.HTTPError("no route")

    monkeypatch.setattr(arxiv, "_call_arxiv_api", no_route)
    return arxiv.run({"keywords": "batch 266 retries"})


def _arxiv_oversized(monkeypatch, tmp_path):
    from tools.builtin import arxiv

    arxiv._cache.clear()

    def huge(*_a, **_k):
        raise arxiv._FeedTooLarge(arxiv.MAX_FEED_BYTES + 1)

    monkeypatch.setattr(arxiv, "_call_arxiv_api", huge)
    return arxiv.run({"keywords": "batch 266 oversized"})


def _project(monkeypatch, tmp_path):
    from tools.builtin import project_docs

    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("# Widget\n", encoding="utf-8")
    monkeypatch.setattr(project_docs, "_project_root", lambda: str(root))
    return project_docs, root


def _denied(*_a, **_k):
    raise PermissionError(13, "Permission denied")


def _disk_full(*_a, **_k):
    raise OSError(28, "No space left on device")


def _read_project_doc(monkeypatch, tmp_path):
    project_docs, _root = _project(monkeypatch, tmp_path)
    # The module's own name, not builtins: the failure lands on this one
    # open and nothing else the test touches.
    monkeypatch.setattr(project_docs, "open", _denied, raising=False)
    return project_docs.read_run({"path": "README.md"})


def _a_doc_name(project_docs):
    return sorted(project_docs._known_doc_names())[0]


def _write_project_doc_refused(monkeypatch, tmp_path):
    project_docs, _root = _project(monkeypatch, tmp_path)
    monkeypatch.setattr(project_docs, "_resolve_in_project",
                        lambda *_a: None)
    return project_docs.write_run({"name": _a_doc_name(project_docs),
                                   "content": "# Doc\n"})


def _write_project_doc_failed(monkeypatch, tmp_path):
    project_docs, _root = _project(monkeypatch, tmp_path)
    monkeypatch.setattr(project_docs, "write_text_atomic", _disk_full)
    return project_docs.write_run({"name": _a_doc_name(project_docs),
                                   "content": "# Doc\n"})


def _workspace(monkeypatch, tmp_path):
    from tools.builtin import file_ops

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("old text\n", encoding="utf-8")
    (ws / "p.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(ws))
    monkeypatch.setattr(file_ops, "WORKSPACE_ROOT", os.path.realpath(ws))
    return file_ops


def _read(monkeypatch, tmp_path):
    file_ops = _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(file_ops, "open", _denied, raising=False)
    return file_ops.read_run({"path": "a.txt"})


def _write(monkeypatch, tmp_path):
    file_ops = _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(file_ops, "write_text_atomic", _disk_full)
    return file_ops.write_run({"path": "b.txt", "content": "new\n"})


def _edit_read(monkeypatch, tmp_path):
    file_ops = _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(file_ops, "open", _denied, raising=False)
    return file_ops.edit_run({"path": "a.txt", "old_text": "old",
                              "new_text": "new"})


def _edit_write(monkeypatch, tmp_path):
    file_ops = _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(file_ops, "write_text_atomic", _disk_full)
    return file_ops.edit_run({"path": "a.txt", "old_text": "old",
                              "new_text": "new"})


def _view_image(monkeypatch, tmp_path):
    file_ops = _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(file_ops, "open", _denied, raising=False)
    return file_ops.view_image_run(
        {"path": "p.png"}, memory=object(), call_id="call_1",
        parent_run=SimpleNamespace(images=True))


def _shell(monkeypatch, tmp_path):
    from tools.builtin import shell

    monkeypatch.setattr(shell, "uncontained_refusal", lambda *_a: None)
    monkeypatch.setattr(shell, "classifier_refusal", lambda *_a: None)
    monkeypatch.setattr(shell, "is_docker_available", lambda: True)
    monkeypatch.setattr(shell, "fallback_changed_refusal", lambda *_a: None)
    monkeypatch.setattr(shell, "classify_command", lambda *_a, **_k: "x")

    def broken(**_kwargs):
        raise RuntimeError("the runtime vanished")

    monkeypatch.setattr(shell, "run_sandboxed", broken)
    return shell.run({"command": "echo hi", "rationale": "a test"})


def _isolation_unreadable(monkeypatch, tmp_path):
    from tools import isolation

    return isolation._interpret("calculator", "", "Traceback: boom", 1, 5.0)


def _isolation_unstartable(monkeypatch, tmp_path):
    from tools import isolation

    def no_interpreter(*_a, **_k):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(isolation.subprocess, "run", no_interpreter)
    return isolation.run_isolated(json.dumps, {}, 5.0,
                                  tool_name="calculator", confine=False)


def _id(fragment: str) -> str:
    return fragment.replace(" ", "-")


#: (driver, logger name, a fragment of the line, whether it keeps a trace)
SITES = [
    (_fetch_url, "tools.builtin.fetch_url", "fetch_url failed for", False),
    (_web_search, "tools.builtin.web_search", "web_search attempt", False),
    (_arxiv_retries, "tools.builtin.arxiv", "arxiv_search attempt", False),
    (_arxiv_oversized, "tools.builtin.arxiv", "dropped a feed over", False),
    (_read_project_doc, "tools.builtin.project_docs",
     "read_project_doc failed for", False),
    (_write_project_doc_refused, "tools.builtin.project_docs",
     "write_project_doc refused", False),
    (_write_project_doc_failed, "tools.builtin.project_docs",
     "write_project_doc failed for", False),
    (_read, "tools.builtin.file_ops", "read tool failed on", True),
    (_write, "tools.builtin.file_ops", "write tool failed on", True),
    (_edit_read, "tools.builtin.file_ops", "edit tool read failed on", True),
    (_edit_write, "tools.builtin.file_ops", "edit tool write failed on", True),
    (_view_image, "tools.builtin.file_ops", "view_image failed on", True),
    (_shell, "tools.builtin.shell", "shell tool failed", True),
    (_isolation_unreadable, "tools.isolation", "produced no readable result",
     False),
    (_isolation_unstartable, "tools.isolation",
     "Could not start an isolated child", False),
]


# ===========================================================================
# ---- 1. Each site: the result carries it, the log line is INFO -----------
# ===========================================================================

class TestAReturnedFailureLogsAtInfo:

    @pytest.mark.parametrize("drive, name, fragment, traced", SITES,
                             ids=[_id(s[2]) for s in SITES])
    def test_the_line_is_info_and_the_result_is_the_error(
            self, drive, name, fragment, traced, monkeypatch, tmp_path,
            caplog):
        caplog.set_level(logging.INFO)

        result = drive(monkeypatch, tmp_path)

        assert "error" in result, result
        lines = [r for r in caplog.records
                 if r.name == name and fragment in r.getMessage()]
        assert lines, (f"the failure is no longer logged at all, so app.log "
                       f"lost it: {[r.getMessage() for r in caplog.records]}")
        assert {r.levelno for r in lines} == {logging.INFO}, (
            f"{fragment!r} reached the screen's level again, beside the "
            f"result line that already says it")

    @pytest.mark.parametrize("drive, name, fragment, traced", SITES,
                             ids=[_id(s[2]) for s in SITES])
    def test_nothing_from_the_tool_reaches_the_screen(
            self, drive, name, fragment, traced, monkeypatch, tmp_path,
            caplog):
        """Not only the named line: any WARNING the failure path raises
        from `tools.*` would be the same double, said differently."""
        caplog.set_level(logging.INFO)

        drive(monkeypatch, tmp_path)

        loud = [(r.name, r.getMessage()) for r in caplog.records
                if r.name.startswith("tools.") and r.levelno >= logging.WARNING]
        assert not loud, loud

    @pytest.mark.parametrize(
        "drive, name, fragment",
        [(d, n, f) for d, n, f, traced in SITES if traced],
        ids=[_id(f) for _d, _n, f, traced in SITES if traced])
    def test_a_site_that_kept_a_traceback_still_does(
            self, drive, name, fragment, monkeypatch, tmp_path, caplog):
        """The owner's third answer: `logger.exception` became INFO WITH
        `exc_info`, so an unexpected failure is still findable in app.log
        with its stack -- only the screen line went."""
        caplog.set_level(logging.INFO)

        drive(monkeypatch, tmp_path)

        line = next(r for r in caplog.records
                    if r.name == name and fragment in r.getMessage())
        assert line.exc_info and line.exc_info[1] is not None


# ===========================================================================
# ---- 2. The two screens, end to end ---------------------------------------
# ===========================================================================

class TestTheOwnersReport:

    def test_the_tui_transcript_draws_no_line_for_a_failed_fetch(
            self, monkeypatch, tmp_path):
        """The owner's screenshot, through the real handler: the failure
        reaches the transcript as the result line and nothing else."""
        from tui.app import LogRecordMessage, TranscriptLogHandler

        posted = []
        handler = TranscriptLogHandler(
            SimpleNamespace(post_message=posted.append))
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.INFO)
        try:
            result = _fetch_url(monkeypatch, tmp_path)
        finally:
            root.removeHandler(handler)
            root.setLevel(old)

        assert "connection reset" in result["error"]
        assert not [m.text for m in posted
                    if isinstance(m, LogRecordMessage)]

    def test_the_cli_terminal_shows_it_once_and_the_file_keeps_it(
            self, monkeypatch, tmp_path, capsys):
        monkeypatch.delenv("AGENT_LOG_LEVEL", raising=False)
        log = tmp_path / "app.log"
        try:
            configure_logging(log_file=str(log))
            result = _fetch_url(monkeypatch, tmp_path)
            for handler in logging.getLogger().handlers:
                handler.flush()
        finally:
            _detach_all()

        assert "connection reset" in result["error"]
        assert "fetch_url failed" not in capsys.readouterr().err
        text = log.read_text(encoding="utf-8")
        assert "[INFO] tools.builtin.fetch_url: fetch_url failed for" in text


def _detach_all():
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


# ===========================================================================
# ---- 3. The CLI's screen level --------------------------------------------
# ===========================================================================

class TestTheCliScreenLevel:
    """`configure_logging`'s stderr handler. The TUI attaches none."""

    def _emit(self, monkeypatch, tmp_path, capsys, *, level=None, env=None):
        if env is None:
            monkeypatch.delenv("AGENT_LOG_LEVEL", raising=False)
        else:
            monkeypatch.setenv("AGENT_LOG_LEVEL", env)
        log = tmp_path / "app.log"
        try:
            configure_logging(level, log_file=str(log))
            probe = logging.getLogger("batch266.probe")
            probe.debug("a debug line")
            probe.info("an info line")
            probe.warning("a warning line")
            for handler in logging.getLogger().handlers:
                handler.flush()
        finally:
            _detach_all()
        return capsys.readouterr().err, log.read_text(encoding="utf-8")

    def test_by_default_the_terminal_shows_warning_and_up(
            self, monkeypatch, tmp_path, capsys):
        err, file = self._emit(monkeypatch, tmp_path, capsys)

        assert "a warning line" in err
        assert "an info line" not in err
        assert "an info line" in file, "the file's level did not move"

    @pytest.mark.parametrize("env, shown", [
        ("DEBUG", "a debug line"),
        ("INFO", "an info line"),
        # Set but unreadable: `_resolve_level` says it runs at INFO, and
        # the terminal shows what that sentence said.
        ("verbose", "an info line"),
        ("", "an info line"),
    ], ids=["debug", "info", "unreadable", "empty"])
    def test_an_asked_for_level_reaches_the_terminal(
            self, monkeypatch, tmp_path, capsys, env, shown):
        err, _file = self._emit(monkeypatch, tmp_path, capsys, env=env)

        assert shown in err

    def test_a_level_a_caller_passes_reaches_the_terminal(
            self, monkeypatch, tmp_path, capsys):
        err, _file = self._emit(monkeypatch, tmp_path, capsys, level="INFO")

        assert "an info line" in err

    def test_the_rule_itself(self, monkeypatch):
        monkeypatch.delenv("AGENT_LOG_LEVEL", raising=False)
        assert _screen_level(None) == logging.WARNING
        assert _screen_level("DEBUG") == logging.NOTSET
        monkeypatch.setenv("AGENT_LOG_LEVEL", "ERROR")
        assert _screen_level(None) == logging.NOTSET


# ===========================================================================
# ---- 4. The guard ----------------------------------------------------------
# ===========================================================================

_LOUD = {"warning", "error", "exception", "critical"}

#: Blocks that log loudly AND return an error ON PURPOSE, keyed by the file
#: and the start of the line's format string. Each says why.
ALLOWED = {
    ("tools/builtin/arxiv.py", "arxiv_search refused a hostile feed"):
        "the line keeps the exception, which carries a string the FEED "
        "chose; the result deliberately names only its category (batch "
        "116's review), so the two do not say the same thing",
}


def _loud_call(stmt):
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return None
    func = stmt.value.func
    if (isinstance(func, ast.Attribute) and func.attr in _LOUD
            and isinstance(func.value, ast.Name)
            and func.value.id in ("logger", "log", "logging")):
        return stmt.value
    return None


def _returns_error(stmt) -> bool:
    return (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == "error"
                    for k in stmt.value.keys))


def _format_of(call) -> str:
    args = call.args
    if not args:
        return ""
    first = args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):
        return "".join(v.value for v in first.values
                       if isinstance(v, ast.Constant))
    return ""


def _statement_lists(node):
    for field in ("body", "orelse", "finalbody"):
        value = getattr(node, field, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            yield value


def doubled_lines(source: str):
    """(line number, format string) of every loud log line whose failure
    the same code also returns as its result.

    Two shapes, the two batch 266 removed. A statement list holding both a
    loud logger call and `return {"error": ...}` -- fetch_url's, and the
    file tools'. And a retry loop whose `except` logs loudly per attempt
    while the loop's `else` returns the error -- web_search's and
    arxiv_search's, whose last attempt said the same as the result.
    """
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        for body in _statement_lists(node):
            if any(_returns_error(s) for s in body):
                for stmt in body:
                    call = _loud_call(stmt)
                    if call is not None:
                        found.add((stmt.lineno, _format_of(call)))
        if (isinstance(node, (ast.For, ast.While)) and node.orelse
                and any(_returns_error(s) for s in node.orelse)):
            for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(inner, ast.ExceptHandler):
                    for stmt in inner.body:
                        call = _loud_call(stmt)
                        if call is not None:
                            found.add((stmt.lineno, _format_of(call)))
    return found


def _tool_sources():
    return sorted(p for p in (ROOT / "tools").rglob("*.py")
                  if "__pycache__" not in p.parts)


def _allowed(rel: str, fmt: str) -> bool:
    return any(rel == f and fmt.startswith(prefix) for f, prefix in ALLOWED)


class TestNoToolSaysAFailureTwice:

    def test_no_block_logs_loudly_what_it_returns(self):
        offenders = []
        for path in _tool_sources():
            rel = path.relative_to(ROOT).as_posix()
            for lineno, fmt in doubled_lines(path.read_text(encoding="utf-8")):
                if not _allowed(rel, fmt):
                    offenders.append(f"{rel}:{lineno} {fmt!r}")
        assert not offenders, (
            "these log at WARNING or above AND return the failure as the "
            "result, so both shells draw it twice (batch 266). Log at INFO "
            "(exc_info=True keeps a traceback in app.log), or add the site "
            "to ALLOWED with the reason its line says something the result "
            "does not:\n  " + "\n  ".join(sorted(offenders)))

    def test_every_allowance_is_still_needed(self):
        """An allowance whose site moved or went quiet would excuse the
        next line that lands there."""
        seen = set()
        for path in _tool_sources():
            rel = path.relative_to(ROOT).as_posix()
            for _lineno, fmt in doubled_lines(path.read_text(encoding="utf-8")):
                for f, prefix in ALLOWED:
                    if rel == f and fmt.startswith(prefix):
                        seen.add((f, prefix))
        assert seen == set(ALLOWED), set(ALLOWED) - seen

    @pytest.mark.parametrize("source, flagged", [
        # The fetch_url shape, as it stood.
        ("def run():\n"
         "    try:\n"
         "        go()\n"
         "    except E as e:\n"
         "        logger.warning('fetch failed for %s', e)\n"
         "        return {'error': str(e)}\n", True),
        # The retry shape, as web_search's stood.
        ("def run():\n"
         "    for attempt in range(3):\n"
         "        try:\n"
         "            go()\n"
         "            break\n"
         "        except E as e:\n"
         "            logger.warning('attempt failed: %s', e)\n"
         "    else:\n"
         "        return {'error': 'failed after 3 attempts'}\n", True),
        ("def run():\n"
         "    try:\n"
         "        go()\n"
         "    except E as e:\n"
         "        logger.exception('read failed')\n"
         "        return {'error': str(e)}\n", True),
        # What the fix looks like.
        ("def run():\n"
         "    try:\n"
         "        go()\n"
         "    except E as e:\n"
         "        logger.info('fetch failed for %s', e, exc_info=True)\n"
         "        return {'error': str(e)}\n", False),
        # A WARNING about something the result does not say is not a
        # double: arxiv's 'kept N of M' sits in a retry loop's body.
        ("def run():\n"
         "    for attempt in range(3):\n"
         "        try:\n"
         "            rows = go()\n"
         "            logger.warning('kept %d of %d', 1, 2)\n"
         "            break\n"
         "        except E:\n"
         "            pass\n"
         "    else:\n"
         "        return {'error': 'failed'}\n", False),
    ], ids=["same-block", "retry", "exception", "info", "not-a-failure"])
    def test_the_guard_sees_the_shapes(self, source, flagged):
        assert bool(doubled_lines(source)) is flagged
