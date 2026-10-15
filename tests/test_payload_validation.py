"""
test_payload_validation.py

ROADMAP_v2 §30 (B1, B4, B6, B9, B11) -- the shape boundary for the ten
research passes, and audit #76.

WHAT THESE TESTS ARE FOR, since a validator is easy to test vacuously. A
test that only asserts "bad payload raises" passes just as well against a
validator that raises on EVERYTHING, so every rejection below has a
control beside it: the shape that must still be accepted. The partial-id
case is the sharpest one -- B5 deliberately does NOT fail a run because a
model got nine ids right and one wrong, and a stricter validator would
pass every rejection test in this file while killing runs that used to
finish.
"""

import re

import pytest

from core.reasoning.payload_validation import (
    CLAIM_TYPES,
    GROUNDING_STATUSES,
    SPECS,
    PayloadShapeError,
    validate,
)

# ---------------------------------------------------------------------------
# ---- The registry (B11) ---------------------------------------------------
# ---------------------------------------------------------------------------

def _orchestrator_source() -> str:
    import inspect

    from core.reasoning import orchestrator

    return inspect.getsource(orchestrator)


def _json_pass_call_sites() -> list:
    """Every pass id the orchestrator routes through the JSON wrapper.

    Read from the SOURCE rather than from a list maintained here, for the
    same reason main.py's "no bare input(" test reads source (§29): a
    registry that has to be updated by hand is the thing that goes stale,
    and the failure mode -- an unvalidated pass -- is silent.
    """
    return re.findall(r'_run_pass_with_json_retry\(\s*"([^"]+)"',
                      _orchestrator_source())


class TestEveryJsonPassHasASpec:

    def test_the_call_sites_and_the_spec_table_agree(self):
        """#76's whole shape is that nothing asserted a payload's shape at
        all. An eleventh JSON-emitting pass added without a spec would
        reintroduce it for exactly one pass, silently -- so this fails at
        CI instead."""
        called = set(_json_pass_call_sites())
        assert called, (
            "no _run_pass_with_json_retry call sites were found in "
            "orchestrator.py -- this test reads source, so a rename of "
            "that function makes it vacuous rather than red."
        )
        missing = sorted(called - set(SPECS))
        assert not missing, (
            f"these passes go through the JSON wrapper with no payload "
            f"spec: {missing}. Add one in "
            f"core/reasoning/payload_validation.py."
        )

    def test_no_spec_is_declared_for_a_pass_that_does_not_exist(self):
        """The other direction. A spec for a pass nothing calls is a rule
        nobody enforces, and reads as coverage that is not there."""
        unused = sorted(set(SPECS) - set(_json_pass_call_sites()))
        assert not unused, (
            f"these specs are declared but no call site uses them: {unused}"
        )

    def test_every_id_checking_pass_is_handed_the_ids(self):
        """A spec can declare an id cross-check and get None at the call
        site, which turns the check off without failing anything. The four
        passes that resolve entries against claims must pass claim_ids."""
        source = _orchestrator_source()
        needs_ids = sorted(
            pass_id for pass_id, spec in SPECS.items()
            if spec.ids or spec.ids_in_keys
            or any(n.ids for n in spec.nested.values())
        )
        assert needs_ids == ["Pass 3a", "Pass 3b", "Pass 4", "Pass 6a", "Pass 6b"]
        for pass_id in needs_ids:
            call = re.search(
                r'_run_pass_with_json_retry\(\s*"' + re.escape(pass_id)
                + r'"[^\n]*', source)
            assert call and "claim_ids=" in call.group(0), (
                f"{pass_id} declares an id cross-check but its call site "
                f"does not pass claim_ids=, so the check never runs."
            )

    def test_an_unknown_pass_raises_rather_than_passing_through(self):
        with pytest.raises(PayloadShapeError, match="no payload spec"):
            validate("Pass 11", {"anything": True})


class TestTheErrorIsContainedTheWayTheOrchestratorExpects:

    def test_payload_shape_error_is_a_value_error(self):
        """Load-bearing twice: json_retry's corrective loop catches
        ValueError, so a shape failure re-enters the retry with no new
        except clause; and the orchestrator's outer handler -- which
        writes the durable status='failed' record -- is unchanged."""
        assert issubclass(PayloadShapeError, ValueError)

    def test_the_message_names_the_pass_and_the_field(self):
        """#76's complaint about the loud half is precisely that
        `'str' object has no attribute 'items'` names neither."""
        with pytest.raises(PayloadShapeError) as caught:
            validate("Pass 3b", [{"claim_id": "c1", "severity": "0.4"}],
                     claim_ids=["c1"])
        message = str(caught.value)
        assert message.startswith("Pass 3b:")
        assert "severity" in message
        assert "number" in message


# ---------------------------------------------------------------------------
# ---- The top-level type ---------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheTopLevelType:

    @pytest.mark.parametrize("pass_id", ["Pass 2", "Pass 3a", "Pass 3b", "Pass 6a"])
    def test_an_object_where_an_array_was_promised_is_rejected(self, pass_id):
        """`{"claims": [...]}` is the single most likely way a model
        deviates here, and before §30 it produced
        `'str' object has no attribute 'items'` -- because the list
        comprehension iterated the dict's KEYS."""
        with pytest.raises(PayloadShapeError, match="expected a JSON array"):
            validate(pass_id, {"claims": []}, claim_ids=["c1"])

    @pytest.mark.parametrize("pass_id", ["Pass 0", "Pass 3c", "Pass 4", "Pass 6b"])
    def test_an_array_where_an_object_was_promised_is_rejected(self, pass_id):
        with pytest.raises(PayloadShapeError, match="expected a JSON object"):
            validate(pass_id, ["a gap"], claim_ids=["c1"])

    @pytest.mark.parametrize("pass_id,payload", [
        ("Pass 0", {"key_entities_or_subjects": []}),
        ("Pass 2", []),
        ("Pass 3a", []),
        ("Pass 3b", []),
        ("Pass 3c", {"gaps": [], "coverage_score": 0.0}),
        ("Pass 4", {"per_claim_flags": {}}),
        ("Pass 6a", []),
        ("Pass 6b", {"grounding": [], "critic": []}),
    ])
    def test_the_minimal_valid_payload_is_accepted(self, pass_id, payload):
        """The control for every rejection above. An empty answer is a real
        answer for each of these -- no claims, no gaps, no flags -- and a
        validator that rejected it would fail runs that used to finish."""
        validate(pass_id, payload, claim_ids=["c1"])

    def test_pass_0_requires_no_keys_at_all(self):
        """preliminary_plan.md names five keys and the orchestrator reads
        ONE of them defensively before handing the plan whole to Pass 3c.
        Requiring all five would fail a run over an omission nothing
        depends on; the demonstrated defect is the bare list above."""
        validate("Pass 0", {"notes": "just prose"})


# ---------------------------------------------------------------------------
# ---- Required keys and declared types (B4) --------------------------------
# ---------------------------------------------------------------------------

class TestRequiredKeys:

    def test_a_claim_with_no_id_is_rejected(self):
        with pytest.raises(PayloadShapeError, match=r"missing required key\(s\) 'id'"):
            validate("Pass 2", [{"text": "t", "type": "factual"}])

    def test_a_revision_with_no_revised_text_is_rejected(self):
        """The 6a loop subscripts rev["revised_text"] where its three
        siblings use .get -- so this was a KeyError out of the middle of
        the retry loop, on a run that had already paid for nine passes."""
        with pytest.raises(PayloadShapeError, match="revised_text"):
            validate("Pass 6a", [{"claim_id": "c1"}], claim_ids=["c1"])

    def test_pass_5_without_per_claim_flags_is_rejected(self):
        """The silent one. `.get("per_claim_flags", {})` yielded {}, the
        whole assumption audit was dropped, and the trace said
        "0 claim(s) flagged" -- which is what it would say for a clean
        run."""
        with pytest.raises(PayloadShapeError, match="per_claim_flags"):
            validate("Pass 4", {"c001": ["a premise"]}, claim_ids=["c001"])

    def test_an_extra_key_is_not_an_error(self):
        """The control. _claim_from_json filters unknown keys deliberately
        (#123), and a validator that rejected them would make that filter
        unreachable."""
        validate("Pass 2", [{"id": "c1", "text": "t", "type": "factual",
                             "confidence": 0.9, "notes": "chatter"}])

    def test_an_entry_that_is_not_an_object_is_rejected(self):
        with pytest.raises(PayloadShapeError, match="entry 1 .*is string"):
            validate("Pass 2", [{"id": "c1", "text": "t", "type": "factual"},
                                "c002"])


class TestDeclaredValueTypes:

    def test_a_string_severity_is_rejected_at_the_pass_that_sent_it(self):
        """#76's sharpest row: this survived Pass 3c and Pass 4 and
        surfaced in Pass 5's PURE-CODE tiering, two passes downstream, as
        `unsupported operand type(s) for -: 'float' and 'str'` -- in the
        one stage that makes no model call and so has nothing to retry."""
        with pytest.raises(PayloadShapeError, match="severity"):
            validate("Pass 3b", [{"claim_id": "c1", "severity": "0.4"}],
                     claim_ids=["c1"])

    def test_an_integer_severity_is_accepted(self):
        """The control -- JSON has one number type, so `0` is a perfectly
        ordinary way to say zero severity."""
        validate("Pass 3b", [{"claim_id": "c1", "severity": 0}],
                 claim_ids=["c1"])

    def test_gaps_must_be_a_list(self):
        """build_coverage_gap_entries iterates it; a string there produces
        one coverage-gap entry per CHARACTER."""
        with pytest.raises(PayloadShapeError, match="'gaps' is string"):
            validate("Pass 3c", {"gaps": "more detail", "coverage_score": 0.5})

    def test_an_unhashable_value_in_an_ENUM_field_is_a_shape_error(self):
        """The enum branch has its own type guard, and it needs its own
        test: `value not in allowed` against a frozenset raises TypeError
        from INSIDE the validator when the value is a list -- an
        unhandleable error about hashing, in place of a message naming the
        pass and the field.

        Found by mutation. Deleting `not isinstance(value, str) or` left
        the whole suite green, because the only unhashable-value test
        aimed at `claim_id`, which entry_types checks first."""
        with pytest.raises(PayloadShapeError, match="type="):
            validate("Pass 2", [{"id": "c1", "text": "t",
                                 "type": ["factual", "synthesis"]}])

    def test_a_non_string_claim_id_is_rejected_before_it_is_looked_up(self):
        """Type guards BEFORE the membership tests, which is review.py's
        rule: an unhashable value in an id field would otherwise raise
        TypeError from inside the validator rather than producing a
        message the model can act on."""
        with pytest.raises(PayloadShapeError, match="claim_id"):
            validate("Pass 3a", [{"claim_id": ["c1"], "status": "grounded"}],
                     claim_ids=["c1"])


# ---------------------------------------------------------------------------
# ---- The two enums (B9) ---------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheEnums:

    def test_the_vocabularies_come_from_base_pys_literals(self):
        """A second copy of a vocabulary is a second thing to keep in step.
        #75 is what happens when a Literal is mistaken for enforcement."""
        assert CLAIM_TYPES == {"factual", "synthesis", "speculative"}
        assert GROUNDING_STATUSES == {"grounded", "partial", "ungrounded"}

    @pytest.mark.parametrize("status", ["Grounded", "GROUNDED", "verified",
                                        "Ungrounded"])
    def test_an_out_of_vocabulary_grounding_status_is_rejected(self, status):
        """Both directions were wrong and neither said anything:
        `"Grounded"` scored a well-sourced claim 0.35/LOW where
        `"grounded"` gives 0.85/HIGH, and `"Ungrounded"` ESCAPED the
        forced-UNVERIFIED rule to land LOW -- a claim the model said it
        could not source, published as merely low-confidence."""
        with pytest.raises(PayloadShapeError, match="status="):
            validate("Pass 3a", [{"claim_id": "c1", "status": status}],
                     claim_ids=["c1"])

    @pytest.mark.parametrize("status", sorted(GROUNDING_STATUSES))
    def test_every_real_status_is_accepted(self, status):
        validate("Pass 3a", [{"claim_id": "c1", "status": status}],
                 claim_ids=["c1"])

    def test_a_capitalised_claim_type_is_rejected(self):
        """It skipped D0's routing to 3a/3b entirely, so the claim was
        never grounded or critiqued, and was then capped at
        NON_FACTUAL_SCORE_CAP -- scoring MEDIUM where the same claim
        spelled `factual` was forced to UNVERIFIED."""
        with pytest.raises(PayloadShapeError, match="type='Factual'"):
            validate("Pass 2", [{"id": "c1", "text": "t", "type": "Factual"}])

    @pytest.mark.parametrize("claim_type", sorted(CLAIM_TYPES))
    def test_every_real_claim_type_is_accepted(self, claim_type):
        validate("Pass 2", [{"id": "c1", "text": "t", "type": claim_type}])

    def test_pass_6c_gets_the_same_vocabularies_through_its_nested_lists(self):
        """revalidate.md returns the 3a and 3b entry shapes under two keys,
        and _apply_grounding/_apply_critic are literally the same two
        functions -- so 6c must not be able to drift from the passes it
        re-runs."""
        with pytest.raises(PayloadShapeError, match=r"under 'grounding'"):
            validate("Pass 6b",
                     {"grounding": [{"claim_id": "c1", "status": "verified"}],
                      "critic": []},
                     claim_ids=["c1"])


# ---------------------------------------------------------------------------
# ---- Duplicate ids (B6) ---------------------------------------------------
# ---------------------------------------------------------------------------

class TestDuplicateClaimIds:

    def test_two_claims_sharing_an_id_are_rejected(self):
        """Driven before the fix: _apply_grounding builds {c.id: c} (last
        wins) while _apply_assumption_flags uses next(...) (first wins), so
        the grounding landed on one object and the assumption flags on the
        other. claim_extraction.md asks for "sequential zero-padded ids"
        and never says they must be unique."""
        with pytest.raises(PayloadShapeError, match="share id='c1'"):
            validate("Pass 2", [
                {"id": "c1", "text": "first", "type": "factual"},
                {"id": "c1", "text": "second", "type": "factual"},
            ])

    def test_the_message_names_both_entries(self):
        with pytest.raises(PayloadShapeError, match="entries 0 and 2"):
            validate("Pass 2", [
                {"id": "c1", "text": "first", "type": "factual"},
                {"id": "c2", "text": "other", "type": "factual"},
                {"id": "c1", "text": "third", "type": "factual"},
            ])

    def test_distinct_ids_are_accepted(self):
        validate("Pass 2", [
            {"id": "c1", "text": "first", "type": "factual"},
            {"id": "c2", "text": "second", "type": "factual"},
        ])

    def test_a_repeated_claim_id_in_a_GROUNDING_payload_is_fine(self):
        """Only Pass 2's `id` is unique-checked. 3a may legitimately answer
        about one claim twice -- last wins, which is what _apply_grounding
        already does -- and rejecting it would fail a run over something
        with no consequence."""
        validate("Pass 3a", [{"claim_id": "c1", "status": "grounded"},
                             {"claim_id": "c1", "status": "partial"}],
                 claim_ids=["c1"])


# ---------------------------------------------------------------------------
# ---- The id cross-check (B5) ----------------------------------------------
# ---------------------------------------------------------------------------

class TestTheIdCrossCheck:

    def test_a_total_mismatch_is_rejected(self):
        """The second silent failure, driven: 3a/3b answering about "1",
        "2", "3" when Pass 2 issued "C1", "C2", "C3" left every claim at
        grounding_status=None under a trace line reading
        "Pass 3a: grounded 4 unique entities across 3 factual claim(s)."
        -- an outcome the pass derived entirely from its own input."""
        with pytest.raises(PayloadShapeError, match="not one of the 3"):
            validate("Pass 3a",
                     [{"claim_id": str(n), "status": "grounded"} for n in (1, 2, 3)],
                     claim_ids=["C1", "C2", "C3"])

    def test_a_partial_mismatch_is_NOT_rejected(self):
        """THE CONTROL, and the one that decides the whole policy. A model
        that gets two of three right is not worth failing a run that has
        already paid for nine passes: the two apply, and the checkpoint
        line reports the count APPLIED rather than the count asked about,
        so the third is visible instead of silent.

        A stricter validator passes every other test in this file."""
        validate("Pass 3a", [{"claim_id": "C1", "status": "grounded"},
                             {"claim_id": "C2", "status": "grounded"},
                             {"claim_id": "nonsense", "status": "grounded"}],
                 claim_ids=["C1", "C2", "C3"])

    def test_an_empty_payload_is_not_a_mismatch(self):
        """assumption_audit.md asks the model to "omit claims with no
        issues rather than including them with an empty list", so no flags
        at all is the ORDINARY answer for a clean run. Pass 4's
        demonstrated defect was the top-level key being absent, which the
        required-key check catches instead."""
        validate("Pass 4", {"per_claim_flags": {}}, claim_ids=["C1"])
        validate("Pass 3a", [], claim_ids=["C1"])

    def test_no_claim_ids_supplied_means_no_check(self):
        """The direct-call tests in test_json_retry.py exercise the retry
        mechanism with no run behind them. None must mean "not supplied",
        not "no claims exist" -- the second would reject every payload."""
        validate("Pass 3a", [{"claim_id": "anything", "status": "grounded"}])

    def test_pass_5_cross_checks_the_KEYS_of_per_claim_flags(self):
        with pytest.raises(PayloadShapeError, match="key of 'per_claim_flags'"):
            validate("Pass 4", {"per_claim_flags": {"999": ["a flag"]}},
                     claim_ids=["C1"])

    def test_pass_6a_resolves_within_the_flagged_batch(self):
        """6a is only asked about `flagged`, and its own lookup already
        resolves within that batch -- so validating against every claim in
        the run would accept a revision that then lands on nothing."""
        with pytest.raises(PayloadShapeError, match="not one of the 1"):
            validate("Pass 6a",
                     [{"claim_id": "C3", "revised_text": "better"}],
                     claim_ids=["C1"])


# ---------------------------------------------------------------------------
# ---- The ensemble-only requirement (B7) -----------------------------------
# ---------------------------------------------------------------------------

class TestAssertedByCandidatesIsRequiredOnlyInEnsembleMode:

    def test_it_is_required_on_an_ensemble_run(self):
        """claim_extraction.md now asks for it on every claim in ensemble
        mode, because an ABSENT field is indistinguishable from an empty
        one at the scorer -- and `len([]) / n` is the MAXIMUM disagreement
        penalty, charged to a claim that exists because a candidate
        asserted it."""
        with pytest.raises(PayloadShapeError, match="asserted_by_candidates"):
            validate("Pass 2", [{"id": "c1", "text": "t", "type": "factual"}],
                     ensemble=True)

    def test_it_is_not_required_otherwise(self):
        """The control. Ensemble mode is off by default, and requiring the
        field on a single-candidate run would fail every ordinary run."""
        validate("Pass 2", [{"id": "c1", "text": "t", "type": "factual"}],
                 ensemble=False)

    def test_an_ensemble_claim_carrying_it_is_accepted(self):
        validate("Pass 2", [{"id": "c1", "text": "t", "type": "factual",
                             "asserted_by_candidates": [1, 2]}],
                 ensemble=True)

# ---------------------------------------------------------------------------
# ---- The members of a container (batch 149) -------------------------------
# ---------------------------------------------------------------------------

def _a_claim(**over) -> dict:
    raw = {"id": "c1", "text": "a claim", "type": "factual"}
    raw.update(over)
    return raw


class TestTheContainerIsDeclaredWhereItsMembersAreRead:
    """The three fields pure code iterates, measures or hashes.

    Each rejection has its control immediately below it, which for these
    is sharper than usual: all three fields are OPTIONAL, so a validator
    that merely demanded them would pass every rejection here and fail
    every ordinary run.
    """

    @pytest.mark.parametrize("value", [None, 123, 1.5, "madrid",
                                       {"madrid": 1}, True])
    def test_entities_must_be_an_array(self, value):
        """Pass 3a builds `sorted({e for c in claims for e in c.entities})`.
        None, a number and a boolean raise there; a STRING does not -- it
        iterates characters, so one entity became six, each a letter, and
        each one a thing the prompt then asks the model to search for."""
        with pytest.raises(PayloadShapeError, match="entities"):
            validate("Pass 2", [_a_claim(entities=value)])

    @pytest.mark.parametrize("value", [["madrid"], [], ["madrid", "1947"]])
    def test_an_array_of_strings_is_accepted(self, value):
        validate("Pass 2", [_a_claim(entities=value)])

    def test_and_an_absent_entities_key_is_accepted(self):
        """The control that matters most: base.py defaults the field to an
        empty list, and a claim with no named entity is an ordinary
        answer, so this is TYPED BUT NOT REQUIRED."""
        validate("Pass 2", [_a_claim()])

    @pytest.mark.parametrize("value", [5, None, "abc", True])
    def test_asserted_by_candidates_must_be_an_array(self, value):
        """Pass 5 computes `len(set(claim.asserted_by_candidates))` and
        divides by the roster size. `5` raises; `"abc"` scored
        `len(set("abc")) / 2` -- a numerator above the denominator, which
        clamps to FULL agreement, so a claim no candidate corroborated was
        scored as unanimous."""
        with pytest.raises(PayloadShapeError, match="asserted_by_candidates"):
            validate("Pass 2", [_a_claim(asserted_by_candidates=value)],
                     ensemble=True)

    def test_an_array_of_candidate_numbers_is_accepted(self):
        validate("Pass 2", [_a_claim(asserted_by_candidates=[1, 2])],
                 ensemble=True)

    @pytest.mark.parametrize("value", [123, None, "abc", {"k": 1}, 1.5, True])
    def test_every_per_claim_flag_value_must_be_an_array(self, value):
        """Pass 5 computes `penalty * len(claim.assumption_flags)`. The
        numbers raise; `"abc"` charged a THREE-flag penalty for one
        string, and `{"k": 1}` charged one flag for one key."""
        with pytest.raises(PayloadShapeError, match="per_claim_flags"):
            validate("Pass 4", {"per_claim_flags": {"c1": value}},
                     claim_ids=["c1"])

    def test_a_list_of_flags_is_accepted(self):
        validate("Pass 4", {"per_claim_flags": {"c1": ["an assumption"]}},
                 claim_ids=["c1"])

    def test_and_an_empty_flag_list_is_accepted(self):
        """assumption_audit.md asks the model to omit a clean claim rather
        than send an empty list, but a model that sends one has still said
        the same thing, and `len([])` is 0."""
        validate("Pass 4", {"per_claim_flags": {"c1": []}}, claim_ids=["c1"])

    @pytest.mark.parametrize("value", [123, None, "abc", {"a": 1}, True])
    def test_pass_0s_key_entities_must_be_an_array(self, value):
        """The Pass 0 checkpoint reports
        `len(run.plan.get('key_entities_or_subjects', []))`. The numbers
        raise; `"abc"` reported THREE key entities anticipated, from one
        word."""
        with pytest.raises(PayloadShapeError, match="key_entities_or_subjects"):
            validate("Pass 0", {"key_entities_or_subjects": value})

    def test_and_pass_0_still_accepts_a_plan_without_that_key(self):
        """preliminary_plan.md names five keys and the orchestrator reads
        one of them DEFENSIVELY. Requiring it would fail a run over an
        omission nothing depends on -- so the type is checked only when
        the key is there."""
        validate("Pass 0", {"outline": ["a point"]})
        validate("Pass 0", {})


class TestTheMembersAreDeclaredToo:
    """The owner's ruling, and the measurement behind it: a declared
    CONTAINER type does not close the crash, because the pure code
    downstream HASHES and SORTS what it iterates."""

    @pytest.mark.parametrize("value, why", [
        (["madrid", 1947], "sorted() cannot order an int against a str"),
        ([["madrid"]], "a list is unhashable"),
        ([{"name": "madrid"}], "a dict is unhashable"),
        ([None], "None orders against nothing"),
        ([1, 2], "two numbers are not two entity names"),
    ])
    def test_an_entity_that_is_not_a_string_is_rejected(self, value, why):
        """Every one of these is a LIST, so the container check accepts it
        and `sorted({...})` is where it lands."""
        with pytest.raises(PayloadShapeError, match="entities"):
            validate("Pass 2", [_a_claim(entities=value)])

    @pytest.mark.parametrize("value", [[{"k": 1}], [[1]], ["1"], [None],
                                       [True]])
    def test_a_candidate_number_that_is_not_a_number_is_rejected(self, value):
        with pytest.raises(PayloadShapeError, match="asserted_by_candidates"):
            validate("Pass 2", [_a_claim(asserted_by_candidates=value)],
                     ensemble=True)

    def test_the_message_names_the_member_that_was_wrong(self):
        """#76's whole complaint is that
        `'str' object has no attribute 'items'` names neither the pass nor
        the field. A member needs one more thing than a field does: WHICH
        member, since the model sent a list and has to find the item."""
        with pytest.raises(PayloadShapeError) as caught:
            validate("Pass 2", [_a_claim(entities=["madrid", 1947])])
        message = str(caught.value)
        assert "Pass 2" in message
        assert "entry 0" in message
        assert "entities[1]" in message, message
        assert "1947" in message
        assert "string" in message

    def test_a_flag_LIST_is_checked_and_the_strings_in_it_are_not(self):
        """B4's rule, kept rather than quietly widened: `len()` is the
        only thing that reads a flag list, so a type on its elements would
        be a check with no demonstrated consequence. This is the control
        that says the vocabulary did not grow further than the crash."""
        validate("Pass 4", {"per_claim_flags": {"c1": [1, None, {"k": 2}]}},
                 claim_ids=["c1"])

    def test_and_pass_3cs_gaps_members_are_not_checked_either(self):
        """Same rule, second instance. `build_coverage_gap_entries` wraps
        each gap in a dict without reading it, so the CONTAINER is typed
        (a string there would produce one entry per character) and the
        members are not."""
        validate("Pass 3c", {"gaps": [{"gap": "x"}, "a bare string", 7],
                             "coverage_score": 0.5})


class TestABooleanIsNotANumber:

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_severity_is_rejected(self, value):
        """`isinstance(True, int)` is True, so this satisfied NUMBER and
        Pass 5 computed `1.0 - True` as 0.0 -- the maximum critic penalty,
        with no trace line and no corrective retry. `false` took the
        opposite extreme the same way."""
        with pytest.raises(PayloadShapeError, match="severity"):
            validate("Pass 3b", [{"claim_id": "c1", "severity": value}],
                     claim_ids=["c1"])

    @pytest.mark.parametrize("value", [0, 1, 0.4, 0.0, 1.0])
    def test_a_number_severity_is_still_accepted(self, value):
        """The control, and it has to include 0 and 1: JSON has one number
        type, and a validator that rejected the integers alongside the
        booleans would refuse the two values the rubric asks for most."""
        validate("Pass 3b", [{"claim_id": "c1", "severity": value}],
                 claim_ids=["c1"])

    def test_the_rule_is_the_one_the_scorer_already_applies(self):
        """Not a new idea, only a new place for it. `source_quality`
        excludes bools from the same tuple when it reads a source's two
        numbers -- guarding a value that was supposed to have been
        rejected here."""
        import inspect

        from core.reasoning import confidence_scoring

        source = inspect.getsource(confidence_scoring.source_quality)
        assert "isinstance(v, bool)" in source


class TestWhatEachOfTheseChecksIsFor:
    """The consequence, not the rejection.

    A test that only asserts "the boundary refuses this" stays green if
    the pure code downstream is later made tolerant -- at which point the
    comment justifying the check is false and nothing says so (defect
    shape 9, which this marathon has now found eleven times). So each
    check is pinned to the code that reads the value.
    """

    def test_pass_5_measures_the_flag_list_it_is_given(self):
        from core.reasoning.base import Claim
        from core.reasoning.confidence_scoring import score_claim

        claim = Claim(id="c1", text="t", type="factual")
        claim.assumption_flags = 123
        with pytest.raises(TypeError, match="has no len"):
            score_claim(claim)

    def test_pass_5_hashes_the_candidate_list_it_is_given(self):
        from core.reasoning.base import Claim
        from core.reasoning.confidence_scoring import score_claim

        claim = Claim(id="c1", text="t", type="factual")
        claim.asserted_by_candidates = [{"candidate": 1}]
        with pytest.raises(TypeError, match="unhashable"):
            score_claim(claim, ensemble_n=2)

    def test_pass_3a_still_sorts_a_set_built_from_every_claims_entities(self):
        """A source relation, because the expression is inline in the
        orchestrator and cannot be called. If it ever stops being a
        `sorted(set)`, the member type on `entities` needs its reason
        re-stated rather than silently kept."""
        assert "sorted({e for c in factual_claims for e in c.entities})" \
            in _orchestrator_source()

    def test_pass_0s_checkpoint_still_measures_the_plan_key(self):
        assert "len(run.plan.get('key_entities_or_subjects', []))" \
            in _orchestrator_source()

    def test_and_a_claim_object_accepts_every_one_of_these_values(self):
        """The premise under all of it: base.py's annotations are
        ANNOTATIONS. Nothing between the payload and the pure code would
        have stopped any of these, which is why the boundary is the fix
        site."""
        from core.reasoning.base import Claim

        claim = Claim(id="c1", text="t", type="factual", entities=None)
        assert claim.entities is None
