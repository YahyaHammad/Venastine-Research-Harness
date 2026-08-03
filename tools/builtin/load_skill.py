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


def run(params: dict) -> dict:
    skill = config_loader.get_skill(params["name"])
    if skill is None:
        return {"error": f"Unknown skill: {params['name']!r} is not in "
                         f"the available skills catalog."}
    return {"result": skill.body}
