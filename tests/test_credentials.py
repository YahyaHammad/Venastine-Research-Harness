"""
test_credentials.py

Issue #19. `credentials.py` had NO test file at all -- `save_credentials`
appeared nowhere under `tests/`, in the module that writes every LLM
provider's API key to disk in cleartext.

The defect it was filed for: `_write_provider_data` used a bare
`open(path, "w")`, so the file's protection was whatever the process
umask happened to give it. Measured at 0644 under the common default,
i.e. readable by every other account on the machine. The absence of an
explicit mode is the defect; a user with `umask 077` was already safe,
by accident of environment rather than by any property of the code.

WHY THE MODE ASSERTIONS SKIP ON WINDOWS. POSIX permission bits do not
exist there, and `os.open`'s mode argument only controls the read-only
flag. Skipping is honest: the property genuinely does not hold on this
platform, and asserting it would either fail or be quietly meaningless.
The behaviour that DOES hold everywhere -- that the file is created,
that a round trip preserves the data -- is asserted unconditionally, so
this file is not empty on the platform it is usually developed on. Same
shape as the symlink and SIGALRM skips elsewhere in the suite; the CI
container is where the mode assertions actually execute.
"""

import json
import os
import stat

import pytest

import credentials

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits do not apply on this platform (os.open's mode "
           "only controls the read-only flag on Windows)")


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    """Point the module at a temp file. LLM_PROVIDERS_FILE is a bare
    relative name, so without this every test would write providers.json
    into whatever directory pytest was launched from -- and that file is
    gitignored precisely because it holds real keys."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(credentials, "LLM_PROVIDERS_FILE", "providers.json")
    return tmp_path


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# ---- The mode (#19) --------------------------------------------------------
# ---------------------------------------------------------------------------

@posix_only
def test_the_credentials_file_is_not_readable_by_anyone_else(_in_tmp):
    """The finding, stated as the property rather than as the number: no
    group or other bit, however permissive the umask is."""
    credentials.save_credentials("ANTHROPIC", "sk-ant-secret-value")

    mode = _mode(_in_tmp / "providers.json")
    assert not mode & stat.S_IRGRP, f"group-readable: {oct(mode)}"
    assert not mode & stat.S_IROTH, f"other-readable: {oct(mode)}"
    assert not mode & stat.S_IWGRP, f"group-writable: {oct(mode)}"
    assert not mode & stat.S_IWOTH, f"other-writable: {oct(mode)}"


@posix_only
def test_a_permissive_umask_does_not_widen_it(_in_tmp):
    """The whole point. Under the old code this file's protection WAS the
    umask; the test that discriminates the fix from the defect is the one
    that makes the umask as permissive as possible and checks the mode
    anyway.

    Without this, `open(path, "w")` passes every other test in this file
    on a developer machine whose umask happens to be 077.
    """
    old = os.umask(0)
    try:
        credentials.save_credentials("ANTHROPIC", "sk-ant-secret-value")
    finally:
        os.umask(old)

    assert _mode(_in_tmp / "providers.json") == 0o600


@posix_only
def test_rewriting_an_existing_file_repairs_a_loose_mode(_in_tmp):
    """THE FIX GREW A MIGRATION IN BATCH 108, and this test said to come
    here and say so rather than delete it. So: it did, and here is why.

    This used to assert 0644 -- that a file created world-readable by an
    older version STAYED world-readable, because `O_TRUNC` on an existing
    file does not reset its mode. It was recorded as behaviour rather than
    fixed, on the honest scope "new writes are safe", with a note that a
    user who ran an older version should check the file themselves.

    Batch 108 made the write atomic to stop an interrupted save destroying
    every stored key, and `write_json_atomic` is temp-file-plus-
    `os.replace` with the mode applied to the TEMP file. `os.replace`
    carries the source's mode across, so the destination's old mode is
    not preserved -- it is replaced. Measured on this machine: 0644 before
    the save, **0600 after**, content intact.

    That is the migration arriving as a side effect, and it is the right
    direction: a `providers.json` left readable by every account on the
    machine is repaired the next time a key is saved, instead of waiting
    for a user to read a docstring. The scope note above is now obsolete,
    which is the one thing worth keeping from it.
    """
    (_in_tmp / "providers.json").write_text("{}", encoding="utf-8")
    os.chmod(_in_tmp / "providers.json", 0o644)

    credentials.save_credentials("ANTHROPIC", "sk-ant-secret-value")

    assert _mode(_in_tmp / "providers.json") == 0o600, (
        "an atomic write replaces the destination, so a loose mode on an "
        "existing file should be repaired rather than carried forward")
    # The discriminating half: a writer that produced an empty file would
    # satisfy the mode assertion above.
    assert "ANTHROPIC" in credentials.load_provider_data()


# ---------------------------------------------------------------------------
# ---- That it still does its job --------------------------------------------
# ---------------------------------------------------------------------------

def test_a_saved_credential_round_trips(_in_tmp):
    """The discriminating half. A writer that produced an empty file, or
    no file, would satisfy every mode assertion above."""
    credentials.save_credentials("ANTHROPIC", "sk-ant-secret-value")

    data = credentials.load_provider_data()
    assert data["ANTHROPIC"]["API_KEY"] == "sk-ant-secret-value"


def test_the_file_is_valid_json_on_disk(_in_tmp):
    """os.fdopen is a different write path from open(); this asserts the
    bytes actually landed rather than trusting the loader that wrote
    them."""
    credentials.save_credentials("OPENAI", "sk-openai-value",
                                 api_url="https://api.example/v1")

    on_disk = json.loads((_in_tmp / "providers.json").read_text(encoding="utf-8"))
    assert on_disk["OPENAI"]["API_KEY"] == "sk-openai-value"


def test_saving_a_second_provider_preserves_the_first(_in_tmp):
    """O_TRUNC rewrites the whole file, so the read-modify-write in
    save_credentials is load-bearing -- dropping it would silently lose
    every other provider's key on the next save."""
    credentials.save_credentials("ANTHROPIC", "sk-ant-one")
    credentials.save_credentials("OPENAI", "sk-openai-two")

    data = credentials.load_provider_data()
    assert data["ANTHROPIC"]["API_KEY"] == "sk-ant-one"
    assert data["OPENAI"]["API_KEY"] == "sk-openai-two"


def test_load_returns_empty_when_there_is_no_file(_in_tmp):
    assert credentials.load_provider_data() == {}


# ---------------------------------------------------------------------------
# ---- The trust store, same treatment ---------------------------------------
# ---------------------------------------------------------------------------

@posix_only
def test_the_trust_store_is_not_world_readable(tmp_path, monkeypatch):
    """Holds no secret -- the exposure is only which projects this user
    trusted and their content hashes. Done for consistency; it was never
    group- or other-WRITABLE, so no one could forge a trust entry.

    The mode goes on the TEMP file: `os.replace` carries the source's
    mode across, so setting it on the destination would be overwritten
    and setting it afterwards would be a second window.
    """
    from core import workspace_trust

    store = tmp_path / "trusted_projects.json"
    monkeypatch.setattr(workspace_trust, "_trust_store_path",
                        lambda: str(store))

    old = os.umask(0)
    try:
        workspace_trust._save_trust_store({"/some/path": "abc123"})
    finally:
        os.umask(old)

    assert _mode(store) == 0o600
    assert json.loads(store.read_text(encoding="utf-8")) == {"/some/path": "abc123"}


def test_the_trust_store_write_is_still_atomic(tmp_path, monkeypatch):
    """The property the temp-file dance exists for, which the mode change
    must not disturb: no `.tmp` is left behind, and the destination is
    complete."""
    from core import workspace_trust

    store = tmp_path / "trusted_projects.json"
    monkeypatch.setattr(workspace_trust, "_trust_store_path",
                        lambda: str(store))

    workspace_trust._save_trust_store({"/a": "1"})

    assert store.exists()
    assert not (tmp_path / "trusted_projects.json.tmp").exists()


# ---------------------------------------------------------------------------
# ---- The path and its failures (#24) ---------------------------------------
# ---------------------------------------------------------------------------

def test_the_env_override_resolves_at_import():
    """AGENT_PROVIDERS_FILE is read once, at import, exactly like
    config.DB_PATH and the other three siblings -- so a launch-time env
    var redirects every reader (load, save, messages) without any call
    site changing. A subprocess because the value is frozen by the time
    THIS process imported the module; importlib.reload would mutate
    shared module state under the rest of the suite."""
    import subprocess
    import sys

    code = (
        "import os; os.environ['AGENT_PROVIDERS_FILE'] = 'elsewhere.json'; "
        "import credentials; "
        "assert credentials.LLM_PROVIDERS_FILE == 'elsewhere.json', "
        "credentials.LLM_PROVIDERS_FILE"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True,
                       cwd=_project_root())
    assert r.returncode == 0, r.stderr


def _project_root() -> str:
    """The directory whose import finds this project's credentials.py --
    whatever a previous test did to the process cwd."""
    return os.path.dirname(os.path.abspath(credentials.__file__))


def test_the_default_stays_cwd_relative():
    """Deliberate (#24): per-checkout state is what a repo-local tool
    wants, and repo-relative would share one key file across clones. The
    default equals the bare name, resolved against wherever python
    runs."""
    import subprocess
    import sys

    code = ("import credentials; "
            "assert credentials.LLM_PROVIDERS_FILE == 'providers.json', "
            "credentials.LLM_PROVIDERS_FILE")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=_project_root())
    assert r.returncode == 0, r.stderr


def test_api_initialization_names_the_file_when_it_is_absent(_in_tmp):
    """The old failure surfaced as 'Unknown provider: ANTHROPIC' -- which
    says nothing about a file, a path, or a working directory (#24's
    founding complaint). The message names what was checked and the one-
    line remedy. Still ValueError: callers catch the type, not the text."""
    from core.client import api_initialization

    with pytest.raises(ValueError, match="was not found in"):
        api_initialization("ANTHROPIC")


def test_api_initialization_lists_what_is_configured_on_a_typo(_in_tmp):
    """/model refuses with the configured set for exactly this reason --
    the typo is answered where it was made. The call path now agrees."""
    (_in_tmp / "providers.json").write_text(
        json.dumps({"OPENAI": {"API_KEY": "k", "API_URL": "",
                               "is_v1_compatible": True}}),
        encoding="utf-8")

    from core.client import api_initialization

    with pytest.raises(ValueError,
                       match=r"Unknown provider: ANTHROPIC\. Configured: OPENAI"):
        api_initialization("ANTHROPIC")


def test_load_credentials_speaks_the_same_two_messages(_in_tmp):
    """load_credentials has no production caller today, but it is public
    API one grep away from being one -- mirroring through the same two
    builders is what keeps its messages from drifting apart from the real
    raiser's again."""
    from credentials import load_credentials

    with pytest.raises(ValueError, match="was not found in"):
        load_credentials("ANTHROPIC")

    (_in_tmp / "providers.json").write_text(
        json.dumps({"OPENAI": {"API_KEY": "k", "API_URL": "",
                               "is_v1_compatible": True}}),
        encoding="utf-8")
    with pytest.raises(ValueError,
                       match=r"Configured: OPENAI \(file: providers\.json\)"):
        load_credentials("ANTHROPIC")


def test_an_empty_but_present_file_is_not_reported_as_missing(_in_tmp):
    """A present-but-empty providers.json skips the missing-file branch:
    'the file is not there' would be false, and 'Configured: none' in the
    unknown-provider message describes it exactly."""
    (_in_tmp / "providers.json").write_text("{}", encoding="utf-8")

    from core.client import api_initialization

    with pytest.raises(ValueError, match=r"Configured: none"):
        api_initialization("ANTHROPIC")


# ---------------------------------------------------------------------------
# ---- Batch 35: resolution once the harness stops living in the cwd ---------
# ---------------------------------------------------------------------------


def test_the_env_file_is_found_from_the_working_directory(tmp_path):
    """The npm channel's one genuine code fix, pinned.

    `env_secrets` used a bare `load_dotenv()`, which calls
    `find_dotenv(usecwd=False)` -- and that walks up from THE CALLING FRAME'S
    FILE, i.e. env_secrets.py's own directory, not from cwd. In a checkout
    those are the same directory, which is the entire reason this looked
    correct for the project's whole life. Installed anywhere else (npm puts
    the harness under node_modules/) the walk starts in the wrong tree: it
    never reaches the user's .env, and it can pick up a stray one sitting
    above the install prefix.

    THIS TEST MUST RUN A SCRIPT FILE, NOT `python -c`. dotenv's
    `_is_interactive()` is true when `__main__` has no `__file__`, which is
    exactly the case under -c, and it sends find_dotenv down its os.getcwd()
    branch. A -c version of this test passes against the BUG -- it was
    written that way first and reported a false green. The subprocess also
    has to be fresh, because load_dotenv runs at import and this suite has
    already imported env_secrets.
    """
    import subprocess
    import sys

    (tmp_path / ".env").write_text("VENASTINE_ENV_PROBE=found\n", encoding="utf-8")
    script = tmp_path / "probe.py"
    script.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {_project_root()!r})\n"
        "import env_secrets\n"
        "print(os.environ.get('VENASTINE_ENV_PROBE', 'MISSING'))\n",
        encoding="utf-8")

    r = subprocess.run([sys.executable, str(script)], capture_output=True,
                       text=True, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "found", (
        f"a .env in the working directory was not loaded (got "
        f"{r.stdout.strip()!r}). load_dotenv() must be given an explicit "
        f"path -- see this test's docstring; the bare call resolves from "
        f"env_secrets.py's directory and only looks right from a checkout.")


def test_agent_env_file_overrides_the_working_directory(tmp_path):
    """AGENT_ENV_FILE is the escape hatch, named to match
    AGENT_PROVIDERS_FILE and resolved at import for the same reason. It has
    to beat a .env sitting in cwd, or it is not an override."""
    import subprocess
    import sys

    (tmp_path / ".env").write_text("VENASTINE_ENV_PROBE=from_cwd\n", encoding="utf-8")
    chosen = tmp_path / "elsewhere.env"
    chosen.write_text("VENASTINE_ENV_PROBE=from_override\n", encoding="utf-8")

    script = tmp_path / "probe.py"
    script.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {_project_root()!r})\n"
        "import env_secrets\n"
        "print(os.environ.get('VENASTINE_ENV_PROBE', 'MISSING'))\n",
        encoding="utf-8")

    env = dict(os.environ, AGENT_ENV_FILE=str(chosen))
    r = subprocess.run([sys.executable, str(script)], capture_output=True,
                       text=True, cwd=str(tmp_path), env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "from_override", (
        f"AGENT_ENV_FILE did not win over the cwd .env (got "
        f"{r.stdout.strip()!r}).")


def test_an_absolute_providers_path_does_not_name_a_directory_it_never_checked(
        monkeypatch, tmp_path):
    """#24's own defect, in the shape the npm launcher introduced.

    The message hardcoded os.getcwd(). AGENT_PROVIDERS_FILE has always
    accepted an absolute path, and the launcher sets one when the working
    directory has no providers.json -- at which point the error named a
    directory the code never looked in and told the user to create the file
    there. That is the same "says nothing about what was actually checked"
    failure #24 was filed for.

    The relative case keeps the cwd clause, because there it is the missing
    half of the answer rather than a wrong one. Both existing matchers in
    this file cover that direction.
    """
    absolute = tmp_path / "nested" / "providers.json"
    monkeypatch.setattr(credentials, "LLM_PROVIDERS_FILE", str(absolute))

    message = credentials.no_providers_message()

    assert str(absolute) in message
    assert "was not found in" not in message, (
        f"an absolute path still reports a working directory: {message!r}. "
        f"The path is the whole answer; naming cwd beside it points at the "
        f"wrong place to create the file.")
    assert "Copy providers.json.example" in message, (
        "the remedy must survive -- it is the half of #24 that made the "
        "message actionable.")



# ---------------------------------------------------------------------------
# ---- The write is atomic too (batch 108) -----------------------------------
# ---------------------------------------------------------------------------
#
# #19 was about the MODE, and it left the other half of the same line
# unexamined. `_write_secret_json` wrote `O_TRUNC` straight over the live
# file, so a crash, a full disk or a kill between the truncate and the
# flush destroyed every stored provider key at once -- and unlike the
# secrets store, this file is in use for every user who has ever saved a
# credential. `json_store.py` was extracted to delete exactly this pattern
# and says so in its own docstring; these two files were the last stores
# still carrying a private copy of it.

class _DiesMidWrite:
    """A value `json.dump` refuses, reached after bytes are already out.

    `json.dump` writes incrementally, so this is a write that dies PARTWAY
    rather than one that never starts -- which is the only kind that can
    tell a truncate-in-place apart from a temp-file-and-replace.
    """


def _dying_dump(monkeypatch):
    """Make the next provider write fail once it has begun."""
    real = json.dump

    def dump(payload, handle, **kwargs):
        payload = dict(payload)
        payload["zzz_dies_here"] = _DiesMidWrite()
        return real(payload, handle, **kwargs)

    monkeypatch.setattr(json, "dump", dump)


def test_an_interrupted_write_leaves_the_previous_keys(_in_tmp, monkeypatch):
    credentials.save_credentials("openai", "sk-the-first-key")
    before = (_in_tmp / "providers.json").read_bytes()
    _dying_dump(monkeypatch)

    with pytest.raises(TypeError):
        credentials.save_credentials("anthropic", "sk-ant-the-second-key")

    assert (_in_tmp / "providers.json").read_bytes() == before
    data = credentials.load_provider_data()
    assert data["openai"]["API_KEY"] == "sk-the-first-key"


def test_an_interrupted_write_leaves_no_temp_file(_in_tmp, monkeypatch):
    credentials.save_credentials("openai", "sk-the-first-key")
    _dying_dump(monkeypatch)

    with pytest.raises(TypeError):
        credentials.save_credentials("anthropic", "sk-ant-the-second-key")

    assert not (_in_tmp / "providers.json.tmp").exists()


def test_the_bare_relative_default_still_writes(_in_tmp):
    """`LLM_PROVIDERS_FILE` defaults to the bare name `providers.json`, so
    `os.path.dirname` of it is `""` -- and `os.makedirs("")` RAISES rather
    than doing nothing.

    Nothing could reach that before batch 108, because all six of
    `write_json_atomic`'s callers built an absolute path. Routing the two
    secret stores through it made a path SHAPE able to decide whether the
    write works, and this is the shape that ships.
    """
    credentials.save_credentials("openai", "sk-a-key-for-the-bare-path")

    assert (_in_tmp / "providers.json").exists()
    assert (credentials.load_provider_data()["openai"]["API_KEY"]
            == "sk-a-key-for-the-bare-path")
