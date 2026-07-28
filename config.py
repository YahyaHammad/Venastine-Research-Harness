import os
from dataclasses import dataclass

# --- Model / loop settings -- these were missing; call_model, RunAgentLoop,
# and database.py all depend on them ---
MODEL_NAME = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
MAX_TOKENS = 1024
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
#   ensemble_mode / ensemble_n -- N-candidate Pass 1 generation + cross-candidate
#     consistency check feeding Pass 4's scoring. Not built yet.
#   critic_model -- routing the critic/grounding passes to a different model
#     than the generator. Not built yet; every pass currently uses the same
#     provider_name/model passed into run_deep_research_pipeline().

# --- Database ---
DB_PATH = os.environ.get("APP_DB_PATH", "app.db")

# --- Output artifacts ---
OUTPUT_DIR = os.environ.get("AGENT_OUTPUT_DIR", "./output")

# --- File-ops workspace (ROADMAP §6) ---
WORKSPACE_DIR = os.environ.get("AGENT_WORKSPACE", "./workspace")
MAX_FILE_SIZE_BYTES = 10_000_000   # 10 MB — hard reject before opening
MAX_READ_LINES = 500               # max lines per read call
MAX_READ_CHARS = 50_000            # max chars per read call


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
