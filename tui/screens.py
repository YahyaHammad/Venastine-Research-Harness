"""
tui/screens.py

ROADMAP_v2 §16 modal screens: the permission prompt (AC2) and the thread
picker (§16 AC1), plus §25's research grant picker.

The permission modal is the load-bearing one. §13 built `permission_channel`
as a plain stdlib queue.Queue precisely so this hand-off works: the worker
thread running _run() blocks on channel.get() while the UI thread shows the
modal, and the user's answer unblocks it. Nothing here is async-aware --
the blocking side is a thread, not a coroutine.

EVERY RENDERABLE BUILT FROM A NON-LITERAL IS WRAPPED IN `Text(...)`
(batch 42, RA1). Textual renders a `str` through `Text.from_markup`, so
a square bracket in a tool argument, a claim, a thread preview or an
`ask_user` option is CONSOLE MARKUP here -- and these screens exist to
show a person exactly what a model asked for. Two live failures:

  * `sed -i "s/[/]//" f.txt` in a permission payload
    raised MarkupError inside compose(). The screen was pushed and
    never rendered, so its dismissal callback never fired and the
    worker parked on Queue.get() for ATTENDED_APPROVAL_TIMEOUT_S --
    600s, presenting as a hang. ARCHITECTURE.md records that every
    DISMISSAL path must produce a boolean; this is the case that rule
    misses, because a screen that cannot render never reaches one.

  * `ReviewScreen`'s title read `[{severity}]`, and `[high]` is a tag.
    The severity was silently absent from every review modal since
    §20 -- an unconditional defect, needing no adversary at all.

`markup=False` IS NOT THE FIX, and looks like it is. On the pinned
textual 1.0.0 `Static.__init__` stores the flag and assigns
`self._content` directly; the `visual` property then calls
`render_str()` -> `Text.from_markup` unconditionally and never reads
it. Only the `renderable` SETTER honours it, and no constructor goes
through the setter -- so `Static(x, markup=False)` still raises,
measured. `render_str` returns a `Text` unaltered, which is why
wrapping works and is the only thing here that does.

A literal written in THIS file is left alone: it is ours and it is
reviewed. The rule is mechanical, so it holds for the next screen
somebody adds -- test_tui.py walks this file's AST and asserts it.
"""

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Input, Label, ListItem, ListView, SelectionList, Static,
)
from textual.widgets.selection_list import Selection


class PermissionScreen(ModalScreen[bool]):
    """Approve or deny one tool call. Dismisses with the decision.

    The payload is shown FULL and UNREDACTED: the user must see the exact
    path/command/URL they are being asked to authorise, and truncation or
    redaction here would make the consent uninformed (the param_digest
    truncation is for the transcript only). Logs redact via
    logging_setup._RedactingFormatter and safety/policy_enforcement's
    check_output_policy, so the full value never reaches disk unguarded —
    do not add redaction here to "match" the transcript.

    Full and unredacted, and NOT INTERPRETED: the payload goes
    through `Text(...)` so a bracket in a command stays a bracket.
    Showing the exact value is this screen's whole purpose, and
    markup parsing is a way of showing something else -- see the
    module docstring.
    """

    BINDINGS = [("escape", "deny", "Deny")]

    def __init__(self, tool_name: str, params: dict, notice: str = None):
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        # Supplied by the tool via ToolSpec.approval_notice, not composed
        # here: what approving authorises is the TOOL's knowledge.
        # spawn_subagent uses it to list the tools the subagent could then
        # run unprompted, which the params alone never say.
        # DISPLAYED FULL above — see class docstring for why this stays
        # unredacted while the transcript's `▸` digest and the log do not.
        self._notice = notice

    def compose(self) -> ComposeResult:
        try:
            rendered = json.dumps(self._params, indent=2, default=str)
        except (TypeError, ValueError):
            rendered = repr(self._params)
        if self._notice:
            rendered = f"{self._notice}\n\n{rendered}"
        yield Grid(
            Label(Text(f"Allow {self._tool_name}?"),
                  id="permission-title"),
            Static(Text(rendered), id="permission-params"),
            Button("Allow", variant="success", id="allow"),
            Button("Deny", variant="error", id="deny"),
            id="permission-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")

    def action_deny(self) -> None:
        # Escape answers with the SAME value the No button produces.
        # Since §23 a dismissal carrying None would be safe anyway --
        # interaction.decode supplies APPROVAL's declining default -- so
        # this is explicitness rather than hang-prevention: one screen
        # should not have two ways to say no.
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
            Selection(Text(f"{name}  —  {description}" if description
                           else name),
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


class SubagentSignoffScreen(ModalScreen[object]):
    """Pick which of a subagent's gated tools it may use unprompted
    (ROADMAP_v2 §23 AC1b).

    Modelled on GrantPickerScreen, and dismissing with the same THREE
    answers for the same reason: a set of names grants those, an EMPTY set
    spawns the agent with nothing granted, and None refuses the spawn
    outright. Those are three different intentions and a bool cannot hold
    them -- which is precisely why §18 shipped this all-or-nothing (S1)
    on a boolean channel, and why it could only be built once §23 gave the
    channel a typed answer.

    Every option starts UNSELECTED. A pre-ticked list makes the convenient
    action the permissive one, and the point of this prompt is that
    approving a spawn used to authorise the child's ENTIRE gated set.
    """

    BINDINGS = [("escape", "refuse", "Refuse")]

    def __init__(self, agent: str, candidates: list):
        super().__init__()
        self._agent = agent
        self._candidates = list(candidates)

    def compose(self) -> ComposeResult:
        if not self._candidates:
            yield Grid(
                Label(Text(f"Run {self._agent}?"),
                      id="permission-title"),
                Static(Text(f"{self._agent} needs no approval-gated "
                            f"tools."),
                       id="permission-params"),
                Button("Run", variant="success", id="signoff-none"),
                Button("Refuse", variant="error", id="signoff-refuse"),
                id="permission-dialog",
            )
            return
        options = [Selection(Text(name), name, False)
                   for name in self._candidates]
        yield Vertical(
            Label(Text(f"What may {self._agent} use without asking again?"),
                  id="grant-title"),
            Static(
                "Space toggles, then choose. Anything left unticked still "
                "prompts you if the subagent tries it.\nRunning with none "
                "selected is fine. Escape refuses the spawn entirely.",
                id="grant-help"),
            SelectionList(*options, id="signoff-list"),
            Button("Run with selected", variant="success", id="signoff-ok"),
            Button("Refuse the spawn", variant="error", id="signoff-refuse"),
            id="grant-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "signoff-refuse":
            self.dismiss(None)
        elif event.button.id == "signoff-none":
            self.dismiss(set())
        else:
            self.dismiss(
                set(self.query_one("#signoff-list", SelectionList).selected))

    def action_refuse(self) -> None:
        # Escape REFUSES rather than running with nothing granted. The two
        # are different answers, and the one a user reaches for when they
        # did not mean to start this is the refusal.
        self.dismiss(None)


class QuestionScreen(ModalScreen[object]):
    """Ask the user the model's question (§23 slice 2, AC2).

    Dismisses with a dict, or None. THREE answers, and the middle one is
    the one to lose -- the same distinction SubagentSignoffScreen draws:

      None                     nobody answered. Escape, and the app's
                               shutdown release.
      {"defer": True}          "let's talk about it instead". A REAL
                               answer: the user engaged and declined to
                               pick, which tells the model something that
                               closing the modal does not.
      {"options": [...],       what they chose, what they typed, or both.
       "text": "..."}

    Four affordances, all four from the spec. Options are a SelectionList
    when several may be chosen and Buttons when only one may -- a
    single-select SelectionList would let a user tick three and then be
    told only one counted, which is a worse lie than not offering it.

    UNSELECTED and EMPTY to start, like every other picker here. The
    convenient action must not be the one that answers on the user's
    behalf, and this modal exists precisely because the model does not
    know the answer.

    The screen does NOT validate. Whether a returned option was actually
    offered is core.interaction.decode's job, which is why it takes the
    request -- ProjectKindScreen made the same move for the same reason.
    """

    BINDINGS = [("escape", "dismiss_unanswered", "No answer")]

    def __init__(self, question: str, options=(), multi_select: bool = False,
                 allow_text: bool = True):
        super().__init__()
        self._question = question
        self._options = list(options)
        self._multi = bool(multi_select)
        self._allow_text = bool(allow_text)

    def compose(self) -> ComposeResult:
        widgets = [Label("The assistant has a question", id="question-title"),
                   Static(Text(self._question), id="question-body")]

        if self._options and self._multi:
            widgets.append(Static(
                "Space toggles; several may be chosen.", id="question-help"))
            widgets.append(SelectionList(
                *[Selection(Text(o), o, False) for o in self._options],
                id="question-list"))
            widgets.append(
                Button("Answer", variant="success", id="question-ok"))
        elif self._options:
            for index, option in enumerate(self._options):
                widgets.append(Button(option, variant="primary",
                                      id=f"question-opt-{index}"))

        if self._allow_text:
            widgets.append(Input(
                placeholder="…or write your own answer",
                id="question-text"))
            if not (self._options and self._multi):
                # A submit button for the typed answer. The multi-select
                # branch already has one, and the single-select branch's
                # option Buttons read the Input too -- but an open question
                # with no options at all would otherwise have no way to
                # submit anything.
                widgets.append(
                    Button("Answer", variant="success", id="question-ok"))

        # Always last, always available: the spec's escape is an answer the
        # user can give on purpose, not only a thing that happens when they
        # close the window.
        widgets.append(Button("Discuss instead", variant="default",
                              id="question-defer"))
        yield Vertical(*widgets, id="question-dialog")

    def _typed(self) -> str:
        if not self._allow_text:
            return ""
        # Read on EVERY path, like ReviewScreen's note: a user who ticks an
        # option and qualifies it in the box has given one answer, and
        # dropping half of it because they pressed the wrong button first
        # would be silent.
        try:
            return self.query_one("#question-text", Input).value.strip()
        except Exception:  # noqa: BLE001 -- absent when allow_text is False
            return ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "question-defer":
            self.dismiss({"defer": True})
            return

        chosen = []
        if button_id.startswith("question-opt-"):
            index = int(button_id.rsplit("-", 1)[1])
            chosen = [self._options[index]]
        elif self._options and self._multi:
            chosen = list(
                self.query_one("#question-list", SelectionList).selected)
        self.dismiss({"options": chosen, "text": self._typed()})

    def action_dismiss_unanswered(self) -> None:
        # None, NOT a deferral. Escape means "I am not dealing with this";
        # "Discuss instead" means "I want to talk about it". The tool says
        # different things to the model about each, so collapsing them here
        # would make the button pointless.
        self.dismiss(None)


class ReviewScreen(ModalScreen[object]):
    """Decide one proposed correction to a finished research run (§20 V4).

    Dismisses with a (decision, notes) tuple. Every dismissal path carries
    one -- escape, the buttons, and the app's shutdown release -- because
    the /research worker is blocked on a queue that must receive AN
    answer. What the answer is matters less than that it arrives:
    interaction.decode turns anything unusable (None, a bare False from
    shutdown, malformed junk) into REVIEW's declining default, so a new
    path added later fails safe -- an edit is never applied because
    nobody answered.

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
            # `[{severity}]` is a MARKUP TAG to Rich, so this title
            # rendered WITHOUT its severity on every review modal from
            # §20 until batch 42 -- `[high]` parsed as an unknown
            # style over a zero-width span. The brackets are wanted
            # here; the parsing never was.
            Label(Text(f"{kind} correction to {target}  [{severity}]"),
                  id="review-title"),
            Static(Text(body), id="review-body"),
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
            Label(Text(header), id="claims-title"),
            Static(Text(_render_claims(self._claims)),
                   id="claims-body"),
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

    `created_at` and `id` are INDEXED deliberately, not .get: a row
    without either has nothing honest to render -- storage.list_threads()
    always supplies both, and the id is the row's whole payload on
    selection. Only `preview` is optional (.get): §27 added it, so
    hand-built test rows predate it, and a missing preview degrades to an
    id-only row rather than a KeyError in a test-only construction.
    """
    created = thread["created_at"]
    stamp = (f"{created:%Y-%m-%d %H:%M}"
             if hasattr(created, "strftime") else str(created))
    preview = (thread.get("preview") or "").strip()
    return f"{stamp}  {preview}  ({thread['id']})" if preview \
        else f"{stamp}  {thread['id']}"


class ConfirmScreen(ModalScreen[bool]):
    """A titled yes/no over a block of text (ROADMAP_v2 §24).

    PermissionScreen is the wrong shape here even though it also returns a
    bool: it titles itself "Allow <tool>?" and renders params as JSON,
    because it exists to gate ONE tool call. /init's question is about a
    set of files, and the substance is the diff -- so the body is text the
    caller composed and the title says what is being decided.

    Escape declines rather than dismissing with no value. Fifth place that
    invariant applies, and here the unsafe failure is a worker parked on a
    queue nobody will answer.
    """

    BINDINGS = [("escape", "decline", "No")]

    def __init__(self, title: str, body: str, confirm_label: str = "Yes"):
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        yield Grid(
            Label(Text(self._title), id="permission-title"),
            Static(Text(self._body), id="permission-params"),
            Button(self._confirm_label, variant="success", id="allow"),
            Button("No", variant="error", id="deny"),
            id="permission-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")

    def action_decline(self) -> None:
        self.dismiss(False)


class ProjectKindScreen(ModalScreen[object]):
    """Which document set /init scaffolds (§24, I13).

    Dismisses with "software", "research", or None to cancel -- three
    answers, not a bool, because cancelling is not the same as picking the
    other one.

    An established project arrives here with a PROPOSAL and the reason for
    it, so the user is confirming a finding. A blank folder arrives with
    neither: there is nothing to infer from, and a proposal would be a
    guess wearing a finding's clothes.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, proposal=None, reason: str = "", blank: bool = False):
        super().__init__()
        self._proposal = proposal
        self._reason = reason
        self._blank = blank

    def compose(self) -> ComposeResult:
        if self._blank:
            explanation = ("This folder is empty, so there is nothing to go "
                           "on. Which kind of project is this?")
        elif self._proposal:
            explanation = (f"This looks like a {self._proposal} project — "
                           f"{self._reason}. Change it if that is wrong.")
        else:
            explanation = "Which kind of project is this?"
        yield Grid(
            Label("Set up documentation for which kind of project?",
                  id="permission-title"),
            Static(
                Text(
                    f"{explanation}\n\n"
                    "software — ARCHITECTURE, ROADMAP, DEVLOG, "
                    "TECHNICAL_DEBT, DOCUMENTATION_STANDARDS, "
                    "TEST_WRITING, BREAKING_CHANGES\n"
                    "research — RESEARCH_QUESTIONS, METHODOLOGY, "
                    "SOURCES, FINDINGS, LIMITATIONS, EXPERIMENT_LOG, "
                    "OPEN_QUESTIONS, DOCUMENTATION_STANDARDS"),
                id="permission-params"),
            Button("Software", variant="success", id="kind-software"),
            Button("Research", variant="primary", id="kind-research"),
            id="permission-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("research" if event.button.id == "kind-research"
                     else "software")

    def action_cancel(self) -> None:
        self.dismiss(None)


class ThreadPickerScreen(ModalScreen[object]):
    """Pick a thread to resume. Dismisses with a UUID, or None to cancel.

    Reads storage.list_threads(), which since §27 returns CONVERSATIONS only
    -- a research run creates ~15 threads of machinery and every automatic
    compaction one more, and all of them used to be offered here as things
    to resume.

    A row leads with the thread's first user message (§27), because a list
    of uuids and timestamps was still unusable once it was short: filtering
    made the picker correct, and the preview is what makes it answerable.
    The id stays, since it is what --thread and /resume take.

    `note` carries one line shown under the title when the list is not the
    whole story (#30/#32): the picker caps at PICKER_LIMIT rows, and a cap
    that does not say so reads as "this is everything" (M14's rule). Older
    conversations remain reachable by id via /resume.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, threads: list[dict], note: str = ""):
        super().__init__()
        self._threads = threads
        self._note = note

    def compose(self) -> ComposeResult:
        items = [ListItem(Label(Text(_thread_row(t))))
                 for t in self._threads]
        children = [Label("Resume which thread?", id="thread-title")]
        if self._note:
            children.append(Static(Text(self._note),
                                   id="thread-note"))
        if items:
            children.append(ListView(*items, id="thread-list"))
        else:
            children.append(Static("No saved threads yet.", id="thread-empty"))
        yield Vertical(*children, id="thread-dialog")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._threads):
            self.dismiss(self._threads[index]["id"])
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
