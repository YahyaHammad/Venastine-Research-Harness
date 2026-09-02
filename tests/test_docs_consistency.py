"""
test_docs_consistency.py

The documented test count has drifted three times now -- §14 left it
stale, §19 corrected it by hand, and the §19-§20 review corrected it by
hand again. TECHNICAL_DEBT.md lists "run the doc cross-check every section
landing" as standing practice, which is the same instruction that has
already been forgotten twice.

This retires the class mechanically instead. Two tiers, because they need
different things to be true:

  1. The three docs agree WITH EACH OTHER. Pure text, no session state, so
     it runs under any invocation -- including `pytest tests/test_foo.py`
     while you are mid-change.
  2. That number is the REAL one. Needs the collected total, which only
     exists on an unfiltered run, so it skips when the invocation narrowed
     rather than failing on a legitimately smaller session.

WHY THIS FILE GREW (audit #121/#125/#126/#129). It used to end with:
"Deliberately narrow. This checks the number that has actually drifted, not
every fact in every document -- a doc-linting framework nobody asked for
would rot faster than the counts it was policing."

That instinct was right and TECHNICAL_DEBT.md item 7 wrote down the
condition under which it stops applying:

    BUILT markers and tree entries are still by hand, because a doc-linting
    framework nobody asked for would rot faster than the counts it was
    policing. IF THOSE TWO START DRIFTING THE SAME WAY, extend this file's
    check rather than adding a new practice to remember.

Both drifted. Eleven of ARCHITECTURE's per-file counts were wrong when the
audit measured them and eighteen were wrong two fix batches later; six of
ROADMAP_v2's index entries were missing their marker, and §23's said its two
tools were unbuilt while 98 tests exercised them. So the file is wider now,
and the rule for what belongs in it is unchanged: a claim earns a check by
having ALREADY drifted, and only if the truth is something the suite can
compute. Every check here compares a document against a live value -- the
collected session, the registry, the permission dataclasses -- never against
another hand-written number.

Unit 16 found the shape all of them share: THE CLAIMS THAT DRIFT ARE THE
ONES SOMEBODY HAD TO COUNT BY HAND.

The checks that need the collected session skip on a narrowed invocation,
via _was_narrowed. The ones that are pure text do not, so they still run
under `pytest tests/test_foo.py` while you are mid-change.
"""

import json
import os
import re
from collections import Counter

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Anchored on the surrounding words, NOT a bare r"(\d+) tests". ARCHITECTURE
# lists a per-file count for every test module in its tree ("# 29 tests --
# ROADMAP_v2 §16 AC1-AC3..."), so a loose pattern would match the first of
# those and compare two unrelated numbers forever.
DOC_PATTERNS = {
    "README.md": r"pytest` — (\d+) tests, fully offline",
    "AGENTS.md": r"# (\d+) tests, offline",
    "ARCHITECTURE.md": r"# (\d+) tests, all offline",
}


def _documented_counts() -> dict:
    found = {}
    for filename, pattern in DOC_PATTERNS.items():
        path = os.path.join(ROOT, filename)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        matches = re.findall(pattern, text)
        assert matches, (
            f"{filename} no longer states a test count matching "
            f"{pattern!r}. If the wording moved, update DOC_PATTERNS here -- "
            f"do not delete the claim, it is the thing this file exists to "
            f"keep honest."
        )
        assert len(matches) == 1, (
            f"{filename} states the test count {len(matches)} times; two "
            f"places to update is how it drifts."
        )
        found[filename] = int(matches[0])
    return found


def test_the_three_docs_state_the_same_test_count():
    counts = _documented_counts()

    assert len(set(counts.values())) == 1, (
        f"The docs disagree about how many tests there are: {counts}. "
        f"They are read by different audiences and all three get quoted."
    )


def _was_narrowed(config) -> bool:
    """Whether this invocation collected less than the whole suite.

    pytest.ini's addopts carries `-m "not integration"`, so a default run
    already has a markexpr -- comparing against None would skip always and
    the assertion would never run, which is the failure mode this guard
    most needs to avoid.
    """
    if getattr(config.option, "keyword", ""):          # -k
        return True
    if getattr(config.option, "markexpr", "") != "not integration":
        return True
    # An arg naming a file or a nodeid; a bare `pytest` or `pytest tests/`
    # collects everything and is not narrowing.
    return any("::" in arg or arg.endswith(".py") for arg in config.args)


class _FakeOption:
    def __init__(self, keyword="", markexpr="not integration"):
        self.keyword = keyword
        self.markexpr = markexpr


class _FakeConfig:
    def __init__(self, args, **kw):
        self.args = args
        self.option = _FakeOption(**kw)


def test_the_default_invocation_is_not_treated_as_narrowed():
    """The guard's own failure mode is SILENT, which is why it gets a
    direct test rather than a revert-and-see-red.

    Get it wrong in the permissive direction and a filtered run asserts
    against a session of one item -- loud, obvious, fixed in a minute. Get
    it wrong in the restrictive direction (say, comparing markexpr against
    None, when pytest.ini's addopts always supplies "not integration") and
    every run skips, the assertion never executes, and the file sits there
    looking like coverage while the counts drift exactly as before.
    """
    assert not _was_narrowed(_FakeConfig(["."]))
    assert not _was_narrowed(_FakeConfig(["tests"]))

    assert _was_narrowed(_FakeConfig(["."], keyword="grounding"))
    assert _was_narrowed(_FakeConfig(["."], markexpr="integration"))
    assert _was_narrowed(_FakeConfig(["tests/test_review.py"]))
    assert _was_narrowed(_FakeConfig(["tests/test_review.py::test_x"]))


def test_the_documented_count_is_the_real_one(request):
    if _was_narrowed(request.config):
        pytest.skip("filtered invocation collects less than the full suite")

    documented = set(_documented_counts().values()).pop()
    actual = len(request.session.items)

    assert documented == actual, (
        f"The docs say {documented} tests; this run collected {actual}. "
        f"Update README.md, AGENTS.md and ARCHITECTURE.md -- the count is "
        f"quoted in all three."
    )


# ---------------------------------------------------------------------------
# ---- #121: ARCHITECTURE's per-file counts ----------------------------------
# ---------------------------------------------------------------------------

# Matches a tree entry: `test_foo.py    # 12 tests -- what it covers`.
# Anchored on the filename so it cannot drift onto the aggregate count twelve
# lines above, which is the trap DOC_PATTERNS' comment already describes.
_TREE_ENTRY = re.compile(r"(test_\w+\.py)\s*#\s*(\d+) tests?\b")


def _tree_entries() -> dict:
    with open(os.path.join(ROOT, "ARCHITECTURE.md"), encoding="utf-8") as f:
        entries = _TREE_ENTRY.findall(f.read())
    assert entries, (
        "ARCHITECTURE.md's tree no longer states any per-file test count in "
        "the form this parses. If the tree's format changed, update "
        "_TREE_ENTRY -- do not delete the claims, they are what this checks."
    )
    counted = Counter(name for name, _ in entries)
    duplicated = [name for name, n in counted.items() if n > 1]
    assert not duplicated, (
        f"ARCHITECTURE.md states a count twice for {duplicated}. Two places "
        f"to update is how a number drifts."
    )
    return {name: int(count) for name, count in entries}


def test_the_tree_states_a_correct_count_for_every_collected_test_file(request):
    """#121. STRICT in both directions: a wrong count fails, and so does a
    test file with no entry at all.

    Eleven of the fifty-three stated counts were wrong when the audit
    measured them, and eight files had no entry -- including
    test_interaction.py, the third-largest in the project. Two fix batches
    later it was eighteen wrong. Nothing anywhere added them up, which is
    why it stayed invisible: each number is only ever read one at a time,
    next to the file it describes, by someone deciding whether that file is
    worth opening.

    A stated file that is not collected is NOT failed here -- it may be
    deselected, like test_mcp_integration.py under the default markexpr.
    The sibling test below is what catches an entry naming a file that has
    genuinely gone away.
    """
    if _was_narrowed(request.config):
        pytest.skip("filtered invocation collects less than the full suite")

    stated = _tree_entries()
    actual = Counter(
        os.path.basename(item.nodeid.split("::")[0])
        for item in request.session.items
    )

    wrong = [(name, stated[name], n) for name, n in sorted(actual.items())
             if name in stated and stated[name] != n]
    missing = [(name, n) for name, n in sorted(actual.items())
               if name not in stated]

    assert not wrong and not missing, (
        "ARCHITECTURE.md's source tree disagrees with the collected suite.\n"
        + "".join(f"  {name}: tree says {says}, collected {is_}\n"
                  for name, says, is_ in wrong)
        + "".join(f"  {name}: no entry in the tree ({n} tests)\n"
                  for name, n in missing)
        + "The tree is the authoritative list of what each part of the suite "
          "covers; a new test file belongs in it."
    )


#: Directories the tree is expected to enumerate module by module. Not a
#: walk of everything: `tests/` has its own check above, and generated or
#: vendored trees have no business being listed by hand.
_MAPPED_DIRS = (
    ".", "bin", "scripts", "core", "core/reasoning", "prompts", "security",
    "safety", "mcp_client", "agents", "skills", "memories", "project_init",
    "tui", "tools", "tools/builtin",
)


def test_the_tree_names_every_production_module():
    """Batch 51. The sibling check above is strict in both directions for
    TEST files and says why -- "the tree is the authoritative list of what
    each part of the suite covers; a new test file belongs in it". The
    production half had no check at all, and had quietly lost eight modules
    and two whole directories: core/session.py,
    core/reasoning/payload_validation.py, tools/isolation.py, ask_user.py,
    remember.py, todo.py, and bin/ and scripts/ entirely -- while AGENTS.md
    gives bin/venastine.mjs a section of its own.

    Matched on TREE ENTRIES, not on the file's text. The first version of
    this audit ran as a substring search, reported six, and missed two:
    `todo.py` matches inside `test_todo.py`, and `payload_validation.py`
    inside `test_payload_validation.py`. A check that reads the whole
    document cannot tell "listed in the map" from "mentioned in a
    paragraph", which is the distinction the map is for.
    """
    with open(os.path.join(ROOT, "ARCHITECTURE.md"), encoding="utf-8") as f:
        tree = f.read().split("```")[1]
    entries = {e.rstrip("/").split("/")[-1]
               for e in re.findall(r"[\u251c\u2514]\u2500\u2500\s+([\w./-]+)", tree)}
    assert entries, (
        "no tree entries parsed out of ARCHITECTURE.md's first fenced "
        "block. If the tree moved or its drawing characters changed, point "
        "this at the new one rather than deleting the check.")

    missing = []
    for folder in _MAPPED_DIRS:
        directory = os.path.join(ROOT, folder)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.endswith((".py", ".mjs")) or name == "__init__.py":
                continue
            if name not in entries:
                missing.append(f"{folder}/{name}")

    assert not missing, (
        "ARCHITECTURE.md's source tree does not name these modules:\n  "
        + "\n  ".join(missing)
        + "\nThe tree is this document's map of the codebase. A module it "
          "omits is one nobody finds by reading the map, and eight of them "
          "accumulated before anyone diffed it.")


def test_no_tree_entry_names_a_test_file_that_does_not_exist():
    """The other direction, and it needs no session -- so it runs even
    mid-change.

    This is #91's class applied to the tree: five live pointers survived the
    rename of test_compaction_e2e.py to test_storage_e2e.py, one of them in
    the docstring of the project's most instructive bug. An entry for a file
    that is gone is the same defect with a smaller blast radius, and the
    collected-session check above cannot see it -- a deselected file and a
    deleted one look identical from there.
    """
    tests_dir = os.path.join(ROOT, "tests")
    gone = [name for name in sorted(_tree_entries())
            if not os.path.exists(os.path.join(tests_dir, name))]

    assert not gone, (
        f"ARCHITECTURE.md's tree lists test files that do not exist: {gone}. "
        f"If one was renamed, the entry moves with it."
    )


# ---------------------------------------------------------------------------
# ---- #129: ROADMAP_v2's status markers -------------------------------------
# ---------------------------------------------------------------------------

_INDEX_ENTRY = re.compile(r"^- (\d+)\. (.+)$", re.MULTILINE)


def test_every_roadmap_v2_index_entry_carries_a_status_marker():
    """#129. The index is the project's status board, and six of its fifteen
    entries carried no marker at all -- §13 through §18, every one of them
    built, because the convention was introduced at §19 and never
    backfilled. They read as outstanding work in the one document a reader
    consults to find out what is outstanding.

    §23's was worse than missing: a positive statement that its two tools
    were still to come, while test_question_tool.py and test_todo.py
    exercised them with 98 tests. That is the shape most likely to make
    someone start building something that already exists.

    Checks only that a marker is PRESENT. What it says is a judgement no
    test can make -- the point is that the question was answered at all.
    """
    with open(os.path.join(ROOT, "ROADMAP_v2.md"), encoding="utf-8") as f:
        entries = _INDEX_ENTRY.findall(f.read())

    assert len(entries) >= 15, (
        f"ROADMAP_v2.md's index parsed as {len(entries)} entries, which is "
        f"fewer than the sections known to exist. If the index format "
        f"changed, update _INDEX_ENTRY rather than letting this pass on a "
        f"list it can no longer see."
    )

    # The convention is a BOLDED PARENTHETICAL, not the word "BUILT":
    # markers read "**(BUILT)**", "**(BUILT: §21a + §21b + §21c)**" and
    # "**(added during §16; BUILT — ...)**". Requiring the word would also
    # make a genuinely unbuilt future section unable to satisfy this, which
    # is the opposite of the point -- what is checked is that the question
    # was ANSWERED, not what the answer was.
    unmarked = [f"§{num}" for num, text in entries if "**(" not in text]
    assert not unmarked, (
        f"ROADMAP_v2.md's index entries carry no status marker: {unmarked}. "
        f"Every section listed there is either built or not, and the index "
        f"is where a reader looks to find out which."
    )


# ---------------------------------------------------------------------------
# ---- #125: README's default-approval table ---------------------------------
# ---------------------------------------------------------------------------

def test_every_registered_tool_appears_in_readmes_approval_table():
    """#125. The table accounted for eighteen of twenty-two tools. The four
    it missed were everything §23 and §24 added, including the gated
    write_project_doc -- the tool /init uses to write documents into a
    repository, absent from the row listing what can write to your disk
    without asking.

    ToolPermissions and ToolApprovals are plain dataclasses, so this is the
    cheap half of the answer: the table has to MENTION every tool. Which row
    it puts each one in is a claim about approval semantics that the section
    around the table already argues in prose, and pinning that too would
    make the table unwritable in any other shape.
    """
    import config

    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        readme = f.read()

    # THE TABLE ROWS ONLY, not a character window around the heading. The
    # first version of this took readme[start:start + 2000], which swallowed
    # the explanatory paragraphs underneath -- and those name
    # `write_project_doc` too, so deleting it from the table left the check
    # green. A window is not a structure; this walks to the table and stops
    # at the first line that is not a row.
    after = readme.split("### What needs approval by default", 1)
    assert len(after) == 2, (
        "README.md no longer has a 'What needs approval by default' heading. "
        "If it moved, update this anchor -- do not drop the check."
    )
    rows = []
    for line in after[1].splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            rows.append(stripped)
        elif rows:
            break
    assert len(rows) >= 3, (
        f"README's approval table parsed as {len(rows)} rows. If its format "
        f"changed, update this parser rather than letting the check pass on "
        f"a table it can no longer see."
    )
    table = "\n".join(rows)

    missing = [name for name in sorted(vars(config.ToolPermissions()))
               if f"`{name}`" not in table]
    assert not missing, (
        f"README's default-approval table does not mention: {missing}. It is "
        f"the outward-facing statement of the security model, and a tool "
        f"absent from it is one nobody auditing this project will weigh."
    )


# ---------------------------------------------------------------------------
# ---- #126: ARCHITECTURE's registry counts ----------------------------------
# ---------------------------------------------------------------------------

def test_architecture_states_the_real_registry_counts():
    """#126. "Under default config this is 12 of 16 registered tools" was
    16 of 22 -- and the parenthetical naming four permission-False tools
    missed two more that fall out through available_check, the mechanism the
    same paragraph explains three sentences later.

    This matters more than a stale count usually would, because the number
    IS the argument: the paragraph exists to say how much surface a headless
    run advertises, and the audit's whole threat model for tools/builtin/
    was framed against it.
    """
    from tools.registry import registry

    registered = len(registry._tools)
    advertised = len(registry.schemas(None))
    callable_ = len(registry.schemas(None, callable_only=True))

    with open(os.path.join(ROOT, "ARCHITECTURE.md"), encoding="utf-8") as f:
        arch = f.read()

    claim = f"this is {advertised} of {registered} registered tools"
    assert claim in arch, (
        f"ARCHITECTURE.md's headless-advertising paragraph does not state "
        f"the live registry counts. Expected {claim!r}; the registry has "
        f"{registered} registered, {advertised} advertised, {callable_} "
        f"callable headless."
    )
    assert f"{callable_} are callable headless" in arch, (
        f"ARCHITECTURE.md does not state the headless-callable count; it is "
        f"{callable_} of the {advertised} advertised."
    )


# ===========================================================================
# ---- §31 (H10), #56: no undeclared third record ---------------------------
# ===========================================================================
#
# `tools/builtin/.ipynb_checkpoints/` held two committed notebook checkpoint
# copies. They were inert -- not importable (the directory name is not a
# valid identifier), not registered (registration is by explicit import),
# not collected (the filenames do not match pytest's pattern) -- and that is
# exactly why nothing caught them.
#
# What made them worth deleting rather than ignoring is what they said.
# `web_search-checkpoint.py` carried `BLOCKED_DOMAINS: set[str] = set()`, an
# EMPTY blocklist, so `grep BLOCKED_DOMAINS` returned it beside the real one
# in safety/policy_enforcement.py -- while #48, #53 and #54 were open
# against that exact control. It also carried the `extra={}` logging bug and
# the `raise WebSearchError` pattern, both of which CLAUDE.md records as
# fixed defects with the reasons they were fixed.
#
# This file's subject is the project not contradicting itself, and #56's own
# argument is that these were "an undeclared third record" sitting beside
# DEVLOG.md and BREAKING_CHANGES.md without being marked historical. So the
# check belongs here.
#
# The check is on the TRACKED set, not on what is present on disk, and
# that distinction was measured rather than assumed: this working tree
# has EIGHT .ipynb_checkpoints directories and exactly one of them was
# ever committed. A test that failed on all eight would fail on any
# machine with notebook history, and a test people learn to ignore
# protects nothing. Untracked residue is the developer's own business
# and .gitignore already covers it; a COMMITTED copy is a claim about
# the project, which is what #56 is about.
#
# Note that .gitignore listing `.ipynb_checkpoints/` did not prevent
# this: both files were added before the entry existed, and gitignore
# does not untrack. So a gitignore-based check would have passed while
# the files sat there.

_CHECKPOINT_DIR = ".ipynb_checkpoints"

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              ".pytest_cache", "output", "logs", "build", "dist",
              _CHECKPOINT_DIR}


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _walk_project():
    """Live source only -- checkpoint directories are excluded here and
    checked separately, so a stale copy cannot answer a question about
    what the project defines."""
    for dirpath, dirnames, filenames in os.walk(_project_root()):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        yield dirpath, dirnames, filenames


def test_no_notebook_checkpoint_file_is_committed():
    """#56. Two of these sat inside tools/builtin/, one carrying an EMPTY
    BLOCKED_DOMAINS beside the real one.

    Skips without git rather than falling back to a filesystem walk: the
    property is about what the repository CLAIMS, and that is a git fact.
    A walk would answer a different question and call it the same name."""
    import subprocess
    try:
        listed = subprocess.run(
            ["git", "ls-files", "*" + _CHECKPOINT_DIR + "*"],
            cwd=_project_root(), capture_output=True, text=True,
            timeout=30)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pytest.skip("git is not available to ask what is tracked")
    if listed.returncode != 0:  # pragma: no cover
        pytest.skip("not a git checkout")

    tracked = [line for line in listed.stdout.splitlines() if line.strip()]
    assert tracked == [], (
        f"{tracked} -- committed notebook checkpoints are stale copies of live code, unmarked as historical, and reachable by every grep and code search a reader or an agent runs (#56)")


def test_the_url_blocklist_is_defined_exactly_once():
    """The harm #56 names, stated as the property rather than as the file.

    A second definition is not a style problem when the name is a security
    control: `grep BLOCKED_DOMAINS` returned an EMPTY set beside the real
    one, and a reader auditing #48/#53/#54 had no way to tell from the grep
    which was live. Anchored on the ASSIGNMENT so the many test-side
    monkeypatches of the same name do not count.
    """
    definition = re.compile(r"^BLOCKED_DOMAINS\s*(:[^=]+)?=", re.M)
    # _walk_project already excludes checkpoint directories, so untracked
    # residue on a developer's machine cannot fail this. The committed
    # case is test_no_notebook_checkpoint_file_is_committed's job.
    defining = []
    for dirpath, _dirnames, filenames in _walk_project():
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8", errors="replace") as f:
                if definition.search(f.read()):
                    defining.append(os.path.relpath(path))
    assert len(defining) == 1, (
        f"BLOCKED_DOMAINS is defined in {defining}; a security control with "
        "two definitions has none that a reader can trust")
    assert defining[0].replace(os.sep, "/") == "safety/policy_enforcement.py"


# ---------------------------------------------------------------------------
# ---- #16 / #17 / #147: the record's index ----------------------------------
# ---------------------------------------------------------------------------
#
# Three issues, one defect. #16: sixteen decisions defined only in DEVLOG
# while the map said the ROADMAPs carried them. #17: two prefixes carrying
# two numbering schemes each -- three, measured. #147: a whole family cited
# as authority in six production files and defined nowhere, whose one
# lookupable id resolved to a different decision.
#
# #147 argued for "one mechanical check over all of them rather than three
# prose fixes", because prose fixes are the thing that already drifted. Same
# rule as the rest of this file: a claim earns a check by having ALREADY
# drifted, and only if the truth is something the suite can compute.
#
# What makes this computable is that ids are a closed set with two sides --
# what the record DEFINES and what the code CITES -- and both are readable.

_RECORD_DOCS = ("ROADMAP.md", "ROADMAP_v2.md")

# THREE SHAPES FOR ONE DEFINITION, and this is the load-bearing detail. The
# record writes a decision as a table row (`| **U1** | ... |`), as a bolded
# lead with the dash INSIDE the bold (`**M1 -- Compaction defends...**`), or
# as a plain table row with no bold (`| D1 | Build order | ... |`). A scanner
# that knows only the first reports the entire M family -- 21 ids -- as
# undefined, which is what the first census in this batch did before anyone
# noticed the number was wrong. test_the_definition_scanner_sees_every_shape
# below is what stops that from being silent.
_DEFINITION_ROW = re.compile(r"^\|\s*\*{0,2}([A-Z]{1,2}\d+[a-z]?)\*{0,2}\s*\|")
_DEFINITION_LEAD = re.compile(r"^\s*(?:[-*]\s*)?\*\*([A-Z]{1,2}\d+[a-z]?)\b")

# A citation in prose. The leading (?<!-) suppresses an id that follows a
# hyphen, which is the trailing endpoint of a RANGE -- doc_sets.py cites
# "(I9-I12)" and config_loader.py says "pre-D29 text". Both of those resolve
# anyway; the case that needs the guard is a range over a SPARSE family,
# where "C1-C10" would report five ids that never existed. (It also caught
# "[a-zA-Z0-9]" back when this scanned raw lines; _prose_lines made that
# half of the reason obsolete, and the mutation pass is what noticed.)
_CITATION = re.compile(r"(?<![A-Za-z0-9_\-])([A-Z]{1,2}\d{1,2})(?![A-Za-z0-9_])")

# The five namespaces that are NOT the record, each declaring which ids it
# covers and how a citation announces it. Covering matters: without it,
# "§32 A3" would read as an acceptance criterion merely because a section
# number precedes it, and A3 is a decision. AGENTS.md carries the same table
# in prose -- if you add a namespace, add it in both places.
_NAMESPACES = (
    # Section-scoped, so the SECTION is the qualifier and it may sit anywhere
    # earlier on the line: "§23 (AC1)" and "§27 T2 / AC6" are already
    # unambiguous to a reader, and rewriting them would be churn.
    ("acceptance criterion", re.compile(r"^AC\d+$"), re.compile(r"§\s*\d+")),
    ("pipeline gate", re.compile(r"^D[012]$"), re.compile(r"gate\s*$", re.I)),
    ("Rev. 1 open question", re.compile(r"^S1[126]$"),
     re.compile(r"Rev\.?\s*1\s*$", re.I)),
    ("mutation id", re.compile(r"^[A-Z]{1,2}\d+$"),
     re.compile(r"mutation\s*$", re.I)),
    ("review finding", re.compile(r"^F\d+$"),
     re.compile(r"finding\s*$", re.I)),
)


def _namespace_of(token, before):
    """Which non-record namespace this citation announces, or None.

    `before` is the text on the same line, left of the id.
    """
    for name, covers, mark in _NAMESPACES:
        if covers.match(token) and mark.search(before):
            return name
    return None


def _decision_definitions(docs=_RECORD_DOCS):
    """{id: [document, ...]} for every decision the record defines."""
    found = {}
    for name in docs:
        path = os.path.join(ROOT, name)
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                for pattern in (_DEFINITION_ROW, _DEFINITION_LEAD):
                    hit = pattern.match(line)
                    if hit:
                        found.setdefault(hit.group(1), []).append(name)
                        break
    return found


def _map_line():
    with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
        text = f.read().replace("\r\n", "\n")
    line = next((l for l in text.split("\n")
                 if "Design Decisions Record" in l and "ROADMAP" in l), None)
    assert line, (
        "AGENTS.md has no documentation-map line naming the Design Decisions "
        "Record. That line is what these checks quantify over; if it moved, "
        "point this at its new home rather than deleting the check.")
    return line.replace("\u2013", "-")


def _claimed_ids():
    """Every decision id AGENTS.md's map says the record holds.

    Ranges AND bare listings, because C is sparse: only the Rev. 1 conflicts
    with live citations were recovered (C1, C3, C6, C8, C10), so claiming
    "C1-C10" would assert five decisions that never existed. A family is
    written as a range when it is contiguous and as a list when it is not.
    """
    flat = _map_line()
    ids = set()
    for hit in re.finditer(r"\b([A-Z]{1,2})(\d+)-[A-Z]{0,2}(\d+)\b", flat):
        fam, lo, hi = hit.group(1), int(hit.group(2)), int(hit.group(3))
        ids |= {f"{fam}{n}" for n in range(lo, hi + 1)}
    ids |= set(re.findall(r"\b[A-Z]{1,2}\d+[a-z]?\b", flat))
    return ids


def _claimed_families():
    return {re.match(r"[A-Z]+", i).group(0) for i in _claimed_ids()}


def _prose_lines(path):
    """(line number, text) for every comment and docstring line in a file.

    Citations live in prose. A runtime string VALUE holding an id is data:
    the pipeline's stage names are literally "D0" and "D1", its trace lines
    are keyed by them, and test_pipeline_trace_contains_expected_lines_in_
    order matches on the "D0:" prefix. Qualifying one of those would change
    what the pipeline emits -- which is exactly what happened when this
    batch tried it, and how the boundary got drawn here instead of guessed.
    """
    import io
    import tokenize

    with open(path, encoding="utf-8", errors="replace") as f:
        source = f.read()
    lines = source.split("\n")
    keep = {}
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []  # pragma: no cover -- a syntax error is another test's job
    for token in tokens:
        if token.type == tokenize.COMMENT:
            keep[token.start[0]] = lines[token.start[0] - 1]
        elif (token.type == tokenize.STRING
              and token.string.lstrip("rbfuRBFU")[:3] in ('"""', "'''")):
            for n in range(token.start[0], token.end[0] + 1):
                keep[n] = lines[n - 1]
    return sorted(keep.items())


def _production_python():
    for dirpath, dirnames, filenames in _walk_project():
        if os.path.basename(dirpath) == "tests":
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def test_the_definition_scanner_sees_every_shape_the_record_uses():
    """The guard on the guard, and the reason it exists is a measured one.

    Every check below is only as good as _decision_definitions, and that
    function is exactly the kind of thing that fails SILENTLY: miss a shape
    and a whole family reads as undefined, or -- worse -- a family that is
    genuinely missing reads as present because the scanner never looked.

    Batch 14's lesson applied here. Both of its mutation survivors were its
    own new tests, and both were vacuous for the same reason: the input
    could not discriminate. So this pins one id per shape, by name, against
    the real documents.
    """
    found = _decision_definitions()

    shapes = {
        "plain table row (| D1 | ... |)": "D1",
        "bolded table row (| **U1** | ... |)": "U1",
        "bolded lead, dash inside (**M1 -- ...**)": "M1",
    }
    missed = {shape: i for shape, i in shapes.items() if i not in found}
    assert not missed, (
        f"the definition scanner no longer sees {missed}. Each entry is one "
        f"of the three shapes the record writes a decision in; losing one "
        f"makes every check below pass on a set it cannot see.")

    # And the count, so narrowing the patterns until they match nothing
    # cannot look like success.
    assert len(found) >= 190, (
        f"the record defines {len(found)} ids, which is far fewer than the "
        f"~200 known to exist. The scanner probably stopped matching a "
        f"shape rather than the record having shrunk.")


def test_every_decision_id_the_map_claims_is_in_the_record():
    """#16. AGENTS.md's map says the ROADMAPs carry the locked record and
    names the families by range. Four of those ids -- S2, S3, E4, E6 -- were
    filed as living only in DEVLOG; measured, it was all sixteen of S1-S4
    and E1-E12, and the map never named E at all.

    The cost is not tidiness. This project's convention on finding no record
    is to treat the question as unsettled and RE-DERIVE it, so a decision
    that cannot be found is a decision that gets contradicted by someone
    following the rules. S2's whole subtlety is that a False approval
    override is indistinguishable from an absent one -- re-derive it without
    that sentence and you implement assignment instead of union.
    """
    record = _decision_definitions()
    claimed = _claimed_ids()
    assert len(_claimed_families()) >= 15, (
        f"the map parsed as {len(_claimed_families())} families, fewer than "
        f"are known to exist. If its wording changed, update _claimed_ids.")

    missing = {}
    for one in sorted(claimed):
        if one not in record:
            missing.setdefault(re.match(r"[A-Z]+", one).group(0),
                               []).append(one)
    assert not missing, (
        f"AGENTS.md's documentation map says the ROADMAPs carry these, and "
        f"they are not there: {missing}. Either move the definition into "
        f"the record or stop claiming the record holds it -- a reader who "
        f"greps and finds nothing re-derives the decision.")


def test_a_claimed_range_covers_its_whole_family():
    """Batch 51. The two checks around this one both PASS on a range that
    stops short, and that is how E13 and E14 came to sit outside the index
    that says where decisions live.

    The forward check asserts every id the map claims is in the record --
    true of E1-E12, which is a subset. The reverse check asserts every
    family is named -- E is named. Neither looks at the endpoint, so a map
    saying `E1-E12` against a record holding E14 is green in both
    directions, while AGENTS.md's own ensemble section cites E13 and E14 by
    number three hundred lines below it.

    The cost is the one #16's docstring already names: this project's
    convention on finding no record is to RE-DERIVE the decision. A
    reader who greps the map for the E family, reads to E12 and stops has
    been told the record ends there.

    Families written as a LIST rather than a range are skipped -- C is
    sparse on purpose (only the Rev. 1 conflicts with live citations were
    recovered), and _claimed_ids' docstring explains why claiming C1-C10
    would be worse than claiming five.
    """
    record = _decision_definitions()
    flat = _map_line()

    highest_defined = {}
    for one in record:
        hit = re.match(r"([A-Z]+)(\d+)$", one)
        if hit:
            family, number = hit.group(1), int(hit.group(2))
            highest_defined[family] = max(
                highest_defined.get(family, 0), number)

    short = []
    for hit in re.finditer(r"\b([A-Z]{1,2})(\d+)-[A-Z]{0,2}(\d+)\b", flat):
        family, top = hit.group(1), int(hit.group(3))
        defined = highest_defined.get(family)
        if defined is not None and defined > top:
            short.append(f"{family}: map claims through {family}{top}, "
                         f"record defines {family}{defined}")

    assert not short, (
        "AGENTS.md's map claims a range that stops short of its family:\n  "
        + "\n  ".join(short)
        + "\nExtend the range. A decision outside the index that says where "
          "decisions live is one the next reader re-derives.")


def test_the_map_names_every_family_the_record_defines():
    """#148's arithmetic, as a check. The map enumerated eleven families
    while the record held thirteen; E1-E12 was the omission, and AGENTS.md's
    own ensemble section cites nine E ids by number three sections below the
    map that says the record runs D through J.

    The forward direction above cannot see this: a family the map never
    mentions has no range to check.
    """
    record = _decision_definitions()
    claimed = _claimed_families()
    defined = {re.match(r"[A-Z]+", i).group(0) for i in record}

    unnamed = sorted(defined - claimed)
    assert not unnamed, (
        f"the record defines {unnamed} and AGENTS.md's map does not name "
        f"them. The map is where a reader learns the record exists, so a "
        f"family missing from it is a family nobody greps for.")


def test_every_decision_id_cited_in_production_code_resolves():
    """#147, and the half of #17 that #17 did not file.

    Measured on the tree this batch started from: 26 distinct ids, 83
    citations, none of which resolved. C3/C6/C10 were cited as authority in
    six production files and defined nowhere. AC was cited unqualified 24
    times, and AC6 alone meant three different criteria in three files --
    §17's, §21's and §21b's. P10 was a mutation id sitting in the P family's
    numbering. S12 was a Rev. 1 open question sitting in S's.

    So the rule is: an id in a comment or docstring either resolves in the
    record, or says which other namespace it belongs to. That is a QUALIFIER
    GRAMMAR rather than a list of blessed ids, which is what makes it hold
    for the next citation nobody has written yet -- the property #148 says
    separates the two guards in this project that worked (D24, J4) from the
    warnings that did not.
    """
    record = _decision_definitions()
    unresolved = {}
    for path in _production_python():
        for number, line in _prose_lines(path):
            for hit in _CITATION.finditer(line):
                token = hit.group(1)
                if _namespace_of(token, line[:hit.start()]):
                    continue
                if token in record:
                    continue
                where = f"{os.path.relpath(path, ROOT)}:{number}"
                unresolved.setdefault(token, []).append(where)

    assert not unresolved, (
        f"these ids are cited in production code and resolve to nothing in "
        f"ROADMAP.md or ROADMAP_v2.md: "
        f"{ {k: v[:3] for k, v in sorted(unresolved.items())} }. Either the "
        f"decision belongs in the record, or the citation belongs to one of "
        f"the namespaces AGENTS.md lists and should say so -- `gate D0`, "
        f"`§21 AC6`, `Rev. 1 S12`, `mutation P10`, `review finding F2`.")


def test_the_qualifier_grammar_discriminates():
    """The guard against loosening the guard.

    test_every_decision_id_cited_in_production_code_resolves passes when
    every citation either resolves or is qualified -- so widening what
    counts as a qualifier makes it pass trivially, and nothing about the
    documents would change. That is the loosen-the-check mutation, and it is
    the same vacuity shape batch 14 found in two of its own new tests: an
    assertion whose input cannot discriminate.

    The pairs below are the distinctions the grammar is FOR. A section
    number is not a blanket qualifier -- `§32 A3` cites decision A3 in a
    sentence about §32, and reading it as an acceptance criterion is the
    exact confusion this batch was fixing.

    ONE namespace is deliberately family-open, and writing this test is what
    made it explicit: a mutation id is named after the decision it mutates
    (`P10`, and batch 14's `U8`/`W9`), so `mutation` cannot be scoped to a
    letter the way `gate` is scoped to D0-D2. It is the one qualifier that
    is a claim about the id rather than a pattern over it. That is a real
    escape hatch -- one deliberate word wide -- and it is here in writing
    rather than discovered later.
    """
    qualifies = [
        ("AC6", "    Returns a plain dict, never a CallToolResult (D23, §17 "),
        ("AC1", "        # §21 "),
        ("D0", "        # claims skip grounding entirely per the pipeline's gate "),
        ("S12", "        # Rev. 1 "),
        ("P10", "        # suite when audit unit 3 ran mutation "),
        ("F2", "        # (review finding "),
    ]
    for token, before in qualifies:
        assert _namespace_of(token, before), (
            f"{token!r} after {before.strip()!r} should announce its "
            f"namespace and does not -- the grammar stopped recognising a "
            f"form that is in use.")

    refuses = [
        # A section number qualifies an AC. It does not qualify a DECISION
        # that happens to follow one, and A3 is a decision (§32).
        ("A3", "    # §32 "),
        # The word is not enough on its own; the id has to be in the family
        # that namespace covers. There are three pipeline gates, and R4 is
        # not one of them.
        ("R4", "    # gate "),
        # The word has to be ADJACENT. "gate" earlier in a sentence that
        # then cites a different gate is prose, not a qualifier -- without
        # this, unanchoring the pattern is a mutation nothing catches.
        ("D0", "    # the gate table lists D1 and D2 beside "),
        # And a bare AC with no section is the defect itself: AC6 meant
        # three different criteria in three files.
        ("AC6", "            path ("),
        ("AC2", "    # so the gate is what makes \"no silent overwrite\" ("),
    ]
    for token, before in refuses:
        assert _namespace_of(token, before) is None, (
            f"{token!r} after {before.strip()!r} was accepted as a qualified "
            f"citation. The grammar has been widened past what it can tell "
            f"apart, and the citation check now passes on anything.")


def test_the_citation_scan_reads_docstrings_and_not_runtime_strings():
    """The domain, pinned. This is the survivor that mattered most.

    A scan that quietly reads LESS finds nothing wrong, and finding nothing
    wrong is exactly what passing looks like -- so narrowing _prose_lines to
    comments only leaves every check above green while the docstrings, where
    most citations actually live, stop being read at all. The mutation pass
    scored that as a survivor, which is how it got a test.

    The other direction is a real constraint, not symmetry: the pipeline's
    stage names ARE the strings "D0" and "D1", its trace lines are keyed by
    them, and test_pipeline_trace_contains_expected_lines_in_order matches
    on the "D0:" prefix. Reading runtime strings would demand a qualifier
    inside a value.
    """
    seen = dict(_prose_lines(os.path.join(ROOT, "core", "interaction.py")))
    assert 4 in seen and "AC1" in seen[4], (
        "core/interaction.py's module docstring cites §23 AC1 on line 4 and "
        "the scan no longer sees it. If the file changed, re-anchor this; if "
        "the scan changed, it is reading less than it claims and every check "
        "above is passing on a domain nobody looked at.")

    orch_path = os.path.join(ROOT, "core", "reasoning", "orchestrator.py")
    orch = dict(_prose_lines(orch_path))
    # Anchored by CONTENT, not by a literal line number (§38). It was
    # `assert 979 not in orch`, and by the time anything moved that line
    # the real `yield from _stage("D0")` had drifted to 1059 -- so the
    # assertion was passing because 979 happened to be blank, testing
    # nothing. A hardcoded line number in a test about drift is the one
    # place it cannot be excused.
    with open(orch_path, encoding="utf-8") as handle:
        stage_lines = [n for n, line in enumerate(handle, 1)
                       if 'yield from _stage("D0")' in line]
    assert stage_lines, (
        "orchestrator.py no longer contains `yield from _stage(\"D0\")`; "
        "re-anchor this test on whatever runtime id string replaced it, or "
        "the runtime-strings half of the domain stops being checked")
    for number in stage_lines:
        assert number not in orch, (
            f"orchestrator.py:{number} is `yield from _stage(\"D0\")` -- a "
            f"runtime VALUE, not a citation. Reading it would demand "
            f"`gate D0` inside a string the trace test matches on.")


def test_a_range_citation_does_not_report_its_own_endpoint():
    """The hyphen guard, whose stated reason went stale under it.

    It was written for "[a-zA-Z0-9]" in a regex literal. _prose_lines then
    stopped reading code, so that reason evaporated -- and the mutation pass
    scored the guard as dead. Measured, it still changes exactly two lines,
    both range citations, and the case it is really for is a range over a
    SPARSE family: C is C1, C3, C6, C8, C10, so "C1-C10" written in a
    comment would report five decisions that never existed.
    """
    line = "    # Recovered per C1-C10, and the I9-I12 set in §24."
    found = [m.group(1) for m in _CITATION.finditer(line)]
    assert found == ["C1", "I9"], (
        f"a range citation reported {found}; only the leading id of a range "
        f"is a citation, and the trailing one is an endpoint.")


# ---------------------------------------------------------------------------
# ---- The npm distribution channel (batch 35) ------------------------------
# ---------------------------------------------------------------------------
#
# This file's rule is that a claim earns a check by having ALREADY drifted,
# and only if the truth is something the suite can compute. The version pair
# below does not qualify on the first half, and is here anyway, because batch
# 35 is the change that CREATES the hand-maintained duplicate: one release now
# states its version in package.json and in pyproject.toml. That is the shape
# D22 exists to prevent ("one dependency list, not two") and the shape this
# file's own summary names -- THE CLAIMS THAT DRIFT ARE THE ONES SOMEBODY HAD
# TO COUNT BY HAND. Pinning it at the moment the duplicate is introduced is
# cheaper than pinning it after the first mismatched publish, which cannot be
# un-published.
#
# What is deliberately NOT here: an audit of the real tarball. That needs
# `npm pack`, and npm is not a dependency of this suite -- the suite is
# offline, Python-only, and runs in a container that has no Node. The tarball
# audit lives in scripts/prepublish-check.mjs, which runs from
# `prepublishOnly` on a machine that by definition has npm. These three check
# the allowlist as TEXT, which is what a Python suite can honestly verify.

_PACKAGE_JSON = os.path.join(ROOT, "package.json")


def _package_json() -> dict:
    with open(_PACKAGE_JSON, encoding="utf-8") as f:
        return json.load(f)


def test_the_npm_package_and_pyproject_state_the_same_version():
    """One release, two files that have to say so.

    npm cannot replace a published version, only deprecate it, so a mismatch
    discovered after the fact is permanent: the tarball would carry Python
    metadata claiming a different version than the package it shipped as.
    scripts/prepublish-check.mjs repeats this at publish time; this is the
    copy that fails in CI, before anyone types the command.
    """
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
        pyproject = f.read()

    versions = re.findall(r'^\s*version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert len(versions) == 1, (
        f"pyproject.toml states {len(versions)} bare `version = \"...\"` lines; "
        f"this check reads it by pattern, and two would make the comparison "
        f"meaningless. Found: {versions}")

    assert _package_json()["version"] == versions[0], (
        f"package.json says {_package_json()['version']!r} and pyproject.toml "
        f"says {versions[0]!r}. They describe one release -- bump both.")


def test_every_npm_allowlist_entry_exists():
    """A `files` entry naming nothing ships nothing, and says nothing.

    npm does not warn about an allowlist entry that matches no file, so a
    renamed or moved module leaves a tarball quietly missing part of the
    harness -- which presents to the user as an ImportError on first run,
    one layer away from the packaging mistake that caused it.
    """
    missing = []
    for entry in _package_json()["files"]:
        if entry.startswith("!"):        # negation guard, checked below
            continue
        if not os.path.exists(os.path.join(ROOT, entry.rstrip("/"))):
            missing.append(entry)

    assert not missing, (
        f"package.json `files` names paths that do not exist: {missing}. "
        f"Either the file moved and the allowlist did not, or the entry is a "
        f"typo -- both ship a tarball with a hole in it.")


def test_the_npm_allowlist_cannot_ship_a_secret():
    """The third copy of "keep local secrets out of a distribution".

    .gitignore covers `git push`, `.gitattributes export-ignore` covers
    `git archive` (H3/C2), and `files` covers npm. The three are separate
    mechanisms for one rule, and npm's is the one that fails OPEN: with no
    `files` key npm falls back to .gitignore, at which point providers.json is
    excluded by accident rather than by decision. An allowlist makes the
    decision explicit, and this asserts it stayed that way.

    Two halves, because the risk has two shapes. A root-level secret can only
    ship if something NAMES it. A subdirectory artifact -- __pycache__, an
    .ipynb_checkpoints copy of a prompt file -- ships because a directory
    entry swept it in, so the negation guards are what stop those.
    """
    files = _package_json()["files"]

    never_named = {"providers.json", ".env", "app.db", "logs", "logs/",
                   "output", "output/", "conftest.py", "tests", "tests/"}
    named = sorted(set(f.rstrip("/") for f in files if not f.startswith("!"))
                   & set(n.rstrip("/") for n in never_named))
    assert not named, (
        f"package.json `files` names {named}, which would put local state or "
        f"credentials in a published tarball. A published npm version cannot "
        f"be withdrawn, only deprecated.")

    required_guards = {"!**/__pycache__", "!**/*.pyc", "!**/.ipynb_checkpoints"}
    assert required_guards <= set(files), (
        f"package.json `files` is missing negation guards: "
        f"{sorted(required_guards - set(files))}. Directory entries like "
        f"`core/` and `prompts/` sweep in whatever is inside them -- npm does "
        f"not skip __pycache__ or .ipynb_checkpoints on its own, and "
        f"test_no_notebook_checkpoint_file_is_committed above exists because "
        f"a checkpoint copy of a prompt file is a real thing in this tree.")


def test_the_launcher_never_sets_the_two_variables_that_would_break_it():
    """bin/venastine.mjs composes the child's environment, and there are two
    variables it must never put in there. Both are load-bearing and neither is
    obvious from the launcher's own point of view.

    PYTHONPATH: tools/isolation.py builds its child's PYTHONPATH from the
    PARENT's resolved sys.path, deliberately -- "handing the child the paths
    the parent actually resolved is deterministic". A value injected by the
    launcher would be inherited into that computation.

    AGENT_WORKSPACE: WORKSPACE_DIR is a PERMISSION BOUNDARY, not a storage
    path. file_ops auto-approves writes inside it and requires approval
    outside. Redirecting it to a shared home directory -- the obvious "make
    it work from anywhere" move, and the one a future reader will reach for
    after seeing APP_DB_PATH handled that way -- would silently auto-approve
    writes there from every project.
    """
    launcher = os.path.join(ROOT, "bin", "venastine.mjs")
    with open(launcher, encoding="utf-8") as f:
        source = f.read()

    # An assignment into the child env, not a mention: the file documents both
    # names in comments on purpose, and this must not read those.
    for name in ("PYTHONPATH", "AGENT_WORKSPACE"):
        assignment = re.search(rf"env\.{name}\s*=(?!=)", source)
        assert assignment is None, (
            f"bin/venastine.mjs assigns env.{name}. See this test's docstring: "
            f"both variables are inherited by machinery that computes its own "
            f"value from the parent process, and setting either from the "
            f"launcher breaks that quietly rather than loudly.")
