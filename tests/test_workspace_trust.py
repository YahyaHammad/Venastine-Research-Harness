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
    """Two projects whose files swap names but keep the same content
    multiset must hash differently -- the relative path is part of the
    digest (Rev. 3 correction 2)."""
    proj_a = _make_project(tmp_path, {"a.md": "X", "b.md": "Y"}, name="pa")
    proj_b = _make_project(tmp_path, {"a.md": "Y", "b.md": "X"}, name="pb")
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
