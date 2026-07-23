# Venastine Research Harness

A custom agentic harness with two modes: a regular tool-using chat loop, and a ten-pass, self-critiquing deep-research pipeline that generates an answer, independently fact-checks and critiques its own claims, audits hidden assumptions, deterministically tiers confidence, and selectively revises only what's flagged.

Supports Anthropic and OpenAI-compatible providers (Google Gemini is stubbed — see ROADMAP.md §9).

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

`main.py` currently runs one hardcoded example query — an interactive CLI (regular chat + research mode, with thread resumption) is specified in full in ROADMAP.md §1 but not yet built.

## Status

Core inference loop, persistence, tool registry with 10 working tools, and the full deep-research pipeline are built and verified (see ARCHITECTURE.md). Not yet usable end-to-end as a day-to-day tool — see ROADMAP.md §1–4 for what's blocking that specifically, versus the rest of the roadmap, which is genuine missing functionality that doesn't block getting a session going.
