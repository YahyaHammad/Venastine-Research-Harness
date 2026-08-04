"""
core/reasoning/authorization.py

ROADMAP_v2 §25. The pipeline's POLICY about pre-flight grants -- which
tools may be offered, and how a user's answer is parsed.

core/approval.py holds the data shapes and core/loop.py enforces them.
This file answers the two questions that are specific to the research
pipeline, and both shells (main.py's flag, tui/app.py's picker) consume it
so the CLI and the TUI cannot drift into offering different sets.

Why the pipeline needs its own answer at all: it is the harness's highest
prompt-injection surface by construction. It fetches web pages and feeds
them to a model that then chooses tools, unattended, across ten passes.
§23 reasoned the same way when it decided the question tool denies
headless -- "an injected question from fetched web content would be least
noticed" there.
"""

import logging

logger = logging.getLogger(__name__)


# Tools never offered for a pipeline grant, whatever the registry says
# about their grantability (§25 R4).
#
# spawn_subagent is grantable in the ordinary sense -- it has no
# approval_check, so approving the NAME is meaningful consent. It is
# excluded here because of what that consent then authorises: approving a
# spawn IS the §18 subagent sign-off, handing the child its whole gated
# set. Pre-granting it unattended compounds one launch-time yes into
# unbounded delegated authority across ten passes, with the depth limit as
# the only remaining bound.
#
# Nothing is lost by excluding it: research passes have never been able to
# delegate. The TUI chat path, where a human is watching and answers per
# turn, is unaffected.
PIPELINE_UNGRANTABLE = frozenset({"spawn_subagent"})


# "The flag was given with no value -- ask me which." A distinct object
# rather than "" so an explicitly empty list (--grant-tools "") stays an
# empty GRANT rather than becoming a prompt. Defined here, not in either
# shell, because both compare against it and two sentinels would compare
# unequal while looking identical.
GRANT_PICKER = object()


def candidates(context=None) -> list[tuple[str, str]]:
    """(name, description) for every tool a pipeline grant could cover.

    Three filters, and the composition is the point:
      * advertised and approval-gated -- i.e. exactly what the headless
        rule hides today. Offering anything else would be offering a grant
        for a tool that never needed one.
      * registry.grantable -- a param-dependent gate was never consented
        to by name (R2).
      * not PIPELINE_UNGRANTABLE -- this file's own policy (R4).

    Descriptions come from the tool's own schema because they are the only
    signal available: MCP exposes no read-only/write metadata, so nothing
    can tell the user that one granted tool searches a library and another
    posts to a channel. Showing what a tool says it does is the whole of
    informed consent here, which is also why the grant is per-tool (R1)
    rather than one yes covering the set.
    """
    from tools.registry import registry

    out = []
    for name in sorted(registry.headless_hidden(context)):
        if not registry.grantable(name) or name in PIPELINE_UNGRANTABLE:
            continue
        spec = registry._tools.get(name)
        description = ""
        if spec is not None:
            description = (spec.schema or {}).get("description", "") or ""
        out.append((name, description.strip().splitlines()[0]
                    if description.strip() else ""))
    return out


class GrantSpecError(ValueError):
    """A --grant-tools value naming something that cannot be granted."""


def parse_grant_spec(text: str, offered) -> set:
    """Turn a comma-separated --grant-tools value into a set of names.

    RAISES on an unknown name rather than dropping it. A typo'd tool would
    otherwise produce an empty or partial grant, the run would proceed
    looking authorised, and the pass would report that a tool it was
    supposed to have was unavailable -- with the actual cause three layers
    away. The same argument D24 settled for undeclared permissions: an
    authorization mistake must be loud at the point it is made.

    `offered` is candidates()' output (or any iterable of names), so what a
    flag may name and what a picker may show are the same set by
    construction.
    """
    allowed = {n if isinstance(n, str) else n[0] for n in offered}
    names = {part.strip() for part in (text or "").split(",") if part.strip()}
    unknown = sorted(names - allowed)
    if unknown:
        raise GrantSpecError(
            f"cannot grant {', '.join(unknown)}: "
            + (f"grantable tools are {', '.join(sorted(allowed))}"
               if allowed else
               "no tool in this run needs a grant, so there is nothing to "
               "authorise")
        )
    return names
