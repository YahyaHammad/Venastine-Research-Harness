"""
project_init/doc_sets.py

ROADMAP_v2 §24 (I9-I12): WHICH documents `/init` scaffolds, where each one
lives, and what an unwritten one looks like.

Data and pure rendering only -- no model call, no filesystem, no decisions
about when to write. That split is why the allowlist `write_project_doc`
enforces can be read from here without a cycle, and why the templates below
can be asserted against directly in tests.

THE HUB IS THE POINT. CONTEXT.md is the only document §14's loader reads,
and the only one injected into an opted-in agent's prompt. Everything else
is an ordinary committed root file that CONTEXT.md links out to -- so the
one file the model always sees is also the map of the ones it does not.
That index is generated from these tuples rather than written by the model
(I11): a model listing its own documents is a model that can get a relative
path wrong in the single file every agent reads.

STUBS, NOT INVENTED CONTENT (I10). CONTEXT.md is generated in full by a
model that has actually read the project. These are skeletons: real
headings, and for each one a statement of what belongs in it and what does
not. A DEVLOG written for a project with no history, or an ARCHITECTURE
describing code an agent skimmed once, is confident fiction -- in files
that then get committed, linked from the hub, and read as established fact.
An honest empty section outperforms a plausible wrong one.
"""

from __future__ import annotations

from typing import Optional

CONTEXT = "CONTEXT.md"

SOFTWARE = "software"
RESEARCH = "research"
PROJECT_KINDS = (SOFTWARE, RESEARCH)

# The shared spine. DOCUMENTATION_STANDARDS is in both sets because the
# question it answers -- how this project writes things down -- is not
# specific to either kind, and a project that answers it early is the one
# whose docs still agree with each other a year later.
_SHARED = ("DOCUMENTATION_STANDARDS.md",)

_SOFTWARE_DOCS = _SHARED + (
    "ARCHITECTURE.md",
    "ROADMAP.md",
    "DEVLOG.md",
    "TECHNICAL_DEBT.md",
    "TEST_WRITING.md",
    "BREAKING_CHANGES.md",
)

_RESEARCH_DOCS = _SHARED + (
    "RESEARCH_QUESTIONS.md",
    "METHODOLOGY.md",
    "SOURCES.md",
    "FINDINGS.md",
    "LIMITATIONS.md",
    "EXPERIMENT_LOG.md",
    "OPEN_QUESTIONS.md",
)

_SETS = {SOFTWARE: _SOFTWARE_DOCS, RESEARCH: _RESEARCH_DOCS}


def document_set(kind: str) -> tuple:
    """The non-hub documents for a project kind, in the order they should
    appear in CONTEXT.md's index. Raises on an unknown kind rather than
    defaulting to software: a caller that got the kind wrong would scaffold
    the wrong seven files and only find out by reading them."""
    if kind not in _SETS:
        raise ValueError(
            f"Unknown project kind {kind!r}; expected one of "
            f"{', '.join(PROJECT_KINDS)}.")
    return _SETS[kind]


def all_document_names() -> frozenset:
    """Every name `write_project_doc` will accept -- both sets plus the hub.

    The union, not the current project's set, and deliberately: a project
    that started as research and grew a codebase should be able to gain an
    ARCHITECTURE.md without /init having to re-classify it first. The
    confinement this provides is "a known document name, never a path",
    which is unaffected by the set being the larger one.
    """
    return frozenset((CONTEXT,) + _SOFTWARE_DOCS + _RESEARCH_DOCS)


# ---------------------------------------------------------------------------
# ---- Stub content ---------------------------------------------------------
# ---------------------------------------------------------------------------

# name -> (one-line purpose, belongs, does not belong, [section headings])
_STUBS: dict = {
    "DOCUMENTATION_STANDARDS.md": (
        "How this project writes things down, so its documents stay usable "
        "as they grow.",
        "Where each kind of information goes; what a good entry looks like; "
        "the conventions that keep documents from drifting apart.",
        "The information itself. This describes the containers, not the "
        "contents.",
        ["Which document holds what", "Writing conventions",
         "When to update which file", "Review checklist"],
    ),
    "ARCHITECTURE.md": (
        "What exists, how the pieces fit, and which boundaries are "
        "deliberate.",
        "Component responsibilities stated as what belongs here and what "
        "does NOT; the data flow between them; the constraints a change has "
        "to respect; known gotchas that have already cost someone a day.",
        "Plans and intentions -- those are ROADMAP.md. A narrative of how "
        "the code got this way -- that is DEVLOG.md.",
        ["Overview", "Components and their boundaries", "Data flow",
         "Invariants that look like simplification opportunities",
         "Known gotchas"],
    ),
    "ROADMAP.md": (
        "What is planned, what is built, and the decisions that are locked.",
        "Sections with stable numbers so they can be cross-referenced; "
        "acceptance criteria per section; a decisions record whose entries "
        "are never renumbered.",
        "What was actually done and how it deviated -- that is DEVLOG.md.",
        ["Status index", "Planned work", "Acceptance criteria",
         "Decisions record"],
    ),
    "DEVLOG.md": (
        "What was actually built, in order, including where it departed "
        "from the plan.",
        "One entry per unit of work: what was followed verbatim, what was "
        "deviated from and on whose decision, what the implementation "
        "turned up that the plan did not anticipate.",
        "Anything speculative. An entry is written after the work, not "
        "before it.",
        ["How to read this file", "Entries"],
    ),
    "TECHNICAL_DEBT.md": (
        "Known compromises, why they were accepted, and what it would take "
        "to undo them.",
        "Numbered items that stay numbered; the symptom, the cause, the "
        "cost of leaving it, and the shape of the real fix.",
        "Bugs. A bug is something to fix; debt is something that was chosen "
        "with the tradeoff understood.",
        ["Open items", "Resolved items"],
    ),
    "TEST_WRITING.md": (
        "How this project tests, and what makes a test here worth having.",
        "How to run the suite; the fixtures and doubles available and what "
        "they do NOT model; the rule that a new test must fail when the fix "
        "it covers is reverted.",
        "What each individual test asserts -- the tests say that "
        "themselves.",
        ["Running the tests", "Fixtures and test doubles",
         "What makes a test worth having", "Patterns to follow",
         "Patterns to avoid"],
    ),
    "BREAKING_CHANGES.md": (
        "What breaks each test when production code changes, and how to "
        "fix it.",
        "Per area: the change that breaks it, the symptom you will see, and "
        "the fix. Written for the person staring at a red suite who did not "
        "write the test.",
        "Changes that break nothing. This file earns its keep by being "
        "short and true.",
        ["How to use this file", "By area"],
    ),
    "RESEARCH_QUESTIONS.md": (
        "What this project is trying to find out, and why it is worth "
        "finding out.",
        "Each question stated so it could be answered wrongly; why it "
        "matters; what would count as an answer.",
        "Answers. Those are FINDINGS.md, and keeping them apart is what "
        "stops a question being quietly rewritten to match what was found.",
        ["Primary questions", "Secondary questions",
         "What would count as an answer", "Out of scope"],
    ),
    "METHODOLOGY.md": (
        "How evidence is gathered here, and how it is judged.",
        "The procedure, in enough detail to be repeated; inclusion and "
        "exclusion criteria; how confidence is assigned; what is done about "
        "a result that does not replicate.",
        "The results of applying it.",
        ["Approach", "Procedure", "Inclusion and exclusion criteria",
         "How confidence is assigned", "Threats to validity"],
    ),
    "SOURCES.md": (
        "Where the evidence came from, in enough detail to be checked.",
        "Full citations; data provenance and retrieval dates; what each "
        "source is relied on FOR, which is the part a bare bibliography "
        "loses.",
        "Conclusions drawn from a source.",
        ["Primary sources", "Secondary sources", "Datasets",
         "Retrieval notes"],
    ),
    "FINDINGS.md": (
        "What has been established, and how firmly.",
        "One finding per entry, each with its supporting sources and a "
        "stated confidence; negative results, which are findings and are "
        "the ones most often left out.",
        "Anything unsupported. A hypothesis lives in RESEARCH_QUESTIONS.md "
        "until something backs it.",
        ["Established", "Provisional", "Negative results"],
    ),
    "LIMITATIONS.md": (
        "What this work cannot claim, stated by the people best placed to "
        "know.",
        "Scope boundaries; where the method is weakest; the results a "
        "reader might reasonably over-read, named before someone else "
        "names them.",
        "Apologies. A limitation is a fact about the work's reach.",
        ["Scope", "Methodological limitations", "Known confounds",
         "What these results do not show"],
    ),
    "EXPERIMENT_LOG.md": (
        "What was run, when, and what came out -- including the runs that "
        "went nowhere.",
        "One entry per run: configuration, date, outcome, and what was "
        "changed as a result. Failed and abandoned runs especially, since "
        "they are what stops the same dead end being explored twice.",
        "Polished conclusions. Those are FINDINGS.md.",
        ["How to read this file", "Entries"],
    ),
    "OPEN_QUESTIONS.md": (
        "What is still unresolved, and what it would take to resolve it.",
        "Questions raised by the work so far; what has been ruled out "
        "already; the next step each one needs.",
        "Questions the project has decided not to pursue -- say so in "
        "RESEARCH_QUESTIONS.md's out-of-scope section instead, where it "
        "reads as a decision rather than a gap.",
        ["Unresolved", "Ruled out", "Next steps"],
    ),
}


def render_stub(name: str, facts: Optional[dict] = None) -> str:
    """A skeleton for one document: title, purpose, the belongs/does-not
    pair, and empty sections.

    `facts` are the DETERMINISTIC ones the manifest established -- project
    name, stack, test command. They are interpolated only where they are
    genuinely known; nothing here guesses.
    """
    if name not in _STUBS:
        raise ValueError(f"No stub template for {name!r}.")
    purpose, belongs, excludes, sections = _STUBS[name]
    facts = facts or {}

    title = name[:-3].replace("_", " ").title()
    out = [f"# {title}", "", purpose, "",
           f"**What belongs here:** {belongs}", "",
           f"**What does NOT:** {excludes}", ""]

    extra = _fact_lines(name, facts)
    if extra:
        out += ["## What `/init` established", ""] + extra + [""]

    out.append("---")
    out.append("")
    for heading in sections:
        out += [f"## {heading}", "", "_Not yet written._", ""]
    out += [f"Linked from [{CONTEXT}](.venastine/{CONTEXT}).", ""]
    return "\n".join(out)


def _fact_lines(name: str, facts: dict) -> list:
    """The handful of places a detected fact belongs in a stub.

    Kept narrow on purpose. Sprinkling the same three facts through every
    document would make each one look more complete than it is, which is
    the failure mode this whole file is arranged against.
    """
    lines = []
    if name == "ARCHITECTURE.md":
        if facts.get("stack"):
            lines.append(f"- Detected stack: {facts['stack']}")
        if facts.get("entry_points"):
            lines.append(
                f"- Candidate entry points: {', '.join(facts['entry_points'])}")
    elif name == "TEST_WRITING.md":
        if facts.get("test_command"):
            lines.append(f"- Detected test command: `{facts['test_command']}`")
        if facts.get("test_dirs"):
            lines.append(f"- Test locations: {', '.join(facts['test_dirs'])}")
    return lines


def render_index(kind: str) -> str:
    """CONTEXT.md's documentation index, generated from the set (I11).

    A PURE FUNCTION OF THE KIND, and that is load-bearing rather than tidy.
    The first version marked which documents already existed, so /init left
    them alone (I12) but said so in the hub. Two things were wrong with it,
    both found by running the command twice: the index changed on the second
    run purely because the first had created the files, which rewrote
    CONTEXT.md for no reason and defeated the "nothing to do" path -- and the
    mark itself was false, labelling documents /init had authored moments
    earlier as pre-existing work it had preserved.

    Which files were left untouched is a fact about one RUN, not about the
    project, so it belongs in that run's report. It is there.
    """
    lines = ["## Project documentation", "",
             "This file is the hub. Each document below states what belongs "
             "in it and what does not.", ""]
    for name in document_set(kind):
        lines.append(f"- [{name}](../{name}) — {_STUBS[name][0]}")
    lines.append("")
    return "\n".join(lines)
