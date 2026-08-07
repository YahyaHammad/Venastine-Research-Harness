"""
test_interaction.py

ROADMAP_v2 §23 (AC1): the response channel, tested on its own before
anything is wired to it.

WHAT IS WORTH ASSERTING. The dataclasses are nearly free to get right; the
whole value of this module is that ONE function decides what a non-answer
means. So these tests are about `decode` and `ask` refusing to be
permissive, kind by kind and route by route:

  * every route into an answer -- no channel, a raising callback, a
    timeout's None, a shutdown release's bare False, a wrong-shaped
    return -- lands on the same declining default
  * the three-way sign-off answer survives, because None (deny the spawn)
    and set() (spawn, grant nothing) are DIFFERENT and collapsing them is
    the easy mistake
  * review stays strict, because it is the only kind whose permissive
    failure applies an edit rather than declining one

The invariant that ties it together and is pinned below:
`decode(Request(kind), None) == SAFE_DEFAULTS[kind]` for every kind. That
is what lets `ask()` route its failure paths through `decode` instead of
reading the table, so a kind added later cannot carry a default the
decoder disagrees with.

`decode` takes the REQUEST rather than the kind because two kinds cannot
be validated without knowing what was asked: a choice is only valid
against the options offered, and a sign-off subset only against the
candidates listed.
"""

import pytest

from core import interaction
from core.interaction import (
    APPROVAL, CHOICE, CONFIRM, REVIEW, SAFE_DEFAULTS, SUBAGENT_SIGNOFF,
    Request, ResponseChannel, ask, decode,
)


def _channel(answer):
    return ResponseChannel(ask=lambda _request: answer)


def _raising_channel():
    def _boom(_request):
        raise RuntimeError("the shell fell over")
    return ResponseChannel(ask=_boom)


# ---------------------------------------------------------------------------
# ---- The invariant ---------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheDefaultInvariant:

    @pytest.mark.parametrize("kind", sorted(SAFE_DEFAULTS))
    def test_decoding_nothing_gives_the_declining_default(self, kind):
        """What lets ask() funnel its failure paths through decode()."""
        assert decode(Request(kind=kind), None) == SAFE_DEFAULTS[kind]

    @pytest.mark.parametrize("kind", sorted(SAFE_DEFAULTS))
    def test_every_default_declines(self, kind):
        """The asymmetry IS the design: no default may be permissive."""
        default = SAFE_DEFAULTS[kind]
        assert default in (False, None) or default == ("reject", "")

    def test_an_unknown_kind_raises_rather_than_defaulting(self):
        """An unanswerable question fails safe; an undefined one is a bug,
        and inventing 'no' for it would hide the typo that built it."""
        with pytest.raises(ValueError):
            decode(Request(kind="summon_a_wizard"), True)


# ---------------------------------------------------------------------------
# ---- Routes ----------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestEveryRouteDeclines:

    @pytest.mark.parametrize("kind", sorted(SAFE_DEFAULTS))
    def test_no_channel_declines(self, kind):
        """§25's V6, and the reason this module exists: the inability to
        ask is not permission to proceed."""
        assert ask(None, Request(kind=kind)) == SAFE_DEFAULTS[kind]

    @pytest.mark.parametrize("kind", sorted(SAFE_DEFAULTS))
    def test_a_raising_channel_declines(self, kind):
        assert ask(_raising_channel(),
                   Request(kind=kind)) == SAFE_DEFAULTS[kind]

    def test_a_raising_channel_is_logged_with_its_traceback(self, caplog):
        """A shell raising here is a real bug and the user's answer was
        discarded — it must not be swallowed silently."""
        with caplog.at_level("ERROR"):
            ask(_raising_channel(), Request(kind=APPROVAL))
        assert any("the shell fell over" in r.getMessage()
                   or r.exc_info for r in caplog.records)

    @pytest.mark.parametrize("kind", sorted(SAFE_DEFAULTS))
    def test_a_shutdown_releases_bare_false_declines(self, kind):
        """`_release_permission_channel` puts a bare False on every
        parked queue. For approval that IS the answer; for the others it
        is a value of the wrong type and must not be read as one."""
        assert ask(_channel(False), Request(kind=kind)) == SAFE_DEFAULTS[kind]

    def test_an_unknown_kind_still_raises_through_ask(self):
        with pytest.raises(ValueError):
            ask(None, Request(kind="summon_a_wizard"))


# ---------------------------------------------------------------------------
# ---- Approval --------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestApproval:

    def test_true_approves(self):
        assert ask(_channel(True), Request(kind=APPROVAL)) is True

    def test_false_denies(self):
        assert ask(_channel(False), Request(kind=APPROVAL)) is False

    @pytest.mark.parametrize("raw", [None, "", 0, [], {}])
    def test_falsy_answers_deny(self, raw):
        assert ask(_channel(raw), Request(kind=APPROVAL)) is False

    def test_the_request_reaches_the_channel_intact(self):
        seen = []
        channel = ResponseChannel(ask=lambda r: (seen.append(r), True)[1])
        ask(channel, Request(kind=APPROVAL,
                             payload={"tool_name": "shell", "params": {"a": 1}},
                             notice="runs a command"))
        assert seen[0].payload["tool_name"] == "shell"
        assert seen[0].notice == "runs a command"

    def test_honour_run_scope_defaults_on_and_is_carried(self):
        """§25 R11: attended mode sets it False so one yes cannot cover
        later calls. The flag lives on the channel, not on the answer."""
        assert ResponseChannel(ask=lambda r: True).honour_run_scope is True
        assert ResponseChannel(ask=lambda r: True,
                               honour_run_scope=False).honour_run_scope is False


# ---------------------------------------------------------------------------
# ---- Subagent sign-off -----------------------------------------------------
# ---------------------------------------------------------------------------

class TestSubagentSignoff:

    @staticmethod
    def _request(candidates=("shell", "fetch_url")):
        return Request(kind=SUBAGENT_SIGNOFF,
                       payload={"agent": "worker",
                                "candidates": list(candidates)})

    def test_a_subset_comes_back_as_a_set_of_names(self):
        answer = ask(_channel({"shell", "fetch_url"}), self._request())
        assert answer == {"shell", "fetch_url"}

    def test_a_list_is_accepted_and_normalised(self):
        """A shell building the answer from a SelectionList hands back a
        list; the loop compares against a set."""
        assert ask(_channel(["shell"]), self._request()) == {"shell"}

    def test_an_empty_set_is_a_real_answer_not_a_denial(self):
        """Spawn the subagent, grant it nothing. Distinct from None, and
        collapsing the two is the easy mistake — GrantPickerScreen draws
        the same three-way distinction for the same reason."""
        answer = ask(_channel(set()), self._request())
        assert answer == set()
        assert answer is not None

    def test_none_denies_the_spawn(self):
        assert ask(_channel(None), self._request()) is None

    @pytest.mark.parametrize("raw", [True, False, "shell", 3, {"a": 1}])
    def test_anything_else_denies_the_spawn(self, raw):
        """Notably True: a shell that answered this like an approval must
        not accidentally grant the child everything."""
        assert ask(_channel(raw), self._request()) is None

    def test_a_name_that_was_not_offered_is_not_granted(self):
        """The sign-off's whole substance is that the list SHOWN and the
        list GRANTED are the same -- agents/manager.py's
        candidate_approvals() says so in its own docstring. A shell
        returning a name nobody proposed is answering a different
        question, and this is why decode takes the request."""
        answer = ask(_channel({"shell", "rm_minus_rf"}), self._request())
        assert answer == {"shell"}

    def test_nothing_offered_means_nothing_granted(self):
        assert ask(_channel({"shell"}), self._request(candidates=())) == set()


# ---------------------------------------------------------------------------
# ---- Review ----------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestReview:

    @pytest.mark.parametrize("decision", ["accept", "reject", "refine",
                                          "reject_all"])
    def test_all_four_decisions_survive(self, decision):
        """Four outcomes, not two. Reject and refine are different
        answers, and reject_all is the escape a long review needs."""
        assert ask(_channel((decision, "note")),
                   Request(kind=REVIEW)) == (decision, "note")

    def test_missing_notes_become_empty_string(self):
        assert ask(_channel(("accept", None)),
                   Request(kind=REVIEW)) == ("accept", "")

    def test_a_bare_decision_string_is_rejected(self):
        """The two decoders this replaced DISAGREED here — review.py
        accepted a bare string, tui/app.py did not. The strict rule wins
        because this is the only kind whose permissive failure APPLIES an
        edit rather than declining one."""
        assert ask(_channel("accept"), Request(kind=REVIEW)) == ("reject", "")

    def test_a_one_element_tuple_is_rejected(self):
        assert ask(_channel(("accept",)), Request(kind=REVIEW)) == ("reject", "")

    def test_an_unrecognised_decision_is_rejected(self):
        assert ask(_channel(("maybe", "")),
                   Request(kind=REVIEW)) == ("reject", "")

    @pytest.mark.parametrize("raw", [None, True, {}, ("a", "b", "c")])
    def test_malformed_answers_reject(self, raw):
        assert ask(_channel(raw), Request(kind=REVIEW)) == ("reject", "")


# ---------------------------------------------------------------------------
# ---- Confirm and choice (§24's two, migrated) ------------------------------
# ---------------------------------------------------------------------------

class TestConfirm:

    def test_yes_and_no(self):
        assert ask(_channel(True), Request(kind=CONFIRM)) is True
        assert ask(_channel(False), Request(kind=CONFIRM)) is False

    def test_a_dismissal_declines(self):
        assert ask(_channel(None), Request(kind=CONFIRM)) is False

    def test_it_is_a_separate_kind_from_approval(self):
        """Both answer with a bool, and the KIND is what tells a shell how
        to render: APPROVAL is a gated call with params, CONFIRM is a
        titled yes/no over text the caller composed."""
        assert CONFIRM != APPROVAL


class TestChoice:

    @staticmethod
    def _request():
        return Request(kind=CHOICE,
                       payload={"options": ["software", "research"]})

    def test_an_offered_option_comes_back(self):
        assert ask(_channel("research"), self._request()) == "research"

    def test_an_unoffered_answer_is_no_choice_at_all(self):
        assert ask(_channel("webapp"), self._request()) is None

    def test_a_dismissal_picks_nothing(self):
        """None, never the first option: nobody answered is not a choice,
        and picking one on the user's behalf is what a declining default
        exists to avoid."""
        assert ask(_channel(None), self._request()) is None

    def test_the_shutdown_releases_false_picks_nothing(self):
        """`_release_permission_channel` puts a bare False. Without the
        options check that False would be returned as if it were a
        choice -- this is the guard §24 shipped by hand in tui/app.py."""
        assert ask(_channel(False), self._request()) is None


# ---------------------------------------------------------------------------
# ---- Nothing is wired yet --------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_module_is_a_leaf():
    """Matches core/approval.py: core/loop.py, main.py, tui/app.py and
    core/reasoning/ all depend on this and it depends on none of them. A
    project import here would invert that and eventually cycle."""
    import ast
    import pathlib

    source = pathlib.Path(interaction.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"dataclasses", "typing", "logging"}, imported
