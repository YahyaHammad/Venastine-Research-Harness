"""
core/config_loader.py

ROADMAP_v2 §14. Discovers and parses agent/skill `.md` files across the
three tiers (D8) -- harness (`agents/builtin/`, `skills/builtin/` shipped
with the repo), project (`.venastine/`, only if trusted per D17), user
(`~/.config/venastine/`) -- merges `settings.json` across tiers, and
exposes the skill catalog (frontmatter only, never bodies) for system
prompt injection.

Progressive disclosure: the model sees name+description of every
discovered skill in its system prompt and must call the `load_skill`
tool to view a body. Bodies never enter context unrequested.

Tier precedence: a harness-tier name can never be shadowed -- a
project/user file colliding with a builtin is rejected with a warning
(D18). Project vs user collisions resolve USER-wins (D29): the user tier is
what you authored; the project tier is what arrived with a directory
you cloned.

`initialize(project_path)` is called once at startup (main.py) and caches
everything; getters return the cached state. Tests call initialize() with
their own tmp dirs and reset() afterwards.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import yaml

import config
from core import workspace_trust

logger = logging.getLogger(__name__)

# The repo root (this file lives in <root>/core/). The harness tier is
# shipped with the harness itself, so it resolves from here, not from cwd.
HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FRONTMATTER_DELIM = re.compile(r"^---\s*$", re.MULTILINE)

_KNOWN_SETTINGS = {
    "default_provider": str,
    "default_model": str,
    # Batch 25 (#139). The effort level every path runs at unless something
    # more specific speaks: CLI flag > this key > config.DEFAULT_EFFORT,
    # with tui.effort still winning INSIDE the TUI (it predates this key
    # and changing its meaning would surprise existing configs).
    #
    # Deliberately NOT an R12 by-name rejection, and the reason is worth
    # stating so nobody "completes" the pattern: R12 rejects keys that are
    # AUTHORITY. This one is cost -- the worst a cloned repo's settings.json
    # can do here is make runs think harder and spend more tokens, which is
    # compaction-settings territory, not granted-tools territory.
    "effort": str,
    # Batch 27 (#4). The optional per-run SPEND ceiling, in billed
    # input+output tokens. None/absent -- the default -- means UNCAPPED:
    # hard spend limits are the provider's dashboard's job, and a harness
    # cap that misreads as a context limit is the exact defect #4 records.
    # Cost knob, not authority, so effort's merge rule applies (normal
    # tiers; deliberately NOT an R12 by-name rejection).
    "max_token_budget": (int, type(None)),
    "ensemble_mode": bool,
    "ensemble_n": int,
    "compaction": dict,
    "tui": dict,
    "research": dict,
}
# ROADMAP_v2 §25 (R12). The authorization MODE is persistable and the
# grant LIST deliberately is not.
#
# The asymmetry is the design. A persisted mode can only ever ADD prompts,
# so the worst a hostile settings.json can do is make your runs more
# annoying. A persisted grant list could only ever REMOVE them -- standing
# authorization for named tools, carried by any repo you clone, and
# settings.json is the one config file where project tier beats user tier.
#
# `granted_tools` is therefore rejected BY NAME rather than left to the
# unknown-key branch, because "unknown key" reads as an oversight to be
# fixed by adding support for it. See _validate_settings.
_KNOWN_RESEARCH = {
    "approval_mode": str,   # "none" (default) or "attended"
    # §20/D9. A MODE, so R12's rule permits persisting it: the worst a
    # hostile settings.json can do here is make a run stop and ask you
    # about corrections it wants to make, which is more friction and never
    # more authority. --no-review escapes it for one run.
    "subagent_review": bool,
}
RESEARCH_APPROVAL_MODES = ("none", "attended")
_KNOWN_COMPACTION = {
    "strength": int,
    "keep_recent_tokens": int,
    # ROADMAP_v2 §21 (M1). This was `buffer_tokens` while §21 was specified
    # as "headroom before the model's context window". It is not headroom:
    # the trigger is a WORKING-SET TARGET -- a window-derived threshold
    # sits at a size a working thread never reaches, while ~40k keeps turns
    # cheap and their tool steps intact. Renamed rather than redefined -- a
    # key that keeps its name and changes its meaning is worse than one
    # that moves, and these keys have been inert since §14 so nothing
    # depends on the old spelling.
    "trigger_tokens": int,
    "warning_margin_tokens": int,
    # §21 M5 / M2 / retry bound.
    "keep_recent_turns": int,
    "strategy": str,
    "max_retries": int,
}
# ROADMAP_v2 §16. TUI preferences live here rather than in a dotfile of
# their own because unknown settings keys RAISE (§14 amendment 1) -- a
# preference the loader doesn't know about is a startup error, not a
# silently ignored line. That is the tradeoff the loud-rejection rule
# buys: adding a preference is a schema change, on purpose.
_KNOWN_TUI = {
    "theme": str,        # one of tui/themes.py's registered names
    "animations": bool,  # master switch for the raven + transitions
    "effort": str,       # persisted reasoning-effort level (§16)
    "todo_position": str,  # §23 slice 2: one of TODO_POSITIONS below
    # §38 (O6): render a turn's reasoning inline, or collapse it to an
    # animated one-line indicator. Defaults to True in tui/app.py rather
    # than to a config.py constant, following `animations` above -- config.py
    # holds no TUI values and is plain-values-only.
    "show_thinking": bool,
}

# §23 slice 2. Validated AT LOAD and raising, following
# research.approval_mode rather than tui.theme.
#
# tui.theme validates at use and falls back, for a stated reason: the loader
# cannot know the valid names without importing tui/themes.py, and a stale
# theme name should not stop the app from starting. Neither applies here --
# the vocabulary is three words that live in this file, and a position the
# renderer does not understand would silently put the panel somewhere the
# user did not ask for. tui.effort had neither check and that was a shipped
# bug (§16), which is the third precedent and the one that decided it.
TODO_POSITIONS = ("top", "bottom", "side")

# Settings whose value is a nested object. Both the validator and the
# cross-tier merge below iterate this rather than naming keys twice --
# `compaction` was the only member until §16 added `tui`, and hardcoding
# it in two places is what made the shallow-merge defect
# (review finding F2) possible in the first place.
_NESTED_SETTINGS = {
    "compaction": _KNOWN_COMPACTION,
    "tui": _KNOWN_TUI,
    "research": _KNOWN_RESEARCH,
}


@dataclass
class SkillDef:
    name: str
    description: str
    # ROADMAP_v2 §19 (K2): the tools this skill's methodology DEPENDS on,
    # not tools it grants. It cannot grant any -- ToolContext.allowed_tools
    # is a whitelist that narrows, and D14 forbids widening -- so this is
    # checked at activation and reported, never applied. See SkillManager.
    additional_tools: list
    body: str
    tier: str
    path: str
    # §19 (K4): folder path from the tier root, posix separators, "" for a
    # skill sitting directly in skills/. Metadata, not identity -- the name
    # stays global, so load_skill, /skill and D18's collision rule are
    # untouched by where a file lives. Used to GROUP the catalog, which is
    # the point: progressive disclosure puts every skill's name and
    # description in every system prompt, and a flat list of forty is
    # exactly what that design exists to avoid.
    category: str = ""


@dataclass
class AgentDef:
    name: str
    description: str
    model: Optional[str]
    provider: Optional[str]
    allowed_tools: Optional[list]
    approval_overrides: dict
    use_project_context: bool
    use_memory: bool
    max_steps: Optional[int]
    body: str
    tier: str
    path: str
    # ROADMAP_v2 §32 (A3), #69. Whether spawn_subagent can actually
    # FEED this agent -- i.e. whether a task string, as the first
    # message of a fresh thread, is the input its body expects.
    #
    # None means UNDECLARED, and what that means depends on who wrote
    # the file. For the harness tier it is a build error, caught by
    # assert_spawnable_declared below: our own omission is a bug, and
    # R13's rule applies -- omission has to be a detectable mistake
    # rather than an inherited answer. For the user and project tiers
    # it reads as False, because a third party's silence must get the
    # SAFE answer: an agent written for /agent and advertised as
    # spawnable does not fail, it under-performs, and the parent has
    # no way to tell (which is #69's actual complaint).
    #
    # Consulted by prompts.system_prompts.agent_catalog_text only.
    # It is NOT a permission: C6 still caps a child's tools, and the
    # depth limit still applies. This decides what the model is TOLD
    # exists, which is D24's rule ("advertising an uncallable tool is
    # not harmless: the model keeps choosing it") applied to the
    # catalog's entries rather than to the tool itself.
    spawnable: Optional[bool] = None


def _user_config_dir() -> str:
    # Call-time resolution so tests can redirect it without touching the
    # real user config dir.
    return os.path.expanduser("~/.config/venastine")


def _catalog_text(value) -> str:
    """One line, bounded, for a string that enters every system prompt.

    Applied to a skill's and an agent's `name` and `description` at
    PARSE TIME, for every tier -- ROADMAP_v2 §32 A5, #131.

    TWO RULES, ONE FUNCTION, because they are one rule about one kind
    of string:

      COLLAPSE WHITESPACE, and this is the security half. The catalogs
        render `- {name}: {description}`, so a value containing a
        newline does not merely look untidy -- it LEAVES ITS BULLET.
        A project skill described with a YAML block scalar holding
        `## Available tools` renders as a top-level section of the
        system prompt, indistinguishable from one this harness wrote.
        Driven, and it worked.

      CAP THE LENGTH, and this is the budget half. Progressive
        disclosure puts every name and description in every prompt of
        every run; without a bound, one project file sets that cost.

    TRUNCATE RATHER THAN REFUSE THE FILE. `_parse_md_file` skips a
    malformed definition, which is right for `additional_tools` --
    a non-string element crashes its first consumer. A description is
    advisory prose read by a model, so it degrades gracefully, and
    losing a whole working skill over the length of its summary would
    be the worse trade. The ellipsis is visible on purpose: a model
    reading a cut-off summary should be able to tell.

    EVERY TIER, not just project. A rule that applies only to
    untrusted input is a second source of truth about what a catalog
    entry is, and the harness tier is where a regression would be
    least visible -- so the shipped files are held to it too, and
    tests/test_catalog_text.py asserts they already comply.
    """
    text = " ".join(str(value).split())
    if len(text) <= config.MAX_CATALOG_TEXT_CHARS:
        return text
    return text[:config.MAX_CATALOG_TEXT_CHARS - 1].rstrip() + "\u2026"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Returns (frontmatter_dict, body). Uses a delimiter that must occupy
    its own line -- NOT text.find("---"), which matches the first bare
    '---' anywhere in the file, including a markdown horizontal rule
    inside the body itself (a very common convention in longer
    instructional content, which is exactly what agent/skill bodies are).
    """
    matches = list(_FRONTMATTER_DELIM.finditer(text))
    if len(matches) < 2:
        raise ValueError("No valid YAML frontmatter block found (need two '---' lines)")
    # The opening delimiter must be the FIRST thing in the file. Without
    # this, a file with no frontmatter at all but two horizontal rules in
    # its body has the prose between them handed to yaml.safe_load --
    # which usually fails the "is it a mapping / does it have a name"
    # checks downstream, but reports the wrong reason when it does.
    if matches[0].start() != 0:
        raise ValueError(
            "File does not begin with a YAML frontmatter block "
            "(the opening '---' must be the first line)")
    frontmatter_text = text[matches[0].end():matches[1].start()]
    body = text[matches[1].end():].strip()
    return yaml.safe_load(frontmatter_text), body


def _parse_md_file(path: str, kind: str, tier: str, category: str = ""):
    """Parse one agent/skill .md file. Returns a SkillDef/AgentDef, or
    None (with a warning) for malformed files -- a broken definition
    warns and skips rather than taking down startup."""
    try:
        # utf-8-sig, not utf-8: a BOM (Notepad's default on this project's
        # platform) is decoded as a leading ﻿, so the frontmatter's
        # ^--- no longer sits at position 0 and the file is skipped with
        # the false diagnosis "does not begin with a YAML frontmatter
        # block". utf-8-sig strips a BOM and is a no-op without one.
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
        fm, body = _parse_frontmatter(text)
    except (OSError, ValueError, yaml.YAMLError) as e:
        logger.warning("Skipping %s file %s: %s", kind, path, e)
        return None
    if not isinstance(fm, dict):
        logger.warning("Skipping %s file %s: frontmatter is not a YAML mapping", kind, path)
        return None
    name = fm.get("name")
    if not isinstance(name, str) or not name:
        logger.warning("Skipping %s file %s: missing 'name' in frontmatter", kind, path)
        return None
    # #131: the NAME is interpolated into the same catalog line as the
    # description (`- {name}: {description}`), so it is the same
    # injection surface and gets the same treatment. Bounded AFTER the
    # emptiness check, because an all-whitespace name collapses to ""
    # and must be reported as missing rather than registered as a
    # nameless definition that D18's collision rule cannot reason about.
    name = _catalog_text(name)
    if not name:
        logger.warning("Skipping %s file %s: 'name' is only whitespace",
                       kind, path)
        return None
    if kind == "skills":
        tools = fm.get("additional_tools") or []
        if not isinstance(tools, list):
            logger.warning("Skipping skill file %s: additional_tools is not a list", path)
            return None
        # Element types, not just the container (review §19-20 f6): the
        # first consumer feeds each element to a startswith() check, so a
        # non-string element parsed cleanly here and crashed /skill at
        # first consumption -- after "Activated" was already shown.
        if not all(isinstance(t, str) for t in tools):
            logger.warning("Skipping skill file %s: additional_tools is not a list of strings", path)
            return None
        return SkillDef(
            name=name,
            description=_catalog_text(fm.get("description", "")),
            additional_tools=tools,
            body=body,
            tier=tier,
            path=path,
            category=category,
        )
    allowed = fm.get("allowed_tools")
    if allowed is not None and not isinstance(allowed, list):
        logger.warning("Skipping agent file %s: allowed_tools is not a list", path)
        return None
    overrides = fm.get("approval_overrides") or {}
    if not isinstance(overrides, dict):
        logger.warning("Skipping agent file %s: approval_overrides is not a mapping", path)
        return None
    max_steps = fm.get("max_steps")
    # REPAIR, NOT REJECT (#45, batch 16 -- explicit owner decision). This
    # guard used to SKIP the whole file for a bad max_steps, which made it
    # the one frontmatter field whose typo cost the agent entirely while
    # every sibling produced a warning-and-carry-on. Worse, a negative int
    # passed the type check and crashed `_run` with an AttributeError on
    # `response.stop_reason` three layers away from the line that caused it.
    #
    # Now: any unusable value -- non-int, YAML's bool-reading of yes/on,
    # or < 1 (`isinstance(True, int)` is True in Python) -- is repaired to
    # None with a warning NAMING the original. None is what "not declared"
    # means downstream, so every call site's existing
    # `max_steps or config.MAX_ITERATIONS` fallback applies unchanged, and
    # the warning's default figure and the one that takes effect are the
    # same number by construction rather than by two copies staying in
    # step. The belt for values that bypass this loader lives in
    # core/loop._run, which raises instead of repairing: a programmatic
    # caller passing a bad value directly has no file to lose, only a bug
    # worth naming.
    if max_steps is not None:
        valid = (isinstance(max_steps, int)
                 and not isinstance(max_steps, bool)
                 and max_steps >= 1)
        if not valid:
            logger.warning(
                "Agent file %s: max_steps must be a positive integer, got "
                "%r; defaulting to %s (config.MAX_ITERATIONS).",
                path, max_steps, config.MAX_ITERATIONS)
            max_steps = None
    # Booleans are VALIDATED, not coerced. bool("false") is True, so
    # `use_memory: "false"` would invert a deliberate opt-out with no
    # warning -- and these two fields are exactly the ones a restrictive
    # agent definition uses to opt OUT. Same warn-and-skip contract every
    # other typed field in this function has.
    flags = {}
    for key, default in (("use_project_context", False), ("use_memory", True)):
        value = fm.get(key, default)
        if not isinstance(value, bool):
            logger.warning(
                "Skipping agent file %s: %s is not a boolean", path, key)
            return None
        flags[key] = value
    spawnable = fm.get("spawnable")
    if spawnable is not None and not isinstance(spawnable, bool):
        logger.warning(
            "Skipping agent file %s: spawnable is not a boolean", path)
        return None
    return AgentDef(
        name=name,
        description=_catalog_text(fm.get("description", "")),
        model=fm.get("model"),
        provider=fm.get("provider"),
        allowed_tools=allowed,
        approval_overrides=overrides,
        use_project_context=flags["use_project_context"],
        use_memory=flags["use_memory"],
        spawnable=spawnable,
        max_steps=max_steps,
        body=body,
        tier=tier,
        path=path,
    )


def _tier_dirs(kind: str, project_path: str, trusted: bool) -> list:
    """Search order, and _discover() is first-wins, so this list IS the
    precedence rule: harness > user > project.

    ORDER CHANGED IN §17 (D29). It was harness > project > user, i.e. the
    conventional "more specific config wins". That is the wrong default
    here, because "project" does not mean "more specific" -- it means "it
    arrived with a directory you cloned", which is the entire premise of
    D17. The user tier is the one you actually authored, so it should
    never be silently displaced by the tier you merely trusted once.

    Harness stays first and un-overridable (D18, unchanged): a project can
    still never impersonate a builtin.

    Note this only ever breaks SAME-NAME ties. Non-colliding definitions
    from every tier all load -- _discover() accumulates a union and
    consults precedence only when a name repeats.
    """
    dirs = [("harness", os.path.join(HARNESS_ROOT, kind, "builtin"))]
    dirs.append(("user", os.path.join(_user_config_dir(), kind)))
    if trusted:
        project_dir = os.path.join(
            workspace_trust.venastine_dir(project_path), kind)
        # #18. A SYMLINK at the project tier root escapes the D17 hash.
        # `os.walk` follows the path handed to it as `top` but not
        # symlinked subdirectories, and the two sides of the trust
        # boundary start from different places: workspace_trust walks from
        # `.venastine`, so a symlinked `skills/` is a subdirectory it will
        # not descend, while _md_files walks from `.venastine/skills`, so
        # it IS the top and gets followed. Directory names never enter the
        # hash either, so the link contributes nothing -- not even its own
        # name. The pointed-to definitions therefore load as project-tier
        # agents/skills while being absent from the trust prompt's listing
        # AND from the hash, so their bodies can be rewritten freely after
        # one grant and `is_trusted()` keeps returning True.
        #
        # Treated as ABSENT rather than loaded-and-warned, matching the
        # module's posture for untrusted content generally. The payload
        # here is not data but instructions: a project-tier body becomes
        # system-prompt content for the user's sessions.
        #
        # Scoped to the PROJECT tier deliberately. Harness and user
        # directories are not hashed by anything, so a symlink in them is
        # not a trust question -- and symlinking `~/.config/venastine/`
        # into a dotfiles repo is a legitimate thing an operator does to
        # their own config.
        if os.path.islink(project_dir):
            logger.warning(
                "Ignoring project %s directory %s: it is a symlink, and "
                "symlinked tier roots are invisible to the workspace-trust "
                "content hash (see issue #18). Move the definitions inside "
                ".venastine/ to load them.", kind, project_dir)
        else:
            dirs.append(("project", project_dir))
    return dirs


def _md_files(directory: str, recursive: bool):
    """(absolute path, category) for every .md file in a tier directory.

    ROADMAP_v2 §19 (K4): SKILLS nest under category folders --
    `skills/builtin/<category>/<skill>.md` -- and agents stay flat, since
    `agents/builtin/` holds one file and speculative structure for it
    would be structure nobody asked for. One branch here rather than a
    second discovery function, so tier handling and collision handling
    cannot diverge between the two kinds.

    The category is DERIVED from the folder, never authored: a `category:`
    frontmatter key would immediately be able to disagree with where the
    file actually sits, and then two places would claim to know.

    Walk order matches workspace_trust.content_files() -- dirs.sort() plus
    sorted(files) -- because same-name collisions resolve first-wins, so a
    nondeterministic order would make WHICH definition loads depend on the
    filesystem.
    """
    if not recursive:
        return [
            (os.path.join(directory, f), "")
            for f in sorted(os.listdir(directory)) if f.endswith(".md")
        ]
    out = []
    for dirpath, dirs, files in os.walk(directory):
        dirs.sort()
        category = os.path.relpath(dirpath, directory).replace(os.sep, "/")
        if category == ".":
            category = ""
        for fname in sorted(files):
            if fname.endswith(".md"):
                out.append((os.path.join(dirpath, fname), category))
    return out


def _discover(kind: str, project_path: str, trusted: bool) -> dict:
    found = {}
    for tier, directory in _tier_dirs(kind, project_path, trusted):
        if not os.path.isdir(directory):
            continue
        for path, category in _md_files(directory, recursive=(kind == "skills")):
            defn = _parse_md_file(path, kind, tier, category)
            if defn is None:
                continue
            if defn.name in found:
                winner = found[defn.name]
                if winner.tier == "harness":
                    logger.warning(
                        "%s-tier %s %r collides with a harness builtin; "
                        "the builtin wins and the %s file is ignored (D18).",
                        tier, kind, defn.name, tier,
                    )
                else:
                    # Cross-tier, USER beats project for agents and skills:
                    # _tier_dirs returns [harness, user, project] and this
                    # loop is first-wins. That is D29, not D8 -- D8 is where
                    # definitions are discovered, D29 is the §17 inversion
                    # that put user ahead of project, because "project" does
                    # not mean "more specific", it means "it arrived with a
                    # directory you cloned". (This comment said the opposite
                    # and cited D8; it was pre-D29 text that survived the
                    # change. See audit #20 -- the warning below was always
                    # right, because it names the winner dynamically.)
                    #
                    # settings.json deliberately goes the OTHER way,
                    # project-over-user, at _load_merged_settings -- because
                    # the trust prompt shows its values verbatim. Two
                    # opposite rules in one module is exactly what a comment
                    # here should disambiguate.
                    #
                    # Same-tier is alphabetical-first-wins. None of this is
                    # guessable from the outside, so say so:
                    # Silently shadowing a definition is how someone spends
                    # an afternoon editing a file that is never loaded.
                    logger.warning(
                        "%s-tier %s %r at %s is shadowed by the %s-tier "
                        "definition at %s, which wins.",
                        tier, kind, defn.name, defn.path, winner.tier,
                        winner.path,
                    )
                continue
            found[defn.name] = defn
    return found


def _type_ok(value, expected) -> bool:
    # bool is a subclass of int in Python, so int-typed keys must reject
    # booleans explicitly, and bool-typed keys must reject ints -- including
    # through a tuple expected-type: isinstance(True, (int, type(None))) is
    # True, and max_token_budget=True would otherwise sail through as 1.
    expecteds = expected if isinstance(expected, tuple) else (expected,)
    if isinstance(value, bool):
        return bool in expecteds
    ok = False
    for e in expecteds:
        if e is bool:
            continue          # a non-bool value can never match bool
        if e is int:
            ok = ok or isinstance(value, int)
        else:
            ok = ok or isinstance(value, e)
    return ok


def _validate_settings(data, source: str) -> None:
    """Loud rejection of unknown keys (a typo must not masquerade as a
    setting) and type checks for known ones. Compaction keys are known
    but not consumed until §21 -- they validate here and warn at init."""
    if not isinstance(data, dict):
        raise ValueError(f"settings.json at {source} is not a JSON object")
    for key, value in data.items():
        if key == "ensemble_models":
            # ROADMAP §10 revisit (E2), and R12's rule applied to a second
            # kind of authority. Rejected BY NAME rather than as an unknown
            # key, because the generic message reads as an oversight to be
            # fixed by adding support for it.
            #
            # Turning ensemble mode on can only spend more of the provider
            # the user already chose. A ROSTER chooses providers -- so a
            # project's settings.json, which beats the user's, could point N
            # research passes at endpoints the user never configured for this
            # work and multiply the run's cost by the length of a list it
            # supplied. §14 already flagged project-tier provider selection as
            # a distinct grant; this is that grant times N.
            raise ValueError(
                f"settings.json at {source}: ensemble_models is deliberately "
                f"not supported -- a roster chooses which providers N research "
                f"passes call, and a project's settings.json beats the "
                f"user's. Set config.ENSEMBLE_MODELS in config.py instead "
                f"(same posture as CRITIC_MODEL).")
        if key == "shell_approval_mode":
            # ROADMAP_v2 §28 (G7), and the third application of R12's rule.
            # Rejected BY NAME for the reason the other two are: the
            # generic unknown-key message reads as an oversight someone
            # should fix by adding support, and this omission IS the
            # design.
            #
            # A project's settings.json beats the user's (D29), and it
            # arrives with a directory you cloned. This key decides
            # whether shell commands are asked about at all -- so
            # supporting it would let a cloned repo set "never" and turn
            # `cat ~/.aws/credentials` into an unprompted host read. D17's
            # trust gate is not a substitute: the same prompt covers a
            # README-shaped CONTEXT.md, and nobody reading it is deciding
            # about their shell gate.
            raise ValueError(
                f"settings.json at {source}: shell_approval_mode is "
                f"deliberately not supported -- it decides whether shell "
                f"commands are approved at all, and a project's "
                f"settings.json beats the user's. Set "
                f"config.SHELL_APPROVAL_MODE in config.py instead (same "
                f"posture as ensemble_models and research.granted_tools).")
        if key not in _KNOWN_SETTINGS:
            raise ValueError(f"settings.json at {source}: unknown key {key!r}")
        expected = _KNOWN_SETTINGS[key]
        if not _type_ok(value, expected):
            expected_name = getattr(expected, "__name__", None) \
                or "an int or null"
            raise ValueError(
                f"settings.json at {source}: key {key!r} must be {expected_name}, "
                f"got {type(value).__name__}")
    for section, known_keys in _NESTED_SETTINGS.items():
        block = data.get(section)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ValueError(
                f"settings.json at {source}: {section!r} must be an object")
        for key, value in block.items():
            if section == "research" and key == "granted_tools":
                # §25 R12: rejected BY NAME, not as an unknown key. The
                # generic message reads as an oversight someone should fix
                # by adding support; this one says the omission is the
                # design, so nobody "fixes" it into standing authorization
                # that a cloned repo would carry.
                raise ValueError(
                    f"settings.json at {source}: research.granted_tools is "
                    f"deliberately not supported -- a persisted grant list "
                    f"would authorise named tools for every future run, and "
                    f"a project's settings.json beats the user's. Grant per "
                    f"run with --grant / --grant-tools instead.")
            if key not in known_keys:
                raise ValueError(
                    f"settings.json at {source}: unknown {section} key {key!r}")
            expected = known_keys[key]
            if not _type_ok(value, expected):
                raise ValueError(
                    f"settings.json at {source}: {section} key {key!r} must be "
                    f"{expected.__name__}, got {type(value).__name__}")
            if section == "research" and key == "approval_mode" and \
                    value not in RESEARCH_APPROVAL_MODES:
                raise ValueError(
                    f"settings.json at {source}: research.approval_mode must "
                    f"be one of {', '.join(RESEARCH_APPROVAL_MODES)}, "
                    f"got {value!r}")
            if section == "tui" and key == "todo_position" and \
                    value not in TODO_POSITIONS:
                raise ValueError(
                    f"settings.json at {source}: tui.todo_position must be "
                    f"one of {', '.join(TODO_POSITIONS)}, got {value!r}")


# ---------------------------------------------------------------------------
# ---- Compaction settings (ROADMAP_v2 §21, D27) ----------------------------
# ---------------------------------------------------------------------------
#
# Resolution lives HERE rather than in core/compaction.py because
# config_loader already owns the settings merge, and core/compaction.py
# reaches agents.manager, which imports this module -- resolving there and
# validating here would be a cycle. One function, called at startup so a
# bad value is a startup error, and called again per compaction so a
# per-invocation `/compact --strength 4` composes with the same rules.

_COMPACTION_DEFAULTS = {
    "trigger_tokens": "COMPACTION_TRIGGER_TOKENS",
    "warning_margin_tokens": "COMPACTION_WARNING_MARGIN_TOKENS",
    "keep_recent_tokens": "COMPACTION_KEEP_RECENT_TOKENS",
    "keep_recent_turns": "COMPACTION_KEEP_RECENT_TURNS",
    "strength": "COMPACTION_STRENGTH",
    "max_retries": "COMPACTION_MAX_RETRIES",
    "strategy": "COMPACTION_STRATEGY",
}


def effective_compaction(overrides: Optional[dict] = None,
                         warn: bool = False) -> dict:
    """The compaction values actually in force.

    A flat {key: value}. NOT where each came from -- see
    TECHNICAL_DEBT.md item 12; the merge below overwrites without
    recording which tier won, and D27's third implementation note asked
    for provenance AND somewhere to show it. This sentence used to
    promise both (audit #91).

    config.py default -> user settings.json -> trusted project
    settings.json -> `overrides` (a per-invocation `/compact --strength 4`,
    which applies to that one run and persists nothing). Nearest wins,
    which is the precedence the rest of the config system already uses.

    RELATIONSHIPS ARE VALIDATED, not just documented -- §21's own
    instruction about warning_margin, which is the one value whose wrong
    setting is not self-correcting: a margin at or above the trigger fires
    the warning after the thing it warns about, which is no warning at all.

    Raises ValueError on a value that cannot produce coherent trigger
    math. That is the same posture settings.json already takes toward an
    unknown key: a project's settings are content the user may not have
    authored (D17's premise), and while nothing here can destroy anything
    -- the archive is never edited by compaction, which is exactly the
    property that makes this safe -- an absurd trigger would force
    compaction constantly, and every compaction is a real model call the
    user pays for.
    """
    import config

    values = {
        key: getattr(config, attr) for key, attr in _COMPACTION_DEFAULTS.items()
    }
    values.update(get_settings().get("compaction") or {})
    values.update({k: v for k, v in (overrides or {}).items() if v is not None})

    strength = values["strength"]
    if strength not in config.COMPACTION_TARGET_RATIOS:
        raise ValueError(
            f"compaction.strength must be one of "
            f"{sorted(config.COMPACTION_TARGET_RATIOS)}, got {strength!r}")
    if values["strategy"] not in config.COMPACTION_STRATEGIES:
        raise ValueError(
            f"compaction.strategy must be one of "
            f"{', '.join(config.COMPACTION_STRATEGIES)}, "
            f"got {values['strategy']!r}")
    if values["trigger_tokens"] < 1:
        raise ValueError(
            f"compaction.trigger_tokens must be positive, got "
            f"{values['trigger_tokens']}")
    if values["warning_margin_tokens"] >= values["trigger_tokens"]:
        raise ValueError(
            f"compaction.warning_margin_tokens ({values['warning_margin_tokens']}) "
            f"must be strictly less than trigger_tokens "
            f"({values['trigger_tokens']}), or the early warning fires after "
            f"the compaction it is warning about.")
    if values["keep_recent_tokens"] >= values["trigger_tokens"]:
        raise ValueError(
            f"compaction.keep_recent_tokens ({values['keep_recent_tokens']}) "
            f"must be strictly less than trigger_tokens "
            f"({values['trigger_tokens']}), or every message is protected at "
            f"the moment compaction fires and there is nothing to summarize.")
    for key in ("keep_recent_turns", "max_retries"):
        if values[key] < 0:
            raise ValueError(f"compaction.{key} must not be negative, "
                             f"got {values[key]}")

    # M1's arithmetic, enforced rather than left in a comment. The prompt
    # is re-billed on every step of a tool-using turn, so a turn starting
    # at T tokens and running k steps spends roughly k*T against the
    # CONFIGURED spend cap (settings.json max_token_budget). A trigger too
    # close to that cap lets a thread reach a size where the cap ends the
    # turn after one response -- compaction would then only ever fire on
    # the turn AFTER the one it should have saved.
    #
    # ONLY MEANINGFUL WHEN A CAP EXISTS (#4). Uncapped -- the default --
    # nothing competes with compaction: the trigger is a context-size
    # target and always was; the billing meter never gated it and now
    # gates nothing at all by default.
    # WARN ONLY WHEN ASKED, which is once at startup. This function is
    # on should_compact()'s path, so it runs once per step of every
    # turn -- an unconditional warning here would repeat a
    # configuration complaint dozens of times per conversation, which
    # is how a real warning becomes one nobody reads. The VALIDATION
    # above still raises on every call; only the advisory is gated.
    cap = spend_cap()
    if warn and cap is not None:
        headroom = cap / max(values["trigger_tokens"], 1)
        if headroom < 3:
            logger.warning(
                "compaction.trigger_tokens (%s) leaves room for only ~%.1f model "
                "calls within your configured spend cap max_token_budget (%s), "
                "because a tool-using turn re-sends its whole prompt each "
                "step. Turns on a nearly-full thread will stop early with "
                "token_budget_exceeded before compaction gets a chance. "
                "Lower the trigger or raise the cap.",
                values["trigger_tokens"], headroom, cap)
    return values


def _read_settings_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    # utf-8-sig for the same reason as _parse_md_file: a BOM'd
    # settings.json makes json.load raise at the first character.
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)  # json.JSONError propagates as ValueError
    _validate_settings(data, path)
    return data


def _load_merged_settings(project_path: str, trusted: bool) -> dict:
    """Resolution order: project (trusted) > user. Anything absent falls
    through to config.py defaults at the consumer.

    Nested sections (`_NESTED_SETTINGS`) merge one level deeper than the
    rest. Every other setting is a scalar, so whole-value replacement IS
    per-key override for them; for a nested key, a plain dict.update()
    would let a project setting `buffer_tokens` silently discard the
    user's `strength` -- a per-key override everywhere else that becomes a
    wholesale reset here, purely because of the value's type.

    Driven off _NESTED_SETTINGS rather than naming sections inline: §16
    added `tui` alongside `compaction`, and a second hardcoded section name
    here is exactly how the first one came to be missed.
    """
    merged = _read_settings_file(os.path.join(_user_config_dir(), "settings.json"))
    if trusted:
        project = _read_settings_file(os.path.join(
            workspace_trust.venastine_dir(project_path), "settings.json"))
        deep = {
            section: {**merged.get(section, {}), **project.get(section, {})}
            for section in _NESTED_SETTINGS
        }
        merged.update(project)
        for section, value in deep.items():
            if value:
                merged[section] = value
    return merged


_state: Optional[dict] = None


def initialize(project_path: str) -> None:
    """Load everything once at startup. Untrusted project content is
    absent entirely -- not loaded-but-disabled (D17)."""
    global _state
    trusted = workspace_trust.is_trusted(project_path)
    if not trusted:
        logger.warning(
            "Project .venastine/ at %s is not trusted; project-level "
            "content (agents, skills, settings, CONTEXT.md) will not load.",
            project_path,
        )
    settings = _load_merged_settings(project_path, trusted)
    context = None
    if trusted:
        context_path = os.path.join(
            workspace_trust.venastine_dir(project_path), "CONTEXT.md")
        if os.path.exists(context_path):
            # Degrade, don't abort. UnicodeDecodeError is a ValueError, so
            # an unreadable CONTEXT.md would reach main.load_project_config's
            # handler and SystemExit(1) EVERY invocation in this directory
            # -- including plain chat, which never reads the file. Every
            # other malformed content file in this module warns and skips.
            try:
                with open(context_path, "r", encoding="utf-8-sig") as f:
                    context = f.read()
            except (OSError, UnicodeDecodeError) as e:
                logger.warning(
                    "Could not read %s; continuing without project "
                    "context: %s", context_path, e)
    _state = {
        "project_path": os.path.realpath(project_path),
        "trusted": trusted,
        "agents": _discover("agents", project_path, trusted),
        "skills": _discover("skills", project_path, trusted),
        "settings": settings,
        "context": context,
    }
    # §32 A3. Before effective_compaction, because this one is about
    # THIS repository's own files rather than about the user's -- if
    # it fires, every other startup check is reporting on a build that
    # should not have shipped.
    assert_spawnable_declared(_state["agents"])
    # AFTER _state is assigned, because effective_compaction() reads
    # get_settings(). Called here so an incoherent compaction block is a
    # STARTUP error naming the file, rather than a ValueError from inside
    # a compaction three hours into a long thread -- which is the one
    # moment the user least wants to find out. §21's "reject at load time,
    # not incoherent trigger math later".
    effective_compaction(warn=True)


def assert_spawnable_declared(agents: dict) -> None:
    """Every HARNESS-tier agent must say whether it is spawnable.

    ROADMAP_v2 §32 (A3). The same shape as
    tools.base.assert_grant_policy_declared and
    assert_budget_declared, and for the same stated reason: a field
    whose absence silently means something is a field nobody will
    remember to set. #69 exists because there was no field at all, so
    every agent inherited "spawnable" and four shipped ones were
    advertised as a delegation route that cannot carry them.

    HARNESS TIER ONLY, and that asymmetry is the decision rather than
    an omission. These are files this project ships, so a missing
    declaration is a build error and CI is where it should surface.
    A user's or a project's agent gets the safe default instead --
    raising there would take down startup over somebody else's file,
    which is the trade _parse_md_file already refuses everywhere else
    in this module.
    """
    undeclared = sorted(
        f"{a.name} ({a.path})" for a in agents.values()
        if a.tier == "harness" and a.spawnable is None)
    if undeclared:
        raise RuntimeError(
            "These harness agents do not declare `spawnable` in their "
            "frontmatter, so nothing can tell whether spawn_subagent "
            "is able to feed them (ROADMAP_v2 §32 A3): "
            + ", ".join(undeclared))


def reset() -> None:
    """Clear the startup cache (tests)."""
    global _state
    _state = None


def get_agents() -> dict:
    """Empty before initialize(), matching every other getter here.

    Uninitialized means "no discovery has run", which is materially the
    same state as "discovery ran and found nothing" for every consumer --
    both mean there are no agents to offer. get_agents/get_skills used to
    raise while get_settings/get_skill/skill_catalog_text/context_for_agent
    returned empty, so the answer to "is calling this before startup an
    error?" depended on which getter you happened to pick. Only main.py's
    __main__ calls initialize(); §16's TUI, §18 and §19 add more entry
    points, so one convention now beats six discoveries later.
    """
    if _state is None:
        return {}
    return dict(_state["agents"])


def get_skills() -> dict:
    """Empty before initialize() -- see get_agents()."""
    if _state is None:
        return {}
    return dict(_state["skills"])


def get_settings() -> dict:
    if _state is None:
        return {}  # pre-init consumers fall through to config.py defaults
    return dict(_state["settings"])


def spend_cap() -> Optional[int]:
    """The configured per-run spend ceiling (#4), or None for uncapped.

    The single resolution point every path reaches through the loop
    wrappers' `_SPEND_UNSET` sentinel: chat turns, each research pass,
    /init, the reviewer and its retries. One number applies per _run()
    invocation -- a user turn, a pass, an init run -- because that is what
    a spend ceiling means; nothing here reasons about context size, which
    is thresholds()/context_limit()'s instrument (compaction has NEVER
    keyed off this figure).

    None -- the default, with or without the key present -- runs uncapped:
    #4's whole point is that the harness's cumulative counter is a billing
    meter, and hard spend limits belong to the provider's dashboard.
    """
    return get_settings().get("max_token_budget")


def get_project_path() -> Optional[str]:
    """The RESOLVED project path this session was initialized against.

    ROADMAP_v2 §21b (M12/D25): a project-scoped memory is keyed to "the
    same resolved project path the workspace-trust store uses", so there
    has to be exactly one answer to "which project" across trust, config
    and memory. This is it -- the realpath already computed at
    initialize() time, not a fresh resolution that could differ.

    None before initialize(), matching every other getter here. A caller
    that gets None has no project to scope to, which memories/manager.py
    treats as "global memories only" rather than as "show everything":
    guessing a project would surface another one's facts.
    """
    if _state is None:
        return None
    return _state["project_path"]


def get_skill(name: str) -> Optional[SkillDef]:
    if _state is None:
        return None
    return _state["skills"].get(name)


def get_agent(name: str) -> Optional[AgentDef]:
    """None before initialize() or for an unknown name -- see
    get_agents(). AgentManager (§18) is the consumer."""
    if _state is None:
        return None
    return _state["agents"].get(name)


def context_for_agent(agent: Optional[AgentDef]) -> Optional[str]:
    """CONTEXT.md is opt-in per agent (use_project_context), so no agent
    (or an agent without the flag) never pays its token cost. Untrusted
    projects have context=None regardless."""
    if _state is None or agent is None or not agent.use_project_context:
        return None
    return _state["context"]


def skill_catalog_text(active: Optional[list] = None) -> str:
    """Frontmatter-only catalog for system prompt injection. Bodies are
    deliberately absent -- the model requests them via load_skill.
    Empty string when uninitialized or when no skills exist, so prompt
    assembly is a no-op append.

    GROUPED BY CATEGORY (§19 K4). Uncategorised skills come first, then
    categories alphabetically, so a flat catalog reads exactly as it did
    before any category folder existed.

    `active` names skills whose full body is already pinned into this
    prompt (§19 K1). They are MARKED rather than omitted: dropping them
    would make the catalog disagree with `/skill`'s listing, and leaving
    them unmarked invites the model to spend a turn calling load_skill for
    text it can already read.
    """
    if _state is None or not _state["skills"]:
        return ""
    active_set = set(active or ())
    lines = [
        "## Available skills",
        "The skills below are available in this session. Only their "
        "summaries are listed here; call the load_skill tool with a "
        "skill name to view a skill's full instructions before "
        "following one.",
    ]
    by_category: dict = {}
    for name in sorted(_state["skills"]):
        skill = _state["skills"][name]
        by_category.setdefault(skill.category, []).append(skill)
    # "" sorts before any real category name, which is the order wanted:
    # uncategorised first, then categories alphabetically.
    for category in sorted(by_category):
        if category:
            lines.append(f"### {category}")
        for skill in by_category[category]:
            suffix = (" [ACTIVE — full text below]"
                      if skill.name in active_set else "")
            lines.append(f"- {skill.name}: {skill.description}{suffix}")
    return "\n".join(lines)


def _catalog_entries(root: str, files: list) -> list:
    """(kind, name, description) for every agent/skill in the listing.

    ROADMAP_v2 §32 A6, #131. The trust prompt's stated criterion is
    written in describe_project_content below -- the files shown
    verbatim are "the ones whose contents change what runs". An
    agent's and a skill's `description` meets it exactly: one `y` puts
    both into the system prompt of every run in this project, with no
    tool call and no further consent, and the reason they were left
    out was that nobody had noticed they qualify.

    PARSED THROUGH _parse_md_file, so what is shown is the NORMALISED
    text -- the same value A5 will put in the prompt, not the raw
    frontmatter. That is the whole point of showing it: a summary
    displaying something other than what gets injected is a worse
    answer than showing nothing, because it invites trust in the
    wrong string. It also means the listing is bounded by
    construction, which is what makes printing it safe at all.

    Runs on UNTRUSTED content by definition, so a file that will not
    parse is reported as unreadable rather than skipped in silence:
    'this file is here and I cannot tell you what it says' is a fact
    the person answering the prompt needs.
    """
    out = []
    for rel in files:
        parts = rel.split("/")
        if len(parts) < 2 or parts[0] not in ("agents", "skills"):
            continue
        if not rel.lower().endswith(".md"):
            continue
        kind = parts[0]
        defn = _parse_md_file(os.path.join(root, *parts), kind,
                              "project")
        if defn is None:
            out.append((kind, rel, "(could not be read -- see the log)"))
        else:
            out.append((kind, defn.name, defn.description))
    return out


def describe_project_content(project_path: str) -> str:
    """Human-readable summary shown in the trust prompt, so approving
    trust is an informed decision -- a project's settings can choose the
    provider and multiply pipeline cost via ensemble_n, and an agent's
    or a skill's description reaches every system prompt (#131)."""
    root = workspace_trust.venastine_dir(project_path)
    files = workspace_trust.content_files(project_path)
    lines = [f"Project .venastine/ content ({root}):"]
    lines += [f"  - {rel}" for rel in files]

    # settings.json and mcp.json are shown VERBATIM, the rest by name.
    # These two are the ones whose contents change what runs: settings can
    # pick the provider and multiply pipeline cost via ensemble_n, and
    # mcp.json names a local command to execute. Approving trust without
    # seeing that command is not an informed decision, which is the whole
    # reason §17 moved project MCP config under .venastine/ instead of
    # leaving it at the project root outside the trust boundary.
    for fname, label in (("settings.json", "settings.json contents:"),
                         ("mcp.json", "mcp.json contents (these commands would run):")):
        path = os.path.join(root, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                body = f.read()
        except (OSError, UnicodeDecodeError) as e:
            # This function runs ONLY for untrusted projects, i.e. content
            # that is adversarial by definition. UnicodeDecodeError is a
            # ValueError, not an OSError, so without it here a UTF-16
            # settings.json kills startup before the trust prompt renders
            # -- including the non-TTY notice and the --trust-project path,
            # making trust ungrantable for that project.
            lines.append(f"{fname}: could not be read ({e})")
            continue
        lines.append(label)
        lines += ["  | " + line for line in body.splitlines()]

    # §32 A6. Shown AFTER the two config files rather than beside the
    # file list, because the list answers "what is here" and this
    # answers "what would it say to the model" -- the same order the
    # two questions are asked in.
    entries = _catalog_entries(root, files)
    if entries:
        lines.append("these descriptions enter EVERY system prompt in "
                     "this project:")
        for kind, name, description in entries:
            lines.append(f"  | {kind[:-1]} {name}: {description}")
    return "\n".join(lines)
