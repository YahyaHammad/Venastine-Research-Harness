"""
core/ansi.py

ROADMAP_v3 §49, slice 2 (SS27, SS34). The two transforms a pty's output
needs before anything else sees it: escape sequences removed, and the
harness's own prompt token taken back out of the stream it marks.

WHY IN core/. `core/shell_sessions.py` reads the stream and the tools read
the buffer; both need the same text, and a second stripper anywhere else
would be the two-copies-of-one-decision shape §46 (EP6) and #157 are about.
Nothing here imports a widget, a tool or a sandbox.

WHY STATEFUL. The reader gets whatever the pipe hands it. Measured (batch
100, probe): 128 KB of output arrived in 74 chunks whose boundaries fall
wherever the kernel put them, so a sequence split across two reads is the
ORDINARY case, not the corner. Every class here holds its partial across
`feed` calls and gives it up on `flush`, mirroring the incremental UTF-8
decoder the reader already wraps the same stream in.

WHAT THIS IS NOT. Not a terminal emulator (SS27). A sequence is REMOVED,
never interpreted: nothing here moves a cursor, tracks a scroll region or
maintains a screen. So a program that paints -- `vim`, `htop`, `less` --
is not supported in an interactive session, and the tool description says
so rather than leaving it to be found. A bare CR is left in the text for
the same reason: making `working\rdone` read as `done` is emulation, and
guessing is worse than showing what arrived.
"""

from __future__ import annotations

_ESC = "\x1b"
_BEL = "\x07"

# A partial sequence is held until it completes. A stream that opens one and
# never closes it would otherwise hold text forever, so a carry this long is
# declared malformed and released as ordinary text. Longer than any real
# sequence: an OSC title is the longest thing that legitimately gets here.
_MAX_CARRY = 4096

# CSI runs ESC [ , parameter and intermediate bytes, then ONE final byte in
# this range. Splitting the ranges out is what keeps this a scanner rather
# than a parser (G2's line): it finds where a sequence ENDS and never asks
# what it meant.
_CSI_FINAL = frozenset(chr(c) for c in range(0x40, 0x7F))
# ESC ( B and friends: one intermediate, then one final byte.
_CHARSET = frozenset("()*+-./")
# Sequences that run until a String Terminator (ESC \) or, for OSC, a BEL.
_STRING_OPENERS = frozenset("]P^_X")


class AnsiStripper:
    """Escape sequences out, CRLF to LF, everything else untouched.

    CRLF is folded because it is the PTY's artifact and not the program's
    text: the same command run by `shell` returns LF, and a session whose
    output differed from a one-shot run's by a carriage return per line
    would be the drift EP6 names, in the only place the agent can see it.
    A LONE CR is left exactly where it was -- see the module docstring.
    """

    def __init__(self) -> None:
        self._carry = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        s = self._carry + text
        self._carry = ""
        out: list[str] = []
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch == _ESC:
                end = _sequence_end(s, i)
                if end is None:
                    self._carry = s[i:]
                    break
                i = end
                continue
            if ch == "\r":
                if i + 1 == n:
                    # It may be the CR of a CRLF whose LF is in the next
                    # read. Held rather than emitted, because deciding now
                    # is deciding without the evidence.
                    self._carry = "\r"
                    break
                if s[i + 1] == "\n":
                    out.append("\n")
                    i += 2
                    continue
            out.append(ch)
            i += 1
        if len(self._carry) > _MAX_CARRY:
            out.append(self._carry)
            self._carry = ""
        return "".join(out)

    def flush(self) -> str:
        """The end of the stream: an unterminated carry was never a
        sequence, so it is text after all."""
        held, self._carry = self._carry, ""
        return held


def _sequence_end(s: str, i: int) -> int | None:
    """Index just past the escape sequence at *i*, or None if it is not all
    here yet. *s[i]* is ESC."""
    n = len(s)
    if i + 1 >= n:
        return None
    opener = s[i + 1]
    if opener == "[":
        j = i + 2
        while j < n:
            if s[j] in _CSI_FINAL:
                return j + 1
            j += 1
        return None
    if opener in _STRING_OPENERS:
        j = i + 2
        while j < n:
            if s[j] == _BEL:
                return j + 1
            if s[j] == _ESC:
                if j + 1 >= n:
                    return None
                if s[j + 1] == "\\":
                    return j + 2
            j += 1
        return None
    if opener in _CHARSET:
        return i + 3 if i + 2 < n else None
    return i + 2


class PromptSplitter:
    """Takes the harness's prompt token back out, and says when it landed.

    SS34's sentinel is machinery, not output: the agent asked for a command's
    result and a token this process minted is not part of it. It is removed
    from what the buffer keeps, and what it MEANT is kept as a count.

    TWO facts, because one cannot answer it. `ready_count` counts how many
    times the shell has PRINTED its prompt, and `at_prompt` says whether the
    stream is resting on one right now. A caller marks the count, writes,
    and waits for both (SS26): the count alone would miss the ordinary case
    where a command's output and the prompt that follows it arrive in a
    SINGLE read -- a transition never happens because the shell was already
    at a prompt when the call began -- and `at_prompt` alone would answer
    yes to that same pre-existing prompt before the command had even run.

    Together they also hold the only case where the token could lie: a
    command that PRINTS it. That raises the count, but the output which
    follows clears `at_prompt`, so no false ready is left standing.
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._carry = ""
        self.ready_count = 0
        self.at_prompt = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        s = self._carry + text
        self._carry = ""
        if not self._token:
            return s
        hits = s.count(self._token)
        # Read BEFORE the removal, and off the carry-joined text, so a token
        # split across two reads is still seen to end the stream.
        self.at_prompt = s.endswith(self._token)
        if hits:
            self.ready_count += hits
            s = s.replace(self._token, "")
        # Hold back only what could still BECOME the token, so at most
        # len(token)-1 characters are ever delayed, and only when they
        # already look like its beginning.
        keep = _prefix_held(s, self._token)
        if keep:
            s, self._carry = s[:-keep], s[-keep:]
        return s

    def flush(self) -> str:
        held, self._carry = self._carry, ""
        return held


def _prefix_held(s: str, token: str) -> int:
    """How many trailing characters of *s* are a proper prefix of *token*."""
    longest = min(len(token) - 1, len(s))
    for size in range(longest, 0, -1):
        if token.startswith(s[-size:]):
            return size
    return 0


class PtyStream:
    """Both transforms, in the one order they can go in.

    Escapes first: the token is looked for in TEXT, and a sequence sitting
    between the prompt and the end of the stream would hide it. One object,
    so a caller cannot hold them in the other order or forget one.
    """

    def __init__(self, token: str) -> None:
        # Required, with no default. A stream with no token to look for
        # would report a prompt that never comes, which is the shape of
        # every one of SS26's bounds answering wrongly -- and nothing
        # builds one: an interactive session always has a minted token,
        # and no other kind builds a PtyStream at all.
        self._ansi = AnsiStripper()
        self._prompt = PromptSplitter(token)

    @property
    def ready_count(self) -> int:
        return self._prompt.ready_count

    @property
    def at_prompt(self) -> bool:
        return self._prompt.at_prompt

    def feed(self, text: str) -> str:
        return self._prompt.feed(self._ansi.feed(text))

    def flush(self) -> str:
        return self._prompt.feed(self._ansi.flush()) + self._prompt.flush()
