"""
tui/screens.py

ROADMAP_v2 §16 modal screens: the permission prompt (AC2) and the thread
picker (AC1), plus §25's research grant picker.

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
from textual.widgets import (
    Button, Input, Label, ListItem, ListView, SelectionList, Static,
)
from textual.widgets.selection_list import Selection


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


class GrantPickerScreen(ModalScreen[object]):
    """Pick which approval-gated tools a research run may use (§25 R1).

    Dismisses with a set of names, or None to cancel the run entirely --
    the two are different answers. An empty SET means "run with no grants"
    (a legitimate choice: the pipeline just keeps hiding gated tools),
    while None means "I did not mean to start this at all".

    Per-tool rather than one prompt for the whole set, and each row shows
    what the tool says it does, because the harness cannot tell a
    read-only tool from one that writes or sends -- MCP exposes no such
    metadata. The description and the individual choice are the entire
    substance of informed consent here.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, offered: list):
        super().__init__()
        self._offered = list(offered)

    def compose(self) -> ComposeResult:
        # Every option starts UNSELECTED. A pre-ticked list would make the
        # convenient action the permissive one, and this prompt exists
        # precisely because nobody will be watching afterwards.
        options = [
            Selection(f"{name}  —  {description}" if description else name,
                      name, False)
            for name, description in self._offered
        ]
        yield Vertical(
            Label("Authorise for this run only", id="grant-title"),
            Static(
                "These tools need approval, and nothing can give it during "
                "an unattended run.\nSpace toggles, Enter confirms, Escape "
                "cancels the run. Nothing is saved.",
                id="grant-help"),
            SelectionList(*options, id="grant-list"),
            Button("Run with selected", variant="success", id="grant-ok"),
            id="grant-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(set(self.query_one("#grant-list", SelectionList).selected))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReviewScreen(ModalScreen[object]):
    """Decide one proposed correction to a finished research run (§20 V4).

    Dismisses with a (decision, notes) tuple. Every dismissal path carries
    one -- escape, the buttons, and the app's shutdown release -- because
    the /research worker is blocked on a queue and a dismissal carrying
    None would park it forever. That invariant now applies in a fifth
    place; the decoder in tui/app.py treats anything malformed as a
    rejection so a new path added later fails safe rather than silently.

    Four outcomes, not two. Reject and refine are different answers: "this
    is wrong" ends the finding, while "you missed something" sends it back
    with a note. And "reject the rest" is the escape a long review needs,
    safe by construction because it only ever declines -- the affordance
    that must NOT exist is the one that accepts in bulk.
    """

    BINDINGS = [("escape", "reject", "Reject")]

    def __init__(self, finding: dict, round_index: int = 0,
                 max_rounds: int = 0):
        super().__init__()
        self._finding = finding
        self._round = round_index
        self._max_rounds = max_rounds

    def compose(self) -> ComposeResult:
        kind = self._finding.get("kind", "?")
        target = self._finding.get("claim_id") or "the report"
        severity = self._finding.get("severity", "unknown")
        body = (
            f"Why: {self._finding.get('reason', '(no reason given)')}\n\n"
            f"Proposed: {self._finding.get('proposed')}"
        )
        if self._round:
            body = (f"(refinement {self._round} of {self._max_rounds})\n\n"
                    f"{body}")
        yield Vertical(
            Label(f"{kind} correction to {target}  [{severity}]",
                  id="review-title"),
            Static(body, id="review-body"),
            Input(placeholder="Note for the reviewer (used by Refine)",
                  id="review-note"),
            Button("Accept", variant="success", id="review-accept"),
            Button("Refine", variant="primary", id="review-refine"),
            Button("Reject", variant="error", id="review-reject"),
            Button("Reject the rest", variant="error", id="review-reject-all"),
            id="review-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        decisions = {
            "review-accept": "accept",
            "review-refine": "refine",
            "review-reject": "reject",
            "review-reject-all": "reject_all",
        }
        note = self.query_one("#review-note", Input).value.strip()
        self.dismiss((decisions[event.button.id], note))

    def action_reject(self) -> None:
        # Escape declines rather than dismissing with no value, for the
        # same reason PermissionScreen's escape denies.
        self.dismiss(("reject", ""))


class ClaimsScreen(ModalScreen[None]):
    """Every claim a research run produced, with the metadata that decided
    its tier (ROADMAP_v2 §26).

    The report a run ends with is a synthesis. What it was synthesised
    FROM -- which assertions were extracted, which were grounded and by
    how many sources, which carried unexamined assumptions, which were
    revised and how often, and what arithmetic produced each tier -- all
    exists on every Claim and reaches /output/<run_id>/, and none of it was
    reachable from either shell. So a run could be read only as prose,
    with the confidence machinery that is the point of the pipeline
    visible nowhere.

    A MODAL rather than transcript rows: a dozen claims at this detail is
    longer than the report, and dumping it inline would push the answer
    off screen at the moment it arrives. Scrollable, escape closes.

    Takes plain DICTS, not Claim objects, and that is deliberate -- the
    three sources are `vars(c)` for a finished in-session run,
    pipeline_storage.load_pipeline_run() for an earlier one (which already
    returns the dicts vars(c) wrote), and the partial picture assembled
    from PipelineEvents while a run is still going. One shape here means
    no branch inside the renderer, and it is the shape §5 already
    persists.
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, claims: list, title: str = "Claims",
                 partial: bool = False):
        super().__init__()
        self._claims = list(claims or [])
        self._title = title
        self._partial = partial

    def compose(self) -> ComposeResult:
        header = f"{self._title}  ({len(self._claims)})"
        if self._partial:
            # Say so, rather than showing thin rows and letting the reader
            # conclude the pipeline recorded nothing: mid-run, only id,
            # text and tier have been broadcast as events.
            header += "  — run in progress, metadata incomplete"
        yield Vertical(
            Label(header, id="claims-title"),
            Static(_render_claims(self._claims), id="claims-body"),
            id="claims-dialog",
        )

    def action_close(self) -> None:
        self.dismiss(None)


# Tier order for the claims view: worst first. A reader opening this is
# looking for what NOT to trust, and burying the unverified claims under
# the confident ones makes them the hardest thing to find.
_TIER_ORDER = ("UNVERIFIED", "UNVERIFIED_COVERAGE", "LOW", "MEDIUM", "HIGH")


def _render_claims(claims: list) -> str:
    if not claims:
        return "No claims recorded for this run."

    def _rank(claim):
        tier = claim.get("confidence_tier")
        return _TIER_ORDER.index(tier) if tier in _TIER_ORDER else len(_TIER_ORDER)

    out = []
    for claim in sorted(claims, key=_rank):
        tier = claim.get("confidence_tier") or "—"
        head = f"{tier:<20} {claim.get('id', '?')}  {claim.get('type') or ''}"
        if claim.get("retry_count"):
            head += f"   revised x{claim['retry_count']}"
        out.append(head.rstrip())
        # final_text is what the claim carries into the report; text is
        # what Pass 2 extracted. Showing final_text when they differ means
        # the view matches the report rather than the draft behind it.
        out.append(f"  {claim.get('final_text') or claim.get('text') or ''}")
        if claim.get("entities"):
            out.append(f"  entities: {', '.join(map(str, claim['entities']))}")
        if claim.get("grounding_status") or claim.get("grounding_sources"):
            sources = len(claim.get("grounding_sources") or ())
            out.append(f"  grounding: {claim.get('grounding_status') or 'none'}"
                       f" ({sources} source{'s' if sources != 1 else ''})")
        for field, label in (("fallacies", "fallacies"),
                             ("contradictions", "contradictions"),
                             ("assumption_flags", "assumptions")):
            if claim.get(field):
                out.append(f"  {label}: {'; '.join(map(str, claim[field]))}")
        if claim.get("score_breakdown"):
            out.append("  score: " + ", ".join(
                f"{k}={v}" for k, v in claim["score_breakdown"].items()))
        if claim.get("annotation"):
            out.append(f"  {claim['annotation']}")
        out.append("")
    return "\n".join(out).rstrip()


def _thread_row(thread: dict) -> str:
    """One picker row: timestamp, first message, id (§27).

    `.get` for preview rather than indexing: the row shape gained fields in
    §27 and this screen is constructed directly in tests with hand-built
    dicts, where a KeyError would be a test-only failure in a renderer whose
    job is to survive a thin row.
    """
    created = thread["created_at"]
    stamp = (f"{created:%Y-%m-%d %H:%M}"
             if hasattr(created, "strftime") else str(created))
    preview = (thread.get("preview") or "").strip()
    return f"{stamp}  {preview}  ({thread['id']})" if preview \
        else f"{stamp}  {thread['id']}"


class ThreadPickerScreen(ModalScreen[object]):
    """Pick a thread to resume. Dismisses with a UUID, or None to cancel.

    Reads storage.list_threads(), which since §27 returns CONVERSATIONS only
    -- a research run creates ~15 threads of machinery and every automatic
    compaction one more, and all of them used to be offered here as things
    to resume.

    A row leads with the thread's first user message (§27), because a list
    of uuids and timestamps was still unusable once it was short: filtering
    made the picker correct, and the preview is what makes it answerable.
    The id stays, since it is what --thread takes.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, threads: list[dict]):
        super().__init__()
        self._threads = threads

    def compose(self) -> ComposeResult:
        items = [ListItem(Label(_thread_row(t))) for t in self._threads]
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
