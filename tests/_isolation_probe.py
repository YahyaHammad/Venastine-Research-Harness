"""
tests/_isolation_probe.py

A handler for tools/isolation.py to run in a child, used by
test_tool_budgets.py to measure that a timed-out call is STOPPED rather
than abandoned.

It lives in its own importable module, and not as a nested function or a
fixture, because that is the shape `run_isolated` requires: the child
resolves a handler by `__module__` and `__name__` in a fresh interpreter,
so a closure or a locally-defined function is exactly what cannot be
reached. The constraint is the mechanism's, not the test's.

The leading underscore keeps pytest from collecting this as a test module
(`test_*.py` is the pattern); it is imported by name.
"""

import os
import time


def tick_forever(params: dict) -> dict:
    """Append to a file every interval, without end.

    A `while True` rather than a long sleep, deliberately: a sleeping
    child is killed by anything, including a mechanism that merely stops
    waiting for it. This one is doing work and leaves evidence of each
    unit, so "did it stop?" is answered by the file rather than by trusting
    the caller's report.
    """
    path = params["path"]
    interval = params.get("interval", 0.05)
    while True:
        with open(path, "a", encoding="utf-8") as f:
            f.write("t")
            f.flush()
            os.fsync(f.fileno())
        time.sleep(interval)


def echo(params: dict) -> dict:
    """The control: a handler that returns promptly and normally."""
    return {"echoed": params.get("value")}
