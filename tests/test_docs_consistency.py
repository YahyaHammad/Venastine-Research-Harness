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
import shutil
import subprocess  # nosec B404 - the gates below drive git and pytest as subprocesses on purpose
import sys
import tempfile
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


def _default_markexpr(config) -> str:
    """The `-m` expression this project's addopts actually supply.

    READ OFF THE LIVE CONFIG RATHER THAN RESTATED (batch 151, S19-03). The
    line below used to compare `markexpr` against the literal
    `"not integration"`, which is a copy of `pytest.ini`'s addopts -- so
    adding a marker there (two lines, because `--strict-markers`) made every
    full run read as narrowed, four gates skip, and the subprocess-collection
    agreement stop running anywhere, with no failure to say so.

    `config.getini("addopts")` returns the SHLEX-SPLIT list, measured:
    `['-ra', '--strict-markers', '-m', 'not integration', ...]`. So `-m` and
    its expression are two elements. The squashed `-mEXPR` spelling is legal
    too and arrives as one.

    NO EXCLUSION FOR LONG OPTIONS, and the absence is deliberate. This read
    `arg.startswith("-m") and not arg.startswith("--")`, on the stated
    grounds that `--strict-markers` also begins with `-m`. It does not:
    every long option begins with `--`, so the prefix test fails at index 1
    for all of them and the second clause could never change an answer. It
    was removed in the batch that added it, by the control that could not
    kill it.

    Returns `""` when addopts passes no `-m` at all, which is the right
    answer: with no default filter, an invocation that carries a markexpr
    HAS been narrowed.
    """
    addopts = list(config.getini("addopts") or [])
    for index, arg in enumerate(addopts):
        if arg == "-m":
            return addopts[index + 1] if index + 1 < len(addopts) else ""
        if arg.startswith("-m"):
            return arg[2:]
    return ""


def _was_narrowed(config) -> bool:
    """Whether this invocation collected less than the whole suite.

    pytest.ini's addopts carries `-m "not integration"`, so a default run
    already has a markexpr -- comparing against None would skip always and
    the assertion would never run, which is the failure mode this guard
    most needs to avoid. What it compares against is read out of the live
    config by `_default_markexpr`, for the reason stated there.
    """
    if getattr(config.option, "keyword", ""):          # -k
        return True
    if getattr(config.option, "markexpr", "") != _default_markexpr(config):
        return True
    # An arg naming a file or a nodeid; a bare `pytest` or `pytest tests/`
    # collects everything and is not narrowing.
    return any("::" in arg or arg.endswith(".py") for arg in config.args)


class _FakeOption:
    def __init__(self, keyword="", markexpr=""):
        self.keyword = keyword
        self.markexpr = markexpr


class _FakeConfig:
    """The parts of a `pytest.Config` that `_was_narrowed` reads.

    `markexpr` DEFAULTS TO WHAT THIS FAKE'S OWN addopts SUPPLY (batch 151,
    S19-03). It used to default to the literal `"not integration"` -- the
    same literal `_was_narrowed` compared against -- so
    `test_the_default_invocation_is_not_treated_as_narrowed` asserted that
    two copies of one string agreed with each other, and could not fail for
    the drift it exists to catch. Deriving it here means "a default run" is
    default by construction, whatever addopts says.
    """

    def __init__(self, args, addopts=("-m", "not integration"),
                 markexpr=None, keyword=""):
        self.args = args
        self._addopts = list(addopts)
        if markexpr is None:
            markexpr = _default_markexpr(self)
        self.option = _FakeOption(keyword=keyword, markexpr=markexpr)

    def getini(self, name):
        assert name == "addopts", f"the fake was asked for {name!r}"
        return list(self._addopts)


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

    # THE CASE THE LITERAL COULD NOT SEE (batch 151, S19-03). A pytest.ini
    # that gains a marker is a two-line edit; before this, the full run it
    # produced read as narrowed and four gates below went quiet.
    moved = ("-m", "not integration and not slow")
    assert not _was_narrowed(_FakeConfig(["tests"], addopts=moved))
    # ...and a real `-m` on the command line is still narrowing, which is
    # the half that must not be lost to make the half above pass.
    assert _was_narrowed(
        _FakeConfig(["tests"], addopts=moved, markexpr="not integration"))


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


def test_the_default_marker_expression_is_read_and_not_restated(request):
    """`_default_markexpr` agrees with the ini pytest itself parsed.

    Live against live, which is this file's rule: the expectation is read
    out of `config.getini("addopts")` by a second, dumber parse rather than
    written down here. The two spellings below are the ones a hand edit to
    `pytest.ini` could produce. The `--strict-markers` case pins that a
    token which is not a `-m` is SKIPPED rather than guessed at, which is a
    property of the loop and would fail if the prefix test were ever
    loosened to a substring search -- it is not about long options being
    excluded, and the comment here said so until the control for that
    exclusion could not be made to kill anything.
    """
    addopts = list(request.config.getini("addopts"))
    assert "-m" in addopts, (
        "pytest.ini's addopts no longer passes `-m`. _default_markexpr "
        "returns '' for that, which makes an UNFILTERED run the default "
        "one -- check _was_narrowed still answers what it claims before "
        f"changing this. addopts is {addopts}")
    expected = addopts[addopts.index("-m") + 1]
    assert _default_markexpr(request.config) == expected

    assert _default_markexpr(
        _FakeConfig([], addopts=("-mnot integration",))) == "not integration"
    assert _default_markexpr(
        _FakeConfig([], addopts=("--strict-markers",))) == ""


# ---------------------------------------------------------------------------
# ---- S19-05: the root conftest's own inventory ----------------------------
# ---------------------------------------------------------------------------


def test_the_root_conftest_header_names_every_module_it_stubs():
    """The header's list of fakes against the modules it installs.

    `conftest.py`'s first paragraph names what is injected into
    `sys.modules`, and `_install_fake_sdks` installs one the paragraph did
    not mention -- `markitdown`, since #144. The omission matters because
    of what follows it: the docstring's next-but-one paragraph is a `NOT
    faked, by explicit decision` list, which makes the first read as the
    COMPLETE set rather than as an example. A reader asking "is markitdown
    stubbed, or do I need it installed?" got "no" from the header and "yes"
    from the installer, and `docs/ARCHITECTURE.md` records what that costs:
    "a green local suite is not a green fresh install".

    Both directions, because either can drift: a stub the header omits, and
    a name the header keeps after the stub is gone. A dotted child counts
    as named by its parent, so installing `google.genai.types` under a
    header that says `google.genai` is not a miss.
    """
    import ast

    with open(os.path.join(ROOT, "conftest.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    installer = next(
        (node for node in tree.body
         if isinstance(node, ast.FunctionDef)
         and node.name == "_install_fake_sdks"), None)
    assert installer is not None, (
        "conftest.py has no _install_fake_sdks; if the stubs moved, this "
        "gate moves with them rather than being deleted")

    installed = set()
    for node in ast.walk(installer):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "modules"
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)):
            installed.add(target.slice.value)
    assert len(installed) > 5, (
        f"the ast walk found {sorted(installed)}, which is too few to be "
        f"the stub set -- the assignment shape changed and this gate is "
        f"now checking nothing")

    #: THE ONE PARAGRAPH THAT IS AN INVENTORY, named rather than bounded by
    #: what happens to follow it. This first split at "Why we fake these",
    #: which left every backquoted name in the preamble in scope -- so the
    #: two function names in the paragraph batch 151 added read as claimed
    #: stubs. The rationale list below it, the `NOT faked, by explicit
    #: decision` list below that and any prose added later are all out of
    #: scope by construction now.
    paragraphs = (ast.get_docstring(tree) or "").split("\n\n")
    inventory = next(
        (p for p in paragraphs if p.startswith("Injects fake")), "")
    assert inventory, (
        "conftest.py's header no longer opens a paragraph with `Injects "
        "fake`, so this gate has nothing to read. Reword the assertion "
        "with it -- do not delete the inventory, it is what the `NOT "
        "faked, by explicit decision` list below it is read against.")
    named = set(re.findall(r"`([A-Za-z_][\w.]*)`", inventory))
    #: Named in that sentence as the DESTINATION, not as a stub.
    named -= {"sys.modules"}

    def covered(name, names):
        return name in names or any(name.startswith(f"{n}.") for n in names)

    missing = sorted(n for n in installed if not covered(n, named))
    assert not missing, (
        f"conftest.py stubs {missing} and its header does not say so. The "
        f"paragraph reads as the complete set because a later one is a "
        f"`NOT faked, by explicit decision` list, so a name missing here "
        f"reads as a package you must install.")

    stale = sorted(n for n in named if not covered(n, installed))
    assert not stale, (
        f"conftest.py's header claims to stub {stale} and the installer "
        f"does not. A header naming a stub that is gone is how someone "
        f"concludes the real package is unreachable in tests.")


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
# offline, Python-only, and has to run where there is no Node. The tarball
# audit lives in scripts/prepublish-check.mjs, which runs from
# `prepublishOnly` on a machine that by definition has npm. These three check
# the allowlist as TEXT, which is what a Python suite can honestly verify.
# (The release-tag half below is the exception, batch 162: it runs that
# script under Node with a fake npm, and skips where there is no Node.)

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


# The release TAG is a third copy of the version, and the two tests above
# cannot see it: a tag exists only on the run it started. So this half is
# behavioural, and it is the one place this file runs Node -- the real
# prepublish-check.mjs, with GITHUB_REF_TYPE/GITHUB_REF_NAME set the way the
# runner sets them. It needs Node and NOT npm: the `npm` it finds is a fake
# that records being called and fails, so nothing is ever packed, nothing
# touches the network, and "refused before anything is packed" becomes a
# file that does or does not exist.

_PREPUBLISH_CHECK = os.path.join(ROOT, "scripts", "prepublish-check.mjs")


def _run_prepublish_check(tmp_path, ref_type, ref_name):
    """Run the real check as a run from `ref_type`/`ref_name` would.

    Returns the finished process and whether the script reached `npm`, which
    it calls for exactly one thing: `npm pack --dry-run`.

    GITHUB_* is stripped from the inherited environment before the two are
    set. This suite runs in CI, and on a tag push the runner's own
    GITHUB_REF_TYPE is `tag` -- the publish job runs this test on exactly
    that push.

    A missing `node` skips here and FAILS in CI. A revert check cannot see a
    test that skips (tests/BREAKING_CHANGES.md says why), and GitHub's
    ubuntu image ships node, so a skip there would mean this guard had
    quietly stopped running where it matters.
    """
    node = shutil.which("node")
    if node is None:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("node is not on PATH in CI, so the release-tag check "
                        "in scripts/prepublish-check.mjs is untested")
        pytest.skip("node is not on PATH; the release-tag check is JavaScript")

    fake_bin = tmp_path / "fake-npm"
    fake_bin.mkdir()
    called = tmp_path / "npm-was-called"
    if os.name == "nt":
        # spawnSync(..., {shell: true}) on Windows: cmd finds npm.cmd by PATHEXT.
        (fake_bin / "npm.cmd").write_text(
            f'@echo called> "{called}"\r\n@exit /b 3\r\n', encoding="ascii")
    else:
        fake = fake_bin / "npm"
        fake.write_text(f'#!/bin/sh\necho called > "{called}"\nexit 3\n',
                        encoding="ascii")
        fake.chmod(0o755)

    env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    if ref_type is not None:
        env["GITHUB_REF_TYPE"] = ref_type
        env["GITHUB_REF_NAME"] = ref_name

    proc = subprocess.run(  # nosec B603 - node, resolved to an absolute path, running this repo's own script
        [node, _PREPUBLISH_CHECK], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=120)
    return proc, called.exists()


@pytest.mark.parametrize("tag", [
    pytest.param("v{version}-rc.1", id="a-prerelease-of-the-version"),
    pytest.param("{version}", id="the-version-without-its-v"),
    pytest.param("", id="no-tag-name"),
])
def test_a_release_tag_that_is_not_the_version_is_refused_before_packing(
        tmp_path, tag):
    """Register item 115, ROADMAP_v3 A78.

    `v0.3.0` and `v0.3.1` were pushed on commits whose files said 0.2.0, and
    the v0.3.1 `publish (npm)` run packed @yahyahammad/venastine@0.2.0 and
    reported the check passed. Nothing compared the tag with anything.

    Each tag here is a near miss, which is the point. A prerelease tag
    begins with the right version, so a prefix comparison lets it through.
    A tag without its `v` contains the right version, so a substring
    comparison lets it through. An empty name has to refuse rather than
    compare equal to nothing. And the refusal has to come before `npm` is
    reached at all: with the call removed, the script goes on to pack.
    """
    version = _package_json()["version"]
    tag = tag.format(version=version)

    proc, packed = _run_prepublish_check(tmp_path, "tag", tag)

    assert proc.returncode == 1, (
        f"the check let tag {tag!r} through for package version {version}.\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    assert f"so its tag is v{version}" in proc.stderr, (
        f"the check failed for some other reason than the tag:\n{proc.stderr}")
    assert not packed, (
        f"the check refused tag {tag!r}, but only after calling npm. The "
        f"comparison has to come before anything is packed.")


@pytest.mark.parametrize("ref_type,ref_name", [
    pytest.param("tag", "v{version}", id="the-tag-that-names-the-version"),
    pytest.param("branch", "main", id="a-run-from-a-branch"),
    pytest.param(None, None, id="a-local-npm-publish"),
])
def test_a_run_the_tag_check_has_no_quarrel_with_goes_on_to_pack(
        tmp_path, ref_type, ref_name):
    """The controls for the test above: the check refuses only what it is for.

    A tag that is `v` + the version goes on to pack. So does a run from a
    branch, which names no version, and a local `npm publish`, which has
    neither variable. Each one reaches the fake `npm`, so each ends in that
    fake's own failure and never in the tag refusal.
    """
    version = _package_json()["version"]
    if ref_name is not None:
        ref_name = ref_name.format(version=version)

    proc, packed = _run_prepublish_check(tmp_path, ref_type, ref_name)

    assert "so its tag is" not in proc.stderr, (
        f"the check refused a {ref_type!r} run named {ref_name!r} for "
        f"version {version}:\n{proc.stderr}")
    assert packed, (
        f"the check never reached `npm pack` for a {ref_type!r} run named "
        f"{ref_name!r}.\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
    assert "`npm pack --dry-run` failed" in proc.stderr, (
        f"the fake npm was called but its failure was not reported:\n"
        f"{proc.stderr}")


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


_MATCH_DEMOS = {
    "exact": lambda value, s: s == value,
    "basename": lambda value, s: os.path.basename(s) == value,
    "suffix": lambda value, s: s.endswith(value),
    "prefix": lambda value, s: any(p.startswith(value) for p in s.split("/")),
    "dir": lambda value, s: s == value or s.startswith(value + "/"),
    "segment": lambda value, s: value in s.split("/"),
    "suffix-or-segment": lambda value, s: (s.endswith(value)
                                           or value in s.split("/")),
}


def test_every_never_distribute_value_is_demonstrated_by_a_sample():
    """An entry's `samples` must drive every PATTERN it declares, not just one.

    The gate above requires samples to EXIST; this one requires them to
    REACH. `compiled python` declared `.pyc` and `__pycache__`, sampled only
    `x.pyc`, and .gitignore carried no `__pycache__` rule at all -- so the git
    channel claimed a pattern it did not have while the npm channel enforced
    the same pattern wholesale through `files`, and the three gates that exist
    to catch exactly that disagreement had nothing to drive it with. It was
    the ONE undriven value in the list when this was written (batch 157, A65),
    which is why this is a check with a demonstrated consequence rather than
    one looking for work.
    """
    for entry in _never_distribute():
        demo = _MATCH_DEMOS.get(entry["match"])
        assert demo, (
            f"{entry['label']!r} uses match kind {entry['match']!r} and this "
            f"gate has no rule for what would demonstrate it. The npm gate "
            f"below checks the CONSUMER knows every kind; this checks the "
            f"DATA carries a path showing what each pattern covers.")
        for value in entry["value"]:
            assert any(demo(value, s) for s in entry["samples"]), (
                f"{entry['label']!r} declares {value!r} and none of its "
                f"samples {entry['samples']} demonstrates it, so every gate "
                f"driven off `samples` skips that pattern in silence. Add a "
                f"sample the {entry['match']!r} match would catch.")


# A path that must NOT be ignored. It rides in the same batch of samples as
# the ones that must be, so the two halves cannot end up disagreeing about
# which tree they measured.
_PUSH_CONTROL = ".venastine/agents/example.md"


def _unignored_in_a_fresh_clone(samples: list) -> set:
    """The `samples` that no rule in the COMMITTED `.gitignore` covers.

    DRIVEN THROUGH `git check-ignore`, so git does the matching and nothing
    here re-implements it. What this adds is the TREE it asks about, and that
    is not a detail: run in ROOT, this gate was green on every developer
    machine for seventeen batches and failed on its first push, in all three
    jobs that run it (batch 157).

    THE WORKING TREE WAS PART OF THE QUESTION, which was the whole defect.
    `logs/`, `output/` and `.ipynb_checkpoints/` are DIRECTORY-ONLY patterns
    and git decides directory-ness by asking the filesystem, so a bare `logs`
    matches on a machine that has run the app and matches nothing in a clone.
    Measured: with `logs/` in .gitignore and nothing on disk, `logs` is not
    ignored; after `mkdir logs` it is. The samples are paths INSIDE those
    directories now (A63), because `git add -A` stages files and a path's
    continuation is what tells git the component is a directory.

    HERMETIC IN THE OTHER DIRECTION TOO. A global `core.excludesFile` also
    reaches `check-ignore` -- measured in the same batch -- and so does
    `.git/info/exclude`, so either could make this pass on evidence that does
    not ship. GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM point at paths inside
    the throwaway tree that do not exist, which git reads as empty
    configuration. A non-existent path rather than `os.devnull`, because this
    suite runs on win32 where that is `nul` and not a file git will open.
    """
    tracked = subprocess.run(  # nosec B603, B607 - `git` with a fixed argv, as above
        ["git", "ls-files", "--", "*.gitignore"],
        cwd=ROOT, capture_output=True, text=True)
    assert tracked.stdout.split() == [".gitignore"], (
        f"this tree tracks {tracked.stdout.split()} and this driver copies "
        f"only the root `.gitignore` into its throwaway tree, so it would "
        f"under-test the others. Copy them all at their own paths -- git "
        f"applies a nested .gitignore only within its own directory.")

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(  # nosec B603, B607 - `git` with a fixed argv, as above
            ["git", "init", "-q", tmp], capture_output=True, check=True)
        shutil.copyfile(os.path.join(ROOT, ".gitignore"),
                        os.path.join(tmp, ".gitignore"))
        env = dict(os.environ,
                   GIT_CONFIG_GLOBAL=os.path.join(tmp, "absent-global"),
                   GIT_CONFIG_SYSTEM=os.path.join(tmp, "absent-system"))
        unignored = set()
        for sample in samples:
            proc = subprocess.run(  # nosec B603, B607 - `git` with a fixed argv, as above
                ["git", "check-ignore", "-q", "--", sample],
                cwd=tmp, env=env, capture_output=True)
            if proc.returncode != 0:
                unignored.add(sample)
    return unignored


def test_the_push_channel_ignores_every_sample_that_claims_it():
    """The git channel, measured in the fresh clone the claim is about --
    `_unignored_in_a_fresh_clone` records why that is the tree.

    The control is inside the same mechanism: a path no rule covers makes
    check-ignore exit non-zero and print nothing, which is how
    `.venastine/mcp.json` was found -- it was absent from the tree's only
    `.gitignore` while four sibling paths resolved to a rule.
    """
    claimed = [(e["label"], s) for e in _never_distribute()
               if "git" in e["channels"] for s in e["samples"]]
    assert claimed, "no entry claims the git channel, so this proves nothing"

    unignored = _unignored_in_a_fresh_clone(
        [s for _, s in claimed] + [_PUSH_CONTROL])

    missing = [f"{s} ({label})" for label, s in claimed if s in unignored]
    assert not missing, (
        f"package.json `neverDistribute` claims the git channel for these, "
        f"and `.gitignore` matches none of them: {missing}. A first-party "
        f"command writes some of these into a user's project, so `git add -A` "
        f"from the project root would stage them -- and a published "
        f"credential is remedied by rotation, not by a later commit.")

    # The positive control: the narrow pair was chosen over `.venastine/`
    # precisely so project-tier agents and skills keep travelling with a
    # repository (D17's trust prompt is the mechanism that exists for them).
    assert _PUSH_CONTROL in unignored, (
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


_CODEQL_CONFIG_REF = "./.github/codeql/codeql-config.yml"
_CODEQL_CONFIG = os.path.join(ROOT, ".github", "codeql", "codeql-config.yml")


def _shipped_paths() -> list:
    """Every file package.json's `files` puts in the tarball, as a
    forward-slash path from the root."""
    out = []
    for entry in _package_json()["files"]:
        if entry.startswith("!"):        # negation guards ship nothing
            continue
        top = os.path.join(ROOT, entry.rstrip("/"))
        if os.path.isfile(top):
            out.append(entry)
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            rel = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
            out.extend(f"{rel}/{name}" for name in filenames)
    return out


def _codeql_ignores(pattern: str, path: str) -> bool:
    """CodeQL's `paths-ignore` reading of *pattern*, closely enough to
    bound it: a bare path covers everything under it, and a leading `**/`
    matches at the root too. `fnmatch`'s `*` crosses `/`, which errs toward
    a match -- the safe direction for a gate that forbids one."""
    pattern = pattern.strip("/")
    candidates = {pattern}
    if pattern.startswith("**/"):
        candidates.add(pattern[3:])
    return any(fnmatch.fnmatch(path, c) or fnmatch.fnmatch(path, c + "/*")
               for c in candidates)


def test_codeql_ignores_only_what_never_ships():
    """Batch 158, A66. `paths-ignore` is how CodeQL is told not to look, so
    it is bounded by the one list that says what reaches a user.

    PR #200's first analysis raised five alerts in tests/, and each was a
    test doing its job: a redaction test has to log a secret to prove the
    secret is redacted. Skipping tests/ is safe because `files` does not
    name it. An entry that a SHIPPED path falls under would turn the same
    file into a way of silencing production code -- and a false positive
    there is dismissed with a recorded reason instead, which keeps the rule
    looking at the next change to that line.
    """
    import yaml

    # PARSED, not grepped: codeql.yml's header names the config file too,
    # so a substring check passes with the key deleted -- mutation A5's
    # shape, one workflow over.
    workflow = yaml.safe_load(_workflow_text()["codeql.yml"])
    inits = [(job, step.get("with") or {})
             for job, spec in workflow["jobs"].items()
             for step in spec["steps"]
             if step.get("uses", "").startswith("github/codeql-action/init@")]
    assert len(inits) >= 2, (
        f"found {len(inits)} CodeQL init step(s); codeql.yml has a security "
        f"job and a quality job, so the sweep is not reaching them")
    unwired = [job for job, given in inits
               if given.get("config-file") != _CODEQL_CONFIG_REF]
    assert not unwired, (
        f"these CodeQL jobs do not read {_CODEQL_CONFIG_REF}: {unwired}. A "
        f"job without it scans tests/ and re-raises the alerts batch 158 "
        f"triaged, on the next pull request that touches a redaction test.")

    with open(_CODEQL_CONFIG, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    extra = sorted(set(config) - {"name", "paths-ignore"})
    assert not extra, (
        f"codeql-config.yml carries {extra}. It is shared by BOTH jobs and "
        f"holds paths only: `queries` would erase the security/quality split "
        f"codeql.yml keeps per job on purpose, and a query filter or `paths` "
        f"narrows what shipped code is checked for -- a decision A66 records "
        f"the owner declining.")

    ignored = config.get("paths-ignore") or []
    assert ignored, (
        "codeql-config.yml ignores nothing, so the relation below is vacuous; "
        "if tests/ is meant to be scanned again, delete the file and this gate")
    shipped = _shipped_paths()
    assert "main.py" in shipped and len(shipped) > 100, (
        f"only {len(shipped)} shipped paths enumerated; the walk is not "
        f"reaching package.json's `files`")
    hidden = sorted({f"{pattern} -> {path}" for pattern in ignored
                     for path in shipped if _codeql_ignores(pattern, path)})
    assert not hidden, (
        f"codeql-config.yml's paths-ignore covers files that ship: "
        f"{hidden[:10]}. CodeQL may skip only what never reaches a user; a "
        f"false positive in shipped code is dismissed in GitHub with its "
        f"reason, and recorded in DEVLOG.")


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


# ---------------------------------------------------------------------------
# ROADMAP_v3 §79 HD9, amended in batch 169: the hosted demo deploys a commit
# only when EVERY CI check on it passed (the owner's rule), and §80's
# New Year job is the one workflow that writes to the repository. Each test
# below is a property of the workflow files that a later edit could break
# without any run noticing until it mattered.
# ---------------------------------------------------------------------------

def _workflow_yaml(name: str) -> dict:
    import yaml

    return yaml.safe_load(_workflow_text()[name])


def _deploy_gate():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "deploy_gate_for_docs", os.path.join(ROOT, "scripts", "deploy_gate.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _on(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as True.
    return doc.get("on", doc.get(True)) or {}


def test_the_deploy_waits_for_every_workflow_a_push_to_main_runs():
    """deploy-demo.yml wakes when one of the workflows it lists finishes,
    and its gate waits for every workflow a push to main runs. A CI
    workflow missing from the list cannot wake it: if that one finished
    last, nothing would ever deploy that commit; a list naming a workflow
    that no push runs wakes the gate for nothing."""
    gate = _deploy_gate()
    listed = sorted(_on(_workflow_yaml("deploy-demo.yml"))["workflow_run"]
                    ["workflows"])
    required = sorted(wf.name for wf in gate.required_workflows())
    assert listed == required, (
        f"deploy-demo.yml's workflow_run list {listed} is not the set of "
        f"workflows a push to main runs, {required}. Make them the same.")
    assert {"tests", "pip-audit"} <= set(required), (
        "the suite and the audit are among the checks the owner named")


def test_only_the_deploy_job_holds_a_secret_and_only_after_the_gate():
    jobs = _workflow_yaml("deploy-demo.yml")["jobs"]
    holders = sorted(name for name, job in jobs.items()
                     if "secrets." in json.dumps(job)
                     or job.get("environment"))
    assert holders == ["deploy-hf"], (
        f"jobs that can read a secret or enter an environment: {holders}. "
        f"Only the deploy job may, and only after the gate.")
    deploy = jobs["deploy-hf"]
    assert deploy["needs"] == "gate"
    assert deploy["if"] == "needs.gate.outputs.verdict == 'deploy'"
    assert deploy["environment"] == "demo"
    assert deploy["concurrency"]["cancel-in-progress"] is False
    # security-events: read is the code scanning alerts (batch 170); the
    # gate stops when it cannot read them, so losing it holds every deploy.
    reads = {"actions": "read", "checks": "read", "contents": "read",
             "security-events": "read"}
    assert jobs["gate"]["permissions"] == reads
    assert deploy["permissions"] == reads
    assert _workflow_yaml("deploy-demo.yml")["permissions"] == {}


def test_the_gate_judges_only_a_push_to_main_from_this_repository():
    """A workflow_run job's github.ref is the default branch whatever woke
    it, so an environment's branch rule cannot make this check; the job's
    `if:` and deploy_gate.py both do. A fork's pull request from a branch
    it named `main` matches `branches: [main]`; its event is not push."""
    condition = " ".join(
        _workflow_yaml("deploy-demo.yml")["jobs"]["gate"]["if"].split())
    for clause in ("github.event.workflow_run.event == 'push'",
                   "github.event.workflow_run.head_branch == 'main'",
                   "github.event.workflow_run.head_repository.full_name == "
                   "github.repository"):
        assert clause in condition, f"the gate's `if:` lost {clause!r}"


@pytest.mark.parametrize("name", ["deploy-demo.yml", "copyright-year.yml"])
def test_no_expression_is_pasted_into_a_shell_line(name):
    """A branch name is chosen by whoever pushed it. Values reach these
    workflows' scripts through `env:`, where the shell cannot read them as
    code."""
    for job_name, job in _workflow_yaml(name)["jobs"].items():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), (
                f"{name}:{job_name}: an expression inside `run:` -- move it "
                f"to the step's `env:`")


def test_the_workflow_never_deploys_unverified():
    assert "--unverified" not in _workflow_text()["deploy-demo.yml"], (
        "`--unverified` skips the CI gate; it exists for the owner's private "
        "test deploys and never for the workflow")


def test_a_schedule_cannot_cancel_a_push_run_the_gate_waits_for():
    """codeql and pip-audit run on a push and on a schedule. Keyed on the
    ref alone, the two share a cancel-in-progress group, and a cancelled
    push run is a failed check to the deploy gate."""
    for name in _workflow_text():
        doc = _workflow_yaml(name)
        on = _on(doc)
        concurrency = doc.get("concurrency") or {}
        if not (isinstance(on, dict) and "push" in on and "schedule" in on
                and concurrency.get("cancel-in-progress")):
            continue
        assert "github.event_name" in concurrency["group"], (
            f"{name}: push and schedule share the group "
            f"{concurrency['group']!r}")


def test_license_and_notice_carry_the_same_copyright():
    """§80: copyright-year.yml rewrites both or neither, and refuses when
    they disagree -- so a hand edit to one would stop it every New Year."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bump_copyright_year_for_docs",
        os.path.join(ROOT, "scripts", "bump_copyright_year.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lines = {}
    for name in module.FILES:
        with open(os.path.join(ROOT, name), encoding="utf-8",
                  newline="") as f:
            m = module._notice(f.read(), name)
        lines[name] = (m["first"], m["last"], m["holder"])
    assert len(set(lines.values())) == 1, lines


def test_the_copyright_job_is_the_only_workflow_that_writes():
    doc = _workflow_yaml("copyright-year.yml")
    crons = [entry["cron"] for entry in _on(doc)["schedule"]]
    assert crons == ["59 23 31 12 *", "7 6 1 1 *"], (
        "23:59 UTC on Dec 31 is the owner's time; the Jan 1 run is the "
        "catch-up for a delayed or dropped first one")
    writers = sorted(name for name, text in _workflow_text().items()
                     if "contents: write" in text)
    assert writers == ["copyright-year.yml"], writers


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


def test_architecture_states_the_real_truth_table_ceiling():
    """The one number batch 143 added, gated on the day it was added.

    `logic._MAX_TRUTH_TABLE_SYMBOLS` is what refuses a truth table before
    `2**n` rows exist, and ARCHITECTURE's tool contracts state it in prose
    -- so it is a value derived in two places, which is the shape S06-10
    and S06-11 on this same surface are both instances of. The bug is never
    that two copies drift; it is that nothing can tell you they have.

    The ROW COUNT is derived here rather than stated in the document, so
    the prose carries one number and this test does the arithmetic. 8
    columns is 256 rows and 33,575 characters measured -- the last table
    under `config.MAX_READ_CHARS`, which is where the figure came from.
    """
    from tools.builtin import logic

    with open(os.path.join(ROOT, "docs", "ARCHITECTURE.md"),
              encoding="utf-8") as handle:
        architecture = handle.read()

    stated = re.findall(r"truth table above \*\*(\d+) columns\*\* is refused",
                        architecture)
    assert len(stated) == 1, (
        "docs/ARCHITECTURE.md should state the truth-table ceiling exactly "
        "once in the shape 'a truth table above **N columns** is refused'; "
        "found %d: %s. Editing that sentence means editing this pattern "
        "too, which is the point." % (len(stated), stated[:4]))

    assert int(stated[0]) == logic._MAX_TRUTH_TABLE_SYMBOLS, (
        "docs/ARCHITECTURE.md says a truth table above %s columns is "
        "refused; `logic._MAX_TRUTH_TABLE_SYMBOLS` is %d, so the document "
        "and the code disagree about what the tool does."
        % (stated[0], logic._MAX_TRUTH_TABLE_SYMBOLS))

    # The claim the number is FOR: the largest table it admits has to fit
    # inside the ceiling the other five tools get, or the two bounds are
    # answering different questions.
    import config
    assert 2 ** logic._MAX_TRUTH_TABLE_SYMBOLS * 140 < config.MAX_READ_CHARS * 2, (
        "a %d-column table is 2**%d rows, which at the ~131 characters per "
        "row measured is past `max_read_chars` -- the input bound and the "
        "output bound no longer agree about where the edge is"
        % (logic._MAX_TRUTH_TABLE_SYMBOLS, logic._MAX_TRUTH_TABLE_SYMBOLS))


# ---------------------------------------------------------------------------
# ---- S07-01: no sink composes its own redaction ---------------------------
# ---------------------------------------------------------------------------

def test_no_production_module_composes_its_own_secret_redaction():
    """The relation, and the only form of this that cannot go vacuous.

    S07-01 was not one wrong line. It was five sinks each holding part of
    a rule: `logging_setup`'s formatter and `tui/app.py`'s log handler
    composed `redact_secrets` with `redact_live_values` -- in opposite
    orders -- while `tui/app.py`'s tool-error line, `core/loop.py`'s
    `mark_turn_failed` and the orchestrator's pipeline event called
    `redact_secrets` alone. Fixing the five was a one-line change each;
    what stops a SIXTH is this.

    `redact_secrets` is public and stays public -- `docs/ARCHITECTURE.md`
    lists it, and it is the vendor-token pass that both compositions are
    built from. The rule is only that nothing outside its own module
    calls it directly, because every caller outside is by definition a
    sink choosing a vocabulary.
    """
    offenders = {}
    for path in _production_python():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if rel == "safety/policy_enforcement.py":
            continue
        with open(path, encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                if line.lstrip().startswith("#"):
                    continue
                if "redact_secrets(" in line:
                    offenders.setdefault(rel, []).append(number)

    assert not offenders, (
        f"these call `redact_secrets` directly: {offenders}. That is the "
        f"vendor-token pass alone -- seven patterns, no credential SHAPES "
        f"and no value redaction -- so the sink would take a smaller "
        f"vocabulary than tool output, which is S07-01 exactly. Call "
        f"`redact_diagnostic_text` instead; it is the composition all five "
        f"existing sinks share, and it decides the order too (S07-05).")


# ---------------------------------------------------------------------------
# ---- Batch 146: what a registered callable may raise, stated once ---------
# ---------------------------------------------------------------------------

#: The spellings the six copies used, in the PRESENT tense. load_skill's
#: copy survives because it is an account of what happened -- "raised",
#: "aborted" -- and every clause of it is true; these say a raise ends a
#: turn today, and dispatch() has contained a handler's exception since
#: well before any of them was written.
_THE_FALSE_CLAIM = (
    "aborts the whole turn",
    "takes down the whole turn",
    "escapes the loop's ToolCallDenied",
)

#: Where the reason lives. Matched on the heading rather than on a sentence
#: so that rewording the paragraph does not quietly empty this relation.
_THE_ONE_COPY = "WHAT A CALLABLE REGISTERED BELOW MAY RAISE"


def test_no_production_module_restates_why_a_handler_validates():
    """The relation, and the reason it is a relation rather than six fixes.

    Six modules in tools/builtin/ justified `params.get` over `params[]`
    with the same mechanism and cited each other as the authority --
    "pin.py's rule", "Same reasoning as load_skill's", "remember.py's
    note". The practice is right; the mechanism was false, and had been
    since `dispatch()` started containing a handler's exception "on
    purpose". Seven cross-citing copies of one claim is seven chances to
    disagree with the code and one shared reason to believe it.

    What none of the six said is the half that was live in their own
    surface: a GATE's raise is not contained, and file_ops' two gates were
    not total. That is in the one copy now, which is what makes this worth
    gating -- the paragraph a seventh copy would be competing with is the
    one that carries the part that matters.
    """
    offenders = {}
    for path in _production_python():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if rel == "tools/base.py":
            continue
        for number, line in _prose_lines(path):
            for claim in _THE_FALSE_CLAIM:
                if claim in line:
                    offenders.setdefault(rel, []).append(
                        f"{number}: {claim}")

    assert not offenders, (
        f"these say a raise out of a tool ends the turn, in the present "
        f"tense: {offenders}. `dispatch()` contains a handler's exception "
        f"-- what validating params actually buys is an error dict the "
        f"model can read instead of a generic wrapper. The one copy of "
        f"this, including the half about GATES that none of the six "
        f"carried, is tools/base.py's "
        f"\"{_THE_ONE_COPY}\" block; cite it rather than restating it.")


def test_the_one_copy_is_where_the_relation_says_it_is():
    """The guard on the guard, and it closes the loosening the test above
    invites: deleting the paragraph in tools/base.py would make every
    citation of it dangle and the relation would go on passing, because
    passing means "nobody restated it" and an empty tree restates nothing.

    So: the block exists, exactly once, in the file whose fields declare
    the two callables it is about -- and it says both halves, because a
    version that kept only the containment half would be the comment the
    six modules had.
    """
    base = os.path.join(ROOT, "tools", "base.py")
    with open(base, encoding="utf-8") as f:
        body = f.read()

    assert body.count(_THE_ONE_COPY) == 1, (
        f"tools/base.py mentions {_THE_ONE_COPY!r} "
        f"{body.count(_THE_ONE_COPY)} times; the relation above exempts "
        f"this file on the strength of there being one copy here")
    for half in ("A HANDLER'S exception is contained",
                 "A GATE'S exception is NOT contained"):
        assert half in body, (
            f"tools/base.py no longer states {half!r}. The six copies this "
            f"replaced all had the first half and none had the second, "
            f"which is the whole reason the paragraph exists.")

    # And the citations are real, so the exemption is paying for something.
    citing = []
    for path in _production_python():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if rel == "tools/base.py":
            continue
        with open(path, encoding="utf-8") as f:
            if _THE_ONE_COPY in f.read():
                citing.append(rel)
    assert len(citing) >= 4, (
        f"only {citing} cite the one copy, and six modules were corrected "
        f"to point at it -- a relation nobody references is a relation "
        f"about nothing")



# ---------------------------------------------------------------------------
# ---- S16-03: a documented private symbol resolves -------------------------
# ---------------------------------------------------------------------------
#
# AGENTS.md named `_ASK_HANDLERS` as the CLI's table of ask handlers. There
# is no such symbol: the CLI's is a closure-local `handlers` published as
# `channel.rendered_kinds`, and the TUI's is an if-chain in
# `VenastineApp._ask_blocking`. `git log -S` returns only the docs commit
# that coined the name, so it was born stale rather than renamed without
# update -- which is the shape no convention catches, because there was
# never a moment when somebody edited code and forgot the document.

_PRIVATE_BACKQUOTED = re.compile(r"`(_[A-Za-z0-9_]+)`")


def _all_python_sources():
    """Every .py file in the live tree, tests included, EXCEPT this one.

    `_production_python` skips `tests`, and this gate must not: AGENTS.md
    legitimately names test helpers, and a production-only scan would
    report those as dangling for a reason that is not a defect.

    THIS FILE IS EXCLUDED, and it is not housekeeping. The gate below
    explains the defect it exists for by naming `_ASK_HANDLERS`, so with
    this file in scope the dangling token resolves against the paragraph
    written to forbid it -- measured: the control that writes that name
    back into AGENTS.md left the gate GREEN. A gate's evidence may not
    come from its own explanation of what it forbids, which is the same
    correction batch 151 made to the conftest-header gate one batch
    earlier, when two function names in its own added prose read as
    claimed stubs.
    """
    here = os.path.abspath(__file__)
    for dirpath, _dirnames, filenames in _walk_project():
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            if os.path.abspath(path) == here:
                continue
            yield path


def test_every_private_symbol_agents_md_names_exists_in_the_tree():
    """S16-03. A reader who greps for a documented symbol has to find it,
    and a fixer who "adds the missing handler to `_ASK_HANDLERS`" must not
    be invited to build a third registry beside the two real ones.

    THE DOMAIN IS EVERY BACKQUOTED PRIVATE IDENTIFIER, not the sentence
    that was wrong. A leading underscore is an unambiguous signal that a
    backquoted token is code rather than English, so nothing here has to
    guess; 131 of them were measured when this was written and 130
    resolved.

    A SUBSTRING MATCH, AND BOTH ALTERNATIVES WERE MEASURED. This file
    already owns an AST index of defined names -- `_defined_python_names`,
    which the cited-test-name gate uses -- and reusing it would be the
    stronger check and no new scanner. It fails on SEVENTEEN of the 131:
    it collects `ast.Name` Store targets and not `ast.Attribute` ones, so
    every instance attribute the document names (`_thinking_pending`,
    `_last_run`, `_viewing`) reads as undefined, as do four dunders.

    A `tokenize` NAME index is the other candidate and the only one that
    can tell code from prose, which is the hole this gate's own first
    form fell into (see `_all_python_sources`). It fails on eight, and
    three of those are not this repository's symbols at all:
    `_check_bindings` and `_watch_theme` are Textual's, `__builtins__` is
    CPython's, and `_compacting` is prose about a design the code
    deliberately did not take. A coherence gate that fails on a CORRECT
    reference to somebody else's symbol is the wrong trade, so the
    substring match stays and its one weakness is closed by excluding
    this file. TECHNICAL_DEBT 106 carries the two the NAME index flagged
    that may be genuinely stale.

    The substring match also answers the reader's own question most
    directly: a name reachable by grep is a name they will find.

    THE DOMAIN IS ASSERTED IN BOTH DIRECTIONS, which is ARCHITECTURE
    section 11's instruction and what batch 151 found missing in two
    guards: a scan that matched no tokens, or read no sources, would pass
    while checking nothing. The floors sit below the measured 131 and 243
    so ordinary editing does not trip them, and far enough above zero
    that a broken pattern or a mis-rooted walk does.

    WHAT IT DOES NOT COVER, said here so the next reader does not assume
    it: a dotted path whose first component is public, such as
    `VenastineApp._ask_blocking`, does not match this pattern as a whole
    -- the leading `V` is not an underscore -- so only its private tail
    would be caught if it were written alone. Widening to dotted paths
    means deciding what a public attribute resolves against, which is a
    different gate.
    """
    text = _agents_md()
    tokens = {}
    for match in _PRIVATE_BACKQUOTED.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        tokens.setdefault(match.group(1), []).append(line)
    assert len(tokens) > 80, (
        f"only {len(tokens)} backquoted private identifiers were found in "
        f"AGENTS.md, and 131 were measured when this gate was written, so "
        f"the pattern has stopped matching what it was written for")

    blobs = []
    for path in _all_python_sources():
        with open(path, encoding="utf-8", errors="replace") as handle:
            blobs.append(handle.read())
    assert len(blobs) > 150, (
        f"the scan read {len(blobs)} python files and 243 were measured, so "
        f"it is looking in the wrong place and would resolve nothing")

    missing = sorted(
        f"{token} (AGENTS.md:{','.join(str(n) for n in lines)})"
        for token, lines in tokens.items()
        if not any(token in blob for blob in blobs))
    assert not missing, (
        "AGENTS.md names private symbols that exist nowhere in the tree: "
        + "; ".join(missing)
        + ". A reader greps the documented name, finds nothing, and either "
        "concludes the thing is unguarded or adds it -- which creates a "
        "second thing beside the real one. Name what exists, or delete "
        "the sentence.")
#: The one sentence in AGENTS.md that promises a named file cannot be
#: carved out of the D17 trust hash. The filename is captured BY THE
#: PATTERN, adjacent to the phrase, rather than by scanning the paragraph
#: around it -- batch 152 lost a gate twice to a scan that could not tell
#: code from the prose written about it, and the paragraph this sits in now
#: names the OLD filename while explaining that it is gone.
_HASH_PROMISE = re.compile(
    r"`([A-Za-z0-9_][A-Za-z0-9_.-]*\.[A-Za-z0-9]+)` is never "
    r"special-cased out of the hash")


def test_the_file_agents_md_promises_is_inside_the_trust_hash(tmp_path):
    """AGENTS.md's I6 bullet guarantees one named file is never carved out
    of the trust hash. That name is pinned against the hash's actual input
    set (S17-05, batch 153).

    THE SENTENCE WAS BORN STALE BY RELOCATION, which is the shape a
    convention cannot catch: nobody edited the trust hash and forgot the
    document. Section 44 moved the hub from `.venastine/CONTEXT.md` to the
    root `AGENTS.md` and removed the old file outright, and this sentence
    went on promising that `CONTEXT.md` is never special-cased out of the
    hash -- a guarantee about a file that is not in the hash because it
    does not exist, while the guarantee that matters (the root `AGENTS.md`
    is in the listing, by WS9, precisely so a cloned hub cannot reach a
    system prompt unasked) went unstated. A reader checking the promise
    would have found nothing to check.

    `content_files()` is the right thing to compare against rather than
    `_content_hash`: it IS the hash's input set, by construction since
    section 44 -- the digest iterates the listing, so a file the listing
    omits cannot enter the hash. That is the relation the sentence is
    about.

    PINNED IN BOTH DIRECTIONS (`docs/ARCHITECTURE.md` section 11). A
    membership test that passes because the listing is large tells you
    nothing, so the fixture's listing is asserted exact, a file the
    sentence does NOT name is asserted present, and the name this sentence
    carried until batch 153 is asserted ABSENT -- which is what makes the
    pass mean something: the test it would have failed is the test it ran.
    """
    from core import workspace_trust

    collapsed = re.sub(r"\s+", " ", _agents_md())
    promised = _HASH_PROMISE.findall(collapsed)
    assert len(promised) == 1, (
        f"AGENTS.md should promise exactly once that a named file is never "
        f"special-cased out of the trust hash; found {len(promised)}: "
        f"{promised[:4]}. Rewording that sentence means editing this "
        f"pattern too -- a gate that silently matches nothing is the "
        f"defect it was written for, one level up.")
    promised = promised[0]

    project = tmp_path / "proj"
    (project / ".venastine" / "agents").mkdir(parents=True)
    (project / ".venastine" / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\n---\nbody\n", encoding="utf-8")
    (project / ".venastine" / "settings.json").write_text(
        "{}\n", encoding="utf-8")
    (project / workspace_trust.PROJECT_CONTEXT_FILENAME).write_text(
        "# hub\n", encoding="utf-8")

    listing = workspace_trust.content_files(str(project))
    assert sorted(listing) == [
        ".venastine/agents/reviewer.md",
        ".venastine/settings.json",
        workspace_trust.PROJECT_CONTEXT_FILENAME,
    ], (f"the fixture project should list exactly its three files and "
        f"listed {listing}; a listing that stopped walking would make the "
        f"check below pass or fail for the wrong reason")
    assert "CONTEXT.md" not in listing, (
        "the name this sentence carried until batch 153 must be absent "
        "from the listing, or this test cannot fail on the claim it was "
        "written to catch")

    assert promised in listing, (
        f"AGENTS.md promises that `{promised}` is never special-cased out "
        f"of the trust hash, and `workspace_trust.content_files()` -- which "
        f"IS the hash's input set -- does not list it: {listing}. Either "
        f"the file moved and the sentence did not, or the sentence names a "
        f"file that no longer exists. A promise about a file outside the "
        f"hash is not a weaker guarantee, it is no guarantee, and it reads "
        f"exactly like the real one.")
