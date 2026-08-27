# Pass 5 — Confidence Tiers (documentation only, NOT a live prompt)

This file is not loaded as a system prompt. Pass 5 makes zero LLM calls by
design — see `core/reasoning/confidence_scoring.py` for the actual
deterministic scoring logic. This file exists purely to document, for a
human reader, what that module computes and why.

**Tiers:** HIGH, MEDIUM, LOW, UNVERIFIED, UNVERIFIED_COVERAGE

- An ungrounded factual claim is always UNVERIFIED, regardless of how
  clean it looks under critique -- lack of grounding is disqualifying on
  its own for anything asserted as fact.
- Non-factual claims (synthesis/speculative) never went through source
  grounding at all (see Pass 2's D0 routing), so their score is capped
  below HIGH -- "clean under critique" isn't the same as "confirmed."
- UNVERIFIED_COVERAGE is not assigned to any actual Claim object -- it's
  attached to gaps identified by Pass 3c: things the response never
  addressed at all, which are an absence, not a claim that failed
  verification.

See `confidence_scoring.py`'s module docstring and the `GROUNDING_WEIGHTS`
/ weight-factor constants at the top of that file for the exact formula
and how to retune it.
