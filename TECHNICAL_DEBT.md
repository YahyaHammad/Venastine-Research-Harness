# Technical Debt

High-level debt themes surfaced by the §19–§20 review (2026-08-04), with
what the fix pass did about each and what remains by design. Finding ids
refer the review report `.qwen/reviews/2026-08-04-223903-local.md`.

**Updated 2026-08-05** after a follow-up pass over the review's own
output. Three themes moved; the moves are marked inline rather than
edited away, because a debt file that only ever shows its current state
loses the thing it is for — which classes of problem this codebase keeps
producing.

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
- **Follow-up (2026-08-05):** the re-synthesis path was still doing the
  opposite of the policy this theme unified. It restored the claims and
  re-raised, so a provider error on that one call failed a run whose ten
  passes all succeeded — code and doc back in disagreement one layer below
  where f1 fixed them. Contained now: the run keeps `status='complete'`
  with the pre-review report, `log_outcomes(committed=False)` so nothing
  reads as applied beside claims that were not, and a test that composes
  with the real pipeline (the stage-level test cannot see a status).

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
- **Follow-up (2026-08-05):** one class of vacuity a revert check cannot
  catch is a test that SKIPS. `test_docs_consistency.py`'s narrowing guard
  would skip on every run if its markexpr comparison were wrong, and the
  file would sit there looking like coverage — so the guard has a direct
  test of its own rather than a revert-and-see-red. Worth generalising:
  where a test can decline to run, prove it runs.

## 7. Documentation drift

The index/ARCHITECTURE entries lagged the code (f7 namespace-package claim
vs a real `__init__.py`; f8 missing **(BUILT)** marker).

- **Fixed:** `skills/__init__.py` deleted (namespace package like
  agents/tools/tui, per owner decision); index marker added.
- **RETIRED (2026-08-05)** for the part that actually drifted.
  `tests/test_docs_consistency.py` asserts the three docs quote the same
  test count and that it matches `len(session.items)` on an unfiltered run
  (skipped under `-k`, a non-default `-m`, or a file/nodeid arg). It found
  the drift the follow-up's own commits had just created, before anyone
  looked. Deliberately narrow: BUILT markers and tree entries are still by
  hand, because a doc-linting framework nobody asked for would rot faster
  than the counts it was policing. If those two start drifting the same
  way, extend this file's check rather than adding a new practice to
  remember.

## 8. `test_tui.py::_settle` budgets pumps, not time (open)

Found during the 2026-08-05 follow-up, **not fixed**, and the attempted
fix is the useful part of the record.

`_settle(pilot, predicate, tries=120)` pumps Textual's event loop a fixed
number of times waiting for a worker thread to hand a message back. A
count is not a wait: 120 bare `pilot.pause()` calls elapse in microseconds
on an idle machine and rather longer on a loaded one, so
`test_ac2_approving_a_permission_prompt_resumes_the_same_generator` failed
roughly one full-suite run in five while passing every time in isolation.
A false failure is worse than a slow test.

The obvious fix -- a wall-clock deadline with `await pilot.pause(0.01)` --
made things **worse**: it turned a 1-in-5 flake in one test into a
deterministic failure of
`test_quitting_during_an_attended_research_prompt_releases_the_worker`,
whose worker then never unblocked at all. `pilot.pause(delay)` evidently
does not drive the message pump the way a bare `pilot.pause()` does, so
the two are not interchangeable and the delay changes what the helper is
actually waiting for. Reverted.

Left open deliberately rather than half-fixed. Whoever takes it should
start from what `pilot.pause(delay)` does to the message queue, not from
the timeout value. Six consecutive full-suite runs were clean afterwards,
so it is rare -- rare enough that a fix applied without understanding it
will look like it worked.

## 9. `MAX_TOKEN_BUDGET` counts the prompt once per step (open)

Raised while building §21a, **deliberately not fixed there**.

`core/loop.py` accumulates `input_tokens + output_tokens` after every call in a
turn, and the prompt is resent in full on every step — so the counter grows
quadratically in a tool-using turn. At ~2k of tool result per step, a 20k-token
thread gets about 9 steps before the budget stops it, a 50k thread about 2, and a
100k thread exactly one response with no tool calls at all.

This is **correct as a billing meter**: the provider really does charge for the
resent prompt (nothing here sets `cache_control`, so no prompt caching offsets it).
It is wrong as anything that reads like "how large may a thread get", and it is easy
to mistake for one because `token_budget_exceeded` is what a user actually sees when
a long thread stops working.

§21a worked around it rather than fixing it — M1 moved the compaction trigger onto
a working-set target and raised the budget to 250k so the two stop competing, and
`config_loader.effective_compaction()` now warns when a configured trigger leaves
too little headroom for a multi-step turn.

The real fix is to separate the two instruments: keep the billing total for the
spend cap, and add a "new tokens this turn" figure for anything reasoning about
size. That changes an existing, tested stop condition, so it belongs in its own
change with its own revert checks — not smuggled into a memory feature, which is
why it was left here. Whoever takes it should decide first whether
`token_budget_exceeded` is meant to mean "this turn cost too much" or "this turn got
too big", because the current code answers the first while every caller reads it as
the second.

## Accepted risks noted in the review, deliberately not "fixed"

- `07_review.json` absent on zero-finding reviewed runs — presence-implies-
  reviewed is the documented signal; trace.md still carries the count line.
- Advisory-only K2 notes after `/agent` switch (f21 fixed to re-notify;
  enforcement was always per-call by design).
- ~~Reviewer prompt catalogs suppressed via `catalogs=False` (f19); other
  agents keep catalogs — the suppression is opt-in per caller.~~
  **RESOLVED at the producer (2026-08-05).** The catalogs' prose
  *instructs* the model to call a tool ("call the load_skill tool", "can
  be spawned with the spawn_subagent tool") and neither knew the
  `ToolContext`, so every restricted agent was being told to call a tool
  it does not have — the reviewer was just the first one anyone noticed.
  `with_catalogs` now takes the context and suppresses each catalog whose
  tool is unreachable; the per-caller flag is gone. This was the
  fix-at-the-consumer shape CLAUDE.md names by name, and it is worth
  reading as the recurring lesson rather than a one-off: a per-case opt-out
  added to a shared assembly point usually means the condition belongs
  inside it.
