# Privacy — Venastine Research Harness

**No telemetry. No analytics. No tracking.**

This harness runs entirely on your machine, from a checkout or from an npm install. It does not phone home, does not collect usage data, and has no hosted backend.

## What is stored locally

State lives in files, in one of two places depending on how you installed it — and in whatever paths you configure via environment variables, which override both:

* **From a clone**, everything is relative to the directory you run in, as the table below describes.
* **From npm**, the launcher points `app.db` and the log at `~/.config/venastine/` instead, so threads and memories follow you between directories rather than each folder keeping a separate silo. Research artifacts still land in `./output` beside the work that produced them, and an existing `./app.db` in the current directory always wins. `venastine --venastine-doctor` prints the resolved path for every row below.

| File / directory | What it holds | Created when |
|---|---|---|
| `app.db` (`APP_DB_PATH`) — `~/.config/venastine/app.db` on an npm install | SQLite: `MessageLog`, `ConversationThread`, `CompactionCheckpoint`, `ThreadSummary`, `UserMemory` (`storage.py`) and `PipelineRunRecord` (`core/reasoning/pipeline_storage.py`) — every chat turn, every research pass, and any `remember` memories | First run creates it |
| `logs/app.log` (`AGENT_LOG_FILE`, 1 MB × 3 rotated) — under `~/.config/venastine/logs/` on an npm install | **INFO and above** by default (`AGENT_LOG_LEVEL` overrides), including provider/HTTP traces. Redacted via `safety/policy_enforcement.redact_secrets` (same patterns as tool output) but may still contain query text | Every run appends |
| `output/<run_id>/` (`AGENT_OUTPUT_DIR`) | Per-run artifacts: `report.md`, `report.pdf` (if `weasyprint`), `00_plan.md` … `07_review.json`, `sources/`, `trace.md` | Every ` --mode research` run |
| `providers.json` (`AGENT_PROVIDERS_FILE`) & `.env` (`AGENT_ENV_FILE`) | Your local secrets (LLM provider keys + tool API keys). Gitignored, `export-ignore`d, and excluded from the npm tarball by the `files` allowlist; never sent except as described below | You create via `cp *.example` |
| `~/.config/venastine/providers.json` | Optional user-level provider keys. An npm install reads this **only** when the current directory has no `providers.json` of its own — a local one always wins | You create it, for a command you run from anywhere |
| `~/.config/venastine/runtime/<version>-<hash>/` | The Python virtual environment an npm install builds on first run, after asking. Holds the pinned dependencies, no data of yours | First `venastine` run, with your consent |
| `~/.config/venastine/settings.json` & `~/.config/venastine/mcp.json` | Your user-tier settings/MCP servers | On first trust / settings save |
| `~/.config/venastine/trusted_projects.json` | Workspace trust store (path + content hash) | After you answer the trust prompt |
| `<project>/.venastine/` | Project-tier config — `settings.json`, `mcp.json`, plus any project `agents/` and `skills/`. Loaded only after you trust the workspace (`core/workspace_trust.py`) | You create it, or `/init` does |
| `<project>/AGENTS.md` | The project's hub document, injected into the system prompt of any agent that opts in. An ordinary committed file, but it is inside the same trust boundary as `.venastine/` and a change to it re-triggers the trust prompt | You write it, or `/init` does |

Everything above is **local-only** by default, and three separate mechanisms keep it out of the three ways this project is distributed: `.gitignore` for `git push`, `.gitattributes` `export-ignore` for `git archive` and "Download ZIP", and the `files` allowlist in `package.json` for the npm tarball. `app.db`, `logs/`, `output/`, `providers.json` and `.env` are excluded by all three.

## What leaves your machine

Only explicit external calls you configured or the model chose:

* **LLM provider** — the prompt + tool results are sent to the provider named in `providers.json` (e.g. `ANTHROPIC`, `OPENAI`, your OpenAI-compatible endpoint at `API_URL`). No other party sees them from this harness.
* **Tool fetches** — `web_search` (via `ddgs` → DuckDuckGo/Brave/etc.), `fetch_url` (direct HTTP GET), `arxiv_search`, and any **MCP server** you listed in `mcp.json` (stdio `command` or http `url`/`headers`). Each is a real HTTP/process call; the target site/server sees the request.
* Nothing else. There is no crash reporter, no update check, no hosted proxy.

Fetched content is subject to its origin's terms (including `robots.txt` / ToS). See the grounding note in `README.md` — do not republish `report.md`/`report.pdf` with large verbatim excerpts without rights.

## Retention and deletion

No automatic expiry — a thread lives until you delete it.

* **Wipe everything (clone):** `rm app.db` + `rm -rf output/` + `rm logs/app.log*` — next run recreates an empty DB.
  On Windows PowerShell: `Remove-Item app.db, logs/app.log* ; Remove-Item -Recurse -Force output/`
* **Wipe everything (npm install):** `rm -rf ~/.config/venastine/app.db ~/.config/venastine/logs/` plus any `output/` directories in the folders you worked in.
  On Windows PowerShell: `Remove-Item -Recurse -Force "$HOME/.config/venastine/app.db", "$HOME/.config/venastine/logs"`
  To remove the Python environment as well: `venastine --venastine-reinstall` deletes it, or `rm -rf ~/.config/venastine/runtime/`. Uninstalling the package (`npm rm -g @yahyahammad/venastine`) does **not** remove anything under `~/.config/venastine/` — that is your data and config, and it is left for you to delete.
* **One thread:** there is no single-thread delete command; wiping the DB is the intended granularity (threads are cheap and local).
* **Durable memories:** `python main.py --memories` to list, `python main.py --forget <id>` to delete one by id (never by substring, by design).
* **Settings/trust:** delete `~/.config/venastine/settings.json` or `trusted_projects.json` entries directly.

## If you publish a report

`output/<run_id>/report.md` may quote third-party pages. Before publishing, verify you have the right to share those excerpts (fair-use / citation) and redact any private data that entered via a tool result or a prompted memory.

## Questions

Open an issue at https://github.com/YahyaHammad/Venastine-Research-Harness/issues.

---
*This project is self-hosted. There is no data controller beyond you. If you fork and add hosted telemetry, you become responsible for disclosing it.*
