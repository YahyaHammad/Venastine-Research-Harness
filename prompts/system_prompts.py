import os

# passes_source_files maps pass_id -> the .md file holding its system
# prompt. Dict order = execution order (Python dicts preserve insertion
# order): 0 -> 1 -> 2 -> 3a -> 3b -> 3c -> 5 -> 6a -> 6c -> final_synthesis.
#
# Pass 4 and Pass 6b are DELIBERATELY ABSENT from this dict -- neither
# makes an LLM call in this implementation. Pass 4 is pure Python (see
# confidence_scoring.py); Pass 6b is a template, not a generation (see
# orchestrator.py). Their .md files still exist on disk as human-readable
# documentation of intent, not as prompts that get loaded here.
passes_source_files = {
    "Pass 0": "preliminary_plan.md",       # Forward-looking sketch of expected content, before Pass 1 generates anything
    "Pass 1": "initial_generation.md",     # Initial response seed -- no self-hedging, no self-tagging
    "Pass 2": "claim_extraction.md",       # Extract a JSON list of atomic, independently-classified claims
    "Pass 3a": "source_grounding.md",      # Source grounding using web/arxiv sources, batched by deduplicated entity
    "Pass 3b": "critic_pass.md",           # Checking for fallacies and contradictions, severity-weighted by grounding
    "Pass 3c": "completeness.md",          # Checking for expectation coverage relative to the ORIGINAL query, independent of Pass 1
    "Pass 5": "assumption_audit.md",       # (Comes before Pass 4 intentionally) hidden premises, framing issues, domain gaps
    "Pass 6a": "revise.md",                # Batched rewrite of every currently-flagged claim in one call
    "Pass 6c": "revalidate.md",            # Batched re-grounding + re-critique of the revised claim subset only
    "Final synthesis": "final_synthesis.md",  # Reads the merged, annotated claim set -- writes the human-facing report
}


def get_system_prompts() -> dict:
    passes_prompts = {}
    current_dir = os.path.dirname(os.path.abspath(__file__))

    universal_path = os.path.join(current_dir, "universal_system_prompt")
    with open(universal_path, "r", encoding="utf-8") as file:
        universal_preamble = file.read().strip()

    for pass_id, pass_filename in passes_source_files.items():
        md_file_path = os.path.join(current_dir, pass_filename)
        with open(md_file_path, "r", encoding="utf-8") as file:
            pass_specific = file.read().strip()
        passes_prompts[pass_id] = f"{universal_preamble}\n\n---\n\n{pass_specific}"
    return passes_prompts


passes_prompts = get_system_prompts()


def with_skill_catalog(base_prompt: str, active=None) -> str:
    """Appends the frontmatter-only skill catalog (ROADMAP_v2 §14) when
    skills were discovered at startup; a no-op append otherwise.

    Skill BODIES never enter the prompt from here -- the model requests
    them via load_skill, and §19's activation pins them separately and
    deliberately outside this function (K6, see with_catalogs).

    `active` only MARKS entries whose body is already pinned elsewhere in
    the same prompt, so the catalog does not invite the model to spend a
    turn loading text it can already read."""
    from core import config_loader

    catalog = config_loader.skill_catalog_text(active)
    if not catalog:
        return base_prompt
    return f"{base_prompt}\n\n{catalog}"


def agent_catalog_text() -> str:
    """Frontmatter-only catalog of SPAWNABLE agents (§18, §32 A4).

    Mirrors skill_catalog_text(): the model learns which agents exist
    without any agent body entering the prompt. Empty string when none
    qualify, which is a no-op append rather than an empty heading.

    SPAWNABLE ONLY, and this is #69 rather than a filter for its own
    sake. The prose below is not a hint, it is an instruction -- "can
    be spawned with the spawn_subagent tool" -- and spawn_subagent
    passes exactly one thing: params["task"], as the first message of
    a FRESH thread. An agent whose body opens "Read the thread so far"
    cannot be fed that way. Spawning it does not fail; it produces a
    confident review of the task description, and the parent has no
    way to tell. Degraded rather than broken is exactly what makes it
    worth suppressing.

    THE TUI'S /agent IS UNAFFECTED -- it lists manager.names(), not
    this. A non-spawnable agent is still selectable by a human, who
    supplies the thread the agent needs by being in one.
    """
    from core import config_loader

    agents = {name: a for name, a in config_loader.get_agents().items()
              if a.spawnable}
    if not agents:
        return ""
    lines = [
        "## Available agents",
        "The agents below can be spawned with the spawn_subagent tool. "
        "Only their summaries are listed here; each agent's full "
        "methodology is applied to its own run when spawned.",
    ]
    for name in sorted(agents):
        lines.append(f"- {name}: {agents[name].description}")
    return "\n".join(lines)


def with_catalogs(base_prompt: str, active_skills=None, context=None,
                  callable_only: bool = False, granted=None) -> str:
    """Both frontmatter-only catalogs (skills §14, agents §18) appended;
    the single assembly point every default system prompt goes through.

    §19 K6 -- ACTIVE SKILL BODIES MUST NOT BE APPENDED HERE. This function
    feeds pass_prompt(), so anything added inside it lands in all ten
    research passes; a skill the user activated in a TUI chat session has
    no business governing a pipeline run they started separately. Bodies
    are pinned by the shell, next to its with_goal() call, via
    skills.manager.prompt_fragment(). `active_skills` reaches only as far
    as the catalog, where it marks entries rather than expanding them.

    `context` (§19-§20 review f19, generalised): each catalog's prose
    INSTRUCTS the model to call a tool -- "call the load_skill tool",
    "can be spawned with the spawn_subagent tool". Neither knew the
    ToolContext, so every agent whose allowed_tools excludes one of those
    was invited to call a tool it does not have, and a model that follows
    the invitation burns a denied call against max_steps. §20's reviewer
    was the first caller to notice; the condition belongs here rather
    than as a per-caller opt-out, because a flag saying what the context
    already knows is a second source of truth for the same fact.

    None means no restriction and both catalogs apply.

    `callable_only` / `granted` (#68, A1/A2): THE SAME TWO FACTS
    registry.schemas() decides advertisement from, with the same names
    and the same defaults, because they are the same question. The
    condition here used to be `is_allowed(name, context)`, which is
    POLICY -- "is this tool permitted" rather than "can this run call
    it". A research pass is headless, so spawn_subagent is permitted,
    dropped from the schema list by schemas(callable_only=True), and
    named in that run's own headless_hidden() WARNING -- while this
    function appended 819 characters instructing the model to spawn
    one. 19% of Pass 1's system prompt was an instruction the pass
    could not follow, and the harness said so to its log and to the
    model in the same breath.

    THE DEFAULTS ARE THE ATTENDED ANSWER, matching schemas(). A TUI
    always has a channel, so tui/app.py and agents/tui_commands.py
    pass nothing and are already correct; the pipeline entry points
    pass the real values, derived from the bundle that carries them.
    A caller that forgets is caught by
    test_no_pass_prompt_invites_a_tool_the_pass_cannot_call, which
    drives every pass id rather than the one that prompted this."""
    from tools.registry import registry

    prompt = base_prompt
    # A catalog whose tool is unreachable is not "less useful", it is an
    # instruction that cannot be followed. Suppress the whole section:
    # listing the skills without the means to load one is worse than
    # silence, since the model can then only guess at their contents.
    if registry.is_advertised("load_skill", context, callable_only,
                              granted):
        prompt = with_skill_catalog(prompt, active_skills)
    catalog = agent_catalog_text()
    if catalog and registry.is_advertised("spawn_subagent", context,
                                          callable_only, granted):
        prompt = f"{prompt}\n\n{catalog}"
    return prompt


def pass_prompt(pass_id: str, callable_only: bool = False,
                granted=None) -> str:
    """A research pass's system prompt with the catalogs it can use.

    BOTH the pass entry point (loop.run_deep_research_mode) and the §3
    JSON-retry path (orchestrator) must go through here so the catalogs
    cannot diverge between an original attempt and its retry -- and
    since #68 that includes the two facts, or a retry would see a
    different tool set than the attempt it is correcting."""
    return with_catalogs(passes_prompts[pass_id],
                         callable_only=callable_only, granted=granted)
