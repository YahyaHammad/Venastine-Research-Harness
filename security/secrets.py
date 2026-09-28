"""
security/secrets.py

ROADMAP_v3 §49 slice 5a (SS56-SS62). The ONE place this harness holds a
secret it did not receive from the user at the moment of use.

WHY THIS EXISTS
---------------
Slice 4 shipped SSH holding no secret at all: SS47 chose key-and-agent auth
precisely so there was nothing to protect, and an encrypted key with no
agent simply FAILED. That is a fine answer right up until the user has an
encrypted key, or a host that wants a password, or -- slice 5b -- a sudo
prompt on the far side of a connection. All three need a value kept between
the moment the user types it and the moment a subprocess needs it.

WHAT THE GUARANTEE IS, AND IS NOT (SS56)
----------------------------------------
Stated precisely, because a half-understood control is worse than none
(UN4), and because this module is the one whose failure is silent.

It DOES defend against:

  * the AGENT reading the file. A shell command on WSL, on SSH, or on the
    host fallback can `cat` this file and gets ciphertext. That is the
    threat this harness actually has, and it is why an OS keystore was
    rejected: DPAPI decrypts for any process running as this user, and the
    agent's shell is a process running as this user;
  * the file leaving the machine -- a backup, a sync client, a stolen
    disk. Without the passphrase it is 32 bytes of salt and a blob;
  * a downgrade of the KDF parameters, which are covered by the AEAD's
    associated data rather than merely stored beside the ciphertext. An
    edited `n` does not weaken the next unlock, it fails it.

It does NOT defend against:

  * arbitrary in-process Python, which `security/posture.py` already
    concedes can rebind anything in this project, this module included;
  * a keylogger, or anything watching the user type the passphrase;
  * a crash dump taken while the store is unlocked. See `Secret` below --
    the plaintext is zeroed on lock, but CPython hands out immutable
    copies that nothing can reach, and that is measured rather than
    assumed.

WHAT IS MEASURED HERE, NOT READ FROM DOCUMENTATION (batch 103)
--------------------------------------------------------------
  * `hashlib.scrypt` REFUSES N=2**15 with the default `maxmem` --
    "ValueError: [digital envelope routines] memory limit exceeded". The
    default is not generous enough for any parameter worth using, so
    `maxmem` is passed explicitly and `_SCRYPT_MAXMEM` is derived from the
    parameters rather than being a constant someone has to remember to
    move. Without it this module would raise on the first unlock on every
    machine, which is the kind of failure that gets "fixed" by lowering N;
  * N=2**15, r=8, p=1 costs 142 ms on the machine this was built on,
    N=2**14 costs 73 ms and N=2**16 costs 286 ms. The parameter is a
    measurement, not a number copied from a blog post, and it is stored in
    the envelope so raising it later does not strand an existing file;
  * zeroing a `bytearray` really does clear the bytes -- read back through
    `ctypes.string_at` at the same address, before and after;
  * `bytes(buf)` and `AESGCM.decrypt(...)` both return IMMUTABLE objects,
    so every conversion makes a copy nothing can zero. That is why
    `Secret` holds a `bytearray` and why `reveal()` says what it costs;
  * `InvalidTag` carries no message at all. A wrong passphrase, a flipped
    tag bit and an edited `n` are indistinguishable from the exception,
    which is the behaviour this module wants: see `unlock`.
"""

import atexit
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import threading
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# A ROOT module, and no new edge: json_store.py's own docstring names
# `security/` as one of the packages it sits at the root to be reachable
# from, beside `config`, `storage`, `database` and `credentials`.
from json_store import write_json_atomic
from security import protected_paths

logger = logging.getLogger(__name__)

#: The envelope's shape. Bumping this is how a future KDF arrives without
#: guessing at an old file's meaning.
ENVELOPE_VERSION = 1

KDF_SCRYPT = "scrypt"
#: Measured on this machine at 142 ms (N=2**14 is 73 ms, N=2**16 is 286 ms).
#: Stored in the envelope, so a file written under one parameter still opens
#: after this constant moves.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12

#: `hashlib.scrypt` REFUSES anything above N=2**14 with its default limit,
#: and the error names "memory limit exceeded" rather than the parameter.
#: Derived rather than hardcoded so the two cannot drift: scrypt's working
#: set is 128*N*r bytes, and the doubling is the slack OpenSSL wants on top.
def _scrypt_maxmem(n: int, r: int) -> int:
    # No floor under this, and that is a deliberate removal rather than an
    # omission. One was written here -- for a tiny `n` the derived figure
    # IS below OpenSSL's working set, and it refuses with the same "memory
    # limit exceeded" a too-large parameter gives -- and the mutation pass
    # showed it can never fire: `_derive` is reached from `_seal` with
    # SCRYPT_N, and from `_open` only after MIN_SCRYPT_N has already
    # refused anything smaller. A guard that cannot fire still reads as
    # the thing protecting you.
    return 128 * n * r * 2


#: The weakest KDF this build will DERIVE against, whatever a file asks for.
#: SS56's downgrade defence has two independent halves and this is the
#: first: a file naming a weaker parameter is refused by policy, before any
#: key is derived. The second is the AEAD, which binds the parameters as
#: associated data so an edited one fails the tag as well. Either alone
#: would do; a security property with one implementation is a security
#: property with one typo between it and nothing.
MIN_SCRYPT_N = 1 << 14


#: What a redacted secret reads as. The same marker
#: `safety/policy_enforcement.py` already uses, imported from nowhere: this
#: module may not depend on `safety/`, and a second constant is pinned
#: against the first by a test rather than shared by an import that would
#: invert the layering `protected_paths` sets out.
REDACTED = "[REDACTED]"

#: The floor `put` enforces. Not a password-strength opinion -- it is what
#: makes SS61's value redaction survivable: a one-character secret would
#: substitute over every occurrence of that character in every tool result,
#: and the output would be destroyed rather than merely redacted.
MIN_SECRET_LENGTH = 4


class SecretsError(Exception):
    """Base for everything this module refuses to do."""


class StoreLocked(SecretsError):
    """The store is not unlocked, so there is nothing to read."""


class WrongPassphrase(SecretsError):
    """The envelope did not open. Deliberately does not say why."""


class StoreUnreadable(SecretsError):
    """The file is not a store this build can open, WHATEVER the passphrase.

    The distinction `WrongPassphrase` cannot carry, and it is a type rather
    than a convention because the convention had already failed twice
    (batch 108).

    IN THE TESTS: `test_an_unknown_version_refuses_rather_than_guessing`
    asserted `SecretsError`, which `WrongPassphrase` subclasses -- so
    deleting the version check entirely still passed, because the AEAD then
    failed the tag for a different reason. The mutation pass found it, and
    the repair was `assert not isinstance(excinfo.value, WrongPassphrase)`
    written out at each site.

    IN PRODUCTION: both callers that report an unlock failure catch
    `SecretsError` and print it. A recovery message added to that branch
    would fire on a MISTYPED PASSPHRASE and tell someone to delete a
    perfectly good store -- the exact footgun, in the exact shape the tests
    had already met.

    So the two are separated where they are RAISED, and a caller cannot get
    the ordering wrong because there is no ordering to get wrong. Every
    structural fault is one of these: an unreadable or non-JSON file, a
    version or KDF this build does not know, malformed base64, key
    derivation parameters below `MIN_SCRYPT_N`, and a plaintext that
    decrypts cleanly and is not an entry table. A wrong passphrase is none
    of them, and neither is a tampered ciphertext -- those are
    indistinguishable by design (SS56) and both stay `WrongPassphrase`.

    Subclasses `SecretsError`, so every existing `except` and every
    `pytest.raises(SecretsError)` keeps working unchanged.
    """


class NoStore(SecretsError):
    """No store file exists yet."""


class Secret:
    """A value that cannot be printed by accident (SS62).

    The cheapest control in this batch and the one that prevents the
    largest class of leak, because every other control in this module
    assumes the value stays where it was put. An f-string, a `%s`, a
    `logger.debug`, a pytest assertion diff and a traceback all reach for
    `__str__` or `__repr__`, and all of them get the marker.

    The bytes come out through `reveal()` and nowhere else, which makes
    every REAL use greppable -- `grep -rn 'reveal()'` is the audit -- and
    every accidental one impossible.

    `reveal()` returns `bytes`, which is IMMUTABLE and therefore a copy
    nothing can zero afterwards. That is measured, not assumed, and it is
    the reason the method is named for what it does rather than called
    `value`: a caller should be able to see at the call site that they
    have just made a copy of a secret that outlives `lock()`. Hold it for
    as short a time as possible.
    """

    __slots__ = ("_buf",)

    def __init__(self, raw) -> None:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self._buf = bytearray(raw)

    def reveal(self) -> bytes:
        """The plaintext, as an immutable copy this module cannot zero."""
        return bytes(self._buf)

    def zero(self) -> None:
        """Overwrite the buffer in place. Verified with `ctypes.string_at`
        against the same address (batch 103), which is the only way to know
        it happened rather than hoping the name means what it says."""
        for index in range(len(self._buf)):
            self._buf[index] = 0

    # -- everything below exists to make an accidental disclosure impossible

    def __str__(self) -> str:
        return REDACTED

    def __repr__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __eq__(self, other) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(bytes(self._buf), bytes(other._buf))

    #: Unhashable, so it cannot become a dict key whose repr is dumped.
    __hash__ = None

    def __reduce__(self):
        raise TypeError("a Secret cannot be pickled")

    def __copy__(self):
        raise TypeError("a Secret cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("a Secret cannot be copied")


# ---------------------------------------------------------------------------
# ---- Where the file lives -------------------------------------------------
# ---------------------------------------------------------------------------

def store_path() -> str:
    """`~/.config/venastine/secrets.json`, resolved at CALL time.

    Call time rather than import time, matching `tui/preferences.py` and
    `core/model_windows.py` rather than `credentials.py`: a moved HOME is
    picked up, and a test can redirect HOME instead of monkeypatching a
    module attribute. `AGENT_SECRETS_FILE` is the explicit override, named
    to match `AGENT_PROVIDERS_FILE` and `AGENT_ENV_FILE`.

    The directory is already a PROTECTED PATH
    (`security/protected_paths.py`), so the shell gate refuses a command
    naming it and the file tools deny it outright. That is not what keeps
    the secret safe -- the encryption is -- but it means the agent cannot
    quietly delete the store either.
    """
    override = os.environ.get("AGENT_SECRETS_FILE", "")
    if override:
        return override
    return os.path.join(protected_paths.user_config_dir(), "secrets.json")


def exists() -> bool:
    """Whether a store file is present. Reads no content and needs no
    passphrase, so every "is this configured" caller can ask."""
    return os.path.exists(store_path())


def recovery_hint() -> str:
    """What to tell someone holding a file this build cannot open.

    ONE copy, for `--secrets` and `/secrets` both, because the two are the
    same sentence and the CLI one is read by someone who has already tried
    the TUI one.

    IT NAMES THE PATH AND DELETES NOTHING. A store that will not open is
    not necessarily a store with nothing in it -- it may have been written
    by a different build, or be recoverable by hand from a backup -- and
    an encrypted file is one this harness cannot inspect to find out. So
    the refusal is a refusal, and removing the file stays the user's
    action. `create` refuses to overwrite for the same reason, which is
    what makes saying the path out loud necessary rather than merely
    helpful: without it the next step is undiscoverable, in a directory
    the shell gate and the file tools both refuse to touch.
    """
    return (
        "This file is not a secrets store this build can open, and that is "
        "not a wrong passphrase -- the envelope itself is unreadable. The "
        "usual cause is a write that was interrupted before batch 108 made "
        "them atomic. Nothing here will delete it for you: if you have no "
        "backup of it, remove %s by hand and run the command again to start "
        "a new store." % store_path()
    )


# ---------------------------------------------------------------------------
# ---- The envelope ---------------------------------------------------------
# ---------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _header(envelope: dict) -> dict:
    """Everything except the ciphertext."""
    return {key: value for key, value in envelope.items() if key != "ct"}


def _aad(envelope: dict) -> bytes:
    """The associated data the ciphertext is bound to: the whole header.

    WHAT THIS ACTUALLY BUYS, corrected by the mutation pass rather than
    left as it was first written. The claim here used to be that the AAD
    is what makes a downgraded `n` fail. Measured, it is NOT: every field
    in the header today either feeds the key derivation (`salt`, `n`, `r`,
    `p`) or is checked outright (`version`, `kdf`), so an edit to any of
    them already fails without the AAD -- through a different key, or
    through an explicit refusal. Replacing this function's return with
    `b""` broke no test, which is how the overstatement was found.

    What it buys is that the property does not DEPEND on that coincidence.
    A header field added later -- a key id, a rotation counter, a hint
    about which agent may use the entry -- would not feed the KDF, and
    without this it would be an instruction the file gives its reader,
    editable by anyone who can write the file. Inside the AAD it is
    covered from the moment it exists, rather than from the moment
    somebody remembers to check it. `test_an_unrecognised_header_field_is
    _still_covered` is that case, written as the thing this is for.

    The DOWNGRADE defence is `MIN_SCRYPT_N` plus the derivation itself;
    this is the third answer, and it is the one that survives a change
    nobody has made yet.

    Canonical JSON with sorted keys, so the bytes do not depend on dict
    ordering across versions.
    """
    return json.dumps(_header(envelope), sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _derive(passphrase: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    """scrypt, with `maxmem` passed EXPLICITLY.

    Measured: the default refuses N=2**15 with "memory limit exceeded", so
    a version of this call without `maxmem` raises on the first unlock on
    every machine. That failure looks like a bad parameter rather than a
    library default, which is exactly how it gets "fixed" by lowering N.
    """
    return hashlib.scrypt(passphrase, salt=salt, n=n, r=r, p=p,
                          dklen=KEY_BYTES, maxmem=_scrypt_maxmem(n, r))


def _seal(entries: dict, passphrase: bytes) -> dict:
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    envelope = {
        "version": ENVELOPE_VERSION,
        "kdf": KDF_SCRYPT,
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
    }
    key = _derive(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    plaintext = json.dumps(entries, sort_keys=True).encode("utf-8")
    envelope["ct"] = _b64(AESGCM(key).encrypt(nonce, plaintext,
                                              _aad(envelope)))
    return envelope


def _open(envelope: dict, passphrase: bytes) -> dict:
    if envelope.get("version") != ENVELOPE_VERSION:
        raise StoreUnreadable(
            "secrets file is version %r; this build writes version %d"
            % (envelope.get("version"), ENVELOPE_VERSION))
    if envelope.get("kdf") != KDF_SCRYPT:
        raise StoreUnreadable("secrets file uses an unknown KDF: %r"
                           % (envelope.get("kdf"),))
    try:
        salt = _unb64(envelope["salt"])
        nonce = _unb64(envelope["nonce"])
        ciphertext = _unb64(envelope["ct"])
        n = int(envelope["n"])
        r = int(envelope["r"])
        p = int(envelope["p"])
    except (KeyError, ValueError, TypeError) as exc:
        raise StoreUnreadable("secrets file is malformed: %s" % exc) from exc
    if n < MIN_SCRYPT_N or r < 1 or p < 1 or n & (n - 1):
        raise StoreUnreadable(
            "secrets file asks for a weaker key derivation than this build "
            "accepts (n=%r, r=%r, p=%r); it will not be opened" % (n, r, p))
    try:
        key = _derive(passphrase, salt, n, r, p)
    except ValueError as exc:
        # Anything the KDF itself refuses is a malformed FILE, not a
        # programming error to propagate raw out of `unlock`.
        raise StoreUnreadable(
            "secrets file's key derivation parameters were rejected: %s"
            % exc) from exc
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(envelope))
    except InvalidTag as exc:
        # MEASURED: InvalidTag carries no message, and the three causes --
        # wrong passphrase, tampered ciphertext, edited KDF parameter --
        # are indistinguishable here. That is the right shape rather than a
        # limitation: telling the two apart would tell someone holding a
        # stolen file that their guess was structurally right.
        raise WrongPassphrase(
            "the passphrase did not open the secrets file") from exc
    try:
        entries = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise StoreUnreadable("secrets file decrypted to something that is not "
                           "an entry table") from exc
    if not isinstance(entries, dict):
        raise StoreUnreadable("secrets file decrypted to something that is not "
                           "an entry table")
    return entries


# ---------------------------------------------------------------------------
# ---- The unlocked state ---------------------------------------------------
# ---------------------------------------------------------------------------

class _Unlocked:
    """What a session holds between `unlock()` and `lock()`.

    `stamp` is how SS60's "the store file changed under us" trigger is
    answered without re-reading the file on every get: the (mtime, size)
    the file had when it was opened. A rotation from another process, or
    an editor writing over it, means what is in memory is no longer what
    is on disk -- and continuing to serve the old values would make the
    two silently disagree.
    """

    __slots__ = ("passphrase", "entries", "stamp")

    def __init__(self, passphrase: bytearray, entries: dict, stamp) -> None:
        self.passphrase = passphrase
        self.entries = entries
        self.stamp = stamp

    def zero(self) -> None:
        for index in range(len(self.passphrase)):
            self.passphrase[index] = 0
        for secret in self.entries.values():
            secret.zero()
        self.entries.clear()


_state: Optional[_Unlocked] = None
#: Re-entrant, because `lock()` is called from inside `guarded()` blocks
#: that already hold it, and from `atexit` while another thread may be in a
#: read.
_state_lock = threading.RLock()


def _stamp_of(path: str):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def is_unlocked() -> bool:
    with _state_lock:
        return _state is not None


def lock(reason: str = "") -> bool:
    """Drop every plaintext this process holds. Returns whether anything
    was actually held.

    SS60's single exit. Every trigger calls THIS -- the slash command, the
    CLI subcommand, quit, `atexit`, `guarded()`'s exception path, the
    askpass listener's refusals, and a store file that changed underneath.
    One function rather than each of them clearing what it happens to know
    about, which is how one of them ends up clearing less.

    Never raises: it is called from `atexit` and from exception handlers,
    and a lock that can fail is a lock that sometimes does not happen.
    """
    global _state
    with _state_lock:
        held = _state is not None
        if held:
            try:
                _state.zero()
            except Exception:                       # pragma: no cover
                logger.error("secrets: zeroing failed during lock",
                             exc_info=True)
            _state = None
        if held and reason:
            # NAMES the reason, never a value (AGENTS.md: a breadcrumb
            # quoting a value is one log rotation away from a durable
            # secret beside the redacted sinks).
            logger.info("secrets: locked (%s)", reason)
        return held


atexit.register(lock, "process exit")


def unlock(passphrase: str) -> None:
    """Open the store for this process.

    Raises `NoStore` when there is no file, and `WrongPassphrase` when the
    envelope does not open -- which, measured, is the same exception for a
    wrong passphrase, a tampered ciphertext and an edited KDF parameter.
    """
    path = store_path()
    if not os.path.exists(path):
        raise NoStore("no secrets file at %s" % path)
    try:
        with open(path, encoding="utf-8") as handle:
            envelope = json.load(handle)
    except (OSError, ValueError) as exc:
        raise StoreUnreadable("secrets file could not be read: %s" % exc) from exc
    if not isinstance(envelope, dict):
        raise StoreUnreadable("secrets file is malformed")
    raw = passphrase.encode("utf-8")
    entries = _open(envelope, raw)
    global _state
    with _state_lock:
        lock()
        _state = _Unlocked(
            bytearray(raw),
            {name: Secret(value) for name, value in entries.items()},
            _stamp_of(path))
    logger.info("secrets: unlocked, %d entries", len(entries))


def create(passphrase: str) -> None:
    """Write a new, empty store. Refuses to overwrite an existing one.

    THE REFUSAL IS THE WRITE, not a check above it (batch 108). This was
    `if os.path.exists(path): raise`, which is a TOCTOU -- and one that got
    sharper rather than softer when `_write` became atomic, because
    `os.replace` overwrites without complaint, so the check was the only
    thing standing between a second `create` and somebody's store.
    `O_EXCL` asks the filesystem the same question indivisibly.

    So this does NOT go through `_write`. Atomic replace is the right shape
    for `_flush`, which rewrites a file that already has contents many
    times; exclusive creation is the right shape for the one write whose
    whole contract is that the file must not already be there. A crash
    inside this one leaves a truncated file that never held anything, which
    is the case `unlock`'s structural refusal names.
    """
    path = store_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    envelope = _seal({}, passphrase.encode("utf-8"))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SecretsError(
            "a secrets file already exists at %s" % path) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, indent=2, sort_keys=True)
    unlock(passphrase)


def _write(envelope: dict, path: str) -> None:
    """Write the envelope to a file created 0600, ATOMICALLY.

    0600 AT CREATION, never `open()` then `os.chmod` -- the same rule and
    the same reason as `credentials._write_secret_json`, whose comment
    records that a post-write chmod leaves a window in which the file is on
    disk with the umask's permissions. On Windows the mode is a no-op and
    the protection is the ACL on the user profile directory; that is
    recorded there too, and is why this file is encrypted rather than
    relying on the mode at all.

    THE OTHER HALF, AND IT WAS MISSING UNTIL BATCH 108. This wrote
    `O_TRUNC` straight over the live store, and `_flush` calls it on every
    `put` and `delete` -- so a crash, a full disk or a kill between the
    truncate and the flush left a half-written envelope. That is worse than
    losing the write: `exists()` stays True, so `unlock` is attempted and
    fails structurally, and `create` refuses to overwrite -- leaving a user
    who cannot store a secret again without deleting a file by hand from a
    directory the shell gate and the file tools both refuse to touch.

    `json_store.write_json_atomic` is temp-file-plus-`os.replace` and
    already existed, with the mode applied to the TEMP file because replace
    carries it across. Its own docstring is where this argument is made --
    "it leaves the file empty for the length of the write and permanently
    damaged if the process dies inside it" -- and it names `security/` as
    one of the packages it sits at the root to be reachable from. So the
    two files in this project that hold credentials were the last two
    JSON stores still using the pattern that module was extracted to
    delete, and this is not a new writer but the one that was already
    there.
    """
    write_json_atomic(path, envelope, mode=0o600, indent=2, sort_keys=True)


def _require_unlocked() -> _Unlocked:
    with _state_lock:
        if _state is None:
            raise StoreLocked("the secrets store is locked")
        path = store_path()
        if _stamp_of(path) != _state.stamp:
            # SS60. What is in memory is no longer what is on disk, so
            # somebody rotated, replaced or removed the file. Continuing to
            # serve the values held here would make the two silently
            # disagree; locking makes the next use ask.
            lock("store file changed on disk")
            raise StoreLocked(
                "the secrets file changed on disk; the store was locked")
        return _state


def names() -> list:
    """Every entry name, sorted. NEVER a value -- this is the only thing a
    listing surface may read, which is what lets `/secrets list` be written
    without a way to print a password."""
    return sorted(_require_unlocked().entries)


def get(name: str) -> Optional[Secret]:
    """The entry, or None when there is no such name."""
    return _require_unlocked().entries.get(name)


def put(name: str, value: str) -> None:
    """Add or replace one entry and rewrite the file.

    Named `put` rather than `set` so the module does not shadow the
    builtin at every internal call site.
    """
    if not name or not name.strip():
        raise SecretsError("a secret needs a name")
    if len(value) < MIN_SECRET_LENGTH:
        raise SecretsError(
            "a secret must be at least %d characters: shorter values are "
            "substituted over every occurrence in every tool result and "
            "would destroy the output rather than redact it"
            % MIN_SECRET_LENGTH)
    with _state_lock:
        state = _require_unlocked()
        previous = state.entries.get(name)
        state.entries[name] = Secret(value)
        try:
            _flush(state)
        except BaseException:
            # MEMORY FOLLOWS DISK (batch 108). This used to zero the old
            # Secret and install the new one BEFORE the write, so a flush
            # that failed left the process holding a value that is not in
            # the file -- reported as an error to the user and usable for
            # the rest of the run, then gone at restart. `_require_unlocked`
            # cannot notice: it compares the stamp against the file, and a
            # failed write leaves both unchanged.
            if previous is None:
                state.entries.pop(name, None)
            else:
                state.entries[name] = previous
            raise
        # AFTER the write, and only on success: zeroing the old value is
        # the irreversible half, so it waits until the new one is really
        # on disk.
        if previous is not None:
            previous.zero()


def delete(name: str) -> bool:
    """Remove one entry. Returns whether it was there."""
    with _state_lock:
        state = _require_unlocked()
        secret = state.entries.get(name)
        if secret is None:
            return False
        del state.entries[name]
        try:
            _flush(state)
        except BaseException:
            # `put`'s rule, and the same reason: a delete that failed to
            # reach the disk must not have happened in memory either.
            state.entries[name] = secret
            raise
        secret.zero()
        return True


def _flush(state: _Unlocked) -> None:
    """Re-seal and rewrite, then re-stamp.

    A FRESH salt and nonce every write, which is not merely tidy: AES-GCM
    is catastrophically broken by a repeated (key, nonce) pair, and a store
    that kept its nonce across writes would reuse one on every edit.
    """
    path = store_path()
    entries = {name: secret.reveal().decode("utf-8")
               for name, secret in state.entries.items()}
    _write(_seal(entries, bytes(state.passphrase)), path)
    state.stamp = _stamp_of(path)


@contextlib.contextmanager
def guarded(what: str):
    """Run a block that uses secrets, and LOCK if anything escapes it.

    SS60's "an exception escaping any secret consumer" trigger, as a
    context manager rather than a rule each consumer remembers. An
    exception here means the harness no longer knows what state the
    subprocess, the connection or the store is in, and holding plaintext
    through an unknown state is exactly the condition the owner asked to
    lock on.

    Re-raises. This decides what to KEEP, not what to report.
    """
    try:
        yield
    except BaseException:
        lock("exception during %s" % what)
        raise


# ---------------------------------------------------------------------------
# ---- Value redaction (SS61) -----------------------------------------------
# ---------------------------------------------------------------------------

def redact_live_values(text: str) -> str:
    """Replace any live secret VALUE in *text* with the marker.

    UNCONDITIONAL -- it does not consult `redact_tool_outputs`, and that is
    the decision rather than an oversight. The master switch governs
    pattern GUESSES about other people's credentials, and a user who turns
    it off has chosen to see those raw. This is a value the harness itself
    put into a process, coming back out; handing it to the model is the
    harness leaking its own secret, and no switch asks for that. It follows
    the depth cap, which `redaction_enabled()` already documents as staying
    fail-closed because it is structure rather than content judgment.

    The values never leave this module: `safety/policy_enforcement.py`
    calls THIS rather than asking for a list to compare against, so there
    is exactly one place holding plaintext and the redactor is not it.

    Longest first, so a secret that contains another is replaced whole
    rather than leaving the tail of the longer one behind.
    """
    if not text:
        return text
    with _state_lock:
        if _state is None:
            return text
        values = sorted(
            (secret.reveal().decode("utf-8", "replace")
             for secret in _state.entries.values()),
            key=len, reverse=True)
    for value in values:
        if value and value in text:
            text = text.replace(value, REDACTED)
    return text
