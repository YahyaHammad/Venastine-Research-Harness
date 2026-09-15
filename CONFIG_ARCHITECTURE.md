# CONFIG_ARCHITECTURE.md

**Why every value in `config.yaml` is what it is.**

`config.yaml` holds the harness's tunable values and says, in a line or two
per key, what each one *does*. This file is the other half: the argument. Most
of these numbers were chosen against a specific measurement or a specific
incident, several were reversed once, and a few are load-bearing in ways that
are invisible from the value alone. **Read the entry for a key before changing
it.** More than one of these has been "corrected" back to a value it was
deliberately moved away from.

The prose here was `config.py`'s, from the years when the values and their
history lived in one 1,210-line module of which 788 lines were comment. It was
carried over verbatim. Section headings are `config.py`'s own; the entry for
each key is named by its **`config.yaml` key**, so a key you are editing leads
straight here.

**Two things about that port, both worth knowing before you trust a
sentence here.** First, it is no longer verbatim in five places, each
marked and dated: the `tool_compute_timeout_s` arithmetic described a
value the code has not had for some time, three entries told the reader to
set something in `config.py` where an edit is now silently overwritten at
import, and one described a `settings.json` key that does not exist. The
rest is unchanged, so a diff against `config.py` before the migration is
still the way to check it. Second, **this file is hand-maintained from
here.** It was produced once by a migration script that was not kept;
nothing regenerates it, so a value's rationale moves when someone moves
it.

## How configuration is laid out

| File | Holds | Tier |
|---|---|---|
| `config.yaml` | every tunable value | harness, one location |
| `config_schema.py` | the schema, the environment overrides, the loader | -- |
| `config.py` | the same values as module attributes, for the code | -- |
| `~/.config/venastine/settings.json` | the user's overrides | user |
| `<project>/.venastine/settings.json` | a project's overrides, if trusted | project |

`settings.json` is merged across tiers by `core/config_loader.py`, and for the
keys it knows a **project's file beats the user's** (D29) because it is what
you authored versus what arrived with a directory you cloned. That precedence
is the reason for the two rules below.

### One location, and no override variable

`config.yaml` resolves from `config_schema.__file__`, beside `config.py`. There
is no user-tier copy, no project-tier copy, and **no environment variable that
redirects it**. That absence is a security property rather than an oversight.

`settings.json` refuses `shell_approval_mode`, `ensemble_models`,
`critic_model`, `embedder_model` and `research.granted_tools` **by name** --
R12, E2, G7 and SQ7, each argued in `ROADMAP_v2.md` -- and the argument is
always the same shape: a project's `settings.json` beats the user's and arrives
with a clone, so a key there would let a cloned repository set this harness's
approval posture or point its research passes at a provider you never chose. A
file that ships inside the harness, in exactly one place, carries none of that
risk. An `AGENT_CONFIG_FILE` variable would hand it straight back, and it would
look like the obvious sibling of `AGENT_ENV_FILE` and `APP_DB_PATH`. It is not:
those redirect *state*, and this is a shipped asset.

### The one location survives an npm update (batch 86)

`config.yaml` ships inside the package, and **npm replaces a package directory wholesale** -- measured with `npm pack` and two installs into a scratch prefix: an edited `max_tokens: 31337` came back as the shipped `16000`, and the new version's `max_iterations` default arrived with it. So the rule above had a cost nobody had paid attention to: every value an npm user had chosen was discarded at each update.

The fix is a merge, **not a second location**. `config_update.py` keeps the pristine copy of the installed version and a mirror of the user's own file in `~/.config/venastine/config-state/`, and after an update it re-applies the difference onto whatever npm has just written. The live document is still the one inside the harness, still resolved from `__file__`, still with no variable that redirects it -- the state directory holds HISTORY, which is state, and state has always been redirectable here. A reader who finds a `config.yaml` under the home directory and concludes the user tier is back has found the mirror, which nothing reads at startup.

Two consequences worth stating. Keys the user never touched adopt the new version's defaults, and keys the new version adds arrive at theirs -- the merge starts FROM the shipped file, so that half needs no code. And the nine AUTHORITY keys are restored like any other, each one named in the report the launch prints: an update must neither silently loosen nor silently tighten the posture, and the file surviving is the status quo.

### Read once, at startup

`config.py` imports `config_schema`, which reads the file, folds the
environment into the raw document, and validates the result — **once**, in
that order. The environment goes in *before* validation so that there is one
model with the overrides already in it: applied afterwards they would reach
only the namespace `config.py` publishes, and `config_schema.current()` would
quietly disagree with `config.<NAME>` about any key that has a variable.
Nothing re-reads the file. `security/posture.py`
depends on that: `config` binding at import is what makes
`os.environ[...] = ...` unable to move the security posture, where anything
reading `os.environ` at call time would follow the mutation (UN1, and batch
37's asymmetry). `tools/builtin/shell.py` validates the approval mode at its
own import, so the values must be final before it runs.

Editing `config.yaml` while the harness is running therefore changes nothing
until the next launch.

### The keys marked AUTHORITY

Nine keys decide what the harness may do to its host, or name a provider it
sends your content to. Nothing *unattended* can change them -- not a tool
call, not a settings file, not an environment variable.
`config_schema.HARNESS_AUTHORITY_KEYS` is the machine-readable list, and a
test holds it against `security/posture.Posture`'s fields and the five
by-name `settings.json` rejections. (Four ROADMAP markers, five keys:
`critic_model` and `embedder_model` share SQ7.)

**Batch 84 gave `/config` a route to them, behind a confirmation.** Until
then the rule was absolute: no in-session surface at all, a slash command
included. What changed is the reading of *why*. The `settings.json`
rejections argue from a file that arrives with a directory you cloned, and
the environment argument from a variable a subprocess can set; neither
describes a person typing at the prompt, who is the harness operator and is
the one `config.yaml` has always been edited by. The model cannot reach the
route -- `tui/commands.dispatch` is called from the prompt's submit handler
and nowhere else.

**What the file tools do here, stated accurately** (corrected in batch 87).
This said "the file tools refuse the harness install tree", and they do not:
`security/protected_paths.PROTECTED_SEGMENTS` is `{".venastine"}`, so
`file_ops._protected_path_error` denies a path carrying that segment and
returns None for `<install>/config.yaml`. The control that actually holds is
weaker and still real — `write` and `edit` ship **denied** in
`tool_permissions` and cannot be enabled at runtime, and `protected_paths`
makes a workspace that overlaps the harness tree a *startup error*, so the
install tree is never inside the workspace and a write there is
approval-gated rather than auto-approved. `protected_paths.py` says why the
difference is worth a paragraph: a half-understood control is worse than
none.

The compensating control is that the confirmation says what the key
PERMITS, not "are you sure": `config_edit.AUTHORITY_EFFECT` carries one
sentence per key and a test fails if a key has none. And the change still
does not take effect in the running process -- it is written to the file
and applied by relaunching, so the frozen posture is never edited under a
session that has already read it.

`ensemble_mode` is deliberately *not* one of them. It is a mode, persistable in
`settings.json`, and the worst it can do is spend more of a provider you
already chose -- the line `core/config_loader.py` draws between authority and
cost.

### Two values with no key in the file

Both are derived in `config_schema.derived_values()` because neither is a static
default, and giving either a key would change behaviour:

- **`OUTPUT_DIR`** is `AGENT_OUTPUT_DIR`, else `<AGENT_WORKSPACE or ".">/output`
  -- one expression with no branch in it. Note the `"."` default here against
  `workspace_dir`'s `"./workspace"`: an unset variable still means `./output`
  rather than `./workspace/output`. A key would have to be ignored whenever
  `AGENT_WORKSPACE` is named, which is the branch this form exists to avoid.
- **`WORKSPACE_DIR_EXPLICIT`** is the **presence** of `AGENT_WORKSPACE`, never
  its value. `main()` uses it to decide whether the resolved workspace is also
  the project path, and the default `./workspace` is a subdirectory of the
  launch directory -- so a value test would move the project one level down for
  everyone who set nothing. YAML cannot express "was this variable named?".

### The environment variables

`AGENT_MODEL`, `APP_DB_PATH`, `AGENT_WORKSPACE`, `AGENT_SHELL` and
`AGENT_SANDBOX_IMAGE` each override one key; `AGENT_OUTPUT_DIR` and
`AGENT_WORKSPACE` feed the two derived values above.
`config_schema.ENV_OVERRIDES` is the table, and `HARNESS_ENV_VARS` the full
list, so neither has to be maintained by hand anywhere else. An override is
folded into the document before validation, so it goes through the same type
check the file's own value does.

Note that there is **no variable for `config.yaml`'s own path** — see *One
location, and no override variable* above. That is the one absence in this
list which is a decision rather than an omission.


## Contents

- [Model and loop](#model-and-loop)
- [Loop control](#loop-control)
- [Subagents (ROADMAP_v2 §18)](#subagents-roadmap_v2-18)
- [Deep research pipeline](#deep-research-pipeline)
- [Spend cap (batch 27, #4)](#spend-cap-batch-27-4)
- [Ensemble mode (ROADMAP §10, as redesigned by its revisit)](#ensemble-mode-roadmap-10-as-redesigned-by-its-revisit)
- [Sampling-parameter support (ROADMAP_v2 §16 prerequisite)](#sampling-parameter-support-roadmap_v2-16-prerequisite)
- [Critic-model routing (ROADMAP §11)](#critic-model-routing-roadmap-11)
- [Embedder routing (ROADMAP_v2 §45, SQ2/SQ7)](#embedder-routing-roadmap_v2-45-sq2sq7)
- [Source authority: domain classes (ROADMAP_v2 §45, SQ4)](#source-authority-domain-classes-roadmap_v2-45-sq4)
- [The sources/ artifact (ROADMAP_v2 §45, SQ8)](#the-sources-artifact-roadmap_v2-45-sq8)
- [Scholarly authority: OpenAlex (ROADMAP_v2 §45, SQ5/SQ9)](#scholarly-authority-openalex-roadmap_v2-45-sq5sq9)
- [Reasoning effort (ROADMAP_v2 §16; default changed batch 25, #139)](#reasoning-effort-roadmap_v2-16-default-changed-batch-25-139)
- [Database](#database)
- [Output artifacts](#output-artifacts)
- [File-ops workspace (ROADMAP §6)](#file-ops-workspace-roadmap-6)
- [/init (ROADMAP_v2 §24)](#init-roadmap_v2-24)
- [MCP teardown (ROADMAP_v2 §37 F6, #64)](#mcp-teardown-roadmap_v2-37-f6-64)
- [Shell / sandbox (ROADMAP §7)](#shell-sandbox-roadmap-7)
- [Output redaction (#167/#49, batch 20)](#output-redaction-16749-batch-20)
- [Compaction (ROADMAP_v2 §21)](#compaction-roadmap_v2-21)
- [Catalog text (ROADMAP_v2 §32, A5)](#catalog-text-roadmap_v2-32-a5)
- [Summaries, references and the checklist (ROADMAP_v2 §21c / §23)](#summaries-references-and-the-checklist-roadmap_v2-21c-23)
- [Context windows (ROADMAP_v2 §21 M1, as amended by batches 30 and 44)](#context-windows-roadmap_v2-21-m1-as-amended-by-batches-30-and-44)
- [Tool permissions and approvals (ROADMAP_v2 §15, D24)](#tool-permissions-and-approvals-roadmap_v2-15-d24)

---

## Model and loop

### `model_name`

--- Model / loop settings -- these were missing; call_model, RunAgentLoop,
and database.py all depend on them ---

### `max_tokens`

Per-call output ceiling. Raised from 4096 in ROADMAP_v2 §16: on current
Anthropic models max_tokens caps THINKING PLUS RESPONSE TEXT together, so
4096 truncated answers mid-sentence as soon as reasoning effort was
enabled. There is no default cumulative budget for it to interact with any
more (batch 27, #4): spend is uncapped unless settings.json
max_token_budget says otherwise.

## Loop control

### `max_iterations`

Raised from 20 in batch 16 (#45): this is THE default step ceiling -- the
`or config.MAX_ITERATIONS` fallback at every agent-shaped call site, the
value an invalid `max_steps:` frontmatter field is repaired to, and the
ceiling of every chat turn and research pass that does not name its own.

### `model_call_max_retries`

Batch 91. Nothing retried a model call before this, and the SDKs cover less
than they appear to: openai and anthropic retry a request that fails BEFORE
its stream opens (408/409/429/5xx, connection errors, twice each), but an
error sent inside a stream that opened with 200 is raised straight up by
both, and google-genai 1.0 retries nothing at all. OpenRouter reports an
overloaded upstream exactly that way -- "The service is temporarily
unavailable", "Upstream idle timeout exceeded" -- which failed four nested
subagents in the run that prompted this.

Two, so a call gets three attempts. The SDK retries are KEPT (owner
decision) and happen inside each attempt, so the worst case is nine HTTP
requests for one call; turning the SDKs' off would lose their retry-after
handling for the statuses they already cover well. `core/provider_errors.py`
decides what qualifies -- temporary provider errors only, never a bad
request, an auth failure or a harness bug -- and `core/loop.py` decides
WHEN: a run whose output reaches no screen is retried whatever it had
streamed, a run someone is watching only before its first delta, because a
transcript row cannot be taken back.

### `model_call_retry_base_delay_s`

Batch 91. Three seconds, then six, with a quarter either way of jitter.
Deliberately slower than the SDKs' half-second start (owner decision): these
retries sit ABOVE the SDK's own, so by the time one fires the provider has
already refused several requests in quick succession, and a rate limit met
with more of the same only extends itself. The jitter is for siblings: the
failures that prompted this came in pairs from one parallel spawn batch, and
without it they would retry in the same instant. A server's retry-after can
lengthen a wait, never shorten it, up to 60 seconds.

## Subagents (ROADMAP_v2 §18)

### `subagent_max_depth`

Maximum spawn_subagent nesting. The counter lives on
ToolContext.subagent_depth; this is the value it is checked against (C3).

### `subagent_max_parallel`

§47 slice 8 (NA10). How many calls of one response may run at once, for
tools whose ToolSpec declares `parallel`. A ceiling, not a target: a
response naming fewer runs fewer, and a response naming more runs them in
waves of this size. 1 makes the parallel path behave exactly like the
sequential one, which is the escape hatch if concurrency is ever suspect.

Three because that is the case the design keeps citing -- one turn
spawning `explore` three times -- and because three children at depth 1,
each able to nest to SUBAGENT_MAX_DEPTH, is a bounded worst case for the
serialised approval queue and for the SQLite writers.

## Deep research pipeline

### `max_pipeline_retries`

max revise/re-validate loop iterations per claim before fallback

### `max_json_retries`

max corrective follow-up attempts when a pass returns malformed JSON

(total attempts per pass = MAX_JSON_RETRIES + 1). See ROADMAP §3.

## Spend cap (batch 27, #4)

There is NO default spend ceiling any more. The loop's cumulative
input+output counter is a BILLING meter -- the prompt is re-sent and
re-billed on every step of a tool-using turn, so it grows quadratically
in steps, which is correct as billing and meaningless as a size limit.
Reading it as "how large may a thread get" is the defect TECHNICAL_DEBT
item 9 recorded; size reasoning reads ModelResponse.turn_new_tokens and
memory.last_input_tokens now, and compaction keys off measured context
size (it never keyed off this number).

A user who wants a hard ceiling sets settings.json `max_token_budget`
(int tokens per _run() invocation -- one user turn, one research pass,
one /init run). core/config_loader.spend_cap() resolves it, and the
headroom advisory in effective_compaction() speaks only when a cap is
configured. Hard limits at the provider remain the stronger instrument:
they see the same bill and cannot be forgotten by a code path. The three
constants this file used to carry (MAX_TOKEN_BUDGET 250k,
RESEARCH_PASS_TOKEN_BUDGET 1M for passes, INIT_TOKEN_BUDGET 1M) died
here -- the two 1M envelopes existed only because the 250k chat value
was misread as bounding pass context (the Pass-1-with-14-tool-calls
incident in DEVLOG), and with no default cap there is nothing to work
around.

Deferred for now (core sequential pipeline only, per current scope):
```text
  (none remaining -- ensemble_mode/ensemble_n built in ROADMAP §10,
   critic_model built in ROADMAP §11)
```

## Ensemble mode (ROADMAP §10, as redesigned by its revisit)

### `ensemble_mode`

Run Pass 1 once per entry below, then extract the union of claims across
candidates and subtract a disagreement penalty in Pass 5. Off by default.

DIVERSITY COMES FROM DIFFERENT MODELS, NOT DIFFERENT SAMPLING (E1). §10
originally raised `temperature` on N runs of one model. That could not work
on current Anthropic models, which reject sampling parameters outright (see
MODELS_REJECTING_SAMPLING_PARAMS), so the section was built, documented as
working, and could not execute against this harness's own default model.
ENSEMBLE_TEMPERATURE is deleted rather than repaired: it was an ABSOLUTE
value being used as though it were a raise, so what "1.0" meant differed
per provider, and so did how much diversity a run actually got.

The deeper reason is §11's, verbatim: "a model checking its own output for
errors shares that model's blind spots." N samples of one model agree most
confidently on that model's systematic errors, which is exactly where a
research harness needs agreement to mean something. Different models do
not share blind spots, so their agreement is real evidence.

N is len(ENSEMBLE_MODELS) -- derived, never configured separately (E3). The
denominator of a confidence score must not be able to disagree with the
roster that produced it.

`config.yaml` ONLY, deliberately -- there is no settings.json key for
this, following `critic_model` (E2). Trusting a cloned repo already lets it pick
the provider and multiply pipeline cost; a project-tier list of N providers
is that same grant multiplied by N.

Fewer than two DISTINCT (provider_name, model) pairs is refused, not
tolerated: one model cannot disagree with itself, so every claim would
score maximal consistency and Pass 5 would read that as confidence. That
is the original defect, and repeating the same entry N times is the way to
recreate it through this config.

### `ensemble_models`

Example, as it is written in `config.yaml` -- a sequence of mappings,
not the Python assignment this example used to show:

```yaml
ensemble_models:
  - {provider_name: ANTHROPIC, model: claude-opus-5}
  - {provider_name: OPENAI, model: gpt-5.1}
  - {provider_name: GOOGLE, model: gemini-2.5-pro}
```

## Sampling-parameter support (ROADMAP_v2 §16 prerequisite)

### `models_rejecting_sampling_params`

Models that reject temperature/top_p/top_k. Current Anthropic models
removed these parameters outright (any value returns HTTP 400); Sonnet 5
rejects non-default values. Steering is expected to happen through
prompting and MODEL_EFFORT_LEVELS instead.

A static table rather than a capability query: the Models API capability
tree does not report sampling support, so unlike effort levels (which ARE
queryable, see MODEL_EFFORT_LEVELS) there is nothing to ask. Same posture
as §21's MODEL_CONTEXT_WINDOWS -- honest about being incomplete, and
incompleteness is safe here because the failure is a loud 400, not a
silent wrong answer.

## Critic-model routing (ROADMAP §11)

### `critic_model`

Route the critic/grounding passes (3a, 3b, 6c) to a different model than
the generator, so a model isn't checking its own output for errors.
None means every pass uses the same provider/model — no special routing.
Example to enable: {"provider_name": "OPENAI", "model": "gpt-5.1"}

## Embedder routing (ROADMAP_v2 §45, SQ2/SQ7)

### `embedder_model`

The model that turns a claim and a source passage into vectors, so
`similarity_score` is a cosine rather than a self-report. None -- the
default -- means the pipeline warns once at launch and falls back to the
grounding model's own number, under source_grounding.md's anchored
rubric.

`config.yaml` ONLY, with no settings.json key, following `critic_model`
and `ensemble_models` (E2): choosing a provider is a grant, and this one sends
claim text and fetched page text to whoever is named. `/embedder` writes
to the user-tier store in core/pipeline_models.py, which OUTRANKS this.
Example: {"provider_name": "OPENAI", "model": "text-embedding-3-small"}

### `similarity_calibration`

Asymmetric embedders score materially worse without their prefixes and
produce NO ERROR AT ALL when they are missing, which is why this is a
table rather than something inferred. Keyed by a substring of the model
slug, longest match wins; a model matching nothing gets no prefix, which
is correct for every symmetric embedder (OpenAI's, Cohere's, Voyage's).

`task_type` is Google's equivalent and is ignored by every other
provider. Incomplete on purpose: an unlisted asymmetric model scores
lower than it should, which is visible in the artifact as a low cosine
on a source that plainly matches -- and adding a row is the fix.
Raw cosine is NOT a 0-1 score, and treating it as one is the quiet way
to publish a wrong number. Unrelated text sits near 0.1 on some models
and above 0.7 on others, so the same passage would score "irrelevant" on
one embedder and "supports the claim" on another. This maps the useful
band onto 0-1: below `floor` is noise, above `ceiling` is as close as
that model gets.

Keyed by a substring of the model slug, longest match wins; anything
unlisted takes DEFAULT_SIMILARITY_CALIBRATION. The RAW cosine is always
recorded beside the calibrated score, so a bad row here is visible in
the artifact and fixable after the fact rather than baked in.

### `default_similarity_calibration`

### `embedder_prefixes`

## Source authority: domain classes (ROADMAP_v2 §45, SQ4)

### `domain_authority_classes`

What a source is worth before anything is known about the specific page.
Two tables, deliberately: a CLASS carries the number, and a SUFFIX names
which class a host belongs to. A user who thinks preprints deserve more
edits one number; a user adding their field's own trusted domain edits a
membership. One combined {suffix: score} table would make the first of
those a find-and-replace across a hundred rows.

THE SIGNAL IS A RESTRICTED REGISTRY, NOT A FAMILIAR SUFFIX. `.gov`,
`.mil`, `.int` and `.edu` gate registration -- you cannot buy one -- and
so do `gov.uk`, `ac.uk`, `gc.ca` and their equivalents worldwide, which
is why source_scoring derives the `<kind>.<cc>` forms rather than
enumerating them here. `.org` gates nothing at all and never has, so it
sits barely above `.com`; treating it as trustworthy is the single most
common version of this mistake.

INCOMPLETE ON PURPOSE, and safe when incomplete: an unlisted host takes
DEFAULT_DOMAIN_AUTHORITY, which is the generic-commercial number. The
failure mode is a good source scored ordinary, never a bad source scored
authoritative, and the model's bounded adjustment (below) is the route
for the first.

**Notes on individual entries.**

.gov .mil .int .edu, gov.uk, ac.uk...
WHO, UN, World Bank, OECD, EU
IETF, W3C, ISO, NIST, IEC
not peer reviewed; §45 SQ5 refines this
encyclopaedias -- a pointer to a source
an unrestricted registry, small bump

### `default_domain_authority`

== generic_commercial, for an unknown host

### `domain_authority_suffixes`

host suffix -> class. Longest suffix wins, so `blogs.nature.com` can be
listed separately from `nature.com` if it ever needs to be.

**Notes on individual entries.**

Restricted registries. The bare TLDs; the `<kind>.<cc>` second-level
forms are derived in source_scoring.py rather than listed.
Intergovernmental bodies, whose hosts are ordinary .org/.int names.
Standards bodies.
Peer-reviewed publishers and indexes.
Preprint servers and repositories -- not peer reviewed.
News organisations with a correction policy and a masthead. An
ALLOWLIST, because "news site" is not a property of a domain suffix
and the alternative is scoring every .com that publishes articles.
Tertiary references -- they summarise sources rather than being one.
Blog and newsletter platforms: the host says nothing about the author.
Forums and Q&A. Often correct, never attributable.
Social media. Speculation and unattributed claims travel here fastest,
which is the whole reason a claim needs grounding elsewhere.
Aggregators that republish without attribution, and image boards with
no text to ground anything in. The measured case: a Pinterest pin
scored similarity 1.0 on a Nobel Prize claim.

### `authority_adjustment_cap`

How far the model may move a computed authority, in either direction
(§45, SQ4). Small on purpose: it is a correction for what a domain
cannot see -- a primary source on an ordinary host, an opinion column on
a masthead -- not a second opinion about the number. An adjustment
arriving without a reason is discarded rather than clamped.

## The sources/ artifact (ROADMAP_v2 §45, SQ8)

### `persist_source_text`

Whether output/<run_id>/sources/ keeps the TEXT each cited page served,
or only its hash, size and provenance.

On by default, because it is what makes a similarity score reproducible
after the run -- and the directory output_writer has created since §12
was described by fetch_url.py's own comment for just as long. Everything
written there has been through redact_output_text on entry to the
corpus.

It is new data at rest, so it has a switch and PRIVACY.md says what it
holds. Turning it off costs the audit trail, not the scores: the corpus
still exists in memory for the run that computes them.

## Scholarly authority: OpenAlex (ROADMAP_v2 §45, SQ5/SQ9)

### `scholar_lookup`

OFF BY DEFAULT. This is the first network call in this harness made by
something that is not a registered tool, so it bypasses the approval
registry and granted_calls.json -- and an out-of-the-box run must make
no call the user did not ask for. With it off, a paper takes its
preprint-server or publisher domain class and nothing else.

### `scholar_api_url`

### `scholar_timeout_s`

### `scholar_max_retries`

### `scholar_cache_ttl_s`

### `scholar_mailto`

OpenAlex asks for a contact address to put a caller in its "polite pool"
(higher rate limits). EMPTY BY DEFAULT AND NEVER DERIVED: an address is
personal data, and sending one the user did not type -- from a git
config, an environment variable, anywhere -- is not a rate-limit
optimisation, it is an unasked-for disclosure.

### `scholar_venue_weight`

How a paper's score is composed. Renormalised over whichever terms are
actually available, so a paper too young to have a citation percentile
is not scored as though it had a bad one.

### `scholar_citation_weight`

### `scholar_author_weight`

### `scholar_venue_credit`

Venue credit. A preprint scores lower than a comparable published paper
-- generally, not strictly, which is why it is a weight and not a gate.

**Notes on individual entries.**

arXiv, bioRxiv, institutional repositories

### `scholar_h_saturation`

max h-index across a paper's authors, log-saturated. 60 is a full career
at the top of most fields; the curve is deliberately flat above it,
because the difference between 60 and 90 says more about field size and
career length than about this paper.

### `scholar_min_cohort_size`

Below this many works, a field-and-year cohort is too small for a
percentile to mean anything and the window widens instead.

### `scholar_min_citation_age_days`

Below this age, citation counts are noise: 72% of computer-science
preprints from 2024 have zero citations, so a three-week-old paper's
zero says nothing at all. The term is DROPPED and the remaining weights
renormalise, rather than being scored as low impact.

### `scholar_retracted_authority`

A retracted paper is not evidence, whatever its venue or citation count
-- and a heavily cited retraction is the most dangerous shape there is.

## Reasoning effort (ROADMAP_v2 §16; default changed batch 25, #139)

### `default_effort`

The default effort level requested when the user has not chosen one.

This shipped as None -- "send nothing", the one universally safe value,
because reasoning_effort is rejected by OpenAI-compatible NON-reasoning
models. Batch 25 changed it to "high" by owner decision: the pipeline
now consumes the setting (#139), so None meant the mode this project
exists for ran at the provider's own default forever, invisibly. The
risk the old note named is managed in MODEL_EFFORT_LEVELS rather than
by silence: known non-reasoning models are listed there with an EMPTY
level set, which effort_for() treats as authoritative and drops the
level cleanly instead of sending a parameter that would 400.

### `model_effort_levels`

Fallback effort levels for providers whose APIs expose no capability
endpoint (every OpenAI-compatible provider, and Google). ANTHROPIC is NOT
listed here on purpose: its Models API reports per-model effort support,
so client.py queries it and new Anthropic models need no entry.

An entry whose value is an EMPTY list means "this model takes no effort
parameter at all" and is authoritative: effort_for() DROPS the requested
level with a naming warning instead of sending it. That is the safety
mechanism behind DEFAULT_EFFORT="high" -- without these entries, the
fallback for an unknown model ASSUMES ["low","medium","high"] and the
default high would reach gpt-4o-class endpoints as a 400 on every call.
Incomplete on purpose (same posture as MODELS_REJECTING_SAMPLING_PARAMS):
an unlisted non-reasoning endpoint still fails loud, and the fix is one
line here or `--effort auto`.

### `default_effort_levels`

### `google_thinking_budgets`

Google expresses reasoning depth as an integer token budget rather than an
enum, so a level has to be mapped to a number at the boundary. -1 is the
SDK's "decide dynamically" sentinel.

## Database

### `db_path`

## Output artifacts

### `output_dir`

BESIDE THE WORK, which since batch 44 means beside the WORKSPACE when one
was named. "State is global, artifacts are local" is the distribution
split, and the local half is only true if "local" tracks where the work
is: with AGENT_WORKSPACE pointing at a project, a run launched from the
harness checkout wrote its reports into the harness. Reading the env var
rather than WORKSPACE_DIR below, and defaulting it to ".", so this stays
one expression with no branch in it -- an unset AGENT_WORKSPACE gives
./output exactly as before, not ./workspace/output.

## File-ops workspace (ROADMAP §6)

### `workspace_dir`

### `workspace_dir_explicit`

Whether AGENT_WORKSPACE was NAMED, as opposed to defaulted (batch 44).

The presence of the variable, never its value: the default "./workspace"
is a subdirectory of wherever you launched, so a value test would make
`./workspace` the project for everyone who never set anything -- which
is the whole population this must not disturb. main() reads this to
decide the project path; the decision is there, because the config layer
holds plain values and this is one.

Note that this value has NO `config.yaml` key at all -- it is computed in
`config_schema.derived_values()`, because YAML cannot ask whether a
variable was named. There is nothing here to edit.

### `max_file_size_bytes`

25 MB — hard reject before opening

### `max_read_lines`

max lines per read call

### `max_read_chars`

max chars per read call

## /init (ROADMAP_v2 §24)

### `initializer_agent`

### `init_read_chars`

Three bounds, each doing a different job (I7). /init is a tool-heavy loop
over documentation that can run to hundreds of kilobytes -- this repo's own
root markdown is 721KB, with DEVLOG.md alone at 226KB.

INIT_READ_CHARS is well below MAX_READ_CHARS because of TECHNICAL_DEBT
item 9 (now closed, batch 27): the billing meter re-counts the WHOLE
prompt on every step, so each 50KB read is re-billed for every step that
follows it. 20KB keeps a dozen reads affordable and still returns a
useful span of a document per call. It stays even though the meter no
longer caps anything by default -- it bounds per-read VOLUME, which is a
different axis from spend and from the number of reads.

### `init_max_steps`

The number of reads, which neither budget nor read size bounds: a model
can spend an unbounded number of small reads inside any ceiling.

## MCP teardown (ROADMAP_v2 §37 F6, #64)

### `teardown_budget_s`

ONE shared wall-clock budget for the whole goodbye -- polite close,
force-cancel, loop-thread join -- replacing three sequential 15-second
waits whose worst case hung a quitting harness for ~45s, Ctrl+C
included. Servers still alive when the budget expires are named in a
WARNING; their child processes may outlive this session. The value
lives with the other harness bounds and is re-exported by
mcp_client/client.py beside its sibling timeouts.

## Shell / sandbox (ROADMAP §7)

### `shell_binary`

auto-detect if empty

### `allow_insecure_sandbox_fallback`

explicitly enable subprocess fallback

### `auto_approve_sandbox_fallback`

auto-approve fallback runs (no per-run prompt)

### `shell_approval_mode`

ROADMAP_v2 §28 (G3). WHICH shell commands need a human to say yes.

```text
  "always"     every command is asked about, whatever it does.
  "tiered"     the classifier decides -- see security/capability.py for
               the one rule, and security/sandbox.py:classify_command for
               how a command is measured against it. Since §48 (CE1) only
               a read-only INERT command runs unasked: anything that may
               run code asks, contained or not.
  "contained"  §48 (CE2): the rule "tiered" had before §48. A command the
               container confines runs unasked unless it gets network,
               code included; reads outside the workspace and network
               commands still ask.
  "never"      nothing is ever asked about.
```

`auto_approve_sandbox_fallback` is honoured under "tiered" and
"contained" (§48, CE5) -- the one written exception to "code asks" under
"tiered" -- and never under "always", whose check returns before the
opt-in is read (CE6).

There is no list of interpreters behind "tiered", deliberately. `python
-c` is the obvious case, but `pytest` runs a conftest.py, `make` runs a
Makefile, and `"python"`, `python3.13` and `echo x | python` are the same
call to a shell -- a list would be a parser whose misses auto-approve
(G2). Every command that is not INERT may run code, and that is the
answer the classifier gives.

This is the gate. Before §28 the gate was ToolApprovals.shell, and
`_shell_approval_check` looked like a five-layer policy underneath it --
but four of those layers could only ever return False on the shipped
flags, so the whole thing was a pass-through for one boolean and the
harness had exactly two settings: ask about everything, or ask about
nothing. "tiered" is the rung that docstring already claimed existed.

"never" is that second setting, preserved deliberately and now NAMED.
It is the pre-§28 `ToolApprovals.shell = False` behaviour exactly: an
inert command runs on the HOST with unrestricted arguments, so `cat
~/.aws/credentials` returns your keys to the model. You reach that by
writing "never", not by switching off a field that reads like "stop
nagging me" -- which is the actual fix for audit #157.
The legal set lives with the code that ENFORCES it --
security/capability.APPROVAL_MODES, which validate_mode() reads. A second
copy here was read by nothing but a test asserting its literal, so the two
could have disagreed and only the unenforced one would have been wrong.

### `sandbox_docker_image`

The image both runtimes run: Docker, or Podman when Docker cannot run a
container (ROADMAP_v3 §49, SS22). The key kept its name when Podman
arrived, because config_update.py carries a user's own value across an
npm update BY KEY, and a renamed key would silently drop it.

FULLY QUALIFIED since batch 93 (SS23), and the reason is Podman's: an
unqualified name is resolved through `registries.conf`, and with no
search registries and no short-name alias -- measured on a stock Ubuntu
Podman, whose `shortnames.conf` happens to alias `python` -- a name it
cannot resolve fails without a terminal to ask on. `docker.io/library/`
is where Docker resolves the short name anyway. A value you set yourself
is carried as written.

A TAG, NOT AN IDENTITY (ROADMAP_v2 §46, EP7): the resolved image ID is
logged once per process, and two machines -- or two runtimes on one --
pulling the same tag on different days get different images.

### `sandbox_timeout_seconds`

### `sandbox_memory_mb`

### `sandbox_cpu_seconds`

### `sandbox_max_pids`

### `shell_session_timeout_cap_s`

ROADMAP_v3 §49 (SS12, SS13). The longest a background or monitor session
may run, whatever the agent asks for. 3600 because the owner's own example
was an hour, and it is also the ceiling on how long a misbehaving agent can
hold the input block (SS2) with one session.

A value above the cap is CLAMPED, not refused, and the started result says
so. Refusing would make the agent guess numbers until one fits, and the cap
is not a secret. The clamp happens in one place, the session manager, and
is enforced twice: the harness kills at the timeout, and coreutils `timeout`
inside the container ends it `SESSION_TIMEOUT_MARGIN_S` later if the harness
is no longer there to.

### `shell_session_max_live`

ROADMAP_v3 §49 (SS12). Live background and monitor sessions, counted
process-wide across the main agent and every subagent -- a subagent does not
get a quota of its own, or three parallel children could each fill one. The
classic one-shot `shell` is not counted: it holds no slot past its own call.

4, because each is a container under `sandbox_memory_mb` (2048), so the
ceiling this implies is 8 GB of container memory. At the cap a start is
refused with the list of live sessions, never queued: a queued session would
start at a time nobody chose.

### `shell_session_max_consecutive_wakes`

ROADMAP_v3 §49 (SS2). How many wake turns may run in a row with no message
from the user. Every wake is a model call nobody typed, and while sessions
are live the prompt is blocked, so an agent that starts a new session on
every wake would otherwise hold the session and spend tokens indefinitely.
Past the limit, waking stops, the plain prompt unblocks, and the results are
delivered with the user's next message; a user message resets the count.

10 is a ceiling on an unattended loop, not an estimate of a normal one. A
subagent has no user to reset it, so it gets one final wake and returns
(SS20).

### `shell_session_output_head_chars`

### `shell_session_output_tail_chars`

ROADMAP_v3 §49 (SS12). How much of a session's output is kept: the first
10 000 characters and the most recent 190 000, IN MEMORY ONLY. Never on disk,
because program output can hold secrets and redaction is pattern-based, so a
file would be a durable copy of whatever the patterns miss. The dropped
middle is reported as a gap when the agent pages through the output.

The head is what shows how a run STARTED -- the command's own banner, the
first error of a cascade -- which a tail-only buffer loses on exactly the long
runs sessions exist for. The split is uneven because the end of a run is what
is usually asked about.

## Output redaction (#167/#49, batch 20)

### `redact_tool_outputs`

The PERMANENT master switch for pattern-based redaction of what leaves a
tool: vendor token substitution and credential-shape redaction, applied
by check_output_policy() to every tool result and by param_digest() to
what the shells display of an argument. On by default; a user whose
workflow needs the model to see real values (debugging their own
credentials, say) turns it off here, or per-run with
VENASTINE_REDACT_OFF in the environment -- see redaction_enabled().

DELIBERATELY NOT a settings.json key (decision recorded in DEVLOG,
batch 20): project tier beats user tier there, so a cloned repo could
ship `.venastine/settings.json` switching the scrubbing off behind a
trust prompt nobody reads -- the exact shape G7 rejected
shell_approval_mode for and R12 rejected research.granted_tools for.
Permanent means editing this line.

NEVER covers three things, whatever this is set to: check_input_policy's
refusals (a denial is legible, not destructive); check_output_policy's
depth-cap substitution (fail-closed structure bound -- making it optional
would recreate the deterministic bypass its comment forbids); and
logging_setup.py's formatter redaction (the second sink keeps its own
guard).

### `tool_compute_timeout_s`

ROADMAP_v2 §31 (H9). The wall clock on a BUDGET_COMPUTE tool call --
the six math tools, which are pure functions of their params and have
nothing bounding them from the inside. dispatch runs them in a
killable subprocess under this budget (tools/isolation.py).

20s is chosen against measurement, not taste. The slowest LEGITIMATE
call found is `symbolic_math series order=1000` at 3.44s, so this is
roughly 6x headroom; the runaways it exists for do not return at all.
Ten passes each burning a full budget is 200s rather than forever,
which is the trade being made.

*(Corrected in batch 83. This paragraph was carried over saying 15s,
roughly 4x and 150s -- all three consistent with a 15 the value has not
been for some time. `README.md` said 15 in three more places against its
own table's 20. The value is unchanged; the arithmetic now describes it.)*

ONE number, not one per tool: there is no evidence any two math tools
want different answers, and a per-tool budget makes each a judgement
call at the registration site. It sits beside SANDBOX_TIMEOUT_SECONDS
because that is the constant a reader would compare it against.

### `network_allowed_commands`

WARNING: commands matching these words get network access inside the
sandbox. A compromised package or script can exfiltrate data or
download payloads. Only add commands you trust.

### `inert_commands`

WARNING: a word added here is a promise that the program neither writes
nor runs code -- under `tiered` it is the ONLY tier that runs unasked
(§48, CE1), so an entry that can execute its arguments or a file (`find
-exec`, `sort --compress-program`, `tar --to-command`) is an unprompted
code path. An inert command runs in the container when Docker or Podman is up and
on the host when it is not (§46, EP5); one whose argument reaches outside
the workspace is HOST_READ, runs on the host, and always asks. The risk of
the host path is information disclosure, not modification.

### `max_granted_tool_calls`

ROADMAP_v2 §25 (R6). Ceiling on how many PRE-GRANTED tool calls one
research run may make without being asked again. Counts granted calls
only -- web_search, arxiv_search and every math tool are approval-free
and never touch it, so this is not a cap on tool use.

Pre-flight authorization trades a per-call decision for one up-front
decision, and the thing it gives up is the natural bound on how many
times the granted tool runs. Set generously: the ceiling exists to stop
an injected loop spending the whole authorization unattended, not to
ration ordinary work across ten passes.

### `attended_approval_timeout_s`

ROADMAP_v2 §25 (R9). How long an attended research run waits for a human
to answer one approval prompt before DENYING that call and carrying on.

Denial is the safe direction, and continuing is the useful one: walking
away from an attended run degrades it rather than wedging it, so nine
completed passes are not lost to one unanswered question. Generous by
default because the whole point of the mode is that someone is there.

### `subagent_review`

ROADMAP_v2 §20 (D9). Whether a finished research run gets a reviewer pass
over its own output. Off by default: it is one more model call plus a
re-synthesis whenever a correction is accepted, and it needs someone
present to consent to anything it changes.

### `max_review_findings`

§20 (V4). Ceiling on how many findings one review may put to the user.
Consent fatigue is the failure mode a mutating review stage has and a
read-only one does not: the fiftieth prompt gets answered differently
from the first, and an injected "correction" only needs one reflexive
yes. Findings past the cap are dropped and the drop is TRACED -- a
silent truncation would read as "the reviewer found twelve things".

### `max_review_refinements`

§20 (V5). How many times one finding may be sent back for refinement.
A human asks for each round, so this is not a runaway guard -- it bounds
the case where the reviewer keeps producing a proposal that misses the
same point, which is when the honest answer is to reject it.

## Compaction (ROADMAP_v2 §21)

### `compaction_trigger_fraction`

D27: these are DEFAULTS, not settings. Every value is a starting point
chosen to be reasonable and explicitly not claimed to be right, and the
compaction block as a whole is overridable through the existing settings
mechanism -- `config.yaml` default -> user
~/.config/venastine/settings.json -> trusted project
.venastine/settings.json -> a per-invocation `/compact --strength N`.
The architecture is what's locked; the numbers are expected to move once
there are real long threads to look at.

**THIS KEY IS NOT ONE OF THE OVERRIDABLE ONES.** The paragraph above is
D27's about the compaction SECTION, and the seven keys a `settings.json`
`compaction` block actually accepts are `strength`, `keep_recent_tokens`,
`trigger_tokens`, `warning_margin_tokens`, `keep_recent_turns`,
`strategy` and `max_retries` -- `core/config_loader._KNOWN_COMPACTION`.
`trigger_fraction` is not among them and unknown nested keys RAISE, so
writing `compaction: {trigger_fraction: 0.9}` is a startup error rather
than an override. `README.md`'s compaction table lists the seven.

How much of the model's context window a thread may fill before
compaction folds it. thresholds() multiplies this by context_limit(),
so it is 850k on a 1M model and 170k on a 200k one.

BATCH 44 REVERSED M1, AND THE REASON M1 GAVE HAS EXPIRED. M1 rejected a
window-derived trigger because "a window-derived trigger sits at a size
the thread can never reach in working order" -- and the arithmetic
behind that was about MAX_TOKEN_BUDGET, then a default spend ceiling of
250k: the prompt is re-sent on every step of a tool-using turn, so a
thread at 160k got one response and no tool calls before the cap ended
the turn. Batch 27 DELETED that default ceiling. There is no cap for a
large trigger to collide with any more, so the premise is gone and the
flat 40k outlived it -- compacting a 1M-window thread at 4% of its
window, spending a model call and losing fidelity to solve a problem
the model did not have.

THE COST OF THE REVERSAL, STATED. MODEL_CONTEXT_WINDOWS and the provider
query decide when every thread folds now, where under M1 a wrong entry
only mistimed a research backstop. That is why core/model_windows.py
exists (the user's own answer, outranking both) and why the fallback
still WARNS. A fraction rather than `window - margin`: a fixed margin
that is right for a 1M model leaves a 64k model no working room at all,
and one that is right for 64k never fires on 1M.

### `compaction_trigger_tokens`

The trigger used when NO MODEL IS IN SCOPE -- and the default
settings.json's `compaction.trigger_tokens` overrides.

One caller has no (provider, model) to derive a window from:
compaction.pin_measurements, which computes #89's cap as
PIN_MAX_TRIGGER_FRACTION x the trigger from a thread id alone. Left at
40_000 deliberately, so the pin cap is exactly what it was before batch
44 -- this batch changes when a thread compacts, not how much of one a
single pin may freeze, and a cap that grew with the window would let one
`pin` call freeze 425k tokens of a 1M-window thread.

### `compaction_warning_margin_tokens`

Fires this many tokens BEFORE the trigger, so there is room to do
something about it -- pin a message, wrap up a thought, run /compact at a
natural break.

STILL ABSOLUTE after batch 44, and that is a decision rather than an
oversight. What this buys is a HUMAN'S REACTION TIME, measured in turns,
and a turn does not get bigger because the window did: 8k is roughly a
turn or two of notice on a 64k model and on a 1M one alike. As a
fraction of the new trigger it would be 128k on a 1M model -- a warning
that fires while the thread is at 72% and has nothing to worry about
yet. §21 singles this out as the one value whose wrong setting
is not self-correcting: too small and it is an alert with no time
attached to it. Validated to be strictly less than the trigger.

### `compaction_keep_recent_tokens`

The most recent tokens that always stay verbatim, never compacted.

ABSOLUTE, like the warning margin above and for the same kind of reason:
this is an amount of CONVERSATION to keep intact, not a share of a
window. It is also the weaker of the two floors in practice -- M5 keeps
whichever protects more, and the three-turn floor below almost always
protects more than 4k on the long threads that reach the trigger at all.
Left where it was so batch 44 changes when a thread folds and not what
survives the fold.

### `compaction_keep_recent_turns`

M5: a FLOOR in turns, on top of the token floor above, and the two
compose -- whichever protects more wins, plus the current turn, which is
never foldable at all. A single tool-heavy turn can consume the whole
keep-token budget by itself, leaving the immediately preceding exchange
summarized; a follow-up like "no, the other one" then has no referent.

### `pin_max_trigger_fraction`

#89 (batch 16). The most one `pin` call may protect, as a fraction of
the trigger above. A pin is a PERMANENT FLOOR under the derived view:
pinned rows re-enter it verbatim forever (M9), nothing folds them, and
before this cap one ungated call with last_n=40 could put a thread
permanently past the trigger with no unpin to undo it. 0.5 leaves room
for a real working session while making "floor the trigger by yourself"
structurally impossible. Enforced at the tool with a REFUSAL that states
the cap and the current share -- never silently trimmed (M15's rule: a
pin asked for more protection than allowed must not quietly deliver less).

### `compaction_strength`

1-5, mapping to the target compression ratios below.

### `compaction_max_retries`

Bounded corrective retries when the compactor misses its target ratio.

### `compaction_target_ratios`

§21: strength is an objectively measurable target, not a qualitative
instruction. "Be more aggressive" behaves differently across models with
different summarization tendencies and no amount of prompting fixes that;
a ratio is checkable after the fact. Qualitative guidance still belongs
in the compactor's prompt, shaping WHAT survives within the budget.

### `compaction_ratio_tolerance`

How far outside the target a summary may land before it is sent back.
Under-shooting is not an error: a summary tighter than asked for has
already done the job, and rejecting it would spend a second call to make
the context bigger.

### `compaction_strategy`

M2 as amended by batch 16 (#90, owner decision). Two strategies:

```text
  "chain"     summarizes the PREVIOUS SUMMARY plus whatever followed it.
              Constant cost per compaction forever -- the input never
              grows past summary-plus-tail -- at the price of loss that
              compounds over a long-lived thread (a summary of a summary
              of a ...).
  "rederive"  summarizes the ORIGINAL messages every time, so exactly
              one summarization step always sits between an original
              message and what the model sees. Fidelity-optimal; input
              grows with the covered span.
```

The DEFAULT is "chain", reversing M2's original choice -- an explicit
owner decision recorded in DEVLOG (batch 16): rederive's whole-span
input made every compaction the most expensive call of the turn, and
cost compounds on exactly the threads that trigger most. Whatever is
configured here, BOTH strategies fall back to chain automatically when
a span outgrows one call (built at last, after three documents promised
it for two sections), saying so in a WARNING -- and truncate the oldest
material with a stated truncation when even chain cannot fit, because
an oversized send is never the right failure direction.

### `compaction_strategies`

### `max_injected_memories`

ROADMAP_v2 §21b (M14). Ceiling on how many durable memories reach one
prompt. Newest first, and the truncation is stated in the fragment the
model reads rather than only in a log.

Every in-scope memory would otherwise enter every turn forever, which is
the unbounded context growth §21 exists to fight -- a memory feature that
quietly reintroduces the problem compaction solves would be a poor trade.
Recency is the ordering because it is the only staleness signal available
without asking a model, and /forget is how a still-relevant old memory
gets kept ahead of the cap.

## Catalog text (ROADMAP_v2 §32, A5)

### `max_catalog_text_chars`

The cap on a skill's or an agent's `name` and `description` -- the
two strings that go from a .md file straight into the system prompt
of every run in a project, with no tool call and no further consent
(#131).

300 is headroom rather than a squeeze: the longest description this
project ships is `compactor` at 171 characters, and all eight are
single-line. A project whose description does not fit in twice the
length of ours is writing instructions, not a summary, which is
exactly the thing the cap is for.

THE NEWLINE COLLAPSE MATTERS MORE THAN THE NUMBER, and they are one
constant because they are one rule -- see config_loader._catalog_text.
Same posture as tools/builtin/arxiv.MAX_SUMMARY_CHARS (600) and
web_search.MAX_SNIPPET_CHARS (300): model-facing text from a source
this project did not write is bounded where it is produced.

## Summaries, references and the checklist (ROADMAP_v2 §21c / §23)

### `compactor_agent`

The agent that does the summarizing. §21: this is the shape of an agent
call -- a system prompt, a task, a judgment-based output -- so it runs
through the same RunAgentLoop as everything else rather than needing its
own LLM-calling path, and that is also what gives it real autonomy over
WHAT to preserve. Mechanical truncation cannot exercise judgment at all.

### `summary_target_chars`

ROADMAP_v2 §21c. How long a whole-thread summary may be, in characters.

ABSOLUTE, not a COMPACTION_TARGET_RATIOS entry, and the difference is the
consumer rather than the summarizer. A fold's summary REPLACES the span it
came from, so scaling with that span is right: a bigger fold earns a bigger
summary and the thread still shrinks. §21c's summary is injected as a prompt
tier present on every turn of the REFERENCING thread, where nothing it
replaces bounds it -- a 500KB thread at strength 3 would put 75KB into every
call indefinitely.

A thread whose rendered text already fits this is stored verbatim and costs
no model call at all.

### `max_injected_refs`

§21c. How many referenced threads may be attached to one thread at once.

Small on purpose, and the cap REFUSES rather than dropping the oldest: a
reference is something a person chose by name, and silently discarding one
to make room for another is exactly what §21b's "removal is by id, never by
substring" rule exists to prevent. The count and the cap are stated in the
fragment the model reads, per M14's no-silent-caps rule.

### `todo_statuses`

§23 slice 2. The todo list's vocabulary and its ceiling.

A plain tuple rather than an Enum, for storage.py's THREAD_KIND_* reason: a
list persisted under an older vocabulary reads back as data rather than
raising, and the tool can then say which status it did not recognise.

### `max_todo_items`

The list is injected into every turn's system prompt, so its length is a
per-call cost for as long as the thread lives. REFUSES rather than
truncating, MAX_INJECTED_REFS' rule: a checklist silently cut to 50 would
have the model believe it had recorded work it has now forgotten, which is
worse than being told to write a shorter list.

### `compaction_pipeline_backstop_tokens`

M6. A research pass is headless and unattended, and each one already
returns a distillation, so routine compaction there would spend on a
judgment call nobody is watching. Passes compact only when approaching
the model's actual context window -- a safety net against a hard provider
error mid-pipeline, not a working-set policy.

## Context windows (ROADMAP_v2 §21 M1, as amended by batches 30 and 44)

### `model_context_windows`

The backstop's source, the summarizer's input budget, and M1's remaining
use for a window table.

NOW A FALLBACK, NOT THE ONLY SOURCE. This said "deliberately NOT a
per-provider API query", on the grounds that the roster of fifteen had no
uniform endpoint and a query would need fifteen adapters to avoid a
fallback constant they would all still need. The premise was measured
against the PINNED SDKs and did not survive:

```text
  anthropic 0.116.0    ModelInfo.max_input_tokens -- on the SAME object
                       core/client.py already retrieves to read
                       capabilities.effort. The answer was in hand and
                       being dropped; reading it costs no extra call.
  google-genai 1.0.0   types.Model.input_token_limit, one field.
  openai 2.45.0        Model is declared extra="allow", so a provider's
                       own field survives onto .model_extra. That makes
                       the OpenAI-compatible side ONE alias reader --
                       context_window (Groq), context_length (Together,
                       OpenRouter, Cohere), max_context_length (Mistral)
                       -- rather than thirteen adapters.
```

So it is two attribute reads and one sniff, not fifteen adapters. The
query lives in core/client.py:context_window_for, beside the effort query
it mirrors; core/compaction.py:context_limit prefers it and falls back
here. (The old note also corrected itself on a provider count from the
dead APICredentials class -- audit #23. Fifteen is right; the count was
never what was wrong with the argument.)

THE CONCLUSION OUTLIVED THE PREMISE, which is why this table stays: OpenAI,
DeepSeek and Perplexity answer /models with id/created/object/owned_by and
nothing else, and Grok, Fireworks, Qwen and Z.AI publish no model-metadata
endpoint at all. A fallback constant is still needed at the end of it.

KEYS ARE STORED NORMALIZED -- no date suffix, no `vendor/` or `region.`
prefix. core/compaction.py:_normalized reduces an incoming id to this form
before looking it up, so a dated key here would be UNREACHABLE. That is
also the bug this fixed: matching was exact, so `claude-sonnet-5-20260724`
missed a million-token entry and silently took the default.

AN INCOMPLETE ENTRY IS EXPENSIVE NOW. Under M1 it cost a mistimed
research backstop and a mistimed summarizer input budget, because the
working-set trigger was a flat number that never consulted this. Batch 44
made that trigger a FRACTION of what context_limit() returns, so a wrong
number here mistimes compaction on every thread -- too high and the
thread never folds and the provider raises a hard context-limit error
instead. core/model_windows.py is the user's answer to that, and /window
is how they give it; this table is what answers before they do.

**Notes on individual entries.**

1M IS THE ORDINARY WINDOW ON THESE, not a beta.

This block used to carry the opposite claim -- that 1M was gated
behind a `context-1m-2025-08-07` header this harness does not send,
so an ordinary account got 200_000 and these entries "want to be
200_000". That was true of the Sonnet 4 era and was never true of
the models listed here; checked against the current model reference
in batch 44, the whole 4.6-and-later family is 1M and Haiku 4.5 is
200K. The values were right and the comment was stale, which is the
dangerous combination now that the trigger reads them: a reader
following the old note would have "corrected" five entries by a
factor of five and compacted every Claude thread at a fifth of the
window.
Dateless, per the normalization note above: this was keyed
"claude-haiku-4-5-20251001" while its five siblings were dateless,
which is the inconsistency that exposed the exact-match bug.
Alibaba markets "1M"; the documented input limit is 983,616, and
drops to that figure specifically when thinking is enabled. The
SMALLER number is the right one to record: this feeds the pipeline
compaction backstop (context_limit - COMPACTION_PIPELINE_BACKSTOP_
TOKENS), where erring low compacts slightly early and erring high
means a hard context-limit error from the provider mid-run.

### `default_context_window`

Logged at WARNING ONCE PER MODEL, per §21 -- a wrong-by-default window
means compaction and the backstop both fire at the wrong time in
whichever direction the guess is wrong, and silent guessing turns a
tuning problem into a mystery. Once per model rather than once per use,
because context_limit() is on should_compact()'s path and that runs at
the top of every step of every turn; the dedup set lives in
core/compaction.py. A long session therefore says this once and is then
silent, which is the trade -- the line is actionable exactly once, and
since batch 44 it names the action (/window, which remembers).

256_000 rather than 200_000 (batch 44): the floor a hosted agentic model
ships with today. Below that is essentially always a self-hosted
deployment, whose operator knows the number and can say it once with
/window -- which is the case this default cannot serve and does not try
to. Erring low here would compact every unknown model early forever to
protect the rarer case that has an owner who can answer.

## Tool permissions and approvals (ROADMAP_v2 §15, D24)

### `tool_permissions`

APICredentials was here and is deleted (audit #23). It was dead -- nothing
constructed it, imported it or type-hinted against it -- and its guidance
contradicted the mechanism that is real: it told the reader to put the key
in an environment variable and write the VARIABLE'S NAME here, while
credentials.save_credentials stores the key value directly in
providers.json. Anyone following it ended up with the literal string
"MY_KEY_VAR" as their API key.

Credentials flow through credentials.py (providers.json) and env_secrets.py
(.env) -- the deliberate two-mechanism split D19 describes, which
env_secrets.py documents at length on the other side. The provider roster
lives in providers.json.example, beside the file it describes.

#### `tool_permissions.fetch_url`  -- ships `true`

ROADMAP_v2 §15/D24: fetch_url was registered and documented as working
but had no field here, so getattr(..., False) denied every call since
the tool was added. Allowed by default, matching web_search -- it is
already constrained by policy_enforcement's blocked-domain list and
its output goes through secret redaction like every other tool's.

#### `tool_permissions.spawn_subagent`  -- ships `true`

§18: spawning is allowed by default (model autonomy, D6) and needs no
approval -- the spawned run's own tools are each gated by policy,
intersection-capped by the parent's context (C6).

#### `tool_permissions.pin`  -- ships `true`

§21/D26. Thread-scoped, reversible (batch 16 gave that word a
mechanism: `unpin`), and it takes effect inside a conversation the
user is watching -- a wrong pin costs some context budget and nothing
else.

#### `tool_permissions.unpin`  -- ships `true`

§21/D26, restored by batch 16 (#89). D26's premise called a pin
reversible while nothing anywhere unpinned one; unpin is the other
half of making the premise true rather than aspirational. Same axis,
same gating posture, same reasons.

#### `tool_permissions.remember`  -- ships `true`

§21b. Allowed everywhere; whether it can actually RUN is decided
by the approval gate below plus §13's headless rule.

#### `tool_permissions.read_project_doc`  -- ships `true`

§24 (I2). Allowed, unlike `read`, because it is confined to the
project directory by realpath and to documentation/manifest files by
an allowlist -- see tools/builtin/project_docs.py for what that
deliberately excludes and why the narrow set is load-bearing.

#### `tool_permissions.write_project_doc`  -- ships `true`

§24 (I1). Allowed, unlike `write`, because it takes a document NAME
from a fixed allowlist rather than a path: the destination is derived,
so the tool cannot be aimed anywhere else. The gate is below.

#### `tool_permissions.ask_user`  -- ships `true`

§23 slice 2. Allowed everywhere, and ungated below (J12): whether it
can actually reach a person is decided by whether the run has a
response channel, which the tool itself checks.

#### `tool_permissions.todo_write`  -- ships `true`

§23 slice 2. Thread-scoped, reversible, and it asks nobody -- so it is
allowed and ungated for `pin`'s reasons (J9).

### `tool_approvals`

#### `tool_approvals.fetch_url`  -- ships `false`

No approval: a tool with approval=True is unusable wherever there is
no permission_channel to answer the prompt (the CLI chat loop and
every research pass), so gating fetch_url would leave it denied in
exactly the places the grounding passes need it -- the same outcome
as the D24 bug, with a different error string. See DEVLOG §15.

#### `tool_approvals.shell`  -- removed, not shipped

There is deliberately no such key. Shell approval is governed solely by
`shell_approval_mode` above: the field this section used to describe was
the RATCHET (`True` forced `always` whatever the mode said), and every
outcome it could produce is an outcome the mode already names -- so a
second switch could only ever agree with the gate or surprise someone who
set the two differently. An agent's `approval_overrides` keeps its own
one-way power over shell through the context layer (D14); what is gone is
only the global duplicate. An old file still carrying the key is refused
at startup naming it (unknown keys are refused, always), and the npm-update
merge drops it as "not a setting in this version" while keeping every
other edit.

#### `tool_approvals.spawn_subagent`  -- ships `true`

Approving a spawn is the §18 subagent sign-off: it authorises the
child's whole approval-gated tool set for the rest of the turn, so
delegation itself has to be the thing approved. Consequence, named
rather than discovered: the headless callability filter drops
spawn_subagent anywhere nothing can grant the approval -- CLI chat,
and any research run that is neither attended nor granting. The
once-per-run notice names it.

§25 R4 declares it GRANT_NEVER (R13, on the ToolSpec): it can never
be PRE-granted, in a research run OR at §18's sign-off, because
approving a spawn hands the child a whole gated set and one
launch-time tick would compound into unbounded delegated authority
across ten unattended passes. #67 is why "or at the sign-off" is in
that sentence: R4's argument is ABOUT the sign-off, and the
exclusion had never reached it. An ATTENDED run can still approve a
spawn live,
which is a per-call human decision and exactly what the gate is for.
The child then runs headless with nothing granted, since
spawn_subagent forwards a grant only alongside a permission_channel.

#### `tool_approvals.pin`  -- ships `false`

§21/D26: pin does NOT require approval, and the asymmetry with
remember (§21b) is the decision. They differ on the axis that already
separates `read` from `write` -- pin is thread-scoped and reversible,
while remember writes something that outlives the thread and silently
shapes conversations the user has not started yet.

Consequence, named rather than discovered: this is what lets pin work
on the CLI and inside a research pass, where an approval-gated tool
is not merely denied but not advertised at all (§13).

#### `tool_approvals.unpin`  -- ships `false`

§21/D26, batch 16 (#89). Ungated for pin's reasons, and now genuinely
symmetric: releasing protection costs context budget in a
conversation the user is watching, exactly like applying it.

#### `tool_approvals.remember`  -- ships `true`

§21b/D26, and the asymmetry with pin above IS the decision. A
memory outlives its thread and silently shapes conversations the
user has not started yet, so it is persistent and invisible at the
moment it matters -- the same axis that separates write from read.

Consequence: unreachable on any headless path, which includes every
research pass. That is deliberate (§21's consequence 1) and is
reinforced by the tool's GRANT_NEVER policy (M17, carried by R13),
because a grant would otherwise route around it.

#### `tool_approvals.read_project_doc`  -- ships `false`

§24 (I2): no approval. Reading the project's own documentation, at the
user's explicit request, on the files the manifest already listed to
them, is not an escalation -- and a gate here would mean a dozen
prompts for one /init, which is the shape of consent that gets clicked
through. The confinement in project_docs.py is the control.

#### `tool_approvals.write_project_doc`  -- ships `true`

§24 (I1): gated, on the same axis that separates `read` from `write`.
This writes a file into the user's project that is then injected into
future prompts, and it invalidates the D17 trust hash as it goes.
/init supplies the approval itself, as the yes to a rendered diff --
so the gate is what makes "no silent overwrite" (§24 AC2) structural
rather than a promise the command makes about itself.

#### `tool_approvals.ask_user`  -- ships `false`

§23 slice 2 (J12): UNGATED. Two reasons, and the second is the one
that decides it. Gating would mean approving a prompt in order to be
shown a prompt -- but worse, §13 does not merely deny a gated tool
where nothing can ask, it stops ADVERTISING it. Gated, this tool would
be invisible in every headless run; §24 AC2 requires it be visible,
called, and answered with a denial the model can work around. Asking
a person is not an action that needs authorising -- it is the least
unilateral thing a run can do.

#### `tool_approvals.todo_write`  -- ships `false`

§23 slice 2 (J9): UNGATED, on the axis that already separates `pin`
from `remember`. A todo list is thread-scoped and reversible, and
rewriting it costs some prompt budget inside a conversation the user
can see. Gating it would also make it invisible in every research pass
(§13), which is where a checklist across ten passes helps most.
