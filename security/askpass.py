"""
security/askpass.py

ROADMAP_v3 §49 slice 5a (SS63, SS64). How a stored secret reaches `ssh`
without ever being in `ssh`'s environment, its argv, or a file.

THE PROPERTY THIS PRESERVES (SS63)
----------------------------------
Slice 4's SS47 read as "BatchMode=yes", but `BatchMode` was never the
property -- it was that **`ssh` can never prompt on a terminal this process
does not own**. `BatchMode=yes` guaranteed that by refusing to prompt at
all, which also meant an encrypted key or a password host simply could not
be used. `SSH_ASKPASS_REQUIRE=force` guarantees the same thing the other
way round: every prompt goes to a helper that answers immediately and
cannot block.

Measured, on this machine, against a real sshd (batch 103):

  * with `BatchMode=no` and NO askpass, Windows OpenSSH 9.5p2 HUNG -- 20 s
    and still waiting, trying to prompt on a terminal this process does not
    own. That is SS47's measurement reproduced, and it is what this module
    exists to prevent;
  * with `SSH_ASKPASS_REQUIRE=force` and a helper, the same call returned
    in 0.8 s, rc=0. Both binaries on this box agree;
  * the Git-for-Windows build did NOT hang under the same conditions
    (3.1 s, refused). So the hang is a property of the BUILD, which is
    exactly why the guarantee may not rest on "ssh will give up".

**When no secret is stored for the host, `BatchMode=yes` is kept exactly as
slice 4 shipped it** and none of this module runs. A user on key-and-agent
auth sees an argv that has not moved by a single option.

WHY A SOCKET AND NOT AN ENVIRONMENT VARIABLE (SS64)
---------------------------------------------------
`ssh` passes its whole environment to the askpass child, so the obvious
implementation puts the password in a variable and the helper echoes it.
That environment is readable by any process running as this user --
`/proc/<pid>/environ` on Linux, the PEB on Windows -- which is precisely
the property that got an OS keystore rejected for the store itself.

So the environment carries a single-use TOKEN and a port. The helper
forwards `(token, prompt)` and is handed one answer.

**What that buys, stated precisely rather than oversold:** an attacker who
can read `ssh`'s environment gets a token, and must then be present during
the connection window and win a race against the helper. An attacker who
can do that could have read the password from the environment at the moment
it mattered anyway. This is a real narrowing of the window -- from "at rest
for the life of the process" to "during one connection" -- and it is not a
boundary. The helper holds no secret and no policy, which is what makes it
safe to leave on disk.

THE CORRECTION THE PROBE FORCED
-------------------------------
This was designed as a ONE-SHOT listener: one connection, one secret, then
closed. Measured, that is wrong. A WRONG password produced **two** askpass
invocations despite `NumberOfPasswordPrompts=1`, because
`keyboard-interactive` and `password` are separate authentication methods
and each gets its own prompt budget. A stacked host
(`AuthenticationMethods publickey,password`) legitimately makes two asks
too -- the key passphrase and then the account password. So the listener
serves a BOUNDED NUMBER of prompts within one connection window, and the
bound is what stops a runaway rather than the count of one.

THE PROMPT STRINGS ARE MEASURED, NOT GUESSED
--------------------------------------------
The harness decides which secret answers which prompt, so these are policy
input. All three were captured from a real `ssh` (batch 103):

    Enter passphrase for key 'C:\\Users\\...\\19552cfb-c47c-4e1a-b':
    venprobe@127.0.0.1's password:
    (venprobe@127.0.0.1) Password:

**The key prompt's path is TRUNCATED mid-string** -- OpenSSH cuts the
prompt to a fixed width -- so matching it against the identity file's path
would fail on any path longer than about sixty characters, which is most of
them. The prefix is what is dependable. The two password forms are the two
authentication methods: `password` produces the first, `keyboard-interactive`
the second, and a server offering both serves keyboard-interactive.

An unrecognised prompt is REFUSED and locks the store. An askpass that
answers a question it did not understand is the wrong secret sent to the
wrong prompt, and the one question `ssh` might ask that is not either of
these is a trust decision -- which SS49 already answers by pinning the host
key, and which must never be answered by a program.
"""

import hmac
import logging
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time

from security import secrets

logger = logging.getLogger(__name__)

#: Logical names the caller supplies answers under.
KEY_PASSPHRASE = "key_passphrase"
LOGIN = "login"

#: Measured (batch 103). PREFIX only: OpenSSH truncates this prompt, so the
#: path in it is not the identity file's path and cannot be matched on.
_KEY_PROMPT_PREFIX = "Enter passphrase for key "
#: Measured: the `password` method and the `keyboard-interactive` method.
_PASSWORD_SUFFIXES = ("'s password: ", ") Password: ")

#: How many prompts one connection window may legitimately produce. A
#: stacked host asks twice; a wrong password asks twice; the cap is above
#: both and low enough that a loop is refused rather than served.
MAX_SERVES = 6
#: How long the listener stays open. Bounded by `ConnectTimeout` and the
#: session in practice; this is the backstop for a connection that neither
#: succeeds nor fails.
DEADLINE_S = 120.0
#: How long the helper may take between connecting and asking.
_CLIENT_TIMEOUT_S = 10.0
#: How often the accept loop wakes to check whether it has been stopped.
_ACCEPT_POLL_S = 0.5

#: The helper, as source. It imports nothing but the stdlib, holds no secret
#: and makes no decision -- it is a pipe with a password on it, which is what
#: makes it safe to write to a temp directory.
_HELPER_SOURCE = '''\
import os
import socket
import sys

token = os.environ.get("VEN_ASKPASS_TOKEN", "")
port = int(os.environ.get("VEN_ASKPASS_PORT", "0") or 0)
prompt = sys.argv[1] if len(sys.argv) > 1 else ""
if not token or not port:
    sys.exit(1)
try:
    conn = socket.create_connection(("127.0.0.1", port), 10)
except OSError:
    sys.exit(1)
try:
    conn.sendall((token + "\\n" + prompt + "\\n\\n").encode("utf-8"))
    buf = b""
    while not buf.endswith(b"\\n") and len(buf) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
finally:
    conn.close()
if buf.strip() == b"":
    sys.exit(1)
sys.stdout.write(buf.decode("utf-8", "replace"))
'''


def classify(prompt: str):
    """Which stored secret answers *prompt*, or None to refuse.

    The one place a prompt string becomes a decision. Returns a logical
    name rather than a value, so this function can be tested against the
    measured strings without a store existing at all.
    """
    if prompt.startswith(_KEY_PROMPT_PREFIX):
        return KEY_PASSPHRASE
    for suffix in _PASSWORD_SUFFIXES:
        if prompt.endswith(suffix):
            return LOGIN
    return None


class _Listener:
    """One connection window's worth of answering."""

    def __init__(self, answers: dict) -> None:
        #: {logical name -> Secret}. Absent means "refuse that prompt",
        #: which is how a host with a passphrase but no password still gets
        #: a refusal rather than an empty string for the password ask.
        self._answers = answers
        self._token = os.urandom(32).hex()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        # HERE, not at the top of `_run`. The thread may not reach its
        # first statement before `close()` runs -- a `serving` block can be
        # that short -- and on Windows calling anything on a closed socket
        # raises WinError 10038, which as an unhandled thread exception
        # prints a traceback to stderr. Setting it while this object is
        # still the only thing holding the socket removes the race rather
        # than catching it.
        self._sock.settimeout(_ACCEPT_POLL_S)
        self.port = self._sock.getsockname()[1]
        self.served = []
        self.refusal = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="venastine-askpass", daemon=True)

    # -- the answering loop -------------------------------------------------

    def _refuse(self, why: str) -> None:
        """Refuse, and LOCK (SS60).

        Every refusal here means something asked for a secret in a way the
        harness did not arrange: a token it should not have, a prompt
        nobody measured, or more asks than a connection can legitimately
        make. None of those is a condition to keep plaintext through.

        NAMES the cause, never the prompt's content or any value.
        """
        if not self.refusal:
            self.refusal = why
        logger.warning("askpass: refused (%s)", why)
        secrets.lock("askpass refused: %s" % why)

    def _run(self) -> None:
        try:
            self._loop()
        except OSError:
            # The socket was closed underneath us, which is what `close()`
            # does and is not a failure. Belt and braces over the
            # `settimeout` move above: this thread must never raise, because
            # an unhandled thread exception is a traceback on stderr and
            # nothing here is worth painting over a running TUI for.
            return

    def _loop(self) -> None:
        deadline = time.monotonic() + DEADLINE_S
        while not self._stop.is_set():
            if time.monotonic() > deadline:
                self._refuse("deadline")
                return
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._answer(conn)
            except OSError as exc:
                self._refuse("connection error: %s" % type(exc).__name__)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _answer(self, conn) -> None:
        conn.settimeout(_CLIENT_TIMEOUT_S)
        data = b""
        while b"\n\n" not in data and len(data) < 8192:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        token, _, rest = data.partition(b"\n")
        prompt = rest.partition(b"\n")[0]
        # compare_digest, not ==. The token is a secret of its own for the
        # length of one connection, and a timing oracle on it is a timing
        # oracle on the password behind it.
        # REFUSE FIRST, REPLY SECOND, in all four branches. The reply is
        # an empty line, which makes the helper exit non-zero and ssh give
        # up -- but sending it before `_refuse` has locked the store leaves
        # a window in which the answer is already out and the plaintext is
        # still held. The window is small and it is not nothing, and
        # closing it costs the two lines being swapped.
        #
        # It also makes the refusal OBSERVABLE the moment the client's
        # `recv` returns, which is what lets the tests assert it without a
        # local wait helper -- and a local wait helper is what
        # `test_pilot_wait.py` forbids, for the reason five copies of one
        # taught this project.
        if not hmac.compare_digest(token.decode("utf-8", "replace"),
                                   self._token):
            self._refuse("bad token")
            conn.sendall(b"\n")
            return
        if len(self.served) >= MAX_SERVES:
            self._refuse("more prompts than a connection can make")
            conn.sendall(b"\n")
            return
        name = classify(prompt.decode("utf-8", "replace"))
        if name is None:
            self._refuse("unrecognised prompt")
            conn.sendall(b"\n")
            return
        secret = self._answers.get(name)
        if secret is None:
            self._refuse("no stored secret answers a %s prompt" % name)
            conn.sendall(b"\n")
            return
        conn.sendall(secret.reveal() + b"\n")
        self.served.append(name)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    @property
    def token(self) -> str:
        return self._token


def _write_helper(directory: str) -> str:
    """Write the helper and return what `SSH_ASKPASS` should point at.

    On Windows `SSH_ASKPASS` has to name something `CreateProcess` can run
    and takes no arguments of its own, so the script cannot be named
    directly -- a `.cmd` shim carries the interpreter. Measured: both a
    `.cmd` and a `.bat` work, and the prompt reaches the script as
    `argv[1]` through either.

    On POSIX the script is executable with its own shebang and no shim.
    """
    script = os.path.join(directory, "venastine_askpass.py")
    with open(script, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_HELPER_SOURCE)
    if os.name == "nt":
        shim = os.path.join(directory, "venastine_askpass.cmd")
        with open(shim, "w", newline="") as handle:
            handle.write('@echo off\r\n"%s" "%s" %%*\r\n'
                         % (sys.executable, script))
        return shim
    os.chmod(script, stat.S_IRWXU)
    launcher = os.path.join(directory, "venastine_askpass.sh")
    with open(launcher, "w", encoding="utf-8", newline="\n") as handle:
        handle.write('#!/bin/sh\nexec "%s" "%s" "$@"\n'
                     % (sys.executable, script))
    os.chmod(launcher, stat.S_IRWXU)
    return launcher


class Serving:
    """The live connection window: what the environment must carry, and
    what happened."""

    def __init__(self, listener: _Listener, env: dict) -> None:
        self._listener = listener
        self.env = env

    @property
    def served(self) -> list:
        return list(self._listener.served)

    @property
    def refusal(self) -> str:
        return self._listener.refusal


class serving:
    """Context manager: stand up a listener and a helper for one connection.

    Yields a `Serving` whose `.env` is the fragment to merge into the
    subprocess environment. On exit the listener is closed and the helper
    directory removed, whatever happened -- and an exception escaping the
    block locks the store, because an unknown state is not a state to hold
    plaintext through (SS60).

    Not a `@contextlib.contextmanager` generator: the cleanup has to run on
    every path including the one where `__enter__` itself fails partway,
    and a class makes each half explicit.
    """

    def __init__(self, answers: dict) -> None:
        # No None-filter here, and that is a deliberate removal: one was
        # written, and the mutation pass showed it can never be observed,
        # because `_Listener._answer` checks for a missing answer anyway
        # and a None value reaches the same refusal. Two guards where one
        # fires is the shape that reads as protection without being any.
        self._answers = dict(answers)
        self._listener = None
        self._dir = ""

    def __enter__(self) -> Serving:
        self._dir = tempfile.mkdtemp(prefix="venastine-askpass-")
        try:
            helper = _write_helper(self._dir)
            self._listener = _Listener(self._answers)
            self._listener.start()
        except BaseException:
            self._cleanup()
            raise
        env = {
            "SSH_ASKPASS": helper,
            # The whole guarantee. Without `force`, ssh uses askpass only
            # when it has no terminal AND DISPLAY is set -- and with a
            # terminal it prompts, which is the 20-second hang measured
            # above.
            "SSH_ASKPASS_REQUIRE": "force",
            "VEN_ASKPASS_TOKEN": self._listener.token,
            "VEN_ASKPASS_PORT": str(self._listener.port),
        }
        return Serving(self._listener, env)

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._cleanup()
        if exc_type is not None:
            secrets.lock("exception during an askpass connection")
        return False

    def _cleanup(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = ""
