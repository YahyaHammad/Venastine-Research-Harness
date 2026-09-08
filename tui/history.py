"""
tui/history.py

Batch 63. What you already sent, so a misfire does not cost the typing.

PURE, like `tui/markdown.py`, `tui/diffs.py` and `tui/meters.py` beside it:
no I/O, no `core/` import, no widget, no clock. `tui/app.py` owns the keys
and the box; this owns the arithmetic, so every test here is a function
call rather than a pilot pressing keys at a live TextArea.
"""

# A hundred prompts is more than a sitting produces and costs nothing to
# hold; the cap exists so a very long session cannot grow this without
# bound, not because anyone will reach it.
HISTORY_LIMIT = 100


class PromptHistory:
    """The prompts submitted this session, and where a browse has got to.

    Session-scoped and in memory: it survives `/new` and `/resume` because
    it records what the PERSON typed, not what a thread holds, and a
    misfire bad enough to start a new thread is exactly when the text is
    wanted back. Nothing here is persisted.

    **Both movers take the box's current text, and that one parameter is
    the whole design.** The history remembers what it last handed out, so
    a `current` that differs from it means the user has typed since --
    which ends the browse and starts a new one with `current` saved as the
    draft. Recall a prompt, edit it, press previous again: the edit is not
    discarded, it becomes the thing `following()` hands back.

    The alternative was to hook `PromptInput.on_text_area_changed`, where
    batch 55's `_api_edit` flag already separates assignment from typing.
    It is the wrong seam: that message is POSTED, so the app would be
    holding a flag across an async hop, and the panel's own
    `SuggestionsChanged` fires on both halves of the split anyway. Asking
    the box what it holds, at the only two moments it matters, needs
    neither.

    `previous()` STOPS at the oldest entry rather than wrapping. A
    suggestion panel wraps because its list is a menu; this is a walk
    backwards through time, and arriving at the start of it by pressing a
    key twelve times should not deposit you at the end.
    """

    def __init__(self) -> None:
        self._entries: list[str] = []       # oldest first
        # The index being shown, or None when the box holds a live draft.
        self._cursor = None
        self._draft = ""
        # The exact string last handed out. `None` while not browsing.
        self._served = None

    def __len__(self) -> int:
        """How many prompts are recallable. `check_action` reads this."""
        return len(self._entries)

    @property
    def browsing(self) -> bool:
        """True while a recalled entry is being shown. For tests."""
        return self._cursor is not None

    def remember(self, text: str) -> None:
        """Record a submitted prompt and end any browse in progress.

        Slash commands included: a mistyped `/research --attended --grant`
        is the worst retyping in the shell, and `on_prompt_input_submitted`
        is one funnel for both kinds.

        A CONSECUTIVE repeat is not appended twice -- sending the same
        thing twice is one thing to walk back past, not two. A repeat with
        something else between them is kept, because the order is the
        record of what happened.
        """
        if not text:
            return
        if not self._entries or self._entries[-1] != text:
            self._entries.append(text)
            del self._entries[:-HISTORY_LIMIT]
        self._forget_browse()

    def previous(self, current: str):
        """The next older prompt, or None when there is nowhere to go.

        None means the caller writes nothing -- an empty history, or a
        browse already at the oldest entry. The browse state is left
        alone in both cases, so a press at the oldest is inert rather
        than a reset.
        """
        if not self._entries:
            return None
        if not self._is_browsing(current):
            self._draft = current
            self._cursor = len(self._entries)
        if self._cursor == 0:
            return None
        self._cursor -= 1
        self._served = self._entries[self._cursor]
        return self._served

    def following(self, current: str):
        """The next newer prompt, or the draft once past the newest.

        None when no browse is in progress, which is what makes the key
        inert on a box the user is typing into fresh: there is nothing
        newer than what they are already looking at.
        """
        if not self._is_browsing(current):
            return None
        self._cursor += 1
        if self._cursor < len(self._entries):
            self._served = self._entries[self._cursor]
            return self._served
        draft = self._draft
        self._forget_browse()
        return draft

    # -- internals ----------------------------------------------------------

    def _is_browsing(self, current: str) -> bool:
        """Whether `current` is still the string this last handed out."""
        return self._cursor is not None and current == self._served

    def _forget_browse(self) -> None:
        self._cursor = None
        self._draft = ""
        self._served = None
