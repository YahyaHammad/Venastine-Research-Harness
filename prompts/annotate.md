# Pass 6c — Annotate (documentation only, NOT a live prompt)

This file is not loaded as a system prompt. Pass 6c is implemented as a
template in `core/reasoning/orchestrator.py` -- attaching a claim's
confidence tier as a fixed-format tag (`[TIER]`) -- rather than a
generation, per the build spec's own guidance to prefer a template over
an LLM call wherever the tag format is fixed.

If the annotation format ever needs to become more nuanced than a fixed
tag (e.g. a natural-language caveat tailored to the specific reason a
claim was flagged), that's the point at which this would become a real
prompt again -- add it back to `passes_source_files` in
`prompts/system_prompts.py` and change the orchestrator to call it via
`RunAgentLoop.run_deep_research_mode` the same way every other pass does.
