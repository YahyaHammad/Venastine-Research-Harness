"""config_schema.py -- the schema for `config.yaml`, and the only reader of it.

WHY THIS FILE EXISTS SEPARATELY FROM `config.py`.

`config.py` is imported by 30 production modules and ~60 test files, and
`core/config_loader.py` imported it too when this file was written. If the
shim had reached back into `core/config_loader` for the values, that would
have been a cycle -- and `config.py` was a LEAF module, importing `os` and
`dataclasses` and nothing else, which is the property that made it safe for
`security/` to depend on. So the reader lives here, one hop away, and this
module imports the stdlib, `ruamel.yaml` and `pydantic` and NOTHING
first-party.

The loader reads `config_schema` rather than `config` now, so the cycle is
gone from both ends -- which is exactly why this rule has to be stated
rather than left to the import graph to imply. Keep it: an import of
anything under `core/`, `security/` or `tools/` from here puts the cycle
back by a longer route, and there would be nothing in the graph to stop it.

Three rules about `config.yaml` are load-bearing rather than tidy. All three
are argued at length in CONFIG_ARCHITECTURE.md; this is the summary.

1. ONE PATH, RESOLVED FROM `__file__`, WITH NO ENVIRONMENT OVERRIDE.
   `config.yaml` ships inside the harness. There is no user tier, no project
   tier and no `AGENT_CONFIG_FILE`. That absence is the whole security
   argument: `settings.json` rejects `shell_approval_mode`,
   `ensemble_models`, `critic_model` and `embedder_model` BY NAME (R12, E2,
   G7, SQ7) because a project's `settings.json` beats the user's and arrives
   with a directory you cloned. A file with exactly one location, inside the
   harness, carries none of that risk -- and an override variable would hand
   it back. `AGENT_ENV_FILE` and `APP_DB_PATH` make such a variable look like
   the obvious next step here. It is not; those redirect STATE, and this is a
   shipped asset.

2. READ ONCE, AT IMPORT, WITH THE ENVIRONMENT FOLDED IN THERE.
   `security/posture.py` depends on `config` binding at import: that is what
   makes `os.environ[...] = ...` unable to move the security posture, while
   anything reading `os.environ` at CALL time would follow the mutation
   (UN1, and batch 37's asymmetry). `tools/builtin/shell.py` also validates
   the approval mode at ITS import, so the values must be final before any
   other module runs. Nothing re-reads the file in a live process; an edit
   takes effect at the next launch.

3. EVERY FIELD IS REQUIRED AND UNKNOWN KEYS ARE REFUSED.
   No field here has a default. A `config.yaml` missing a key is a startup
   error naming that key, which is the D24 defect made impossible rather than
   merely fixed: `fetch_url` was registered and documented as working while
   having no `ToolPermissions` field, so `getattr(..., False)` denied every
   call to it for as long as the tool existed. A default here would recreate
   exactly that -- a value nobody chose, silently in force.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Annotated, Any, Literal, Optional

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# ---------------------------------------------------------------------------
# ---- Where the file is ----------------------------------------------------
# ---------------------------------------------------------------------------

CONFIG_FILENAME = "config.yaml"
#: The harness root. This file lives at the root beside `config.py`, so the
#: root is its own directory -- resolved from `__file__` rather than from cwd
#: for `core/config_loader.HARNESS_ROOT`'s reason: the harness tier ships with
#: the harness, and every shipped asset in this project resolves this way.
HARNESS_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HARNESS_ROOT, CONFIG_FILENAME)


# ---------------------------------------------------------------------------
# ---- Scalar types ---------------------------------------------------------
# ---------------------------------------------------------------------------

def _number_only(value: Any) -> Any:
    """Reject a bool or a string where a number is wanted.

    `bool` is a subclass of `int` in Python and pydantic's lax `float` takes
    both `True` and `"9.5"`. `core/config_loader._type_ok` goes to the same
    trouble for `settings.json` and says why: a `True` sailing through as `1`
    is a value nobody wrote. Ints ARE accepted, so `8` is a legal way to
    write `8.0` in YAML.
    """
    if isinstance(value, bool):
        raise ValueError("expected a number, got a boolean")
    if isinstance(value, str):
        raise ValueError("expected a number, got a quoted string")
    return value


#: A float that accepts an int but never a bool or a quoted string.
#:
#: `allow_inf_nan=False` is the half that is not about tidiness. YAML spells
#: them `.nan` and `.inf`, both parse to legal floats, and a NaN makes every
#: `<` and `>` comparison FALSE -- so `compaction_trigger_fraction: .nan`
#: would mean compaction never fires, silently, with no error ever raised.
#: Refused here once rather than per field.
Number = Annotated[float, BeforeValidator(_number_only),
                   Field(allow_inf_nan=False)]

# THE THREE RANGE SHAPES, MIRRORED FROM THE SETTINGS VALIDATOR.
#
# `core/config_loader.py` already expresses exactly these three for
# `settings.json` -- `_UNIT`, `_POSITIVE_INT`, `_NON_NEGATIVE_INT` -- and the
# same values arriving from `config.yaml` had no range check at all, so
# `max_iterations: 0` and `compaction_trigger_fraction: 5.0` were accepted.
# Mirrored rather than imported, deliberately: importing from `core/` is the
# cycle this module exists to avoid. `security/protected_paths.py` carries the
# same kind of deliberate duplicate and says so about `_user_config_dir`.

#: A number in [0, 1]. Weights, ratios, fractions and authority scores.
Unit = Annotated[float, BeforeValidator(_number_only),
                 Field(ge=0.0, le=1.0, allow_inf_nan=False)]

#: A count that must be at least one. A ceiling of zero is not a tighter
#: bound, it is a harness that cannot do the thing at all.
PositiveInt = Annotated[int, Field(strict=True, gt=0)]

#: A count where zero is a real answer -- no retries, no injected memories.
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]

#: A duration in seconds that must be positive.
PositiveNumber = Annotated[float, BeforeValidator(_number_only),
                           Field(gt=0.0, allow_inf_nan=False)]

#: A multiplier where zero is meaningful (`1 + tolerance`).
NonNegativeNumber = Annotated[float, BeforeValidator(_number_only),
                              Field(ge=0.0, allow_inf_nan=False)]

#: "always" / "tiered" / "never". A closed vocabulary rather than a plain
#: string, so an unknown mode is a startup failure and never a policy that
#: quietly means something else -- one direction of a fallback asks about
#: everything and the other about nothing, and a typo cannot pick.
ShellApprovalMode = Literal["always", "tiered", "never"]

#: "rederive" / "chain". Validated here AND in
#: `config_loader.effective_compaction`, which checks the value a
#: `settings.json` or a `/compact --strength N` supplied against this list.
CompactionStrategy = Literal["rederive", "chain"]


class _Model(BaseModel):
    """Common posture: no unknown keys, no mutation after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# ---- The two permission tables --------------------------------------------
# ---------------------------------------------------------------------------
#
# The FIELD NAMES stay declared in Python while the booleans come from YAML,
# and the split is deliberate. `tools/registry.py` checks at import that every
# statically registered tool has a field in both tables (D24); with the names
# in YAML, omitting one would turn that build-time check into a runtime
# surprise in a data file. With the names here and the fields required, a
# `config.yaml` missing `shell` cannot start the harness at all.
#
# Field ORDER matters and follows `config.py`'s: `ToolPermissions` and
# `ToolApprovals` are rebuilt as real dataclasses from these models, and
# `tests/test_docs_consistency.py` reads `vars(config.ToolPermissions())` to
# check README's approval table names every tool.

class ToolPermissionsModel(_Model):
    """Whether each registered tool may be called at all.

    `security/permissions.is_tool_allowed` constructs `ToolPermissions()`
    fresh on every call and returns before any context or approval logic, so
    these are a global floor that no agent context or skill can widen (D14).
    """

    web_search: StrictBool
    fetch_url: StrictBool
    get_time: StrictBool
    arxiv_search: StrictBool
    symbolic_math: StrictBool
    linear_algebra: StrictBool
    probability_stats: StrictBool
    discrete_math: StrictBool
    logic: StrictBool
    geometry: StrictBool
    read: StrictBool
    write: StrictBool
    edit: StrictBool
    shell: StrictBool
    load_skill: StrictBool
    spawn_subagent: StrictBool
    pin: StrictBool
    unpin: StrictBool
    remember: StrictBool
    read_project_doc: StrictBool
    write_project_doc: StrictBool
    ask_user: StrictBool
    todo_write: StrictBool


class ToolApprovalsModel(_Model):
    """Whether each tool needs a human yes before it runs.

    Approval ORs across every layer, so a `True` here is a one-way ratchet
    that can only ever add prompts. `shell` is the case to read carefully: it
    is the RATCHET and not the gate -- `shell_approval_mode` is the gate.
    """

    web_search: StrictBool
    fetch_url: StrictBool
    get_time: StrictBool
    arxiv_search: StrictBool
    symbolic_math: StrictBool
    linear_algebra: StrictBool
    probability_stats: StrictBool
    discrete_math: StrictBool
    logic: StrictBool
    geometry: StrictBool
    read: StrictBool
    write: StrictBool
    edit: StrictBool
    shell: StrictBool
    load_skill: StrictBool
    spawn_subagent: StrictBool
    pin: StrictBool
    unpin: StrictBool
    remember: StrictBool
    read_project_doc: StrictBool
    write_project_doc: StrictBool
    ask_user: StrictBool
    todo_write: StrictBool


# ---------------------------------------------------------------------------
# ---- The whole file -------------------------------------------------------
# ---------------------------------------------------------------------------
#
# Annotations preserve the RUNTIME TYPES consumers already rely on, which is
# why several are looser than they could be:
#
#   * `similarity_calibration` stays `dict[str, dict[str, float]]` rather than
#     a nested model, because `source_scoring.py` subscripts the band with
#     `band["floor"]`.
#   * `ensemble_models` is `list[Any] | None`, not a model list, because
#     `orchestrator._ensemble_roster` validates the entries itself and reports
#     the malformed one -- a schema that rejected them first would move that
#     error message and say less.
#   * `critic_model` / `embedder_model` stay plain dicts, because
#     `core/pipeline_models.resolve` treats them as dicts and the user-tier
#     store outranks them (SQ7).

class HarnessConfig(_Model):
    """Every value in `config.yaml`, validated.

    Field order is `config.py`'s section order, so the two can be diffed.
    """

    # -- Model / loop ------------------------------------------------------
    model_name: str
    max_tokens: PositiveInt
    max_iterations: PositiveInt

    # -- Subagents ---------------------------------------------------------
    # Zero is a real answer here: `subagent_depth >= this` then refuses
    # every spawn, which is a harness with subagents turned off.
    subagent_max_depth: NonNegativeInt
    subagent_max_parallel: PositiveInt

    # -- Deep research pipeline -------------------------------------------
    max_pipeline_retries: NonNegativeInt
    max_json_retries: NonNegativeInt

    # -- Ensemble mode -----------------------------------------------------
    ensemble_mode: StrictBool
    ensemble_models: Optional[list[Any]]

    # -- Sampling-parameter support ---------------------------------------
    models_rejecting_sampling_params: frozenset[str]

    # -- Critic / embedder routing ----------------------------------------
    critic_model: Optional[dict[str, Any]]
    embedder_model: Optional[dict[str, Any]]
    similarity_calibration: dict[str, dict[str, Unit]]
    default_similarity_calibration: dict[str, Unit]
    embedder_prefixes: dict[str, dict[str, str]]

    # -- Source authority --------------------------------------------------
    domain_authority_classes: dict[str, Unit]
    default_domain_authority: Unit
    domain_authority_suffixes: dict[str, str]
    authority_adjustment_cap: Unit

    # -- The sources/ artifact --------------------------------------------
    persist_source_text: StrictBool

    # -- Scholarly authority (OpenAlex) -----------------------------------
    scholar_lookup: StrictBool
    scholar_api_url: str
    scholar_timeout_s: PositiveNumber
    scholar_max_retries: NonNegativeInt
    scholar_cache_ttl_s: NonNegativeInt
    scholar_mailto: str
    scholar_venue_weight: Unit
    scholar_citation_weight: Unit
    scholar_author_weight: Unit
    scholar_venue_credit: dict[str, Unit]
    scholar_h_saturation: PositiveInt
    scholar_min_cohort_size: PositiveInt
    scholar_min_citation_age_days: NonNegativeInt
    scholar_retracted_authority: Unit

    # -- Reasoning effort --------------------------------------------------
    default_effort: Optional[str]
    model_effort_levels: dict[str, list[str]]
    default_effort_levels: list[str]
    google_thinking_budgets: dict[str, StrictInt]

    # -- Database ----------------------------------------------------------
    db_path: str

    # -- File-ops workspace ------------------------------------------------
    # `output_dir` and `workspace_dir_explicit` are deliberately absent --
    # see DERIVED_VALUES below.
    workspace_dir: str
    max_file_size_bytes: PositiveInt
    max_read_lines: PositiveInt
    max_read_chars: PositiveInt

    # -- /init -------------------------------------------------------------
    initializer_agent: str
    init_read_chars: PositiveInt
    init_max_steps: PositiveInt

    # -- MCP teardown ------------------------------------------------------
    teardown_budget_s: PositiveNumber

    # -- Shell / sandbox ---------------------------------------------------
    shell_binary: str
    allow_insecure_sandbox_fallback: StrictBool
    auto_approve_sandbox_fallback: StrictBool
    shell_approval_mode: ShellApprovalMode
    sandbox_docker_image: str
    sandbox_timeout_seconds: PositiveInt
    sandbox_memory_mb: PositiveInt
    sandbox_cpu_seconds: PositiveInt
    sandbox_max_pids: PositiveInt

    # -- Output redaction / tool isolation --------------------------------
    redact_tool_outputs: StrictBool
    tool_compute_timeout_s: PositiveInt
    network_allowed_commands: list[str]
    inert_commands: list[str]

    # -- Research grants and review ---------------------------------------
    max_granted_tool_calls: NonNegativeInt
    attended_approval_timeout_s: PositiveInt
    subagent_review: StrictBool
    max_review_findings: NonNegativeInt
    max_review_refinements: NonNegativeInt

    # -- Compaction --------------------------------------------------------
    compaction_trigger_fraction: Unit
    compaction_trigger_tokens: PositiveInt
    compaction_warning_margin_tokens: NonNegativeInt
    compaction_keep_recent_tokens: NonNegativeInt
    compaction_keep_recent_turns: NonNegativeInt
    pin_max_trigger_fraction: Unit
    compaction_strength: PositiveInt
    compaction_max_retries: NonNegativeInt
    compaction_target_ratios: dict[int, Unit]
    compaction_ratio_tolerance: NonNegativeNumber
    compaction_strategy: CompactionStrategy
    # The ELEMENT vocabulary, not just the scalar's. Typed `str`, a
    # `hybrid` added here passed `effective_compaction`'s membership test
    # and then `core/compaction.py` treated anything that is not exactly
    # `chain` as `rederive` -- silently, recording `rederive` in the
    # checkpoint. Non-empty because an empty roster makes every strategy
    # illegal, including the one this file ships.
    compaction_strategies: Annotated[tuple[CompactionStrategy, ...],
                                     Field(min_length=1)]
    max_injected_memories: NonNegativeInt

    # -- Catalog text ------------------------------------------------------
    max_catalog_text_chars: PositiveInt

    # -- Summaries, references, the checklist -----------------------------
    compactor_agent: str
    summary_target_chars: PositiveInt
    max_injected_refs: NonNegativeInt
    # Non-empty: this becomes the enum in todo_write's own schema.
    todo_statuses: Annotated[tuple[str, ...], Field(min_length=1)]
    max_todo_items: NonNegativeInt
    compaction_pipeline_backstop_tokens: NonNegativeInt

    # -- Context windows ---------------------------------------------------
    model_context_windows: dict[str, PositiveInt]
    default_context_window: PositiveInt

    # -- Tool permissions and approvals -----------------------------------
    tool_permissions: ToolPermissionsModel
    tool_approvals: ToolApprovalsModel

    @model_validator(mode="after")
    def _check_cross_field_defaults(self) -> HarnessConfig:
        """Fail fast on a harness default that can never work.

        EVERY CHECK HERE IS AN INVARIANT BETWEEN TWO KEYS OF THIS FILE. A
        single field's shape belongs in its annotation; what belongs here is
        the pairs -- a value that is individually well-typed and impossible
        beside its neighbour. Each one was verified to be ACCEPTED before
        this validator grew, and each failed somewhere later and further
        from the edit that caused it.

        The four that are about a key the CONSUMER SUBSCRIBES DIRECTLY are
        the sharpest, because the annotation could not express them: the
        type says `dict[str, Unit]` while `scholar.py` and
        `source_scoring.py` index particular keys out of it, so a table that
        merely has the right value type still raises `KeyError` in the
        middle of a research run, after the model calls are paid for.

        Runtime customisation is unaffected: `/effort` and
        `/compact --strength` go through `effort_for()` /
        `effective_compaction()`, not the schema, so per-model and
        per-invocation levels keep working.
        """
        if self.compaction_strength not in self.compaction_target_ratios:
            raise ValueError(
                f"compaction_strength {self.compaction_strength!r} must be "
                f"one of {sorted(self.compaction_target_ratios)} "
                f"(keys of compaction_target_ratios)")
        if (self.default_effort is not None
                and self.default_effort not in self.default_effort_levels):
            raise ValueError(
                f"default_effort {self.default_effort!r} must be null or one "
                f"of {self.default_effort_levels} "
                f"(members of default_effort_levels)")
        # Defence in depth rather than the only guard: `initialize()` calls
        # `effective_compaction()`, which already refuses this pair at
        # startup on the CLI and TUI paths. Checked here too because it is
        # the same shape as the strength pair above, the message names the
        # config.yaml key rather than the settings.json one, and a process
        # that never calls `initialize()` -- a library import, a bare test --
        # otherwise carries the incoherent pair to the first fold.
        if self.compaction_strategy not in self.compaction_strategies:
            raise ValueError(
                f"compaction_strategy {self.compaction_strategy!r} must be "
                f"one of {list(self.compaction_strategies)} "
                f"(members of compaction_strategies)")

        # -- keys the consumers index directly ----------------------------
        for key in ("unknown", "repository"):
            if key not in self.scholar_venue_credit:
                raise ValueError(
                    f"scholar_venue_credit must contain {key!r}: "
                    f"core/reasoning/scholar.py subscripts it for a work "
                    f"whose location type is unrecognised, and a missing "
                    f"entry is a KeyError mid research run")
        if "restricted_registry" not in self.domain_authority_classes:
            raise ValueError(
                "domain_authority_classes must contain 'restricted_registry': "
                "core/reasoning/source_scoring.py subscripts it for a "
                "second-level government or academic host")
        unknown_classes = sorted(
            {name for name in self.domain_authority_suffixes.values()
             if name not in self.domain_authority_classes})
        if unknown_classes:
            raise ValueError(
                f"domain_authority_suffixes maps hosts onto classes that "
                f"domain_authority_classes does not define: "
                f"{unknown_classes}. source_scoring.py looks the class name "
                f"up directly, so each is a KeyError on the first host that "
                f"matches that suffix")
        bands = {"default_similarity_calibration":
                 self.default_similarity_calibration}
        bands.update({f"similarity_calibration[{name!r}]": band
                      for name, band in self.similarity_calibration.items()})
        for where, band in bands.items():
            missing = [edge for edge in ("floor", "ceiling")
                       if edge not in band]
            if missing:
                raise ValueError(
                    f"{where} is missing {missing}: source_scoring.py reads "
                    f"both edges of every calibration band")
            if band["floor"] > band["ceiling"]:
                raise ValueError(
                    f"{where} has floor {band['floor']} above ceiling "
                    f"{band['ceiling']}, which no similarity can fall inside")
        return self


# ---------------------------------------------------------------------------
# ---- Environment overrides ------------------------------------------------
# ---------------------------------------------------------------------------
#
# `config.yaml` holds the static default; the variable wins when it is set.
# Applied BEFORE validation, once, inside `load()` -- never at call time, for
# the reason rule 2 in this module's docstring gives.
#
# A declared table rather than five hand-written expressions so the set is
# enumerable: `HARNESS_ENV_VARS` below is what documentation and any future
# config surface can list without going stale.

#: {UPPER_CASE config name: environment variable}. String-valued only; the
#: variable's value is used verbatim, with no parsing, exactly as
#: `os.environ.get(var, default)` did.
ENV_OVERRIDES = {
    "MODEL_NAME": "AGENT_MODEL",
    "DB_PATH": "APP_DB_PATH",
    "WORKSPACE_DIR": "AGENT_WORKSPACE",
    "SHELL_BINARY": "AGENT_SHELL",
    "SANDBOX_DOCKER_IMAGE": "AGENT_SANDBOX_IMAGE",
}

#: Every variable this module reads, including the two that only reach a
#: derived value. `AGENT_WORKSPACE` appears in both.
HARNESS_ENV_VARS = tuple(sorted(
    set(ENV_OVERRIDES.values()) | {"AGENT_OUTPUT_DIR", "AGENT_WORKSPACE"}))

#: The two values with NO `config.yaml` key, because neither is a static
#: default and inventing one would change behaviour. Named here so the
#: round-trip check in `tests/` can tell "absent on purpose" from "forgotten".
DERIVED_VALUES = ("OUTPUT_DIR", "WORKSPACE_DIR_EXPLICIT")

#: Keys that need a human at the keyboard. No UNATTENDED surface may write
#: them -- not a settings file, not an environment variable, not a tool
#: call -- and `/config` writes one only behind a confirmation naming what
#: the key permits (batch 84; `config_edit.AUTHORITY_EFFECT` is that
#: sentence, per key, and a test holds the two lists against each other).
#: Four are the frozen security posture
#: (`security/posture.Posture`); three are the Authority rosters that name a
#: provider the harness will call (E2, SQ7); two are the global tool floor.
#:
#: `ensemble_mode` is deliberately NOT here. It is a MODE, persistable in
#: `settings.json` today, and the worst it can do is spend more of a provider
#: the user already chose -- the line `core/config_loader.py` draws between
#: authority and cost.
HARNESS_AUTHORITY_KEYS = frozenset({
    "shell_approval_mode",
    "allow_insecure_sandbox_fallback",
    "auto_approve_sandbox_fallback",
    "redact_tool_outputs",
    "ensemble_models",
    "critic_model",
    "embedder_model",
    "tool_permissions",
    "tool_approvals",
})


# ---------------------------------------------------------------------------
# ---- Reading the file -----------------------------------------------------
# ---------------------------------------------------------------------------

def _reader() -> YAML:
    """The import-path reader.

    `typ="safe"` rather than `typ="rt"`: the round-trip parser exists to
    preserve comments across a WRITE, and nothing writes this file yet. Safe
    mode also refuses `!!python/...` tags outright, which matters for a file
    that decides the tool permissions. Both modes raise `DuplicateKeyError` on
    a repeated key, so a second `shell_approval_mode` is an error rather than
    a silent last-one-wins.

    A future config editor wants `YAML(typ="rt")` instead, on its own write
    path -- that is the reason `ruamel.yaml` is the dependency here rather
    than the `pyyaml` that `core/config_loader.py` uses for frontmatter.
    """
    return YAML(typ="safe")


def _describe(path: Optional[str]) -> str:
    """How to name the file being complained about.

    `config.yaml` for the live document, the caller's path for a candidate.
    Hardcoding `CONFIG_FILENAME` made every message about a candidate misname
    the file and, worse, offer the live document's remedy -- telling someone
    whose draft has a typo that "the package is incomplete" and to "restore
    the file from git".
    """
    if path is None or os.path.abspath(path) == CONFIG_PATH:
        return CONFIG_FILENAME
    return os.path.basename(path)


def read_document(path: Optional[str] = None) -> dict:
    """The raw mapping, parsed and nothing else. No validation, no env."""
    target = path or CONFIG_PATH
    name = _describe(path)
    try:
        with open(target, encoding="utf-8") as handle:
            data = _reader().load(handle)
    except FileNotFoundError:
        if path is None:
            raise ValueError(
                f"{CONFIG_FILENAME} is missing at {target}. It ships with the "
                f"harness beside config.py and holds every tunable value, so "
                f"nothing can start without it. If this is an installed copy, "
                f"the package is incomplete; if it is a checkout, restore the "
                f"file from git.") from None
        raise ValueError(f"{name} does not exist at {target}.") from None
    except UnicodeDecodeError as exc:
        # A ValueError but NOT an OSError, so the clause below never saw it
        # and the message named neither the file nor the remedy. The way to
        # arrive here on Windows is Notepad's "ANSI" or "UTF-16" save option.
        raise ValueError(
            f"{name} at {target} is not valid UTF-8 ({exc}). Save it as "
            f"UTF-8 without a byte-order mark; an editor offering \"ANSI\" "
            f"or \"UTF-16\" will produce this.") from None
    except (OSError, YAMLError) as exc:
        raise ValueError(f"{name} at {target} could not be "
                         f"read: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"{name} at {target} is not a mapping of settings "
            f"(parsed as {type(data).__name__}).")
    return data


def validate(data: dict, source: Optional[str] = None) -> HarnessConfig:
    """Validate a raw mapping, reporting every problem at once.

    Raises `ValueError` naming the file and each offending key. A missing key
    and an unknown key are both errors -- see rule 3 in the module docstring.
    """
    where = source or CONFIG_PATH
    name = _describe(source)
    try:
        return HarnessConfig(**data)
    except TypeError as exc:
        # `HarnessConfig(**data)` raises before pydantic is entered when a
        # key is not a string, so `except ValidationError` never saw it and
        # the one input class that most needs the file-and-key message got a
        # bare `TypeError: keywords must be strings`. The way to arrive here
        # is de-indenting a nested block: `1: 0.4` at column 0 under
        # `compaction_target_ratios` parses to a mapping with an int key,
        # which passes `read_document`'s mapping guard.
        bad = sorted(repr(key) for key in data if not isinstance(key, str))
        raise ValueError(
            f"{name} at {where} has {'keys' if len(bad) != 1 else 'a key'} "
            f"that {'are' if len(bad) != 1 else 'is'} not a setting name: "
            f"{', '.join(bad) or exc}. A nested block de-indented to column "
            f"0 does this -- check the indentation around them.") from None
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            key = ".".join(str(part) for part in err["loc"]) or "<root>"
            lines.append(f"  {key}: {err['msg']}")
        raise ValueError(
            f"{name} at {where} is not valid "
            f"({len(lines)} problem(s)):\n" + "\n".join(lines)) from None


def _overlay_env(data: dict) -> dict:
    """Fold the environment into the raw document, BEFORE validation.

    Before validation rather than after, and that ordering is the whole
    reason this is a separate function. An override applied afterwards would
    live only in the namespace `config.py` publishes, so
    `config_schema.current()` and `config.<NAME>` would disagree about any
    key that has a variable -- silently, and only for whoever adds the sixth
    override to a key `core/config_loader.py` happens to read. Folded in
    here there is ONE model, with the environment already in it, and an
    override goes through the same type check the file's value does.

    Returns a copy with the overrides folded in, leaving the input untouched,
    so a caller validating a candidate file does not have its document
    rewritten underneath it.
    """
    merged = dict(data)
    for name, variable in ENV_OVERRIDES.items():
        override = os.environ.get(variable)
        if override is not None:
            merged[name.lower()] = override
    return merged


def derived_values() -> dict:
    """The two `{UPPER_CASE_NAME: value}` entries with no `config.yaml` key.

    Separate from the model because neither is a static default: one is a
    composed path and the other is a question about the environment that YAML
    cannot ask. `DERIVED_VALUES` names them so a completeness check can tell
    "absent on purpose" from "forgotten".
    """
    return {
        # AGENT_OUTPUT_DIR, else `<AGENT_WORKSPACE or ".">/output`. ONE
        # expression with no branch, carried over verbatim, and note the
        # default is "." here where WORKSPACE_DIR's is "./workspace": an unset
        # variable still means `./output` rather than `./workspace/output`.
        # There is deliberately no `output_dir` key -- one would have to be
        # ignored whenever AGENT_WORKSPACE is named, which is the branch this
        # form exists to avoid.
        "OUTPUT_DIR": os.environ.get(
            "AGENT_OUTPUT_DIR",
            os.path.join(os.environ.get("AGENT_WORKSPACE", "."), "output")),
        # PRESENCE, never value. `main()` uses this to decide whether the
        # resolved workspace is also the PROJECT PATH, and the default
        # `./workspace` is a subdirectory of the launch directory -- so a
        # value test would move the project one level down for everyone who
        # set nothing.
        "WORKSPACE_DIR_EXPLICIT": "AGENT_WORKSPACE" in os.environ,
    }


def as_module_namespace(cfg: HarnessConfig) -> dict:
    """`{UPPER_CASE_NAME: value}` for `config.py` to publish as globals.

    Excludes the two permission tables, which `config.py` turns back into
    dataclasses, and includes the two derived values. The environment is
    already in `cfg` -- see `_overlay_env`.
    """
    values = {}
    for name in HarnessConfig.model_fields:
        if name in ("tool_permissions", "tool_approvals"):
            continue
        values[name.upper()] = getattr(cfg, name)
    values.update(derived_values())
    return values


def _make_dataclass(name: str, model: BaseModel) -> type:
    """Rebuild `ToolPermissions` / `ToolApprovals` as real dataclasses.

    A real dataclass rather than the pydantic model itself, because the whole
    codebase depends on the shape: `security/permissions.py` calls
    `config.ToolPermissions()` with no arguments, tests mutate an instance's
    fields and then swap the module attribute for a factory returning it, and
    `tests/test_docs_consistency.py` enumerates `vars(instance)`. A frozen
    pydantic model satisfies none of those.
    """
    fields = [(field, bool, dataclasses.field(default=getattr(model, field)))
              for field in type(model).model_fields]
    # The binding below (`ToolPermissions` / `ToolApprovals` globals plus the
    # idempotent factories) is what makes these PICKLABLE: `make_dataclass`
    # sets `__module__` from the calling frame, which is this module either
    # way, so pickle looks the name up here -- and used to find nothing,
    # because the class was only ever bound on `config`. No `module=`
    # argument: that kwarg needs Python 3.12+ and the floor is 3.11. The
    # original `@dataclass class ToolPermissions` in config.py pickled fine,
    # so this was a silent shape regression that nothing would notice until
    # one of these crossed a process boundary.
    return dataclasses.make_dataclass(name, fields)


_cached: Optional[HarnessConfig] = None
#: The rebuilt dataclasses, built once. Bound at module level so `pickle` can
#: resolve `config_schema.ToolPermissions` by name, and so the two factories
#: are idempotent -- a second call used to hand back a fresh class whose
#: instances compared unequal to the harness's own, because dataclass
#: `__eq__` requires `other.__class__ is self.__class__`.
ToolPermissions: Optional[type] = None
ToolApprovals: Optional[type] = None


def load(path: Optional[str] = None, force: bool = False) -> HarnessConfig:
    """Read, validate and cache `config.yaml`.

    Called by `config.py` at import, WITH `force=True` -- see the long note
    at that call. Importing `config` is what "bind the configuration now"
    means, so that module decides when the file is read; a cache that
    outlived a re-import of `config` would hand a fresh `config` the previous
    environment's values, which is a technique the suite actually uses.

    `core/config_loader.py` calls this with no arguments to read the shipped
    defaults it used to reach through `getattr(config, "<NAME>")`. The cache
    means that costs nothing and, more importantly, means the loader and the
    `config` module always see the same document.

    `path` JUDGES A CANDIDATE FILE ON ITS OWN, with no environment overlay
    and without touching the cache. That is the seam a restart-required
    config editor uses, and the overlay is left out for the reason the seam
    exists: the file is what the next launch will read, and the environment
    it will read it under is not knowable now. With the overlay in, a
    candidate missing `db_path` was ACCEPTED whenever `APP_DB_PATH` happened
    to be set in the editing shell, and then REFUSED at the startup it was
    supposed to be certifying. `force` applies to the live document only, so
    combining it with `path` asks for two different things at once and is
    refused rather than silently dropped.
    """
    global _cached
    if path is not None:
        if force:
            raise TypeError(
                "load(path=..., force=True) asks for two different things: "
                "`path` judges a candidate file and never touches the cache, "
                "`force` re-reads the live document into it. Pass one.")
        return validate(read_document(path), source=path)
    if _cached is None or force:
        _cached = validate(_overlay_env(read_document(None)),
                           source=CONFIG_PATH)
    return _cached


def current() -> HarnessConfig:
    """The validated document this process is running on."""
    return load()


def tool_permissions_dataclass() -> type:
    """The `ToolPermissions` class, built once and bound to this module.

    Idempotent, and that matters rather than being tidy: dataclass `__eq__`
    compares `other.__class__ is self.__class__`, so a second class built
    from the same values produced instances that compared UNEQUAL to the
    harness's own, and `isinstance` against the live class was False.
    """
    global ToolPermissions
    if ToolPermissions is None:
        ToolPermissions = _make_dataclass(
            "ToolPermissions", current().tool_permissions)
    return ToolPermissions


def tool_approvals_dataclass() -> type:
    """The `ToolApprovals` class, built once. See the sibling above."""
    global ToolApprovals
    if ToolApprovals is None:
        ToolApprovals = _make_dataclass(
            "ToolApprovals", current().tool_approvals)
    return ToolApprovals
