"""
project_init/generator.py

ROADMAP_v2 §24: the one place that decides what `/init` does.

BOTH SHELLS CALL THIS. §26's L6 and §27's T5 again: a per-shell
implementation is how the CLI and the TUI come to disagree about whether an
existing document is overwritten, which is a policy question and not a
painting one. The shells supply consent and rendering as callbacks; every
decision about what happens lives here.

THE AGENT PRODUCES TEXT; THIS MODULE WRITES (I8). The initializer gets
`read_project_doc` and nothing else, and returns the hub document's prose as its
final answer. Diffing, asking and dispatching `write_project_doc` happen
here, because consent belongs to the shell (§20's V3) and a tool writing
from inside the loop could not show a human a diff first.

ONE CONSENT, A NAMED LIST. `write_project_doc` is approval-gated, so a naive
implementation would prompt eight times for one command -- which is the
shape of consent that gets clicked through. The user is asked once, shown
the hub's diff and the exact list of files that will be created, and
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
from project_init import (config_files, doc_sets,
                          manifest as manifest_mod)

logger = logging.getLogger(__name__)


class InitError(Exception):
    """Something /init cannot proceed past. Carries a message meant for a
    human, not a traceback; both shells print it verbatim.

    It may be several lines. A write that fails partway through has to say
    what it already did (#95), and one line cannot carry "this failed" and
    "these eight files exist now" without losing one of them.
    """


def propose_kind(project_path: str) -> tuple:
    """(kind, reason) for an established project (I13).

    A PYTHON HEURISTIC, NOT A MODEL CALL, and confirmed by the user either
    way. A code manifest is a strong, checkable signal; the alternative --
    an extra round trip so a model can read the same filename this function
    reads -- spends a call to reach the same place with less certainty
    about how it got there. The reason is returned so the confirmation
    prompt can show its working.

    IT BRANCHES ON THE MANIFEST SET, NOT ON THE STACK (#96). Those are two
    questions -- "is this a codebase" and "what is it written in" -- and
    answering the first with the second is what made a Java, Ruby, Gradle,
    C or container project `research` with "no code manifests found"
    printed as the reason, while the manifest shown to the agent listed the
    manifest. `Makefile` and `Dockerfile` still carry no stack, which is
    the point: they answer the first question and not the second.

    The reason now names the files it found, because I13 asked for working
    that can be shown and a filename is the whole of the evidence.
    """
    facts = manifest_mod.detect_facts(project_path)
    found = facts.get("code_manifests")
    if not found:
        return (doc_sets.RESEARCH,
                "no code manifests found, so this looks like written work "
                "rather than a codebase")
    named = ", ".join(found)
    if facts.get("stack"):
        return (doc_sets.SOFTWARE,
                f"{facts['stack']} project files are present ({named})")
    return doc_sets.SOFTWARE, f"code manifests are present ({named})"


def _existing_documents(project_path: str, kind: str) -> set:
    """Which documents in the set the project ALREADY has (I12)."""
    from tools.builtin.project_docs import doc_path
    return {name for name in doc_sets.document_set(kind)
            if os.path.exists(doc_path(project_path, name))}


def _config_plan(project_path: str) -> dict:
    """What `--config` would create here: `{"files", "dirs", "existing"}`.

    AN EXISTING FILE IS NEVER OVERWRITTEN (I12, applied to the file it was
    written for). The document rule -- only the hub is diffed and asked
    about, because it is the only file /init claims to author -- reads
    even harder here: settings.json and mcp.json are configuration
    somebody tuned, and a scaffold of defaults landing on top of a tuned
    file would be this command destroying the thing it exists to help you
    start. Left alone and reported, per file, so a project with a
    settings.json and no mcp.json still gets the mcp.json.

    Directories are listed separately because creating one is not writing
    anything: they carry nothing into the D17 hash (`content_files()`
    walks files) and git does not track an empty directory, so they are a
    hint on this machine and never part of what a clone receives.
    """
    root = workspace_trust.venastine_dir(project_path)
    names = (config_files.SETTINGS_FILENAME, config_files.MCP_FILENAME)
    return {
        "files": [name for name in names
                  if not os.path.exists(os.path.join(root, name))],
        "existing": [name for name in names
                     if os.path.exists(os.path.join(root, name))],
        "dirs": [name for name in config_files.TIER_DIRS
                 if not os.path.isdir(os.path.join(root, name))],
    }


def _write_config(project_path: str, name: str) -> None:
    """One configuration file, written DIRECTLY (I14).

    Not through `write_project_doc`, which denies `.venastine/` by path
    segment and names these two files as the reason. Extending that
    allowlist was the alternative and is rejected in the record: it would
    advertise, to every run and all ten research passes, a tool that can
    author a file naming a local command to execute. What replaces the
    tool's approval gate is the same single consent that already covers
    the document set -- the shape `_settle_trust` already uses to write
    the trust store from here.

    OSError becomes InitError, so a full disk or a read-only mount takes
    the same reporting path as a failed document write (#95) rather than
    escaping as a traceback.
    """
    from json_store import write_json_atomic

    payload = (config_files.render_settings()
               if name == config_files.SETTINGS_FILENAME
               else config_files.render_mcp())
    path = os.path.join(workspace_trust.venastine_dir(project_path), name)
    try:
        write_json_atomic(path, payload, trailing_newline=True)
    except OSError as e:
        raise InitError(f"Could not write {path}: {e}")


def _shadow_lines() -> list:
    """What the scaffolded settings.json takes over from the user's own.

    A project settings.json beats the user's (D29) by PRESENCE and not by
    difference, so even a file of pure defaults starts deciding every key
    it names. Said BEFORE the consent, where it can still change the
    answer, rather than in the report afterwards.
    """
    shadowed = config_files.shadowed(config_loader.user_settings())
    if not shadowed:
        return []
    lines = ["This project's settings.json will now decide "
             f"{len(shadowed)} key{'' if len(shadowed) == 1 else 's'} your "
             f"own settings.json was deciding:"]
    lines += [f"  {path}: {theirs!r} -> {ours!r}"
              for path, theirs, ours in shadowed]
    lines.append("Delete those lines from the new file to go back to "
                 "yours.")
    return lines


def _read_existing_context(project_path: str) -> Optional[str]:
    from tools.builtin.project_docs import HUB_FILENAME, doc_path
    path = doc_path(project_path, HUB_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        # Degrade like config_loader does with an unreadable hub document.
        # An undecodable existing file must not stop /init from writing a
        # good one -- but it is also not something to revise, so the run
        # proceeds as if there were none.
        logger.warning("Could not read the existing %s at %s; "
                       "generating a fresh one: %s", HUB_FILENAME, path, e)
        return None


def _task(manifest_text: str, existing: Optional[str], kind: str) -> str:
    """The user message: the manifest, plus the current file if there is
    one (I4). Both go HERE and not in the system prompt -- see
    project_init/manifest.py on why that distinction is load-bearing.

    THE HUB'S NAME IS INTERPOLATED, NEVER SPELLED (§44 WS8). This function
    said "Write CONTEXT.md" for two batches after the hub became the root
    AGENTS.md: the shell wrote to the right path, and the model drafted a
    document that called itself by the old name. Reading the constant means
    the next relocation cannot leave the prompt behind again.
    """
    from tools.builtin.project_docs import HUB_FILENAME

    parts = [
        f"Write {HUB_FILENAME} for this {kind} project.",
        "",
        manifest_text,
    ]
    if existing:
        parts += [
            f"## The current {HUB_FILENAME}",
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
            f"there is nothing to generate the hub document with.")

    context = manager.active_context(agent)
    system_prompt = manager.system_prompt_for(
        agent, DEFAULT_SYSTEM_PROMPT, context=context)

    response = RunAgentLoop.run_agent_conversation(
        user_goal=_task(manifest_mod.build_manifest(project_path),
                        existing, kind),
        model=agent.model or model,
        provider_name=agent.provider or provider_name,
        max_steps=agent.max_steps or config.INIT_MAX_STEPS,
        # #4: no separate /init ceiling any more -- the wrapper resolves the
        # configured spend cap, same as every other path.
        context=context,
        system_prompt=system_prompt,
        # §27: the initializer is a SIXTH thread source. Left unlabelled it
        # clutters the picker with a thread nobody will ever resume.
        thread_kind=THREAD_KIND_SUBAGENT,
    )
    body = (response.text or "").strip()
    if not body:
        raise InitError(
            "The initializer returned nothing, so there is no hub document "
            "to write. Nothing has been changed.")
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
    scaffold_docs: bool = True,
    scaffold_config: bool = False,
) -> dict:
    """Scaffold this project's documentation set, its configuration, or both.

    confirm(summary) -> bool   the one consent (I5). None means there is no
                               way to ask, and therefore nothing is written.
    notify(text) -> None       progress and the diff, rendered by the shell.
    choose_kind(proposal, reason, blank) -> str | None
                               resolves the project kind. Called with
                               blank=True when there is nothing to infer
                               from (I13), in which case `proposal` is None.
    scaffold_docs              the AGENTS.md hub and the stubs it links.
    scaffold_config            `.venastine/settings.json`, `mcp.json` and
                               the two tier directories (§24 I17).

    TWO FLAGS RATHER THAN TWO FUNCTIONS. `--config` on its own is a
    different OPERATION -- no kind question, no initializer, no spend --
    but it is not a different set of decisions, and the decisions are
    what this module exists to hold in one place. A config-only entry
    point beside this one would be a second answer to "is an existing
    file overwritten" and a second place to forget the trust settle.

    Returns a notice dict in the {"kind", "text"} shape both shells render.
    """
    from tools.builtin.project_docs import HUB_FILENAME, doc_path
    from tools.registry import registry, ToolCallDenied

    root = project_path or config_loader.get_project_path()
    if root is None:
        raise InitError("No project directory has been resolved.")
    root = os.path.realpath(root)
    say = notify or (lambda _text: None)
    if not scaffold_docs and not scaffold_config:
        raise InitError("/init was asked to scaffold nothing.")

    body = None
    diff = ""
    to_create = []
    already_there = set()
    context_path = doc_path(root, HUB_FILENAME)

    # ---- 1. Which set --------------------------------------------------
    # Only when there is a set. `--config` alone asks nothing and spends
    # nothing, which is the whole of why it is a separate flag rather
    # than an addition to a run that always costs a model call.
    if scaffold_docs:
        if kind is None:
            blank = manifest_mod.is_blank(root)
            proposal, reason = (None, "") if blank else propose_kind(root)
            if choose_kind is None:
                # No way to ask. Refusing beats scaffolding seven files of
                # the wrong kind into someone's project -- V6's rule, and
                # the wrong answer here is expensive to undo by hand.
                raise InitError(
                    "The project type could not be determined and there is "
                    "no way to ask. Re-run with --software or --research.")
            kind = choose_kind(proposal, reason, blank)
            if kind is None:
                return {"kind": "init",
                        "text": "Cancelled — nothing was written."}
        if kind not in doc_sets.PROJECT_KINDS:
            raise InitError(f"Unknown project type {kind!r}; expected one of "
                            f"{', '.join(doc_sets.PROJECT_KINDS)}.")

        # ---- 2. Generate -----------------------------------------------
        existing_context = _read_existing_context(root)
        say(f"Reading the project and drafting {HUB_FILENAME}…")
        body = _run_initializer(root, kind, existing_context, model,
                                provider_name)

        already_there = _existing_documents(root, kind)
        body = f"{body.rstrip()}\n\n{doc_sets.render_index(kind)}"

        to_create = [name for name in doc_sets.document_set(kind)
                     if name not in already_there]
        diff = build_diff(existing_context, body, context_path)

    # ---- 3. Show, then ask once ----------------------------------------
    plan = _config_plan(root) if scaffold_config else {"files": [],
                                                       "existing": [],
                                                       "dirs": []}
    if not diff and not to_create and not plan["files"] and not plan["dirs"]:
        return {"kind": "init", "text": _nothing_to_do(kind, scaffold_docs,
                                                       scaffold_config)}

    if scaffold_docs:
        if diff:
            say(diff)
        else:
            say(f"{HUB_FILENAME} is unchanged.")
        if to_create:
            say("Will also create, as stubs: " + ", ".join(to_create))
        if already_there:
            say("Leaving existing documents untouched: "
                + ", ".join(sorted(already_there)))
    if plan["files"]:
        say("Will create, with default values: "
            + ", ".join(".venastine/" + name for name in plan["files"]))
        if config_files.SETTINGS_FILENAME in plan["files"]:
            for line in _shadow_lines():
                say(line)
    if plan["existing"]:
        say("Leaving existing configuration untouched: "
            + ", ".join(".venastine/" + name for name in plan["existing"]))

    if confirm is None:
        # §25's V6, and the same rule §20 applies to review corrections: the
        # inability to ask is not permission to proceed. A piped `--init`
        # reports what it would do and writes nothing.
        return {"kind": "init",
                "text": "Nothing was written: there is no way to confirm "
                        "these changes here. Re-run on a terminal."}
    if not confirm(_consent_summary(context_path, to_create, bool(diff),
                                    plan)):
        return {"kind": "init", "text": "Declined — nothing was written."}

    # ---- 4. Write ------------------------------------------------------
    # Captured BEFORE the write (I6). Afterwards the content hash has moved
    # and is_trusted() answers False for a project that WAS trusted, which
    # is exactly the state this distinction exists to tell apart.
    was_trusted = workspace_trust.is_trusted(root)

    granted = lambda _name, _params: True  # noqa: E731 -- consent is above
    written = []
    made = []

    failure = None
    try:
        if scaffold_docs:
            facts = manifest_mod.detect_facts(root)
            if diff:
                _write(registry, granted, HUB_FILENAME, body)
                written.append(HUB_FILENAME)
            for name in to_create:
                _write(registry, granted, name,
                       doc_sets.render_stub(name, facts))
                written.append(name)
        # The directories first, so a failure to write settings.json still
        # leaves a `.venastine/` whose shape says where the rest goes.
        for name in plan["dirs"]:
            os.makedirs(
                os.path.join(workspace_trust.venastine_dir(root), name),
                exist_ok=True)
            made.append(".venastine/" + name + "/")
        for name in plan["files"]:
            _write_config(root, name)
            written.append(".venastine/" + name)
    except OSError as e:
        failure = f"Could not create a directory under .venastine/: {e}"
    except ToolCallDenied as e:
        failure = f"The write was denied: {e}"
    except InitError as e:
        # THE REACHABLE FAILURE, and the one that had no handler (#95).
        # `ToolCallDenied` cannot arrive here -- consent is granted a few
        # lines up -- while `_write` raises `InitError` for every error
        # dict `write_run` returns, and `write_run` turns ANY OSError into
        # one. Disk full, permissions, a read-only mount: all of them took
        # this path, and this path did not exist.
        failure = str(e)

    # ---- 5. Trust ------------------------------------------------------
    # ON BOTH PATHS (#95). The partial write has already moved the D17
    # content hash, so a project the user trusted a moment ago is untrusted
    # now -- exactly the state I6 exists to prevent, reached by the route
    # I6 was not looking at. Its precondition is unchanged by the failure:
    # they just authored this content, and that is as true after five of
    # eight writes as after eight. Skipping this made the failure wrong
    # about two things instead of one, and silent about the second.
    trust_note = _settle_trust(root, was_trusted)

    if failure is not None:
        # NAME WHAT WAS CREATED. The success path reports the files so the
        # user knows what changed; the failure path reported the single
        # file that did NOT change -- the one thing they could already see
        # -- and discarded the list of the ones that did. "Could not write
        # TECHNICAL_DEBT.md" left no way to tell whether nothing had
        # happened or most of it had.
        #
        # NOTHING IS ROLLED BACK, deliberately. The hub's previous
        # content was overwritten at the first step and cannot be restored,
        # so "rolled back" would be true of the stubs and false of the one
        # file that mattered -- and deleting documents out of someone's
        # project on an error path is a worse failure than the one being
        # reported.
        lines = [failure]
        if written:
            lines.append(f"Wrote {len(written)} file"
                         f"{'' if len(written) == 1 else 's'} before that: "
                         + ", ".join(written))
        else:
            lines.append("Nothing was written.")
        lines.append(trust_note)
        raise InitError("\n".join(line for line in lines if line))

    lines = [f"Wrote {len(written)} file{'' if len(written) == 1 else 's'}: "
             + ", ".join(written)]
    if made:
        lines.append("Created: " + ", ".join(made))
    if already_there:
        lines.append("Left alone: " + ", ".join(sorted(already_there)))
    if plan["existing"]:
        lines.append("Left alone: "
                     + ", ".join(".venastine/" + name
                                 for name in plan["existing"]))
    lines.append(trust_note)
    return {"kind": "init", "text": "\n".join(line for line in lines if line)}


def _nothing_to_do(kind, scaffold_docs: bool, scaffold_config: bool) -> str:
    """The message for a run that found everything already in place.

    Assembled from the halves that actually ran, so a `--config` run in a
    configured project does not report on a document set it never looked
    at -- which is the same complaint #95 makes about a failure naming
    the one file that did NOT change.
    """
    from tools.builtin.project_docs import HUB_FILENAME

    parts = []
    if scaffold_docs:
        parts.append(f"{HUB_FILENAME} is already up to date and every "
                     f"document in the {kind} set exists")
    if scaffold_config:
        parts.append(".venastine/ already has its settings.json and "
                     "mcp.json")
    return " and ".join(parts) + ". Nothing to do."


def _consent_summary(context_path: str, to_create: list, changing: bool,
                     plan: Optional[dict] = None) -> str:
    """ONE consent covering everything the run will touch (I5).

    The configuration files are named in it rather than asked about
    separately: eight prompts for one command is the shape of consent that
    gets clicked through, which is the reason this summary exists at all.
    """
    plan = plan or {"files": [], "dirs": []}
    parts = []
    if changing:
        parts.append(f"write {context_path}")
    if to_create:
        parts.append(f"create {len(to_create)} stub document"
                     f"{'' if len(to_create) == 1 else 's'} "
                     f"({', '.join(to_create)})")
    if plan["files"]:
        parts.append("create " + ", ".join(".venastine/" + name
                                           for name in plan["files"])
                     + " with default values")
    if plan["dirs"]:
        parts.append("create " + ", ".join(".venastine/" + name + "/"
                                           for name in plan["dirs"]))
    return "/init will " + " and ".join(parts) + ". Proceed?"


def _write(registry, granted, name: str, content: str) -> None:
    result = registry.dispatch(
        "write_project_doc", {"name": name, "content": content},
        approval_callback=granted)
    if isinstance(result, dict) and result.get("error"):
        raise InitError(result["error"])


def _settle_trust(project_path: str, was_trusted: bool) -> str:
    """I6. Re-grant only for a project that was ALREADY trusted.

    Writing the hub changes the D17 content hash, so a project the user
    had approved becomes untrusted purely because they ran this command --
    the trust system working as designed, producing a bad outcome. Since
    they just authored the content, re-granting is unambiguous.

    But ONLY then. In an untrusted project, `.venastine/` may already hold
    agents, skills and an mcp.json that arrived with a repo someone cloned.
    Granting trust as a side effect of /init would launder all of it in --
    a hole in D17 opened from a direction the trust prompt never sees.
    The hub is never special-cased out of the hash either; that would be
    the same hole with a smaller mouth.
    """
    if not was_trusted:
        return ("This project is not trusted, so nothing here has changed "
                "that. You will be asked about its .venastine/ content on "
                "the next launch.")
    workspace_trust.grant_trust(project_path)
    return ("Workspace trust re-granted for the updated .venastine/ content, "
            "so the next launch will not re-ask.")
