import os
from dataclasses import dataclass

# --- Model / loop settings -- these were missing; call_model, RunAgentLoop,
# and database.py all depend on them ---
MODEL_NAME = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
MAX_TOKENS = 4096
# --- Loop control ---
MAX_ITERATIONS = 20  # matches the max_steps default used elsewhere

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
ENSEMBLE_MODE = False
ENSEMBLE_N = 3
ENSEMBLE_TEMPERATURE = 1.0

# --- Critic-model routing (ROADMAP §11) ---
# Route the critic/grounding passes (3a, 3b, 6c) to a different model than
# the generator, so a model isn't checking its own output for errors.
# None means every pass uses the same provider/model — no special routing.
# Example to enable: {"provider_name": "OPENAI", "model": "gpt-5.1"}
CRITIC_MODEL: dict | None = None

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

@dataclass
class ToolApprovals:
    web_search: bool = False
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
