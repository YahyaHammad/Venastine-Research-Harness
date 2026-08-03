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
# conversation and every research pass. Placeholder default; tune based
# on actual usage patterns once you have real runs to look at.
MAX_TOKEN_BUDGET = 100_000

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
    spawn_subagent: bool = False
