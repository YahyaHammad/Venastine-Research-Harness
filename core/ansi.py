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

BOTH ENCODINGS OF A CONTROL, since batch 145. Every sequence here has two
forms: `ESC [` and the single character U+009B are the SAME introducer, and
a terminal in UTF-8 mode decodes the two bytes `\xc2\x9b` back into one.
Until batch 145 only the ESC form was removed, which was not a judgement --
`_sequence_end` keyed on ESC and the 8-bit forms were never considered.
Measured, all seven introducers survived (CSI, OSC, DCS, SOS, PM, APC, and
ST as a terminator), so `x\x9b2Jy` came through whole while `x\x1b[2Jy`
became `xy`. The asymmetry is the argument on its own: this module exists
because a control must not reach a terminal, and which of two spellings
the sender chose was never part of that decision. The whole C1 range
0x80-0x9F goes, not only the introducers, for the same symmetry -- `ESC D`
and `ESC E` already went through the two-character fallback below, and
U+0084 and U+0085 are those same two controls.

  The cost, stated because it is real: text that was decoded as latin-1
  where it was written as cp1252 carries C1 characters where its curly
  quotes belong, and those characters are now DELETED rather than drawn
  as something odd. A C1 character cannot be drawn meaningfully either
  way -- the alternative is handing it to the terminal as a control --
  so the choice is between losing it and acting on it.

THE UNTERMINATED CARRY IS THE ONE THING THE CONSUMERS DISAGREE ABOUT, which
is why it is a flag and not a rule (batch 145). For the pty, a carry that
never completes is text: a program's output cut off mid-sequence is still
the output the agent asked for, and holding it back forever would lose it.
For a TRANSCRIPT the same release is a leak, because the text there is
handed to a terminal, and `ESC [` with no final byte consumes whatever is
printed after it as its final byte. `release_unterminated` names the two,
and the pty's answer stays the default so nothing about SS27 moves.

A THIRD CONSUMER SINCE BATCH 152, AND IT IS NOT A STREAM. `main.py` prints
model- and third-party text to a terminal one whole string at a time -- an
approval headline, a notice, the agent's own stated reason, a question, a
reviewer's proposed correction, an MCP server's description of what its
tool does. None of those is a pty's output and none is a transcript's
deltas, but the hazard is the transcript's exactly: the text is handed to
a terminal. Measured before the fix (batch 152), through the real channel --
a headline of `cat README.md`, CSI `2K`, a carriage return and a second
`$ cat .venastine/settings.json` reached stdout with the introducer
intact, and CSI `2K` erases the row the approved command was drawn on. So
`for_display` below takes the transcript's answer to the carry, for the
transcript's reason, and it lives here so that the CLI composes nothing of
its own.

WHAT `for_display` REMOVES AND WHAT IT KEEPS, since a display filter
cannot inherit the pty's answer to either. A LONE CARRIAGE RETURN GOES.
This module leaves one in a pty's text because interpreting it would be
emulation -- and that argument inverts at a terminal, where a carriage
return is not ambiguous at all: it returns the cursor to column 0 and
whatever prints next overwrites the line the user is reading. The other
other controls below U+0020 go with it, and DEL, for the same reason
rather than for tidiness: backspace, vertical tab and form feed move a
cursor too.
A newline and a tab stay, because they are the only two a caller means.
The per-line `$ ` prefix in `main.py`'s headline loop keeps doing its own
job: measured, `str.splitlines()` splits on a carriage return as well as a
newline, so one inside a headline already became a second prefixed line
rather than an overwrite, and the forged line wears a doubled prefix.

NOT A BIDI OR ZERO-WIDTH FILTER, and that boundary is deliberate. U+202E
and its family can make a terminal draw text in an order the bytes do not
have, which is the same class of problem -- but removing them is not the
same kind of answer, because a bidi mark is legitimate content in Arabic
and Hebrew text while a control character is never legitimate content at
all. TECHNICAL_DEBT 104 carried it, and batch 160 answered it where the
answer is different: `reveal_invisible_counted` shows them as `<U+202E>`
in the two approval prompts, and nowhere else.
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

# The 8-bit forms. Each is one character where the ESC form is two, and
# means exactly the same thing to a terminal that decodes it -- so the
# scanner below reaches the SAME two helpers for either spelling, which is
# what keeps there being one answer per sequence shape rather than two.
_C1_CSI = "\x9b"                     # ESC [
_C1_ST = "\x9c"                      # ESC \ , a terminator rather than an opener
# DCS, SOS, OSC, PM, APC: ESC P, ESC X, ESC ], ESC ^, ESC _
_C1_STRING_OPENERS = frozenset("\x90\x98\x9d\x9e\x9f")
# Every C1 control. The ones that are not introducers are single-character
# controls a terminal still ACTS on (U+0084 is IND, U+0085 is NEL), and
# their ESC spellings were already removed by the fallback at the end of
# `_sequence_end`, so removing these is the symmetry and not a widening.
_C1 = frozenset(chr(c) for c in range(0x80, 0xA0))


class AnsiStripper:
    """Escape sequences out, CRLF to LF, everything else untouched.

    CRLF is folded because it is the PTY's artifact and not the program's
    text: the same command run by `shell` returns LF, and a session whose
    output differed from a one-shot run's by a carriage return per line
    would be the drift EP6 names, in the only place the agent can see it.
    A LONE CR is left exactly where it was -- see the module docstring.

    `release_unterminated` is the pty's answer by default and the module
    docstring argues both; `False` is the transcript's, where a carry that
    never completed must not become text. It governs BOTH releases -- the
    `_MAX_CARRY` overflow and `flush` -- because they are the same event
    reached by two routes, and covering one would leave the other as the
    way out.

    `sequences_removed` counts what this object took out, so a caller can
    say that something was removed without rescanning the text to find out.
    Nothing here decides what to do with that count; the only thing that
    could is the layer that knows who is reading.
    """

    def __init__(self, *, release_unterminated: bool = True) -> None:
        self._carry = ""
        self._release = release_unterminated
        self.sequences_removed = 0

    def feed(self, text: str) -> str:
        if not text:
            return ""
        s = self._carry + text
        self._carry = ""
        out: list[str] = []
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch == _ESC or ch in _C1:
                end = _sequence_end(s, i)
                if end is None:
                    self._carry = s[i:]
                    break
                self.sequences_removed += 1
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
            # The declared-malformed release. For the pty it is text (see
            # _MAX_CARRY's own comment); for a transcript it is the leak
            # this flag exists to close, and dropping it costs at most
            # _MAX_CARRY characters of a sequence nobody ever closed.
            if self._release:
                out.append(self._carry)
            else:
                self.sequences_removed += 1
            self._carry = ""
        return "".join(out)

    def flush(self) -> str:
        """The end of the stream: an unterminated carry was never a
        sequence, so it is text after all -- unless the caller said it is
        not, in which case it is dropped and nothing is returned. See the
        class docstring for which consumer wants which."""
        held, self._carry = self._carry, ""
        if not self._release:
            if held:
                self.sequences_removed += 1
            return ""
        return held


# What `for_display` will not hand to a terminal. The 8-bit controls at
# U+0080-U+009F are already gone by the time this applies --
# `_sequence_end` removes every one of them -- so this is U+0000-U+001F
# plus DEL, less the two a caller means.
_DISPLAY_KEEPS = frozenset("\n\t")
_NOT_PRINTABLE = (frozenset(chr(c) for c in range(0x20)) - _DISPLAY_KEEPS
                  | frozenset("\x7f"))


def for_display_counted(text) -> tuple[str, int]:
    """*text* as it may be PRINTED to a terminal, and what that cost.

    The count is sequences-and-controls REMOVED, and that is why this form
    exists at all: `main.py`'s consent prompts say so out loud, because a
    tool call whose headline tries to move the cursor is evidence about the
    decision being asked rather than a rendering detail. Folding CRLF to
    LF is NOT counted -- it is a normalisation, and counting it would put
    the warning on ordinary text that arrived with Windows line endings.

    Takes anything rather than only a str, so a call site can hand it the
    same `payload.get(...)` an f-string would have interpolated, None
    included. The alternative is twenty call sites each deciding how to
    coerce, which is the shape this module exists to prevent.
    """
    stripper = AnsiStripper(release_unterminated=False)
    scanned = stripper.feed(str(text)) + stripper.flush()
    kept = "".join(ch for ch in scanned if ch not in _NOT_PRINTABLE)
    return kept, stripper.sequences_removed + len(scanned) - len(kept)


def for_display(text) -> str:
    """`for_display_counted` without the count, for the callers printing
    untrusted text where nothing is being decided -- a replayed thread, a
    research pass's progress lines. Defined in terms of it rather than
    beside it, so there is one scanner and one answer.
    """
    return for_display_counted(text)[0]


#: Characters that change how text is DRAWN without being drawn
#: (TECHNICAL_DEBT 104, batch 160): the bidirectional embeddings, overrides
#: and isolates, the marks, the zero-width characters and the byte-order
#: mark, the invisible operators, and the TAG block -- which is ASCII
#: spelled invisibly, and the published way to smuggle text past a reader.
_INVISIBLE = frozenset(
    [0x061C, 0x180E, 0xFEFF]
    + list(range(0x200B, 0x2010))
    + list(range(0x202A, 0x202F))
    + list(range(0x2060, 0x2065))
    + list(range(0x2066, 0x206A))
    + list(range(0xE0000, 0xE0080)))
_REVEALED = {code: f"<U+{code:04X}>" for code in _INVISIBLE}


def reveal_invisible_counted(text) -> tuple[str, int]:
    """*text* with each character in `_INVISIBLE` shown as `<U+XXXX>`, and
    how many there were.

    FOR CONSENT PROMPTS ONLY, by owner ruling (batch 160), and that scope
    is the module docstring's reason applied rather than set aside. A bidi
    mark is ordinary content in Arabic and Hebrew, a zero-width joiner
    builds every family emoji, a non-joiner spells Persian -- so revealing
    them in a transcript corrupts correct text. In an approval the trade
    reverses: the text is a command a person is about to authorise, a
    U+202E can draw it in an order its bytes do not have (the Trojan Source
    class), and the reader needs the bytes. Revealed rather than removed,
    because removing one is the corruption, and a reader shown
    `<U+202E>` can see both what is there and that something was.

    Takes anything, as `for_display_counted` does; meant to run on its
    output, since a control character is removed and an invisible one is
    shown, and the two never overlap.
    """
    text = str(text)
    count = sum(1 for ch in text if ord(ch) in _INVISIBLE)
    if not count:
        return text, 0
    return text.translate(_REVEALED), count


def _sequence_end(s: str, i: int) -> int | None:
    """Index just past the escape sequence at *i*, or None if it is not all
    here yet. *s[i]* is ESC or a C1 control (batch 145).

    The two spellings reach the SAME two scanners -- a CSI ends at its final
    byte, a string sequence ends at its terminator -- and differ only in
    where the body starts, one character in for a C1 introducer and two for
    ESC. That is the whole of the difference, which is why it is expressed
    as an offset rather than as a second branch per shape.
    """
    n = len(s)
    ch = s[i]
    if ch != _ESC:
        if ch == _C1_CSI:
            return _csi_end(s, i + 1)
        if ch in _C1_STRING_OPENERS:
            return _string_end(s, i + 1)
        # Every other C1 is a single-character control, ST included: it
        # terminates a string sequence, and a bare one outside any
        # sequence is still a control the terminal would act on.
        return i + 1
    if i + 1 >= n:
        return None
    opener = s[i + 1]
    if opener == "[":
        return _csi_end(s, i + 2)
    if opener in _STRING_OPENERS:
        return _string_end(s, i + 2)
    if opener in _CHARSET:
        return i + 3 if i + 2 < n else None
    return i + 2


def _csi_end(s: str, j: int) -> int | None:
    """Index just past the CSI sequence whose BODY starts at *j*."""
    n = len(s)
    while j < n:
        if s[j] in _CSI_FINAL:
            return j + 1
        j += 1
    return None


def _string_end(s: str, j: int) -> int | None:
    """Index just past the string sequence whose BODY starts at *j*.

    Three terminators, because a sender may mix the spellings freely: BEL
    (OSC's traditional one), `ESC \\`, and the 8-bit ST U+009C. An OSC
    opened with ESC and closed with the 8-bit ST is legal and was not
    recognised before batch 145, which left the ESC consumed and the
    terminator standing.
    """
    n = len(s)
    while j < n:
        if s[j] == _BEL or s[j] == _C1_ST:
            return j + 1
        if s[j] == _ESC:
            if j + 1 >= n:
                return None
            if s[j + 1] == "\\":
                return j + 2
        j += 1
    return None


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
