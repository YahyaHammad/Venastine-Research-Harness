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
