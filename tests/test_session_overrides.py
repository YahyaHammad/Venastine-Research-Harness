"""
test_session_overrides.py

ROADMAP_v2 §21's revisit, batch 31: `/window` and `/trigger`, ephemeral and
bound to the (provider, model) they were set for.

WHAT THIS FILE IS ABOUT. Not the arithmetic -- test_compaction.py owns the
thresholds and test_pin_tool.py owns #89's cap. This is about the two rules
that make the feature safe, and both are rules about when an override does
NOT apply:

  THE BINDING     a value set for one model must not reach another. The
                  window feeds _input_budget(), which gates the lossy
                  truncation in summarize_thread(), so a 1M number leaking
                  onto a 200k critic model discards thread content. Reading
                  a mismatched pair returns None and does NOT clear, so a
                  routine critic pass cannot destroy an override either.
  THE CLEAR       /model drops both outright, so switching away and back
                  gives the DERIVED value. The binding alone would restore
                  it, which is why both rules exist and neither is
                  redundant.

Everything here is offline: the overrides are module state and the reads are
pure functions of it plus config.
"""

import pytest

import config
from core import compaction, session


@pytest.fixture(autouse=True)
def _clean():
    """tests/conftest.py already clears this suite-wide; repeated here so
    the file is honest standalone and so a failure inside a test cannot
    leak a threshold into the next one."""
    session.clear()
    yield
    session.clear()


PAIR = ("ANTHROPIC", "claude-sonnet-5")
OTHER_MODEL = ("ANTHROPIC", "claude-haiku-4-5")
OTHER_PROVIDER = ("OPENAI", "claude-sonnet-5")


# ---------------------------------------------------------------------------
# ---- The binding ----------------------------------------------------------
# ---------------------------------------------------------------------------

def test_an_override_applies_to_the_pair_it_was_set_for():
    session.set_window(*PAIR, 400_000)
    assert session.window_for(*PAIR) == 400_000
    assert compaction.context_limit(PAIR[1], PAIR[0]) == 400_000


@pytest.mark.parametrize("other", [OTHER_MODEL, OTHER_PROVIDER])
def test_another_model_or_provider_does_not_see_it(other):
    """An agent pinning its own model (§18) and a critic-routed pass (§11)
    both reach compaction on a pair the user never typed. They must get
    that model's own window, not a number tuned for a different one."""
    # A value no table entry and no default can produce, so "did not
    # apply" is distinguishable from "applied and happened to agree" --
    # OTHER_PROVIDER deliberately reuses the same MODEL name, which the
    # table answers for identically.
    session.set_window(*PAIR, 777_777)

    assert session.window_for(*other) is None
    # ...and the resolved answer falls through to the ordinary sources.
    assert compaction.context_limit(other[1], other[0]) != 777_777


def test_a_mismatched_read_does_not_clear_the_override():
    """THE HALF THAT IS EASY TO GET WRONG. If a mismatch cleared, one
    routine critic pass mid-research would silently destroy an override
    set for the chat, and the user would have no idea which command did
    it."""
    session.set_window(*PAIR, 400_000)

    session.window_for(*OTHER_MODEL)
    session.window_for(*OTHER_PROVIDER)
    compaction.context_limit(OTHER_MODEL[1], OTHER_MODEL[0])

    assert session.window_for(*PAIR) == 400_000


def test_no_provider_means_no_override():
    """compaction.context_limit still accepts callers that do not know the
    provider. Without one no pair can match, which is the same answer as
    having no override -- and is what keeps every pre-batch-31 call site
    behaving exactly as it did."""
    session.set_window(*PAIR, 400_000)
    assert session.window_for(None, PAIR[1]) is None
    assert compaction.context_limit(PAIR[1]) == 1_000_000


# ---------------------------------------------------------------------------
# ---- The clear ------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_clear_drops_everything_and_reports_what_it_dropped():
    session.set_window(*PAIR, 400_000)
    session.set_trigger(*PAIR, 80_000)

    assert sorted(session.clear()) == ["trigger", "window"]
    assert session.window_for(*PAIR) is None
    assert session.trigger_for(*PAIR) is None
    # Nothing to say the second time -- the shell prints only real drops.
    assert session.clear() == []


def test_switching_away_and_back_gives_the_default_not_the_old_value():
    """The owner's rule, stated exactly: 'go back to the default value even
    if switched to the model before that for which the value was changed'.

    This is why /model clears rather than relying on the binding. Under the
    binding alone the value would still be stored against the original pair
    and would reappear the moment that pair came back."""
    session.set_window(*PAIR, 400_000)
    derived = compaction.context_limit(OTHER_MODEL[1], OTHER_MODEL[0])

    session.clear()                       # what /model does on the way out
    assert compaction.context_limit(OTHER_MODEL[1], OTHER_MODEL[0]) == derived
    # ...and coming back to the original model does NOT restore it.
    assert session.window_for(*PAIR) is None
    assert compaction.context_limit(PAIR[1], PAIR[0]) == 1_000_000


def test_one_key_can_be_cleared_without_the_other():
    """`/window off` must not take the trigger with it."""
    session.set_window(*PAIR, 400_000)
    session.set_trigger(*PAIR, 80_000)

    assert session.clear(session.WINDOW) == ["window"]
    assert session.window_for(*PAIR) is None
    assert session.trigger_for(*PAIR) == 80_000


# ---------------------------------------------------------------------------
# ---- Source order ---------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_window_override_beats_the_provider_query():
    """Source 0 above source 1. The query can be wrong in the direction
    that matters -- a beta-gated 1M window reports as 200k -- so a number
    a human set on purpose has to win."""
    from core import client as client_module

    client_module._context_window_cache[PAIR] = 200_000
    assert compaction.context_limit(PAIR[1], PAIR[0]) == 200_000

    session.set_window(*PAIR, 1_000_000)
    assert compaction.context_limit(PAIR[1], PAIR[0]) == 1_000_000


def test_the_trigger_override_moves_the_working_set_threshold():
    _, default_at = compaction.thresholds(PAIR[1], provider_name=PAIR[0])
    assert default_at == config.COMPACTION_TRIGGER_TOKENS

    session.set_trigger(*PAIR, 80_000)
    warn_at, compact_at = compaction.thresholds(PAIR[1], provider_name=PAIR[0])

    assert compact_at == 80_000
    assert warn_at == 80_000 - config.COMPACTION_WARNING_MARGIN_TOKENS


def test_an_explicit_compact_override_still_wins():
    """D27's chain is unchanged: the session value arrives through the
    per-invocation slot, and a caller that passes its own is nearer."""
    session.set_trigger(*PAIR, 80_000)

    _, compact_at = compaction.thresholds(
        PAIR[1], overrides={"trigger_tokens": 25_000}, provider_name=PAIR[0])
    assert compact_at == 25_000


def test_the_trigger_override_does_not_touch_the_backstop():
    """M1's separation, from the other direction. The two commands move two
    different numbers on two different branches, and a /trigger must not
    quietly reshape an unattended research pass."""
    session.set_trigger(*PAIR, 80_000)

    _, compact_at = compaction.thresholds(
        PAIR[1], mode="backstop", provider_name=PAIR[0])
    assert compact_at == (compaction.context_limit(PAIR[1], PAIR[0])
                          - config.COMPACTION_PIPELINE_BACKSTOP_TOKENS)


def test_the_window_override_moves_the_backstop_and_the_input_budget():
    session.set_window(*PAIR, 300_000)

    _, compact_at = compaction.thresholds(
        PAIR[1], mode="backstop", provider_name=PAIR[0])
    assert compact_at == 300_000 - config.COMPACTION_PIPELINE_BACKSTOP_TOKENS
    assert compaction._input_budget(PAIR[1], PAIR[0]) == (
        300_000 - config.COMPACTION_PIPELINE_BACKSTOP_TOKENS)


# ---------------------------------------------------------------------------
# ---- #89's pin cap --------------------------------------------------------
# ---------------------------------------------------------------------------

def test_trigger_any_ignores_the_binding():
    """Its one caller, pin_measurements, has no pair to match against."""
    session.set_trigger(*PAIR, 12_000)
    assert session.trigger_any() == 12_000
    assert session.trigger_for(*OTHER_MODEL) is None


def test_the_pin_cap_follows_a_LOWERED_session_trigger(mocker):
    """#89 must not become violable through a different command.

    The cap is PIN_MAX_TRIGGER_FRACTION x the trigger. Read the CONFIGURED
    trigger while a session set 12k and the cap stays at 20k -- so a pin
    could protect more than the entire trigger and floor the thread
    permanently above it, which is the precise defect #89 exists to
    prevent.
    """
    mocker.patch("storage._ordered_rows", return_value=[])

    before = compaction.pin_measurements("t", 1)["max_tokens"]
    assert before == int(config.PIN_MAX_TRIGGER_FRACTION
                         * config.COMPACTION_TRIGGER_TOKENS)

    session.set_trigger(*PAIR, 12_000)
    after = compaction.pin_measurements("t", 1)["max_tokens"]

    assert after == int(config.PIN_MAX_TRIGGER_FRACTION * 12_000)
    assert after < before


def test_a_RAISED_session_trigger_leaves_the_pin_cap_stricter(mocker):
    """The min() in the other direction. Erring strict is safe; erring
    loose is #89 again."""
    mocker.patch("storage._ordered_rows", return_value=[])

    session.set_trigger(*PAIR, 200_000)
    assert compaction.pin_measurements("t", 1)["max_tokens"] == int(
        config.PIN_MAX_TRIGGER_FRACTION * config.COMPACTION_TRIGGER_TOKENS)


# ---------------------------------------------------------------------------
# ---- Nothing persists -----------------------------------------------------
# ---------------------------------------------------------------------------

def test_nothing_reaches_the_settings_schema():
    """The whole point is that these do not survive a restart. If either
    name ever becomes a settings.json key, a cloned project could ship one
    behind a trust prompt -- the shape G7 rejected shell_approval_mode for
    and R12 rejected research.granted_tools for."""
    from core.config_loader import _KNOWN_COMPACTION

    assert "context_window" not in _KNOWN_COMPACTION
    assert "window" not in _KNOWN_COMPACTION
