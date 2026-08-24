"""
core/session.py

Ephemeral, per-session overrides of two compaction numbers, and nothing
else. ROADMAP_v2 §21's revisit, batch 31.

WHAT THIS MODULE OWNS
  set_window / set_trigger    record an override against a (provider, model)
  window_for / trigger_for    read one back, but only for that same pair
  trigger_any                 the binding-free read, for #89's pin cap
  clear                       drop everything, on a /model switch

WHAT IT DOES NOT OWN
  Resolving the numbers -- core/compaction.py:context_limit and
  thresholds() do that, and they treat what is here as the most local
  source rather than as the answer. Persisting anything: NOTHING here
  reaches disk, which is the whole point (see NEVER PERSISTED below).

TWO NUMBERS, NOT ONE, because they are not the same number and treating
them as one is what made this feature ambiguous to specify:

  window   what context_limit() returns. Feeds the RESEARCH-PASS backstop
           (context_limit - COMPACTION_PIPELINE_BACKSTOP_TOKENS) and the
           summarizer's one-call input budget. It does NOT affect when an
           ordinary chat compacts.
  trigger  the working-set ceiling, config.COMPACTION_TRIGGER_TOKENS. An
           ABSOLUTE size, not a margin below the window (M1) -- a 200k
           model and a 1M model both compact at 40k in chat. THIS is the
           number that decides when a chat thread folds.

BOUND TO A (provider, model) PAIR, AND THAT IS THE INTERESTING RULE.
An override applies only when the model that was named is the model
actually running. A read from any other pair returns None WITHOUT
clearing, so:

  * an agent that pins its own model (§18) uses that model's derived
    value, rather than a number tuned for a different one;
  * a critic-routed research pass (§11) does the same -- and, because a
    mismatch does not clear, one routine critic pass cannot silently
    destroy an override set for the chat;
  * a research pass or subagent on the SESSION's model matches, so it
    inherits with no plumbing at all. Propagation is a consequence of the
    binding, not a separate mechanism.

Clearing is a separate act, and only /model does it. That is what makes
"switch away and come back" return the DEFAULT rather than the old
override: the pair-binding alone would restore it, so both rules are
needed and they do different jobs.

NEVER PERSISTED. Not settings.json (project tier beats user tier there,
D29 -- and a value that survives a restart is not what was asked for),
not extra_data, not anywhere. Restarting the harness is the guaranteed
way back to the derived numbers.

MODULE STATE, written from the UI thread and read from worker threads.
Same posture as core/compaction.py's `_compacting` flag and for the same
reason: these are single assignments of an int or a tuple, so there is no
half-set state to observe. Tests clear it in an autouse fixture, which is
the rule every other module-level cache in this project follows.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# key -> (provider_name, model, value). One slot each; setting replaces.
_overrides: dict = {}

WINDOW = "window"
TRIGGER = "trigger"


def _set(key: str, provider_name: str, model: str, value: int) -> None:
    _overrides[key] = (provider_name, model, int(value))
    logger.info("Session %s override: %s for %s/%s (this session only).",
                key, value, provider_name, model)


def _get(key: str, provider_name: Optional[str],
         model: Optional[str]) -> Optional[int]:
    """The override for exactly this pair, or None.

    A pair mismatch is not an error and does not clear -- see the module
    docstring. `provider_name` is Optional because compaction.context_limit
    still accepts callers that do not know it; without a provider no
    override can match, which is the same answer as having none.
    """
    entry = _overrides.get(key)
    if entry is None or not provider_name:
        return None
    stored_provider, stored_model, value = entry
    if stored_provider == provider_name and stored_model == model:
        return value
    return None


def set_window(provider_name: str, model: str, tokens: int) -> None:
    """Override what context_limit() reports for this (provider, model)."""
    _set(WINDOW, provider_name, model, tokens)


def set_trigger(provider_name: str, model: str, tokens: int) -> None:
    """Override the working-set compaction ceiling for this pair."""
    _set(TRIGGER, provider_name, model, tokens)


def window_for(provider_name: Optional[str],
               model: Optional[str]) -> Optional[int]:
    return _get(WINDOW, provider_name, model)


def trigger_for(provider_name: Optional[str],
                model: Optional[str]) -> Optional[int]:
    return _get(TRIGGER, provider_name, model)


def trigger_any() -> Optional[int]:
    """The trigger override IGNORING the pair binding.

    Exists for exactly one caller: compaction.pin_measurements, which
    computes #89's cap as PIN_MAX_TRIGGER_FRACTION x the trigger and has
    no model in scope to match against. That caller takes the MINIMUM of
    this and the configured trigger, which is safe under every binding --
    a raised session trigger leaves the cap stricter than it needs to be,
    and a lowered one pulls the cap down with it. Reading the configured
    trigger alone would let `/trigger 12k` leave pins capped at 20k, able
    to floor a thread permanently above its own trigger, which is the
    defect #89 exists to prevent.

    Do not reach for this anywhere the pair IS available.
    """
    entry = _overrides.get(TRIGGER)
    return entry[2] if entry else None


def clear(key: Optional[str] = None) -> list:
    """Drop overrides and report which were actually dropped.

    Called with no key by /model, which is the ONLY thing that clears
    both. `/window off` and `/trigger off` clear one. The return value is
    what the shell says out loud -- a switch that silently discarded a
    number the user set would be exactly the invisible state this feature
    is careful not to introduce.
    """
    keys = [key] if key else [WINDOW, TRIGGER]
    dropped = [name for name in keys if _overrides.pop(name, None) is not None]
    if dropped:
        logger.info("Session overrides cleared: %s.", ", ".join(dropped))
    return dropped


def state() -> dict:
    """{key: (provider, model, value)} for the bare-command report."""
    return dict(_overrides)
