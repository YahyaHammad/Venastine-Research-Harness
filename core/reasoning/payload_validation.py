"""
core/reasoning/payload_validation.py

ROADMAP_v2 §30 (B1). One shape boundary for the ten research passes.

§3's retry corrects a response that does not PARSE. Nothing checked that
what parsed was the SHAPE the pass promised, so the same defect appeared
in six places at once (audit #76): a wrapped list crashed with
`'str' object has no attribute 'items'`, a bad `severity` survived two
passes and surfaced in Pass 5's pure-code tiering, and two shapes
completed a run and reported a false outcome.

WHY THIS IS ITS OWN MODULE, AND WHY IT LOOKS FAMILIAR. §20 already built
this validator -- `review.py:_validated` checks a top-level type, the
required keys per entry with type guards BEFORE the membership tests, an
id cross-check against `{c.id for c in run.claims}`, an enum, and a
duplicate -- and built it once, for the one payload that is NOT a pass.
The passes never got it. Putting the general version in orchestrator.py
would repeat json_retry.py's own mistake in reverse: that module was
extracted precisely so a mechanism a second consumer might want is not
trapped in the orchestrator.

`review.py` is deliberately NOT migrated onto this module (§30, B1). Its
POLICY is different and correctly so: a finding it cannot apply is
dropped and traced, because the review stage is optional and contained,
and a reviewer that misfires must not cost a completed ten-pass run. A
pass is not optional -- a payload it cannot apply is a run that will
report something untrue -- so a failure here re-enters §3's corrective
retry (B2) and ultimately raises. Same shape, two policies. Merging them
means choosing one, and neither is wrong for its own caller.

WHAT IS CHECKED, AND WHY ONLY THIS (B4). Three properties, plus a
declared type where a value is used arithmetically -- every one has a
demonstrated consequence in #76's table, and nothing else does:

    the top-level type          {"claims": [...]} instead of [...]
    the required keys per entry a claim with no `id`, a revision with no
                                `revised_text`, Pass 4 with no
                                `per_claim_flags`
    a declared value type       `"severity": "0.0"` -- a string that
                                fails two passes later, in Pass 5
    that entry ids resolve      3a/3b answering about ids Pass 2 never
                                issued, which grounded nothing while the
                                trace said it had grounded everything

Not a schema language, and no new dependency (#145 tracks those). A
richer vocabulary would let a spec describe things no pass has ever got
wrong, at the price of a second place where a payload's meaning lives.

STATED LIMIT, NOT FIXED (B11). Every spec below is a transcription of the
`Respond with ONLY this JSON structure` block in that pass's own .md, and
NOTHING CHECKS THAT THE TWO STILL AGREE. A prompt edit that renames a
field will fail the run rather than silently mis-apply it -- which is the
better failure -- but it will fail it at the model's expense. The parity
that IS mechanised is narrower and is the one that goes wrong silently:
every pass the orchestrator routes through `_run_pass_with_json_retry`
has a spec here (see tests/test_payload_validation.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Re-exported, not re-derived: base.py defines these beside the Literals
# they come from, and the scorer reads the same two names (§30, B9).
from core.reasoning.base import CLAIM_TYPES, GROUNDING_STATUSES  # noqa: F401

NUMBER = (int, float)


class PayloadShapeError(ValueError):
    """A pass's response parsed as JSON but is not the shape it promised.

    SUBCLASSES ValueError deliberately, and that is load-bearing twice:
    json_retry.py's corrective loop already catches ValueError (so a shape
    failure re-enters the retry with no new except clause), and the
    orchestrator's outer try/except -- which turns a failure into a
    durable status='failed' record -- is byte-for-byte unchanged.

    The message names the pass, the field and the expected shape, because
    the whole complaint in #76's loud half is that
    `'str' object has no attribute 'items'` names none of the three.
    """


@dataclass(frozen=True)
class Spec:
    """One pass's promised payload shape.

    top               "object" or "array"
    required          top-level keys that must be present (top="object")
    key_types         top-level key -> permitted Python type(s)
    entry_required    keys every entry must carry (top="array")
    entry_types       entry key -> permitted Python type(s)
    enums             entry key -> the set of permitted values
    ids               entry key holding a claim id, cross-checked against
                      the claims the apply step will resolve against
    unique            entry key that must not repeat (Pass 2's `id`)
    ensemble_required entry keys required only on an ensemble run (B7)
    ids_in_keys       top-level key whose KEYS are claim ids (Pass 4)
    nested            top-level key -> Spec for the array beneath it
    """

    top: str
    required: frozenset = frozenset()
    key_types: dict = field(default_factory=dict)
    entry_required: frozenset = frozenset()
    entry_types: dict = field(default_factory=dict)
    enums: dict = field(default_factory=dict)
    ids: Optional[str] = None
    unique: Optional[str] = None
    ensemble_required: frozenset = frozenset()
    ids_in_keys: Optional[str] = None
    nested: dict = field(default_factory=dict)


# The 3a and 3b entry shapes are named, because Pass 6b is documented in
# revalidate.md as returning exactly those two lists under two keys --
# and _apply_grounding/_apply_critic are literally the same two functions.
# Writing them twice would let 6c drift from the passes it re-runs.
_GROUNDING_ENTRY = Spec(
    top="array",
    entry_required=frozenset({"claim_id", "status"}),
    entry_types={"claim_id": str},
    enums={"status": GROUNDING_STATUSES},
    ids="claim_id",
)

_CRITIC_ENTRY = Spec(
    top="array",
    entry_required=frozenset({"claim_id", "severity"}),
    # `severity` is the arithmetic one: Pass 5 computes `1.0 - severity`,
    # so a string here raises a TypeError two passes downstream, in the
    # one stage that makes no model call and has nothing to retry.
    entry_types={"claim_id": str, "severity": NUMBER},
    ids="claim_id",
)


SPECS = {
    # preliminary_plan.md names five keys, and the orchestrator reads ONE
    # of them defensively (`run.plan.get("key_entities_or_subjects", [])`)
    # before handing the plan whole to Pass 3c. So requiring all five
    # would fail runs over an omission nothing depends on. The
    # demonstrated defect is a bare list, which is what `top` catches.
    "Pass 0": Spec(top="object"),

    "Pass 2": Spec(
        top="array",
        entry_required=frozenset({"id", "text", "type"}),
        entry_types={"id": str, "text": str},
        enums={"type": CLAIM_TYPES},
        # B6. Two claims sharing an id split in half: _apply_grounding
        # builds {c.id: c} (last wins) while _apply_assumption_flags uses
        # next(...) (first wins), so the grounding landed on one object
        # and the assumption flags on the other. claim_extraction.md asks
        # for "sequential zero-padded ids" and never says they must be
        # unique.
        unique="id",
        # B7. Only in ensemble mode, and only because claim_extraction.md
        # is now asked to supply it on every claim there. An ABSENT field
        # is indistinguishable from an empty one at the scorer, and
        # `len([]) / n` is the MAXIMUM disagreement penalty -- charged to
        # a claim that exists because a candidate asserted it.
        ensemble_required=frozenset({"asserted_by_candidates"}),
    ),

    "Pass 3a": _GROUNDING_ENTRY,
    "Pass 3b": _CRITIC_ENTRY,

    "Pass 3c": Spec(
        top="object",
        required=frozenset({"gaps", "coverage_score"}),
        # `gaps` is iterated by build_coverage_gap_entries; a string there
        # would produce one coverage-gap entry per CHARACTER.
        key_types={"gaps": list},
    ),

    "Pass 4": Spec(
        top="object",
        required=frozenset({"per_claim_flags"}),
        key_types={"per_claim_flags": dict},
        ids_in_keys="per_claim_flags",
    ),

    "Pass 6a": Spec(
        top="array",
        entry_required=frozenset({"claim_id", "revised_text"}),
        entry_types={"claim_id": str, "revised_text": str},
        ids="claim_id",
    ),

    "Pass 6b": Spec(
        top="object",
        required=frozenset({"grounding", "critic"}),
        key_types={"grounding": list, "critic": list},
        nested={"grounding": _GROUNDING_ENTRY, "critic": _CRITIC_ENTRY},
    ),
}


def _type_name(value: Any) -> str:
    """JSON's vocabulary, not Python's -- the message goes back to a model
    that was asked for JSON, so `object`/`array` is what it can act on."""
    return {dict: "object", list: "array", str: "string",
            bool: "boolean", int: "number", float: "number",
            type(None): "null"}.get(type(value), type(value).__name__)


def _permitted(types) -> str:
    if not isinstance(types, tuple):
        types = (types,)
    return " or ".join(sorted({_type_name(t()) for t in types}))


def validate(pass_id: str, payload: Any, claim_ids=None,
             ensemble: bool = False) -> None:
    """Raise PayloadShapeError unless `payload` is what `pass_id` promised.

    claim_ids   the ids the APPLY step will resolve against -- all claims
                for 3a/3b/5/6c, the currently-flagged batch for 6a. Not
                "every claim in the run": Pass 6a is only asked about
                `flagged`, and its own lookup already resolves within that
                batch, so validating against a wider set would accept a
                revision that then lands on nothing. None skips the check.
    ensemble    True on an ensemble run, which is the only place Pass 2 is
                asked for `asserted_by_candidates` (B7).

    An unknown pass_id RAISES rather than passing through unvalidated. A
    pass with no spec is the silent hole this module exists to close, and
    the eleventh pass must not be able to arrive by forgetting a line.
    """
    spec = SPECS.get(pass_id)
    if spec is None:
        raise PayloadShapeError(
            f"{pass_id}: no payload spec is declared for this pass. Every "
            f"pass routed through _run_pass_with_json_retry needs one in "
            f"core/reasoning/payload_validation.py (§30, B11)."
        )
    _validate_against(pass_id, payload, spec, claim_ids, ensemble)


def _validate_against(pass_id, payload, spec, claim_ids, ensemble,
                      where: str = "") -> None:
    at = f" under {where!r}" if where else ""

    if spec.top == "array":
        if not isinstance(payload, list):
            raise PayloadShapeError(
                f"{pass_id}: expected a JSON array{at}, got "
                f"{_type_name(payload)}. Return the array itself, not an "
                f"object wrapping it."
            )
        _validate_entries(pass_id, payload, spec, claim_ids, ensemble, at)
        return

    if not isinstance(payload, dict):
        raise PayloadShapeError(
            f"{pass_id}: expected a JSON object{at}, got "
            f"{_type_name(payload)}."
        )

    missing = sorted(spec.required - set(payload))
    if missing:
        raise PayloadShapeError(
            f"{pass_id}: the response{at} is missing required key(s) "
            f"{', '.join(repr(m) for m in missing)}. Expected an object "
            f"with {', '.join(sorted(spec.required))}."
        )

    for key, types in spec.key_types.items():
        if key in payload and not isinstance(payload[key], types):
            raise PayloadShapeError(
                f"{pass_id}: {key!r}{at} is {_type_name(payload[key])}; "
                f"expected {_permitted(types)}."
            )

    for key, nested in spec.nested.items():
        _validate_against(pass_id, payload[key], nested, claim_ids,
                          ensemble, where=key)

    if spec.ids_in_keys:
        _check_ids(pass_id, list(payload[spec.ids_in_keys]), claim_ids,
                   f"key of {spec.ids_in_keys!r}")


def _validate_entries(pass_id, entries, spec, claim_ids, ensemble, at) -> None:
    required = spec.entry_required | (spec.ensemble_required if ensemble
                                      else frozenset())
    seen = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise PayloadShapeError(
                f"{pass_id}: entry {index}{at} is {_type_name(entry)}; "
                f"every entry must be an object with "
                f"{', '.join(sorted(required))}."
            )
        missing = sorted(required - set(entry))
        if missing:
            raise PayloadShapeError(
                f"{pass_id}: entry {index}{at} is missing required key(s) "
                f"{', '.join(repr(m) for m in missing)}. Every entry needs "
                f"{', '.join(sorted(required))}."
            )
        # TYPE GUARDS BEFORE THE MEMBERSHIP TESTS, which is review.py's
        # rule and the reason it is worth copying: a JSON array or object
        # in an enum-checked field is unhashable, so `value in ALLOWED`
        # raises TypeError from inside the validator rather than
        # producing a message the model can act on.
        for key, types in spec.entry_types.items():
            if key in entry and not isinstance(entry[key], types):
                raise PayloadShapeError(
                    f"{pass_id}: entry {index}{at} has {key}="
                    f"{entry[key]!r}, which is {_type_name(entry[key])}; "
                    f"expected {_permitted(types)}."
                )
        for key, allowed in spec.enums.items():
            if key not in entry:
                continue
            value = entry[key]
            if not isinstance(value, str) or value not in allowed:
                raise PayloadShapeError(
                    f"{pass_id}: entry {index}{at} has {key}={value!r}; "
                    f"expected exactly one of "
                    f"{', '.join(sorted(allowed))} (lower case)."
                )
        if spec.unique:
            value = entry[spec.unique]
            if value in seen:
                raise PayloadShapeError(
                    f"{pass_id}: entries {seen[value]} and {index}{at} "
                    f"share {spec.unique}={value!r}. Ids must be unique -- "
                    f"two claims with one id are applied to inconsistently "
                    f"and end up carrying half of each other's results."
                )
            seen[value] = index

    if spec.ids:
        _check_ids(pass_id, [e[spec.ids] for e in entries], claim_ids,
                   spec.ids)


def _check_ids(pass_id, supplied, claim_ids, field_name) -> None:
    """B5: a TOTAL mismatch is the error; a partial one is not.

    The demonstrated silent failure is every id being wrong -- 3a/3b
    answering about "1", "2", "3" when Pass 2 issued "c001", "c002" --
    which grounded nothing while the checkpoint line reported that it had
    grounded everything. That is a pass with no effect at all, and it is
    worth a corrective retry.

    A model that gets nine of ten right is NOT worth failing a run that
    has already paid for nine passes: the nine apply, and the checkpoint
    line reports the count APPLIED rather than the count asked about, so
    the tenth is visible instead of silent (see the orchestrator's
    _apply_* return values).

    An EMPTY collection is not a mismatch. assumption_audit.md asks the
    model to "omit claims with no issues rather than including them with
    an empty list", so no flags at all is the ordinary answer for a clean
    run -- and Pass 4's demonstrated defect was the top-level key being
    absent, which the required-key check above catches instead.
    """
    if claim_ids is None or not supplied:
        return
    known = set(claim_ids)
    if any(value in known for value in supplied):
        return
    sample = ", ".join(repr(v) for v in supplied[:3])
    expected = ", ".join(repr(v) for v in sorted(known)[:3])
    raise PayloadShapeError(
        f"{pass_id}: not one of the {len(supplied)} {field_name} value(s) "
        f"matches a claim you were given (got {sample}; expected ids like "
        f"{expected}). Use the claim ids exactly as they appear in the "
        f"input."
    )
