"""
core/plugin_runner.py

ROADMAP_v3 §94, slice 2 (PL24). The one program that runs a plugin's code.
The harness starts it once per plugin per session, on that plugin's first
tool call, and talks to it in JSON lines over its stdin and stdout until
the session ends or it has been idle for `idle_exit_s`.

STANDALONE: the standard library and nothing of the harness. In a container
this file is the only part of the harness there is -- mounted read-only at
/venastine/plugin_runner.py, so no plugin can change how the harness talks
to it -- and it is started with `python -I -S -B`, so neither the
environment nor any site-packages decides what it imports, and no bytecode
is written beside the plugin's files, whose hashes PL8 checks. In `process` isolation on
Linux it loads one more file, security/confine.py, by path, to put up the
grant's walls before a line of the plugin runs (PL25).

THE PROTOCOL, one JSON object per line:

    harness -> runner   {"type": "init", "protocol": 1, "tools": {name:
                         module}, "plugin_dir", "deps_dir", "workspace",
                         "access", "network", "tmp_dir", "idle_exit_s",
                         "confine": {"module": path} | null}
    runner -> harness   {"type": "ready", "protocol": 1, "python": "3.13.1"}
                        or {"type": "refused", "reason": "..."}, then exit
    harness -> runner   {"type": "call", "id": 7, "tool": "word-count",
                         "params": {...}, "settings": {...}}
    runner -> harness   {"type": "result", "id": 7, "ok": true,
                         "result": {...}}
                        or {"type": "result", "id": 7, "ok": false,
                         "error": "...", "trace": [...]}

Settings travel with every call, so `/plugin config` takes effect on the
next one without a restart.

THE PLUGIN'S OUTPUT IS NOT THE PROTOCOL. The protocol is written to a copy
of the original stdout, and descriptor 1 is then pointed at stderr, so a
`print()` in a tool -- or a C library writing to fd 1 -- lands in stderr,
which the harness drains and bounds, instead of corrupting a reply.
"""

import importlib
import importlib.util
import json
import os
import platform
import queue
import sys
import threading
import traceback

PROTOCOL = 1
#: A tool's whole result, as JSON. Past this the call fails by name rather
#: than reaching the model cut short: a truncated JSON object is not one.
MAX_RESULT_BYTES = 1_000_000
MAX_ERROR_CHARS = 2000
MAX_TRACE_LINES = 12


class Context:
    """What a tool's `run(params, context)` is handed.

    settings    the plugin's settings, your values or their defaults
    workspace   the workspace's path where the grant lets the tool see it,
                else None
    access      "none", "read" or "write": what this project granted
    network     whether this project granted the network
    plugin_dir  the plugin's own files, read-only
    tmp_dir     a folder the tool may write scratch files to
    """

    def __init__(self, settings, init):
        self.settings = dict(settings)
        self.workspace = init.get("workspace")
        self.access = init.get("access", "none")
        self.network = bool(init.get("network"))
        self.plugin_dir = init["plugin_dir"]
        self.tmp_dir = init.get("tmp_dir")


def _write(out, message) -> None:
    out.write(json.dumps(message) + "\n")
    out.flush()


def _read_lines(stream, lines) -> None:
    for raw in stream:
        lines.put(raw)
    lines.put(None)


def _load_file(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _confine(init) -> str:
    """The grant's walls (PL25), or why they are not up. Empty when up."""
    try:
        confine = _load_file("venastine_confine", init["confine"]["module"])
    except Exception as exc:  # noqa: BLE001 -- reported as the refusal
        return f"could not load the confinement code: {exc}"
    reason = confine.plugin_lock_down(
        plugin_dir=init["plugin_dir"], deps_dir=init.get("deps_dir"),
        workspace=init.get("workspace"), access=init.get("access", "none"),
        network=bool(init.get("network")), tmp_dir=init["tmp_dir"])
    return reason or ""


def _failure(call_id, exc) -> dict:
    trace = traceback.format_exception(type(exc), exc, exc.__traceback__)
    lines = "".join(trace).rstrip().splitlines()[-MAX_TRACE_LINES:]
    return {"type": "result", "id": call_id, "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS],
            "trace": lines}


def _call(request, modules, init):
    """The reply to one call, as a line of JSON text."""
    call_id = request.get("id")
    tool = request.get("tool")
    module_name = init["tools"].get(tool)
    if module_name is None:
        return json.dumps({"type": "result", "id": call_id, "ok": False,
                           "error": f"this plugin has no tool {tool!r}"})
    try:
        module = modules.get(module_name)
        if module is None:
            module = importlib.import_module(f"tools.{module_name}")
            modules[module_name] = module
        run = getattr(module, "run", None)
        if not callable(run):
            raise TypeError(f"tools/{module_name}.py defines no "
                            f"run(params, context)")
        result = run(dict(request.get("params") or {}),
                     Context(request.get("settings") or {}, init))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 -- the tool's error is its result
        return json.dumps(_failure(call_id, exc))
    if not isinstance(result, dict):
        result = {"result": result}
    try:
        text = json.dumps(result)
    except (TypeError, ValueError) as exc:
        return json.dumps({"type": "result", "id": call_id, "ok": False,
                           "error": f"the tool returned something JSON "
                                    f"cannot hold: {exc}"[:MAX_ERROR_CHARS]})
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        return json.dumps({"type": "result", "id": call_id, "ok": False,
                           "error": f"the result is {len(text):,} characters "
                                    f"as JSON; a plugin tool returns at most "
                                    f"{MAX_RESULT_BYTES:,} bytes"})
    return ('{"type": "result", "id": %s, "ok": true, "result": %s}'
            % (json.dumps(call_id), text))


def main() -> int:
    # Both ends of the protocol on private copies, which a child process
    # does not inherit (PEP 446), and descriptors 0 and 1 pointed
    # elsewhere. For 1, so a print cannot corrupt a reply. For 0, because
    # on Windows a child inheriting the pipe this process's reader thread
    # is blocked on HANGS at startup: synchronous I/O on one pipe handle is
    # serialised. Measured: a tool's `subprocess.run([python, "-c", ...])`
    # timed out at 60 s until this line, and returned in a second after it.
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    protocol_in = os.fdopen(os.dup(0), "rb")
    os.dup2(2, 1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    sys.stdout = sys.stderr
    lines = queue.Queue()
    threading.Thread(target=_read_lines, args=(protocol_in, lines),
                     daemon=True).start()
    first = lines.get()
    if first is None:
        return 0
    try:
        init = json.loads(first)
        if init.get("type") != "init" or init.get("protocol") != PROTOCOL:
            raise ValueError("not an init line of this protocol")
    except ValueError as exc:
        _write(out, {"type": "refused", "reason": f"bad init: {exc}"})
        return 2
    if init.get("confine"):
        reason = _confine(init)
        if reason:
            _write(out, {"type": "refused", "reason": reason})
            return 3
    # AFTER the walls, so nothing the plugin ships is on the path while
    # they go up, and APPENDED, so the standard library always wins over a
    # module a plugin ships under the same name.
    sys.path.append(init["plugin_dir"])
    if init.get("deps_dir"):
        sys.path.append(init["deps_dir"])
    if init.get("workspace") and init.get("access") in ("read", "write"):
        try:
            os.chdir(init["workspace"])
        except OSError:
            pass  # the harness set the working directory already
    _write(out, {"type": "ready", "protocol": PROTOCOL,
                 "python": platform.python_version()})
    modules = {}
    idle = float(init.get("idle_exit_s") or 1800)
    while True:
        try:
            raw = lines.get(timeout=idle)
        except queue.Empty:
            return 0
        if raw is None:
            return 0
        try:
            request = json.loads(raw)
        except ValueError:
            continue
        if request.get("type") != "call":
            continue
        out.write(_call(request, modules, init) + "\n")
        out.flush()


if __name__ == "__main__":
    sys.exit(main())
