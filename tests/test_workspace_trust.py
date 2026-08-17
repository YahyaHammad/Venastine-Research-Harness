"""
test_workspace_trust.py

ROADMAP_v2 §14 acceptance criteria 1-2 plus hash-control properties of
core/workspace_trust.py:

  AC1: untrusted project .venastine/ content does not load until
       grant_trust() (is_trusted is the gate the loader consults).
  AC2: modifying any file under a trusted project's .venastine/ flips
       is_trusted() back to False.

The trust store is redirected into tmp_path via HOME/USERPROFILE so
tests never touch the real user config dir.
"""

import os

import pytest

from core import workspace_trust


@pytest.fixture(autouse=True)
def _redirect_trust_store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    yield home


def _make_project(tmp_path, files: dict, name="proj"):
    proj = tmp_path / name
    for rel, content in files.items():
        p = proj / ".venastine" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return proj


def test_no_venastine_dir_is_trusted(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    assert workspace_trust.is_trusted(str(proj)) is True


def test_ac1_untrusted_until_granted(tmp_path):
    proj = _make_project(tmp_path, {"skills/x.md": "---\nname: x\n---\nbody"})
    assert workspace_trust.is_trusted(str(proj)) is False

    workspace_trust.grant_trust(str(proj))
    assert workspace_trust.is_trusted(str(proj)) is True


def test_ac2_content_change_revokes_trust(tmp_path):
    proj = _make_project(tmp_path, {"skills/x.md": "---\nname: x\n---\nbody"})
    workspace_trust.grant_trust(str(proj))
    assert workspace_trust.is_trusted(str(proj)) is True

    (proj / ".venastine" / "skills" / "x.md").write_text(
        "---\nname: x\n---\ninjected body", encoding="utf-8")
    assert workspace_trust.is_trusted(str(proj)) is False


def test_added_file_revokes_trust(tmp_path):
    proj = _make_project(tmp_path, {"settings.json": "{}"})
    workspace_trust.grant_trust(str(proj))

    (proj / ".venastine" / "CONTEXT.md").write_text("hi", encoding="utf-8")
    assert workspace_trust.is_trusted(str(proj)) is False


def test_hash_sees_paths_not_just_content(tmp_path):
    """The relative path is part of the digest (Rev. 3 correction 2).

    Identical content in identical WALK ORDER, differing only in where
    the file lives. The previous version swapped two files' names, which
    cannot detect its own regression: os.walk descends sorted, so file
    positions are fixed by name and the two projects hash differently
    whether or not the path bytes are fed in. That left the actual
    failure untested -- a relocation preserving the content sequence
    inheriting trust silently, which is precisely what path-in-hash
    exists to catch.
    """
    proj_a = _make_project(tmp_path, {"agents/x.md": "C"}, name="pa")
    proj_b = _make_project(tmp_path, {"skills/x.md": "C"}, name="pb")
    assert workspace_trust._content_hash(str(proj_a)) != \
        workspace_trust._content_hash(str(proj_b))


def test_hash_deterministic_across_calls(tmp_path):
    proj = _make_project(tmp_path, {
        "skills/x.md": "one",
        "agents/y.md": "two",
        "settings.json": "{}",
    })
    first = workspace_trust._content_hash(str(proj))
    second = workspace_trust._content_hash(str(proj))
    assert first == second


def test_store_written_under_user_home(tmp_path, _redirect_trust_store):
    proj = _make_project(tmp_path, {"settings.json": "{}"})
    workspace_trust.grant_trust(str(proj))

    store = _redirect_trust_store / ".config" / "venastine" / "trusted_projects.json"
    assert store.exists()


def test_content_files_sorted_and_relative(tmp_path):
    proj = _make_project(tmp_path, {"skills/z.md": "z", "agents/a.md": "a"})
    assert workspace_trust.content_files(str(proj)) == [
        "agents/a.md", "skills/z.md"]
    assert workspace_trust.content_files(str(tmp_path / "missing")) == []


# ---------------------------------------------------------------------------
# ---- Bounded, regular-files-only hashing (review f4) ----------------------
# ---------------------------------------------------------------------------
#
# This hash runs BEFORE the user has consented to anything, over content
# that arrived with a directory they cloned. Reading it with one f.read()
# made a multi-GB blob a single unbounded allocation at startup, and
# open() on a FIFO (os.walk lists it; a symlink to one resolves) blocks
# forever. Neither is content worth reading to decide whether to trust it.

def test_large_file_hashes_correctly_in_chunks(tmp_path):
    """A file well past the 64 KiB chunk size must produce the same digest
    a whole-file read would. A chunk loop that reads once and stops looks
    fine on small fixtures and silently ignores the tail of every real
    one."""
    import hashlib

    blob = ("A" * 4096 + "B" * 4096) * 32          # 256 KiB, 4 chunks
    proj = _make_project(tmp_path, {"big.md": blob})

    expected = hashlib.sha256()
    expected.update(b"big.md")
    expected.update(b"\0")
    expected.update(blob.encode("utf-8"))
    expected.update(b"\0")

    assert workspace_trust._content_hash(str(proj)) == expected.hexdigest()


def test_non_regular_file_is_hashed_by_name_and_never_opened(
        tmp_path, monkeypatch):
    """A path that isn't a regular file contributes its NAME to the
    digest, not its contents -- because opening it is what hangs. Two
    projects differing only in such a file's bytes must therefore hash
    identically; before the fix they hashed differently, which is the
    proof the bytes were being read."""
    real_isfile = os.path.isfile

    def fake_isfile(path):
        # Stand in for a FIFO/device, which cannot be created portably.
        if os.path.basename(path) == "pipe.dat":
            return False
        return real_isfile(path)

    monkeypatch.setattr(os.path, "isfile", fake_isfile)

    a = _make_project(tmp_path, {"pipe.dat": "AAAA"}, name="a")
    b = _make_project(tmp_path, {"pipe.dat": "ZZZZ"}, name="b")

    assert workspace_trust._content_hash(str(a)) == \
        workspace_trust._content_hash(str(b))


def test_regular_file_contents_still_change_the_digest(tmp_path):
    """Control for the test above: without the isfile patch, differing
    contents MUST differ -- otherwise the first test would pass against
    an implementation that ignores every file's content."""
    a = _make_project(tmp_path, {"pipe.dat": "AAAA"}, name="a")
    b = _make_project(tmp_path, {"pipe.dat": "ZZZZ"}, name="b")

    assert workspace_trust._content_hash(str(a)) != \
        workspace_trust._content_hash(str(b))


# ---------------------------------------------------------------------------
# ---- The store fails closed, never crashes (review f23, f25) -------------
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", ["{truncated", "[]", "null", '"text"'])
def test_damaged_store_means_untrusted_not_a_crash(tmp_path,
                                                   _redirect_trust_store, body):
    """A partial write (Ctrl+C, full disk) or a non-object store used to
    raise out of is_trusted() at EVERY startup for EVERY project until
    the file was deleted by hand. Unknown state must mean untrusted --
    which re-triggers the prompt -- not unstartable."""
    store = _redirect_trust_store / ".config" / "venastine"
    store.mkdir(parents=True, exist_ok=True)
    (store / "trusted_projects.json").write_text(body, encoding="utf-8")

    proj = _make_project(tmp_path, {"settings.json": "{}"})
    assert workspace_trust.is_trusted(str(proj)) is False


def test_grant_is_written_atomically(tmp_path, _redirect_trust_store,
                                     monkeypatch):
    """A truncate-then-write leaves the store empty for the length of the
    write and permanently damaged if the process dies inside it. Asserted
    by failing the serialization midway: the ORIGINAL file must survive
    intact, which a direct write cannot manage."""
    first = _make_project(tmp_path, {"settings.json": "{}"}, name="first")
    workspace_trust.grant_trust(str(first))
    path = _redirect_trust_store / ".config" / "venastine" / "trusted_projects.json"
    before = path.read_text(encoding="utf-8")

    def _explode(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(workspace_trust.json, "dump", _explode)
    second = _make_project(tmp_path, {"settings.json": "{}"}, name="second")
    with pytest.raises(RuntimeError):
        workspace_trust.grant_trust(str(second))

    assert path.read_text(encoding="utf-8") == before
    assert workspace_trust.is_trusted(str(first)) is True


# ---------------------------------------------------------------------------
# ---- Correction (1): descent order does not reach the digest (issue #22) --
# ---------------------------------------------------------------------------
#
# `_content_hash` and `content_files` both call `dirs.sort()` inside their
# os.walk loop, and `_content_hash`'s docstring argues for it explicitly:
#
#     `os.walk` yields subdirectories in filesystem order, not sorted order.
#     Sorting `files` alone is not enough -- `dirs` must be sorted IN PLACE so
#     os.walk itself descends deterministically, or the same unchanged
#     .venastine/ can hash differently between runs and re-prompt for trust
#     that was already granted.
#
# Replacing that line with `pass` left all 1406 tests green. Correction (2)
# in the same docstring IS pinned (test_hash_sees_paths_not_just_content), so
# this was one unpinned guard inside an otherwise covered function.
#
# The consequence is not a trust bypass -- it is a spurious re-prompt. That
# still matters, because repeated unexplained trust prompts are exactly what
# trains someone to approve them without reading, which is the failure the
# verbatim settings.json/mcp.json display in the prompt exists to prevent.


def _walk_in(order):
    """Build an `os.walk` stand-in that hands the caller subdirectories in a
    CHOSEN order and then descends in whatever order the caller left them.

    Two things this has to get right, and the first draft of it got neither.

    1. FIDELITY. Real `os.walk` re-reads `dirs` after yielding, which is the
       only reason an in-place `dirs.sort()` is observable at all. A stand-in
       yielding a fixed sequence of tuples would make every test below
       vacuous, because the sort would have nothing to influence.

    2. BOTH SIDES MUST BE CONTROLLED. The first version compared a "reversed"
       walk against the REAL one -- and on this filesystem `.venastine/`
       enumerates as skills, agents, which is already reverse-alphabetical.
       The two orders coincided, the comparison was of a list against itself,
       and removing `dirs.sort()` from production left it green. So the two
       walks compared here are both synthetic and differ by construction.
    """
    def walk(top):
        entries = sorted(os.scandir(top), key=lambda e: e.name)
        dirs = [e.name for e in entries if e.is_dir()]
        files = [e.name for e in entries if not e.is_dir()]
        dirs.sort(reverse=(order == "desc"))
        yield top, dirs, files
        for name in list(dirs):          # the order the CALLER left it in
            yield from walk(os.path.join(top, name))
    return walk


@pytest.fixture
def _two_subdirs(tmp_path):
    """Two sibling subdirectories, each with a file, so descent order changes
    the order paths enter the hash. One subdirectory cannot show this -- and
    the common .venastine/ layout (agents/, skills/) has two."""
    return _make_project(tmp_path, {
        "agents/reviewer.md": "agent body",
        "skills/crypto.md": "skill body",
        "settings.json": "{}",
    })


def _digest_with(walk, project, monkeypatch):
    monkeypatch.setattr(workspace_trust.os, "walk", walk)
    try:
        return workspace_trust._content_hash(str(project))
    finally:
        monkeypatch.undo()


def test_descent_order_does_not_change_the_digest(_two_subdirs, monkeypatch):
    """Two walks over the same tree, differing only in the order they hand
    over subdirectories, must produce the same digest. Removing
    `dirs.sort()` from `_content_hash` fails this."""
    ascending = _digest_with(_walk_in("asc"), _two_subdirs, monkeypatch)
    descending = _digest_with(_walk_in("desc"), _two_subdirs, monkeypatch)

    assert ascending == descending, (
        "The .venastine/ content hash depends on the order os.walk happens to "
        "descend into subdirectories. The same unchanged project then hashes "
        "differently between runs and re-prompts for trust that was already "
        "granted -- correction (1) in _content_hash's docstring.")


def test_the_two_walk_doubles_really_do_differ(_two_subdirs):
    """Guards the guard. If the two stand-ins ever stopped differing -- or
    stopped honouring the caller's in-place sort -- the test above would pass
    for the wrong reason and silently protect nothing. That is not
    hypothetical: it is what the first version of this test did."""
    root = workspace_trust.venastine_dir(str(_two_subdirs))

    asc = [os.path.basename(d) for d, _, _ in _walk_in("asc")(root)]
    desc = [os.path.basename(d) for d, _, _ in _walk_in("desc")(root)]
    assert asc[1:] == ["agents", "skills"]
    assert desc[1:] == ["skills", "agents"]

    # ... and that production's sort is what collapses the difference.
    sorted_desc = []
    for dirpath, dirs, _files in _walk_in("desc")(root):
        dirs.sort()                       # what the production code does
        sorted_desc.append(os.path.basename(dirpath))
    assert sorted_desc == asc


def test_descent_order_does_not_change_the_trust_prompt_listing(
        _two_subdirs, monkeypatch):
    """`content_files` carries the identical `dirs.sort()`, and it is what the
    D17 trust prompt lists. AGENTS.md's rule is to grep every other call site
    sharing a root cause; this is that site.

    A reordered listing is cosmetic where a reordered digest is not -- but it
    is the same one-line guard, and it is the text a user reads to decide
    whether to trust a project.
    """
    def listing(walk):
        monkeypatch.setattr(workspace_trust.os, "walk", walk)
        try:
            return workspace_trust.content_files(str(_two_subdirs))
        finally:
            monkeypatch.undo()

    ascending = listing(_walk_in("asc"))
    assert listing(_walk_in("desc")) == ascending

    # Not `sorted(...)`. The listing is sorted WITHIN each directory and
    # walk-ordered ACROSS them, so the root's own files come first and
    # "settings.json" precedes "agents/reviewer.md" -- lexicographically
    # backwards, and correct. What must be stable is the directory order.
    assert ascending == ["settings.json", "agents/reviewer.md", "skills/crypto.md"]


# ---------------------------------------------------------------------------
# ---- #18: the hash must cover exactly what the loader will read ------------
# ---------------------------------------------------------------------------
#
# The property that actually failed. `content_files()` (the trust prompt's
# listing) and `_content_hash()` (the gate) are two independent traversals
# of "the files under .venastine/", and `config_loader._md_files` is a
# THIRD, rooted one level deeper. When the third disagreed with the first
# two, project content loaded unhashed and unlisted.
#
# The fix makes the loader refuse the layout the hash cannot see, so the
# three agree again. These tests assert the agreement rather than the
# mechanism, so they survive whichever way a later change restores it --
# including the deeper fix the issue prefers, where content_files() and
# _content_hash() stop being two walks.


def _granted(tmp_path, files, links=None, name="proj"):
    """A project with `files` under .venastine/, plus optional
    `.venastine/<name> -> target` directory symlinks, with trust granted."""
    proj = _make_project(tmp_path, files, name=name)
    for rel, target in (links or {}).items():
        try:
            (proj / ".venastine" / rel).symlink_to(
                target, target_is_directory=True)
        except (OSError, NotImplementedError):    # pragma: no cover
            pytest.skip("platform does not support directory symlinks")
    workspace_trust.grant_trust(str(proj))
    return proj


def test_a_symlinked_tier_root_is_invisible_to_both_the_hash_and_the_listing(
        tmp_path):
    """The escape itself, stated as the trust module sees it.

    This is NOT the fix -- `os.walk` still will not descend a symlinked
    subdirectory, and making it do so is the more intrusive option the
    issue weighs. What changed is that the LOADER no longer reads what
    this cannot see. Pinned as current behaviour so that a later change to
    follow links is a deliberate one, taken with the cycle and realpath
    guards it needs.
    """
    outside = tmp_path / "outside" / "skills"
    outside.mkdir(parents=True)
    (outside / "evil.md").write_text("Body v1.", encoding="utf-8")

    proj = _granted(tmp_path, {"settings.json": "{}"},
                    links={"skills": outside})

    assert workspace_trust.content_files(str(proj)) == ["settings.json"]

    before = workspace_trust.is_trusted(str(proj))
    (outside / "evil.md").write_text("Body v2 CHANGED.", encoding="utf-8")
    after = workspace_trust.is_trusted(str(proj))

    assert before is True and after is True, (
        "rewriting the symlink target changed the trust verdict; if the "
        "hash now covers linked content, #18's loader guard can be "
        "reconsidered -- see the issue's option 2")


def test_the_loader_reads_no_file_the_trust_listing_omits(tmp_path,
                                                          monkeypatch):
    """THE INVARIANT, and the one worth keeping however #18 is fixed.

    Every file the loader would read as project tier must appear in the
    listing the trust prompt showed the user. D29's premise is that the
    prompt shows what is being consented to; a file the loader reads and
    the prompt never named breaks that at the moment the design stops to
    ask.
    """
    from core import config_loader

    outside = tmp_path / "outside" / "skills"
    outside.mkdir(parents=True)
    (outside / "evil.md").write_text(
        "---\nname: evil\ndescription: d\n---\n\nBody.\n", encoding="utf-8")

    proj = _granted(tmp_path, {
        "settings.json": "{}",
        "skills/real.md": "---\nname: real\ndescription: d\n---\n\nBody.\n",
    }, links={"agents": outside})

    listed = set(workspace_trust.content_files(str(proj)))
    venastine = workspace_trust.venastine_dir(str(proj))

    read = set()
    for kind in ("agents", "skills"):
        for tier, directory in config_loader._tier_dirs(kind, str(proj), True):
            if tier != "project" or not os.path.isdir(directory):
                continue
            for path, _category in config_loader._md_files(directory, True):
                read.add(os.path.relpath(path, venastine).replace(os.sep, "/"))

    assert read <= listed, (
        f"the loader reads {sorted(read - listed)}, which the trust prompt "
        "never listed and the hash does not cover (#18)")


def test_that_invariant_can_actually_fail(tmp_path, monkeypatch):
    """Guards the guard. `read <= listed` is trivially true when `read` is
    empty, so the check above would pass loudest if `_md_files` stopped
    finding anything at all.

    Restoring the pre-fix behaviour -- a `_tier_dirs` that hands back the
    symlinked project root -- must make it fail. This is the mutation, run
    as a test, because the invariant is the kind that decays into a tautology
    without anyone noticing.
    """
    from core import config_loader

    outside = tmp_path / "outside" / "skills"
    outside.mkdir(parents=True)
    (outside / "evil.md").write_text(
        "---\nname: evil\ndescription: d\n---\n\nBody.\n", encoding="utf-8")

    proj = _granted(tmp_path, {"settings.json": "{}"},
                    links={"skills": outside})

    listed = set(workspace_trust.content_files(str(proj)))
    venastine = workspace_trust.venastine_dir(str(proj))

    # The pre-#18 _tier_dirs: append the project root unconditionally.
    unguarded = os.path.join(venastine, "skills")
    read = {os.path.relpath(p, venastine).replace(os.sep, "/")
            for p, _c in config_loader._md_files(unguarded, True)}

    assert read, "the fixture stopped producing a readable skill"
    assert not read <= listed, (
        "the pre-fix layout no longer escapes the listing, so the "
        "invariant test above is no longer discriminating anything")
