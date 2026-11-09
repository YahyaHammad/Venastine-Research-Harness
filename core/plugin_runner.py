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
    harness -> runner   {"type": "check"}
    runner -> harness   {"type": "checked", "tools": {name: "" | "why"}}

Settings travel with every call, so `/plugin config` takes effect on the
next one without a restart. A `check` imports every tool's module and asks
whether it defines `run`, calling nothing: the marketplace's CI (slice 4,
PL35) sends it, behind the same walls a call has.

THE PLUGIN'S OUTPUT IS NOT THE PROTOCOL. The protocol is written to a copy
of the original stdout, and descriptor 1 is then pointed at stderr, so a
`print()` in a tool -- or a C library writing to fd 1 -- lands in stderr,
which the harness drains and bounds, instead of corrupting a reply.

SLICE 3 (PL29): `plugin_runner.py serve <spec.json>` runs one of a plugin's
MCP servers instead. The spec is a file, not a line on stdin, because stdin
is the server's from its first byte: MCP over stdio, which the harness's
MCP client speaks. The walls go up as for a tool, then the server's module
runs as `__main__` with `sys.stdin` and `sys.stdout` on private copies of
the pipes -- so a server on any MCP SDK, old or new, speaks on those -- and
descriptors 0 and 1 pointed at the null device and stderr, for the reasons
above. The spec:

    {"protocol": 1, "module": "notes_server", "args": [...],
     "env": {name: value}, "settings": {...}, "plugin_dir", "deps_dir",
     "workspace", "access", "network", "tmp_dir",
     "confine": {"module": path} | null}

The server reads its plugin's settings as JSON in VENASTINE_PLUGIN_SETTINGS,
and the workspace's path, where the grant lets it see one, in
VENASTINE_WORKSPACE.
"""

import importlib
import importlib.util
import json
import os
import platform
import queue
import runpy
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


def _check(init, modules) -> dict:
    """Whether each tool's module imports and defines a callable `run`.
    Importing runs the module's top level, as a first call would."""
    found = {}
    for tool, module_name in sorted(init["tools"].items()):
        try:
            module = modules.get(module_name)
            if module is None:
                module = importlib.import_module(f"tools.{module_name}")
                modules[module_name] = module
        except (Exception, SystemExit) as exc:  # noqa: BLE001 -- the import's error is the answer
            found[tool] = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            continue
        found[tool] = ("" if callable(getattr(module, "run", None)) else
                       f"tools/{module_name}.py defines no run(params, "
                       f"context)")
    return {"type": "checked", "tools": found}


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
        if request.get("type") == "check":
            _write(out, _check(init, modules))
            continue
        if request.get("type") != "call":
            continue
        out.write(_call(request, modules, init) + "\n")
        out.flush()


def serve(spec_path: str) -> int:
    """One of a plugin's MCP servers, from the spec at `spec_path` (PL29).
    A refusal is a line on stderr and an exit, which the harness reads
    back as the reason the server did not connect."""
    try:
        with open(spec_path, encoding="utf-8") as handle:
            spec = json.load(handle)
        if spec.get("protocol") != PROTOCOL or not spec.get("module"):
            raise ValueError("not a serve spec of this protocol")
    except (OSError, ValueError) as exc:
        print(f"venastine: bad serve spec: {exc}", file=sys.stderr, flush=True)
        return 2
    wire_in = os.fdopen(os.dup(0), "r", encoding="utf-8", newline="")
    wire_out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    if spec.get("confine"):
        reason = _confine(spec)
        if reason:
            print(f"venastine: refused to run: {reason}", file=sys.stderr,
                  flush=True)
            return 3
    # After the walls and appended, as for a tool: the standard library
    # wins over a module the plugin ships under the same name.
    sys.path.append(os.path.join(spec["plugin_dir"], "servers"))
    if spec.get("deps_dir"):
        sys.path.append(spec["deps_dir"])
    os.environ.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
    os.environ["VENASTINE_PLUGIN_SETTINGS"] = json.dumps(spec.get("settings") or {})
    os.environ.pop("VENASTINE_WORKSPACE", None)
    if spec.get("workspace") and spec.get("access") in ("read", "write"):
        os.environ["VENASTINE_WORKSPACE"] = spec["workspace"]
        try:
            os.chdir(spec["workspace"])
        except OSError:
            pass  # the harness set the working directory already
    sys.stdin, sys.stdout = wire_in, wire_out
    sys.argv = [spec["module"], *[str(a) for a in spec.get("args") or ()]]
    try:
        runpy.run_module(spec["module"], run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    finally:
        try:
            wire_out.flush()
        except (OSError, ValueError):
            pass  # the harness closed its end; nothing is listening
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "serve":
        sys.exit(serve(sys.argv[2]))
    sys.exit(main())
