# Venastine Research Harness

An agentic harness built from scratch — no LangChain, no agent framework — with two modes that share the same machinery:

- **Chat** — a normal tool-using assistant loop.
- **Research** — a ten-pass pipeline that writes an answer, then independently fact-checks and critiques its own claims, audits the assumptions hiding underneath them, scores every claim by a fixed formula rather than by asking the model how sure it is, and revises only what came back flagged.

Works with Anthropic, any OpenAI-compatible provider (OpenAI, DeepSeek, Groq, Mistral, local endpoints), and Google Gemini.

The design premise is that a model's own confidence is not evidence. Everything in the research pipeline exists to replace "the model said it was sure" with something you can audit: which claims were checked against sources, which critique survived, which assumptions were never stated, and which numbers produced the final tier.

---

## Setup

**Python 3.11 or newer.** `mcp_client/client.py` uses `asyncio.timeout`, which is 3.11+, and 27 sites use runtime `X | Y` annotations (3.10+). `pyproject.toml` declares the floor, so `pip install -e .` refuses an older interpreter rather than failing later from inside the MCP connect path.

```bash
pip install -r requirements.txt
```

`markitdown` is an optional extra (`pip install -e ".[documents]"`) — its only consumer is behind `read`/`write`/`edit`, which are denied by default and cannot be enabled at runtime. The PDF export path is likewise optional: `pip install -e ".[pdf]"`.

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

**Six of those stages make no model call at all.** Pass 4, Pass 6b, D0, D1, D2 and the merge are ordinary Python. That is the point: the step that decides how confident to be is a formula you can read, not a judgement call the model gets to make about its own work.

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

### A pass that answers the wrong shape is corrected, then it stops the run

Each pass is asked for a specific JSON structure, and each one is checked against that structure
before anything is applied. A payload that parses but is not what the pass promised goes back to the
model with the problem named — the same corrective turn a malformed one gets — and if it comes back
wrong again the run fails, saying which pass and which field:

```
Pass 3b: entry 0 has severity='0.0', which is string; expected number.
Pass 2: expected a JSON array, got object. Return the array itself, not an object wrapping it.
```

That matters more than it sounds. The failures worth preventing are not the loud ones: a model that
answered about claim ids nobody issued used to produce a *finished* run whose trace said
`Pass 3a: grounded 4 unique entities across 3 factual claim(s)` while grounding nothing, and then
spent four more model calls concluding that nothing could be verified. Those counts are what the
pass was **asked** about; the trace now reports what was actually applied — `grounded 2 of 3` — so a
pass that half-worked says so.

The run does not die over a single stray id: entries that resolve are applied, and only a payload
where *nothing* matched is treated as a failure. Every number in `04_confidence.json`'s
`score_breakdown` is one that was actually used, so the fields reconstruct the score they came with.

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

### Every tool says what a call to it may cost

Approval answers *whether* a tool may run. It says nothing about how long it may run for, and until
recently nothing else did either: a maths tool handed a large enough number simply never came back,
taking the turn — or an unattended ten-pass run — with it.

Each registered tool now declares one of three answers, and a tool registered without one fails at
startup rather than at its first call:

| | |
|---|---|
| **compute** | The six maths tools. Pure functions of their arguments with nothing to interrupt them from the inside, so they run in a separate process that can be stopped — 15 seconds by default, plus CPU and memory ceilings on Linux |
| **io** | Anything that reads a file, fetches a page or talks to an MCP server. Each already carries its own limit: a request timeout, a size check before opening, a sandbox |
| **human** | `ask_user` and `spawn_subagent`, which wait on a person or on a separately metered sub-run. A ten-minute pause here is the feature |

The distinction between the first two is not bureaucratic. A timeout wrapped around a blocking read
can only stop *waiting* — the work carries on — so declaring an operation bounded when it is not
would produce a harness that reports timeouts that never happened. Compute tools get a bound that
genuinely stops them; io tools have to already have one, and three that did not were fixed:

```
                             before    after
edit (on a 30 MB file)       60.0 MB   0.0 MB   rejected before opening, as `read` always did
read_project_doc             60.0 MB   0.1 MB   one page costs one page, not the whole document
fetch_url                  unbounded   capped   streamed, so a hostile body stops at the cap
```

When a compute tool does hit its limit, the model is told which tool and which limit, so it can ask
for something smaller instead of retrying the same call:

```
symbolic_math exceeded its 15s limit and was stopped. The inputs given are too
large for this operation -- try smaller values.
```

### Access control: stricter always wins

Two separate questions, asked in this order, and they never blend into one score:

1. **May this tool be called at all?** — a logical AND across the global config and every active layer (the current agent, each skill it activated). A tool disabled globally can never be re-enabled by anything else; that check runs first and unconditionally.
2. **Does this specific call need you to approve it?** — a logical OR across the same layers plus the tool's own dynamic check.

Because approval ORs, an override can only ever *add* a prompt. There is no way to spell "this tool no longer needs approval" from an agent or skill definition — and since those can arrive with a repository you cloned, that asymmetry is the design rather than a gap.

### What needs approval by default

| | |
|---|---|
| No approval | `web_search`, `fetch_url`, `arxiv_search`, `get_time`, the six maths tools (`symbolic_math`, `linear_algebra`, `probability_stats`, `discrete_math`, `logic`, `geometry`), `load_skill`, `pin`, `ask_user`, `todo_write`, `read_project_doc` |
| Depends on the call | `read`, `write`, `edit` — decided per path, and all three are denied by default anyway |
| Always | `shell`, `spawn_subagent`, `remember`, `write_project_doc`, and **every MCP tool** |

Every registered tool appears in that table, and a test asserts it (audit #125): `ToolPermissions` and `ToolApprovals` are plain dataclasses, so "did the table keep up with the registry" is a question the suite can answer instead of a reader. `write_project_doc` is the one the omission mattered for — it is what `/init` uses to write documents into a repository, and §24's I1 gives it a fixed allowlist of document *names* with no path parameter precisely because it is dangerous enough to constrain.

`ask_user` is ungated for a reason worth stating, since a table of default approvals is exactly where it belongs: an approval-gated tool is not *advertised* where nothing can ask, so gating the tool whose entire job is asking would make it invisible rather than deniable.

`pin` and `remember` sit either side of the line that matters: pinning a message is thread-scoped and undoable, while remembering something outlives the conversation and silently shapes ones you have not started yet.

### `shell` is classified, not just approved

`shell` ships **disabled** (`ToolPermissions.shell = False`) and cannot be enabled at runtime, so none of this applies to a default install. If you do enable it, the gate is `config.SHELL_APPROVAL_MODE`:

| mode | behaviour |
|---|---|
| `always` | every command is asked about, whatever it does |
| `tiered` | **shipped.** The classifier decides — see below |
| `never` | nothing is ever asked about |

`ToolApprovals.shell` is **not** the gate; it is the ratchet. It ships `False`, and setting it `True` forces `always` regardless of the mode. An agent's `approval_overrides` reaches the same place with the same one-way power: these can tighten and never loosen. An unknown mode string raises at startup rather than falling back to a default — one direction of that default asks about everything and the other about nothing, and a typo cannot pick.

Under `tiered`, each command is classified **once** into what it can do, and the same answer is read by the approval check and by the sandbox that runs it:

| tier | what it is | where it runs | asked? |
|---|---|---|---|
| `INERT` | read-only command, every argument inside the workspace | host | no |
| `HOST_READ` | read-only, but an argument reaches outside the workspace | host | **yes** |
| `SANDBOXED` | anything else, no network | container | no, if Docker is confirmed up |
| `SANDBOXED_NET` | its first word is on the network allowlist (`curl`, `pip`, `git`, …) | container, with network | **yes** |
| `UNKNOWN` | could not be characterised at all | — | **yes** |

The auto-approved set is narrower than the `read` tool's, which is already unprompted inside the workspace. A `SANDBOXED` command can do what `write` and `edit` can already do without asking — corrupt the workspace — bounded to 1 CPU, 1 GB, 200 processes, 60 seconds, no network and no host filesystem.

**The argument rule does not parse, deliberately.** Every token after the first is read as a path and required to stay inside the workspace; a flag like `-la` passes only because it is *relative*, not because anything recognised it as a flag. Refusing to interpret shell syntax is what makes the check trustworthy — a classifier that parses is a shell parser, and a parser that is wrong auto-approves something dangerous. The cost is occasional false positives: `grep /etc/passwd notes.txt` searches for a string that looks like a path, and costs one prompt.

**An approved host read still reads the host.** `cat /etc/shadow` is asked about; if you say yes, it runs on the host, because that is what you approved. Routing it into a container would answer a different question than the one you were asked.

**`never` is the old unbounded behaviour, kept on purpose.** In that mode `cat ~/.aws/credentials` runs on the host, unprompted, and its output goes into the model's context. It is reachable by writing the word `never` — which is the fix for [#157](https://github.com/YahyaHammad/Venastine-Research-Harness/issues/157): previously you reached it by switching off a field that read like "stop nagging me".

`shell_approval_mode` is **rejected** in `.venastine/settings.json`, by name and with a reason. A project's settings beat your own, and a cloned repository must not be able to set this. Set it in `config.py`.

If your workspace contains a `.venastine/` directory, it is bind-mounted **read-only** inside the container, so a sandboxed command cannot rewrite the context and MCP definitions that feed later prompts. This covers writes on the Docker path only — not reads, and not the subprocess fallback.

### "Headless" means *unable to ask*, not "not a GUI"

When nothing can put a question in front of a human, approval-gated tools are **hidden from the model entirely** rather than offered and then denied. This is not cosmetic. A tool that is advertised and always refused gets chosen again and again, and the only trace is a denial string buried in a tool result — that exact bug shipped once here, in which `fetch_url` was registered, documented as working, and denied on every call for its whole life. Registering a tool without declaring its permissions now raises at import time instead.

The rule that falls out of this: **inability to ask is never treated as permission to proceed.**

Two consequences, both easier to get wrong than they look:

- **A grant is an answer, so a granted tool stays visible.** "Nothing can ask" is not "nothing has answered" — a pre-flight grant answered the question before the call existed. Getting this wrong is what made `--grant-tools` do nothing on its own for three sections (see below).
- **A headless run cannot spawn a subagent at all**, because spawning is itself approval-gated. That is also why a spawned subagent *keeps* its gated tools: the only runs that can spawn have a human reachable, and the child inherits that route. It is not a separate permission — it falls out of this rule.

When a gated call *is* refused because nothing could ask, the model is told which of the two reasons applies: the tool is unavailable however it is called, or it needs approval **for these arguments** and may not for others. "Requires approval and was not given" invites a retry that cannot succeed, and the model spends its remaining steps discovering that.

### Content policy, on the way in and on the way out

Symmetric, and applied centrally to every tool in every mode rather than by tools that opted in:

- **Arguments are refused, not rewritten.** If a tool call carries something matching a credential pattern or a URL on the blocked-domain list, the call is refused. Silently rewriting an argument would run the tool with parameters neither you nor the model chose, and then report success.
- **Results are redacted.** Anything key-shaped in a tool's output becomes `[REDACTED]` before it reaches the model, the transcript, the database **or the log file**. This recurses through nested structures — third-party MCP servers return arbitrary JSON, and that was once the one path nothing scanned — and it covers *every* key of a result, not a list of expected ones, because the two search tools returned theirs under a key the list did not name. The log was a fourth sink and an unlisted exception rather than an accepted one: a failing tool's traceback carries the request that produced it, which for an HTTP client is a URL with the key in its query string.
- **URLs are checked before every request, including redirects.** `fetch_url` follows redirects by hand and re-checks each hop, so a public URL that redirects to a blocked domain or into your private network is refused *before* the request is issued. Addresses that are not publicly routable — loopback, private ranges, link-local, and the cloud metadata endpoint at `169.254.169.254` — are refused outright. The blocklist matches subdomains. **Known limitation:** the address check resolves and then hands the URL to the HTTP client, which resolves again, so a host whose DNS changes between the two is not covered.
- **The scan fails closed.** Traversal is depth-bounded so a hostile structure cannot exhaust the stack, and hitting that bound redacts rather than passing the value through. A bound that emits unchecked data is just a documented bypass.
- **Refusal reasons never quote the secret.** Explaining that a credential leaked, by printing it, would leak it into model context, the transcript and the database.
- **Tool arguments are redacted before they are truncated** for display — truncating first can cut a key below the length its pattern needs to match, which turns a display feature into a disclosure.

### Project directories are untrusted until you say otherwise

A repo you clone can carry `.venastine/` — agents, skills, `settings.json`, `mcp.json`, `CONTEXT.md`. On first run in a directory you are shown what it contains and asked once — the file list, `settings.json` and `mcp.json` verbatim, and **every agent's and skill's description**, because those go into the system prompt of every run in the project the moment you say yes. Until then that content is **absent, not loaded-and-disabled**. Trust is keyed to the resolved path *and* a content hash, so it does not silently carry over when the files change.

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

- **A grant only covers tools with no per-call gate**, which in practice means MCP tools — literally so: **on a stock install with no MCP server connected, the picker is empty.** `shell` and the file tools decide case by case from the actual arguments, so granting the *name* would authorise a call you never saw; nobody ticking a checkbox labelled "shell" is agreeing to every command for the rest of the run. Those fall back to being asked. Everything else is either ungated already or excluded below.
- **A grant is per tool, and shows you what each one says it does.** MCP exposes no read-only/write metadata, so nothing can tell you that one granted tool searches a library and another posts to a channel; showing its own description is the whole of informed consent here.
- **Every tool declares how far approving its *name* goes**, and both places that can pre-grant — an unattended run, and the sign-off when the model spawns a subagent — read the same declaration. They used to keep separate lists, which is how the sign-off ended up offering the two tools below.
- **`spawn_subagent` and `remember` can never be pre-granted, anywhere.** Approving a spawn *is* the sign-off for the child's entire tool set, so pre-granting it would compound one yes into unbounded delegated authority. A memory outlives the run. An *attended* run can still approve either one live — a per-call human decision, which is what the gate is for.
- **`write_project_doc` can be signed off for one turn, but never pre-granted to an unattended run.** It resolves a document *name* to a path and overwrites it with no diff and no second question, and one of the files it can write is the project context injected into later sessions. A human answering about one named agent for one turn is consent; one tick covering ten unattended passes that are reading fetched web pages is not.
- **A grant makes the tool visible to the model, not just permitted.** These are two different things and the difference was a real bug: gated tools are normally withheld from a run that has no way to ask about them, and until recently the grant was not consulted when deciding that — so `--grant-tools` *on its own* authorised a tool and then sent the model the same list as an unflagged run, while printing that it had authorised something. It only worked alongside `--attended`, which is not the mode any of this is for. A stale grant still cannot widen anything: what may be pre-granted is re-checked against the tool's own declaration at the point the list is built.
- **Running out of grant budget degrades to asking, not to failing.** One budget is shared across all ten passes, and it does not retract a tool mid-run — the ceiling changes *when* you get asked, never what the model can see.
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

An agent declares `spawnable` — whether the `spawn_subagent` tool can actually feed it. A task string in a fresh thread is all a spawn can pass, and an agent that needs a transcript, a thread or a finished research run cannot be given one; spawning it anyway produces a confident answer about nothing. The four built-in agents each have a real caller that supplies what they need, so none of them is spawnable, and the catalog no longer offers them as a route that cannot work. Your own agents are not spawnable unless they say so.

### A description is text, and the prompt treats it as one line

An agent's or a skill's `name` and `description` go straight into the system prompt, so they are collapsed to a single line and capped at 300 characters when the file is read. Without that, a description written as a multi-line YAML block escapes its bullet and renders as a section of the prompt in its own right — indistinguishable from one this harness wrote. The cap is generous next to real ones: the longest description shipped here is 171 characters.

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

### The CLI reads stdin exactly once

Chat mode, the approval prompts, the trust prompt, `--init` and the MCP acknowledgement all read
through **one** object. That sounds like an implementation detail and is not: before it, an approval
prompt started a background reader that never stopped, so from the first gated tool call onwards
chat's own `input()` lost every line to it and blocked forever — silently, and looking exactly like
the model thinking. Recovery was Ctrl+C and `--thread <id>`.

Two disciplines, because the two kinds of prompt want opposite things:

| | drains stale input | deadline |
|---|---|---|
| the gated prompts — approvals, sign-offs, questions, review | **yes** | from the channel |
| the chat prompt and the startup prompts | no | none |

The drain is a safety rule: a line typed after a question timed out was answering *that* question,
and letting it fall through would approve the next call on the strength of it. The chat prompt must
*not* drain, for the mirror-image reason — a line typed while the model was still streaming is an
answer to the prompt about to appear, and the terminal has been buffering it for you.

**A deadline belongs to a run, not to a question.** An approval asked during an attended research run
expires after `ATTENDED_APPROVAL_TIMEOUT_S` (600s) and denies that call, because there is a run
waiting on the answer and it has to keep moving. `--init`'s prompts have no run behind them, so they
wait as long as you need — and they do not print a `(600s)` they would not honour.

`--help` and a mistyped flag now do nothing at all. They used to create a 77 KB six-table SQLite
database and a log directory in whichever directory you ran them from, before argparse had seen a
single flag.

`--review` and `--no-review` in chat mode are refused rather than ignored, matching
`--grant`/`--grant-tools`/`--attended`. Inside the TUI, use `/research --review <query>`.

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

**Built:** ROADMAP.md §1–§12 (§10's ensemble mode rebuilt by its revisit — diversity now comes from a roster of different models) and ROADMAP_v2.md §13 (streaming loop), §14 (config loader + workspace trust), §15 (permissions), §16 (TUI), §17 (MCP), §18 (agents), §19 (skills), §20 (post-pipeline review), §21a (compaction + `pin`), §21b (durable memory), §22 (live pipeline progress), §25 (authorised tool use in the pipeline), §26 (research legibility: per-pass tool calls, code-stage announcements, the claims view, role colour, `/copy`), §27 (thread legibility: transcript replay on resume, a conversations-only thread picker), §21c (session summaries and cross-thread referencing — §21 is now complete), §24 (`/init`), §23 (interactive tools: the response channel, per-tool subagent sign-off, `ask_user`, and the todo list), §28 (the shell capability classifier).

**Remaining:** nothing in either roadmap, and §10's revisit is now closed. TECHNICAL_DEBT.md items 9 and 10 are open, and two live checks are recorded against §10 (see DEVLOG) that cannot be settled offline: the default `temperature` on OpenAI and Google, and whether OpenAI's reasoning models belong in `config.MODELS_REJECTING_SAMPLING_PARAMS`.

**Not finished, though: an audit is open.** Both roadmaps being built is not the same as the
code being clean, and saying only the first would be the "built and runs are different claims"
mistake this project keeps recording against itself. Audit Pass 1 is tracked in GitHub issues —
**102 open**, of which 3 are S1 and 18 are S2 — and is being worked in numbered fix batches
(eight merged or pending so far, each recorded in `DEVLOG.md` and `tests/BREAKING_CHANGES.md`
with what was measured and what would break the fix).

**#157 — `shell`'s unbounded auto-approval — is closed** by batch 8 and ROADMAP_v2 §28; the
classifier is described under *Security model* above. If you have a fork or a local `config.py`,
note that `ToolApprovals.shell` now ships `False` and `SHELL_APPROVAL_MODE` is the gate — see
`tests/BREAKING_CHANGES.md` §24.

Run the test suite with `pytest` — 2105 tests, fully offline, no API keys needed. One further test is marked `integration` and excluded by default; it spawns a real stdio MCP server (`pytest -m integration`).

## Documentation

This file is the user-facing one. For working on the code:

- **[AGENTS.md](./AGENTS.md)** — the agent-context file, and the place to start. `CLAUDE.md` and `QWEN.md` are pointers to it so a tool looking for a particular filename finds one without the guidance being duplicated.
- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — file-by-file contracts, what belongs where, and the known gotchas. Read before changing anything non-trivial, especially around persistence (`database.py` / `storage.py` / `core/memory.py` are three distinct and easily confused responsibilities).
- **[ROADMAP.md](./ROADMAP.md)** and **[ROADMAP_v2.md](./ROADMAP_v2.md)** — implementation specs plus the locked Design Decisions Record that most of the reasoning above traces back to.
- **[DEVLOG.md](./DEVLOG.md)** — what was followed, what was deviated from, and why.
