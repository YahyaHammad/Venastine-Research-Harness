"""
test_process_memos.py

#233 (TECHNICAL_DEBT 80). Production keeps answers in module globals --
caches, warn-once sets, quarantines, a store's unlocked plaintext -- and a
test that leaves one behind decides what the next test on its worker sees.
Under `--dist loadscope` WHICH test is next is a scheduling lottery, so a
leak there is a failure that moves when a file is added.

`isolate_sandbox_probes` closed this for `security/sandbox.py` in batch 107
and `test_probe_isolation.py` holds that one module to its fixture. Every
other module was left to "the fixture in the file that tests it", and the
review found five memos in that state. Re-counted for the fix, it was more:
the effort-level cache inside the very fixture that owns its siblings, a
notice set nothing reset at all, and two warn-once sets batches 220 and 221
added without anyone noticing -- which is the argument for a census rather
than a list somebody keeps.

So this file is the census. It reads every production module's source for
state with a process lifetime, in `test_probe_isolation._module_state`'s two
shapes, and requires each name to be reset by an autouse fixture in
`tests/conftest.py` or exempted below with the reason its value cannot
decide what a test asserts. Both directions are checked, so a name that
disappears from the tree cannot stay on a list as a false account.

WHAT IT DOES NOT COUNT: memos held by a decorator (`functools.lru_cache`)
or inside an object's attributes. Those have no module-level NAME to read,
and the two `lru_cache`s in the tree today are `security/sandbox.py`'s,
which its own reset helpers own.
"""

import ast
import os

from tests import conftest
from tests.test_docs_consistency import _production_python, _project_root

#: Container constructors, beside the literals: a name bound to one of these
#: at module level is state that can change without being rebound.
_CONTAINERS = frozenset({
    "dict", "list", "set", "defaultdict", "OrderedDict", "deque", "Counter",
    "WeakSet", "WeakValueDictionary", "WeakKeyDictionary",
})


def _callee(call):
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None)


def _memos(path):
    """Every name `path` holds process-lifetime state in, read off its
    source.

    `test_probe_isolation._module_state`'s two shapes, for every module
    rather than one:

      * a name some function declares `global` -- a name the module
        REASSIGNS, which is every lazily computed answer and every flag.
      * a lowercase module-level container, or a `*Cache` instance -- one
        it can change in place without ever rebinding.

    UPPERCASE is exempt in the second shape only, as there: a module-level
    UPPERCASE dict is a table by convention, while an UPPERCASE name a
    function declares `global` is still being reassigned."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        callee = _callee(value) if isinstance(value, ast.Call) else ""
        if not (isinstance(value, (ast.Dict, ast.Set, ast.List))
                or (callee or "") in _CONTAINERS
                or (callee or "").endswith("Cache")):
            continue
        for target in targets:
            if (isinstance(target, ast.Name)
                    and not target.id.lstrip("_").isupper()):
                names.add(target.id)
    return {name for name in names
            if not (name.startswith("__") and name.endswith("__"))}


def _census():
    """{(relative path, name)} across production. The root `conftest.py`
    is the suite's fake-SDK installer, not production, and is skipped."""
    root = _project_root()
    found = set()
    for path in _production_python():
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        if os.path.basename(relative) == "conftest.py":
            continue
        found.update((relative, name) for name in _memos(path))
    return found


def _path_of(module_name):
    return module_name.replace(".", "/") + ".py"


#: Memos an autouse fixture OTHER than `reset_process_memos` already resets,
#: named with the fixture, so a reader looking for the reset finds it.
RESET_ELSEWHERE = {
    ("config_schema.py", "_cached"): "restore_config_schema_cache",
    ("core/client.py", "_client_cache"): "clear_client_cache",
    ("core/client.py", "_context_window_cache"): "clear_client_cache",
    ("core/client.py", "_context_window_failures"): "clear_client_cache",
    ("core/client.py", "_effort_levels_cache"): "clear_client_cache",
    ("core/session.py", "_overrides"): "clear_client_cache",
    ("core/config_loader.py", "_state"): "clear_config_loader_state",
    ("core/reasoning/scholar.py", "_cache"): "clear_scholar_cache",
    ("tools/builtin/arxiv.py", "_cache"): "clear_network_tool_caches",
    ("tools/builtin/fetch_url.py", "_body_cache"):
        "clear_network_tool_caches",
    ("tools/builtin/web_search.py", "_cache"): "clear_network_tool_caches",
    ("security/sandbox.py", "_runtime_probe"): "isolate_sandbox_probes",
    ("security/sandbox.py", "_wsl_probe"): "isolate_sandbox_probes",
    ("security/sandbox.py", "_ssh_probe"): "isolate_sandbox_probes",
    ("security/sandbox.py", "_ssh_known_hosts_path"):
        "isolate_sandbox_probes",
    ("security/sandbox.py", "_ssh_auth_rejected"): "isolate_sandbox_probes",
    ("security/sandbox.py", "_sudo_rejected"): "isolate_sandbox_probes",
}

#: State whose value cannot decide what a test asserts, with the reason.
#: An exemption is a claim about the code, so each says what makes it true.
EXEMPT = {
    ("config_schema.py", "ToolPermissions"):
        "a dataclass built once from the schema's tool list, and config.py "
        "binds it at import -- a rebuilt class would make every instance "
        "made before it a different type from every one made after",
    ("config_schema.py", "ToolApprovals"):
        "the same, for the approvals dataclass",
    ("core/file_mentions.py", "_indexes"):
        "a WeakSet of the live mention indexes, kept so shutdown can stop "
        "their scanners; an entry leaves when its owner is collected, and "
        "it holds no answer",
    ("main.py", "_STDIN_READER"):
        "the process's one reader thread on stdin; a reset would start a "
        "second reader racing the first, so the `cli_stdin` fixture "
        "replaces the accessor `_stdin_reader` per test instead",
    ("mcp_client/client.py", "_loop"):
        "the background event loop MCP sessions run on, which lives as long "
        "as the process by design",
    ("mcp_client/client.py", "_thread"):
        "the thread that loop runs in",
    ("mcp_client/client.py", "_holders"):
        "the clients connected on that loop, so the last to disconnect "
        "stops it (PL30); a client leaves as it disconnects, and a "
        "leftover only keeps the loop running, which `_loop` already does",
    ("prompts/system_prompts.py", "passes_source_files"):
        "a constant table, pass id to file name; nothing mutates it",
    ("security/posture.py", "_posture"):
        "bound at import from config, and every seam that changes it "
        "restores it: `set_posture` and `rebind_posture` through "
        "monkeypatch, `override_for_tests` in its finally",
}


def _reset_here():
    return {(_path_of(module), name)
            for module, names, _reset in conftest.PROCESS_MEMOS
            for name in names}


def _autouse_fixtures():
    """The names of the autouse fixtures tests/conftest.py defines."""
    with open(conftest.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call)
                    and any(k.arg == "autouse"
                            and isinstance(k.value, ast.Constant)
                            and k.value.value is True
                            for k in decorator.keywords)):
                found.add(node.name)
    return found


class TestEveryMemoIsAccountedFor:

    def test_every_memo_in_production_is_reset_or_exempt(self):
        accounted = _reset_here() | set(RESET_ELSEWHERE) | set(EXEMPT)
        unaccounted = sorted(_census() - accounted)
        assert not unaccounted, (
            f"production holds process-lifetime state in {unaccounted}, and "
            f"no autouse fixture resets it. A test that leaves a value there "
            f"decides what the next test on its worker sees (#233). Add it "
            f"to PROCESS_MEMOS in tests/conftest.py with how to reset it -- "
            f"or to RESET_ELSEWHERE here if another autouse fixture already "
            f"does, or to EXEMPT with the reason its value cannot decide "
            f"what a test asserts.")

    def test_every_accounted_memo_still_exists(self):
        """The other direction: a name on a list that is gone from the
        tree is a false account, and the list would read as covering
        something that is no longer there."""
        census = _census()
        for label, names in (("PROCESS_MEMOS", _reset_here()),
                             ("RESET_ELSEWHERE", set(RESET_ELSEWHERE)),
                             ("EXEMPT", set(EXEMPT))):
            stale = sorted(names - census)
            assert not stale, (
                f"{label} names {stale}, which the census no longer finds "
                f"in production. Remove the entry, or fix the census if "
                f"the state is still there in a shape it does not read.")

    def test_no_memo_is_accounted_for_twice(self):
        lists = (_reset_here(), set(RESET_ELSEWHERE), set(EXEMPT))
        for index, one in enumerate(lists):
            for other in lists[index + 1:]:
                assert not one & other, sorted(one & other)

    def test_every_fixture_named_as_a_reset_is_autouse_in_conftest(self):
        """RESET_ELSEWHERE is only true while the fixtures it names exist
        and run for every test."""
        autouse = _autouse_fixtures()
        missing = sorted({*RESET_ELSEWHERE.values(), "reset_process_memos"}
                         - autouse)
        assert not missing, (
            f"{missing} are named as resets but are not autouse fixtures in "
            f"tests/conftest.py")


class TestTheCensusReadsBothShapes:
    """THE CONTROL. Without it the census passes by finding nothing."""

    def test_it_finds_a_container_and_a_reassigned_name(self):
        found = _census()
        assert ("core/client.py", "_effort_levels_cache") in found
        assert ("core/client.py", "_google_budget_support") in found
        assert ("core/reasoning/scholar.py", "_cache") in found
        assert len(found) >= 30, sorted(found)

    def test_a_constant_table_is_not_mistaken_for_state(self):
        assert not any(name == "_DANGEROUS_FLAGS" for _path, name in _census())


class TestWhatOneTestLeavesForTheNext:
    """The behavioural half, as a PAIR in one class so `--dist loadscope`
    runs them in order on one worker: the first dirties every memo
    `reset_process_memos` owns, plus the effort-level cache this issue
    found missing from `clear_client_cache`, and the second asserts each
    is back. A
    single test asserting they are empty would pass whether or not the
    fixture resets anything -- it never dirtied them."""

    def test_a_test_may_dirty_every_memo(self):
        import config_edit
        import core.client as client
        import core.compaction as compaction
        import core.loop as loop
        from mcp_client import registration
        from security import permissions, posture, secrets

        config_edit._document_cache = (("stamp",), object())
        client._effort_levels_cache[("P", "m")] = ["low"]
        client._google_budget_support = False
        client._ineligible_explained = True
        for name in ("_context_window_warned", "_floor_warned",
                     "_read_warned", "_yield_logged"):
            getattr(compaction, name).add(("P", "m"))
        loop._headless_notices_shown.add(frozenset({"x"}))
        loop._image_notices_shown.add(("P", "chat", frozenset({"x"})))
        registration._registered["mcp__probe__x"] = object()
        permissions.set_dynamic_approval_default("mcp__probe__x", False)
        posture.current()
        secrets._state = secrets._Unlocked(bytearray(b"pw"), {}, None)

        assert posture._read is True
        assert secrets.is_unlocked()

    def test_and_the_next_one_finds_every_memo_reset(self):
        import config_edit
        import core.client as client
        import core.compaction as compaction
        import core.loop as loop
        from mcp_client import registration
        from security import permissions, posture, secrets

        assert config_edit._document_cache is None
        assert not client._effort_levels_cache
        assert client._google_budget_support is None
        assert client._ineligible_explained is False
        for name in ("_context_window_warned", "_floor_warned",
                     "_read_warned", "_yield_logged"):
            assert not getattr(compaction, name), name
        assert not loop._headless_notices_shown
        assert not loop._image_notices_shown
        assert not registration._registered
        assert not permissions._dynamic_approval_defaults
        assert posture._read is False
        assert not secrets.is_unlocked()
