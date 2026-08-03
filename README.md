# Venastine Research Harness

A custom agentic harness with two modes: a regular tool-using chat loop, and a ten-pass, self-critiquing deep-research pipeline that generates an answer, independently fact-checks and critiques its own claims, audits hidden assumptions, deterministically tiers confidence, and selectively revises only what's flagged.

Supports Anthropic, OpenAI-compatible (OpenAI, DeepSeek, Groq, Mistral, …), and Google Gemini providers.

## Documentation

- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — what's built, how it fits together, and exactly what belongs in each file. Read this before making changes, especially before touching anything persistence-related (`database.py` / `storage.py` / `core/memory.py` are three distinct, easy-to-confuse responsibilities).
- **[ROADMAP.md](./ROADMAP.md)** — everything not yet built, with complete implementation specs (exact signatures, exact data shapes, no open design decisions left).

## Setup

```bash
pip install -r requirements.txt
```

**LLM provider credentials** go in `providers.json` (created/managed via `credentials.py` — see ARCHITECTURE.md §4.2). Not committed to version control.

**Misc tool API keys** (e.g. a GitHub token, an NVD/NIST key for future grounding tools) go in `.env`:

```bash
cp .env.example .env
# then fill in only the keys your registered tools actually need
```

These are two intentionally separate mechanisms — see `credentials.py` and `env_secrets.py`'s docstrings, or ARCHITECTURE.md §4.2–4.3, if it's ever unclear which one a new secret belongs in.

## Running it

```bash
python main.py                                    # chat mode, new thread
python main.py --thread <uuid>                    # resume a thread
python main.py --mode research "some query"       # deep-research pipeline
python main.py --provider OPENAI --model gpt-5.1  # override defaults
```

Ctrl+C or Ctrl+D exits chat mode; the thread id is printed after the first response so you can resume it later. Research mode prints the final report plus the trace log, and writes full artifacts to `output/<run_id>/`.

Project-level configuration (`.venastine/` — agents, skills, `settings.json`, `CONTEXT.md`) requires a one-time trust confirmation on first run. Pass `--trust-project` to grant it non-interactively for scripts and CI.

## Status

ROADMAP.md §1–§12 are fully built, as are ROADMAP_v2.md §13 (streaming loop), §14 (config loader + workspace trust), and §15 (permission system). That covers the interactive CLI, the inference loop with streaming, persistence, a 15-tool registry, the full deep-research pipeline, and three-tier agent/skill discovery. Remaining work is ROADMAP_v2.md §16–§21: the TUI, MCP client, agent and skill systems, subagent review, and the memory/compaction system.

Run the test suite with `pytest` — 307 tests, fully offline, no API keys needed.
