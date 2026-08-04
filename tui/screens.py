"""
tui/screens.py

ROADMAP_v2 §16 modal screens: the permission prompt (AC2) and the thread
picker (AC1).

The permission modal is the load-bearing one. §13 built `permission_channel`
as a plain stdlib queue.Queue precisely so this hand-off works: the worker
thread running _run() blocks on channel.get() while the UI thread shows the
modal, and the user's answer unblocks it. Nothing here is async-aware --
the blocking side is a thread, not a coroutine.
"""

import json

from textual.app import ComposeResult
from textual.containers import Grid, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static


class PermissionScreen(ModalScreen[bool]):
    """Approve or deny one tool call. Dismisses with the decision."""

    BINDINGS = [("escape", "deny", "Deny")]

    def __init__(self, tool_name: str, params: dict, notice: str = None):
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        # Supplied by the tool via ToolSpec.approval_notice, not composed
        # here: what approving authorises is the TOOL's knowledge.
        # spawn_subagent uses it to list the tools the subagent could then
        # run unprompted, which the params alone never say.
        self._notice = notice

    def compose(self) -> ComposeResult:
        try:
            rendered = json.dumps(self._params, indent=2, default=str)
        except (TypeError, ValueError):
            rendered = repr(self._params)
        if self._notice:
            rendered = f"{self._notice}\n\n{rendered}"
        yield Grid(
            Label(f"Allow {self._tool_name}?", id="permission-title"),
            Static(rendered, id="permission-params"),
            Button("Allow", variant="success", id="allow"),
            Button("Deny", variant="error", id="deny"),
            id="permission-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")

    def action_deny(self) -> None:
        # Escape denies rather than dismissing with no value: the worker
        # thread is blocked on channel.get() and a dismissal carrying None
        # would hang the loop rather than decline the call.
        self.dismiss(False)


class ThreadPickerScreen(ModalScreen[object]):
    """Pick a thread to resume. Dismisses with a UUID, or None to cancel.

    Reads storage.list_threads(), which already exists and already returns
    threads newest-first -- §16 AC1 confirmed no storage.py change is needed
    for thread switching.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, threads: list[dict]):
        super().__init__()
        self._threads = threads

    def compose(self) -> ComposeResult:
        items = [
            ListItem(Label(
                f"{t['created_at']:%Y-%m-%d %H:%M}  {t['id']}"
                if hasattr(t["created_at"], "strftime")
                else f"{t['created_at']}  {t['id']}"
            ))
            for t in self._threads
        ]
        yield Vertical(
            Label("Resume which thread?", id="thread-title"),
            ListView(*items, id="thread-list") if items
            else Static("No saved threads yet.", id="thread-empty"),
            id="thread-dialog",
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._threads):
            self.dismiss(self._threads[index]["id"])
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
