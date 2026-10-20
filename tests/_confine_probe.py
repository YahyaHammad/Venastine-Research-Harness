"""
tests/_confine_probe.py

Handlers for tools/isolation.py to run in a CONFINED child, used by
test_confine.py: each tries one escape security/confine.py claims to
prevent and says whether it was refused, so the test asserts on what the
child could actually do rather than on which walls were reported up.

An importable module for _isolation_probe.py's reason: the child resolves a
handler by `__module__` and `__name__` in a fresh interpreter. The leading
underscore keeps pytest from collecting it.
"""

import errno
import os
import socket
import subprocess  # nosec B404 - the probe asserts that starting one is refused
import sys


def _fork() -> None:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)


_ATTEMPTS = {
    "read": lambda p: open(p["path"], "rb").read(),
    "write": lambda p: open(p["path"], "wb").write(b"planted"),
    "list": lambda p: os.listdir(p["path"]),
    "socket": lambda p: socket.socket().close(),
    "fork": lambda p: _fork(),
    "exec": lambda p: subprocess.run([sys.executable, "-c", "pass"],  # nosec B603 - fixed arguments
                                     check=False),
    "read the parent's environment":
        lambda p: open(f"/proc/{os.getppid()}/environ", "rb").read(1),
    "signal the parent": lambda p: os.kill(os.getppid(), 0),
}

ESCAPES = tuple(_ATTEMPTS)


def attempt(params: dict) -> dict:
    """Try the escape named by `what`; `refused` or `allowed`."""
    try:
        _ATTEMPTS[params["what"]](params)
    except OSError as exc:
        return {"outcome": "refused",
                "errno": errno.errorcode.get(exc.errno, str(exc.errno))}
    return {"outcome": "allowed"}


def compute(params: dict) -> dict:
    """Math that still has to work behind the walls, including a SymPy
    submodule imported for the first time after they went up."""
    import sympy

    x = sympy.Symbol("x")
    answer = sympy.integrate(sympy.exp(-x ** 2), (x, -sympy.oo, sympy.oo))
    import sympy.physics.units as units

    return {"integral": str(answer), "unit": str(units.meter)}
