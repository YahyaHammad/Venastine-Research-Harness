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
python main.py --tui                              # Textual TUI (chat + /research)
```

Ctrl+C or Ctrl+D exits chat mode; the thread id is printed after the first response so you can resume it later. Research mode prints the final report plus the trace log, and writes full artifacts to `output/<run_id>/`.

Project-level configuration (`.venastine/` — agents, skills, `settings.json`, `mcp.json`, `CONTEXT.md`) requires a one-time trust confirmation on first run. Pass `--trust-project` to grant it non-interactively for scripts and CI.

**MCP servers** are configured in `mcp.json`, either at `~/.config/venastine/mcp.json` (yours) or `.venastine/mcp.json` (the project's, trust-gated). The format is Claude Code compatible, and keys this harness doesn't recognize are ignored, so a config shared with another MCP client works as-is:

```json
{"mcpServers": {"files": {"command": "npx", "args": ["-y", "@example/server"]}}}
```

MCP tools are namespaced `mcp__<server>__<tool>` and **require approval before each call** by default; add `"autoApprove": true` to a server entry to opt out. On a name collision your user-level entry wins over the project's.

## Status

ROADMAP.md §1–§12 are fully built (§10's ensemble mode carries a revisit note), as are ROADMAP_v2.md §13 (streaming loop), §14 (config loader + workspace trust), §15 (permission system), §16 (Textual TUI shell, reasoning-effort switching), §17 (MCP client), §18 (agent system: `/agent`, `spawn_subagent`, goal mode, `/grill-me`), §19 (skill system: `/skill`, category folders, four built-in skills), §20 (post-pipeline review: a reviewer agent checks the finished run and proposes corrections you accept, reject or refine), and §25 (authorized tool use in the research pipeline: pre-flight grants, attended mode, argument-side content policy). Remaining work is ROADMAP_v2.md §21–§24: memory/compaction, pipeline observability, interactive tools, and `/init`.

Run the test suite with `pytest` — 776 tests, fully offline, no API keys needed. One further test is marked `integration` and excluded by default; it spawns a real stdio MCP server (`pytest -m integration`).
