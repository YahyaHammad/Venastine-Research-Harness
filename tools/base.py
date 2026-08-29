from dataclasses import dataclass
from typing import Callable, Iterable, Optional


# ROADMAP_v2 §25 (R13). How far approving a tool BY NAME goes -- the
# question asked before any call exists, by the §18 subagent sign-off and
# by the §25 pipeline grant.
#
# Three values rather than two booleans. Booleans can express "an
# unattended pipeline run may pre-grant this, but a human answering in the
# moment may not", which is incoherent -- the pipeline is strictly
# stricter than the sign-off, so the answers form an order.
GRANT_ANYWHERE = "anywhere"
GRANT_SIGNOFF_ONLY = "signoff_only"
GRANT_NEVER = "never"

GRANT_POLICIES = frozenset({GRANT_ANYWHERE, GRANT_SIGNOFF_ONLY, GRANT_NEVER})


# ROADMAP_v2 §31 (H1). What a call to this tool is allowed to COST, and
# therefore which mechanism bounds it. Asked once, at registration, for
# the same reason grant_policy is: the answer is a property of the tool,
# and a layer that infers it from a name or a module is a second source
# of truth that drifts the moment a tool is added.
#
#   BUDGET_COMPUTE  A pure function of its params. Nothing bounds it from
#                   the INSIDE -- sympy has no interruption points -- so
#                   dispatch runs it in a killable subprocess under
#                   config.TOOL_COMPUTE_TIMEOUT_S (H2).
#   BUDGET_IO       Carries its own bound already: a request timeout, a
#                   size check before opening, a sandbox, or an operation
#                   bounded by construction. NOT wrapped by dispatch,
#                   because an outer clock over a blocking read can only
#                   stop WAITING -- mcp_client says so about its own
#                   backstop, and a mechanism that reports a timeout while
#                   the work continues is worse than none (H7).
#   BUDGET_HUMAN    Blocks on a person, or on a sub-run metered
#                   separately. ask_user waits up to
#                   ATTENDED_APPROVAL_TIMEOUT_S; spawn_subagent is bounded
#                   by the child's max_steps and the token budget. A
#                   compute clock over either would be a bug, not a
#                   tightening (H8).
BUDGET_COMPUTE = "compute"
BUDGET_IO = "io"
BUDGET_HUMAN = "human"

BUDGETS = frozenset({BUDGET_COMPUTE, BUDGET_IO, BUDGET_HUMAN})


@dataclass
class ToolSpec:
    name: str  # Tool name
    schema: dict  # Tool schema
    handler: Callable[[dict], dict]  # The actual function the tool runs
    # Optional path-dependent approval policy. When present, its result is
    # OR'd with the generic requires_approval() lookup by
    # registry.approval_needed(). Signature: (tool_name, params) -> bool.
    # Used by file_ops to auto-approve within WORKSPACE_DIR and require
    # user approval outside it, and by shell for Docker-availability.
    #
    # ROADMAP_v2 §15 changed this from a REPLACEMENT for requires_approval
    # to one term of an OR. Before, defining an approval_check made a tool
    # invisible to config/context-level approval settings entirely, which
    # would have silently voided agent approval_overrides for read/write/
    # edit/shell -- the four tools where per-agent tightening matters most.
    approval_check: Optional[Callable[[str, dict], bool]] = None
    # Optional "is this tool meaningful right now?" predicate, consulted by
    # registry.schemas() when deciding what to advertise to the model.
    # Signature: () -> bool. Distinct from permissions: the tool is allowed,
    # it just has nothing to act on yet (load_skill with an empty catalog).
    # Not consulted by dispatch() -- a tool declaring itself unavailable is
    # expected to return a clean error if called anyway, and a second denial
    # path would only add a worse message.
    available_check: Optional[Callable[[], bool]] = None
    # Optional "once approved in this run, don't ask again" marker. Only
    # "run" is defined; None (the default) means every call is asked about
    # independently, which is the existing behaviour for every builtin.
    #
    # Exists so §18's subagent sign-off is a property of spawn_subagent
    # rather than a tool name hard-coded into core/loop.py -- the loop asks
    # the registry what a tool's grant scope is, the same way it asks
    # whether approval is needed at all. §23's response channel reuses it.
    grant_scope: Optional[str] = None
    # ROADMAP_v2 §23. Which KIND of question approving this tool asks.
    # "approval" (the default) is a yes/no; spawn_subagent sets
    # "subagent_signoff", whose answer is the SUBSET of tools the child may
    # then use without asking again.
    #
    # A property of the tool rather than a name hard-coded into
    # core/loop.py, for exactly the reason grant_scope is: the loop asks
    # the registry what shape of question a tool needs, the same way it
    # asks whether it needs one at all.
    request_kind: str = "approval"
    # Optional extra fields for that question's payload, when the answer
    # depends on something only the tool knows. Signature:
    # (params, context) -> dict. spawn_subagent uses it to carry the
    # candidate tool list, which is agents/manager.py's knowledge and must
    # not become the loop's. Mirrors approval_notice exactly.
    request_payload: Optional[Callable[[dict, object], dict]] = None
    # ROADMAP_v2 §32 (A7). Optional "I would refuse this call anyway"
    # pre-flight. Signature: (params, context) -> Optional[str], the
    # refusal reason or None. Returning a reason does NOT deny the
    # call -- the handler still runs and still refuses; it tells the
    # loop not to ASK first.
    #
    # core/loop.py already establishes this ordering for the case it
    # can see on its own: "a context-excluded tool reports the context
    # denial instead of prompting and then being denied anyway". That
    # reasoning is not specific to contexts, but the loop cannot apply
    # it to a condition only the tool knows -- spawn_subagent's
    # unknown-agent and depth-limit checks live inside run(), so both
    # prompted first and errored second. An unknown name produced a
    # sign-off modal with an EMPTY notice and an EMPTY candidate list:
    # a question with nothing in it.
    #
    # A property of the tool rather than a name hard-coded into the
    # loop, for exactly the reason grant_scope and request_kind are.
    refusal_check: Optional[Callable[[dict, object], Optional[str]]] = None
    # Optional extra text shown in the approval prompt, above the params.
    # Signature: (params, context) -> str. Lets a tool explain what
    # approving actually authorises when the params alone don't say --
    # spawn_subagent uses it to list the tools the subagent would then be
    # able to run without asking again. Keeps that knowledge in the tool
    # instead of teaching the TUI about agents.
    approval_notice: Optional[Callable[[dict, object], str]] = None
    # ROADMAP_v2 §25 (R13). One of GRANT_ANYWHERE / GRANT_SIGNOFF_ONLY /
    # GRANT_NEVER, above.
    #
    # DISTINCT from registry.grantable(), which is mechanical: does this
    # tool decide approval from its PARAMS, in which case a name-level
    # answer was never about the call that happens. This field is policy:
    # given that consent by name is meaningful, is ONE TICK BEFORE ANY
    # CALL EXISTS an acceptable answer, and does that depend on whether a
    # human is present? Both callers check both.
    #
    # None means UNDECLARED, not a default. Omission has to be a
    # detectable mistake rather than an inherited answer -- this field
    # exists because the previous shape was a denylist in ONE consumer's
    # module, so a tool registered after it was written was offered for a
    # grant without anybody being asked. That is what happened to
    # write_project_doc between §24 and #133.
    #
    # assert_grant_policy_declared() below makes omission fatal at import,
    # the same trade D24 made for permissions.
    grant_policy: Optional[str] = None
    # ROADMAP_v2 §31 (H1). One of BUDGET_COMPUTE / BUDGET_IO /
    # BUDGET_HUMAN, above.
    #
    # None means UNDECLARED, not a default -- grant_policy's rule one
    # question over, for grant_policy's reason. Before §31 every bound in
    # the tool layer was per-tool and voluntary: three network tools each
    # set their own REQUEST_TIMEOUT_S, `shell` inherited the sandbox's,
    # and MCP tools got a two-layer clock in mcp_client -- while the six
    # math tools, the only ones that can burn a core indefinitely, had
    # none at all (#57). A default here would recreate exactly that, and
    # silently: an unwrapped tool looks identical to a correctly bounded
    # one from every layer above.
    #
    # assert_budget_declared() below makes omission fatal at import.
    budget: Optional[str] = None
    # ROADMAP_v2 §42 (RA2). Which parameter, if any, carries the
    # AGENT'S OWN STATED REASON for making this call. `shell` sets
    # "rationale"; every other tool leaves it None.
    #
    # DISPLAY ONLY, and that is the whole contract. The named param is
    # rendered on the approval prompt and skipped by the transcript
    # digest, and it reaches NOTHING else: not
    # `_shell_approval_check`, not `classify_command`, not
    # `run_sandboxed`. A self-reported reason is unverifiable by
    # construction -- a hijacked agent writes a reassuring one -- so
    # letting it touch a decision would be strictly worse than not
    # having it. Its value is longitudinal: a thread's worth of stated
    # reasons, archived beside the calls, is a drift signal no single
    # prompt can give. test_rationale.py pins the invariance by
    # asserting on the arguments the classifier RECEIVES, not on the
    # decision it returns -- an outcome check would pass for a tool
    # that read the field and happened to agree.
    #
    # NO IMPORT-TIME GUARD, unlike grant_policy and budget. For those,
    # None is an unanswered question and silence is the failure; here
    # None is the correct answer for every tool but one, so a
    # declaration requirement would be noise on twenty-two of them.
    rationale_param: Optional[str] = None


def assert_grant_policy_declared(tools: Iterable[str], specs=None) -> None:
    """R13: every statically registered tool must declare a grant policy,
    and it must be one of the three defined values.

    Raises rather than warning, for D24's reason: the failure this guards
    against is invisible at runtime. A tool with no declaration resolves
    to GRANT_NEVER (registry.grant_policy), so forgetting the field would
    silently drop a tool out of both grant paths with nothing logged --
    and a MISSPELLED value would do the same while looking answered,
    which is worse. Both are caught here instead.

    The precedent is `fetch_url` under D24: registered, documented as
    working, and denied on every call for its entire life because nobody
    added a field. The equivalent mistake here is quieter still, because
    a tool that is never offered for a grant simply prompts per call --
    correct-looking behaviour that hides an unanswered policy question.

    Dynamically-named `mcp__*` tools are exempt by design; they are named
    at connection time and can never carry a static declaration.
    `mcp_client/registration.py` passes one explicitly anyway, so the
    fallback is documentation rather than the live path.
    """
    specs = specs if specs is not None else tools
    undeclared, invalid = [], []
    for name in tools:
        if name.startswith("mcp__"):
            continue
        spec = specs.get(name) if hasattr(specs, "get") else None
        policy = getattr(spec, "grant_policy", None)
        if policy is None:
            undeclared.append(name)
        elif policy not in GRANT_POLICIES:
            invalid.append(f"{name}={policy!r}")
    if undeclared or invalid:
        raise RuntimeError(
            "Tools are registered without a usable grant policy, so the "
            "question of whether one tick before any call exists may "
            "authorise them was never answered: "
            f"undeclared={sorted(undeclared)} invalid={sorted(invalid)}. "
            f"Set ToolSpec.grant_policy to one of {sorted(GRANT_POLICIES)} "
            "(ROADMAP_v2 §25, R13)."
        )


def assert_budget_declared(tools: Iterable[str], specs=None,
                           injectable=None) -> None:
    """H1: every statically registered tool must declare a cost class, it
    must be one of the three defined values, and a BUDGET_COMPUTE tool
    must not also declare an injection.

    Raises rather than warning, and the argument is the one above it,
    unchanged: the failure is invisible at runtime. An undeclared tool is
    simply not wrapped, which from every layer above looks exactly like a
    tool that is correctly bounded somewhere else -- and that is the state
    #57 found, where dispatch called every handler directly and nothing
    anywhere recorded whether that was intended.

    The third check is not a style rule. BUDGET_COMPUTE is not merely a
    policy: it routes the call through a PROCESS boundary, and nothing in
    _INJECTABLE_PARAMS survives one. A live ConversationMemory or a
    response_channel cannot be pickled, so a tool declaring both would
    fail at its first call rather than here. The two fields are checked
    together because neither is wrong on its own.

    Dynamically-named `mcp__*` tools are exempt, as they are for
    grant_policy and for the same reason -- they are named at connection
    time. Their bound is mcp_client's own two-layer clock, and
    registration.py passes BUDGET_IO explicitly to say so.
    """
    specs = specs if specs is not None else tools
    injectable = injectable or {}
    undeclared, invalid, contradictory = [], [], []
    for name in tools:
        if name.startswith("mcp__"):
            continue
        spec = specs.get(name) if hasattr(specs, "get") else None
        budget = getattr(spec, "budget", None)
        if budget is None:
            undeclared.append(name)
        elif budget not in BUDGETS:
            invalid.append(f"{name}={budget!r}")
        elif budget == BUDGET_COMPUTE and injectable.get(name):
            contradictory.append(
                f"{name} injects {sorted(injectable[name])}")
    if undeclared or invalid or contradictory:
        raise RuntimeError(
            "Tools are registered without a usable cost class, so what a "
            "call to them is allowed to cost was never answered: "
            f"undeclared={sorted(undeclared)} invalid={sorted(invalid)} "
            f"compute-with-injection={sorted(contradictory)}. "
            f"Set ToolSpec.budget to one of {sorted(BUDGETS)} "
            "(ROADMAP_v2 §31, H1)."
        )
