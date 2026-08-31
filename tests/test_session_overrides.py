"""
test_session_overrides.py

ROADMAP_v2 §21's revisit, batch 31: `/window` and `/trigger`, bound to the
(provider, model) they were set for.

WHAT THIS FILE IS ABOUT. Not the arithmetic -- test_compaction.py owns the
thresholds and test_pin_tool.py owns #89's cap. This is about the two rules
that make the feature safe, and both are rules about when an override does
NOT apply:

  THE BINDING     a value set for one model must not reach another. The
                  window feeds _input_budget(), which gates the lossy
                  truncation in summarize_thread(), so a 1M number leaking
                  onto a 200k critic model discards thread content. Reading
                  a mismatched pair returns None, so an agent that pins its
                  own model and a critic-routed pass both get their own.
  THE LIFETIME    which differs between the two since batch 44, and that is
                  now the most interesting thing here. /trigger is
                  EPHEMERAL and /model clears it, so switching away and
                  back gives the derived value. /window is REMEMBERED, in
                  core/model_windows.py's user-tier store, because a
                  context window is a fact about a deployment rather than a
                  preference about a session -- it survives the switch and
                  the restart, and `/window off` is the only thing that
                  drops it.

Everything here is offline: the trigger is module state, the window is a
JSON file conftest redirects per test, and the reads are pure functions of
those plus config.
"""

import pytest

import config
from core import compaction, model_windows, session


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
    assert model_windows.remember_window(*PAIR, 400_000)
    assert model_windows.window_for(*PAIR) == 400_000
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
    model_windows.remember_window(*PAIR, 777_777)

    assert model_windows.window_for(*other) is None
    # ...and the resolved answer falls through to the ordinary sources.
    assert compaction.context_limit(other[1], other[0]) != 777_777


def test_every_pair_is_kept_not_just_the_latest():
    """Batch 44's reason for a store rather than a slot. §31 held ONE
    window bound to ONE pair, so answering for a second model discarded
    the first -- and the whole point of remembering is that a window is
    entered once per model and never again."""
    model_windows.remember_window(*PAIR, 400_000)
    model_windows.remember_window(*OTHER_MODEL, 128_000)

    assert model_windows.window_for(*PAIR) == 400_000
    assert model_windows.window_for(*OTHER_MODEL) == 128_000
    assert model_windows.all_windows() == {PAIR: 400_000,
                                           OTHER_MODEL: 128_000}


def test_a_mismatched_read_leaves_the_entry_alone():
    """A routine critic pass mid-research reads a pair the user never
    typed; it must not disturb what they did type."""
    model_windows.remember_window(*PAIR, 400_000)

    model_windows.window_for(*OTHER_MODEL)
    model_windows.window_for(*OTHER_PROVIDER)
    compaction.context_limit(OTHER_MODEL[1], OTHER_MODEL[0])

    assert model_windows.window_for(*PAIR) == 400_000


def test_no_provider_means_no_override():
    """compaction.context_limit still accepts callers that do not know the
    provider. Without one no pair can match, which is the same answer as
    having no override -- and is what keeps every pre-batch-31 call site
    behaving exactly as it did."""
    model_windows.remember_window(*PAIR, 400_000)
    assert model_windows.window_for(None, PAIR[1]) is None
    assert compaction.context_limit(PAIR[1]) == 1_000_000


def test_a_hand_edited_nonsense_value_costs_the_derived_window(tmp_path):
    """The store is a plain JSON file a person can open. A bad entry must
    cost the derived window rather than the session -- and a bool is not a
    window, for _record_window's reason one layer down."""
    import json

    with open(model_windows.store_path(), "w", encoding="utf-8") as f:
        json.dump({"version": model_windows.STORE_VERSION,
                   "windows": {"ANTHROPIC|claude-sonnet-5": True,
                               "ANTHROPIC|claude-haiku-4-5": -5}}, f)

    assert model_windows.window_for(*PAIR) is None
    assert compaction.context_limit(PAIR[1], PAIR[0]) == 1_000_000


# ---------------------------------------------------------------------------
# ---- The lifetimes, which are not the same one ----------------------------
# ---------------------------------------------------------------------------

def test_clear_drops_the_trigger_and_reports_it():
    session.set_trigger(*PAIR, 80_000)

    assert session.clear() == ["trigger"]
    assert session.trigger_for(*PAIR) is None
    # Nothing to say the second time -- the shell prints only real drops.
    assert session.clear() == []


def test_a_model_switch_does_not_forget_a_remembered_window():
    """Batch 44 reversed §31 here, deliberately.

    §31's rule was that /model clears both, so switching away and back
    gives the derived value. That is still right for the trigger, which is
    a knob for a thread that is behaving badly. It was wrong for the
    window: re-entering the same fact about the same endpoint on every
    switch is exactly what nobody does, so the value would never be set at
    all."""
    model_windows.remember_window(*PAIR, 400_000)
    session.set_trigger(*PAIR, 80_000)

    session.clear()                       # what /model does on the way out

    assert session.trigger_for(*PAIR) is None
    assert model_windows.window_for(*PAIR) == 400_000
    assert compaction.context_limit(PAIR[1], PAIR[0]) == 400_000


def test_window_off_drops_one_entry_and_says_which_outcome():
    """Three outcomes, because the shell says a different sentence for
    each -- and a failed write reported as "nothing was set" would be a
    lie about a file the user can go and read."""
    model_windows.remember_window(*PAIR, 400_000)
    model_windows.remember_window(*OTHER_MODEL, 128_000)

    assert model_windows.forget_window(*PAIR) == "removed"
    assert model_windows.window_for(*PAIR) is None
    assert model_windows.window_for(*OTHER_MODEL) == 128_000

    assert model_windows.forget_window(*PAIR) == "absent"


def test_a_write_that_cannot_land_is_reported_not_swallowed(
        tmp_path, monkeypatch):
    """/window claims "Remembered" only on a write that landed, matching
    /theme and /model. The WARNING reaches the transcript through
    TranscriptLogHandler.

    Blocked by a FILE where the directory would go, rather than by an
    unwritable absolute path: `os.makedirs("/nope")` succeeds on Windows,
    where the drive root is writable, so that version of this test passed
    for the wrong reason on POSIX and failed outright here."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(model_windows, "store_path",
                        lambda: str(blocker / "model_windows.json"))

    assert model_windows.remember_window(*PAIR, 400_000) is False
    assert model_windows.window_for(*PAIR) is None


# ---------------------------------------------------------------------------
# ---- Source order ---------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_remembered_window_beats_the_provider_query():
    """Source 0 above source 1, and the inversion is deliberate.

    Everywhere else in this project a measured value beats a configured
    one, because a static table goes stale. Here the direction flips: a
    query can be confidently wrong -- a gateway reporting its own default
    rather than the deployment's -- while a number someone typed is a
    statement about the deployment they are actually running."""
    from core import client as client_module

    client_module._context_window_cache[PAIR] = 200_000
    assert compaction.context_limit(PAIR[1], PAIR[0]) == 200_000

    model_windows.remember_window(*PAIR, 1_000_000)
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
    model_windows.remember_window(*PAIR, 300_000)

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
