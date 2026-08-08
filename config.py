import os
from dataclasses import dataclass

# --- Model / loop settings -- these were missing; call_model, RunAgentLoop,
# and database.py all depend on them ---
MODEL_NAME = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
# Per-call output ceiling. Raised from 4096 in ROADMAP_v2 §16: on current
# Anthropic models max_tokens caps THINKING PLUS RESPONSE TEXT together, so
# 4096 truncated answers mid-sentence as soon as reasoning effort was
# enabled. Interacts with MAX_TOKEN_BUDGET below, which is the cumulative
# governor across a whole _run() -- at 16k per call roughly six calls fit in
# the default budget. Raise both together if you run at xhigh/max effort,
# where the provider guidance is 64k+ per call.
MAX_TOKENS = 16_000
# --- Loop control ---
MAX_ITERATIONS = 20  # matches the max_steps default used elsewhere

# --- Subagents (ROADMAP_v2 §18) ---
# Maximum spawn_subagent nesting. The counter lives on
# ToolContext.subagent_depth; this is the value it is checked against (C3).
SUBAGENT_MAX_DEPTH = 2

# --- Deep research pipeline ---
MAX_PIPELINE_RETRIES = 2  # max revise/re-validate loop iterations per claim before fallback
MAX_JSON_RETRIES = 2  # max corrective follow-up attempts when a pass returns malformed JSON
                      # (total attempts per pass = MAX_JSON_RETRIES + 1). See ROADMAP §3.

# --- Cumulative token budget ---
# New, separate from MAX_TOKENS (which caps a single call's output).
# This caps TOTAL tokens (input+output, summed across every call) spent
# within one RunAgentLoop._run() invocation -- applies to both regular
# conversation and every research pass.
#
# IT IS A SPEND METER, NOT A CONTEXT LIMIT, and the difference is
# load-bearing (ROADMAP_v2 §21, M1). The prompt is resent on every step of
# a tool-using turn, so the provider bills it again and this counter adds
# it again. That is correct as billing and quadratic as anything else: at
# ~2k of tool result per step, a 20k-token thread gets about 9 steps, a
# 50k thread about 2, and a 100k thread exactly one response with no tool
# calls at all.
#
# Raised from 100_000 at §21. The old value was labelled a placeholder by
# its own comment, and it was small enough that COMPACTION_TRIGGER_TOKENS
# could not be reached: for compaction at threshold T to fire and still
# leave k usable steps, this needs to be roughly k*T. At 250k a compacted
# ~40k thread gets its full run of steps, and a genuine runaway still
# stops well short of anything ruinous.
#
# core/config_loader.py WARNS if a configured trigger is too close to this
# to leave room for a multi-step turn -- the relationship is validated
# rather than left in a comment.
MAX_TOKEN_BUDGET = 250_000

# The same meter, for ONE research pass. Separate from the chat budget
# because the two are used differently and the meter is quadratic: it
# re-counts the entire prompt on every step (TECHNICAL_DEBT.md item 9),
# so a pass that makes a dozen tool calls with large results burns the
# ceiling far faster than its actual context growth suggests.
#
# This was found the hard way. A Pass 1 that made 14 tool calls -- mostly
# fetch_url against URLs the model guessed and got 404s for -- crossed
# 250k and returned a TOOL-CALLING response with empty text, because a
# budget stop returns whatever the last response held. The orchestrator
# stored "" as raw_response, Pass 2 correctly reported that it had been
# given nothing to extract claims from, and the run died three passes
# later with a TypeError about Claim's constructor. Raising the ceiling
# is the immediate fix; _run_pass now also refuses to carry an empty
# truncated pass forward, which is the durable one.
#
# NOT a fix for item 9 itself. That entry asks for the billing meter and
# a per-turn size figure to be separated, in a change of its own with its
# own revert checks; this only stops a legitimate pass being cut off by a
# number that was never meant to bound it.
RESEARCH_PASS_TOKEN_BUDGET = 1_000_000

# Deferred for now (core sequential pipeline only, per current scope):
#   (none remaining -- ensemble_mode/ensemble_n built in ROADMAP §10,
#    critic_model built in ROADMAP §11)

# --- Ensemble mode (ROADMAP §10) ---
# Run Pass 1 N times at higher temperature for diversity, then extract
# the union of claims across candidates with a cross-candidate consistency
# score feeding Pass 4's formula. Off by default.
#
# WARNING: the temperature-based diversity mechanism below does not work on
# current Anthropic models (see MODELS_REJECTING_SAMPLING_PARAMS). The
# orchestrator refuses to run ensemble mode on such a model rather than
# spending ensemble_n x the tokens on N identical candidates. Redesigning
# the diversity mechanism is a deferred §10 revisit.
ENSEMBLE_MODE = False
ENSEMBLE_N = 3
ENSEMBLE_TEMPERATURE = 1.0

# --- Sampling-parameter support (ROADMAP_v2 §16 prerequisite) ---
# Models that reject temperature/top_p/top_k. Current Anthropic models
# removed these parameters outright (any value returns HTTP 400); Sonnet 5
# rejects non-default values. Steering is expected to happen through
# prompting and MODEL_EFFORT_LEVELS instead.
#
# A static table rather than a capability query: the Models API capability
# tree does not report sampling support, so unlike effort levels (which ARE
# queryable, see MODEL_EFFORT_LEVELS) there is nothing to ask. Same posture
# as §21's MODEL_CONTEXT_WINDOWS -- honest about being incomplete, and
# incompleteness is safe here because the failure is a loud 400, not a
# silent wrong answer.
MODELS_REJECTING_SAMPLING_PARAMS = frozenset({
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
})

# --- Critic-model routing (ROADMAP §11) ---
# Route the critic/grounding passes (3a, 3b, 6c) to a different model than
# the generator, so a model isn't checking its own output for errors.
# None means every pass uses the same provider/model — no special routing.
# Example to enable: {"provider_name": "OPENAI", "model": "gpt-5.1"}
CRITIC_MODEL: dict | None = None

# --- Reasoning effort (ROADMAP_v2 §16) ---
# The default effort level requested when the user has not chosen one.
# None means "send nothing" -- the provider's own default applies.
DEFAULT_EFFORT: str | None = None

# Fallback effort levels for providers whose APIs expose no capability
# endpoint (every OpenAI-compatible provider, and Google). ANTHROPIC is NOT
# listed here on purpose: its Models API reports per-model effort support,
# so client.py queries it and new Anthropic models need no entry. Consulted
# only on the fallback path, which logs at WARNING -- same posture as §21's
# MODEL_CONTEXT_WINDOWS.
MODEL_EFFORT_LEVELS: dict[str, list[str]] = {}
DEFAULT_EFFORT_LEVELS = ["low", "medium", "high"]

# Google expresses reasoning depth as an integer token budget rather than an
# enum, so a level has to be mapped to a number at the boundary. -1 is the
# SDK's "decide dynamically" sentinel.
GOOGLE_THINKING_BUDGETS = {
    "low": 2_048,
    "medium": 8_192,
    "high": 16_384,
    "xhigh": 24_576,
    "max": -1,
}

# --- Database ---
DB_PATH = os.environ.get("APP_DB_PATH", "app.db")

# --- Output artifacts ---
OUTPUT_DIR = os.environ.get("AGENT_OUTPUT_DIR", "./output")

# --- File-ops workspace (ROADMAP §6) ---
WORKSPACE_DIR = os.environ.get("AGENT_WORKSPACE", "./workspace")
MAX_FILE_SIZE_BYTES = 10_000_000   # 10 MB — hard reject before opening
MAX_READ_LINES = 500               # max lines per read call
MAX_READ_CHARS = 50_000            # max chars per read call

# --- /init (ROADMAP_v2 §24) ---
INITIALIZER_AGENT = "initializer"

# Three bounds, each doing a different job (I7). /init is a tool-heavy loop
# over documentation that can run to hundreds of kilobytes -- this repo's own
# root markdown is 721KB, with DEVLOG.md alone at 226KB.
#
# INIT_READ_CHARS is well below MAX_READ_CHARS because of TECHNICAL_DEBT item
# 9: the budget meter re-counts the WHOLE prompt on every step, so each 50KB
# read is re-billed for every step that follows it. 20KB keeps a dozen reads
# affordable and still returns a useful span of a document per call.
INIT_READ_CHARS = 20_000
# And the budget itself, for the same reason RESEARCH_PASS_TOKEN_BUDGET
# exists (§26): a tool-heavy run hits the chat ceiling long before its
# context is a problem. MAX_TOKEN_BUDGET is a spend meter, not a context
# limit. Item 9's real fix -- counting incrementally -- is still open, and
# would retire this constant along with the research one.
INIT_TOKEN_BUDGET = 1_000_000
# The number of reads, which the other two do not bound: a model can spend
# an unbounded number of small reads inside a large budget.
INIT_MAX_STEPS = 12

# --- Shell / sandbox (ROADMAP §7) ---
SHELL_BINARY = os.environ.get("AGENT_SHELL", "")  # auto-detect if empty
ALLOW_INSECURE_SANDBOX_FALLBACK = False  # explicitly enable subprocess fallback
AUTO_APPROVE_SANDBOX_FALLBACK = False    # auto-approve fallback runs (no per-run prompt)
SANDBOX_DOCKER_IMAGE = os.environ.get("AGENT_SANDBOX_IMAGE", "python:3.13-slim")
SANDBOX_TIMEOUT_SECONDS = 60
SANDBOX_MEMORY_MB = 1024
SANDBOX_CPU_SECONDS = 30
SANDBOX_MAX_PIDS = 200

# WARNING: commands matching these words get network access inside the
# sandbox. A compromised package or script can exfiltrate data or
# download payloads. Only add commands you trust.
NETWORK_ALLOWED_COMMANDS = [
    "pip", "pip3", "curl", "wget", "git",
    "npm", "yarn", "pnpm", "apt", "apt-get",
    "yum", "dnf", "brew", "cargo",
]

# WARNING: inert commands run WITHOUT filesystem isolation — they can
# read files outside the workspace (e.g. cat /etc/passwd). They are
# read-only inspection commands; the risk is information disclosure,
# not modification.
INERT_COMMANDS = [
    "ls", "cat", "pwd", "echo", "grep", "wc",
    "head", "tail", "date", "whoami", "uname", "which",
    "where", "type", "file", "stat", "du", "df", "tree",
    "uniq", "diff", "md5sum", "sha256sum",
]


# ROADMAP_v2 §25 (R6). Ceiling on how many PRE-GRANTED tool calls one
# research run may make without being asked again. Counts granted calls
# only -- web_search, arxiv_search and every math tool are approval-free
# and never touch it, so this is not a cap on tool use.
#
# Pre-flight authorization trades a per-call decision for one up-front
# decision, and the thing it gives up is the natural bound on how many
# times the granted tool runs. Set generously: the ceiling exists to stop
# an injected loop spending the whole authorization unattended, not to
# ration ordinary work across ten passes.
MAX_GRANTED_TOOL_CALLS = 150

# ROADMAP_v2 §25 (R9). How long an attended research run waits for a human
# to answer one approval prompt before DENYING that call and carrying on.
#
# Denial is the safe direction, and continuing is the useful one: walking
# away from an attended run degrades it rather than wedging it, so nine
# completed passes are not lost to one unanswered question. Generous by
# default because the whole point of the mode is that someone is there.
ATTENDED_APPROVAL_TIMEOUT_S = 600


# ROADMAP_v2 §20 (D9). Whether a finished research run gets a reviewer pass
# over its own output. Off by default: it is one more model call plus a
# re-synthesis whenever a correction is accepted, and it needs someone
# present to consent to anything it changes.
SUBAGENT_REVIEW = False

# §20 (V4). Ceiling on how many findings one review may put to the user.
# Consent fatigue is the failure mode a mutating review stage has and a
# read-only one does not: the fiftieth prompt gets answered differently
# from the first, and an injected "correction" only needs one reflexive
# yes. Findings past the cap are dropped and the drop is TRACED -- a
# silent truncation would read as "the reviewer found twelve things".
MAX_REVIEW_FINDINGS = 25

# §20 (V5). How many times one finding may be sent back for refinement.
# A human asks for each round, so this is not a runaway guard -- it bounds
# the case where the reviewer keeps producing a proposal that misses the
# same point, which is when the honest answer is to reject it.
MAX_REVIEW_REFINEMENTS = 3


# ---------------------------------------------------------------------------
# --- Compaction (ROADMAP_v2 §21) -------------------------------------------
# ---------------------------------------------------------------------------
#
# D27: these are DEFAULTS, not settings. Every value is a starting point
# chosen to be reasonable and explicitly not claimed to be right, and every
# one is overridable through the existing settings mechanism -- config.py
# default -> user ~/.config/venastine/settings.json -> trusted project
# .venastine/settings.json -> a per-invocation `/compact --strength N`.
# The architecture is what's locked; the numbers are expected to move once
# there are real long threads to look at.

# The working-set size compaction maintains. Compaction fires when the last
# measured input_tokens reaches this.
#
# M1: NOT `context_limit - buffer`. §21 parameterized the trigger against
# the model's context window on the assumption that the window is what a
# long thread hits first. On this codebase it is not -- MAX_TOKEN_BUDGET
# above makes a turn unusable at well under half of any modern window, so a
# window-derived trigger sits at a size the thread can never reach in
# working order. Compaction defends per-turn cost and step headroom; the
# window is a backstop below.
#
# This also makes MODEL_CONTEXT_WINDOWS much less dangerous to get wrong,
# which was a risk §21 flagged about its own design.
COMPACTION_TRIGGER_TOKENS = 40_000

# Fires this many tokens BEFORE the trigger, so there is room to do
# something about it -- pin a message, wrap up a thought, run /compact at a
# natural break. §21 singles this out as the one value whose wrong setting
# is not self-correcting: too small and it is an alert with no time
# attached to it. Validated to be strictly less than the trigger.
COMPACTION_WARNING_MARGIN_TOKENS = 8_000

# The most recent tokens that always stay verbatim, never compacted.
COMPACTION_KEEP_RECENT_TOKENS = 4_000

# M5: a FLOOR in turns, on top of the token floor above, and the two
# compose -- whichever protects more wins, plus the current turn, which is
# never foldable at all. A single tool-heavy turn can consume the whole
# keep-token budget by itself, leaving the immediately preceding exchange
# summarized; a follow-up like "no, the other one" then has no referent.
COMPACTION_KEEP_RECENT_TURNS = 3

# 1-5, mapping to the target compression ratios below.
COMPACTION_STRENGTH = 3

# Bounded corrective retries when the compactor misses its target ratio.
COMPACTION_MAX_RETRIES = 2

# §21: strength is an objectively measurable target, not a qualitative
# instruction. "Be more aggressive" behaves differently across models with
# different summarization tendencies and no amount of prompting fixes that;
# a ratio is checkable after the fact. Qualitative guidance still belongs
# in the compactor's prompt, shaping WHAT survives within the budget.
COMPACTION_TARGET_RATIOS = {1: 0.40, 2: 0.25, 3: 0.15, 4: 0.10, 5: 0.05}

# How far outside the target a summary may land before it is sent back.
# Under-shooting is not an error: a summary tighter than asked for has
# already done the job, and rejecting it would spend a second call to make
# the context bigger.
COMPACTION_RATIO_TOLERANCE = 0.5

# M2. "rederive" summarizes the ORIGINAL messages every time, so exactly
# one summarization step always sits between an original message and what
# the model sees -- which is what makes an early trigger free in fidelity
# terms rather than a tradeoff. "chain" summarizes the previous summary
# plus what followed it: constant cost per compaction forever, at the price
# of loss that compounds over a long-lived thread.
#
# rederive falls back to chain on its own when the span outgrows a single
# call, and says so. Setting "chain" here forces it always.
COMPACTION_STRATEGY = "rederive"
COMPACTION_STRATEGIES = ("rederive", "chain")

# ROADMAP_v2 §21b (M14). Ceiling on how many durable memories reach one
# prompt. Newest first, and the truncation is stated in the fragment the
# model reads rather than only in a log.
#
# Every in-scope memory would otherwise enter every turn forever, which is
# the unbounded context growth §21 exists to fight -- a memory feature that
# quietly reintroduces the problem compaction solves would be a poor trade.
# Recency is the ordering because it is the only staleness signal available
# without asking a model, and /forget is how a still-relevant old memory
# gets kept ahead of the cap.
MAX_INJECTED_MEMORIES = 50

# The agent that does the summarizing. §21: this is the shape of an agent
# call -- a system prompt, a task, a judgment-based output -- so it runs
# through the same RunAgentLoop as everything else rather than needing its
# own LLM-calling path, and that is also what gives it real autonomy over
# WHAT to preserve. Mechanical truncation cannot exercise judgment at all.
COMPACTOR_AGENT = "compactor"

# ROADMAP_v2 §21c. How long a whole-thread summary may be, in characters.
#
# ABSOLUTE, not a COMPACTION_TARGET_RATIOS entry, and the difference is the
# consumer rather than the summarizer. A fold's summary REPLACES the span it
# came from, so scaling with that span is right: a bigger fold earns a bigger
# summary and the thread still shrinks. §21c's summary is injected as a prompt
# tier present on every turn of the REFERENCING thread, where nothing it
# replaces bounds it -- a 500KB thread at strength 3 would put 75KB into every
# call indefinitely.
#
# A thread whose rendered text already fits this is stored verbatim and costs
# no model call at all.
SUMMARY_TARGET_CHARS = 2_000

# §21c. How many referenced threads may be attached to one thread at once.
#
# Small on purpose, and the cap REFUSES rather than dropping the oldest: a
# reference is something a person chose by name, and silently discarding one
# to make room for another is exactly what §21b's "removal is by id, never by
# substring" rule exists to prevent. The count and the cap are stated in the
# fragment the model reads, per M14's no-silent-caps rule.
MAX_INJECTED_REFS = 3

# §23 slice 2. The todo list's vocabulary and its ceiling.
#
# A plain tuple rather than an Enum, for storage.py's THREAD_KIND_* reason: a
# list persisted under an older vocabulary reads back as data rather than
# raising, and the tool can then say which status it did not recognise.
TODO_STATUSES = ("pending", "in_progress", "completed")

# The list is injected into every turn's system prompt, so its length is a
# per-call cost for as long as the thread lives. REFUSES rather than
# truncating, MAX_INJECTED_REFS' rule: a checklist silently cut to 50 would
# have the model believe it had recorded work it has now forgotten, which is
# worse than being told to write a shorter list.
MAX_TODO_ITEMS = 50

# M6. A research pass is headless and unattended, and each one already
# returns a distillation, so routine compaction there would spend on a
# judgment call nobody is watching. Passes compact only when approaching
# the model's actual context window -- a safety net against a hard provider
# error mid-pipeline, not a working-set policy.
COMPACTION_PIPELINE_BACKSTOP_TOKENS = 20_000

# The backstop's source, and M1's remaining use for a window table.
#
# Deliberately NOT a per-provider API query: APICredentials lists thirteen
# providers, most reaching the same OpenAI-compatible path, and there is no
# uniform endpoint exposing context length across them -- thirteen adapters
# to avoid a fallback constant they would all still need. A static table is
# honest about being incomplete, and under M1 an incomplete entry costs
# much less than it would have: the working-set trigger does not consult
# this at all.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-5": 200_000,
    "claude-sonnet-5": 200_000,
    "claude-fable-5": 200_000,
    "claude-opus-4-8": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "gpt-5.1": 400_000,
    "gemini-2.5-pro": 1_000_000,
    # Alibaba markets "1M"; the documented input limit is 983,616, and
    # drops to that figure specifically when thinking is enabled. The
    # SMALLER number is the right one to record: this feeds the pipeline
    # compaction backstop (context_limit - COMPACTION_PIPELINE_BACKSTOP_
    # TOKENS), where erring low compacts slightly early and erring high
    # means a hard context-limit error from the provider mid-run.
    "qwen3.8-max-preview": 983_616,
}
# Logged at WARNING when used, per §21 -- a wrong-by-default window means
# the backstop fires at the wrong time in whichever direction the guess is
# wrong, and silent guessing turns a tuning problem into a mystery.
DEFAULT_CONTEXT_WINDOW = 128_000


@dataclass
class APICredentials:
    provider_name: str # Pick one of the following (ensure correct spelling & capitalization) {OPENAI, ANTHROPIC, GOOGLE, OPENROUTER, DEEPSEEK, GROK, MISTRAL, GROQ, TOGETHERAI, PERPLEXITY, FIREWORKS, QWEN, Z.AI, COHERE]
    api_key: str # Add your api key to the system environment variables then insert the name of the variable you created between the double quotations 
    
    # api_url: str


@dataclass
class ToolPermissions:
    web_search: bool = True
    # ROADMAP_v2 §15/D24: fetch_url was registered and documented as working
    # but had no field here, so getattr(..., False) denied every call since
    # the tool was added. Allowed by default, matching web_search -- it is
    # already constrained by policy_enforcement's blocked-domain list and
    # its output goes through secret redaction like every other tool's.
    fetch_url: bool = True
    get_time: bool = True
    arxiv_search: bool = True
    symbolic_math: bool = True
    linear_algebra: bool = True
    probability_stats: bool = True
    discrete_math: bool = True
    logic: bool = True
    geometry: bool = True
    read: bool = False
    write: bool = False
    edit: bool = False
    shell: bool = False
    load_skill: bool = True
    # §18: spawning is allowed by default (model autonomy, D6) and needs no
    # approval -- the spawned run's own tools are each gated by policy,
    # intersection-capped by the parent's context (C6).
    spawn_subagent: bool = True
    # §21/D26. Thread-scoped, reversible, and it takes effect inside a
    # conversation the user is watching -- a wrong pin costs some context
    # budget and nothing else.
    pin: bool = True
    # §21b. Allowed everywhere; whether it can actually RUN is decided
    # by the approval gate below plus §13's headless rule.
    remember: bool = True
    # §24 (I2). Allowed, unlike `read`, because it is confined to the
    # project directory by realpath and to documentation/manifest files by
    # an allowlist -- see tools/builtin/project_docs.py for what that
    # deliberately excludes and why the narrow set is load-bearing.
    read_project_doc: bool = True
    # §24 (I1). Allowed, unlike `write`, because it takes a document NAME
    # from a fixed allowlist rather than a path: the destination is derived,
    # so the tool cannot be aimed anywhere else. The gate is below.
    write_project_doc: bool = True
    # §23 slice 2. Allowed everywhere, and ungated below (J12): whether it
    # can actually reach a person is decided by whether the run has a
    # response channel, which the tool itself checks.
    ask_user: bool = True
    # §23 slice 2. Thread-scoped, reversible, and it asks nobody -- so it is
    # allowed and ungated for `pin`'s reasons (J9).
    todo_write: bool = True

@dataclass
class ToolApprovals:
    web_search: bool = False
    # No approval: a tool with approval=True is unusable wherever there is
    # no permission_channel to answer the prompt (the CLI chat loop and
    # every research pass), so gating fetch_url would leave it denied in
    # exactly the places the grounding passes need it -- the same outcome
    # as the D24 bug, with a different error string. See DEVLOG §15.
    fetch_url: bool = False
    get_time: bool = False
    arxiv_search: bool = False
    symbolic_math: bool = False
    linear_algebra: bool = False
    probability_stats: bool = False
    discrete_math: bool = False
    logic: bool = False
    geometry: bool = False
    read: bool = False
    write: bool = False
    edit: bool = False
    shell: bool = True
    load_skill: bool = False
    # Approving a spawn is the §18 subagent sign-off: it authorises the
    # child's whole approval-gated tool set for the rest of the turn, so
    # delegation itself has to be the thing approved. Consequence, named
    # rather than discovered: the headless callability filter drops
    # spawn_subagent anywhere nothing can grant the approval -- CLI chat,
    # and any research run that is neither attended nor granting. The
    # once-per-run notice names it.
    #
    # §25 R4 keeps it out of PIPELINE_UNGRANTABLE's reach deliberately: it
    # can never be PRE-granted for a research run, because approving a
    # spawn hands the child a whole gated set and one launch-time tick
    # would compound into unbounded delegated authority across ten
    # unattended passes. An ATTENDED run can still approve a spawn live,
    # which is a per-call human decision and exactly what the gate is for.
    # The child then runs headless with nothing granted, since
    # spawn_subagent forwards a grant only alongside a permission_channel.
    spawn_subagent: bool = True
    # §21/D26: pin does NOT require approval, and the asymmetry with
    # remember (§21b) is the decision. They differ on the axis that already
    # separates `read` from `write` -- pin is thread-scoped and reversible,
    # while remember writes something that outlives the thread and silently
    # shapes conversations the user has not started yet.
    #
    # Consequence, named rather than discovered: this is what lets pin work
    # on the CLI and inside a research pass, where an approval-gated tool
    # is not merely denied but not advertised at all (§13).
    pin: bool = False
    # §21b/D26, and the asymmetry with pin above IS the decision. A
    # memory outlives its thread and silently shapes conversations the
    # user has not started yet, so it is persistent and invisible at the
    # moment it matters -- the same axis that separates write from read.
    #
    # Consequence: unreachable on any headless path, which includes every
    # research pass. That is deliberate (§21's consequence 1) and is
    # reinforced by PIPELINE_UNGRANTABLE, because a grant would otherwise
    # route around it.
    remember: bool = True
    # §24 (I2): no approval. Reading the project's own documentation, at the
    # user's explicit request, on the files the manifest already listed to
    # them, is not an escalation -- and a gate here would mean a dozen
    # prompts for one /init, which is the shape of consent that gets clicked
    # through. The confinement in project_docs.py is the control.
    read_project_doc: bool = False
    # §24 (I1): gated, on the same axis that separates `read` from `write`.
    # This writes a file into the user's project that is then injected into
    # future prompts, and it invalidates the D17 trust hash as it goes.
    # /init supplies the approval itself, as the yes to a rendered diff --
    # so the gate is what makes "no silent overwrite" (AC2) structural
    # rather than a promise the command makes about itself.
    write_project_doc: bool = True
    # §23 slice 2 (J12): UNGATED. Two reasons, and the second is the one
    # that decides it. Gating would mean approving a prompt in order to be
    # shown a prompt -- but worse, §13 does not merely deny a gated tool
    # where nothing can ask, it stops ADVERTISING it. Gated, this tool would
    # be invisible in every headless run; AC2 requires it be visible,
    # called, and answered with a denial the model can work around. Asking
    # a person is not an action that needs authorising -- it is the least
    # unilateral thing a run can do.
    ask_user: bool = False
    # §23 slice 2 (J9): UNGATED, on the axis that already separates `pin`
    # from `remember`. A todo list is thread-scoped and reversible, and
    # rewriting it costs some prompt budget inside a conversation the user
    # can see. Gating it would also make it invisible in every research pass
    # (§13), which is where a checklist across ten passes helps most.
    todo_write: bool = False
