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

Add it with `git commit -s -m "…"`. It certifies the [DCO 1.1](https://developercertificate.org/):

> You have the right to submit the contribution under Apache-2.0 and it is your original work or you have permission to submit it.

No CLA to sign, and **no CI check** — CI runs the test suite and nothing else, and the repository's own history predates this file, so sign-off is requested rather than enforced. It is the Apache-2.0 §5 paragraph above that actually establishes the inbound license; the DCO trailer is a clearer per-commit record of the same thing, and a PR without one will not be rejected over it.

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

Instead: email the maintainer at the contact in `SECURITY.md`, or use GitHub's *Report a vulnerability* button on the Security tab where it is enabled. Include: affected file(s) and line(s), the concrete trigger (not just "could be a problem"), and whether you can reproduce it offline. You will get an acknowledgment within **2 business days** and an initial assessment within **7 days**; a fix will ship before public disclosure. `SECURITY.md` carries the full timeline, scope and disclosure policy — those numbers live there, and this paragraph only points at them.

## Proposing a large or design change

For anything that adds a new `§` / `D-number`, changes a file-level contract in `ARCHITECTURE.md`, or touches `core/loop.py` / `core/client.py` / `tools/registry.py` / `security/` boundaries: **open an issue first and get a go-ahead before writing code.**

Small bugfixes and doc typos do not need this — just open the PR. Large/design PRs without a prior issue will be asked to split or re-scope, not because the work is unwelcome but because the decision record (`ROADMAP.md`/`ROADMAP_v2.md` D-numbers) is append-only and a deviation needs to be recorded as an explicit owner decision in `DEVLOG.md`.

## Issue format — how we file and triage (reproducible)

Standardized titles and bodies are not bureaucracy — they are what let a maintainer triage 100+ audit findings without re-reading the code each time. Follow the pattern the repo already uses (see any closed severity-tagged issue, e.g. `#157`, `#133`, `#4`). `.github/ISSUE_TEMPLATE/` encodes this as a form, so opening an issue on GitHub gives you the shape below pre-filled:

**Title — one line, machine-sortable:**

```
[S<severity>][<area>] concise imperative summary
```

* **Severity** `S1` critical / `S2` high / `S3` medium / `S4` low — the audit-severity namespace, defined by the `#15` tracker. Use the lowest that still describes the impact.
  * ⚠️ `S1`–`S4` is also the id range of a *design-decision* family in `ROADMAP_v2.md` (from the §14–§18 review). They are unrelated. `AGENTS.md`'s namespace table is the arbiter; outside an issue title, always write "severity `S1`" so the reference cannot resolve to the wrong record.
* `<area>` is one of: `security`, `correctness`, `coherence`, `test-quality`, `docs-drift`, `performance`, `ux`, `stability` — pick the closest; don't invent a new one without discussion.
* Other title shapes in use (don't mix with `S`):
  * `[tracker] Audit Pass 1 — coverage tracker (read this first)` — the single tracker (`#15`)
  * `[unit X<n>] <area> — description` — per-unit reviews (`#134`, `#142`), e.g. `[unit X5] Decision coherence`
  * `[enhancement]` / `[docs]` — non-audit polish, no severity

Example: `[S1][security] shell auto-approval is unbounded: inert commands run on the host with unrestricted arguments`

**Labels — set one:**

* `bug` — an `S1`/`S2` finding, or any severity where behavior is outright wrong today
* `enhancement` — `S3`/`S4` polish, coherence and performance work where nothing misbehaves yet. Most open findings sit here, including `[S3][coherence]` and `[S3][correctness]` ones — see `#12`, `#9`, `#6`
* `documentation` — drift between code and `README`/`ARCHITECTURE`/`ROADMAP`/`AGENTS`, plus the `#15` tracker itself

**Body — four sections in this order (copy-paste this template):**

```markdown
Found during #<context> / Unit <n> (<files:lines>).

## What happens

Concrete trigger, not "could be a problem". Show the exact call, file:line, and the file-system / network / model state that reaches it. Include a minimal code block or table of inputs → outputs if it helps.

## Why it's wrong / Severity

Which invariant it breaks (`D14 forbids widening`, a layer boundary from `AGENTS.md` § *The layers…*, etc.) and the real harm. State the severity (`S1`–`S4`) and why. Cite `file:line` for code — that convention is exact and expected in an issue body.

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

All docs below are at the repo root unless noted. Update the row that matches what you changed — don't duplicate the same fact in two places (`AGENTS.md`: "one copy, several filenames").

> This table cites **section headings**, not line numbers, so it survives edits to the files it points at. The `file:line` convention used everywhere else in the repo is deliberate and stays — it just doesn't belong in a table that outlives every one of those lines.

| Document | What it is for | Where to find instructions for updating it |
|---|---|---|
| **`README.md`** | User-facing overview: setup, running, research pipeline, security model, TUI/CLI reference, config tables. Also the entry point to the policy docs below — it links `SECURITY`, `CONTRIBUTING`, `CODE_OF_CONDUCT`, `PRIVACY` and `THIRD_PARTY_NOTICES` | Update when user-visible behavior, flags, or defaults change. Keep § *CLI flag reference* and § *The TUI* in sync with `main.py`/`tui/commands.py`, and § *Status* in sync with the actual open-issue counts. No separate guide — the file is the guide. |
| **`AGENTS.md`** | **Single source of truth** for agent context. `CLAUDE.md`/`QWEN.md` are verbatim pointers to it | `AGENTS.md` § *Documentation map* + per-section invariants. Change here only when a new invariant or command is added — keep it DRY; do not copy `ARCHITECTURE.md` prose. |
| **`ARCHITECTURE.md`** | File-by-file contracts: what each module owns / does not own, known gotchas | `ARCHITECTURE.md`'s file tree + `AGENTS.md` § *The layers, and the boundaries that matter*. Update the owning-file table when you add/move a module; add a gotcha entry when you fix a subtle bug. |
| **`ROADMAP.md` (§1–§12) / `ROADMAP_v2.md` (§13–§37)** | Locked design decisions (D1–D31, the §14–§18 review's S1–S4, R1–R16, K1–K7, …) + section specs | These are **append-only specs, not living docs** — never edit a built section's decision record; add a new `§` or `DEVLOG.md` deviation entry instead. Section + D-numbers are stable — see `AGENTS.md` § *Documentation map* and its namespace table. |
| **`DEVLOG.md`** | Per-section/per-batch implementation log: what was followed verbatim, what was deviated from and why | Append a new `## Batch N — title (YYYY-MM-DD)` entry. See the last two entries for shape (Context → Decisions → What was built → Verification → Counts). Every deviation was an explicit owner decision — record it. |
| **`TECHNICAL_DEBT.md`** | Open debt items from reviews, with what was fixed vs. left by design | The theme list at the top of the file. Add a theme only after a review surfaces it; mark fixes with `RESOLVED (batch N, #issue)` and keep the original text. |
| **`tests/BREAKING_CHANGES.md`** | What breaks each test, the symptom, and the fix — the revert-check oracle | The three-column *Change* / *Test* / *Fix* table at the top of the file. Add a row when you pin a new behavior with a test. Tests read this, not the reverse. |
| **`CONTRIBUTING.md`** *(this file)* | Inbound license (Apache-2.0 + DCO) + contributor workflow, issue/PR format | This file. Keep the license block verbatim; update the *How to contribute* steps only when `AGENTS.md` § *Commands* changes. Keep § *Issue format* in sync with `.github/ISSUE_TEMPLATE/`. The table you are reading is the update guide for the other docs. |
| **`CODE_OF_CONDUCT.md`** | Community behavior (Contributor Covenant 2.1) | Covenant text verbatim. Only edit the `[INSERT CONTACT EMAIL]` / enforcement contact under `## Enforcement`. Otherwise keep as-is. |
| **`PRIVACY.md`** | What is stored locally (`app.db`, `logs/`, `output/`) vs. what leaves (`providers.json` → LLM, `fetch_url`/`web_search`/MCP) and how to delete | Update when storage paths (`APP_DB_PATH`, `AGENT_OUTPUT_DIR`, `AGENT_LOG_FILE`) or external-call surfaces change. Keep the deletion commands (`rm app.db`, `--forget <id>`) executable. |
| **`THIRD_PARTY_NOTICES.md`** | Dependency license assessment (declared direct deps; all permissive, Apache-2.0 compatible), plus § *Non-code third-party material* — the third-party **text** the repo reproduces | Regenerate the dependency tables via `pip-licenses --format markdown --with-urls` or edit them by hand. Update the version column when you pin/bump a dep in `requirements.txt` or `pyproject.toml`'s `[project.optional-dependencies]`. Add a row to § *Non-code third-party material* if you ever copy in prose, a template, an asset or a snippet you did not write — and never assert a licence identifier you have not verified at the source. |
| **`LICENSE`** | Apache License 2.0 + copyright line | Only edit `Copyright 2026 Yahya Hammad` line. Never replace the Apache text. Change requires a major version decision. Travels with `NOTICE` — change the copyright in one and you change it in both. |
| **`NOTICE`** | Apache-style notice: project, copyright, licence pointer, and a line delegating dependency detail to `THIRD_PARTY_NOTICES.md` | **Keep it minimal.** It is voluntary (Apache-2.0 §4(d) obliges propagating a NOTICE you *received*, not authoring one), and everyone who redistributes this project must carry its contents forward — so anything added here is added to their burden too. Dependency and attribution detail belongs in `THIRD_PARTY_NOTICES.md`, not here. If you add a file, add it to `license-files` in `pyproject.toml` or it ships in no sdist or wheel. |
| **`.github/workflows/tests.yml`** | CI: `pip install` + `cp providers.json.example providers.json` + `python -m pytest -q` | `AGENTS.md` § *Commands* is the source; keep the workflow's steps in sync (Python 3.11 pin, `requirements.txt` cache path). See the fresh-clone note in that same section. |
| **`.github/ISSUE_TEMPLATE/`, `.github/PULL_REQUEST_TEMPLATE.md`** | The issue and PR forms that encode § *Issue format* below — severity/area dropdowns, the four required body sections, and the security-report contact link | § *Issue format* in this file is the prose source; the templates are its machine-readable copy. Change both together, or the taxonomy drifts. A malformed `.yml` silently disables its form — check *New issue* renders after editing. |
| **`pyproject.toml` / `requirements.txt`** | Project metadata + single dependency list (D22) | `dynamic = ["dependencies"]` reads `requirements.txt` — edit only `requirements.txt` for deps. Edit `pyproject.toml`'s `[project]` block for `license`/`authors`/`classifiers`/`[project.urls]`. |
| **`prompts/*.md`, `agents/builtin/*.md`, `skills/builtin/**/*.md`** | System prompts, agent definitions, skill bodies (injected via `with_catalogs()`) | `AGENTS.md` § *Documentation map* + `ARCHITECTURE.md`'s file tree. Agent/skill frontmatter is validated by `core/config_loader.py` — keep `name`/`description` ≤300 chars (`config.MAX_CATALOG_TEXT_CHARS`). |

> **Rule of thumb for step 5:** `README.md` for users, `AGENTS.md`/`ARCHITECTURE.md` for contributors, `DEVLOG.md` for the *why behind this PR*, `tests/BREAKING_CHANGES.md` for *how to fix the test if this PR is reverted*. If a fact would live in two of them, pick one (`AGENTS.md`: "one copy, several filenames").

## What to expect

* Maintainer **PR** review is single-threaded — response may take a few days. (Security reports run on the faster clock in [SECURITY.md](./SECURITY.md); this line is about pull requests.)
* Small, reviewable PRs merge faster than large ones.
* This project follows the [Code of Conduct](./CODE_OF_CONDUCT.md) in all community spaces.

## Questions

Open an issue at https://github.com/YahyaHammad/Venastine-Research-Harness/issues.

---
*If you need a different inbound license for a specific contribution, state `Not a Contribution` in the PR description and contact the maintainer before submitting.*
