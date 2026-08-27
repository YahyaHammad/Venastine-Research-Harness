# Contributing to Venastine Research Harness

Thanks for considering a contribution. This file makes the inbound license explicit so no one has to guess.

## License of contributions

This project is licensed under the **Apache License 2.0** — see [LICENSE](./LICENSE) (Copyright 2026 Yahya Hammad).

By submitting a pull request, issue, or patch you agree that your contribution will be licensed under the same Apache-2.0 terms, per Apache-2.0 §5:

> Unless you explicitly state otherwise, any Contribution intentionally submitted for inclusion in the Work by you to the Licensor shall be under the terms of the Apache-2.0 License, without additional terms.

If you do *not* want this, mark your communication conspicuously as `Not a Contribution`.

### Developer Certificate of Origin (DCO)

Please sign off your commits with `Signed-off-by`:

```
Signed-off-by: Your Name <your.email@example.com>
```

Add it with `git commit -s -m "…"` . It certifies the [DCO 1.1](https://developercertificate.org/):

> You have the right to submit the contribution under Apache-2.0 and it is your original work or you have permission to submit it.

No CLA to sign — the sign-off in git history is the record.

## How to contribute

1. **Fork and branch** from `main`.
2. **Setup (Python 3.11+):**
   ```bash
   pip install -r requirements.txt
   cp providers.json.example providers.json  # stays empty for tests
   python -m pytest -q   # runs all tests, fully offline, uses no API keys
   ```
   * `providers.json` is gitignored — the suite fakes SDKs via `conftest.py`, but needs the file to exist.
   * Optional extras: `pip install -e ".[documents]"` (markitdown) or `pip install -e ".[pdf]"`.
3. **Make your change** — keep it focused. Read `AGENTS.md` then `ARCHITECTURE.md` plus other relevant markdown project docs before touching any code.
4. **Test:** `python -m pytest -q` must stay green. Add a test if you fix a bug; note the breaking-change table in `tests/BREAKING_CHANGES.md` if you touch a pinned behavior.
5. **Update Project Docs:** if everything works and the tests run green and you have completed the set of changes you want in the code, update the project docs — see the table below for what lives where.
6. **Commit with sign-off:** `git commit -s -m "area: what changed"`
7. **Open a PR** against `main` — describe *what* and *why*, not just *how*. Link any issue.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.** Public issues notify watchers, get indexed, and can be exploited before a fix ships. This line is intentionally redundant with `SECURITY.md` — redundancy here is the point.

Instead: email the maintainer at the contact in `CODE_OF_CONDUCT.md` / `SECURITY.md`, or use GitHub's *Report a vulnerability* → *Private vulnerability reporting* on the Security tab. Include: affected file(s) and line(s), the concrete trigger (not just "could be a problem"), and whether you can reproduce it offline. You will get an acknowledgment within a few days; a fix will ship before public disclosure.

## Proposing a large or design change

For anything that adds a new `§` / `D-number`, changes a file-level contract in `ARCHITECTURE.md`, or touches `core/loop.py` / `core/client.py` / `tools/registry.py` / `security/` boundaries: **open an issue first and get a go-ahead before writing code.**

Small bugfixes and doc typos do not need this — just open the PR. Large/design PRs without a prior issue will be asked to split or re-scope, not because the work is unwelcome but because the decision record (`ROADMAP.md`/`ROADMAP_v2.md` D-numbers) is append-only and a deviation needs to be recorded as an explicit owner decision in `DEVLOG.md`.

## Issue format — how we file and triage (reproducible)

Standardized titles and bodies are not bureaucracy — they are what lets a maintainer triage 100+ audit findings without re-reading the code each time. Follow the pattern the repo already uses (see any closed `S1`–`S4` issue, e.g. `#157`, `#133`, `#6`):

**Title — one line, machine-sortable:**

```
[S<severity>][<area>] concise imperative summary
```

* `S1` critical / `S2` high / `S3` medium / `S4` low — from `#15` audit tracker. Use the lowest that still describes the impact.
* `<area>` is one of: `security`, `correctness`, `coherence`, `test-quality`, `docs-drift`, `performance`, `ux`, `stability` — pick the closest; don't invent a new one without discussion.
* Other title shapes in use (don't mix with `S`):
  * `[tracker] Audit Pass 1 — coverage tracker (read this first)` — the single tracker (`#15`)
  * `[unit X<n>] <area> — description` — per-unit reviews (`#134`, `#142`), e.g. `[unit X5] Decision coherence`
  * `[enhancement]` / `[docs]` — non-audit polish, no severity

Example: `[S1][security] shell auto-approval is unbounded: inert commands run on the host with unrestricted arguments`

**Labels — set one:**

* `bug` — security/correctness/coherence/test-quality failures
* `enhancement` — performance/ux polish with no wrong behavior
* `documentation` — drift between code and `README`/`ARCHITECTURE`/`ROADMAP`/`AGENTS`

**Body — four sections in this order (copy-paste this template):**

```markdown
Found during #<context> / Unit <n> (<files:lines>).

## What happens

Concrete trigger, not "could be a problem". Show the exact call, file:line, and the file-system / network / model state that reaches it. Include a minimal code block or table of inputs → outputs if it helps.

## Why it's wrong / Severity

Which invariant it breaks (`D14 forbids widening`, `AGENTS.md:91` layer, etc.) and the real harm. State the severity (`S1`–`S4`) and why.

## Suggested shape (not a patch)

Describe the fix in words — the data shape or boundary that would close it structurally, not a diff. Name the files that would change and what a test would assert. If you have a sketch, keep it small.

## Not doing

What is explicitly out of scope for this issue, so a reviewer knows you considered it.

---
*Teams: add footer* `Found while checking … Measured against <commit> on <branch>.` *so a later reader knows what was in force.*
```

What makes this reproducible:
* Title sorts by severity then area, so `gh issue list --search "label:bug"` and `S1` are the triage.
* Body always starts with *trigger* then *invariant* — no issue is filed with "this could be a problem" and no scenario.
* `Suggested shape` is words, not a PR, so a reviewer can accept the shape without bikeshedding the diff.
* `Not doing` prevents scope creep between audit and fix batches.

If you are unsure about severity or area, file it as `S3`/`coherence` and the maintainer will relabel — don't block on taxonomy.

## Project documentation — what lives where

All docs below are at the repo root unless noted. Update the row that matches what you changed — don't duplicate the same fact in two places (see `AGENTS.md:10` "one copy, several filenames").

| Document | What it is for | Where to find instructions for updating it |
|---|---|---|
| **`README.md`** | User-facing overview: setup, running, research pipeline, security model, TUI/CLI reference, config tables | Update when user-visible behavior, flags, or defaults change. Keep the CLI flag table (`README.md:500`) and TUI command table (`README.md:424`) in sync with `main.py`/`tui/commands.py`. No separate guide — the file is the guide. |
| **`AGENTS.md`** | **Single source of truth** for agent context. `CLAUDE.md`/`QWEN.md` are verbatim pointers to it | `AGENTS.md:60` *Documentation map* + per-section invariants. Change here only when a new invariant or command is added — keep it DRY; do not copy `ARCHITECTURE.md` prose. |
| **`ARCHITECTURE.md`** | File-by-file contracts: what each module owns / does not own, known gotchas | `ARCHITECTURE.md:32` file tree + `AGENTS.md:91` layer table. Update the owning-file table when you add/move a module; add a gotcha entry when you fix a subtle bug. |
| **`ROADMAP.md` (§1–§12) / `ROADMAP_v2.md` (§13–§37)** | Locked design decisions (D1–D31, S1–S4, R1–R16, K1–K7, …) + section specs | These are **append-only specs, not living docs** — never edit a built section's decision record; add a new `§` or `DEVLOG.md` deviation entry instead. Section + D-numbers are stable (`AGENTS.md:65`). |
| **`DEVLOG.md`** | Per-section/per-batch implementation log: what was followed verbatim, what was deviated from and why | Append a new `## Batch N — title (YYYY-MM-DD)` entry. See the last two entries for shape (Context → Decisions → What was built → Verification → Counts). Every deviation was an explicit owner decision — record it. |
| **`TECHNICAL_DEBT.md`** | Open debt items from reviews, with what was fixed vs. left by design | `TECHNICAL_DEBT.md:1` theme list. Add a theme only after a review surfaces it; mark fixes with `RESOLVED (batch N, #issue)` and keep the original text. |
| **`tests/BREAKING_CHANGES.md`** | What breaks each test, the symptom, and the fix — the revert-check oracle | `tests/BREAKING_CHANGES.md:1` table `| Change | Test | Fix |`. Add a row when you pin a new behavior with a test. Tests read this, not the reverse. |
| **`CONTRIBUTING.md`** *(this file)* | Inbound license (Apache-2.0 + DCO) + contributor workflow | This file. Keep the license block verbatim; update the *How to contribute* steps only when `AGENTS.md:13` commands change. The table you are reading is the update guide for the other docs. |
| **`CODE_OF_CONDUCT.md`** | Community behavior (Contributor Covenant 2.1) | Covenant text verbatim. Only edit the `[INSERT CONTACT EMAIL]` / enforcement contact under `## Enforcement`. Otherwise keep as-is. |
| **`PRIVACY.md`** | What is stored locally (`app.db`, `logs/`, `output/`) vs. what leaves (`providers.json` → LLM, `fetch_url`/`web_search`/MCP) and how to delete | Update when storage paths (`APP_DB_PATH`, `AGENT_OUTPUT_DIR`, `AGENT_LOG_FILE`) or external-call surfaces change. Keep the deletion commands (`rm app.db`, `--forget <id>`) executable. |
| **`THIRD_PARTY_NOTICES.md`** | Dependency license assessment (all permissive, Apache-2.0 compatible) | Regenerate via `pip-licenses --format markdown --with-urls` or edit the manual table. Update the version column when you pin/bump a dep in `requirements.txt`/`pyproject.toml:48`. |
| **`LICENSE`** | Apache License 2.0 + copyright line | Only edit `Copyright 2026 Yahya Hammad` line. Never replace the Apache text. Change requires a major version decision. |
| **`.github/workflows/tests.yml`** | CI: `pip install` + `cp providers.json.example providers.json` + `python -m pytest -q` | `AGENTS.md:13` commands are the source; keep the workflow's steps in sync (Python 3.11 pin, `requirements.txt` cache path). See `AGENTS.md:54` fresh-clone note. |
| **`pyproject.toml` / `requirements.txt`** | Project metadata + single dependency list (D22) | `pyproject.toml:40` `dynamic = ["dependencies"]` reads `requirements.txt` — edit only `requirements.txt` for deps; `pyproject.toml:22` for `license`/`authors`/`classifiers`/`[project.urls]`. |
| **`prompts/*.md`, `agents/builtin/*.md`, `skills/builtin/**/*.md`** | System prompts, agent definitions, skill bodies (injected via `with_catalogs()`) | `AGENTS.md:60` + `ARCHITECTURE.md:32`. Agent/skill frontmatter is validated by `core/config_loader.py` — keep `name`/`description` ≤300 chars (`config.py:490`). |

> **Rule of thumb for step 5:** `README.md` for users, `AGENTS.md`/`ARCHITECTURE.md` for contributors, `DEVLOG.md` for the *why behind this PR*, `tests/BREAKING_CHANGES.md` for *how to fix the test if this PR is reverted*. If a fact would live in two of them, pick one (per `AGENTS.md:10`).

## What to expect

* Maintainer review is single-threaded — response may take a few days.
* Small, reviewable PRs merge faster than large ones.
* This project follows the [Code of Conduct](./CODE_OF_CONDUCT.md) in all community spaces.

## Questions

Open an issue at https://github.com/YahyaHammad/Venastine-Research-Harness/issues.

---
*If you need a different inbound license for a specific contribution, state `Not a Contribution` in the PR description and contact the maintainer before submitting.*
