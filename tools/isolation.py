"""
tools/isolation.py

ROADMAP_v2 §31 (H2, H3). A wall clock for tool calls that can actually
stop the work.

WHY A PROCESS AND NOT A THREAD, WHICH IS THE WHOLE DECISION.

#57 was deferred once on the grounds that `signal.alarm` is main-thread
only and the TUI runs tools in a worker, so a thread-based watchdog was
the shape -- and "abandoning a thread mid-sympy is not clean". That is
true, and it is an argument against the thread rather than against the
bound. A thread cannot be cancelled in CPython: there is no interruption
point inside `sympy.binomial` for anything to check, so a watchdog that
times out returns an error while the core stays busy until the process
exits. The harness would then report a timeout that did not happen, which
is the class of mechanism this project keeps deciding against -- the same
reason `with_catalogs` suppresses a catalog rather than showing one whose
tool cannot be called.

A subprocess has the interruption point the thread lacks: the OS. Killing
it reclaims the CPU, which
`test_a_timed_out_call_is_STOPPED_not_merely_abandoned` measures directly
-- by evidence the child leaves on disk, not by os.times(), whose
children_user field is always 0.0 on Windows and would make the
assertion vacuous on the platform it usually runs on. That measurement is
the difference between the two designs rather than a detail of this one.

WHAT IT COSTS. A fresh interpreter plus sympy plus the tool module is
~0.34s measured, against a harness where every step is a multi-second
model call. No warm pool: a pool holds interpreters that a killed call
would have to replace anyway, and the state a pooled worker accumulates
is exactly what a bound is supposed to discard.

MODELLED ON mcp_client.client.call_tool, which had this shape first and
wrote down why: an inner bound that stops the work (there,
`read_timeout_seconds`; here, RLIMIT_CPU) with an outer wall clock as the
backstop "for the case the answer is never coming at all". On Windows
there is no rlimit and no cheap equivalent, so the clock runs alone --
H3 records that asymmetry rather than papering over it.

WHAT CROSSES THE BOUNDARY. Params in as JSON, the result dict out as
JSON, and nothing else. That is not a limitation to work around: it is
the check that a BUDGET_COMPUTE tool really is a pure function of its
params, which `assert_budget_declared` enforces from the other side by
refusing a compute tool that declares an injection.
"""

from __future__ import annotations

import json
import logging
import math
import os
import platform
import subprocess
import sys

import config

logger = logging.getLogger(__name__)

# The child prints one JSON object on this marker line. A marker rather
# than "parse the whole of stdout" because an imported module is entitled
# to write to stdout -- a deprecation notice from a pinned SymPy would
# otherwise be indistinguishable from a malformed result.
MARKER = "@@VENASTINE_RESULT@@"

# Bounded on the CHILD side, where the size is known before anything is
# written, and re-checked here. #55's own rule -- bound the read, do not
# slice afterwards -- applied to the mechanism that fixes #55. The value
# is generous next to what a math tool can actually return: CPython caps
# int->str at 4300 digits, so the realistic ceiling is far below this and
# the check is for the pathological case rather than the ordinary one.
MAX_CHILD_OUTPUT_BYTES = 4_000_000

# How much of the child's stderr reaches the error message. Enough to name
# a real failure, bounded because it is unbounded text from a process that
# just misbehaved.
_STDERR_EXCERPT = 400


def _package_root() -> str:
    """The directory holding the `tools` package.

    Derived from __file__, never from the working directory: dispatch has
    no guarantee about cwd -- the TUI runs from wherever it was launched
    and a subagent inherits nothing -- and a child that cannot import its
    own tool would fail as a transport error on every call.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _child_env() -> dict:
    """A minimal environment for the child.

    A math tool needs no credentials, so it is handed none. This is
    security/sandbox._scrubbed_env's argument applied to the other
    subprocess this project spawns, with one difference: the sandbox
    scrubs a shell's inherited environment, while there is nothing to
    scrub here -- the child's environment is built rather than filtered,
    which is the stronger direction.

    PYTHONPATH is built from the PARENT's resolved sys.path rather than
    from the package root alone. Guessing which variables reconstruct an
    import surface is a losing game -- on Windows the user site-packages
    is derived from APPDATA, a virtualenv needs its own prefix, conda
    needs another -- and getting it wrong presents as ModuleNotFoundError
    for sympy on every math call. Handing the child the paths the parent
    actually resolved is deterministic and gives it exactly the parent's
    imports and no more.

    SYSTEMROOT is not optional on Windows: without it the interpreter
    cannot initialise its socket/crypto machinery and exits before
    reaching any tool.
    """
    root = _package_root()
    path = [root] + [p for p in sys.path
                     if p and p != root and os.path.isdir(p)]
    env = {"PYTHONPATH": os.pathsep.join(path),
           # No .pyc writes from a process that exists for one call, and
           # no first-call penalty for the directory scan.
           "PYTHONDONTWRITEBYTECODE": "1",
           # Deterministic, and it keeps a child's traceback readable.
           "PYTHONIOENCODING": "utf-8"}
    for name in ("SYSTEMROOT", "SystemRoot", "WINDIR", "PATH", "TEMP", "TMP",
                 "LD_LIBRARY_PATH"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _resource_limits():
    """H3's inner layer: a preexec_fn setting RLIMIT_CPU and RLIMIT_AS, or
    None where the platform has no rlimit.

    Deliberately NOT security.sandbox._unix_resource_limits, though it is
    the same three lines and the same idea. That one spends
    SANDBOX_CPU_SECONDS (30), which is ABOVE this budget -- reusing it
    would install a limit that can never fire and read, to anyone
    checking, as though the inner layer were present. The CPU ceiling has
    to be the compute budget or it is decoration.

    The MEMORY ceiling IS shared with the sandbox, because that number is
    about what one contained subprocess of this harness may hold and
    nothing here argues for a different answer.
    """
    if platform.system() == "Windows":
        return None

    import resource

    cpu_seconds = max(1, int(math.ceil(config.TOOL_COMPUTE_TIMEOUT_S)))
    mem_bytes = config.SANDBOX_MEMORY_MB * 1024 * 1024

    def _set_limits():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    return _set_limits


def _interpret(tool_name: str, stdout: str, stderr: str, returncode: int,
               timeout: float) -> dict:
    """Turn what the child said into the dict dispatch expects.

    Every branch returns rather than raises. dispatch's own containment
    would convert an exception into an error dict anyway, but it would log
    a traceback with it -- and an oversized factorial is an ordinary thing
    for a model to ask for, not a bug in this harness.
    """
    if len(stdout) > MAX_CHILD_OUTPUT_BYTES:
        return {"error": f"{tool_name} produced more output than can be "
                         f"returned ({len(stdout)} bytes). Ask for a smaller "
                         f"result."}

    for line in stdout.splitlines():
        if line.startswith(MARKER):
            try:
                return json.loads(line[len(MARKER):])
            except ValueError:
                break

    excerpt = (stderr or "").strip().replace("\n", " ")[-_STDERR_EXCERPT:]
    # logger.warning, not exception: there is no traceback here to keep,
    # and this path is reachable by ordinary input.
    logger.warning("%s produced no readable result (exit %s): %s",
                   tool_name, returncode, excerpt)
    return {"error": f"{tool_name} did not return a usable result "
                     f"(exit {returncode}). {excerpt}".strip()}


def run_isolated(handler, params: dict, timeout: float,
                 tool_name: str = "") -> dict:
    """Run `handler` in a killable child and return its result dict.

    `handler` is resolved by module and qualified name in the child rather
    than pickled: the registry's handlers are plain module-level functions
    and naming them keeps the child's import surface to the one tool.
    Both names come from the registry, never from a model.
    """
    tool_name = tool_name or getattr(handler, "__name__", "tool")
    module = getattr(handler, "__module__", None)
    function = getattr(handler, "__name__", None)
    if not module or not function:
        # A handler that is not a named module-level function cannot be
        # reached from a fresh interpreter. Refuse rather than fall back to
        # an in-process call, which would silently drop the bound this
        # whole module exists to apply.
        return {"error": f"{tool_name} cannot be isolated: its handler is "
                         f"not a named module-level function."}

    request = json.dumps({"module": module, "function": function,
                          "params": params, "max_bytes":
                          MAX_CHILD_OUTPUT_BYTES})

    popen_kwargs = {}
    if platform.system() != "Windows":
        popen_kwargs["preexec_fn"] = _resource_limits()

    try:
        child = subprocess.run(
            [sys.executable, "-m", "tools.isolation"],
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=_package_root(),
            env=_child_env(),
            **popen_kwargs,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed the child and reaped it, which
        # is the property that makes this a bound rather than a report.
        return {"error": f"{tool_name} exceeded its {timeout:g}s limit and "
                         f"was stopped. The inputs given are too large for "
                         f"this operation -- try smaller values."}
    except OSError as e:
        logger.warning("Could not start an isolated child for %s: %s",
                       tool_name, e)
        return {"error": f"{tool_name} could not be run: {e}"}

    return _interpret(tool_name, child.stdout, child.stderr,
                      child.returncode, timeout)


def _child_main() -> int:
    """The child. Reads one request from stdin, prints one marked result.

    Kept deliberately small: everything it does before the handler runs is
    time the wall clock is already counting.
    """
    request = json.loads(sys.stdin.read())
    import importlib

    module = importlib.import_module(request["module"])
    handler = getattr(module, request["function"])
    result = handler(request["params"])

    payload = json.dumps(result, default=str)
    limit = request.get("max_bytes", MAX_CHILD_OUTPUT_BYTES)
    if len(payload) > limit:
        # Refused HERE, where the size is known before anything is
        # written. The parent checks too, but this is the producer and
        # #55's whole point is that the producer is where a bound belongs.
        payload = json.dumps({
            "error": "the result is too large to return "
                     f"({len(payload)} bytes). Ask for a smaller result."})
    sys.stdout.write(MARKER + payload + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(_child_main())
