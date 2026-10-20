"""
security/confine.py

ROADMAP_v3 §79 (HD11). The walls a math tool's child puts up around itself in
a shared hosted deployment: after it has imported SymPy, and before it reads
the expression a visitor's model sent.

WHY THE MATH CHILD. SymPy's parser ends in `eval` -- `_math_common.py`'s
docstring is the long account of how far that goes -- and in a shared demo
the visitor connects and prompts their OWN model, so they choose every
expression the six math tools are handed. `_validate_ast` is the lock. This
is what stands behind it if that lock ever opens: a child that cannot read
another visitor's files, write anywhere, open a socket, start a process,
signal or trace one, or keep root. `tools/isolation.py` already ran each
call in its own short-lived child for the CPU bound (H2, H3); this module
is what that child does to itself first, so the boundary already existed
and only its walls are new.

FOUR LAYERS, in the order they go up. Each was measured on the Space before
it was relied on (2026-10-20: Amazon Linux kernel 6.18, x86_64, sessions
running as root with Docker's default capabilities less NET_RAW, Landlock
ABI 7, seccomp filters installable):

  1. LIMITS. No file may grow (RLIMIT_FSIZE 0), few descriptors, no core
     dump, and the process is non-dumpable. CPU and memory are already
     bounded by isolation.py's preexec_fn.
  2. IDENTITY. A child started as root becomes `nobody`, and loses every
     capability with it -- DAC_OVERRIDE above all, which is what lets root
     read a 0700 directory it does not own. Why the session environment
     lives in /opt when the bridge runs as root: `nobody` must be able to
     import SymPy from it (session_env.py).
  3. LANDLOCK. Read-only beneath the interpreter's own import roots and
     nothing else -- no other visitor's session directory, no /tmp, no
     /proc, no write anywhere. ABI 4 adds no TCP; ABI 6 adds no signals or
     abstract sockets outside the child.
  4. SECCOMP. A denylist answered with EPERM, so SymPy sees an ordinary
     OSError and the tool reports it, rather than a killed process. Last,
     because it refuses the calls the layers before it are made of.

A DENYLIST, NOT AN ALLOWLIST, and on purpose. Which syscalls CPython, glibc
and SymPy's lazy imports need is a property of their versions, not of this
code, and an allowlist that misses one presents as a math tool that fails
for no reason the model can act on. Landlock carries the filesystem, so
the denylist only has to carry what Landlock does not: processes,
networking on a kernel older than ABI 4, cross-process access, privilege,
and the kernel's own control surfaces.

ALL OR NOTHING. `lock_down()` returns None only when every layer is in
place; anything else is the reason, and the caller does not run the tool.
A half-walled child that ran anyway would be the "reports a bound that did
not happen" this project keeps refusing (isolation.py says it first).
Linux on x86_64 only; anywhere else every call is refused, because the
syscall numbers below are that architecture's.

THE SELF-TEST is how a deployment knows, rather than hopes. The bridge runs
`python -m security.confine --self-test` on the session interpreter at
startup: the child locks itself down exactly as a math call's child does,
computes an integral, and then tries every escape this module claims to
prevent. Only a clean pass lets the bridge tell its sessions
`VENASTINE_CONFINEMENT=verified`; without it a shared deployment refuses
the six math tools outright (security/hosted.py).
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import subprocess  # nosec B404 - runs this module on the session interpreter, never a shell
import sys
import sysconfig
from typing import Optional

MARKER = "@@VENASTINE_CONFINE@@"

# --- Landlock -----------------------------------------------------------------
# One syscall number on every architecture: they postdate the unified table.
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1
_FS_EXECUTE = 1 << 0
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REFER = 1 << 13            # ABI 2
_FS_TRUNCATE = 1 << 14         # ABI 3
_FS_IOCTL_DEV = 1 << 15        # ABI 5
_NET_BIND_TCP = 1 << 0         # ABI 4
_NET_CONNECT_TCP = 1 << 1      # ABI 4
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0   # ABI 6
_SCOPE_SIGNAL = 1 << 1                 # ABI 6
# What the child may do beneath an import root: read, and map a C
# extension. EXECUTE grants execve, which seccomp refuses regardless.
_READ_ONLY = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR

# Never a read root, whatever sys.path says: a jail with `/` or `/tmp` in it
# is no jail, and /proc is where another process's environment lives.
_NEVER_ROOTS = ("/proc", "/sys", "/dev", "/tmp", "/var/tmp", "/run")  # nosec B108 - places the child may NOT read

# --- prctl and seccomp ----------------------------------------------------------
_PR_SET_DUMPABLE = 4
_PR_SET_SECCOMP = 22
_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_MODE_FILTER = 2
_AUDIT_ARCH_X86_64 = 0xC000003E
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000
_RET_ALLOW = 0x7FFF0000
_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_RET_K = 0x06
_X32_SYSCALL_BIT = 0x40000000

# x86_64 numbers, grouped by what each group would let an escaped child do.
DENIED_SYSCALLS = {
    # Start a process, or become a different program.
    "clone": 56, "fork": 57, "vfork": 58, "execve": 59, "execveat": 322,
    "clone3": 435,
    # Reach the network, or another process through a socket.
    "socket": 41, "connect": 42, "accept": 43, "bind": 49, "listen": 50,
    "socketpair": 53, "accept4": 288,
    # Signal, trace or read another process.
    "kill": 62, "tkill": 200, "tgkill": 234, "rt_sigqueueinfo": 129,
    "rt_tgsigqueueinfo": 297, "pidfd_send_signal": 424, "pidfd_open": 434,
    "pidfd_getfd": 438, "ptrace": 101, "process_vm_readv": 310,
    "process_vm_writev": 311, "kcmp": 312, "perf_event_open": 298,
    # Change who it is, or undo the walls.
    "setuid": 105, "setgid": 106, "setreuid": 113, "setregid": 114,
    "setgroups": 116, "setresuid": 117, "setresgid": 119, "setfsuid": 122,
    "setfsgid": 123, "capset": 126, "prctl": 157, "personality": 135,
    # Rearrange the filesystem it sees.
    "chroot": 161, "pivot_root": 155, "mount": 165, "umount2": 166,
    "unshare": 272, "setns": 308, "open_tree": 428, "move_mount": 429,
    "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433,
    "mount_setattr": 442, "name_to_handle_at": 303, "open_by_handle_at": 304,
    # The kernel's own control surfaces.
    "bpf": 321, "userfaultfd": 323, "io_uring_setup": 425,
    "io_uring_enter": 426, "io_uring_register": 427, "keyctl": 250,
    "add_key": 248, "request_key": 249, "init_module": 175,
    "finit_module": 313, "delete_module": 176, "kexec_load": 246,
    "kexec_file_load": 320, "reboot": 169, "swapon": 167, "swapoff": 168,
    "acct": 163, "quotactl": 179, "syslog": 103, "iopl": 172, "ioperm": 173,
    "sethostname": 170, "setdomainname": 171, "settimeofday": 164,
    "clock_settime": 227, "adjtimex": 159, "clock_adjtime": 305,
    "vhangup": 153,
}

_OPEN_FILES = 64


def supported() -> Optional[str]:
    """None where this module can confine a child, else why it cannot."""
    if not sys.platform.startswith("linux"):
        return f"{sys.platform} has no Landlock or seccomp"
    if platform.machine() != "x86_64":
        return f"the seccomp filter is x86_64's, not {platform.machine()}'s"
    return None


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def _errno_name() -> str:
    code = ctypes.get_errno()
    return errno.errorcode.get(code, str(code))


def landlock_abi() -> int:
    """Landlock's ABI version, or -1 when the kernel offers none."""
    if not sys.platform.startswith("linux"):
        return -1
    abi = _libc().syscall(_LANDLOCK_CREATE_RULESET, None, 0,
                          _LANDLOCK_CREATE_RULESET_VERSION)
    return int(abi) if abi >= 0 else -1


def read_roots() -> list:
    """The directories the child may read beneath: where this interpreter
    imports from, and nothing that holds another process's data."""
    candidates = {sysconfig.get_paths()[key]
                  for key in ("stdlib", "platstdlib", "purelib", "platlib")}
    candidates.update(p for p in sys.path if p)
    candidates.add(sys.base_prefix)
    roots = set()
    for candidate in candidates:
        real = os.path.realpath(candidate)
        if not os.path.isdir(real) or real == "/":
            continue
        if any(real == never or real.startswith(never + "/")
               for never in _NEVER_ROOTS):
            continue
        roots.add(real)
    return sorted(roots)


# --- the layers ---------------------------------------------------------------

def _apply_limits() -> Optional[str]:
    import resource

    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (_OPEN_FILES, _OPEN_FILES))
    except (OSError, ValueError) as exc:
        return f"limits: {exc}"
    if _libc().prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        return f"non-dumpable: {_errno_name()}"
    return None


def _drop_root() -> Optional[str]:
    """Become `nobody` when started as root; nothing to do otherwise."""
    if os.geteuid() != 0:
        return None
    import pwd

    try:
        entry = pwd.getpwnam("nobody")
        os.setgroups([])
        os.setgid(entry.pw_gid)
        os.setuid(entry.pw_uid)
    except (KeyError, OSError) as exc:
        return f"dropping root: {exc}"
    if os.geteuid() == 0 or os.getegid() == 0:
        return "dropping root: still root afterwards"
    return None


def _apply_landlock(roots) -> Optional[str]:
    abi = landlock_abi()
    if abi < 1:
        return "Landlock is not available on this kernel"

    handled_fs = (1 << 13) - 1                       # every ABI 1 right
    if abi >= 2:
        handled_fs |= _FS_REFER
    if abi >= 3:
        handled_fs |= _FS_TRUNCATE
    if abi >= 5:
        handled_fs |= _FS_IOCTL_DEV
    fields = [("handled_access_fs", ctypes.c_uint64)]
    values = [handled_fs]
    if abi >= 4:
        fields.append(("handled_access_net", ctypes.c_uint64))
        values.append(_NET_BIND_TCP | _NET_CONNECT_TCP)
    if abi >= 6:
        fields.append(("scoped", ctypes.c_uint64))
        values.append(_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL)

    class RulesetAttr(ctypes.Structure):
        _fields_ = fields

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64),
                    ("parent_fd", ctypes.c_int32)]

    libc = _libc()
    attr = RulesetAttr(*values)
    ruleset = libc.syscall(_LANDLOCK_CREATE_RULESET, ctypes.byref(attr),
                           ctypes.sizeof(attr), 0)
    if ruleset < 0:
        return f"Landlock ruleset: {_errno_name()}"
    try:
        for root in roots:
            try:
                dirfd = os.open(root, os.O_PATH | os.O_CLOEXEC)
            except OSError as exc:
                return f"Landlock root {root}: {exc.strerror}"
            try:
                rule = PathBeneath(_READ_ONLY & handled_fs, dirfd)
                if libc.syscall(_LANDLOCK_ADD_RULE, ruleset,
                                _LANDLOCK_RULE_PATH_BENEATH,
                                ctypes.byref(rule), 0) != 0:
                    return f"Landlock rule {root}: {_errno_name()}"
            finally:
                os.close(dirfd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return f"no_new_privs: {_errno_name()}"
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset, 0) != 0:
            return f"Landlock restrict: {_errno_name()}"
    finally:
        os.close(ruleset)
    return None


def seccomp_program() -> list:
    """The filter as (code, jt, jf, k) rows: the architecture checked first,
    an x32 number refused, every DENIED_SYSCALLS entry answered EPERM, and
    everything else allowed."""
    denied = sorted(set(DENIED_SYSCALLS.values()))
    count = len(denied)
    rows = [
        (_BPF_LD_W_ABS, 0, 0, 4),                       # seccomp_data.arch
        (_BPF_JEQ_K, 1, 0, _AUDIT_ARCH_X86_64),
        (_BPF_RET_K, 0, 0, _RET_KILL_PROCESS),
        (_BPF_LD_W_ABS, 0, 0, 0),                       # seccomp_data.nr
        (_BPF_JGE_K, count + 1, 0, _X32_SYSCALL_BIT),
    ]
    for index, number in enumerate(denied):
        rows.append((_BPF_JEQ_K, count - index, 0, number))
    rows.append((_BPF_RET_K, 0, 0, _RET_ALLOW))
    rows.append((_BPF_RET_K, 0, 0, _RET_ERRNO | errno.EPERM))
    return rows


def _apply_seccomp() -> Optional[str]:
    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort),
                    ("filter", ctypes.POINTER(Filter))]

    rows = seccomp_program()
    array = (Filter * len(rows))(*[Filter(*row) for row in rows])
    program = Program(len(rows), array)
    libc = _libc()
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        return f"no_new_privs: {_errno_name()}"
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER,
                  ctypes.byref(program), 0, 0) != 0:
        return f"seccomp: {_errno_name()}"
    return None


def lock_down(roots: Optional[list] = None) -> Optional[str]:
    """Put every wall up around THIS process. None when all are up; else
    the reason, and the caller must not run the tool (see the docstring).
    Irreversible: call it in a process that exists for one tool call."""
    reason = supported()
    if reason:
        return reason
    roots = read_roots() if roots is None else roots
    for layer in (_apply_limits, _drop_root, lambda: _apply_landlock(roots),
                  _apply_seccomp):
        reason = layer()
        if reason:
            return reason
    return None


# --- the self-test --------------------------------------------------------------

def _attempt(fn) -> str:
    try:
        fn()
    except OSError as exc:
        return f"refused ({errno.errorcode.get(exc.errno, exc.errno)})"
    except Exception as exc:  # noqa: BLE001 -- reported; the verdict reads it
        return f"failed ({type(exc).__name__})"
    return "ALLOWED"


def _ptrace_parent() -> None:
    libc = _libc()
    libc.ptrace.restype = ctypes.c_long
    if libc.ptrace(16, os.getppid(), None, None) != 0:   # PTRACE_ATTACH
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _escapes(app_root: str) -> dict:
    """Every escape this module claims to prevent, by name."""
    parent = os.getppid()
    planted = os.path.join(app_root, f".confine-probe-{os.getpid()}")
    return {
        "read /etc/hostname": lambda: open("/etc/hostname", "rb").read(1),
        "list /tmp": lambda: os.listdir("/tmp"),  # nosec B108 - asserting it is refused
        "write /tmp": lambda: open(f"/tmp/.confine-probe-{os.getpid()}", "wb"),  # nosec B108 - asserting it is refused
        "write into the app": lambda: open(planted, "wb"),
        "read the parent's environment":
            lambda: open(f"/proc/{parent}/environ", "rb").read(1),
        "open a socket": lambda: __import__("socket").socket().close(),
        "fork": os.fork,
        "start a program": lambda: subprocess.run(["true"], check=False),  # nosec B603 B607 - asserting it is refused
        "signal the parent": lambda: os.kill(parent, 0),
        "trace the parent": _ptrace_parent,
        "become root again": lambda: os.setuid(0),
    }


def self_test() -> dict:
    """In THIS process: load the math tool, lock down, compute, try every
    escape. `ok` only when the walls went up, the math still works and
    every escape was refused."""
    app_root = os.getcwd()
    try:
        from tools.builtin import symbolic_math
    except Exception as exc:  # noqa: BLE001 -- the verdict says why
        return {"ok": False, "reason": f"the math tool would not load: {exc}"}
    started_as_root = os.geteuid() == 0
    reason = lock_down()
    if reason:
        return {"ok": False, "reason": reason}
    answer = symbolic_math.run({"operation": "integrate",
                                "expression": "sin(x)**2"})
    checks = {"math": answer.get("result") or f"failed: {answer.get('error')}"}
    if started_as_root:
        # Landlock and seccomp would still hold, but root's capabilities
        # are a wall this module claims to remove, so it is checked too.
        checks["still root"] = ("ALLOWED" if os.geteuid() == 0
                                else f"refused (uid {os.geteuid()})")
    try:
        __import__("sympy.physics.units")
        checks["a lazy import"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- the verdict says why
        checks["a lazy import"] = f"failed ({type(exc).__name__})"
    for label, fn in _escapes(app_root).items():
        checks[label] = _attempt(fn)
    escaped = [label for label, verdict in checks.items()
               if verdict == "ALLOWED"]
    broken = [label for label in ("math", "a lazy import")
              if str(checks[label]).startswith("failed")]
    ok = not escaped and not broken
    return {"ok": ok, "uid": os.getuid(), "landlock_abi": landlock_abi(),
            "reason": None if ok else
            f"allowed: {escaped}; broken: {broken}", "checks": checks}


def verify(python, root, *, run=subprocess.run, timeout: float = 120) -> dict:
    """Run the self-test in a child on `python`, from `root`. The verdict,
    or `ok: False` with why the child gave none."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        done = run([str(python), "-m", "security.confine", "--self-test"],
                   cwd=str(root), env=env, capture_output=True, text=True,
                   timeout=timeout, check=False)  # nosec B603 - fixed arguments, no shell
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "reason": f"the self-test did not run: "
                                       f"{type(exc).__name__}"}
    for line in (done.stdout or "").splitlines():
        if line.startswith(MARKER):
            try:
                return json.loads(line[len(MARKER):])
            except ValueError:
                break
    tail = (done.stderr or "").strip()[-400:]
    return {"ok": False, "reason": f"the self-test gave no verdict (exit "
                                   f"{done.returncode}): {tail}"}


if __name__ == "__main__":
    if sys.argv[1:] != ["--self-test"]:
        print("usage: python -m security.confine --self-test", file=sys.stderr)
        sys.exit(2)
    verdict = self_test()
    sys.stdout.write(MARKER + json.dumps(verdict) + "\n")
    sys.stdout.flush()
    sys.exit(0)
