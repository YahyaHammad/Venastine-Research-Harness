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


# What both shells say when nothing in this run can be granted (#106).
#
# One string, two callers, for the same reason the module exists: they
# drifted, and the TUI's version was FALSE where it fired. It said "no
# approval-gated tool is AVAILABLE to this run" when two were -- they
# simply could not be pre-granted, which is a different sentence and the
# only one either shell is in a position to make.
#
# This module already kept the two from offering different SETS. What it
# had never been given is what they say about an empty one.
NOTHING_TO_GRANT = (
    "No approval-gated tool in this run can be granted up front, so there "
    "is nothing to authorise. Gated tools are still asked about per call "
    "wherever something can answer."
)


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
      * grant_policy is GRANT_ANYWHERE (R13). This is the STRICT half of
        the pair: a GRANT_SIGNOFF_ONLY tool may be pre-granted by a human
        answering about one named agent for one turn, and may not be
        pre-granted to ten unattended passes on one launch-time tick.

    That third filter used to be a frozenset in this module, which is
    exactly how #67 and #133 happened: agents/manager.py could not see it,
    so the §18 sign-off and this function held different ideas of what a
    grant covers, and a tool registered after the set was written
    (write_project_doc, §24) joined the picker without anyone being asked.
    The answer now lives on the tool and both callers read it.

    On a default install this returns an EMPTY list, and that is correct
    rather than a regression: every built-in is either ungated, param-
    dependent, or excluded by policy. The population this exists for is
    MCP tools -- allowed but approval-gated by default (§17 decision D),
    no approval_check, and named at connection time.

    Descriptions come from the tool's own schema because they are the only
    signal available: MCP exposes no read-only/write metadata, so nothing
    can tell the user that one granted tool searches a library and another
    posts to a channel. Showing what a tool says it does is the whole of
    informed consent here, which is also why the grant is per-tool (R1)
    rather than one yes covering the set.
    """
    from tools.base import GRANT_ANYWHERE
    from tools.registry import registry

    out = []
    for name in sorted(registry.headless_hidden(context)):
        if (not registry.grantable(name)
                or registry.grant_policy(name) != GRANT_ANYWHERE):
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
