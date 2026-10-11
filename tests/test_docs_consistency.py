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
  2. That number is the REAL one. Needs the collected total, so it skips
     when the invocation narrowed rather than failing on a legitimately
     smaller session -- EXCEPT when the invocation is THIS FILE ALONE,
     where it collects the suite in a subprocess instead.

     That exception is the entire point of tests.yml's `docs-consistency`
     job, and it was missing from it. The job runs this file alone as a
     fast gate so that "doc drift fails here instead of after the full
     15-minute suite" -- and because both count checks skipped there, it
     could not fail on drift at all. On c5fd7c2 the gate passed at 09:47
     and the full suite failed on exactly these two tests at 09:53.

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
via _was_narrowed -- unless that invocation is this file alone, which is
the fast gate's shape and gets a subprocess collection instead. The ones
that are pure text never skip, so they still run under
`pytest tests/test_foo.py` while you are mid-change.
"""

import fnmatch
import io
import json
import os
import re
import subprocess  # nosec B404 - the gates below drive git and pytest as subprocesses on purpose
import sys
from collections import Counter

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The reference record moved out of the root (batch 89). Everything the
# root still owns -- README, AGENTS, CONFIG_ARCHITECTURE, LICENSE, NOTICE --
# keeps joining ROOT; everything under docs/ joins this.
DOCS = os.path.join(ROOT, "docs")

# Anchored on the surrounding words, NOT a bare r"(\d+) tests". ARCHITECTURE
# lists a per-file count for every test module in its tree ("# 29 tests --
# ROADMAP_v2 §16 AC1-AC3..."), so a loose pattern would match the first of
# those and compare two unrelated numbers forever.
DOC_PATTERNS = {
    "README.md": r"pytest` — (\d+) tests, fully offline",
    "AGENTS.md": r"# (\d+) tests, offline",
    "docs/ARCHITECTURE.md": r"# (\d+) tests, all offline",
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


#: This file's own name, which is the one narrowing that still owes the
#: full answer rather than a skip.
_THIS_FILE = os.path.basename(__file__)


def _is_this_file_alone(config) -> bool:
    """Whether the invocation named this file and nothing else.

    `pytest tests/test_docs_consistency.py` is two things at once: the CI
    fast gate's shape, and the shape a reader uses when they want exactly
    the answer this file gives. Both are ASKING the question, so both get
    a real answer. Every other narrowing still skips -- a targeted
    `pytest -k something` must not pay for a full collection it did not
    ask for.
    """
    named = [a for a in config.args if a not in (".", "tests", "tests/")]
    return bool(named) and all(
        os.path.basename(a.split("::")[0]) == _THIS_FILE for a in named)


#: Filled once per process by _collect_in_a_subprocess.
_COLLECTED = []


def _collect_in_a_subprocess() -> list:
    """Every node id a full `pytest` would collect, from a child process.

    Measured at ~4.6s for 4261 ids, and the whole gate job at ~10s,
    which is what buys it the point: it fails on drift in seconds
    instead of after the full suite.

    pytest.ini's addopts reaches the child, so its `-m "not integration"`
    makes this the same population `session.items` holds on a full run --
    and the test below pins that equality rather than assuming it.
    `-p no:cacheprovider` keeps the child off .pytest_cache, which the
    parent may be writing at the same time. No recursion: the child
    collects and runs nothing.

    `-n0` BECAUSE addopts REACHES THE CHILD, which is the same sentence
    two lines up read the other way round. Since batch 109 that line also
    carries `-n auto --dist loadscope`, so without this the child spawns a
    worker per core to collect a list and run nothing -- pure cost, on a
    call that is already the slowest thing in the fast CI gate. It also
    keeps the parse below reading pytest's own `--collect-only -q` output
    and not a distributed run's, whatever a future xdist does with the two
    together.
    """
    if _COLLECTED:
        return _COLLECTED

    proc = subprocess.run(  # nosec B603 - re-collects this suite in a child to compare against the session
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-n0"],
        cwd=ROOT, capture_output=True, text=True,
    )
    # Node ids are rootdir-relative and always use forward slashes, so this
    # cannot pick up a summary line -- and an empty list is a FAILURE here,
    # never a count of zero.
    ids = [line.strip() for line in proc.stdout.splitlines()
           if line.startswith("tests/") and "::" in line]

    assert proc.returncode == 0 and ids, (
        f"Collecting the suite in a subprocess failed (exit "
        f"{proc.returncode}, {len(ids)} ids parsed). The count checks "
        f"cannot run without it, and reporting zero would be worse than "
        f"saying so.\n"
        f"--- stdout (tail) ---\n{proc.stdout[-3000:]}\n"
        f"--- stderr (tail) ---\n{proc.stderr[-2000:]}"
    )
    _COLLECTED.extend(ids)
    return _COLLECTED


def _collected_nodeids(request) -> list:
    """The node ids the two count checks compare against, or a skip.

    A full run already holds them, exactly and for free, and stays the
    authority. Only the fast gate pays for a subprocess.
    """
    if not _was_narrowed(request.config):
        return [item.nodeid for item in request.session.items]
    if _is_this_file_alone(request.config):
        return _collect_in_a_subprocess()
    pytest.skip("filtered invocation collects less than the full suite")


def test_this_file_alone_is_the_only_narrowing_that_gets_an_answer():
    """The same silent failure mode as the guard above, one layer out.

    Wrong in the restrictive direction and _is_this_file_alone never
    fires: the fast gate goes back to skipping, stays green through any
    drift, and looks exactly as it did while it was blind. Wrong in the
    permissive direction and every `pytest -k foo` spawns a full
    collection, which is loud and gets fixed the same afternoon.
    """
    assert _is_this_file_alone(_FakeConfig(["tests/test_docs_consistency.py"]))
    assert _is_this_file_alone(
        _FakeConfig(["tests/test_docs_consistency.py::test_x"]))

    assert not _is_this_file_alone(_FakeConfig(["."]))
    assert not _is_this_file_alone(_FakeConfig(["tests"]))
    assert not _is_this_file_alone(_FakeConfig(["tests/test_review.py"]))
    assert not _is_this_file_alone(
        _FakeConfig(["tests/test_docs_consistency.py", "tests/test_review.py"]))


def test_the_subprocess_collection_agrees_with_the_session(request):
    """The two paths into _collected_nodeids must not disagree.

    They are read by different runs -- the fast gate uses one, the full
    suite the other -- so a divergence would show up as the gate failing
    on a number the suite says is right, which reads as the gate being
    broken and gets it disabled. Compared per FILE and not just in total,
    because two offsetting errors are exactly what a total hides.
    """
    if _was_narrowed(request.config):
        pytest.skip("needs the full session to compare against")

    session = Counter(item.nodeid.split("::")[0]
                      for item in request.session.items)
    child = Counter(nodeid.split("::")[0]
                    for nodeid in _collect_in_a_subprocess())

    assert session == child, (
        "A subprocess collection disagrees with this session:\n"
        + "".join(f"  {name}: session {session[name]}, subprocess "
                  f"{child[name]}\n"
                  for name in sorted(set(session) | set(child))
                  if session[name] != child[name])
        + "The fast gate reads the subprocess and the full suite reads the "
          "session; they answer the same question or the gate is noise."
    )


def test_the_documented_count_is_the_real_one(request):
    documented = set(_documented_counts().values()).pop()
    actual = len(_collected_nodeids(request))

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
    with open(os.path.join(DOCS, "ARCHITECTURE.md"), encoding="utf-8") as f:
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
    stated = _tree_entries()
    actual = Counter(
        os.path.basename(nodeid.split("::")[0])
        for nodeid in _collected_nodeids(request)
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
    with open(os.path.join(DOCS, "ARCHITECTURE.md"), encoding="utf-8") as f:
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
    with open(os.path.join(DOCS, "ROADMAP_v2.md"), encoding="utf-8") as f:
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

    with open(os.path.join(DOCS, "ARCHITECTURE.md"), encoding="utf-8") as f:
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
    import subprocess  # nosec B404 - as above, deferred into the gate
    try:
        listed = subprocess.run(  # nosec B603, B607 - `git check-ignore` with a fixed argv; git is on PATH by assumption in a git repo
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


def test_no_source_file_carries_double_encoded_text():
    """Batch 59. `agents/tui_commands.py` had been decoded as cp1252 and
    re-encoded as UTF-8 at some point, so it literally CONTAINED the
    mojibake: `Â§` on six lines where `§` belonged, and `â€”` on three --
    two of which were strings the user reads (`/agent` and the busy
    refusal). Every sibling command module spells `§` correctly, so the
    file disagreed with the whole codebase around it and nothing said so.

    Invisible to every other check here. It is not a count, not a tree
    entry, and not a claim about the code -- the module imports, the
    tests pass, and the strings simply render wrong on screen. A reader
    diffing the file sees `§` in their editor only if their editor
    guesses the same wrong encoding twice.

    DETECTED BY ROUND TRIP rather than by a list of bad sequences: if
    `line.encode('cp1252').decode('utf-8')` both SUCCEEDS and DIFFERS,
    the line is mangled and that expression is also the repair. A correct
    line fails one of the two -- ordinary ASCII cannot decode as UTF-8
    into something else, and a genuine `§` is not cp1252-encodable at
    all. That is why this needs no vocabulary of dashes and quotes to
    keep up to date, which is exactly the doc-linting rot this file's
    header warns about.
    """
    suspicious = ("Ã", "â", "Â")   # the lead bytes mojibake always starts with
    extensions = {".py", ".md", ".json", ".tcss", ".mjs", ".toml", ".ini"}
    mangled = []

    for dirpath, _dirnames, filenames in _walk_project():
        for name in filenames:
            if os.path.splitext(name)[1] not in extensions:
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if not any(c in line for c in suspicious):
                    continue
                try:
                    repaired = line.encode("cp1252").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if repaired != line:
                    rel = os.path.relpath(path, _project_root())
                    mangled.append((rel, n, line.strip(), repaired.strip()))

    assert not mangled, (
        "double-encoded text (UTF-8 read as cp1252, then re-encoded):\n"
        + "".join(f"  {rel}:{n}\n    is: {bad[:90]}\n"
                  f"    should be: {good[:90]}\n"
                  for rel, n, bad, good in mangled)
        + "The repair is the same round trip: "
          "line.encode('cp1252').decode('utf-8')."
    )

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

_RECORD_DOCS = ("ROADMAP.md", "ROADMAP_v2.md", "ROADMAP_v3.md")

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
        path = os.path.join(DOCS, name)
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
    line = next((ln for ln in text.split("\n")
                 if "Design Decisions Record" in ln and "ROADMAP" in ln), None)
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
        f"ROADMAP.md, ROADMAP_v2.md or ROADMAP_v3.md: "
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


def test_the_test_extra_matches_requirements_test():
    """S18-07, batch 136. The suite's own packages in two files, pinned to
    each other so they cannot drift.

    They used to be at the bottom of requirements.txt, which
    `dynamic = ["dependencies"]` reads, so pytest, pytest-mock,
    pytest-asyncio, pytest-xdist and execnet were HARD REQUIREMENTS of the
    distribution: `pip install venastine-research-harness` resolved a test
    runner, and bin/venastine.mjs installed the same set into every end
    user's venv.

    TWO COPIES IS THE DELIBERATE TRADE, because setuptools'
    `optional-dependencies` is all-or-nothing -- making `test` read a file
    would force `documents` and `pdf` into files of their own. #144's rule is
    not that a duplicate is always wrong, it is that a hand-maintained one is
    only as good as the gate over it. This is the gate.

    It compares the REQUIREMENT STRINGS, not just the names, so a floor
    raised in one file and not the other fails here rather than at whichever
    install happens to resolve differently.
    """
    def requirements(text):
        found = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                found.append(line)
        return sorted(found)

    with open(os.path.join(ROOT, "requirements-test.txt"),
              encoding="utf-8") as f:
        from_file = requirements(f.read())

    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
        pyproject = f.read()
    block = re.search(r"^test = \[(.*?)\]", pyproject, re.MULTILINE | re.DOTALL)
    assert block, (
        "pyproject.toml has no `test = [...]` extra. If the group was renamed, "
        "repoint this test -- do not delete it: the names it guards are what "
        "stopped shipping to end users in batch 136.")
    from_pyproject = sorted(re.findall(r'"([^"]+)"', block.group(1)))

    # Vacuity guard. An empty file or an empty extra would make the
    # comparison below pass while the suite's packages were declared nowhere.
    assert len(from_file) >= 4, (
        f"requirements-test.txt names only {from_file}; the suite needs at "
        f"least pytest, pytest-mock, pytest-asyncio and pytest-xdist")

    assert from_file == from_pyproject, (
        f"requirements-test.txt and pyproject.toml's `test` extra disagree.\n"
        f"  only in requirements-test.txt: "
        f"{sorted(set(from_file) - set(from_pyproject))}\n"
        f"  only in the pyproject extra:   "
        f"{sorted(set(from_pyproject) - set(from_file))}\n"
        f"They are the same four packages said twice -- change both.")


def test_no_test_package_is_a_runtime_dependency():
    """The other half of S18-07, and the one that would catch the regression
    rather than the drift.

    The gate above compares two files that both describe the TEST set; this
    one asserts the test set is not in the SHIPPED set. Adding
    `pytest` back to requirements.txt -- or adding a sixth test package there
    by habit -- puts it in `[project].dependencies` again and into every end
    user's venv, and nothing else in this suite would notice.
    """
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
        shipped = [line.strip() for line in f
                   if line.strip() and not line.strip().startswith("#")]

    # Not a blanket "pytest" substring match: `defusedxml` lives in the
    # shipped set deliberately and a future comment could mention pytest.
    offenders = [line for line in shipped
                 if re.match(r"^(pytest|execnet|pluggy|iniconfig)\b", line)]
    assert not offenders, (
        f"{offenders} are in requirements.txt, which pyproject.toml reads for "
        f"`[project].dependencies` -- so they are hard requirements of the "
        f"published package and of every venv bin/venastine.mjs builds. Test "
        f"packages belong in requirements-test.txt (S18-07, batch 136).")

    # And the shipped set must still carry the one that LOOKS like a test
    # dependency and is not: arxiv.py imports defusedxml at module load.
    assert any(line.startswith("defusedxml") for line in shipped), (
        "defusedxml left requirements.txt. It is a PRODUCTION dependency -- "
        "tools/builtin/arxiv.py imports it at module load -- and it sat under "
        "a 'Test dependencies' heading for twenty batches, which is exactly "
        "how it would get tidied away by mistake (TECHNICAL_DEBT 18).")


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

    # DERIVED, since batch 140. This set was the FOURTH hand-written copy of
    # "keep local secrets out of a distribution" -- and the one that was
    # missing `secrets.json` and the WAL sidecars while both git channels had
    # them. It now comes from package.json's `neverDistribute`, which all
    # three channels read. `conftest.py` and `tests` are kept by hand: they
    # are not credential-bearing, they are the npm channel's own concern
    # (ROOT_PY_NOT_SHIPPED in prepublish-check.mjs says why for conftest),
    # and the list they belong to is this one rather than the shared one.
    never_named = {"conftest.py", "tests", "tests/"}
    for entry in _never_distribute():
        if "npm" in entry["channels"]:
            never_named.update(entry["samples"])
            never_named.update(entry["value"])
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


# ---------------------------------------------------------------------------
# ---- S18-01 / S18-08: one exclusion list, three channels, driven ----------
# ---------------------------------------------------------------------------
#
# "Keep local secrets out of a distribution" had FIVE copies and no two
# agreed, while two documents claimed three. The disagreements were not
# cosmetic: `secrets.json` was in both git channels and in neither npm-side
# list, `.venastine/mcp.json` was in none of the five although `/init
# --config` writes it and README names `Authorization` as what rides in it,
# and the npm database test was `endsWith('.db')`, which misses the `-wal`
# sidecar that both git channels had already been fixed for.
#
# package.json's `neverDistribute` is now the one list. These gates check the
# channels against it rather than against each other.

_NEVER_DISTRIBUTE_FIELDS = {"label", "match", "value", "channels", "samples"}
_CHANNELS = {"git", "archive", "npm"}


def _never_distribute() -> list:
    """The one list, read from where both consumers read it."""
    entries = _package_json().get("neverDistribute")
    assert entries, (
        "package.json has no `neverDistribute` list. It is the single source "
        "for three exclusion channels (batch 140, S18-01/S18-08); if it was "
        "renamed, repoint these gates -- do not delete them, because the "
        "thing they replaced was five hand-written lists that disagreed.")
    return entries


def test_every_never_distribute_entry_is_well_formed_and_drivable():
    """An entry nothing can test is an entry that stops being enforced.

    `samples` is required for exactly that reason: it is what the two gates
    below drive, so a pattern cannot be added without a concrete path that
    demonstrates it.
    """
    entries = _never_distribute()
    assert len(entries) >= 10, (
        f"only {len(entries)} entries; this list covers the union of "
        f".gitignore, .gitattributes export-ignore and the npm denylist, so a "
        f"short list means something was dropped rather than tidied")

    for entry in entries:
        missing = _NEVER_DISTRIBUTE_FIELDS - set(entry)
        assert not missing, f"{entry.get('label')!r} is missing {missing}"
        assert entry["value"], f"{entry['label']!r} has no patterns"
        assert entry["samples"], (
            f"{entry['label']!r} has no samples, so no gate can drive it")
        unknown = set(entry["channels"]) - _CHANNELS
        assert not unknown, (
            f"{entry['label']!r} names unknown channels {unknown}; the three "
            f"are git (push), archive (git archive) and npm (the tarball)")
        assert entry["channels"], (
            f"{entry['label']!r} excludes nothing from anywhere")


def test_the_push_channel_ignores_every_sample_that_claims_it():
    """DRIVEN through `git check-ignore`, so git does the matching and this
    gate re-implements none of it.

    The control is inside the same mechanism: a path no rule covers makes
    check-ignore exit non-zero and print nothing, which is how
    `.venastine/mcp.json` was found -- it was absent from the tree's only
    `.gitignore` while four sibling paths resolved to a rule.
    """
    import subprocess  # nosec B404 - as above, deferred into the gate

    claimed = [(e["label"], s) for e in _never_distribute()
               if "git" in e["channels"] for s in e["samples"]]
    assert claimed, "no entry claims the git channel, so this proves nothing"

    unignored = []
    for label, sample in claimed:
        proc = subprocess.run(  # nosec B603, B607 - `git` with a fixed argv, as above
            ["git", "check-ignore", "-q", "--", sample],
            cwd=ROOT, capture_output=True)
        if proc.returncode != 0:
            unignored.append(f"{sample} ({label})")

    assert not unignored, (
        f"package.json `neverDistribute` claims the git channel for these, "
        f"and `.gitignore` matches none of them: {unignored}. A first-party "
        f"command writes some of these into a user's project, so `git add -A` "
        f"from the project root would stage them -- and a published "
        f"credential is remedied by rotation, not by a later commit.")

    # The positive control: a path that must NOT be ignored, because the
    # narrow pair was chosen over `.venastine/` precisely so project-tier
    # agents and skills keep travelling with a repository (D17's trust prompt
    # is the mechanism that exists for them).
    proc = subprocess.run(  # nosec B603, B607 - `git` with a fixed argv, as above
        ["git", "check-ignore", "-q", "--", ".venastine/agents/example.md"],
        cwd=ROOT, capture_output=True)
    assert proc.returncode != 0, (
        "`.venastine/agents/` is ignored. The two credential files are "
        "excluded by name on purpose -- excluding the directory wholesale "
        "makes project-tier agents and skills untrackable, which is the "
        "feature workspace trust exists to serve.")


def test_the_archive_channel_excludes_every_sample_that_claims_it():
    """TEXTUAL, and the reason is measured rather than assumed.

    `git check-attr export-ignore` is NOT a faithful driver here. Measured in
    batch 140 against a throwaway repository: `logs/ export-ignore` makes
    `git archive` exclude `logs/a.txt`, while `check-attr` reports
    `unspecified` for both `logs` and `logs/a.txt` -- archive prunes the tree
    ENTRY, and attributes apply to files. A gate built on check-attr would
    have reported two working entries as broken and invited a "fix" to them.

    Driving `git archive` instead would be faithful and vacuous: it ships only
    TRACKED content, and every path here is untracked, so each would be absent
    whatever `.gitattributes` said.

    So this reads the patterns. The comparison is deliberately crude -- equal,
    or a directory prefix -- because the patterns in that file are, and a
    cleverer matcher here would be the second implementation this whole batch
    removes.
    """
    with open(os.path.join(ROOT, ".gitattributes"), encoding="utf-8") as f:
        patterns = [line.split()[0].rstrip("/")
                    for line in f
                    if "export-ignore" in line and not line.startswith("#")]
    assert patterns, (
        ".gitattributes states no export-ignore patterns; if the format "
        "moved, repoint this -- H3/C2 is the channel it covers")

    def covered(sample: str) -> bool:
        """Equal, a directory prefix, or a glob -- on the whole path or on
        the basename, because `*.db` is written to match `app.db` anywhere.

        An APPROXIMATION of git's gitattributes matching, stated as one:
        `fnmatch`'s `*` crosses `/` where git's does not, so this is the
        permissive direction. That is the right way round for a gate whose
        job is to find patterns that cover NOTHING -- it cannot produce a
        false red, only miss a pattern that is narrower than it looks.
        """
        base = os.path.basename(sample)
        return any(sample == p
                   or sample.startswith(p + "/")
                   or fnmatch.fnmatch(sample, p)
                   or fnmatch.fnmatch(base, p)
                   for p in patterns)

    missing = [f"{s} ({e['label']})"
               for e in _never_distribute() if "archive" in e["channels"]
               for s in e["samples"] if not covered(s)]

    assert not missing, (
        f"`neverDistribute` claims the archive channel for these and "
        f".gitattributes carries no export-ignore pattern for them: "
        f"{missing}. That channel is `git archive` and GitHub's "
        f"\"Download ZIP\".")


def test_the_npm_gate_derives_its_denylist_rather_than_restating_it():
    """The structural half, and the one that keeps the others honest.

    `scripts/prepublish-check.mjs` used to carry its own FORBIDDEN literal,
    and that copy is where `secrets.json` went missing and where the `.db`
    test failed to cover `.db-wal`. A gate that only compared the lists would
    pass again the moment somebody re-inlined one.
    """
    with open(os.path.join(ROOT, "scripts", "prepublish-check.mjs"),
              encoding="utf-8") as f:
        source = f.read()

    assert "pkg.neverDistribute" in source, (
        "prepublish-check.mjs no longer reads package.json's "
        "`neverDistribute`. If it grew its own list again, that is the fourth "
        "copy returning -- the one that was missing secrets.json and the WAL "
        "sidecars while both git channels had them.")

    assert "const FORBIDDEN = [" not in source, (
        "prepublish-check.mjs declares a literal FORBIDDEN array again. The "
        "list is package.json's; this file applies it.")

    # Every match kind the data uses has to exist in the consumer, or the
    # consumer throws at publish time -- which is correct behaviour and a
    # terrible moment to discover it.
    kinds = {entry["match"] for entry in _never_distribute()}
    for kind in sorted(kinds):
        assert f"{kind}:" in source or f"'{kind}':" in source, (
            f"package.json uses match kind {kind!r} and "
            f"prepublish-check.mjs has no matcher for it. The consumer throws "
            f"on an unknown kind by design, so this would abort the publish "
            f"rather than silently skip the pattern -- but the publish is the "
            f"wrong place to find out.")


# ---------------------------------------------------------------------------
# ---- S18-02..S18-05: CI properties nothing in the repo checked ------------
# ---------------------------------------------------------------------------
#
# Five findings in this scope were CI properties that no file asserted, which
# is why they survived a hundred commits. Each fix here is the property plus
# something that notices when it stops holding.

_WORKFLOW_DIR = os.path.join(ROOT, ".github", "workflows")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _workflow_text() -> dict:
    out = {}
    for name in sorted(os.listdir(_WORKFLOW_DIR)):
        if name.endswith((".yml", ".yaml")):
            with open(os.path.join(_WORKFLOW_DIR, name), encoding="utf-8") as f:
                out[name] = f.read()
    assert out, "no workflows found; repoint this if .github/ moved"
    return out


def test_every_action_reference_is_pinned_to_a_commit():
    """S18-05. A tag is a mutable ref held by a third party.

    All 24 `uses:` lines were floating major tags, including the three in
    `publish.yml` -- the job that holds `id-token: write` and will hold
    `NODE_AUTH_TOKEN`. The rule was already written down in `gitleaks.yml`
    ("D22's exact-pin rule applies to CI tooling too"), in a file whose own
    checkout was floating.

    The trailing `# vX.Y.Z` comment is required too, and not for tidiness: a
    bare 40-hex is unreadable in review, and it is the form Dependabot
    updates in place.
    """
    unpinned, uncommented = [], []
    for name, text in _workflow_text().items():
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- uses:") and " uses:" not in stripped:
                continue
            if "uses:" not in stripped:
                continue
            ref = stripped.split("uses:", 1)[1].strip()
            target = ref.split("#")[0].strip()
            if "@" not in target:
                unpinned.append(f"{name}: {target}")
                continue
            digest = target.rsplit("@", 1)[1]
            if not _SHA40.match(digest):
                unpinned.append(f"{name}: {target}")
            elif "#" not in ref:
                uncommented.append(f"{name}: {target}")

    assert not unpinned, (
        f"these `uses:` references are not pinned to a commit: {unpinned}. A "
        f"tag can be repointed by whoever owns it and the next run inherits "
        f"the new code with no diff here.")
    assert not uncommented, (
        f"these pins carry no trailing version comment: {uncommented}. "
        f"`uses: owner/action@<sha>  # v1.2.3` is what makes a pin readable "
        f"in review and is the form Dependabot rewrites in place.")

    # Vacuity guard: this swept something.
    pinned = sum(len([1 for line in text.splitlines() if "uses:" in line])
                 for text in _workflow_text().values())
    assert pinned >= 20, (
        f"only {pinned} `uses:` lines found across the workflows; the sweep "
        f"is not reaching them")


def test_dependabot_can_update_a_pinned_action():
    """The other half of S18-05, and the reason pinning is not a trap.

    SHA-pinning without an update channel means the pins are never moved
    again. `dependabot.yml` declared `pip` and `npm` only, so nothing
    proposed action bumps -- pinned or not.
    """
    with open(os.path.join(ROOT, ".github", "dependabot.yml"),
              encoding="utf-8") as f:
        text = f.read()
    assert "github-actions" in text, (
        "dependabot.yml declares no `github-actions` ecosystem, so the SHA "
        "pins the gate above requires would never be updated again. That "
        "trades one risk for another.")


def test_the_publish_path_scans_for_secrets_before_publishing():
    """S18-03. A release tag published with no secret scan on the commit.

    `publish.yml` declares no `needs:` and reads no other workflow's verdict,
    so the scanners ran CONCURRENTLY with it -- and `npm publish` is
    irreversible. Measured on the one release tag that exists, v0.2.0: two
    runs, tests and publish, no scanner.

    It is not redundant with `prepublish-check.mjs`, which matches on
    FILENAMES: a credential pasted into a file that is SUPPOSED to ship is
    named by no entry of its denylist.
    """
    text = _workflow_text()["publish.yml"]
    assert "gitleaks detect" in text, (
        "publish.yml does not run gitleaks. The tarball check beside it "
        "matches filenames, so a credential inside a file that ships is not "
        "something it can see.")

    scan = text.index("gitleaks detect")
    publish = text.rindex("npm publish")
    assert scan < publish, (
        "publish.yml runs the secret scan AFTER `npm publish`. A published "
        "npm version can never be replaced, only deprecated.")

    # The scan must come after the step that plants the fake credentials file
    # for the suite, or it is scanning a deliberately-planted secret and
    # passing only because gitleaks.toml happens to allowlist that name.
    removal = text.index("rm -f providers.json")
    assert removal < scan, (
        "publish.yml scans for secrets BEFORE removing the providers.json it "
        "copied in for the suite. That passes only because gitleaks.toml "
        "allowlists the filename -- which is the 'it cannot happen because of "
        "a list somewhere else' assumption the removal step itself refuses to "
        "make.")

    assert "--source ." in text, (
        "the scan must use a RELATIVE --source: an absolute path makes every "
        ".gitleaksignore fingerprint unmatchable (measured).")


def test_the_bandit_job_checks_counts_as_well_as_the_baseline():
    """S18-02. `-b` suppresses by (file, test id, message), so a second
    finding of the same id in the same file is silent -- measured,
    `security/sandbox.py` went 7 -> 18 B603 with this job reporting nothing.
    """
    text = _workflow_text()["bandit.yml"]
    # The INVOCATION, not the name: the comment block above the step names
    # the script too, so a substring check passes with the step disabled.
    # Measured -- mutation A5 survived the first version of this gate.
    assert "run: python scripts/bandit-count-check.py" in text, (
        "bandit.yml does not run scripts/bandit-count-check.py, so a second "
        "subprocess call in a file that already carries a baselined B603 is "
        "reported by nothing.")
    assert "-b .github/bandit-baseline.json" in text, (
        "the count check runs BESIDE `-b`, not instead of it: that one "
        "matches on message text and still catches a reworded finding at an "
        "unchanged count.")
    assert os.path.exists(os.path.join(ROOT, "scripts",
                                       "bandit-count-check.py")), (
        "the script the job runs does not exist")


def test_pip_audit_covers_every_declared_dependency_file():
    """S18-04, and the half batch 136 created.

    The job audited `requirements.txt` and its header called that "the one
    dependency list". Batch 136 split the suite's packages into
    `requirements-test.txt` and declared them as pyproject's `test` extra, so
    four distributions were audited by nothing.
    """
    text = _workflow_text()["pip-audit.yml"]
    declared = [name for name in os.listdir(ROOT)
                if name.startswith("requirements") and name.endswith(".txt")]
    assert len(declared) >= 2, (
        f"expected at least requirements.txt and requirements-test.txt, "
        f"found {declared}")
    missing = [name for name in declared if f"-r {name}" not in text]
    assert not missing, (
        f"pip-audit.yml audits neither of {missing}. A requirements file "
        f"nothing audits is a dependency set with no supply-chain gate, and "
        f"the whole point of this job is that it is the only one.")


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


def test_the_launcher_and_python_agree_about_the_config_state():
    """Batch 86, narrowed in batch 87. Every name spelled in two languages is
    a drift that fails silently in the worst way -- the launcher writing to a
    path the merge never reads means `npm update` goes back to discarding
    every setting with nothing failing anywhere.

    So batch 87 removed the duplication that mattered. The launcher no longer
    writes the mirror (it spawns `--mirror` and Python decides), and it no
    longer computes a state path (it READS `installs.json`, which Python
    writes). What is left is a read-only doctor line, and these are the only
    strings it still has to know.
    """
    import config_update

    with open(os.path.join(ROOT, "bin", "venastine.mjs"), encoding="utf-8") as f:
        source = f.read()

    for spelling in (config_update.STATE_DIRNAME, config_update.VERSION_FILE,
                     config_update.REPORT_FILE, config_update.INSTALLS_FILE):
        assert f"'{spelling}'" in source, (
            f"bin/venastine.mjs no longer spells {spelling!r}, which "
            f"config_update.py still writes. The doctor line reads these; a "
            f"name only one side believes in reports the wrong directory.")

    assert "'--mirror'" in source, (
        "the launcher no longer spawns the post-session mirror. Without it a "
        "/config write made in a session is lost by an `npm update` that "
        "happens before the next launch -- the common upgrade order.")
    assert config_update.YOURS_FILE not in source, (
        "bin/venastine.mjs names yours.yaml again. Writing the mirror from "
        "Node is what batch 87 removed: it was the one writer not subject to "
        "rule 1, and it destroyed the user's settings after a failed merge.")


# ---------------------------------------------------------------------------
# ---- hand-counted roster numbers (the September 2026 drift) -----------------
# ---------------------------------------------------------------------------

_SMALL_NUMBERS = {
    "three": 3, "nine": 9, "eleven": 11, "twelve": 12, "fifteen": 15,
    "nineteen": 19, "twenty-one": 21, "thirty-five": 35,
}


def _word_number(word):
    word = word.lower()
    if word.isdigit():
        return int(word)
    assert word in _SMALL_NUMBERS, (
        f"a doc claim counts {word!r}, which is outside the small parser. "
        f"Extend _SMALL_NUMBERS rather than working around it.")
    return _SMALL_NUMBERS[word]


def test_the_documented_provider_roster_matches_the_example():
    """Three prose numbers described one roster three different ways: AGENTS.md
    said nineteen providers, ARCHITECTURE.md and ROADMAP.md said fourteen,
    and providers.json.example held fifteen -- with the stream-usage trio
    right in every telling, so each paragraph read as careful while
    disagreeing about its own denominator. The same drift lived in
    tui/meters.py's and tui/app.py's comments, which is how a reader met the
    wrong number whichever document they opened first.

    Unit 16's shape again: somebody counted the roster by hand, in five
    places, and the roster moved. So this parses the claimed totals out of
    the live claims and compares them against the example file itself --
    never against another hand-written number.

    DEVLOG.md is deliberately out of scope: it is append-only history, and a
    batch entry's contemporary count was true when written. tests/ is out for
    the same reason the tree test skips it -- docstrings there describe the
    behaviour under test, they are not a claim an agent navigates by.
    """
    with open(os.path.join(ROOT, "providers.json.example"),
              encoding="utf-8") as f:
        example = json.load(f)
    live_total = len(example)
    live_true = sorted(name for name, entry in example.items()
                       if entry.get("supports_stream_usage"))

    with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
        agents = f.read()
    total = _word_number(re.search(
        r"THREE of the (\w+) configured providers", agents).group(1))
    complement = _word_number(re.search(
        r"the other (\w+) -- D21", agents).group(1))
    assert (total, complement) == (live_total, live_total - len(live_true)), (
        f"AGENTS.md's meter bullet claims {total} providers with "
        f"{complement} complements; providers.json.example holds "
        f"{live_total} with {live_total - len(live_true)} complements.")
    assert len(live_true) == 3 and live_true == [
            "ANTHROPIC", "OPENAI", "QWEN_TOKEN_PLAN"], (
        f"the stream-usage trio changed: {live_true}. The bullet's THREE is "
        f"stale alongside its denominator -- update both, not one.")

    for doc, pattern in (
            ("ARCHITECTURE.md",
             r"across the (\d+) providers in `providers.json.example`"),
            ("ROADMAP.md",
             r"Across the (\d+) providers in `providers.json.example`")):
        with open(os.path.join(DOCS, doc), encoding="utf-8") as f:
            hit = re.search(pattern, f.read())
        assert hit is not None, (
            f"{doc} no longer states the roster size where it used to -- "
            f"if the sentence moved, move this pattern with it.")
        assert int(hit.group(1)) == live_total, (
            f"{doc} claims {hit.group(1)} providers; the example holds "
            f"{live_total}.")

    for path, patterns in (
            ("tui/meters.py", [r"(\w+) configured providers",
                               r"forever on\s+(\w+) providers"]),
            ("tui/app.py", [r"is (\w+) of the (\w+) configured",
                             r"nothing on (\w+) of the (\w+)"])):
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            source = f.read()
        claimed = set()
        for pattern in patterns:
            for hit in re.finditer(pattern, source):
                claimed.update(_word_number(g) for g in hit.groups())
        assert claimed <= {live_total, live_total - len(live_true)}, (
            f"{path} counts {sorted(claimed)} against a live roster of "
            f"{live_total} with {live_total - len(live_true)} complements.")


def test_the_held_widget_count_matches_the_prose():
    """Batch 76 wrote "seven" and §47 added three more holds without
    revisiting the sentence -- the transcript, the six crash-path widgets,
    then the thread viewer, the crumb and the pane switcher, all held at
    mount with a never-mounted fallback. A count that understates the set
    tells the next author the set is closed, which is exactly how the two
    NoMatches crashes happened: a widget added as a per-event query on a
    path that runs under a modal.

    The live value is the holds themselves, not a second list: every
    `self._x = self.query_one(...)` in tui/app.py, all of which sit in
    on_mount today, so a tenth hold anywhere in the file trips this and a
    human decides whether the sentence follows it.
    """
    with open(os.path.join(ROOT, "tui", "app.py"), encoding="utf-8") as f:
        app_source = f.read()
    live = len(re.findall(r"self\._\w+ = self\.query_one\(", app_source))

    with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
        agents = f.read()
    claimed = _word_number(re.search(
        r"The held set is (\w+) now", agents).group(1))
    assert claimed == live, (
        f"AGENTS.md says the held set is {claimed}; tui/app.py holds "
        f"{live}. Name the new hold in the bullet, or move it to a query "
        f"with its NoMatches guard -- the count is the tripwire, the "
        f"preconditions sentence beside it is the argument.")


def _sections_missing_from_the_index(record):
    """(headings, the section numbers with no index row) for one roadmap.

    One `## N.` heading per section, in either of the two spellings the
    record uses (`## 13.` for the older sections, `## §39.` for the newer
    ones). The index block is everything between `## Index` and its closing
    rule, in both of its row spellings (`- 13.` and `- **§32.**`).
    """
    with open(os.path.join(DOCS, record), encoding="utf-8") as f:
        text = f.read()
    headings = set(int(n) for n in
                   re.findall(r"^## (?:§)?(\d+)\.", text, re.M))
    index_block = text.split("## Index")[1].split("---")[0]
    indexed = set(int(n) for n in re.findall(r"§(\d+)\.", index_block))
    indexed.update(int(n) for n in
                   re.findall(r"^- (\d+)\.", index_block, re.M))
    return headings, sorted(headings - indexed)


def test_the_roadmap_v2_index_covers_every_section():
    """TECHNICAL_DEBT's index entry, closed 2026-09-10: the index stopped at
    §31 while the record ran to §47, then backfills reached §45 and left
    §39, §40, §46 and §47 absent -- the document a reader consults to find
    out what is outstanding, missing the security posture and all of the
    agent-navigation work. The marker test cannot see this: it checks the
    entries it found, and an absent row has no marker to miss.

    The prescription the debt entry recorded is the check: one index row per
    section heading.
    """
    headings, missing = _sections_missing_from_the_index("ROADMAP_v2.md")
    assert headings, "no section headings parsed -- the pattern moved."
    assert not missing, (
        f"ROADMAP_v2.md sections without an index row: {missing}. A section "
        f"missing from the index reads as nonexistent, which is #129's "
        f"complaint one level up.")


def test_the_roadmap_v3_index_covers_every_section():
    """ROADMAP_v3 §49 (SS21) opened a third record document, and the two
    index checks were written against ROADMAP_v2 by NAME -- so without
    this, v3 could grow a section its index never lists, the exact drift
    the v2 check was written after."""
    headings, missing = _sections_missing_from_the_index("ROADMAP_v3.md")
    assert 49 in headings, (
        "ROADMAP_v3.md parsed no §49 heading -- it opens at §49, so the "
        "pattern moved or the document did.")
    assert not missing, (
        f"ROADMAP_v3.md sections without an index row: {missing}.")


def test_every_roadmap_v3_index_entry_carries_a_status_marker():
    """#129's rule for the third document. v3 writes every row in the bold
    `- **§49. Title** — **(STATUS)**` spelling, which `_INDEX_ENTRY` (the
    `- 13.` spelling) never matches -- so the v2 marker test cannot see a
    v3 row at all. The marker is the bolded parenthetical AFTER the title,
    since the title is itself bold."""
    with open(os.path.join(DOCS, "ROADMAP_v3.md"), encoding="utf-8") as f:
        block = f.read().split("## Index")[1].split("---")[0]
    rows = re.findall(r"^- \*\*§\d+\..*$", block, re.M)
    assert rows, "ROADMAP_v3.md's index parsed no rows -- the spelling moved."
    unmarked = [row[:40] for row in rows
                if "**(" not in row.split("**", 2)[2]]
    assert not unmarked, (
        f"ROADMAP_v3.md index rows with no status marker: {unmarked}.")


def test_an_index_status_marker_agrees_with_its_own_section():
    """The marker test above checks a marker EXISTS. It does not check that
    the marker is true, and that gap shipped twice.

    Batch 105 found `§49`'s index row still reading `IN PROGRESS` and still
    stopping at slice 4, while the Slices list inside §49 said "§49 is
    complete" -- written by batch 104, which had also written the slice list.
    Batch 103 missed it before that. The index is the thing a reader
    consults to find out what is outstanding, so a stale marker there is
    worse than none: it answers, and the answer is wrong.

    The check is deliberately narrow, because a general "is this status
    accurate" test cannot exist. A section that declares itself complete IN
    ITS OWN BODY may not be advertised as in progress or unbuilt by its own
    index row. That is a contradiction inside one document, which is
    decidable, and it is exactly the drift that occurred.
    """
    with open(os.path.join(DOCS, "ROADMAP_v3.md"), encoding="utf-8") as f:
        text = f.read()

    index_block = text.split("## Index")[1].split("---")[0]
    rows = {}
    for match in re.finditer(r"^- \*\*§(\d+)\..*$", index_block, re.M):
        rows[int(match.group(1))] = match.group(0)
    assert rows, "ROADMAP_v3.md's index parsed no rows -- the spelling moved."

    # Each section's own body, from its heading to the next one.
    bodies = {}
    starts = [(int(m.group(1)), m.start())
              for m in re.finditer(r"^## §(\d+)\.", text, re.M)]
    for i, (number, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        bodies[number] = text[start:end]

    STALE = ("IN PROGRESS", "not built", "NOT BUILT")
    contradictions = []
    for number, body in bodies.items():
        declares_complete = (
            f"§{number} is complete" in body
            or f"§{number} is COMPLETE" in body)
        row = rows.get(number, "")
        if declares_complete and any(word in row for word in STALE):
            contradictions.append(
                f"§{number}: its body says it is complete, its index row "
                f"says {[w for w in STALE if w in row]}")

    assert not contradictions, (
        "ROADMAP_v3.md's index contradicts its own sections:\n  "
        + "\n  ".join(contradictions)
        + "\nThe index is what a reader consults for what is outstanding; a "
          "marker that is stale answers the question wrongly rather than "
          "leaving it open.")


def test_the_revisit_note_covers_the_ensemble_family():
    """L1's shape, and the hole in the range test's scope: ROADMAP.md's §10
    revisit blockquote said "Decisions record: E1-E12" while the record held
    E1-E14, and test_a_claimed_range_covers_its_whole_family only reads
    AGENTS.md's map line -- a range anywhere else can stop short while every
    range check stays green.

    Narrow on purpose: this pins the one out-of-scope range that already
    drifted, against the record's live maximum, rather than rescanning every
    document for ranges nobody has mis-stated yet.
    """
    record = _decision_definitions()
    highest = max(int(re.match(r"E(\d+)$", one).group(1))
                  for one in record if re.match(r"E\d+$", one))

    with open(os.path.join(DOCS, "ROADMAP.md"), encoding="utf-8") as f:
        roadmap = f.read()
    hit = re.search(r"Decisions record: E1[–-]E?(\w+)", roadmap)
    assert hit is not None, (
        "ROADMAP.md's revisit note no longer carries its decisions-record "
        "range -- if the sentence moved, move this pattern with it.")
    assert _word_number(hit.group(1)) >= highest, (
        f"ROADMAP.md's revisit note runs E1-E{hit.group(1)} while the "
        f"record defines through E{highest}.")


# ---------------------------------------------------------------------------
# ---- Documented ceilings against the live config (batch 83) ---------------
# ---------------------------------------------------------------------------
#
# Two clusters of documented numbers had been wrong for months, and nothing
# anywhere compared a number in prose to the value that binds:
#
#   * the maths-tool wall clock ships 20 and README said 15 in three places,
#     including a sample of output the harness cannot emit, while README's own
#     settings table said 20;
#   * the sandbox ships 120 s and 2048 MB and README said 60 s and 1024 MB in
#     three places, its settings table among them.
#
# That earns a check, by this file's own rule: a claim earns one by having
# ALREADY drifted, and only if the truth is something the suite can compute.
#
# EXPLICIT TRIPLES, NOT A REGEX OVER PROSE. A scraped version was tried first
# and rejected: it reported 25 candidate passages that were almost all section,
# issue and batch numbers, and it MISSED the real bug, because `15s` is glued
# to a letter and the pattern wanted a word boundary. A detector that quiet
# would be read as a clean bill of health, which is worse than no detector.
#
# Each row asserts BOTH halves. The substring must be present -- otherwise a
# reword silently retires the check -- and the number inside it must equal the
# live value. `{}` is where the number goes.
DOCUMENTED_CEILINGS = [
    ("README.md", "tool_compute_timeout_s",
     "stopped — {} seconds by default"),
    ("README.md", "tool_compute_timeout_s",
     "symbolic_math exceeded its {}s limit"),
    ("README.md", "tool_compute_timeout_s",
     "killable subprocesses under a {}-second wall clock"),
    ("README.md", "tool_compute_timeout_s",
     "| `tool_compute_timeout_s` | {} |"),
    ("README.md", "sandbox_timeout_seconds",
     "200 processes, {} seconds, no network"),
    ("README.md", "sandbox_timeout_seconds",
     "the container runs under a {}-second wall clock"),
    ("README.md", "sandbox_timeout_seconds",
     "`docker.io/library/python:3.13-slim`; {} s,"),
    ("README.md", "sandbox_memory_mb", "wall clock, {} MB of memory"),
    ("README.md", "sandbox_memory_mb", "s, {} MB, 1 CPU core"),
    ("README.md", "sandbox_max_pids", "{} processes, 120 seconds"),
    ("README.md", "sandbox_cpu_seconds",
     "`sandbox_cpu_seconds` ({} CPU-s) is a budget"),
    # S02-08 (batch 142). The prose three pages up states this
    # number too, and nothing matched it: `grep -c "30 CPU-seconds"` -> 1
    # and `grep -c "30 CPU-s,"` -> 1, two separate claims with one gate.
    # Changing the value in config.yaml turned the table row red and
    # left the sentence saying 30 for ever, which is the failure this
    # gate's own docstring names.
    ("README.md", "sandbox_cpu_seconds",
     "{} CPU-seconds and the same memory ceiling"),
    ("README.md", "max_read_chars", "truncated at {:,} characters"),
    ("README.md", "default_context_window", "| {}k fallback |"),
    ("CONFIG_ARCHITECTURE.md", "tool_compute_timeout_s",
     "{}s is chosen against measurement"),
]


def _ceiling_shown(template: str, value) -> str:
    """Render the number the way the document writes it."""
    if "{:,}" in template:
        return template.format(value)
    if "{}k" in template:
        return template.format(value // 1000)
    return template.format(value)


def test_every_documented_ceiling_matches_the_live_config():
    """A number in prose must be the number that binds.

    The failure names the document, the claim and the live value, because the
    thing that went wrong twice was someone updating a table row and not the
    sentence three pages up that said something else.
    """
    import config_schema

    cfg = config_schema.current()
    wrong, missing = [], []
    texts = {}
    for filename, key, template in DOCUMENTED_CEILINGS:
        if filename not in texts:
            with open(os.path.join(ROOT, filename), encoding="utf-8") as f:
                texts[filename] = f.read()
        live = getattr(cfg, key)
        expected = _ceiling_shown(template, live)
        if expected in texts[filename]:
            continue
        # Present but with some other number in it? Then it is drift. Absent
        # entirely? Then the claim was reworded and this row needs pointing
        # at the new wording -- a different problem with a different fix.
        head, _, tail = template.partition("{}")
        head = head.rstrip()
        if head and head in texts[filename]:
            index = texts[filename].index(head)
            wrong.append(
                f"{filename}: expected {expected!r} for {key}={live!r}; the "
                f"document says "
                f"{texts[filename][index:index + len(expected) + 12]!r}")
        else:
            missing.append(f"{filename}: {template!r} (for {key}) is gone")

    assert not wrong, (
        "documented ceilings disagree with config.yaml:\n  "
        + "\n  ".join(wrong)
        + "\nThe value is what binds. Either the prose is stale or the value "
          "moved without the prose; fix whichever is wrong and say which in "
          "the commit.")
    assert not missing, (
        "these documented claims no longer appear, so nothing is being "
        "checked:\n  " + "\n  ".join(missing)
        + "\nIf the wording moved, point DOCUMENTED_CEILINGS at the new "
          "wording rather than deleting the row -- the claim is the thing "
          "being kept honest.")


def test_the_ceiling_check_can_actually_fail():
    """The guard on the guard.

    Every row above could be absent from its document and the test would
    still pass its first assertion, so the renderer is exercised directly
    against a value the document does NOT carry.
    """
    import config_schema

    cfg = config_schema.current()
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        readme = f.read()

    template = "killable subprocesses under a {}-second wall clock"
    assert _ceiling_shown(template, cfg.tool_compute_timeout_s) in readme
    assert _ceiling_shown(template, cfg.tool_compute_timeout_s + 1) \
        not in readme, (
        "README carries the wall clock at two different numbers, which is "
        "the exact defect this pair of tests exists to catch")


# ---------------------------------------------------------------------------
# ---- Cited TEST NAMES resolve too (batch 108) ------------------------------
# ---------------------------------------------------------------------------

_CITED_TEST = re.compile(r"\b(Test[A-Z][A-Za-z0-9]*|test_[a-z0-9_]{4,})\b")


def _defined_python_names():
    """(every name the tree defines, every module basename).

    DEFINITIONS, not just `def` and `class`, and the width is measured
    rather than cautious: `project_init/manifest.py` documents a dataclass
    field called `test_command` in its own docstring, and a def-only index
    reports that as a dangling citation. Assignment targets, annotated
    targets and parameters are all things prose legitimately names.
    """
    import ast

    names, stems = set(), set()
    for dirpath, _dirnames, filenames in _walk_project():
        for name in filenames:
            if not name.endswith(".py"):
                continue
            stems.add(os.path.splitext(name)[0])
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8", errors="replace") as handle:
                try:
                    tree = ast.parse(handle.read())
                except SyntaxError:  # pragma: no cover -- another test's job
                    continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    names.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx,
                                                               ast.Store):
                    names.add(node.id)
                elif (isinstance(node, ast.AnnAssign)
                      and isinstance(node.target, ast.Name)):
                    names.add(node.target.id)
                elif isinstance(node, ast.arg):
                    names.add(node.arg)
    return names, stems


def test_every_test_name_cited_in_production_code_resolves():
    """The guard beside
    `test_every_decision_id_cited_in_production_code_resolves`, for the
    other thing production prose points at.

    THE ONE IT WAS WRITTEN FROM (batch 108). `security/sandbox.py` closed
    the argument for `_SHELL_METACHARACTERS` -- the closure that makes Q1
    a closure rather than the next patch in a series -- with "pinned as
    one by" the same words in lower snake case, as though the pin were a
    function. It is the CLASS `TestTheTwoTokenisersCannotDisagree`, which
    the same file cites correctly 140 lines further down. It survived two
    batches, and a review found it rather than a reader.

    WHY IT IS MORE THAN A TYPO. `protected_paths.user_config_dir` already
    recorded this exact failure about itself and named the cost: "a reader
    who greps the name finds nothing and concludes the duplication is
    unguarded". Both sit under a sentence asserting that something
    dangerous is pinned by a test, which is when a citation is
    load-bearing.

    PRODUCTION ONLY, deliberately. The suite's own prose legitimately
    names tests and files that no longer exist -- `test_compaction_e2e.py`
    is discussed by name in this very file, in the docstring of the check
    that exists because five pointers survived its rename. A guard that
    flagged those would need an allowlist on its first day.

    A WRAPPED NAME IS NOT A MISSING ONE: a long citation broken across two
    docstring lines arrives truncated, so a candidate that is a prefix of
    a real name is read as that name rather than reported.
    """
    names, stems = _defined_python_names()
    ordered = sorted(names)
    dangling = []
    for path in _production_python():
        for line_number, text in _prose_lines(path):
            for cited in sorted(set(_CITED_TEST.findall(text))):
                if cited in names or cited in stems:
                    continue
                if any(real != cited and real.startswith(cited)
                       for real in ordered):
                    continue
                dangling.append(
                    f"{os.path.relpath(path, ROOT)}:{line_number} "
                    f"cites {cited}")

    assert not dangling, (
        "Production code cites test names that do not exist:\n  "
        + "\n  ".join(dangling)
        + "\nA citation under a claim that something is pinned by a test "
          "is load-bearing: a reader who greps the name and finds nothing "
          "concludes the thing is unguarded. Fix the name, or describe the "
          "pin without spelling a name that is not there."
    )


def test_the_cited_test_name_guard_can_actually_fail():
    """The guard on the guard, in the shape
    `test_the_ceiling_check_can_actually_fail` already uses here.

    A walk that stopped finding files, or a regex that stopped matching,
    would leave the check above passing forever. Each half is asserted
    against a name nothing defines.
    """
    names, stems = _defined_python_names()
    invented = "TestNothingAnywhereDefinesThis"

    # The walk reaches real files.
    assert "TestTheTwoTokenisersCannotDisagree" in names
    assert len(list(_production_python())) > 20
    # And the matcher would report an absent one.
    assert invented not in names and invented not in stems
    assert _CITED_TEST.findall(f"pinned by `{invented}`") == [invented]
    assert not any(real.startswith(invented) for real in names)
# ===========================================================================
# ---- Batch 128: the numbers AGENTS.md states about the tree ---------------
# ===========================================================================
#
# The two gates above this -- test_every_decision_id_cited_in_production_code_resolves
# and test_every_test_name_cited_in_production_code_resolves -- are PRODUCTION
# ONLY, deliberately, because "the suite's own prose legitimately names tests
# and files that no longer exist". That reasoning is right about test prose and
# wrong about AGENTS.md, which is not prose about tests: it is the file CLAUDE.md
# calls not-optional reading because its decisions "have already been broken
# once by someone who reasoned from the code alone".
#
# Swept by hand in batch 128, AGENTS.md was clean on everything mechanical --
# 529 of 529 cited identifiers resolved, no dangling test name, no file:line
# citation anywhere -- and wrong on SEVEN NUMBERS, every one of them a count of
# something in this tree that nothing checked. So the counts get what
# ARCHITECTURE's per-file counts got in audit #121, and for the reason that
# document states about itself: a figure "was written as 113 in batch 84 and
# left there through five batches that added thirty keys; the test counts, this
# sentence did not".
#
# Each of these reads the figure back OUT of AGENTS.md rather than hardcoding
# it, so the failure names the sentence to edit rather than a constant in here.


def _agents_md() -> str:
    with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as handle:
        return handle.read()


def _one_number(pattern, what):
    """The single integer AGENTS.md states for *what*, or a failure naming it."""
    found = re.findall(pattern, _agents_md())
    assert len(found) == 1, (
        f"AGENTS.md should state {what} exactly once in the shape "
        f"{pattern!r}; found {len(found)}: {found[:4]}. Editing that sentence "
        f"means editing this pattern too -- which is the point: the number and "
        f"the thing that checks it are supposed to move together.")
    return int(found[0].replace(",", ""))


#: Relative to the project root, and the same depth window AGENTS.md's sentence
#: names. `read_project_doc` resolves anywhere under the project; the window is
#: what makes the figure a figure rather than an unbounded walk.
_DOC_BOUND_MAX_DEPTH = 3


def _readable_docs_on_a_clean_checkout():
    """(count, bytes) for what `read_project_doc` would admit, TRACKED ONLY.

    Tracked only, and that is the deliberate narrowing. This worktree holds
    untracked `output/`, `.qwen/reviews/` and `.ipynb_checkpoints/` files that
    `is_readable_doc` accepts quite correctly, so a plain walk would make this
    gate answer differently on every machine. `git ls-files` is how
    test_no_notebook_checkpoint_file_is_committed asks the same question, with
    the same two skips when git cannot answer at all.

    Measured through the tool's OWN predicate. `tools/builtin/project_docs.py`
    records what happened the last time this rule had a second implementation:
    the two "disagreed in both directions -- six advertised paths the tool
    refused, and four it accepted that never appeared".
    """
    from tools.builtin import project_docs

    root = _project_root()
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=root,  # nosec B603, B607 - `git ls-files`, fixed argv
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pytest.skip("git is not available to ask what is tracked")
    if listed.returncode != 0:  # pragma: no cover
        pytest.skip("not a git checkout")

    count = 0
    total = 0
    for line in listed.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        # Depth IS the slash count of the project-relative path: a root file
        # is 0, `docs/X.md` is 1, `agents/builtin/x.md` is 2. Written as the
        # bound rather than `- 1`, which dropped every depth-3 file and made
        # this gate report 46 against a measured 60 on its first run.
        if rel.count("/") > _DOC_BOUND_MAX_DEPTH:
            continue
        if not project_docs.is_readable_doc(root, rel):
            continue
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.exists(full):  # pragma: no cover -- deleted but staged
            continue
        count += 1
        total += os.path.getsize(full)
    return count, total


def test_the_documented_doc_read_bound_is_not_understated():
    """THE ONE NUMBER HERE WHOSE ERROR HAS A DIRECTION.

    `read_project_doc` is registered with permission True, so it is advertised
    to every run -- plain chat and all ten research passes, not just an agent
    that declared it. AGENTS.md states how much that reaches, and the sentence
    after it says why the figure is written down at all: "ARCHITECTURE.md used
    to say the most a prompt-injected pass gains is the README (audit #97);
    understating a security bound is the one direction that costs something."

    It then understated it by 3.3x. Batch 128 measured 60 files and 3.58 MB
    against a stated 40 files and ~1.1 MB, and almost all of the growth was
    this record -- DEVLOG.md alone is 1.2 MB. Nobody widened the tool; the
    documents it can read got bigger, which is exactly the drift no reader
    would think to re-check.

    SO THE TWO FIGURES ARE GATED DIFFERENTLY, on purpose. The file COUNT is
    exact: it moves only when a document is added or removed, which is rare and
    worth being told about. The SIZE is a CEILING: this record grows every
    batch, so an equality would go red on the next commit and be retyped
    without being read, which is how a gate becomes a formality. A ceiling goes
    red when the bound crosses a round number, and that is when noticing is
    the correct outcome.
    """
    stated_count = _one_number(r"\*\*(\d+) files\n\s*and under \d+ MB at depths 0-3\*\*",
                               "the readable-document count")
    stated_mb = _one_number(r"\*\*\d+ files\n\s*and under (\d+) MB at depths 0-3\*\*",
                            "the readable-document size ceiling")

    count, total = _readable_docs_on_a_clean_checkout()
    megabytes = total / (1024.0 * 1024.0)

    assert count == stated_count, (
        f"AGENTS.md says `read_project_doc` reaches {stated_count} files on a "
        f"clean checkout; it reaches {count}. Update the sentence in the "
        f"read_project_doc paragraph -- and if the number went UP, read it "
        f"first: that paragraph is about a security bound, and the direction "
        f"that costs something is the one you are in.")

    assert megabytes < stated_mb, (
        f"AGENTS.md says `read_project_doc` reaches under {stated_mb} MB on a "
        f"clean checkout; it reaches {megabytes:.2f} MB. The bound genuinely "
        f"grew -- raise the ceiling in that sentence deliberately rather than "
        f"to the nearest number that passes, because the figure exists to be "
        f"read by someone deciding whether this tool is safe to advertise.")


def test_agents_md_states_the_real_test_tui_class_count():
    """AGENTS.md said 41 classes from batch 114 until batch 128, when there
    were 64.

    The number is load-bearing where it sits: the paragraph is the argument
    that classing `test_tui.py` is what unbound the suite's loadscope floor,
    and `TestThisFileIsGroupedForLoadscope::test_every_test_here_is_inside_a_class`
    makes every new test join a class. So the count rising is the rule
    WORKING, and the sentence saying 41 made it look like a one-off.

    `ast`, not a `grep`: a `^class ` count also takes the seven helper classes
    in that file, and `Test*` is what pytest collects.
    """
    stated = _one_number(r"are \*\*(\d+)\*\* now, which is the rule working",
                         "test_tui.py's class count")

    import ast
    path = os.path.join(ROOT, "tests", "test_tui.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    live = sum(1 for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name.startswith("Test"))

    assert live == stated, (
        f"AGENTS.md says tests/test_tui.py is {stated} classes; it declares "
        f"{live} `Test*` classes at module level. One digit, in the "
        f"FLOOR-BOUND paragraph.")


def test_agents_md_states_the_real_modal_parametrisation(request):
    """AGENTS.md said 3 x 18 = 54 until batch 128, when it was 3 x 19 = 57.

    Counted from the COLLECTED NODE IDS, because `_DECISION_CASES` is
    `_modal_cases() + _overflowing_modal_cases()` -- computed, so there is no
    literal list to read and a reimplementation of those two functions here
    would be the parallel-implementation mistake `project_docs` records.

    Narrowing is already solved in this file: `_collected_nodeids` falls back
    to a subprocess collection when the invocation filtered the session, and
    `test_the_default_invocation_is_not_treated_as_narrowed` guards the guard.
    """
    sizes = _one_number(r"is (\d+) sizes \u00d7 \d+ cases = \*\*\d+\*\* of them",
                        "the modal size count")
    cases = _one_number(r"is \d+ sizes \u00d7 (\d+) cases = \*\*\d+\*\* of them",
                        "the modal case count")
    product = _one_number(r"is \d+ sizes \u00d7 \d+ cases = \*\*(\d+)\*\* of them",
                          "the modal node-id count")

    assert sizes * cases == product, (
        f"AGENTS.md states {sizes} x {cases} = {product}, which is not a "
        f"product. Fix the arithmetic before trusting either factor.")

    name = "test_every_modal_keeps_its_decision_on_screen"
    live = sum(1 for node in _collected_nodeids(request) if name in node)
    if not live:  # pragma: no cover -- a collection that cannot see test_tui.py
        pytest.skip("the modal test was not collected in this invocation")

    assert live == product, (
        f"AGENTS.md says {name} is {sizes} x {cases} = {product} node ids; "
        f"pytest collects {live}. A size or a screen was added -- update that "
        f"sentence in the parametrised-floor paragraph.")


def test_agents_md_states_the_real_sympy_allowlist_sizes():
    """Three of the `safe_parse` figures, and the fourth left ungated on purpose.

    GATED: the two MODULE CONSTANTS -- `_ALLOWED_NAMES` at 230 and
    `_ALLOWED_NODES` at 39. Both are literals in `_math_common`, so both are
    deterministic in any process that imports it.

    NOT GATED, and the second one is the more interesting reason.

    "out of the ~130 Python defines" is not gated because the interpreter is
    not pinned: the floor is 3.11, 3.13 counts 132 `ast.AST` subclasses, and
    the original sentence said 118. A gate would go red on an upgrade, which
    is not a defect in anything.

    `dir(sympy)` is not gated because **it is not a fixed number at all**, and
    this test is how that was found. It read 926 when the four new gates ran
    alone and 927 when the whole file ran. The extra name is `stats`:
    `tools/builtin/probability_stats.py` does `import sympy.stats`, and
    importing a submodule binds it as an attribute of the package, so
    `dir(sympy)` grows by whatever any module happened to import first. WSL's
    unpinned 1.14.0 reads 928 again. Pinning `sympy` exactly does not make
    `dir()` on it stable, which is what the first draft of this test assumed.

    THE CONSEQUENCE IS REAL AND IT IS INERT, which is worth having written
    down rather than rediscovered. `_SAFE_GLOBALS` is built from `dir(sympy)`
    at import time, so importing `probability_stats` first puts the `stats`
    MODULE OBJECT into it -- and the module docstring's own rule is that "no
    module object is ever permitted". It is unreachable anyway: `stats` is not
    in `_ALLOWED_NAMES`, so `_validate_ast` never resolves the name, and the
    implicit-multiplication transform turns the bare word into `a*s**2*t**2`
    rather than a lookup. `stats.__class__` and `stats.Die` are refused
    because `ast.Attribute` is absent from the node allowlist. Measured, all
    four. The name gate is what makes the globals dict's contents not matter.

    THE OFF-BY-ONE THAT STARTED THIS: AGENTS.md said "231 of 927 exposed" from
    the day it was written -- the allowlist literal has held 230 words, none
    duplicated, since #52 created it, and nothing has edited that region since.
    Not drift. Wrong on arrival, for three years of batches, in the sentence
    that says which names can reach the parser.
    """
    import sympy

    from tools.builtin import _math_common

    stated_names = _one_number(r"\(\*\*(\d+)\*\* of the ~\d+", "the allowlist size")
    stated_exposed = _one_number(r"\(\*\*\d+\*\* of the ~(\d+)", "SymPy's exposed count")
    stated_nodes = _one_number(r"allowlist of \*\*(\d+)\*\* node types",
                               "the AST node allowlist size")

    assert len(_math_common._ALLOWED_NAMES) == stated_names, (
        f"AGENTS.md says `_ALLOWED_NAMES` holds {stated_names} names; it holds "
        f"{len(_math_common._ALLOWED_NAMES)}.")
    assert len(_math_common._ALLOWED_NODES) == stated_nodes, (
        f"AGENTS.md says the AST allowlist is {stated_nodes} node types; it is "
        f"{len(_math_common._ALLOWED_NODES)}.")

    # APPROXIMATE, for the import-order reason in the docstring: a window, not
    # an equality, so the sentence stays honest without claiming a stability
    # `dir()` on a package does not have.
    exposed = [name for name in dir(sympy) if not name.startswith("_")]
    assert abs(len(exposed) - stated_exposed) <= 5, (
        f"AGENTS.md says SymPy exposes ~{stated_exposed} public names; this "
        f"SymPy ({sympy.__version__}) exposes {len(exposed)}, which is outside "
        f"the few names import order can account for. If the pin moved, the "
        f"sentence about what the allowlist admits is a claim about the new "
        f"surface and needs re-reading, not just a new denominator.")

    # The claim the two numbers are FOR: an allowlist, not a denylist.
    assert stated_names < stated_exposed, (
        "the allowlist cannot admit more names than SymPy exposes")
