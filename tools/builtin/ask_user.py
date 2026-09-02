"""
tools/builtin/ask_user.py

ROADMAP_v2 §23 slice 2: the question tool. "Up to 4 options, multi-select, a
write-your-own answer, and a 'chat about this' escape that returns control to
the conversation instead of forcing a choice."

IT ASKS THROUGH THE INJECTED CHANNEL, NOT THROUGH THE APPROVAL BRIDGE. The
loop's `_obtain_approval` fires only under `if needs_approval:`, builds a
payload of {"tool_name", "params"}, and coerces the answer to (bool, grant) --
a free-text or multi-select answer has nowhere to go in it. It does not need
one: `response_channel` is already an injectable parameter, handed to every
handler that names it on BOTH dispatch branches, which is what
tools/registry.py's `_INJECTABLE_PARAMS` comment anticipated -- "generalised
so §23's response-channel tools add a third injectable name without reopening
dispatch()". So core/loop.py is untouched by this tool.

UNGATED, DELIBERATELY (J12). Requiring approval to ask a question would mean
approving a prompt in order to be shown a prompt -- and worse, §13 does not
merely deny a gated tool where nothing can ask, it stops ADVERTISING it. A
gated question tool would be invisible in every headless run rather than
denied with a reason, and §23 AC2 wants the opposite: the model must see the tool,
call it, and receive "there is nobody to ask" as something it can work around.
Same shape as `pin`'s ungating (D26), for the same reason.

NO CHANNEL MEANS DENIED, AND THE DENIAL EXPLAINS ITSELF (§23 AC2). An unattended
ten-pass pipeline is exactly where a blocking prompt nobody will answer turns
into a hang, and where a question injected by fetched web content would be
least noticed. An attended run carries a channel and can ask -- that is §25's
amendment, and it is why the condition tested here is "is there a channel"
rather than "is this the pipeline".
"""

import logging

from core import interaction

logger = logging.getLogger(__name__)

# The spec's cap, enforced rather than advisory. See run() for why a fifth
# option is an error and not a truncation.
MAX_OPTIONS = 4

#: Longest option the modal can show whole (batch 49). QuestionScreen is a
#: fixed 66 columns and an option button 58 of them, so 100 characters is
#: two wrapped lines for every shape measured -- prose, twenty-character
#: words, and an unbroken run -- and 105 is three for two of them. It is a
#: BUDGET, not a guess: the options region scrolls, so exceeding it costs
#: legibility rather than correctness, and the number is re-derivable by
#: the test that measures the drawn rows rather than trusting this
#: comment.
MAX_OPTION_CHARS = 100

TOOL_SCHEMA = {
    "name": "ask_user",
    "description": (
        "Ask the user a question and wait for their answer. Use this when a "
        "decision is genuinely theirs to make and getting it wrong would "
        "waste work -- an ambiguous requirement, a choice between "
        "approaches with different tradeoffs, a missing fact only they "
        "have. Do NOT use it for things you can determine yourself, or to "
        "confirm work you have already been asked to do. The user can pick "
        "one or more of your options, write their own answer, or ask to "
        "discuss it instead of choosing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question, in one or two sentences. State "
                               "what you need and why it changes what you "
                               "will do.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"Up to {MAX_OPTIONS} concrete answers to "
                               f"choose from. Optional -- omit it to ask an "
                               f"open question. Each must be at most "
                               f"{MAX_OPTION_CHARS} characters, so it can be "
                               f"shown whole -- put the reasoning in the "
                               f"question, not in the options.",
            },
            "multi_select": {
                "type": "boolean",
                "description": "True if several options can be chosen "
                               "together. Defaults to false.",
            },
            "allow_text": {
                "type": "boolean",
                "description": "Whether the user may write their own answer "
                               "instead of picking. Defaults to true.",
            },
        },
        "required": ["question"],
    },
}


def run(params: dict, response_channel=None) -> dict:
    # params.get throughout, never params[]: no provider validates tool
    # inputs against the schema, and a bare KeyError escapes the loop's
    # ToolCallDenied handler and takes down the whole turn. pin.py's rule.
    question = params.get("question")
    if not question or not isinstance(question, str):
        return {"error": "ask_user requires a 'question' string."}

    options = params.get("options") or []
    if not isinstance(options, (list, tuple)):
        return {"error": "ask_user requires 'options' to be a list of strings."}
    options = [str(o) for o in options]

    if len(options) > MAX_OPTIONS:
        # AN ERROR, NOT A TRUNCATION. Silently dropping the fifth option
        # would show the user a different question from the one the model
        # asked, and the model would then read the answer as a response to
        # the full list. M15's rule -- never silently drop something
        # somebody named -- with the model in the naming role. An error it
        # can correct costs one step; a mis-answered question costs the
        # decision.
        return {
            "error": f"ask_user allows at most {MAX_OPTIONS} options; "
                     f"{len(options)} were given. Ask a narrower question, "
                     f"or drop the options and ask it openly.",
        }

    too_long = [o for o in options if len(o) > MAX_OPTION_CHARS]
    if too_long:
        # AN ERROR, NOT A TRUNCATION, for the reason directly above: an
        # option shortened here is not the option the model offered, and
        # the answer would come back as though it were. The modal used to
        # truncate this way by accident -- an option wider than the dialog
        # was CUT at its edge, so a user chose between two options whose
        # visible halves were identical -- which is the failure this
        # refusal exists to make impossible rather than merely unlikely.
        longest = max(too_long, key=len)
        return {
            "error": f"ask_user allows at most {MAX_OPTION_CHARS} characters "
                     f"per option so it can be read whole; one is "
                     f"{len(longest)}. Shorten it to the choice itself and "
                     f"put the reasoning in the question: "
                     f"{longest[:60]!r}...",
        }

    if not options and params.get("allow_text", True) is False:
        # #114, same family as the two refusals above: a question with no
        # options and no text box renders as a modal whose only button is
        # "Discuss instead", so a defer means "that was my only button"
        # rather than "let's talk about it" -- collapsing exactly the
        # distinction QuestionScreen's escape/defer pair exists to keep.
        return {
            "error": "ask_user needs something to answer with: give "
                     "options, or leave allow_text true so the user can "
                     "write a reply.",
        }

    if response_channel is None:
        # §23 AC2. Not an exception: dispatch would convert one to an error dict
        # anyway, and this way the message is written for the reader who
        # needs it -- a model mid-run that must now decide something itself.
        return {
            "error": "ask_user cannot reach anyone in this run -- there is "
                     "nobody to answer. Decide it yourself, state the "
                     "assumption you are proceeding on, and carry on.",
        }

    answer = interaction.ask(response_channel, interaction.Request(
        kind=interaction.QUESTION,
        payload={
            "question": question,
            "options": options,
            "multi_select": bool(params.get("multi_select")),
            # Defaults TRUE: the spec lists a write-your-own answer as one of
            # the four affordances, so absence of the key is not absence of
            # the affordance.
            "allow_text": params.get("allow_text", True) is not False,
        },
    ))

    if answer is None:
        # Nobody answered -- dismissed, timed out, or a shell returning
        # something decode could not read. Distinct from a deferral below,
        # and reported as such: "they declined to answer" and "they want to
        # discuss it" call for different next moves.
        return {
            "error": "The user did not answer the question. Proceed on your "
                     "best judgement and say which assumption you made.",
        }

    if answer.get("defer"):
        # The spec's escape, "returns control to the conversation instead of
        # forcing a choice". A result rather than any change of control
        # flow: the loop has one shape, and a tool that could abort a turn
        # would be a second one. The model simply talks next.
        return {
            "result": "The user would rather discuss this than pick an "
                      "answer. Raise it in conversation -- do not call "
                      "ask_user again for the same question.",
        }

    chosen, text = answer.get("options") or [], answer.get("text") or ""
    if not chosen and not text:
        # A blank submission. They were present and said nothing, which is
        # not the same as never being asked -- so this is not the no-channel
        # error, and the wording does not tell the model to assume anything.
        return {
            "result": "The user answered without choosing an option or "
                      "writing anything. Ask again more specifically, or "
                      "raise it in conversation.",
        }

    return {"result": {"chosen": chosen, "text": text}}
