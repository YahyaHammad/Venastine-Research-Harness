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
(D18). Project vs user collisions resolve project-wins.

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

from core import workspace_trust

logger = logging.getLogger(__name__)

# The repo root (this file lives in <root>/core/). The harness tier is
# shipped with the harness itself, so it resolves from here, not from cwd.
HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FRONTMATTER_DELIM = re.compile(r"^---\s*$", re.MULTILINE)

_KNOWN_SETTINGS = {
    "default_provider": str,
    "default_model": str,
    "ensemble_mode": bool,
    "ensemble_n": int,
    "compaction": dict,
}
_KNOWN_COMPACTION = {
    "strength": int,
    "keep_recent_tokens": int,
    "buffer_tokens": int,
    "warning_margin_tokens": int,
}


@dataclass
class SkillDef:
    name: str
    description: str
    additional_tools: list
    body: str
    tier: str
    path: str


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


def _user_config_dir() -> str:
    # Call-time resolution so tests can redirect it without touching the
    # real user config dir.
    return os.path.expanduser("~/.config/venastine")


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
    frontmatter_text = text[matches[0].end():matches[1].start()]
    body = text[matches[1].end():].strip()
    return yaml.safe_load(frontmatter_text), body


def _parse_md_file(path: str, kind: str, tier: str):
    """Parse one agent/skill .md file. Returns a SkillDef/AgentDef, or
    None (with a warning) for malformed files -- a broken definition
    warns and skips rather than taking down startup."""
    try:
        with open(path, "r", encoding="utf-8") as f:
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
    if kind == "skills":
        tools = fm.get("additional_tools") or []
        if not isinstance(tools, list):
            logger.warning("Skipping skill file %s: additional_tools is not a list", path)
            return None
        return SkillDef(
            name=name,
            description=str(fm.get("description", "")),
            additional_tools=tools,
            body=body,
            tier=tier,
            path=path,
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
    if max_steps is not None and not isinstance(max_steps, int):
        logger.warning("Skipping agent file %s: max_steps is not an integer", path)
        return None
    return AgentDef(
        name=name,
        description=str(fm.get("description", "")),
        model=fm.get("model"),
        provider=fm.get("provider"),
        allowed_tools=allowed,
        approval_overrides=overrides,
        use_project_context=bool(fm.get("use_project_context", False)),
        use_memory=bool(fm.get("use_memory", True)),
        max_steps=max_steps,
        body=body,
        tier=tier,
        path=path,
    )


def _tier_dirs(kind: str, project_path: str, trusted: bool) -> list:
    dirs = [("harness", os.path.join(HARNESS_ROOT, kind, "builtin"))]
    if trusted:
        dirs.append(("project", os.path.join(
            workspace_trust.venastine_dir(project_path), kind)))
    dirs.append(("user", os.path.join(_user_config_dir(), kind)))
    return dirs


def _discover(kind: str, project_path: str, trusted: bool) -> dict:
    found = {}
    for tier, directory in _tier_dirs(kind, project_path, trusted):
        if not os.path.isdir(directory):
            continue
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".md"):
                continue
            defn = _parse_md_file(os.path.join(directory, fname), kind, tier)
            if defn is None:
                continue
            if defn.name in found:
                if found[defn.name].tier == "harness":
                    logger.warning(
                        "%s-tier %s %r collides with a harness builtin; "
                        "the builtin wins and the %s file is ignored (D18).",
                        tier, kind, defn.name, tier,
                    )
                # project vs user: project wins silently (D8) -- the user
                # tier was simply overridden, which is the documented order.
                continue
            found[defn.name] = defn
    return found


def _type_ok(value, expected) -> bool:
    # bool is a subclass of int in Python, so int-typed keys must reject
    # booleans explicitly, and bool-typed keys must reject ints.
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def _validate_settings(data, source: str) -> None:
    """Loud rejection of unknown keys (a typo must not masquerade as a
    setting) and type checks for known ones. Compaction keys are known
    but not consumed until §21 -- they validate here and warn at init."""
    if not isinstance(data, dict):
        raise ValueError(f"settings.json at {source} is not a JSON object")
    for key, value in data.items():
        if key not in _KNOWN_SETTINGS:
            raise ValueError(f"settings.json at {source}: unknown key {key!r}")
        expected = _KNOWN_SETTINGS[key]
        if not _type_ok(value, expected):
            raise ValueError(
                f"settings.json at {source}: key {key!r} must be {expected.__name__}, "
                f"got {type(value).__name__}")
    compaction = data.get("compaction")
    if compaction is not None:
        if not isinstance(compaction, dict):
            raise ValueError(f"settings.json at {source}: 'compaction' must be an object")
        for key, value in compaction.items():
            if key not in _KNOWN_COMPACTION:
                raise ValueError(
                    f"settings.json at {source}: unknown compaction key {key!r}")
            if not _type_ok(value, int):
                raise ValueError(
                    f"settings.json at {source}: compaction key {key!r} must be int")


def _read_settings_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)  # json.JSONError propagates as ValueError
    _validate_settings(data, path)
    return data


def _load_merged_settings(project_path: str, trusted: bool) -> dict:
    """Resolution order: project (trusted) > user. Anything absent falls
    through to config.py defaults at the consumer."""
    merged = _read_settings_file(os.path.join(_user_config_dir(), "settings.json"))
    if trusted:
        project = _read_settings_file(os.path.join(
            workspace_trust.venastine_dir(project_path), "settings.json"))
        merged.update(project)
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
    if "compaction" in settings:
        logger.warning(
            "compaction settings present but not implemented yet "
            "(ROADMAP_v2 §21); ignored.",
        )
    context = None
    if trusted:
        context_path = os.path.join(
            workspace_trust.venastine_dir(project_path), "CONTEXT.md")
        if os.path.exists(context_path):
            with open(context_path, "r", encoding="utf-8") as f:
                context = f.read()
    _state = {
        "project_path": os.path.realpath(project_path),
        "trusted": trusted,
        "agents": _discover("agents", project_path, trusted),
        "skills": _discover("skills", project_path, trusted),
        "settings": settings,
        "context": context,
    }


def reset() -> None:
    """Clear the startup cache (tests)."""
    global _state
    _state = None


def _require_state() -> dict:
    if _state is None:
        raise RuntimeError(
            "config_loader.initialize() has not been called for this run")
    return _state


def get_agents() -> dict:
    return dict(_require_state()["agents"])


def get_skills() -> dict:
    return dict(_require_state()["skills"])


def get_settings() -> dict:
    if _state is None:
        return {}  # pre-init consumers fall through to config.py defaults
    return dict(_state["settings"])


def get_skill(name: str) -> Optional[SkillDef]:
    if _state is None:
        return None
    return _state["skills"].get(name)


def context_for_agent(agent: Optional[AgentDef]) -> Optional[str]:
    """CONTEXT.md is opt-in per agent (use_project_context), so no agent
    (or an agent without the flag) never pays its token cost. Untrusted
    projects have context=None regardless."""
    if _state is None or agent is None or not agent.use_project_context:
        return None
    return _state["context"]


def skill_catalog_text() -> str:
    """Frontmatter-only catalog for system prompt injection. Bodies are
    deliberately absent -- the model requests them via load_skill.
    Empty string when uninitialized or when no skills exist, so prompt
    assembly is a no-op append."""
    if _state is None or not _state["skills"]:
        return ""
    lines = [
        "## Available skills",
        "The skills below are available in this session. Only their "
        "summaries are listed here; call the load_skill tool with a "
        "skill name to view a skill's full instructions before "
        "following one.",
    ]
    for name in sorted(_state["skills"]):
        skill = _state["skills"][name]
        lines.append(f"- {skill.name}: {skill.description}")
    return "\n".join(lines)


def describe_project_content(project_path: str) -> str:
    """Human-readable summary shown in the trust prompt (file list plus
    settings.json verbatim), so approving trust is an informed decision --
    a project's settings can choose the provider and multiply pipeline
    cost via ensemble_n."""
    root = workspace_trust.venastine_dir(project_path)
    files = workspace_trust.content_files(project_path)
    lines = [f"Project .venastine/ content ({root}):"]
    lines += [f"  - {rel}" for rel in files]
    settings_path = os.path.join(root, "settings.json")
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            lines.append("settings.json contents:")
            lines += ["  | " + line for line in f.read().splitlines()]
    return "\n".join(lines)
