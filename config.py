import os
from dataclasses import dataclass

# --- Model / loop settings -- these were missing; call_model, RunAgentLoop,
# and database.py all depend on them ---
MODEL_NAME = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
# Per-call output ceiling. Raised from 4096 in ROADMAP_v2 §16: on current
# Anthropic models max_tokens caps THINKING PLUS RESPONSE TEXT together, so
# 4096 truncated answers mid-sentence as soon as reasoning effort was
# enabled. There is no default cumulative budget for it to interact with any
# more (batch 27, #4): spend is uncapped unless settings.json
# max_token_budget says otherwise.
MAX_TOKENS = 16_000
# --- Loop control ---
# Raised from 20 in batch 16 (#45): this is THE default step ceiling -- the
# `or config.MAX_ITERATIONS` fallback at every agent-shaped call site, the
# value an invalid `max_steps:` frontmatter field is repaired to, and the
# ceiling of every chat turn and research pass that does not name its own.
MAX_ITERATIONS = 50

# --- Subagents (ROADMAP_v2 §18) ---
# Maximum spawn_subagent nesting. The counter lives on
# ToolContext.subagent_depth; this is the value it is checked against (C3).
SUBAGENT_MAX_DEPTH = 2

# --- Deep research pipeline ---
MAX_PIPELINE_RETRIES = 2  # max revise/re-validate loop iterations per claim before fallback
MAX_JSON_RETRIES = 2  # max corrective follow-up attempts when a pass returns malformed JSON
                      # (total attempts per pass = MAX_JSON_RETRIES + 1). See ROADMAP §3.

# --- Spend cap (batch 27, #4) ---
# There is NO default spend ceiling any more. The loop's cumulative
# input+output counter is a BILLING meter -- the prompt is re-sent and
# re-billed on every step of a tool-using turn, so it grows quadratically
# in steps, which is correct as billing and meaningless as a size limit.
# Reading it as "how large may a thread get" is the defect TECHNICAL_DEBT
# item 9 recorded; size reasoning reads ModelResponse.turn_new_tokens and
# memory.last_input_tokens now, and compaction keys off measured context
# size (it never keyed off this number).
#
# A user who wants a hard ceiling sets settings.json `max_token_budget`
# (int tokens per _run() invocation -- one user turn, one research pass,
# one /init run). core/config_loader.spend_cap() resolves it, and the
# headroom advisory in effective_compaction() speaks only when a cap is
# configured. Hard limits at the provider remain the stronger instrument:
# they see the same bill and cannot be forgotten by a code path. The three
# constants this file used to carry (MAX_TOKEN_BUDGET 250k,
# RESEARCH_PASS_TOKEN_BUDGET 1M for passes, INIT_TOKEN_BUDGET 1M) died
# here -- the two 1M envelopes existed only because the 250k chat value
# was misread as bounding pass context (the Pass-1-with-14-tool-calls
# incident in DEVLOG), and with no default cap there is nothing to work
# around.

# Deferred for now (core sequential pipeline only, per current scope):
#   (none remaining -- ensemble_mode/ensemble_n built in ROADMAP §10,
#    critic_model built in ROADMAP §11)

# --- Ensemble mode (ROADMAP §10, as redesigned by its revisit) ---
# Run Pass 1 once per entry below, then extract the union of claims across
# candidates and subtract a disagreement penalty in Pass 5. Off by default.
#
# DIVERSITY COMES FROM DIFFERENT MODELS, NOT DIFFERENT SAMPLING (E1). §10
# originally raised `temperature` on N runs of one model. That could not work
# on current Anthropic models, which reject sampling parameters outright (see
# MODELS_REJECTING_SAMPLING_PARAMS), so the section was built, documented as
# working, and could not execute against this harness's own default model.
# ENSEMBLE_TEMPERATURE is deleted rather than repaired: it was an ABSOLUTE
# value being used as though it were a raise, so what "1.0" meant differed
# per provider, and so did how much diversity a run actually got.
#
# The deeper reason is §11's, verbatim: "a model checking its own output for
# errors shares that model's blind spots." N samples of one model agree most
# confidently on that model's systematic errors, which is exactly where a
# research harness needs agreement to mean something. Different models do
# not share blind spots, so their agreement is real evidence.
#
# N is len(ENSEMBLE_MODELS) -- derived, never configured separately (E3). The
# denominator of a confidence score must not be able to disagree with the
# roster that produced it.
#
# config.py ONLY, deliberately -- there is no settings.json key for this,
# following CRITIC_MODEL (E2). Trusting a cloned repo already lets it pick
# the provider and multiply pipeline cost; a project-tier list of N providers
# is that same grant multiplied by N.
#
# Fewer than two DISTINCT (provider_name, model) pairs is refused, not
# tolerated: one model cannot disagree with itself, so every claim would
# score maximal consistency and Pass 5 would read that as confidence. That
# is the original defect, and repeating the same entry N times is the way to
# recreate it through this config.
ENSEMBLE_MODE = False
ENSEMBLE_MODELS: list[dict] | None = None
# Example:
# ENSEMBLE_MODELS = [
#     {"provider_name": "ANTHROPIC", "model": "claude-opus-5"},
#     {"provider_name": "OPENAI", "model": "gpt-5.1"},
#     {"provider_name": "GOOGLE", "model": "gemini-2.5-pro"},
# ]

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

# --- Embedder routing (ROADMAP_v2 §45, SQ2/SQ7) ---
# The model that turns a claim and a source passage into vectors, so
# `similarity_score` is a cosine rather than a self-report. None -- the
# default -- means the pipeline warns once at launch and falls back to the
# grounding model's own number, under source_grounding.md's anchored
# rubric.
#
# `config.py` ONLY, with no settings.json key, following CRITIC_MODEL and
# ENSEMBLE_MODELS (E2): choosing a provider is a grant, and this one sends
# claim text and fetched page text to whoever is named. `/embedder` writes
# to the user-tier store in core/pipeline_models.py, which OUTRANKS this.
# Example: {"provider_name": "OPENAI", "model": "text-embedding-3-small"}
EMBEDDER_MODEL: dict | None = None

# Asymmetric embedders score materially worse without their prefixes and
# produce NO ERROR AT ALL when they are missing, which is why this is a
# table rather than something inferred. Keyed by a substring of the model
# slug, longest match wins; a model matching nothing gets no prefix, which
# is correct for every symmetric embedder (OpenAI's, Cohere's, Voyage's).
#
# `task_type` is Google's equivalent and is ignored by every other
# provider. Incomplete on purpose: an unlisted asymmetric model scores
# lower than it should, which is visible in the artifact as a low cosine
# on a source that plainly matches -- and adding a row is the fix.
# Raw cosine is NOT a 0-1 score, and treating it as one is the quiet way
# to publish a wrong number. Unrelated text sits near 0.1 on some models
# and above 0.7 on others, so the same passage would score "irrelevant" on
# one embedder and "supports the claim" on another. This maps the useful
# band onto 0-1: below `floor` is noise, above `ceiling` is as close as
# that model gets.
#
# Keyed by a substring of the model slug, longest match wins; anything
# unlisted takes DEFAULT_SIMILARITY_CALIBRATION. The RAW cosine is always
# recorded beside the calibrated score, so a bad row here is visible in
# the artifact and fixable after the fact rather than baked in.
SIMILARITY_CALIBRATION: dict[str, dict] = {
    "text-embedding-3": {"floor": 0.10, "ceiling": 0.65},
    "text-embedding-ada": {"floor": 0.70, "ceiling": 0.92},
    "e5-": {"floor": 0.70, "ceiling": 0.92},
    "multilingual-e5": {"floor": 0.70, "ceiling": 0.92},
    "bge-": {"floor": 0.55, "ceiling": 0.85},
    "gte-": {"floor": 0.55, "ceiling": 0.88},
    "nomic-embed": {"floor": 0.40, "ceiling": 0.80},
    "embed-english": {"floor": 0.25, "ceiling": 0.80},
    "voyage": {"floor": 0.35, "ceiling": 0.85},
    "gemini-embedding": {"floor": 0.35, "ceiling": 0.85},
}

DEFAULT_SIMILARITY_CALIBRATION = {"floor": 0.25, "ceiling": 0.85}

EMBEDDER_PREFIXES: dict[str, dict] = {
    "e5-": {"query": "query: ", "passage": "passage: "},
    "multilingual-e5": {"query": "query: ", "passage": "passage: "},
    "bge-": {"query": "Represent this sentence for searching relevant passages: ",
             "passage": ""},
    "gte-": {"query": "", "passage": ""},
    "nomic-embed": {"query": "search_query: ", "passage": "search_document: "},
    "gemini-embedding": {"query_task_type": "RETRIEVAL_QUERY",
                         "passage_task_type": "RETRIEVAL_DOCUMENT"},
    "text-embedding-004": {"query_task_type": "RETRIEVAL_QUERY",
                           "passage_task_type": "RETRIEVAL_DOCUMENT"},
}

# --- Source authority: domain classes (ROADMAP_v2 §45, SQ4) ---
# What a source is worth before anything is known about the specific page.
# Two tables, deliberately: a CLASS carries the number, and a SUFFIX names
# which class a host belongs to. A user who thinks preprints deserve more
# edits one number; a user adding their field's own trusted domain edits a
# membership. One combined {suffix: score} table would make the first of
# those a find-and-replace across a hundred rows.
#
# THE SIGNAL IS A RESTRICTED REGISTRY, NOT A FAMILIAR SUFFIX. `.gov`,
# `.mil`, `.int` and `.edu` gate registration -- you cannot buy one -- and
# so do `gov.uk`, `ac.uk`, `gc.ca` and their equivalents worldwide, which
# is why source_scoring derives the `<kind>.<cc>` forms rather than
# enumerating them here. `.org` gates nothing at all and never has, so it
# sits barely above `.com`; treating it as trustworthy is the single most
# common version of this mistake.
#
# INCOMPLETE ON PURPOSE, and safe when incomplete: an unlisted host takes
# DEFAULT_DOMAIN_AUTHORITY, which is the generic-commercial number. The
# failure mode is a good source scored ordinary, never a bad source scored
# authoritative, and the model's bounded adjustment (below) is the route
# for the first.
DOMAIN_AUTHORITY_CLASSES: dict[str, float] = {
    "restricted_registry": 0.90,     # .gov .mil .int .edu, gov.uk, ac.uk...
    "intergovernmental": 0.90,       # WHO, UN, World Bank, OECD, EU
    "standards_body": 0.88,          # IETF, W3C, ISO, NIST, IEC
    "peer_reviewed_publisher": 0.85,
    "established_news": 0.65,
    "preprint_server": 0.60,         # not peer reviewed; §45 SQ5 refines this
    "reference_tertiary": 0.55,      # encyclopaedias -- a pointer to a source
    "generic_org": 0.55,             # an unrestricted registry, small bump
    "generic_commercial": 0.45,
    "blog_platform": 0.30,
    "forum_qa": 0.25,
    "social_media": 0.15,
    "content_farm": 0.10,
}

DEFAULT_DOMAIN_AUTHORITY = 0.45      # == generic_commercial, for an unknown host

# host suffix -> class. Longest suffix wins, so `blogs.nature.com` can be
# listed separately from `nature.com` if it ever needs to be.
DOMAIN_AUTHORITY_SUFFIXES: dict[str, str] = {
    # Restricted registries. The bare TLDs; the `<kind>.<cc>` second-level
    # forms are derived in source_scoring.py rather than listed.
    "gov": "restricted_registry",
    "mil": "restricted_registry",
    "int": "restricted_registry",
    "edu": "restricted_registry",

    # Intergovernmental bodies, whose hosts are ordinary .org/.int names.
    "who.int": "intergovernmental",
    "un.org": "intergovernmental",
    "europa.eu": "intergovernmental",
    "oecd.org": "intergovernmental",
    "imf.org": "intergovernmental",
    "worldbank.org": "intergovernmental",
    "iaea.org": "intergovernmental",

    # Standards bodies.
    "ietf.org": "standards_body",
    "rfc-editor.org": "standards_body",
    "w3.org": "standards_body",
    "iso.org": "standards_body",
    "iec.ch": "standards_body",
    "ieee.org": "standards_body",
    "unicode.org": "standards_body",
    "ecma-international.org": "standards_body",

    # Peer-reviewed publishers and indexes.
    "nature.com": "peer_reviewed_publisher",
    "science.org": "peer_reviewed_publisher",
    "sciencemag.org": "peer_reviewed_publisher",
    "nejm.org": "peer_reviewed_publisher",
    "thelancet.com": "peer_reviewed_publisher",
    "bmj.com": "peer_reviewed_publisher",
    "cell.com": "peer_reviewed_publisher",
    "pnas.org": "peer_reviewed_publisher",
    "sciencedirect.com": "peer_reviewed_publisher",
    "springer.com": "peer_reviewed_publisher",
    "link.springer.com": "peer_reviewed_publisher",
    "onlinelibrary.wiley.com": "peer_reviewed_publisher",
    "wiley.com": "peer_reviewed_publisher",
    "tandfonline.com": "peer_reviewed_publisher",
    "sagepub.com": "peer_reviewed_publisher",
    "acm.org": "peer_reviewed_publisher",
    "aps.org": "peer_reviewed_publisher",
    "acs.org": "peer_reviewed_publisher",
    "plos.org": "peer_reviewed_publisher",
    "frontiersin.org": "peer_reviewed_publisher",
    "mdpi.com": "peer_reviewed_publisher",
    "jstor.org": "peer_reviewed_publisher",
    "pubmed.ncbi.nlm.nih.gov": "peer_reviewed_publisher",

    # Preprint servers and repositories -- not peer reviewed.
    "arxiv.org": "preprint_server",
    "biorxiv.org": "preprint_server",
    "medrxiv.org": "preprint_server",
    "chemrxiv.org": "preprint_server",
    "ssrn.com": "preprint_server",
    "papers.ssrn.com": "preprint_server",
    "osf.io": "preprint_server",
    "researchsquare.com": "preprint_server",
    "semanticscholar.org": "preprint_server",

    # News organisations with a correction policy and a masthead. An
    # ALLOWLIST, because "news site" is not a property of a domain suffix
    # and the alternative is scoring every .com that publishes articles.
    "reuters.com": "established_news",
    "apnews.com": "established_news",
    "bbc.com": "established_news",
    "bbc.co.uk": "established_news",
    "ft.com": "established_news",
    "economist.com": "established_news",
    "nytimes.com": "established_news",
    "washingtonpost.com": "established_news",
    "wsj.com": "established_news",
    "theguardian.com": "established_news",
    "npr.org": "established_news",
    "pbs.org": "established_news",
    "bloomberg.com": "established_news",
    "nikkei.com": "established_news",
    "afp.com": "established_news",
    "dw.com": "established_news",

    # Tertiary references -- they summarise sources rather than being one.
    "wikipedia.org": "reference_tertiary",
    "wikimedia.org": "reference_tertiary",
    "wiktionary.org": "reference_tertiary",
    "britannica.com": "reference_tertiary",

    "org": "generic_org",

    # Blog and newsletter platforms: the host says nothing about the author.
    "medium.com": "blog_platform",
    "substack.com": "blog_platform",
    "wordpress.com": "blog_platform",
    "blogspot.com": "blog_platform",
    "blogger.com": "blog_platform",
    "tumblr.com": "blog_platform",
    "wixsite.com": "blog_platform",
    "ghost.io": "blog_platform",

    # Forums and Q&A. Often correct, never attributable.
    "reddit.com": "forum_qa",
    "quora.com": "forum_qa",
    "stackexchange.com": "forum_qa",
    "stackoverflow.com": "forum_qa",
    "news.ycombinator.com": "forum_qa",
    "4chan.org": "content_farm",

    # Social media. Speculation and unattributed claims travel here fastest,
    # which is the whole reason a claim needs grounding elsewhere.
    "x.com": "social_media",
    "twitter.com": "social_media",
    "facebook.com": "social_media",
    "instagram.com": "social_media",
    "tiktok.com": "social_media",
    "threads.net": "social_media",
    "linkedin.com": "social_media",
    "mastodon.social": "social_media",
    "bsky.app": "social_media",
    "youtube.com": "social_media",
    "vk.com": "social_media",
    "weibo.com": "social_media",
    "t.me": "social_media",

    # Aggregators that republish without attribution, and image boards with
    # no text to ground anything in. The measured case: a Pinterest pin
    # scored similarity 1.0 on a Nobel Prize claim.
    "pinterest.com": "content_farm",
    "answers.com": "content_farm",
    "ehow.com": "content_farm",
    "scribd.com": "content_farm",
    "coursehero.com": "content_farm",
    "chegg.com": "content_farm",
}

# How far the model may move a computed authority, in either direction
# (§45, SQ4). Small on purpose: it is a correction for what a domain
# cannot see -- a primary source on an ordinary host, an opinion column on
# a masthead -- not a second opinion about the number. An adjustment
# arriving without a reason is discarded rather than clamped.
AUTHORITY_ADJUSTMENT_CAP = 0.15

# --- The sources/ artifact (ROADMAP_v2 §45, SQ8) ---
# Whether output/<run_id>/sources/ keeps the TEXT each cited page served,
# or only its hash, size and provenance.
#
# On by default, because it is what makes a similarity score reproducible
# after the run -- and the directory output_writer has created since §12
# was described by fetch_url.py's own comment for just as long. Everything
# written there has been through redact_output_text on entry to the
# corpus.
#
# It is new data at rest, so it has a switch and PRIVACY.md says what it
# holds. Turning it off costs the audit trail, not the scores: the corpus
# still exists in memory for the run that computes them.
PERSIST_SOURCE_TEXT = True

# --- Scholarly authority: OpenAlex (ROADMAP_v2 §45, SQ5/SQ9) ---
# OFF BY DEFAULT. This is the first network call in this harness made by
# something that is not a registered tool, so it bypasses the approval
# registry and granted_calls.json -- and an out-of-the-box run must make
# no call the user did not ask for. With it off, a paper takes its
# preprint-server or publisher domain class and nothing else.
SCHOLAR_LOOKUP = False

SCHOLAR_API_URL = "https://api.openalex.org"
SCHOLAR_TIMEOUT_S = 8.0
SCHOLAR_MAX_RETRIES = 2
SCHOLAR_CACHE_TTL_S = 900

# OpenAlex asks for a contact address to put a caller in its "polite pool"
# (higher rate limits). EMPTY BY DEFAULT AND NEVER DERIVED: an address is
# personal data, and sending one the user did not type -- from a git
# config, an environment variable, anywhere -- is not a rate-limit
# optimisation, it is an unasked-for disclosure.
SCHOLAR_MAILTO = ""

# How a paper's score is composed. Renormalised over whichever terms are
# actually available, so a paper too young to have a citation percentile
# is not scored as though it had a bad one.
SCHOLAR_VENUE_WEIGHT = 0.40
SCHOLAR_CITATION_WEIGHT = 0.40
SCHOLAR_AUTHOR_WEIGHT = 0.20

# Venue credit. A preprint scores lower than a comparable published paper
# -- generally, not strictly, which is why it is a weight and not a gate.
SCHOLAR_VENUE_CREDIT = {
    "journal": 1.0,
    "conference": 1.0,
    "book series": 0.85,
    "repository": 0.55,     # arXiv, bioRxiv, institutional repositories
    "unknown": 0.6,
}

# max h-index across a paper's authors, log-saturated. 60 is a full career
# at the top of most fields; the curve is deliberately flat above it,
# because the difference between 60 and 90 says more about field size and
# career length than about this paper.
SCHOLAR_H_SATURATION = 60

# Below this many works, a field-and-year cohort is too small for a
# percentile to mean anything and the window widens instead.
SCHOLAR_MIN_COHORT_SIZE = 200

# Below this age, citation counts are noise: 72% of computer-science
# preprints from 2024 have zero citations, so a three-week-old paper's
# zero says nothing at all. The term is DROPPED and the remaining weights
# renormalise, rather than being scored as low impact.
SCHOLAR_MIN_CITATION_AGE_DAYS = 60

# A retracted paper is not evidence, whatever its venue or citation count
# -- and a heavily cited retraction is the most dangerous shape there is.
SCHOLAR_RETRACTED_AUTHORITY = 0.05

# --- Reasoning effort (ROADMAP_v2 §16; default changed batch 25, #139) ---
# The default effort level requested when the user has not chosen one.
#
# This shipped as None -- "send nothing", the one universally safe value,
# because reasoning_effort is rejected by OpenAI-compatible NON-reasoning
# models. Batch 25 changed it to "high" by owner decision: the pipeline
# now consumes the setting (#139), so None meant the mode this project
# exists for ran at the provider's own default forever, invisibly. The
# risk the old note named is managed in MODEL_EFFORT_LEVELS rather than
# by silence: known non-reasoning models are listed there with an EMPTY
# level set, which effort_for() treats as authoritative and drops the
# level cleanly instead of sending a parameter that would 400.
DEFAULT_EFFORT: str | None = "high"

# Fallback effort levels for providers whose APIs expose no capability
# endpoint (every OpenAI-compatible provider, and Google). ANTHROPIC is NOT
# listed here on purpose: its Models API reports per-model effort support,
# so client.py queries it and new Anthropic models need no entry.
#
# An entry whose value is an EMPTY list means "this model takes no effort
# parameter at all" and is authoritative: effort_for() DROPS the requested
# level with a naming warning instead of sending it. That is the safety
# mechanism behind DEFAULT_EFFORT="high" -- without these entries, the
# fallback for an unknown model ASSUMES ["low","medium","high"] and the
# default high would reach gpt-4o-class endpoints as a 400 on every call.
# Incomplete on purpose (same posture as MODELS_REJECTING_SAMPLING_PARAMS):
# an unlisted non-reasoning endpoint still fails loud, and the fix is one
# line here or `--effort auto`.
MODEL_EFFORT_LEVELS: dict[str, list[str]] = {
    "gpt-4o": [],
    "gpt-4o-mini": [],
    "gpt-4.1": [],
    "gpt-4.1-mini": [],
    "gpt-4.1-nano": [],
    "gpt-3.5-turbo": [],
}
DEFAULT_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max", "ultra"]

# Google expresses reasoning depth as an integer token budget rather than an
# enum, so a level has to be mapped to a number at the boundary. -1 is the
# SDK's "decide dynamically" sentinel.
GOOGLE_THINKING_BUDGETS = {
    "low": 2_048,
    "medium": 8_192,
    "high": 16_384,
    "xhigh": 24_576,
    "ultra": 32_768,
    "max": -1,
}

# --- Database ---
DB_PATH = os.environ.get("APP_DB_PATH", "app.db")

# --- Output artifacts ---
#
# BESIDE THE WORK, which since batch 44 means beside the WORKSPACE when one
# was named. "State is global, artifacts are local" is the distribution
# split, and the local half is only true if "local" tracks where the work
# is: with AGENT_WORKSPACE pointing at a project, a run launched from the
# harness checkout wrote its reports into the harness. Reading the env var
# rather than WORKSPACE_DIR below, and defaulting it to ".", so this stays
# one expression with no branch in it -- an unset AGENT_WORKSPACE gives
# ./output exactly as before, not ./workspace/output.
OUTPUT_DIR = os.environ.get(
    "AGENT_OUTPUT_DIR",
    os.path.join(os.environ.get("AGENT_WORKSPACE", "."), "output"))

# --- File-ops workspace (ROADMAP §6) ---
WORKSPACE_DIR = os.environ.get("AGENT_WORKSPACE", "./workspace")

# Whether AGENT_WORKSPACE was NAMED, as opposed to defaulted (batch 44).
#
# The presence of the variable, never its value: the default "./workspace"
# is a subdirectory of wherever you launched, so a value test would make
# `./workspace` the project for everyone who never set anything -- which
# is the whole population this must not disturb. main() reads this to
# decide the project path; the decision is there, because config.py holds
# plain values and this is one.
WORKSPACE_DIR_EXPLICIT = "AGENT_WORKSPACE" in os.environ
MAX_FILE_SIZE_BYTES = 25_000_000   # 25 MB — hard reject before opening
MAX_READ_LINES = 500               # max lines per read call
MAX_READ_CHARS = 50_000            # max chars per read call

# --- /init (ROADMAP_v2 §24) ---
INITIALIZER_AGENT = "initializer"

# Three bounds, each doing a different job (I7). /init is a tool-heavy loop
# over documentation that can run to hundreds of kilobytes -- this repo's own
# root markdown is 721KB, with DEVLOG.md alone at 226KB.
#
# INIT_READ_CHARS is well below MAX_READ_CHARS because of TECHNICAL_DEBT
# item 9 (now closed, batch 27): the billing meter re-counts the WHOLE
# prompt on every step, so each 50KB read is re-billed for every step that
# follows it. 20KB keeps a dozen reads affordable and still returns a
# useful span of a document per call. It stays even though the meter no
# longer caps anything by default -- it bounds per-read VOLUME, which is a
# different axis from spend and from the number of reads.
INIT_READ_CHARS = 20_000
# The number of reads, which neither budget nor read size bounds: a model
# can spend an unbounded number of small reads inside any ceiling.
INIT_MAX_STEPS = 12

# --- MCP teardown (ROADMAP_v2 §37 F6, #64) ---
# ONE shared wall-clock budget for the whole goodbye -- polite close,
# force-cancel, loop-thread join -- replacing three sequential 15-second
# waits whose worst case hung a quitting harness for ~45s, Ctrl+C
# included. Servers still alive when the budget expires are named in a
# WARNING; their child processes may outlive this session. The value
# lives with the other harness bounds and is re-exported by
# mcp_client/client.py beside its sibling timeouts.
TEARDOWN_BUDGET_S = 10.0

# --- Shell / sandbox (ROADMAP §7) ---
SHELL_BINARY = os.environ.get("AGENT_SHELL", "")  # auto-detect if empty
ALLOW_INSECURE_SANDBOX_FALLBACK = False  # explicitly enable subprocess fallback
AUTO_APPROVE_SANDBOX_FALLBACK = False    # auto-approve fallback runs (no per-run prompt)

# ROADMAP_v2 §28 (G3). WHICH shell commands need a human to say yes.
#
#   "always"  every command is asked about, whatever it does.
#   "tiered"  the classifier decides -- see security/capability.py for the
#             one rule, and security/sandbox.py:classify_command for how a
#             command is measured against it.
#   "never"   nothing is ever asked about.
#
# This is the gate. Before §28 the gate was ToolApprovals.shell, and
# `_shell_approval_check` looked like a five-layer policy underneath it --
# but four of those layers could only ever return False on the shipped
# flags, so the whole thing was a pass-through for one boolean and the
# harness had exactly two settings: ask about everything, or ask about
# nothing. "tiered" is the rung that docstring already claimed existed.
#
# "never" is that second setting, preserved deliberately and now NAMED.
# It is the pre-§28 `ToolApprovals.shell = False` behaviour exactly: an
# inert command runs on the HOST with unrestricted arguments, so `cat
# ~/.aws/credentials` returns your keys to the model. You reach that by
# writing "never", not by switching off a field that reads like "stop
# nagging me" -- which is the actual fix for audit #157.
# The legal set lives with the code that ENFORCES it --
# security/capability.APPROVAL_MODES, which validate_mode() reads. A second
# copy here was read by nothing but a test asserting its literal, so the two
# could have disagreed and only the unenforced one would have been wrong.
SHELL_APPROVAL_MODE = "tiered"
SANDBOX_DOCKER_IMAGE = os.environ.get("AGENT_SANDBOX_IMAGE", "python:3.13-slim")
SANDBOX_TIMEOUT_SECONDS = 120
SANDBOX_MEMORY_MB = 2048
SANDBOX_CPU_SECONDS = 30
SANDBOX_MAX_PIDS = 200

# ---------------------------------------------------------------------------
# --- Output redaction (#167/#49, batch 20) ----------------------------------
# ---------------------------------------------------------------------------

# The PERMANENT master switch for pattern-based redaction of what leaves a
# tool: vendor token substitution and credential-shape redaction, applied
# by check_output_policy() to every tool result and by param_digest() to
# what the shells display of an argument. On by default; a user whose
# workflow needs the model to see real values (debugging their own
# credentials, say) turns it off here, or per-run with
# VENASTINE_REDACT_OFF in the environment -- see redaction_enabled().
#
# DELIBERATELY NOT a settings.json key (decision recorded in DEVLOG,
# batch 20): project tier beats user tier there, so a cloned repo could
# ship `.venastine/settings.json` switching the scrubbing off behind a
# trust prompt nobody reads -- the exact shape G7 rejected
# shell_approval_mode for and R12 rejected research.granted_tools for.
# Permanent means editing this line.
#
# NEVER covers three things, whatever this is set to: check_input_policy's
# refusals (a denial is legible, not destructive); check_output_policy's
# depth-cap substitution (fail-closed structure bound -- making it optional
# would recreate the deterministic bypass its comment forbids); and
# logging_setup.py's formatter redaction (the second sink keeps its own
# guard).
REDACT_TOOL_OUTPUTS = True

# ROADMAP_v2 §31 (H9). The wall clock on a BUDGET_COMPUTE tool call --
# the six math tools, which are pure functions of their params and have
# nothing bounding them from the inside. dispatch runs them in a
# killable subprocess under this budget (tools/isolation.py).
#
# 15s is chosen against measurement, not taste. The slowest LEGITIMATE
# call found is `symbolic_math series order=1000` at 3.44s, so this is
# roughly 4x headroom; the runaways it exists for do not return at all.
# Ten passes each burning a full budget is 150s rather than forever,
# which is the trade being made.
#
# ONE number, not one per tool: there is no evidence any two math tools
# want different answers, and a per-tool budget makes each a judgement
# call at the registration site. It sits beside SANDBOX_TIMEOUT_SECONDS
# because that is the constant a reader would compare it against.
TOOL_COMPUTE_TIMEOUT_S = 20

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

# How much of the model's context window a thread may fill before
# compaction folds it. thresholds() multiplies this by context_limit(),
# so it is 850k on a 1M model and 170k on a 200k one.
#
# BATCH 44 REVERSED M1, AND THE REASON M1 GAVE HAS EXPIRED. M1 rejected a
# window-derived trigger because "a window-derived trigger sits at a size
# the thread can never reach in working order" -- and the arithmetic
# behind that was about MAX_TOKEN_BUDGET, then a default spend ceiling of
# 250k: the prompt is re-sent on every step of a tool-using turn, so a
# thread at 160k got one response and no tool calls before the cap ended
# the turn. Batch 27 DELETED that default ceiling. There is no cap for a
# large trigger to collide with any more, so the premise is gone and the
# flat 40k outlived it -- compacting a 1M-window thread at 4% of its
# window, spending a model call and losing fidelity to solve a problem
# the model did not have.
#
# THE COST OF THE REVERSAL, STATED. MODEL_CONTEXT_WINDOWS and the provider
# query decide when every thread folds now, where under M1 a wrong entry
# only mistimed a research backstop. That is why core/model_windows.py
# exists (the user's own answer, outranking both) and why the fallback
# still WARNS. A fraction rather than `window - margin`: a fixed margin
# that is right for a 1M model leaves a 64k model no working room at all,
# and one that is right for 64k never fires on 1M.
COMPACTION_TRIGGER_FRACTION = 0.85

# The trigger used when NO MODEL IS IN SCOPE -- and the default
# settings.json's `compaction.trigger_tokens` overrides.
#
# One caller has no (provider, model) to derive a window from:
# compaction.pin_measurements, which computes #89's cap as
# PIN_MAX_TRIGGER_FRACTION x the trigger from a thread id alone. Left at
# 40_000 deliberately, so the pin cap is exactly what it was before batch
# 44 -- this batch changes when a thread compacts, not how much of one a
# single pin may freeze, and a cap that grew with the window would let one
# `pin` call freeze 425k tokens of a 1M-window thread.
COMPACTION_TRIGGER_TOKENS = 40_000

# Fires this many tokens BEFORE the trigger, so there is room to do
# something about it -- pin a message, wrap up a thought, run /compact at a
# natural break.
#
# STILL ABSOLUTE after batch 44, and that is a decision rather than an
# oversight. What this buys is a HUMAN'S REACTION TIME, measured in turns,
# and a turn does not get bigger because the window did: 8k is roughly a
# turn or two of notice on a 64k model and on a 1M one alike. As a
# fraction of the new trigger it would be 128k on a 1M model -- a warning
# that fires while the thread is at 72% and has nothing to worry about
# yet. §21 singles this out as the one value whose wrong setting
# is not self-correcting: too small and it is an alert with no time
# attached to it. Validated to be strictly less than the trigger.
COMPACTION_WARNING_MARGIN_TOKENS = 8_000

# The most recent tokens that always stay verbatim, never compacted.
#
# ABSOLUTE, like the warning margin above and for the same kind of reason:
# this is an amount of CONVERSATION to keep intact, not a share of a
# window. It is also the weaker of the two floors in practice -- M5 keeps
# whichever protects more, and the three-turn floor below almost always
# protects more than 4k on the long threads that reach the trigger at all.
# Left where it was so batch 44 changes when a thread folds and not what
# survives the fold.
COMPACTION_KEEP_RECENT_TOKENS = 4_000

# M5: a FLOOR in turns, on top of the token floor above, and the two
# compose -- whichever protects more wins, plus the current turn, which is
# never foldable at all. A single tool-heavy turn can consume the whole
# keep-token budget by itself, leaving the immediately preceding exchange
# summarized; a follow-up like "no, the other one" then has no referent.
COMPACTION_KEEP_RECENT_TURNS = 3

# #89 (batch 16). The most one `pin` call may protect, as a fraction of
# the trigger above. A pin is a PERMANENT FLOOR under the derived view:
# pinned rows re-enter it verbatim forever (M9), nothing folds them, and
# before this cap one ungated call with last_n=40 could put a thread
# permanently past the trigger with no unpin to undo it. 0.5 leaves room
# for a real working session while making "floor the trigger by yourself"
# structurally impossible. Enforced at the tool with a REFUSAL that states
# the cap and the current share -- never silently trimmed (M15's rule: a
# pin asked for more protection than allowed must not quietly deliver less).
PIN_MAX_TRIGGER_FRACTION = 0.5

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

# M2 as amended by batch 16 (#90, owner decision). Two strategies:
#
#   "chain"     summarizes the PREVIOUS SUMMARY plus whatever followed it.
#               Constant cost per compaction forever -- the input never
#               grows past summary-plus-tail -- at the price of loss that
#               compounds over a long-lived thread (a summary of a summary
#               of a ...).
#   "rederive"  summarizes the ORIGINAL messages every time, so exactly
#               one summarization step always sits between an original
#               message and what the model sees. Fidelity-optimal; input
#               grows with the covered span.
#
# The DEFAULT is "chain", reversing M2's original choice -- an explicit
# owner decision recorded in DEVLOG (batch 16): rederive's whole-span
# input made every compaction the most expensive call of the turn, and
# cost compounds on exactly the threads that trigger most. Whatever is
# configured here, BOTH strategies fall back to chain automatically when
# a span outgrows one call (built at last, after three documents promised
# it for two sections), saying so in a WARNING -- and truncate the oldest
# material with a stated truncation when even chain cannot fit, because
# an oversized send is never the right failure direction.
COMPACTION_STRATEGY = "chain"
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

# --- Catalog text (ROADMAP_v2 §32, A5) ---
#
# The cap on a skill's or an agent's `name` and `description` -- the
# two strings that go from a .md file straight into the system prompt
# of every run in a project, with no tool call and no further consent
# (#131).
#
# 300 is headroom rather than a squeeze: the longest description this
# project ships is `compactor` at 171 characters, and all eight are
# single-line. A project whose description does not fit in twice the
# length of ours is writing instructions, not a summary, which is
# exactly the thing the cap is for.
#
# THE NEWLINE COLLAPSE MATTERS MORE THAN THE NUMBER, and they are one
# constant because they are one rule -- see config_loader._catalog_text.
# Same posture as tools/builtin/arxiv.MAX_SUMMARY_CHARS (600) and
# web_search.MAX_SNIPPET_CHARS (300): model-facing text from a source
# this project did not write is bounded where it is produced.
MAX_CATALOG_TEXT_CHARS = 300

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

# The backstop's source, the summarizer's input budget, and M1's remaining
# use for a window table.
#
# NOW A FALLBACK, NOT THE ONLY SOURCE. This said "deliberately NOT a
# per-provider API query", on the grounds that the roster of fifteen had no
# uniform endpoint and a query would need fifteen adapters to avoid a
# fallback constant they would all still need. The premise was measured
# against the PINNED SDKs and did not survive:
#
#   anthropic 0.116.0    ModelInfo.max_input_tokens -- on the SAME object
#                        core/client.py already retrieves to read
#                        capabilities.effort. The answer was in hand and
#                        being dropped; reading it costs no extra call.
#   google-genai 1.0.0   types.Model.input_token_limit, one field.
#   openai 2.45.0        Model is declared extra="allow", so a provider's
#                        own field survives onto .model_extra. That makes
#                        the OpenAI-compatible side ONE alias reader --
#                        context_window (Groq), context_length (Together,
#                        OpenRouter, Cohere), max_context_length (Mistral)
#                        -- rather than thirteen adapters.
#
# So it is two attribute reads and one sniff, not fifteen adapters. The
# query lives in core/client.py:context_window_for, beside the effort query
# it mirrors; core/compaction.py:context_limit prefers it and falls back
# here. (The old note also corrected itself on a provider count from the
# dead APICredentials class -- audit #23. Fifteen is right; the count was
# never what was wrong with the argument.)
#
# THE CONCLUSION OUTLIVED THE PREMISE, which is why this table stays: OpenAI,
# DeepSeek and Perplexity answer /models with id/created/object/owned_by and
# nothing else, and Grok, Fireworks, Qwen and Z.AI publish no model-metadata
# endpoint at all. A fallback constant is still needed at the end of it.
#
# KEYS ARE STORED NORMALIZED -- no date suffix, no `vendor/` or `region.`
# prefix. core/compaction.py:_normalized reduces an incoming id to this form
# before looking it up, so a dated key here would be UNREACHABLE. That is
# also the bug this fixed: matching was exact, so `claude-sonnet-5-20260724`
# missed a million-token entry and silently took the default.
#
# AN INCOMPLETE ENTRY IS EXPENSIVE NOW. Under M1 it cost a mistimed
# research backstop and a mistimed summarizer input budget, because the
# working-set trigger was a flat number that never consulted this. Batch 44
# made that trigger a FRACTION of what context_limit() returns, so a wrong
# number here mistimes compaction on every thread -- too high and the
# thread never folds and the provider raises a hard context-limit error
# instead. core/model_windows.py is the user's answer to that, and /window
# is how they give it; this table is what answers before they do.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # 1M IS THE ORDINARY WINDOW ON THESE, not a beta.
    #
    # This block used to carry the opposite claim -- that 1M was gated
    # behind a `context-1m-2025-08-07` header this harness does not send,
    # so an ordinary account got 200_000 and these entries "want to be
    # 200_000". That was true of the Sonnet 4 era and was never true of
    # the models listed here; checked against the current model reference
    # in batch 44, the whole 4.6-and-later family is 1M and Haiku 4.5 is
    # 200K. The values were right and the comment was stale, which is the
    # dangerous combination now that the trigger reads them: a reader
    # following the old note would have "corrected" five entries by a
    # factor of five and compacted every Claude thread at a fifth of the
    # window.
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    # Dateless, per the normalization note above: this was keyed
    # "claude-haiku-4-5-20251001" while its five siblings were dateless,
    # which is the inconsistency that exposed the exact-match bug.
    "claude-haiku-4-5": 200_000,
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
# Logged at WARNING ONCE PER MODEL, per §21 -- a wrong-by-default window
# means compaction and the backstop both fire at the wrong time in
# whichever direction the guess is wrong, and silent guessing turns a
# tuning problem into a mystery. Once per model rather than once per use,
# because context_limit() is on should_compact()'s path and that runs at
# the top of every step of every turn; the dedup set lives in
# core/compaction.py. A long session therefore says this once and is then
# silent, which is the trade -- the line is actionable exactly once, and
# since batch 44 it names the action (/window, which remembers).
#
# 256_000 rather than 200_000 (batch 44): the floor a hosted agentic model
# ships with today. Below that is essentially always a self-hosted
# deployment, whose operator knows the number and can say it once with
# /window -- which is the case this default cannot serve and does not try
# to. Erring low here would compact every unknown model early forever to
# protect the rarer case that has an owner who can answer.
DEFAULT_CONTEXT_WINDOW = 256_000


# APICredentials was here and is deleted (audit #23). It was dead -- nothing
# constructed it, imported it or type-hinted against it -- and its guidance
# contradicted the mechanism that is real: it told the reader to put the key
# in an environment variable and write the VARIABLE'S NAME here, while
# credentials.save_credentials stores the key value directly in
# providers.json. Anyone following it ended up with the literal string
# "MY_KEY_VAR" as their API key.
#
# Credentials flow through credentials.py (providers.json) and env_secrets.py
# (.env) -- the deliberate two-mechanism split D19 describes, which
# env_secrets.py documents at length on the other side. The provider roster
# lives in providers.json.example, beside the file it describes.


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
    # §21/D26. Thread-scoped, reversible (batch 16 gave that word a
    # mechanism: `unpin`), and it takes effect inside a conversation the
    # user is watching -- a wrong pin costs some context budget and nothing
    # else.
    pin: bool = True
    # §21/D26, restored by batch 16 (#89). D26's premise called a pin
    # reversible while nothing anywhere unpinned one; unpin is the other
    # half of making the premise true rather than aspirational. Same axis,
    # same gating posture, same reasons.
    unpin: bool = True
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
    # ROADMAP_v2 §28 (G3). False since §28, and False here does NOT mean
    # "never ask" -- SHELL_APPROVAL_MODE is the gate, and it ships
    # "tiered". This field is the RATCHET, and it still only tightens:
    # approval_needed() ORs the tool's own check with requires_approval(),
    # and requires_approval() reads this field, so setting it True forces
    # "always" whatever the mode says. An agent's approval_overrides
    # reaches the same OR and has the same one-way power (D14).
    #
    # It stays declared because D24 requires every registered tool to have
    # a field in both dataclasses, and because the ratchet needs somewhere
    # to live. Do not delete it and do not read it as the gate.
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
    # §25 R4 declares it GRANT_NEVER (R13, on the ToolSpec): it can never
    # be PRE-granted, in a research run OR at §18's sign-off, because
    # approving a spawn hands the child a whole gated set and one
    # launch-time tick would compound into unbounded delegated authority
    # across ten unattended passes. #67 is why "or at the sign-off" is in
    # that sentence: R4's argument is ABOUT the sign-off, and the
    # exclusion had never reached it. An ATTENDED run can still approve a
    # spawn live,
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
    # §21/D26, batch 16 (#89). Ungated for pin's reasons, and now genuinely
    # symmetric: releasing protection costs context budget in a
    # conversation the user is watching, exactly like applying it.
    unpin: bool = False
    # §21b/D26, and the asymmetry with pin above IS the decision. A
    # memory outlives its thread and silently shapes conversations the
    # user has not started yet, so it is persistent and invisible at the
    # moment it matters -- the same axis that separates write from read.
    #
    # Consequence: unreachable on any headless path, which includes every
    # research pass. That is deliberate (§21's consequence 1) and is
    # reinforced by the tool's GRANT_NEVER policy (M17, carried by R13),
    # because a grant would otherwise route around it.
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
    # so the gate is what makes "no silent overwrite" (§24 AC2) structural
    # rather than a promise the command makes about itself.
    write_project_doc: bool = True
    # §23 slice 2 (J12): UNGATED. Two reasons, and the second is the one
    # that decides it. Gating would mean approving a prompt in order to be
    # shown a prompt -- but worse, §13 does not merely deny a gated tool
    # where nothing can ask, it stops ADVERTISING it. Gated, this tool would
    # be invisible in every headless run; §24 AC2 requires it be visible,
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
