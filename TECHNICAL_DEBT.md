# Technical Debt

High-level debt themes surfaced by the §19–§20 review (2026-08-04), with
what the fix pass did about each and what remains by design. Finding ids
refer the review report `.qwen/reviews/2026-08-04-223903-local.md`.

## 1. Review-stage durability lagged its documentation

The §20 docs promise "the review still runs and still records", but the
findings' content, the refinement process, and the artifact write all lived
only until the shells' last step.

- **Fixed:** per-finding trace lines before the consent walk (r1-2);
  refinement notes/rounds/superseded proposals in the decision record
  (r4-2); the TUI summary no longer asserts a 07_review.json that a
  swallowed write failure never produced (r4-3).
- **Remaining (by design):** `subagent_reviews` has no DB column (V8) and
  07_review.json is written last by each shell (V9). Both are locked
  decisions; the trace is the durable channel. If a future consumer needs
  structured decisions in the DB, that is a V8 revisit, not a bugfix.

## 2. Failure-containment policy was split across documents

review.py's prose said an optional stage must not lose a completed run;
CLAUDE.md said reviewer failures land on `status='failed'`. Both halves of
the changeset stated opposite policies (f1).

- **Fixed:** transient reviewer failures (provider error, unrecoverable
  JSON) are contained like the missing-agent branch — traced skip, run
  completes; CLAUDE.md rewritten to one policy. Deferred commit (f4) keeps
  a failed re-synthesis from persisting corrected claims beside a stale
  report.
- **Remaining:** a failure that escapes the stage itself (a bug, not a
  transient reviewer error) still flips the run to `failed` — intended,
  test-pinned (f16).

## 3. The shared JSON-retry abstraction leaked pass-shape

The extraction generalized the system prompt and the audit hook but not
the corrective wording ("the JSON object the pass asked for"), which could
talk an array-contract caller out of its own format (r5-1).

- **Fixed:** caller-neutral wording + caller-neutral parse-error text.
- **Remaining:** no explicit shape hint parameter; the generalized wording
  covers today's two callers. If a third caller with an exotic contract
  appears, add the hint then.

## 4. Consent-walk hardening cluster

The walk was fail-safe in direction but loose at the edges: unhashable
input crashed instead of dropping (r2-2), same-target duplicates
overwrote silently (r1-3), tier overrides left a stale annotation (r3-1),
equal-value "corrections" spent a re-synthesis (r4-1), the refine loop made
one send-back too many (f2), and refinements skipped the JSON-retry
recovery (f5).

- **Fixed:** all six, each with a regression test.
- **Remaining:** r5-1's trigger frequency is model behaviour and is not
  measurable in the offline suite; the wording fix is the mitigation.

## 5. TUI shutdown/consent lifecycle

The one-shot channel release assumed one parked ask; the review walk
re-arms per ask (r1-1 hang), and `--attended --review` spawned two stdin
pumps racing for one stdin (f3).

- **Fixed:** `_shutting_down` flag consulted by both blocking ask paths;
  one `_StdinReader` per process shared by both builders.
- **Remaining (by design):** unanswered asks still wait out the 600 s
  attended timeout in the normal (non-shutdown) path — R9's degrade-and-
  continue trade.

## 6. Test vacuity pockets in the newest surfaces

Several headline behaviours were pinned by tests that could not fail:
the K1 test called `prompt_fragment` directly instead of the pinning site
(f13), the propagation test never composed the stage with the pipeline's
except (f16), the TUI review-ON handoff was unexercised (f17), fail-closed
refine branches untested (f18), and the walk-order test re-sorted away the
very order it existed to check (r3-2).

- **Fixed:** all five rewritten/added as mutation-sighted tests.
- **Standing practice:** mutation-check any new §19/§20-adjacent test
  before trusting it; the BREAKING_CHANGES tables now name the
  mutation-sighted forms.

## 7. Documentation drift

The index/ARCHITECTURE entries lagged the code (f7 namespace-package claim
vs a real `__init__.py`; f8 missing **(BUILT)** marker).

- **Fixed:** `skills/__init__.py` deleted (namespace package like
  agents/tools/tui, per owner decision); index marker added.
- **Standing debt:** doc counts and status lines drift on every section.
  A mechanical doc-consistency check (test counts, BUILT markers, tree
  entries) would retire this class permanently; until then every section
  landing should run the doc cross-check the review agents did by hand.

## Accepted risks noted in the review, deliberately not "fixed"

- `07_review.json` absent on zero-finding reviewed runs — presence-implies-
  reviewed is the documented signal; trace.md still carries the count line.
- Advisory-only K2 notes after `/agent` switch (f21 fixed to re-notify;
  enforcement was always per-call by design).
- Reviewer prompt catalogs suppressed via `catalogs=False` (f19); other
  agents keep catalogs — the suppression is opt-in per caller.
