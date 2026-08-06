"""
project_init/generator.py

ROADMAP_v2 §24: the one place that decides what `/init` does.

BOTH SHELLS CALL THIS. §26's L6 and §27's T5 again: a per-shell
implementation is how the CLI and the TUI come to disagree about whether an
existing document is overwritten, which is a policy question and not a
painting one. The shells supply consent and rendering as callbacks; every
decision about what happens lives here.

THE AGENT PRODUCES TEXT; THIS MODULE WRITES (I8). The initializer gets
`read_project_doc` and nothing else, and returns CONTEXT.md's prose as its
final answer. Diffing, asking and dispatching `write_project_doc` happen
here, because consent belongs to the shell (§20's V3) and a tool writing
from inside the loop could not show a human a diff first.

ONE CONSENT, A NAMED LIST. `write_project_doc` is approval-gated, so a naive
implementation would prompt eight times for one command -- which is the
shape of consent that gets clicked through. The user is asked once, shown
the CONTEXT.md diff and the exact list of files that will be created, and
that answer is then passed as `approval_callback` to each dispatch. Same
move core/loop.py makes after a channel approval, and for the same reason.
"""

from __future__ import annotations

import difflib
import logging
import os
from typing import Callable, Optional

import config
from core import config_loader, workspace_trust
from project_init import doc_sets, manifest as manifest_mod

logger = logging.getLogger(__name__)


class InitError(Exception):
    """Something /init cannot proceed past. Carries a message meant for a
    human, not a traceback -- both shells print it as one line."""


def propose_kind(project_path: str) -> tuple:
    """(kind, reason) for an established project (I13).

    A PYTHON HEURISTIC, NOT A MODEL CALL, and confirmed by the user either
    way. A code manifest is a strong, checkable signal; the alternative --
    an extra round trip so a model can read the same filename this function
    reads -- spends a call to reach the same place with less certainty
    about how it got there. The reason is returned so the confirmation
    prompt can show its working.
    """
    facts = manifest_mod.detect_facts(project_path)
    if facts.get("stack"):
        return doc_sets.SOFTWARE, f"{facts['stack']} project files are present"
    return (doc_sets.RESEARCH,
            "no code manifests found, so this looks like written work "
            "rather than a codebase")


def _existing_documents(project_path: str, kind: str) -> set:
    """Which documents in the set the project ALREADY has (I12)."""
    from tools.builtin.project_docs import doc_path
    return {name for name in doc_sets.document_set(kind)
            if os.path.exists(doc_path(project_path, name))}


def _read_existing_context(project_path: str) -> Optional[str]:
    from tools.builtin.project_docs import CONTEXT_FILENAME, doc_path
    path = doc_path(project_path, CONTEXT_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        # Degrade like config_loader does with an unreadable CONTEXT.md.
        # An undecodable existing file must not stop /init from writing a
        # good one -- but it is also not something to revise, so the run
        # proceeds as if there were none.
        logger.warning("Could not read the existing CONTEXT.md at %s; "
                       "generating a fresh one: %s", path, e)
        return None


def _task(manifest_text: str, existing: Optional[str], kind: str) -> str:
    """The user message: the manifest, plus the current file if there is
    one (I4). Both go HERE and not in the system prompt -- see
    project_init/manifest.py on why that distinction is load-bearing."""
    parts = [
        f"Write CONTEXT.md for this {kind} project.",
        "",
        manifest_text,
    ]
    if existing:
        parts += [
            "## The current CONTEXT.md",
            "",
            "This already exists and may contain hand edits. Revise it: keep "
            "everything still true, correct what the project has outgrown, "
            "add what is missing. Do not reword something merely to phrase "
            "it your way.",
            "",
            existing,
            "",
        ]
    return "\n".join(parts)


def _run_initializer(project_path: str, kind: str, existing: Optional[str],
                     model: str, provider_name: str) -> str:
    from agents.manager import manager
    from core.loop import RunAgentLoop, DEFAULT_SYSTEM_PROMPT
    from storage import THREAD_KIND_SUBAGENT

    agent = manager.get(config.INITIALIZER_AGENT)
    if agent is None:
        raise InitError(
            f"The {config.INITIALIZER_AGENT!r} agent is not available, so "
            f"there is nothing to generate CONTEXT.md with.")

    context = manager.active_context(agent)
    system_prompt = manager.system_prompt_for(
        agent, DEFAULT_SYSTEM_PROMPT, context=context)

    response = RunAgentLoop.run_agent_conversation(
        user_goal=_task(manifest_mod.build_manifest(project_path),
                        existing, kind),
        model=agent.model or model,
        provider_name=agent.provider or provider_name,
        max_steps=agent.max_steps or config.INIT_MAX_STEPS,
        # §26's reasoning, second instance: a tool-heavy run on the chat
        # budget is cut off by a meter that re-counts the whole prompt every
        # step, long before its context is a problem.
        max_total_tokens=config.INIT_TOKEN_BUDGET,
        context=context,
        system_prompt=system_prompt,
        # §27: the initializer is a SIXTH thread source. Left unlabelled it
        # clutters the picker with a thread nobody will ever resume.
        thread_kind=THREAD_KIND_SUBAGENT,
    )
    body = (response.text or "").strip()
    if not body:
        raise InitError(
            "The initializer returned nothing, so there is no CONTEXT.md to "
            "write. Nothing has been changed.")
    return body


def build_diff(old: Optional[str], new: str, path: str) -> str:
    """Unified diff of what /init would change (I5). Empty string when the
    file would be identical, which the caller reports rather than writing."""
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    label = os.path.basename(path)
    return "".join(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{label} (current)" if old else "/dev/null",
        tofile=f"{label} (proposed)"))


def generate(
    project_path: Optional[str] = None,
    model: Optional[str] = None,
    provider_name: Optional[str] = None,
    *,
    kind: Optional[str] = None,
    confirm: Optional[Callable] = None,
    notify: Optional[Callable] = None,
    choose_kind: Optional[Callable] = None,
) -> dict:
    """Scaffold this project's documentation set.

    confirm(summary) -> bool   the one consent (I5). None means there is no
                               way to ask, and therefore nothing is written.
    notify(text) -> None       progress and the diff, rendered by the shell.
    choose_kind(proposal, reason, blank) -> str | None
                               resolves the project kind. Called with
                               blank=True when there is nothing to infer
                               from (I13), in which case `proposal` is None.

    Returns a notice dict in the {"kind", "text"} shape both shells render.
    """
    from tools.builtin.project_docs import CONTEXT_FILENAME, doc_path
    from tools.registry import registry, ToolCallDenied

    root = project_path or config_loader.get_project_path()
    if root is None:
        raise InitError("No project directory has been resolved.")
    root = os.path.realpath(root)
    say = notify or (lambda _text: None)

    # ---- 1. Which set --------------------------------------------------
    if kind is None:
        blank = manifest_mod.is_blank(root)
        proposal, reason = (None, "") if blank else propose_kind(root)
        if choose_kind is None:
            # No way to ask. Refusing beats scaffolding seven files of the
            # wrong kind into someone's project -- V6's rule, and the wrong
            # answer here is expensive to undo by hand.
            raise InitError(
                "The project type could not be determined and there is no "
                "way to ask. Re-run with --software or --research.")
        kind = choose_kind(proposal, reason, blank)
        if kind is None:
            return {"kind": "init", "text": "Cancelled — nothing was written."}
    if kind not in doc_sets.PROJECT_KINDS:
        raise InitError(f"Unknown project type {kind!r}; expected one of "
                        f"{', '.join(doc_sets.PROJECT_KINDS)}.")

    # ---- 2. Generate ---------------------------------------------------
    existing_context = _read_existing_context(root)
    say(f"Reading the project and drafting {CONTEXT_FILENAME}…")
    body = _run_initializer(root, kind, existing_context, model, provider_name)

    already_there = _existing_documents(root, kind)
    body = f"{body.rstrip()}\n\n{doc_sets.render_index(kind)}"

    to_create = [name for name in doc_sets.document_set(kind)
                 if name not in already_there]

    # ---- 3. Show, then ask once ----------------------------------------
    context_path = doc_path(root, CONTEXT_FILENAME)
    diff = build_diff(existing_context, body, context_path)
    if not diff and not to_create:
        return {"kind": "init",
                "text": f"{CONTEXT_FILENAME} is already up to date and every "
                        f"document in the {kind} set exists. Nothing to do."}

    if diff:
        say(diff)
    else:
        say(f"{CONTEXT_FILENAME} is unchanged.")
    if to_create:
        say("Will also create, as stubs: " + ", ".join(to_create))
    if already_there:
        say("Leaving existing documents untouched: "
            + ", ".join(sorted(already_there)))

    if confirm is None:
        # §25's V6, and the same rule §20 applies to review corrections: the
        # inability to ask is not permission to proceed. A piped `--init`
        # reports what it would do and writes nothing.
        return {"kind": "init",
                "text": "Nothing was written: there is no way to confirm "
                        "these changes here. Re-run on a terminal."}
    if not confirm(_consent_summary(context_path, to_create, bool(diff))):
        return {"kind": "init", "text": "Declined — nothing was written."}

    # ---- 4. Write ------------------------------------------------------
    # Captured BEFORE the write (I6). Afterwards the content hash has moved
    # and is_trusted() answers False for a project that WAS trusted, which
    # is exactly the state this distinction exists to tell apart.
    was_trusted = workspace_trust.is_trusted(root)

    granted = lambda _name, _params: True  # noqa: E731 -- consent is above
    written = []
    facts = manifest_mod.detect_facts(root)

    try:
        if diff:
            _write(registry, granted, CONTEXT_FILENAME, body)
            written.append(CONTEXT_FILENAME)
        for name in to_create:
            _write(registry, granted, name, doc_sets.render_stub(name, facts))
            written.append(name)
    except ToolCallDenied as e:
        raise InitError(f"The write was denied: {e}")

    # ---- 5. Trust ------------------------------------------------------
    trust_note = _settle_trust(root, was_trusted)

    lines = [f"Wrote {len(written)} file{'' if len(written) == 1 else 's'}: "
             + ", ".join(written)]
    if already_there:
        lines.append("Left alone: " + ", ".join(sorted(already_there)))
    lines.append(trust_note)
    return {"kind": "init", "text": "\n".join(line for line in lines if line)}


def _consent_summary(context_path: str, to_create: list, changing: bool) -> str:
    parts = []
    if changing:
        parts.append(f"write {context_path}")
    if to_create:
        parts.append(f"create {len(to_create)} stub document"
                     f"{'' if len(to_create) == 1 else 's'} "
                     f"({', '.join(to_create)})")
    return "/init will " + " and ".join(parts) + ". Proceed?"


def _write(registry, granted, name: str, content: str) -> None:
    result = registry.dispatch(
        "write_project_doc", {"name": name, "content": content},
        approval_callback=granted)
    if isinstance(result, dict) and result.get("error"):
        raise InitError(result["error"])


def _settle_trust(project_path: str, was_trusted: bool) -> str:
    """I6. Re-grant only for a project that was ALREADY trusted.

    Writing CONTEXT.md changes the D17 content hash, so a project the user
    had approved becomes untrusted purely because they ran this command --
    the trust system working as designed, producing a bad outcome. Since
    they just authored the content, re-granting is unambiguous.

    But ONLY then. In an untrusted project, `.venastine/` may already hold
    agents, skills and an mcp.json that arrived with a repo someone cloned.
    Granting trust as a side effect of /init would launder all of it in --
    a hole in D17 opened from a direction the trust prompt never sees.
    CONTEXT.md is never special-cased out of the hash either; that would be
    the same hole with a smaller mouth.
    """
    if not was_trusted:
        return ("This project is not trusted, so nothing here has changed "
                "that. You will be asked about its .venastine/ content on "
                "the next launch.")
    workspace_trust.grant_trust(project_path)
    return ("Workspace trust re-granted for the updated .venastine/ content, "
            "so the next launch will not re-ask.")
