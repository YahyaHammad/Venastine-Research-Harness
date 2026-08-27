# Privacy — Venastine Research Harness

**No telemetry. No analytics. No tracking.**

This harness runs entirely on your machine from a checkout. It does not phone home, does not collect usage data, and has no hosted backend.

## What is stored locally

All state is in files in the repository's working directory (or the paths you configure via environment variables):

| File / directory | What it holds | Created when |
|---|---|---|
| `app.db` (`APP_DB_PATH`) | SQLite: `MessageLog`, `ConversationThread`, `PipelineRun`, `CompactionCheckpoint`, `ThreadSummary`, `UserMemory` — every chat turn, every research pass, and any `remember` memories | First `python main.py` run creates it |
| `logs/app.log` (`AGENT_LOG_FILE`, 1 MB × 3 rotated) | WARNING+ transcript + provider/HTTP traces. Redacted via `safety/policy_enforcement.redact_secrets` (same patterns as tool output) but may still contain query text | Every run appends |
| `output/<run_id>/` (`AGENT_OUTPUT_DIR`) | Per-run artifacts: `report.md`, `report.pdf` (if `weasyprint`), `00_plan.md` … `07_review.json`, `sources/`, `trace.md` | Every ` --mode research` run |
| `providers.json` (`AGENT_PROVIDERS_FILE`) & `.env` | Your local secrets (LLM provider keys + tool API keys). Gitignored + `export-ignore`; never sent except as described below | You create via `cp *.example` |
| `~/.config/venastine/settings.json` & `~/.config/venastine/mcp.json` | Your user-tier settings/MCP servers | On first trust / settings save |
| `~/.config/venastine/trusted_projects.json` | Workspace trust store (path + content hash) | After you answer the trust prompt |

Everything above is **local-only** by default. `git` does not track `app.db`/`logs/`/`output/`/`providers.json`/`.env` (see `.gitignore` + `.gitattributes` `export-ignore`).

## What leaves your machine

Only explicit external calls you configured or the model chose:

* **LLM provider** — the prompt + tool results are sent to the provider named in `providers.json` (e.g. `ANTHROPIC`, `OPENAI`, your OpenAI-compatible endpoint at `API_URL`). No other party sees them from this harness.
* **Tool fetches** — `web_search` (via `ddgs` → DuckDuckGo/Brave/etc.), `fetch_url` (direct HTTP GET), `arxiv_search`, and any **MCP server** you listed in `mcp.json` (stdio `command` or http `url`/`headers`). Each is a real HTTP/process call; the target site/server sees the request.
* Nothing else. There is no crash reporter, no update check, no hosted proxy.

Fetched content is subject to its origin's terms (including `robots.txt` / ToS). See the grounding note in `README.md` — do not republish `report.md`/`report.pdf` with large verbatim excerpts without rights.

## Retention and deletion

No automatic expiry — a thread lives until you delete it.

* **Wipe everything:** `rm app.db` + `rm -rf output/` + `rm logs/app.log*` — next run recreates an empty DB.
* **One thread:** there is no single-thread delete command; wiping the DB is the intended granularity (threads are cheap and local).
* **Durable memories:** `python main.py --memories` to list, `python main.py --forget <id>` to delete one by id (never by substring, by design).
* **Settings/trust:** delete `~/.config/venastine/settings.json` or `trusted_projects.json` entries directly.

## If you publish a report

`output/<run_id>/report.md` may quote third-party pages. Before publishing, verify you have the right to share those excerpts (fair-use / citation) and redact any private data that entered via a tool result or a prompted memory.

## Questions

Open an issue at https://github.com/YahyaHammad/Venastine-Research-Harness/issues.

---
*This project is self-hosted. There is no data controller beyond you. If you fork and add hosted telemetry, you become responsible for disclosing it.*
