"""
project_init/config_files.py

ROADMAP_v2 §24 (I14-I17): WHAT `/init --config` writes into `.venastine/`,
and what it deliberately leaves out.

Data and pure rendering only -- no filesystem, no model call, no decision
about when to write. The same split as doc_sets.py and for the same
reason: the templates can be asserted against directly, and the one place
that decides whether anything is written stays generator.py.

THE FILE CANNOT EXPLAIN ITSELF, SO THIS ONE HAS TO. `_validate_settings`
raises on an unknown key (§14, amendment 1), so settings.json has no
comment syntax, no `$schema` line, and no way to carry a key that is
present but inert. Every line the scaffold writes is a live setting.

WHICH MAKES "THE DEFAULT" A THING SOME KEYS CANNOT SAY. A key read by
PRESENCE rather than by value is not restated by writing its default --
it is decided. Six were found that way, by writing each key alone into a
trusted project and diffing the observable state against a project with
no file at all:

    default_provider, default_model   the /model store's staleness key.
                                      `_startup_pair`: "edit either, ADD
                                      either, or remove either and the
                                      file re-asserts itself" -- so
                                      scaffolding them discards a
                                      remembered choice
    effort                            `_effort_named` flips False->True,
                                      and its own comment says a
                                      default-derived level must not fire
                                      the mount-time probe (#138)
    compaction.trigger_tokens         `_trigger_is_configured()` is a
                                      presence test, and presence makes
                                      the fixed number outrank the
                                      window-derived trigger -- invisibly,
                                      since the file only says 40000
    ensemble_mode                     `config.ENSEMBLE_MODE if x is None`
    research.subagent_review          `if persisted is not None` -- both
                                      of these freeze today's config.py
                                      value against a later edit of it

All six are in OMITTED. What survives is a scaffold that changes NOTHING
until a human edits it, which is the only claim "a template of defaults"
can honestly make, and `test_the_scaffolded_settings_file_changes_nothing`
asserts it over the whole observable set rather than key by key.

THE DEFAULTS ARE READ, NOT RETYPED (I15) -- `config_loader.shipped_defaults()`
and `config.py`'s own constants. A template that quotes a default is a
second copy of it, in the one file that has to be right the first time or
the harness will not start.

WHAT IT WRITES IS STILL NOT NEUTRAL BETWEEN TIERS. A project settings.json
beats the user's (D29) by PRESENCE and not by difference, so even this file
takes every key it names away from `~/.config/venastine/settings.json`.
`shadowed()` is how the command says so; generator.py renders it.
"""

from __future__ import annotations

import config
from core import config_loader

SETTINGS_FILENAME = "settings.json"
MCP_FILENAME = "mcp.json"

#: Tier directories the loader looks for under `.venastine/`, created
#: EMPTY. They carry nothing into the trust hash -- `content_files()`
#: walks files, and a directory name never enters the digest -- and git
#: does not track an empty directory, so a project that commits its
#: `.venastine/` does not ship them. They are a hint on the author's
#: machine about where a project-tier agent or skill goes, and nothing
#: more. Named here rather than in config_loader because the loader's
#: `_tier_dirs` derives them from the `kind` argument it is called with,
#: which is not a list anything can read.
TIER_DIRS = ("agents", "skills")

#: Settings the loader knows and this file does NOT write, each with the
#: reason. Dotted `section.key` for a nested one.
#:
#: A SILENT OMISSION IS INDISTINGUISHABLE FROM AN OVERSIGHT, which is why
#: this is data with prose in it rather than a set: the next person to
#: read the scaffold and find `effort` missing should find out why here,
#: and `test_the_template_omits_only_what_it_can_name` keeps the entries
#: honest by requiring every one of them to still exist in the loader's
#: vocabulary.
OMITTED = {
    "default_provider":
        "presence, not value, is the /model store's staleness key -- "
        "adding this to a settings.json discards a remembered choice, "
        "which _startup_pair's own docstring says in as many words",
    "default_model":
        "the other half of the same staleness key; the pair is compared "
        "and restored whole or not at all",
    "effort":
        "sets _effort_named, which exists to tell a level a HUMAN chose "
        "from one derived from a default. Writing it makes every launch "
        "probe the provider's effort table, and warn on the models that "
        "have none (#138's healthy-mount silence)",
    "ensemble_n":
        "vestigial -- the roster's length decides the count now, and the "
        "loader warns when this is set, so a template carrying it would "
        "ship that warning to everyone who ran the command",
    "ensemble_mode":
        "resolved as `config.ENSEMBLE_MODE if value is None`, so writing "
        "`false` is not a restatement of the default but a decision to "
        "outrank whatever config.py says later",
    "compaction.trigger_tokens":
        "_trigger_is_configured() is a PRESENCE test: the window-derived "
        "trigger only applies while no human has named a number. Writing "
        "the default pins compaction to it for every model, and the file "
        "gives no hint that it did -- on a large-context model that is a "
        "much earlier fold with nothing on screen to explain it",
    "research.subagent_review":
        "same `is not None` shape as ensemble_mode: an explicit false "
        "beats config.SUBAGENT_REVIEW rather than deferring to it",
    "tui.theme":
        "no shipped default to write -- absent, _startup_theme() resolves "
        "one -- and an invented value would outrank a theme the user "
        "picked with /theme, for every session in this project",
    "tui.effort":
        "no shipped default either, and it feeds the same _effort_named "
        "flag as the top-level key above",
    "source_scoring.similarity_floor":
        "defaults to None against a `float` schema, so writing the "
        "default IS a startup error. None here means the per-model table "
        "decides, which no JSON value can say",
    "source_scoring.similarity_ceiling":
        "the other half of the same per-model pair",
}


def _defaults() -> dict:
    """Every setting that has a shipped default, at that default.

    Before OMITTED is applied, so the removal below is visible as a
    removal. `tui.theme` and `tui.effort` are the two entries in OMITTED
    that cannot appear here at all -- there is no default to produce --
    and the test asserts on the rendered key set rather than on which
    mechanism excluded a key, so both routes are covered.

    The three `tui` values are the only ones typed out rather than read.
    config.py holds no TUI values on purpose (the note beside `animations`
    in tui/app.py), so their defaults are literals in `VenastineApp.__init__`
    and there is no constant to import without dragging textual into
    project_init. `test_the_scaffolded_settings_file_changes_nothing`
    drives the real app, so a drift here fails there rather than here.
    """
    from core.loop import DEFAULT_PROVIDER

    shipped = config_loader.shipped_defaults()
    confidence = dict(shipped["confidence"])
    # JSON HAS NO NULL KEY. `grounding_weights` carries one -- the weight
    # for a claim with no grounding status at all -- and it serialises as
    # the string "null", which `_apply_grounding_weights` then rejects
    # against GROUNDING_STATUSES and logs about on every launch. The three
    # named statuses ARE settable and the None one is not, by that
    # vocabulary's own definition, so the template emits exactly the keys
    # the loader will take. Read from the loader's re-export rather than
    # retyped, like everything else here.
    confidence["grounding_weights"] = {
        status: weight
        for status, weight in confidence["grounding_weights"].items()
        if status in config_loader.GROUNDING_STATUSES
    }
    return {
        "default_provider": DEFAULT_PROVIDER,
        "default_model": config.MODEL_NAME,
        "effort": config.DEFAULT_EFFORT,
        "max_token_budget": None,
        "ensemble_mode": config.ENSEMBLE_MODE,
        "compaction": shipped["compaction"],
        "tui": {
            "animations": True,
            "show_thinking": True,
            "todo_position": "side",
        },
        "research": {
            # The vocabulary's first entry is the default, and
            # _KNOWN_RESEARCH says so beside it. Read rather than typed
            # for the same reason as everything else here.
            "approval_mode": config_loader.RESEARCH_APPROVAL_MODES[0],
            "subagent_review": config.SUBAGENT_REVIEW,
        },
        "confidence": confidence,
        "source_scoring": shipped["source_scoring"],
    }


def _coerce(section: str, key: str, value):
    """A value declared `float` is emitted as one.

    Every confidence knob is a genuine float today, so this changes no
    byte of the current template. It is here because the failure it
    prevents is silent at the wrong end: a future default landing as an
    int (0.0 written as 0, say) makes `_type_ok(0, float)` False, and the
    scaffold this file exists to produce becomes a file that stops the
    harness from starting. Cheaper to make impossible than to catch.
    """
    known = config_loader._NESTED_SETTINGS.get(section)
    expected = (known or config_loader._KNOWN_SETTINGS).get(key)
    if expected is float and isinstance(value, (int, float)):
        return float(value)
    return value


def render_settings() -> dict:
    """The scaffolded `.venastine/settings.json`, as a payload."""
    settings = _defaults()
    for path in OMITTED:
        section, dot, key = path.partition(".")
        if dot:
            settings.get(section, {}).pop(key, None)
        else:
            settings.pop(section, None)
    return {
        key: ({sub: _coerce(key, sub, value) for sub, value in block.items()}
              if isinstance(block, dict) and key in config_loader._NESTED_SETTINGS
              else _coerce("", key, block))
        for key, block in settings.items()
    }


def render_mcp() -> dict:
    """The scaffolded `.venastine/mcp.json`.

    The skeleton and nothing else. mcp.json's validation is by CONNECTION
    rather than by schema (§17 decision G), so there is no equivalent
    "every key at its default" to write -- and a server entry is the one
    piece of project configuration that names a local command to execute,
    which is not a thing to pre-fill with an example somebody might leave
    in place.
    """
    return {"mcpServers": {}}


def leaf_paths(settings: dict) -> set:
    """Dotted paths of the LEAF settings in a payload, `section.key` for
    a nested one. The shape both the omission list and the vocabulary
    check are written in."""
    out = set()
    for key, value in settings.items():
        if key in config_loader._NESTED_SETTINGS and isinstance(value, dict):
            out |= {"%s.%s" % (key, sub) for sub in value}
        else:
            out.add(key)
    return out


def shadowed(user: dict) -> list:
    """(path, theirs, ours) for every key the scaffold would start
    deciding instead of the user's own settings.json.

    DIFFERENCES ONLY. A key set to the same value on both sides changes
    nothing anyone can observe, and a warning that lists it teaches the
    reader to skim the ones that matter.

    Nested sections compare per key, because that is how the merge treats
    them (`_load_merged_settings` deepens exactly one level); a whole-
    section comparison would report a `compaction` block as shadowed for
    one differing knob inside it.
    """
    ours = render_settings()
    out = []
    for path in sorted(leaf_paths(ours)):
        section, dot, key = path.partition(".")
        if dot:
            block = user.get(section)
            if not isinstance(block, dict) or key not in block:
                continue
            theirs, mine = block[key], ours[section][key]
        else:
            if path not in user:
                continue
            theirs, mine = user[path], ours[path]
        if theirs != mine:
            out.append((path, theirs, mine))
    return out
