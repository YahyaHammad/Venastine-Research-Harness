"""
tools/builtin/load_skill.py

Skill progressive disclosure (ROADMAP_v2 §14): the model's system prompt
lists available skills as name + one-line description only; this tool
returns a skill's full body on request. View-only -- activation
semantics (additional_tools, slash commands) belong to §19's
SkillManager, not here.
"""

from core import config_loader

TOOL_SCHEMA = {
    "name": "load_skill",
    "description": "View the full instructions of an available skill. "
                   "The system prompt lists available skills by name and "
                   "one-line summary; call this with a skill name to load "
                   "its complete methodology before following it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name exactly as listed in the "
                               "Available skills catalog.",
            },
        },
        "required": ["name"],
    },
}


def has_skills() -> bool:
    """ToolSpec.available_check (ROADMAP_v2 §15, review finding F5).

    With no skills discovered -- or before config_loader.initialize() has
    run at all -- the system prompt carries no catalog, so this tool's
    only possible answer is "Unknown skill". Advertising it anyway is the
    same defect shape as the fetch_url one §15 exists to fix: a schema the
    model can see, choose, and never get value from. Returning False here
    keeps it out of registry.schemas() until there is something to load.

    Reads the SAME function prompt assembly uses (skill_catalog_text, via
    prompts/system_prompts.with_skill_catalog) rather than a parallel
    check, so "advertised" and "catalogued" cannot drift apart.
    """
    return bool(config_loader.skill_catalog_text())


def run(params: dict) -> dict:
    # params.get, not params[]. NO provider validates tool inputs against
    # the schema, so a call with the key missing or misspelled raised a
    # bare KeyError -- and _run() catches only ToolCallDenied, so it
    # aborted the whole turn, skipped any remaining batched tool calls,
    # and persisted no tool_result for the tool_use id. An error dict
    # lets the model see what it got wrong and correct itself, which is
    # what the unknown-name branch two lines down already does.
    name = params.get("name")
    if not name:
        return {"error": "load_skill requires a 'name' -- the skill's name "
                         "exactly as listed in the available skills catalog."}
    skill = config_loader.get_skill(name)
    if skill is None:
        return {"error": f"Unknown skill: {name!r} is not in "
                         f"the available skills catalog."}
    return {"result": skill.body}
