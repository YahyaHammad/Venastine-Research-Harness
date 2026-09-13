"""config.py -- the harness's settings, as module attributes.

THE VALUES LIVE IN `config.yaml` NOW. This module is the compatibility layer
that publishes them: it reads the file once, through `config_schema.py`, and
binds each value as a plain module global, so `config.MAX_TOKENS` means what
it always meant to the 30 production modules and ~60 test files that read it.

WHERE TO LOOK FOR WHAT:

  * `config.yaml`              the values, with a short note on what each DOES
  * `CONFIG_ARCHITECTURE.md`   why each value is what it is -- the whole
                               history, section by section, under the same
                               names. Read it before changing a number.
  * `config_schema.py`         the schema, the environment overrides, and the
                               argument for why the file has exactly one
                               location and no override variable

FOUR THINGS ABOUT THIS FILE ARE DELIBERATE.

1. THE GLOBALS ARE PLAIN AND MUTABLE, and are bound EAGERLY rather than
   through a module `__getattr__`. Four access patterns in this codebase
   depend on it and a lazy proxy breaks at least one of each:
   `monkeypatch.setattr(config, ...)`, a bare `config.X = v` (an autouse
   fixture in `tests/conftest.py` does this for every test in the suite),
   `mocker.patch.dict(config.__dict__, {...})`, and
   `getattr(config, "<NAME>", default)` -- which must raise `AttributeError`,
   not `KeyError`, for a name that does not exist.

2. THE READ HAPPENS AT IMPORT, and so does the environment fold-in.
   `security/posture.py` depends on it: `config` binding at import is what
   makes `os.environ[...] = ...` unable to move the security posture, where
   anything reading `os.environ` at CALL time would follow the mutation (UN1,
   and batch 37's asymmetry). `tools/builtin/shell.py` validates the approval
   mode at its own import, so the values must be final before it runs.
   Nothing re-reads the file; an edit to `config.yaml` takes effect at the
   next launch.

3. THERE IS STILL NO LOGIC HERE. No `if`, no function that decides anything,
   no `os.environ` read -- those moved to `config_schema.py` rather than
   arriving here. ARCHITECTURE.md §4.1's rule is unchanged in substance: a
   function that reads these values and makes a decision belongs in whichever
   file consumes the setting.

4. `ToolPermissions` / `ToolApprovals` ARE REAL DATACLASSES, rebuilt from the
   schema with `config.yaml`'s booleans as their field defaults. Not the
   pydantic models themselves, because the codebase depends on the shape:
   `security/permissions.py` calls `config.ToolPermissions()` with no
   arguments on every check, tests mutate an instance and then swap this
   module's attribute for a factory returning it, and
   `tests/test_docs_consistency.py` enumerates `vars(instance)` to check
   README's approval table names every registered tool. The FIELD NAMES are
   declared in `config_schema.py`, in Python, so D24's build-time check that
   every registered tool has a field cannot be turned into a runtime surprise
   by an omission in a data file.
"""

import config_schema

# Read, validate, apply the environment. Once, here, before anything else in
# the process has run. A missing file, an unknown key, a missing key, a
# duplicated key or a value of the wrong type raises `ValueError` naming the
# file and the key -- loudly, at startup, rather than as a wrong default
# three hours into a run.
#
# `force=True` IS LOAD-BEARING, and it is the one thing about this file that
# is not obvious. IMPORTING THIS MODULE is what "bind the configuration now"
# means, so this module -- not the schema's cache -- decides when the file is
# read. Without the flag, `config_schema`'s cache outlives a re-import of
# `config`, and re-importing `config` under a changed environment is a real
# technique in this repo rather than a hypothetical: `test_storage_e2e`'s
# `real_storage` fixture pops `sqlmodel`, `config`, `database` and `storage`
# out of `sys.modules`, sets `APP_DB_PATH` to a throwaway file, and imports
# them again to get real SQLite. `config_schema` is not in that list and
# cannot be -- the fixture predates it -- so a cached model would hand the
# fresh `config` the OLD `db_path` and point the engine at the developer's
# real `app.db`. That is how it was found: one test comparing a migrated
# column against a fresh one, reading a database that had accumulated
# migrations.
#
# The cache still does its job. `core/config_loader.py` reads five values off
# `config_schema.current()`, which returns whatever this import bound, so the
# two cannot disagree -- and a re-import replaces the cache rather than
# bypassing it.
_config = config_schema.load(force=True)

# Bind every value as a module global. `as_module_namespace` returns
# `{UPPER_CASE_NAME: value}` for every field in `config.yaml` except the two
# permission tables, plus the two values that have no key because they are
# derived from the environment rather than defaulted: OUTPUT_DIR (composed
# from AGENT_OUTPUT_DIR / AGENT_WORKSPACE) and WORKSPACE_DIR_EXPLICIT (the
# PRESENCE of AGENT_WORKSPACE, never its value).
globals().update(config_schema.as_module_namespace(_config))

ToolPermissions = config_schema.tool_permissions_dataclass()
ToolApprovals = config_schema.tool_approvals_dataclass()
