# Venastine Research Harness

An agentic harness built from scratch — no LangChain, no agent framework — with two modes that share the same machinery:

- **Chat** — a normal tool-using assistant loop.
- **Research** — a ten-pass pipeline that writes an answer, then independently fact-checks and critiques its own claims, audits the assumptions hiding underneath them, scores every claim by a fixed formula rather than by asking the model how sure it is, and revises only what came back flagged.

Works with Anthropic, any OpenAI-compatible provider (OpenAI, DeepSeek, Groq, Mistral, local endpoints), and Google Gemini.

The design premise is that a model's own confidence is not evidence. Everything in the research pipeline exists to replace "the model said it was sure" with something you can audit: which claims were checked against sources, which critique survived, which assumptions were never stated, and which numbers produced the final tier.

---

## Setup

```bash
pip install -r requirements.txt
```

### Two credential stores, deliberately separate

| What | Where | Managed by |
|---|---|---|
| **LLM provider keys** (Anthropic, OpenAI, Google, …) | `providers.json` | `credentials.py`, template at `providers.json.example` |
| **Tool API keys** (GitHub token, NVD key, …) | `.env` | `env_secrets.py`, template at `.env.example` |

```bash
cp .env.example .env     # then fill in only the keys your tools actually need
```

Both are gitignored. They are kept apart on purpose: provider keys are read by one module at model-call time, while tool keys are handed to code that talks to third parties. Mixing them would put every tool's secrets in reach of the credential path and vice versa. If it is ever unclear which file a new secret belongs in, it belongs in `.env` unless a model provider is going to authenticate with it.

---

## Running it

```bash
python main.py                                    # chat, new thread
python main.py --thread <uuid>                    # resume a thread
python main.py --mode research "some query"       # the ten-pass pipeline
python main.py --provider OPENAI --model gpt-5.1  # override provider/model
python main.py --tui                              # the full-screen interface
```

The line-based CLI is the default and is never going away — every feature is reachable from it. The TUI is a nicer shell over the same code.

Ctrl+C or Ctrl+D exits. The thread id is printed after the first response so you can resume later — and resuming **replays the conversation**, so you can see what you are picking up. Tool calls replay as one line each; their results do not, since a single research turn can carry hundreds of kilobytes of fetched pages.

---

## How the research pipeline works

One query goes in. Ten passes run, each in **its own fresh conversation** — they hand each other distilled JSON, never raw history, so a later pass cannot be talked into agreeing with an earlier one by reading how it argued.

```
Pass 0   Plan            what needs establishing, and what entities matter
Pass 1   Generate        the first full answer
Pass 2   Extract         break that answer into individually checkable claims
  D0     Route           are there factual claims? (pure code)
Pass 3a  Ground          check each factual claim against real sources
Pass 3b  Critique        attack each factual claim on its merits
Pass 3c  Completeness    what did the answer never address at all?
Pass 5   Assumptions     what is being taken for granted and never stated?
  Pass 4 Tier            score and tier every claim (pure code, no model)
  D1     Route           which claims are flagged? (pure code)
Pass 6a  Revise          rewrite only the flagged claims
Pass 6c  Re-validate     re-ground and re-critique them
  D2     Route           retries exhausted → fall back to UNVERIFIED (pure code)
Pass 6b  Annotate        attach tier labels to claim text (templated, no model)
  Merge  Assemble        final claim set (pure code)
Final    Synthesise      the report, written from the tiered claims
```

**Five of those stages make no model call at all.** Pass 4, Pass 6b, D0, D1, D2 and the merge are ordinary Python. That is the point: the step that decides how confident to be is a formula you can read, not a judgement call the model gets to make about its own work.

### Confidence tiers

Every claim comes out with a tier, a score, and the breakdown that produced it.

| Tier | Meaning |
|---|---|
| **HIGH** | score ≥ 0.8 |
| **MEDIUM** | score ≥ 0.55 |
| **LOW** | score ≥ 0.3 |
| **UNVERIFIED** | below 0.3 — **or any factual claim that could not be grounded, regardless of score** |
| **UNVERIFIED_COVERAGE** | not a claim at all: something Pass 3c found the answer never addressed |

For a factual claim the score is `0.5 × grounding + 0.35 × (1 − critic severity) − 0.15 per assumption flag`. Non-factual claims (opinion, projection, definition) are capped at 0.65 and can never reach HIGH, because there was nothing to ground them against. The weights live at the top of `core/reasoning/confidence_scoring.py` and are meant to be tuned.

A claim that stays flagged after two revise/re-validate rounds falls to UNVERIFIED rather than being quietly retried forever.

### Different models can check each other

Set `CRITIC_MODEL` and passes 3a, 3b and 6c run on a *different* provider and model from the one that wrote the answer. A model checking its own output brings its own blind spots to the inspection.

### What you get out

Every run writes `output/<run_id>/`:

```
00_plan.md            04_confidence.json    trace.md
01_raw_response.md    05_assumptions.json   report.md
02_claims.json        06_revisions.json     report.pdf          (if weasyprint installed)
03_grounding.json     07_review.json        confidence_chart.png
03_critic.json        granted_calls.json    sources/  code/
03_completeness.json
```

Runs are also persisted to the database, so a run that crashes halfway still leaves a record populated through its last completed pass — and so does one you kill with Ctrl+C.

### Watching it happen

A ten-pass run takes minutes. It reports as it goes, in both shells: pass boundaries, the tools each pass is calling, code-only stages, claim tiers as they land, retry rounds, and warnings.

```bash
python main.py --mode research "does the claim about X hold up?"
```

In the TUI, a sidebar tracks passes and a running tier tally; `/claims` or **ctrl+l** opens every claim with its tier, grounding status, assumption flags and score breakdown — during the run as well as after it.

### Having it review itself

```bash
python main.py --mode research --review "some query"
```

A reviewer agent reads the finished run and proposes corrections. **Each one is offered to you individually** — accept, reject, refine, or reject everything. Nothing is applied without your say-so, and there is deliberately no "accept all" button, because an injected instruction only needs one reflexive yes. If nothing can ask you (a piped or scripted run), the review still runs and still records what it found, but applies nothing.

---

## Security model

The harness assumes the model is working with untrusted text — web pages it fetched, files it read, output from third-party MCP servers — and that any of it may be trying to steer the next tool call.

### Access control: stricter always wins

Two separate questions, asked in this order, and they never blend into one score:

1. **May this tool be called at all?** — a logical AND across the global config and every active layer (the current agent, each skill it activated). A tool disabled globally can never be re-enabled by anything else; that check runs first and unconditionally.
2. **Does this specific call need you to approve it?** — a logical OR across the same layers plus the tool's own dynamic check.

Because approval ORs, an override can only ever *add* a prompt. There is no way to spell "this tool no longer needs approval" from an agent or skill definition — and since those can arrive with a repository you cloned, that asymmetry is the design rather than a gap.

### What needs approval by default

| | |
|---|---|
| No approval | `web_search`, `fetch_url`, `arxiv_search`, `get_time`, the six maths tools, `load_skill`, `pin` |
| Depends on the call | `read`, `write`, `edit` — decided per path |
| Always | `shell`, `spawn_subagent`, `remember`, and **every MCP tool** |

`pin` and `remember` sit either side of the line that matters: pinning a message is thread-scoped and undoable, while remembering something outlives the conversation and silently shapes ones you have not started yet.

### "Headless" means *unable to ask*, not "not a GUI"

When nothing can put a question in front of a human, approval-gated tools are **hidden from the model entirely** rather than offered and then denied. This is not cosmetic. A tool that is advertised and always refused gets chosen again and again, and the only trace is a denial string buried in a tool result — that exact bug shipped once here, in which `fetch_url` was registered, documented as working, and denied on every call for its whole life. Registering a tool without declaring its permissions now raises at import time instead.

The rule that falls out of this: **inability to ask is never treated as permission to proceed.**

### Content policy, on the way in and on the way out

Symmetric, and applied centrally to every tool in every mode rather than by tools that opted in:

- **Arguments are refused, not rewritten.** If a tool call carries something matching a credential pattern or a URL on the blocked-domain list, the call is refused. Silently rewriting an argument would run the tool with parameters neither you nor the model chose, and then report success.
- **Results are redacted.** Anything key-shaped in a tool's output becomes `[REDACTED]` before it reaches the model, the transcript or the database. This recurses through nested structures — third-party MCP servers return arbitrary JSON, and that was once the one path nothing scanned.
- **The scan fails closed.** Traversal is depth-bounded so a hostile structure cannot exhaust the stack, and hitting that bound redacts rather than passing the value through. A bound that emits unchecked data is just a documented bypass.
- **Refusal reasons never quote the secret.** Explaining that a credential leaked, by printing it, would leak it into model context, the transcript and the database.
- **Tool arguments are redacted before they are truncated** for display — truncating first can cut a key below the length its pattern needs to match, which turns a display feature into a disclosure.

### Project directories are untrusted until you say otherwise

A repo you clone can carry `.venastine/` — agents, skills, `settings.json`, `mcp.json`, `CONTEXT.md`. On first run in a directory you are shown what it contains and asked once. Until then that content is **absent, not loaded-and-disabled**. Trust is keyed to the resolved path *and* a content hash, so it does not silently carry over when the files change.

```bash
python main.py --trust-project        # for CI and scripts
```

For MCP servers, agents and skills, precedence is **harness > user > project** — inverted from the usual "more specific wins", because here "more specific" means "arrived with someone else's repository".

### Granting tools to an unattended run

A ten-pass research run has no human at the keyboard for minutes at a time. Two independent controls, both off by default:

```bash
python main.py --mode research --grant "query"                    # pick from a list, interactively
python main.py --mode research --grant-tools mcp__files__search "query"
python main.py --mode research --attended "query"                 # approve every gated call as it happens
```

The limits are the interesting part:

- **A grant only covers tools with no per-call gate**, which in practice means MCP tools. `shell` and the file tools decide case by case from the actual arguments, so granting the *name* would authorise a call you never saw — nobody ticking a checkbox labelled "shell" is agreeing to every command for the rest of the run. Those simply fall back to being asked.
- **A grant is per tool, and shows you what each one says it does.** MCP exposes no read-only/write metadata, so nothing can tell you that one granted tool searches a library and another posts to a channel; showing its own description is the whole of informed consent here.
- **`spawn_subagent` and `remember` can never be pre-granted.** Approving a spawn *is* the sign-off for the child's entire tool set, so pre-granting it would compound one yes into unbounded delegated authority across ten passes. A memory outlives the run. An *attended* run can still approve either one live — that is a per-call human decision, which is what the gate is for.
- **Running out of grant budget degrades to asking, not to failing.** One budget is shared across all ten passes.
- **Grants are never persistable, and the setting is rejected by name** so nobody "fixes" the omission. A persisted *mode* can only ever add prompts; a persisted *grant list* could only ever remove them — and `settings.json` is the one config file where a project's values beat yours.

### MCP servers: allowed by default, trusted by default never

MCP tools can be called without you naming them, but every call is approved individually unless you add `"autoApprove": true` to that server. They are third-party code the model can reach, and config files grow entries that installers appended.

---

## Everything else

### Long conversations condense themselves

Older turns are summarised automatically as a thread grows. **The archive is never edited** — the full history is always recoverable, and each compaction summarises the *originals*, so exactly one summarisation step ever sits between what you said and what the model sees. Summaries are never written to disk as messages.

`pin` keeps recent turns out of any summary; `/compact` in the TUI triggers it by hand. A research pass only compacts at a hard backstop near the real context window — spending a model call on a judgement nobody is watching is not worth it mid-run.

A replayed thread always shows what you originally said, never the summary — the archive is what is replayed, so a compacted conversation reads back the way you had it.

### Durable memory

`remember` saves a fact that outlives its thread, scoped to this project or to everything — **with your approval each time**, which is also why it is unreachable from any unattended research pass.

```bash
python main.py --memories        # list what's visible from here
python main.py --forget <id>     # delete one, by id
```

Removal is by id, never by substring: a substring matching two memories would have to pick one, and picking silently deletes something you did not name. Injection is capped at 50 most-recent, and the model is told when it is seeing a truncated set.

### Agents and skills

Markdown files with YAML frontmatter, discovered from three tiers (built-in, yours at `~/.config/venastine/`, the project's at `.venastine/`). Agents set a system prompt and a tool policy; skills pin a body of instructions into the session. `/agent`, `/skill`, `/goal`, `/grill-me`.

A skill declaring `additional_tools` is stating a *need*, not granting anything — activation proceeds and tells you what is missing.

### MCP servers

Configured in `mcp.json`, at `~/.config/venastine/mcp.json` or `.venastine/mcp.json`. Claude Code compatible, and unrecognised keys are ignored so a config shared with another client works as-is:

```json
{"mcpServers": {"files": {"command": "npx", "args": ["-y", "@example/server"]}}}
```

Tools are namespaced `mcp__<server>__<tool>`. A server that fails to connect is named and skipped, never fatal.

### The TUI

```
/help          /theme [name]        /effort [level|auto]     /model [[PROVIDER] name]
/research      /claims [run id]     /copy [last|report|claims|all] [--file <path>]
/compact       /threads             /new                     /quit
/agent         /goal                /grill-me                /skill
/memories      /forget               /summary                 /ref [--list|--clear]
/init [--software|--research]
```

When a subagent is spawned, you are asked which of its approval-gated tools it may use without asking again — per tool, all unticked by default. Running it with none selected is fine, and refusing the spawn entirely is a separate answer from granting it nothing. Before this, approving a spawn authorised the child's whole set.

`/init` reads the project and writes its documentation set. `.venastine/CONTEXT.md` is the hub — the short, factual description this harness injects into every agent that opts into project context — and it links out to the documents that hold the detail: `ARCHITECTURE`, `ROADMAP`, `DEVLOG`, `TECHNICAL_DEBT`, `DOCUMENTATION_STANDARDS`, `TEST_WRITING` and `BREAKING_CHANGES` for a codebase, or `RESEARCH_QUESTIONS`, `METHODOLOGY`, `SOURCES`, `FINDINGS`, `LIMITATIONS`, `EXPERIMENT_LOG` and `OPEN_QUESTIONS` for written work. It asks which kind you have — proposing an answer when there is something to go on, and asking outright when the folder is empty.

Only `CONTEXT.md` is written for you in full; the rest arrive as skeletons with real headings and a note in each saying what belongs in it and what does not. That is deliberate: a DEVLOG invented for a project with no history is fiction in a file that then gets committed and read as fact. Documents you already have are never touched, and regeneration revises your `CONTEXT.md` rather than replacing it — you see a diff and approve it before anything is written. From the CLI it is `--init`, with `--software-project` / `--research-project` to skip the question.

`/summary` distils this conversation and shows it — it does **not** shorten what the model sees; that is `/compact`. `/ref` picks another conversation, summarises it, and attaches that summary to this one as standing context: you choose what crosses between threads, so nothing read or argued in one conversation can steer another without your say-so. `/ref --list` and `/ref --clear` are the way back out, and the summaries are labelled so the model knows they are not part of this conversation. From the CLI the same two are launch flags: `--summary <thread>` and a repeatable `--ref <thread>`.

`/threads` lists your **conversations** — not the ~15 internal threads each research run creates, nor the one every automatic compaction makes. Each row leads with its first message so you can tell them apart. A run's own pass threads are recorded with the run (`output/<run_id>/pass_threads.json`) and can still be opened by id with `--thread`, if you want to see how a particular pass argued.

`/model` switches provider and model for the session and saves nothing — use the launch flags or `default_provider` / `default_model` in `settings.json` to make it stick.

`/copy` exists because Textual 1.0 cannot select text at all. Clipboard delivery uses an escape sequence that some multiplexers drop silently and **cannot be confirmed**, so `--file <path>` is the route that provably worked. Shift+drag usually bypasses the mouse capture and lets your terminal select natively.

### Configuration files

| File | Holds | Notes |
|---|---|---|
| `providers.json` | LLM provider keys | gitignored |
| `.env` | tool API keys | gitignored |
| `settings.json` | defaults and modes | unknown keys **raise** |
| `mcp.json` | MCP servers | unknown keys ignored |
| `CONTEXT.md` | project context for the model | |

Precedence for provider and model is CLI flag > `settings.json` > `config.py`.

---

## Status

**Built:** ROADMAP.md §1–§12 (§10's ensemble mode carries a revisit note) and ROADMAP_v2.md §13 (streaming loop), §14 (config loader + workspace trust), §15 (permissions), §16 (TUI), §17 (MCP), §18 (agents), §19 (skills), §20 (post-pipeline review), §21a (compaction + `pin`), §21b (durable memory), §22 (live pipeline progress), §25 (authorised tool use in the pipeline), §26 (research legibility: per-pass tool calls, code-stage announcements, the claims view, role colour, `/copy`), §27 (thread legibility: transcript replay on resume, a conversations-only thread picker), §21c (session summaries and cross-thread referencing — §21 is now complete), §24 (`/init`), §23 (interactive tools: the response channel, per-tool subagent sign-off, `ask_user`, and the todo list).

**Remaining:** nothing in either roadmap. §10's ensemble mode carries a revisit note (its diversity mechanism is a raised `temperature`, which current Anthropic models reject), and TECHNICAL_DEBT.md items 9 and 10 are open.

Run the test suite with `pytest` — 1386 tests, fully offline, no API keys needed. One further test is marked `integration` and excluded by default; it spawns a real stdio MCP server (`pytest -m integration`).

## Documentation

This file is the user-facing one. For working on the code:

- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — file-by-file contracts, what belongs where, and the known gotchas. Read before changing anything non-trivial, especially around persistence (`database.py` / `storage.py` / `core/memory.py` are three distinct and easily confused responsibilities).
- **[ROADMAP.md](./ROADMAP.md)** and **[ROADMAP_v2.md](./ROADMAP_v2.md)** — implementation specs plus the locked Design Decisions Record that most of the reasoning above traces back to.
- **[DEVLOG.md](./DEVLOG.md)** — what was followed, what was deviated from, and why.
