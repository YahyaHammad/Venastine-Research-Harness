from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID, uuid4
import json
import logging
import os

import credentials
from credentials import load_provider_data, no_providers_message, \
    unknown_provider_message
import config

# ROADMAP_v2 §33 W7 (#135). The three provider SDKs are NOT imported here.
#
# core.loop imports this module and main.py imports core.loop, so a module
# -scope `from openai import OpenAI` is paid by every invocation of either
# shell -- `--help`, `--summary`, a mistyped flag, the thread picker --
# before argparse runs. Measured on Windows: 5.6s to print --help, of
# which 3.5s was the three SDKs, and no run uses more than one of them.
#
# Each import therefore sits at the point of use, INSIDE the branch that
# needs it rather than at the top of the enclosing function: a
# GOOGLE-only name imported at the top of `_messages_for_provider` would
# be paid on every Anthropic turn, which is most of them, and would leave
# the defect in place while looking fixed.
#
# Deferred imports are otherwise not this file's idiom. The trade is
# stated here rather than left to be re-derived: `import` is cached after
# the first call, so the cost is a dict lookup per call on the hot path
# and seconds saved on every launch that never makes one.

logger = logging.getLogger(__name__)

# Caches initialized clients so repeated calls for the same provider don't
# rebuild the client (and re-read the credentials file) every time.
_client_cache: dict[str, object] = {}


def api_initialization(provider_name: str):
    if provider_name in _client_cache:
        return _client_cache[provider_name]

    provider_data = load_provider_data()

    # #24. The file's absence and the provider's absence are different
    # problems and now say so: the first names the path that was checked
    # and its remedy, the second names what IS configured, like /model's
    # refusal. Still ValueError either way -- callers (and tests) catch
    # the type, not the text. The constant is read THROUGH the module,
    # not imported: a from-import would freeze the value at this module's
    # import time and a test's monkeypatch of credentials.LLM_PROVIDERS_FILE
    # would not be seen here.
    if not provider_data and not os.path.exists(
            credentials.LLM_PROVIDERS_FILE):
        raise ValueError(no_providers_message())
    if provider_name not in provider_data:
        raise ValueError(unknown_provider_message(provider_name, provider_data))

    entry = provider_data[provider_name]

    if entry.get("is_v1_compatible", False):
        from openai import OpenAI
        client = OpenAI(base_url=entry["API_URL"], api_key=entry["API_KEY"])
    elif provider_name == "GOOGLE":
        from google import genai
        client = genai.Client(api_key=entry["API_KEY"])
    elif provider_name == "ANTHROPIC":
        from anthropic import Anthropic
        client = Anthropic(api_key=entry["API_KEY"])
        # Add further proprietary API formats here
    else:
        raise ValueError(f"Provider {provider_name} is not currently supported")

    _client_cache[provider_name] = client
    return client


# ============================================================================
# ---- Normalized response shape ---------------------------------------------
# ============================================================================

@dataclass
class ToolCallRequest:
    id: str
    name: str
    input: dict
    # §33 W2 (#37). Set when the provider's `arguments` string could not
    # be read as a JSON object -- most often because the response was cut
    # off mid-arguments by the token limit. TRANSPORT ONLY: it describes
    # one wire read, not the thread, so `add_assistant_message` persists
    # {id, name, input} exactly as before and this never reaches storage.
    #
    # The call is still constructed rather than dropped. The model made a
    # call and is owed an answer about it; a silently missing tool_result
    # is M4's pairing defect, and `input={}` with no error would run the
    # tool with arguments nobody sent.
    parse_error: Optional[str] = None


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    stop_reason: str = "complete"  # set by RunAgentLoop._run(), not here -- see core/loop.py
    thread_id: Optional[UUID] = None  # set by the public entry points (run_deep_research_mode,
                                      # continue_conversation) so callers can persist/retry
                                      # against the same thread. call_model() itself NEVER
                                      # sets this -- it has no thread concept.
    # §25: calls that proceeded on a PRE-FLIGHT grant rather than a live
    # approval. Set by _run() (like stop_reason), never by call_model().
    #
    # Authorization moved up-front, so accountability moves after: this is
    # the record of what a single launch-time yes actually authorised,
    # written into the run artifacts for a pipeline nobody was watching.
    # Empty for every run that granted nothing, which is every run today.
    granted_calls: list = field(default_factory=list)
    # ROADMAP_v2 §21: compaction notices raised during this run, each a
    # {"kind", "text"} dict. Set by _run() like stop_reason and
    # granted_calls, never by call_model().
    #
    # Carried here as well as yielded as a LoopEvent because
    # run_to_completion() discards non-final events: the TUI watches the
    # generator, but the CLI and the pipeline drain it, so a notice with
    # only the event route would be invisible in two shells out of three.
    notices: list = field(default_factory=list)
    # #4 (batch 27): the two per-turn figures the loop computes, set by
    # _run() like stop_reason -- never by call_model(), which has no turn
    # concept. UNPRICED APPROXIMATIONS, deliberately: raw provider counts,
    # no cache-read/write pricing and no currency anywhere. Precise spend
    # lives in the provider's dashboard; these exist for RELATIVE reasoning
    # ("is this turn ballooning the context?"), not billing truth.
    #
    # turn_billed_tokens: cumulative input+output over this user-turn so
    # far -- the spend meter's running total (the prompt is re-sent every
    # step, so this grows quadratically in a tool-using turn, which is
    # correct: that is what the provider bills).
    # turn_new_tokens: what THIS TURN added to the thread -- output plus
    # positive input deltas, the first call's inherited prompt excluded
    # and mid-turn compaction shrinks clamped away. This is the figure
    # anything reasoning about SIZE reads; the spend meter must never be
    # read that way again (TECHNICAL_DEBT item 9, closed batch 27).
    turn_billed_tokens: Optional[int] = None
    turn_new_tokens: Optional[int] = None


def _tool_arguments(raw_args: str, tool_name: str,
                    finish_reason: Optional[str] = None) -> tuple:
    """(input, parse_error) for one provider `arguments` string.

    The ONLY place a wire string is parsed as JSON. Google and Anthropic
    hand back dicts, so this is the OpenAI-compatible branch's alone --
    which is the branch a local model server takes, and truncated or
    malformed tool arguments are likeliest there.

    §33 W1 (#37): a failure is RETURNED, not raised. `dispatch()` turning
    handler exceptions into {"error": ...} is the whole reason a raising
    TOOL cannot kill a run; nothing played that role for a raising
    TRANSLATION one layer up, so a response cut mid-arguments took a
    JSONDecodeError out of this generator, out of _run(), and into the
    pipeline's `except Exception` -- recording status='failed' on a
    finished ten-pass run.

    §33 W4: `finish_reason` is what separates "the provider emitted bad
    JSON" from "the answer was cut off", which is the likeliest cause by
    far and the only one the model can act on.
    """
    # W9: no arguments is not an error. `get_time` declares properties=[]
    # and `pin` has no required parameters, so this is a live path -- and
    # json.loads("") raises. Dropping this fallback was green on the whole
    # suite when audit unit 3 ran mutation P10.
    if not raw_args:
        return {}, None

    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        if finish_reason == "length":
            return {}, (
                f"The arguments for {tool_name} were cut off by the response "
                f"token limit, so the call could not be made. Reissue it with "
                f"shorter arguments."
            )
        return {}, (
            f"The arguments for {tool_name} were not valid JSON ({exc.msg}), "
            f"so the call could not be made. Reissue it with valid JSON "
            f"arguments."
        )

    # Valid JSON that is not an object -- "null", "[1,2]", "3". Rarer than
    # a truncation and caught in the same place because the consequence is
    # the same: dispatch() would be handed something that is not params.
    if not isinstance(parsed, dict):
        return {}, (
            f"The arguments for {tool_name} parsed as "
            f"{type(parsed).__name__}, not a JSON object, so the call could "
            f"not be made. Reissue it with a JSON object."
        )

    return parsed, None


@dataclass
class StreamToken:
    """One token-level event yielded by call_model_stream(). Exactly one
    field is populated per yield: text_delta for incremental text,
    thinking_delta for incremental REASONING text, or final_response for
    the terminal ModelResponse (with accumulated tool calls and usage).

    §38 O1: thinking is DISPLAY-ONLY. It never joins the accumulated
    `text`, is never persisted, and is never sent back on the wire -- so
    the ModelResponse each branch produces is byte-identical to what it
    produced before thinking was captured, and D20's persisted turn and
    M4's fold boundary keep exactly the bytes they have today.

    Which providers actually populate it (§38 O2): Anthropic, and every
    OpenAI-compatible provider that streams `reasoning_content` or
    `reasoning` (DeepSeek, Qwen, Z.AI, OpenRouter). It stays empty on
    OPENAI -- its Chat Completions endpoint returns no reasoning text at
    all -- and on GOOGLE, whose request deliberately does not ask for
    thought summaries. Absence is not a bug there; see O2.
    """
    text_delta: Optional[str] = None
    thinking_delta: Optional[str] = None
    final_response: Optional[ModelResponse] = None


# ============================================================================
# ---- Tool schema translation ------------------------------------------------
# ============================================================================

def _tools_for_provider(provider_name: str, tool_schemas: list[dict]):
    """
    tool_schemas arrive in Anthropic's native shape (name/description/
    input_schema), since that's what every tool's TOOL_SCHEMA already
    produces. Translate into whatever shape the target provider expects.
    """
    if not tool_schemas:
        return None

    if provider_name == "ANTHROPIC":
        return tool_schemas

    if provider_name == "GOOGLE":
        from google.genai import types as genai_types
        return [genai_types.Tool(
            function_declarations=[
                genai_types.FunctionDeclaration(
                    name=s["name"],
                    description=s["description"],
                    parameters=s["input_schema"],
                )
                for s in tool_schemas
            ]
        )]

    # OpenAI-compatible providers
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["input_schema"],
            },
        }
        for s in tool_schemas
    ]


# ============================================================================
# ---- Message history translation -------------------------------------------
# ============================================================================
# core/memory.py stores history in a provider-neutral shape (see that
# file's docstring). This is the counterpart to _tools_for_provider above,
# applied to conversation history instead of tool schemas -- the ONLY
# place either provider's specific message wire format should exist.

def _is_tool_result_message(msg: dict) -> bool:
    return isinstance(msg.get("content"), list) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in msg["content"]
    )


def _is_empty_assistant_turn(msg: dict) -> bool:
    """An assistant turn carrying neither text nor tool calls (#13, #36).

    Produced when the model returns nothing at all -- a safety filter, a
    truncation, an empty pass response. `core/loop.py` persists it because
    D20 requires the assistant turn to be written before any stop-condition
    branching, so it reaches translation on every resume of that thread.

    It is meaningless on every provider, and each branch used to mangle it
    differently from the same neutral input:

        OPENAI:    {"role": "assistant", "content": null}   <- #13
        ANTHROPIC: {"role": "assistant", "content": []}     <- #36
        GOOGLE:    skipped entirely                         <- guarded

    Only Google was guarded, and only because Google was the provider being
    debugged when it surfaced. Dropping it above the branch split is
    AGENTS.md's *fix at the producer, not the consumer*: all three branches
    become correct at once, and so does the next one.

    Requiring BOTH halves to be empty is load-bearing. An empty text WITH
    tool calls is legitimate on the OpenAI schema -- a tool-call-only turn
    -- and `test_messages_openai_assistant_with_null_text` covers it, so a
    guard keyed on text alone breaks a valid case.
    """
    return (msg.get("role") == "assistant"
            and not msg.get("text")
            and not msg.get("tool_calls"))


def _messages_for_provider(provider_name: str, neutral_messages: list[dict]) -> list:
    # One rule, above the split (#13, #36). Note this can leave two
    # consecutive user turns -- which is the shape Google's branch has
    # always produced here, and which
    # `test_messages_google_empty_assistant_turn_skipped` has always
    # asserted.
    neutral_messages = [m for m in neutral_messages
                        if not _is_empty_assistant_turn(m)]

    if provider_name == "GOOGLE":
        from google.genai import types as genai_types
        # Build a {tool_call_id: function_name} lookup from assistant
        # messages so we can populate FunctionResponse.name (required by
        # Google but absent from the neutral tool-result shape).
        call_id_to_name: dict[str, str] = {}
        for msg in neutral_messages:
            if msg["role"] == "assistant":
                for tc in msg.get("tool_calls", []):
                    call_id_to_name[tc["id"]] = tc["name"]

        translated = []
        for msg in neutral_messages:
            role = msg["role"]

            if role == "user":
                translated.append(genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=msg["content"])],
                ))

            elif role == "assistant":
                parts = []
                if msg.get("text"):
                    parts.append(genai_types.Part(text=msg["text"]))
                for tc in msg.get("tool_calls", []):
                    parts.append(genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            name=tc["name"],
                            args=tc["input"],
                            id=tc["id"],
                        )
                    ))
                # Google rejects Content with empty parts. The turn that
                # produced them is now dropped above the branch split, so
                # this branch no longer needs its own guard -- one rule
                # rather than two that can disagree (#36).
                translated.append(genai_types.Content(role="model", parts=parts))

            elif role == "tool":
                name = call_id_to_name.get(msg["tool_call_id"])
                if name is None:
                    raise ValueError(
                        f"Tool result references tool_call_id={msg['tool_call_id']!r} "
                        f"but no assistant message in the history made that call"
                    )
                part = genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        name=name,
                        response={"result": msg["content"]},
                    )
                )
                # Batch consecutive tool results into a single user
                # Content — Google requires strict user/model alternation.
                if (translated
                        and translated[-1].role == "user"
                        and all(p.function_response is not None for p in translated[-1].parts)):
                    translated[-1].parts.append(part)
                else:
                    translated.append(genai_types.Content(role="user", parts=[part]))

        return translated

    if provider_name == "ANTHROPIC":
        translated: list[dict] = []
        for msg in neutral_messages:
            role = msg["role"]

            if role == "user":
                translated.append({"role": "user", "content": msg["content"]})

            elif role == "assistant":
                content = []
                if msg.get("text"):
                    content.append({"type": "text", "text": msg["text"]})
                for tc in msg.get("tool_calls", []):
                    content.append({
                        "type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"],
                    })
                translated.append({"role": "assistant", "content": content})

            elif role == "tool":
                # Anthropic wants every tool result from one turn batched
                # into a SINGLE user message with multiple tool_result
                # blocks, not one message per result -- merge consecutive
                # tool-role entries into the same message rather than
                # appending a new one each time.
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg["content"],
                }
                if translated and translated[-1]["role"] == "user" and _is_tool_result_message(translated[-1]):
                    translated[-1]["content"].append(block)
                else:
                    translated.append({"role": "user", "content": [block]})

        return translated

    # OpenAI-compatible: close to 1:1, each tool result is its own message
    translated = []
    for msg in neutral_messages:
        role = msg["role"]

        if role == "user":
            translated.append({"role": "user", "content": msg["content"]})

        elif role == "assistant":
            entry: dict = {"role": "assistant", "content": msg.get("text") or None}
            if msg.get("tool_calls"):
                entry["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["input"])},
                    }
                    for tc in msg["tool_calls"]
                ]
            translated.append(entry)

        elif role == "tool":
            translated.append({
                "role": "tool", "tool_call_id": msg["tool_call_id"], "content": msg["content"],
            })

    return translated


# ============================================================================
# ---- The actual call --------------------------------------------------------
# ============================================================================

# ============================================================================
# ---- Sampling + reasoning-effort translation (ROADMAP_v2 §16) ---------------
# ============================================================================
#
# Both helpers below are the same "translate at the boundary, once" case that
# _tools_for_provider / _messages_for_provider exist for: the three providers
# express these concepts differently enough that no caller should have to know.

# (provider_name, model) -> supported effort levels. Populated lazily; the
# Anthropic branch queries the API once per process rather than per call.
_effort_levels_cache: dict[tuple[str, str], list[str]] = {}

# (provider_name, model) -> context window in tokens, or None for "asked,
# and this provider does not report one". Same lazy, once-per-process
# posture as the effort cache above; see context_window_for() for why None
# is cached and a raised query is not.
_context_window_cache: dict[tuple[str, str], Optional[int]] = {}

# (provider_name, model) -> how many times the query has RAISED.
#
# The middle ground between #35's two bad options. Caching a failure makes
# one transient error suppress the query for the life of the process;
# never caching one means a provider with no /models endpoint at all --
# Grok, Fireworks, Z.AI -- is asked again on every turn, which across a
# ten-pass research run is ten failing HTTP calls to learn the same thing.
# So a failure is retried, and after this many it settles on the table and
# says so once. Transient blips still recover; a permanent 404 costs three
# calls per process, not one per turn.
_context_window_failures: dict[tuple[str, str], int] = {}
MAX_CONTEXT_WINDOW_QUERY_ATTEMPTS = 3


def _sampling_kwargs(provider_name: str, model: str, temperature) -> dict:
    """Sampling parameters for this call, or {} when the model rejects them.

    Current Anthropic models removed temperature/top_p/top_k outright (any
    value returns a 400) and Sonnet 5 rejects non-default values. Sending
    one anyway is not a degraded request, it is a failed one -- which is how
    ROADMAP §10's ensemble mode came to be built, documented as working, and
    incapable of running against this harness's own default model.

    Dropping the parameter is logged at WARNING rather than done silently.

    THIS FUNCTION NOW HAS NO CALLER THAT DEPENDS ON IT, and the warning is
    what makes that safe. Ensemble mode used to be the one, and
    orchestrator.py carried a guard refusing to run it on a model in the set
    below. §10's revisit replaced sampling variation with a roster of
    different models, so the guard is gone and nothing in the harness passes
    a temperature any more.

    Kept rather than removed: `temperature` still threads through
    core/loop.py, this is still the correct boundary translation for it, and
    the next caller to want sampling variation needs to be told rather than
    silently ignored. If this warning ever fires, something new is relying on
    sampling variation without knowing it cannot have it -- which is exactly
    how §10 came to be built, documented as working, and incapable of running
    against this harness's own default model.
    """
    if temperature is None:
        return {}
    if model in config.MODELS_REJECTING_SAMPLING_PARAMS:
        logger.warning(
            "Dropping temperature=%s: model %r rejects sampling parameters. "
            "Steer via prompting or reasoning effort instead.",
            temperature, model,
        )
        return {}
    return {"temperature": temperature}


def effort_for(client, provider_name: str, model: str,
               requested: Optional[str]) -> Optional[str]:
    """The effort level to actually SEND to (provider_name, model), or None.

    One validator for every send site, because nothing else checked a
    level against the model that would receive it. Three ways a wrong one
    used to reach the wire, all of them 400ing every turn until the user
    intervened by hand:

      * a persisted settings.json `tui.effort` was trusted as-is;
      * the TUI's current level was inherited by an agent that declares
        its OWN model/provider, so a level validated for one model was
        sent to a different one;
      * nothing revalidated after a /model or agent switch.

    `effort=None` sends nothing on every provider and is the one
    universally safe value, so dropping is always available as an answer.

    On a FAILED lookup the requested level is passed through unverified
    with a warning rather than dropped: a transient network blip must not
    silently downgrade a deliberate choice. A genuinely wrong level still
    fails at the provider, which is the honest outcome.
    """
    if not requested:
        return None
    levels, authoritative = _effort_levels(client, provider_name, model)
    if requested in levels:
        return requested
    if not authoritative:
        logger.warning(
            "Could not verify effort %r for %s/%s; sending it unverified.",
            requested, provider_name, model)
        return requested
    logger.warning(
        "Dropping effort %r: %s/%s accepts %s. Sending no effort parameter.",
        requested, provider_name, model, levels or "no effort control")
    return None


def effort_levels_for_model(client, provider_name: str, model: str) -> list[str]:
    """Reasoning-effort levels this model accepts, most-to-least capable last.

    Anthropic is queried, not tabulated: its Models API reports per-model
    effort support (`capabilities.effort.<level>.supported`), so a newly
    released Anthropic model needs no manual entry here. Every other provider
    is tabulated because there is nothing to ask -- no OpenAI-compatible
    provider in providers.json exposes a capability endpoint, and Google
    expresses depth as an integer budget rather than a level set at all.

    Falling back logs at WARNING, matching §21's MODEL_CONTEXT_WINDOWS
    posture: a static table is honest about being incomplete, but it should
    say so out loud when it is the thing answering.

    Returns [] when the model supports no effort control, which callers must
    treat as "offer no picker and send no effort parameter".
    """
    return _effort_levels(client, provider_name, model)[0]


def _effort_levels(client, provider_name: str, model: str) -> tuple:
    """(levels, authoritative).

    `authoritative` is False when the answer came from the static table
    after the capability QUERY failed, as opposed to because no query was
    ever possible for that provider. Only effort_for() needs the
    distinction -- an unverified answer must not be used to drop a level
    the user deliberately chose.
    """
    key = (provider_name, model)
    if key in _effort_levels_cache:
        return _effort_levels_cache[key], True

    levels: Optional[list[str]] = None
    query_failed = False
    if provider_name == "GOOGLE" and not _google_supports_thinking_budget():
        # Reporting no levels is the honest answer, not a silent [] : on the
        # pinned SDK there is no field to put a budget in, so a picker
        # offering levels would be offering something unsendable.
        _effort_levels_cache[key] = []
        return [], True
    if provider_name == "ANTHROPIC":
        try:
            # ATTRIBUTES, not subscripts (#35). This read used to be
            # `capabilities["effort"]` with `effort.get(name)` and
            # `isinstance(..., dict)`. On the exactly-pinned SDK those are
            # pydantic models -- ModelCapabilities, EffortCapability,
            # CapabilitySupport -- supporting neither [] nor .get(), so the
            # TypeError was caught below, query_failed was set, and the
            # query FELL BACK EVERY SINGLE TIME. Not "failed sometimes":
            # it could not succeed. All three dict spellings were wrong,
            # so correcting the subscript alone would have failed one line
            # further down.
            #
            # No dict/attribute dual path on purpose. A defensive default
            # is only safe where absence is impossible, requirements.txt
            # pins anthropic exactly, and test_sdk_conformance.py exists
            # to go red when the pinned shape moves -- which is a better
            # signal than a fallback that silently keeps guessing.
            info = client.models.retrieve(model)
            # ONE retrieve, TWO readings (§21's revisit). The context
            # window is on this SAME ModelInfo as `max_input_tokens`, so
            # recording it here IS the entire Anthropic half of the
            # window query: no second call, no second failure mode, and
            # it lands on the path every send already crosses
            # (core/loop.py calls effort_for before the wire). §21
            # rejected a window query on the belief that there was
            # nothing to ask; for this provider the answer was already
            # in hand and being dropped. See context_window_for below.
            _record_window(provider_name, model,
                           getattr(info, "max_input_tokens", None))
            capabilities = info.capabilities
            effort = capabilities.effort
            levels = [
                name for name in ("low", "medium", "high", "xhigh", "max")
                # getattr, because `xhigh` is Optional on the pinned type
                # and a model that does not offer a level reports None
                # rather than supported=False.
                if getattr(getattr(effort, name, None), "supported", False)
            ]
        except Exception as e:
            # Network failure, an SDK too old to expose capabilities, or a
            # model the endpoint doesn't know. Fall through to the table.
            query_failed = True
            logger.warning(
                "Could not read effort capabilities for %r from the Anthropic "
                "Models API (%s); falling back to the static table.", model, e,
            )

    if levels is None:
        levels = config.MODEL_EFFORT_LEVELS.get(model)
        if levels is None:
            logger.warning(
                "No effort levels known for %r on %s; assuming %s. Add an entry "
                "to config.MODEL_EFFORT_LEVELS if this is wrong.",
                model, provider_name, config.DEFAULT_EFFORT_LEVELS,
            )
            levels = list(config.DEFAULT_EFFORT_LEVELS)

    if query_failed:
        # Do NOT cache a failure-derived guess. Caching it made one
        # transient error permanently suppress the query that is this
        # function's entire reason to exist: for the whole process life
        # the picker would offer a guessed level set, and every selection
        # from it could 400 with no path back short of a restart.
        return levels, False

    _effort_levels_cache[key] = levels
    return levels, True


# The field names a provider might report a context window under, in the
# order they are tried. ONE reader covers every OpenAI-compatible provider
# rather than one adapter each, because openai.types.Model is declared
# `extra="allow"` -- a field the SDK does not model survives on the parsed
# object and reaches `.model_extra`. Verified against the pinned openai
# 2.45.0 in test_sdk_conformance.py, because that permissiveness is the
# whole reason this is one function.
#
#   context_window      Groq
#   context_length      Together, OpenRouter, Cohere (native /v1 only)
#   max_context_length  Mistral
#   max_input_tokens    Anthropic's spelling, accepted here so a gateway
#                       proxying Anthropic through an OpenAI-shaped API
#                       is not silently unreadable
#
# OpenAI, DeepSeek and Perplexity report NONE of these -- they answer with
# id/created/object/owned_by and nothing else. That is not a failure, it is
# the answer, and it is why config.MODEL_CONTEXT_WINDOWS still exists.
_WINDOW_FIELD_ALIASES = (
    "context_window",
    "context_length",
    "max_context_length",
    "max_input_tokens",
)

#: Returned by _model_info when a COMPLETE listing did not contain the model.
#: Distinct from a raise: the provider answered, so there is nothing to retry.
_NOT_LISTED = object()


def _window_from_model_object(info) -> Optional[int]:
    """The context window off one provider model object, or None.

    Declared fields first, then model_extra -- the same object may carry
    either depending on whether the installed SDK models the field.
    ATTRIBUTES not subscripts, for #35's reason: these are pydantic objects
    on every pinned SDK here.

    `top_provider.context_length` is the one NESTED spelling, and it is
    OpenRouter's: the flat `context_length` is the model's maximum across
    every backend, while top_provider carries the routed one. Read second,
    so the flat value still wins where both exist -- this is a fallback for
    entries that carry only the nested form, not a preference for the
    narrower number.
    """
    extra = getattr(info, "model_extra", None) or {}
    for name in _WINDOW_FIELD_ALIASES:
        value = getattr(info, name, None)
        if value is None:
            value = extra.get(name)
        if value is not None:
            return value
    top = extra.get("top_provider")
    if isinstance(top, dict):
        return top.get("context_length")
    return None


def _model_info(client, model):
    """The provider's object for one model, via retrieve or via the listing.

    THE ROUTE WAS THE BUG, NOT THE READER (batch 44). This asked
    `client.models.retrieve(model)` alone, which is `GET {base}/models/{id}`
    -- and OpenRouter implements `GET /api/v1/models` plus
    `/models/:author/:slug/endpoints` and no plain per-id retrieve, so an id
    containing a slash (`minimax/minimax-m3`) turns the path into a route
    that does not exist. 404, every time, on a provider whose listing has
    carried `context_length` all along and which _WINDOW_FIELD_ALIASES'
    own comment names. NVIDIA's endpoint has the same shape, and so does
    every other provider whose ids are `vendor/model`.

    Retrieve stays FIRST: it is one call and it is the right one for Groq,
    Together and Mistral, and a listing can be hundreds of entries. The
    fallback costs one extra call per provider per process, on the path
    that was previously three failures and a permanent give-up.

    Three outcomes, deliberately distinct:
      an object     something answered
      _NOT_LISTED   a complete listing came back without this model, which
                    is an ANSWER ("this provider has nothing to say about
                    it") and must not be retried like a network blip
      raises        neither route could answer; the caller counts it
    """
    try:
        return client.models.retrieve(model)
    except Exception as retrieve_error:
        listing = getattr(getattr(client, "models", None), "list", None)
        if listing is None:
            raise
        try:
            entries = list(listing())
        except Exception:
            # The listing is not a second opinion about the retrieve; it is
            # the same question by another route. Report the FIRST failure,
            # which is the one that names the endpoint the caller asked for.
            raise retrieve_error from None
        for entry in entries:
            if getattr(entry, "id", None) == model:
                return entry
        if not entries:
            # An empty listing answered nothing at all; report the failure
            # that names the endpoint the caller actually asked for.
            raise retrieve_error from None
        return _NOT_LISTED


def _record_window(provider_name: str, model: str, value) -> Optional[int]:
    """Cache a queried window, coercing anything unusable to None.

    A POSITIVE int or nothing. The Anthropic Models API types
    `max_input_tokens` as nullable and its own documented example carries
    a placeholder 0, so "present" is not "usable" -- and a 0 reaching
    compaction would make `context_limit - backstop` clamp to the floor
    and compact continuously. Anything that is not a positive int is
    recorded as None, which means "asked, got no usable answer" and sends
    the caller to the static table.
    """
    window = value if isinstance(value, int) and not isinstance(value, bool) else None
    if window is not None and window <= 0:
        window = None
    _context_window_cache[(provider_name, model)] = window
    return window


def known_context_window(provider_name: str, model: str) -> Optional[int]:
    """The queried window for (provider, model), or None if none is known.

    CACHE ONLY -- this never makes a call, which is what lets
    core/compaction.py ask the question without holding a client or
    importing an SDK. The cache is primed by context_window_for() on the
    path core/loop.py already crosses before every send, so by the time a
    compaction check runs the answer is there if the provider had one.
    """
    return _context_window_cache.get((provider_name, model))


def context_window_for(client, provider_name: str, model: str) -> tuple:
    """(window_or_None, authoritative). Queries the provider once per process.

    §21 rejected this as needing "fifteen adapters" for a roster where no
    uniform endpoint existed. Re-measured against the PINNED SDKs, that
    premise did not hold, and the shape below is what it actually costs:

      ANTHROPIC   already answered. _effort_levels retrieves the ModelInfo
                  to read capabilities.effort and max_input_tokens rides
                  along on it, so this branch is usually a cache hit and
                  makes no call at all.
      GOOGLE      models.get(...).input_token_limit, one field on the
                  pinned google-genai type.
      v1-compat   ONE alias sniff over model_extra, not thirteen adapters,
                  because openai.types.Model allows extra fields -- over
                  whichever of models.retrieve() and models.list() answers
                  (see _model_info: providers whose ids carry a slash have
                  no per-id route, so retrieve 404s on them).

    §21's CONCLUSION survives intact and is why the table remains: OpenAI,
    DeepSeek and Perplexity report nothing, so a fallback constant is
    still needed at the end of it.

    THE FAILURE/SILENCE SPLIT IS THE POINT, and it is _effort_levels'
    query_failed rule (#35's lesson) applied to a second query. A call
    that RAISED is warned and NOT cached: a transient network error must
    not permanently suppress the query for the life of the process. A
    call that SUCCEEDED and reported no window IS cached as None, because
    that is a real and stable answer, and re-asking it on every
    compaction evaluation would spend a request per step to learn the
    same thing.
    """
    key = (provider_name, model)
    if key in _context_window_cache:
        return _context_window_cache[key], True

    try:
        if provider_name == "GOOGLE":
            info = client.models.get(model=model)
            return _record_window(
                provider_name, model,
                getattr(info, "input_token_limit", None)), True
        info = _model_info(client, model)
        if info is _NOT_LISTED:
            # Asked and answered by a listing that did not name it.
            return _record_window(provider_name, model, None), True
        window = _window_from_model_object(info)
        if window is not None:
            return _record_window(provider_name, model, window), True
        # Asked and answered: this provider reports no window.
        return _record_window(provider_name, model, None), True
    except Exception as e:
        # Network failure, a provider with no models endpoint at all, or a
        # model the endpoint does not know. Not cached as an ANSWER -- see
        # the docstring -- but counted, so a permanent failure stops being
        # re-asked once it has proved it is not a blip.
        attempts = _context_window_failures.get(key, 0) + 1
        _context_window_failures[key] = attempts
        giving_up = attempts >= MAX_CONTEXT_WINDOW_QUERY_ATTEMPTS
        logger.warning(
            "Could not read a context window for %r from %s (%s); "
            "config.MODEL_CONTEXT_WINDOWS will answer instead.%s",
            model, provider_name, e,
            " Not asking again this session." if giving_up else "")
        if giving_up:
            _record_window(provider_name, model, None)
        return None, False


_google_budget_support: Optional[bool] = None


def _google_supports_thinking_budget() -> bool:
    """Whether the INSTALLED google-genai can express a thinking budget.

    Checked rather than assumed, and the check earns its keep: the pinned
    google-genai (1.0.0) ships a ThinkingConfig whose only field is
    `include_thoughts` -- `thinking_budget` arrived in a later release. So
    the obvious implementation, ThinkingConfig(thinking_budget=N), raises a
    TypeError against the version this repo actually pins.

    Rather than bump the pin (which would put §9's verified Google
    implementation back in play for a feature Google users can do without),
    reasoning effort is simply unavailable on Google until the pin moves.
    D22's rule, applied: check the dependency's real shape, don't assume it.
    """
    from google.genai import types as genai_types

    global _google_budget_support
    if _google_budget_support is None:
        fields = getattr(genai_types.ThinkingConfig, "model_fields", {})
        _google_budget_support = "thinking_budget" in fields
        if not _google_budget_support:
            logger.info(
                "Installed google-genai has no ThinkingConfig.thinking_budget; "
                "reasoning effort is unavailable on GOOGLE until the pin moves."
            )
    return _google_budget_support


def _thinking_for_provider(provider_name: str, effort: Optional[str]) -> dict:
    """Reasoning-effort kwargs for this provider, or {} when no effort is set.

    Effort is opt-in: `effort=None` sends nothing at all and every provider
    behaves exactly as it did before §16. That matters because the parameter
    is not universally safe -- `reasoning_effort` is rejected by
    OpenAI-compatible NON-reasoning models, and `thinking: {"type": "adaptive"}`
    is rejected by pre-4.6 Anthropic models. Callers are expected to offer
    only the levels effort_levels_for_model() reported, and to offer nothing
    when it reported none.

    Returned keys are merged into the provider's own kwargs by the caller;
    the Google entry is nested inside GenerateContentConfig rather than sent
    top-level, matching how that provider takes every other setting.
    """
    if not effort:
        return {}
    if provider_name == "ANTHROPIC":
        # Adaptive thinking + an effort level is the current-model pairing;
        # budget_tokens was removed and returns a 400.
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
    if provider_name == "GOOGLE":
        from google.genai import types as genai_types
        budget = config.GOOGLE_THINKING_BUDGETS.get(effort)
        if budget is None or not _google_supports_thinking_budget():
            return {}
        return {"thinking_config": genai_types.ThinkingConfig(thinking_budget=budget)}
    return {"reasoning_effort": effort}


# ============================================================================
# ---- Streaming call + helpers -----------------------------------------------
# ============================================================================

def call_model_stream(
    client,
    provider_name: str,
    model: str,
    messages: list[dict],
    system_prompt: str,
    tool_schemas: list[dict],
    temperature: Optional[float] = None,
    effort: Optional[str] = None,
):
    """The one model call, for every provider. Yields StreamToken
    events: text_delta for incremental text, thinking_delta for
    incremental reasoning text where the provider sends any (§38 O2),
    final_response for the terminal ModelResponse with accumulated tool
    calls and usage.

    Three provider implementations — Anthropic typed events, Google
    chunked candidates, OpenAI-compatible index-accumulated tool-call
    fragments — which must agree with each other about the ModelResponse
    they produce for the same inputs.

    There used to be a non-streaming `call_model()` beside this, and this
    docstring used to promise the two were identical. They were not: it
    set ModelResponse.raw and no streaming branch did, which is what a
    second implementation nobody runs accumulates (#38). It is gone, and
    with it `raw`; the only thing left to keep in step is the three
    branches below.

    D21 (usage-or-raise): if the provider is marked as supporting stream
    usage but the stream completes with zero usage, raise rather than
    silently reporting zero (which disables budget enforcement and §21's
    compaction trigger).
    """
    translated_messages = _messages_for_provider(provider_name, messages)
    sampling = _sampling_kwargs(provider_name, model, temperature)
    thinking = _thinking_for_provider(provider_name, effort)

    # Read once so every branch applies the same D21 usage-or-raise rule --
    # all three of them, since audit #40. This comment used to claim that in
    # its first sentence and take it back in the second ("the flag only gates
    # the Google and OpenAI-compatible branches"), which left the one
    # provider whose example config ships the flag `true` as the one provider
    # where setting it did nothing.
    provider_data = load_provider_data()
    supports_stream_usage = provider_data.get(provider_name, {}).get(
        "supports_stream_usage", False
    )

    if provider_name == "ANTHROPIC":
        stream_kwargs = dict(
            model=model,
            max_tokens=config.MAX_TOKENS,
            system=system_prompt,
            messages=translated_messages,
            tools=_tools_for_provider(provider_name, tool_schemas),
        )
        stream_kwargs.update(sampling)
        stream_kwargs.update(thinking)

        with client.messages.stream(**stream_kwargs) as stream:
            # §38 O3. Iterating the STREAM rather than stream.text_stream,
            # because text_stream yields text deltas only and there is no
            # way to have both: `text_stream` is an instance attribute
            # built from the same underlying iterator in MessageStream's
            # __init__, so consuming one consumes the other.
            #
            # The SDK emits its own synthetic events beside the raw ones --
            # TextEvent(type="text", text=<delta>) and
            # ThinkingEvent(type="thinking", thinking=<delta>) -- which is
            # why this reads two attribute names rather than unpacking
            # content_block_delta by hand. Verified against the pinned
            # anthropic==0.116.0 rather than assumed (D22), and pinned in
            # tests/test_sdk_conformance.py.
            #
            # Every other event kind is ignored on purpose: `text` is the
            # accumulated answer read off get_final_message() below, not
            # off these deltas, so nothing here can change what this
            # branch returns.
            for ev in stream:
                kind = getattr(ev, "type", None)
                if kind == "text":
                    yield StreamToken(text_delta=ev.text)
                elif kind == "thinking":
                    yield StreamToken(thinking_delta=ev.thinking)
            final_msg = stream.get_final_message()
            text = "\n".join(
                b.text for b in final_msg.content if b.type == "text"
            )
            calls = [
                ToolCallRequest(id=b.id, name=b.name, input=b.input)
                for b in final_msg.content if b.type == "tool_use"
            ]
            usage_obj = getattr(final_msg, "usage", None)
            usage = {
                "input_tokens": getattr(usage_obj, "input_tokens", 0) if usage_obj else 0,
                "output_tokens": getattr(usage_obj, "output_tokens", 0) if usage_obj else 0,
            }

        # D21 parity with the other two branches (audit #40). This branch
        # used to be exempt on the reasoning that get_final_message() always
        # carries usage -- which is very likely right for anthropic==0.116.0,
        # and is exactly the kind of assumption D21 exists because nobody can
        # verify from inside. The getattr defaults above are the softened
        # form D21's own note forbids, so on the DEFAULT provider an SDK
        # field rename would have zeroed the budget silently, disabling both
        # the budget stop condition and §21's compaction trigger. That is the
        # mcp v1->v2 failure mode, and #35 is this file's live example of it.
        #
        # Honouring the flag rather than always raising keeps it meaning ONE
        # thing on all three providers, which is the coherence #40 is about;
        # providers.json.example ships it true for ANTHROPIC, so the default
        # posture is the checked one, and a proxy that genuinely omits usage
        # can still opt out instead of being unable to run.
        if supports_stream_usage and usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
            raise RuntimeError(
                f"Streaming call to {provider_name} completed without usage "
                f"data despite supports_stream_usage=True. The stream may "
                f"have been interrupted before the final usage chunk."
            )

        yield StreamToken(final_response=ModelResponse(
            text=text, tool_calls=calls, usage=usage,
        ))
        return

    if provider_name == "GOOGLE":
        from google.genai import types as genai_types
        genai_config_kwargs = dict(
            system_instruction=system_prompt,
            tools=_tools_for_provider(provider_name, tool_schemas),
            max_output_tokens=config.MAX_TOKENS,
        )
        genai_config_kwargs.update(sampling)
        genai_config_kwargs.update(thinking)

        text_parts = []
        calls = []
        usage = {"input_tokens": 0, "output_tokens": 0}

        for chunk in client.models.generate_content_stream(
            model=model,
            contents=translated_messages,
            config=genai_types.GenerateContentConfig(**genai_config_kwargs),
        ):
            candidate = chunk.candidates[0] if chunk.candidates else None
            content = candidate.content if candidate else None
            parts = content.parts if content else []
            for p in parts:
                if p.text is not None:
                    text_parts.append(p.text)
                    yield StreamToken(text_delta=p.text)
                if p.function_call is not None:
                    fc = p.function_call
                    calls.append(ToolCallRequest(
                        id=fc.id or str(uuid4()),
                        name=fc.name,
                        input=fc.args or {},
                    ))
            usage_meta = getattr(chunk, "usage_metadata", None)
            if usage_meta:
                usage["input_tokens"] = getattr(usage_meta, "prompt_token_count", 0)
                usage["output_tokens"] = getattr(usage_meta, "candidates_token_count", 0)

        # D21 parity with the OpenAI branch: raise if marked as supporting
        # stream usage but the stream completed with zero usage.
        if supports_stream_usage and usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
            raise RuntimeError(
                f"Streaming call to {provider_name} completed without usage "
                f"data despite supports_stream_usage=True. The stream may "
                f"have been interrupted before the final usage chunk."
            )

        yield StreamToken(final_response=ModelResponse(
            text="".join(text_parts), tool_calls=calls, usage=usage,
        ))
        return

    # OpenAI-compatible path (supports_stream_usage already read above)
    full_messages = [{"role": "system", "content": system_prompt}] + translated_messages
    openai_kwargs = dict(
        model=model,
        messages=full_messages,
        max_completion_tokens=config.MAX_TOKENS,
        tools=_tools_for_provider(provider_name, tool_schemas),
        stream=True,
    )
    openai_kwargs.update(sampling)
    openai_kwargs.update(thinking)
    if supports_stream_usage:
        openai_kwargs["stream_options"] = {"include_usage": True}

    text_parts = []
    tool_fragments: dict[int, dict] = {}
    usage = {"input_tokens": 0, "output_tokens": 0}

    finish_reason = None
    for chunk in client.chat.completions.create(**openai_kwargs):
        if chunk.choices:
            # W4 (#37): free -- chunk.choices[0] is already read here. The
            # provider sets this on the last content chunk, and `or
            # finish_reason` keeps it once seen because the usage chunk
            # that follows carries none. getattr because a chunk is only
            # required to have `delta`.
            choice = chunk.choices[0]
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            delta = choice.delta
            if delta.content:
                text_parts.append(delta.content)
                yield StreamToken(text_delta=delta.content)
            # §38 O3. Reasoning text, where the provider sends any. There
            # is no standard field: DeepSeek, Qwen and Z.AI use
            # `reasoning_content`, OpenRouter and Groq use `reasoning`,
            # and OpenAI's own endpoint sends neither. openai's ChoiceDelta
            # is extra="allow" (pinned in test_sdk_conformance.py), so an
            # unknown-to-the-SDK field arrives as a real attribute.
            #
            # NEVER appended to text_parts: reasoning is not the answer,
            # and folding it in would change every ModelResponse this
            # branch returns, put thinking into the persisted thread, and
            # send it back on the next turn's wire.
            reasoning = (getattr(delta, "reasoning_content", None)
                         or getattr(delta, "reasoning", None))
            if isinstance(reasoning, str) and reasoning:
                yield StreamToken(thinking_delta=reasoning)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_fragments:
                        tool_fragments[idx] = {
                            "id": "", "name_parts": [], "arguments_parts": [],
                        }
                    if tc_delta.id:
                        tool_fragments[idx]["id"] = tc_delta.id
                    # Accumulate name like arguments: some v1-compatible
                    # providers split function.name across deltas, so a
                    # last-write-wins assignment would corrupt the name.
                    if tc_delta.function and tc_delta.function.name:
                        tool_fragments[idx]["name_parts"].append(
                            tc_delta.function.name
                        )
                    if tc_delta.function and tc_delta.function.arguments:
                        tool_fragments[idx]["arguments_parts"].append(
                            tc_delta.function.arguments
                        )
        usage_obj = getattr(chunk, "usage", None)
        if usage_obj:
            usage["input_tokens"] = getattr(usage_obj, "prompt_tokens", 0)
            usage["output_tokens"] = getattr(usage_obj, "completion_tokens", 0)

    # D21: if the provider is marked as supporting stream usage but we
    # got zero usage, the stream was likely interrupted before the final
    # usage chunk arrived. Raise rather than silently reporting zero.
    if supports_stream_usage and usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
        raise RuntimeError(
            f"Streaming call to {provider_name} completed without usage "
            f"data despite supports_stream_usage=True. The stream may "
            f"have been interrupted before the final usage chunk."
        )

    calls = []
    for idx in sorted(tool_fragments):
        frag = tool_fragments[idx]
        name = "".join(frag["name_parts"])
        parsed, parse_error = _tool_arguments(
            "".join(frag["arguments_parts"]), name, finish_reason)
        if parse_error is not None:
            logger.warning(
                "%s: %s (finish_reason=%r)", name, parse_error, finish_reason)
        calls.append(ToolCallRequest(
            id=frag["id"], name=name, input=parsed, parse_error=parse_error,
        ))

    yield StreamToken(final_response=ModelResponse(
        text="".join(text_parts), tool_calls=calls, usage=usage,
    ))
