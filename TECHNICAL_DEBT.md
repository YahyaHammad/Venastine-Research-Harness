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
AGENTS.md said reviewer failures land on `status='failed'`. Both halves of
the changeset stated opposite policies (f1).

- **Fixed:** transient reviewer failures (provider error, unrecoverable
  JSON) are contained like the missing-agent branch — traced skip, run
  completes; AGENTS.md rewritten to one policy. Deferred commit (f4) keeps
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
- **THE TRIGGER FIRED (2026-08-17), and the entry was right to name it in
  advance.** Both categories drifted, exactly as written: audit #121 found
  eleven of ARCHITECTURE's fifty-three per-file counts wrong and eight test
  files with no entry at all, and #129 found six ROADMAP_v2 index entries
  with no marker — §13–§18, every one built, because the convention arrived
  at §19 and was never backfilled — plus §23 stating its two tools were
  unbuilt while 98 tests exercised them.

  By the time the fix ran, two more batches had moved the numbers again:
  **18** counts wrong, `test_math_tools.py` claiming 14 against 123. That
  is the entry's own point, measured — a hand-maintained number does not
  drift once and stop.

  Extended rather than replaced, as instructed. Four assertions now live in
  `tests/test_docs_consistency.py`: per-file counts (strict in both
  directions, plus an entry naming a file that no longer exists), `(BUILT)`
  markers, README's approval table against `ToolPermissions`/`ToolApprovals`,
  and ARCHITECTURE's registry counts. The narrowness rule is unchanged and
  is now in that file's docstring: **a claim earns a check by having already
  drifted, and only if the truth is something the suite can compute.**

  What this leaves open is the class the entry could not have predicted:
  #16, #17 and #147 are three ways the *decision record's index* disagrees
  with itself (four decisions living only in `DEVLOG.md`, two prefixes
  carrying two numbering schemes each, and a `C` family cited in six
  production files and defined nowhere). Those are ids rather than counts,
  and #147 argues for one mechanical check over all three. Not done here —
  it needs a namespace decision first.

## 8. `test_tui.py::_settle` budgets pumps, not time (**closed 2026-08-07**)

Found during the 2026-08-05 follow-up and left open; fixed on 2026-08-07,
after measuring. The 2026-08-05 entry is kept below in full, because two of
its three claims were wrong and *how* they were wrong is the useful part.

### What was actually wrong: two independent defects

**Defect 1 — a pump count is not a wait, and swings 46×.** `pilot.pause()`
with no argument ends on `textual._wait.wait_for_idle(0)`, whose exit
condition is a **process-wide CPU heuristic** (`process_time()` deltas),
floored at one 20 ms tick and capped at 1000 ms. Measured in the fix
container:

| Condition | per `pause()` | `tries=120` was worth |
|---|---|---|
| nothing burning CPU in-process | 21 ms | **2.5 s** |
| one busy sibling thread | 1021 ms | **122 s** |

So the helper's patience was set by something unrelated to what it waited
for. Instrumenting every `_settle` call in a clean full run gave the real
requirement: **44 calls, max 0.304 s, p90 0.132 s, median 45 ms**. The fix
is a wall-clock deadline defaulting to 10 s — ~33× the observed maximum.
Suite runtime is unchanged (35 s → 36.5 s, all of it the new tests),
because a satisfied predicate returns immediately either way; only
failures pay a deadline.

**Defect 2 — a predicate can go true before the state a test depends on.**
`isinstance(app.screen, PermissionScreen)` becomes true while the worker is
still inside `call_from_thread(push_screen, …)`, blocked on the loop until
the mount completes — it has **not** reached `channel.get()`. A test that
then calls `t.join()` from the event-loop thread deadlocks: worker waits
for loop, loop waits for worker. The old helper hid this by **accident**,
because `wait_for_idle`'s sleep happens to let the mount finish. Fixed by
quiescing (`await pilot.pause()`) once the predicate holds.

The two defects pull in opposite directions, which is why the first attempt
read as a mystery: raising patience alone leaves the deadlock, and making
polling more responsive exposes it.

### What the 2026-08-05 entry got wrong

- **"120 bare `pilot.pause()` calls elapse in microseconds"** — false.
  Measured 2.535 s. The 20 ms `SLEEP_GRANULARITY` floor makes microseconds
  impossible. The *variance* was real; the magnitude was not.
- **"`pilot.pause(delay)` does not drive the message pump the way a bare
  `pilot.pause()` does"** — false, and it sent the next reader somewhere
  there is nothing to find. `Pilot.pause` (textual 1.0.0, `pilot.py:520`)
  runs the same `_wait_for_screen()` barrier *before* and the same
  `_on_timer_update()` *after* in both branches; only the exit condition
  differs (`wait_for_idle(0)` versus `asyncio.sleep(delay)`).
- **"start from what `pilot.pause(delay)` does to the message queue"** — a
  dead end, for the reason above. The thing to start from was what
  *`wait_for_idle`* does, and what the predicate is true of.

Its **conclusion** was right, though: the two are genuinely not
interchangeable. Reconstructed and measured, three runs each, under 4× CPU
load, on `tests/test_tui.py`:

| Variant | Result |
|---|---|
| baseline (`tries=120`) | 43 passed ×3 |
| deadline 10 s + bare `pause()` | 43 passed ×3 |
| deadline 10 s + `pause(0.01)` | **1 failed ×3** — the quitting test |
| deadline 2 s + `pause(0.01)` (the reverted attempt) | **1 failed ×3** — same |
| deadline 10 s + `pause(0.01)` + quiesce | 43 passed ×3 |
| deadline 10 s + bare `pause()` + quiesce | 43 passed ×3 |

The deadline *length* was never the issue — 10 s fails identically to 2 s.

### What shipped

`settle` and `pump` in `tests/conftest.py`, replacing **five** copies
(budgets 40/60/120/120/200 and two incompatible orderings) plus one
open-coded inline loop. `pump` is deliberately separate and stays
count-based: it backs *negative* assertions, where there is no predicate
and a deadline would make each site pay its full timeout — measured at +20 s
of suite runtime when mutated to one.

Two honesty notes, both discovered by mutation testing *after* the fix was
written:

1. **The quiesce is not load-bearing as shipped.** Deleting it leaves
   `tests/test_tui.py` green 43/43 under load, because the bare `pause()`
   used for polling already masks the need. It is the second of two
   independent defences, and `tests/test_pilot_wait.py` pins it because no
   TUI test can. The first version of this fix claimed a red test that does
   not exist.
2. **The flake itself was never reproduced.** Zero failures in ~10
   full-suite runs in the fix container, so "it stopped failing" was not
   available as evidence and is not being claimed. The patience half rests
   on the measurement (2.5 s–122 s budget against a 0.304 s requirement)
   and on `test_pilot_wait.py`, which pins the deadline semantics directly.
   This is the 2026-08-05 entry's own warning — "rare enough that a fix
   applied without understanding it will look like it worked" — taken
   seriously in the one direction it can be.

Adjacent, fixed with it: `_blocking_modal` parks on
`channel.get(timeout=ATTENDED_APPROVAL_TIMEOUT_S)` = 600 s, so a dismissal
path that drops its value **stalled** the suite for ten minutes instead of
failing. An autouse fixture shrinks it under test.

### The original entry, 2026-08-05

> Found during the 2026-08-05 follow-up, **not fixed**, and the attempted
> fix is the useful part of the record.
>
> `_settle(pilot, predicate, tries=120)` pumps Textual's event loop a fixed
> number of times waiting for a worker thread to hand a message back. A
> count is not a wait: 120 bare `pilot.pause()` calls elapse in microseconds
> on an idle machine and rather longer on a loaded one, so
> `test_ac2_approving_a_permission_prompt_resumes_the_same_generator` failed
> roughly one full-suite run in five while passing every time in isolation.
> A false failure is worse than a slow test.
>
> The obvious fix -- a wall-clock deadline with `await pilot.pause(0.01)` --
> made things **worse**: it turned a 1-in-5 flake in one test into a
> deterministic failure of
> `test_quitting_during_an_attended_research_prompt_releases_the_worker`,
> whose worker then never unblocked at all. `pilot.pause(delay)` evidently
> does not drive the message pump the way a bare `pilot.pause()` does, so
> the two are not interchangeable and the delay changes what the helper is
> actually waiting for. Reverted.
>
> Left open deliberately rather than half-fixed. Whoever takes it should
> start from what `pilot.pause(delay)` does to the message queue, not from
> the timeout value. Six consecutive full-suite runs were clean afterwards,
> so it is rare -- rare enough that a fix applied without understanding it
> will look like it worked.

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

## 10. `config_loader.initialize(".")` in tests is CWD-dependent (open, latent)

Found by the §21a review sweep, **not fixed**, and the reason is scope rather than
difficulty.

Twelve test sites across five files call `config_loader.initialize(".")` to load the
real harness-tier agents and skills — which is the right instinct: it keeps those
suites honest about the `.md` files existing and parsing, rather than stubbing
`manager.get`. But `"."` is the working directory, so the call also discovers any
project-tier `.venastine/` sitting there. This repository has none, and a project
tier only loads once trust has been granted (D17), so the risk is latent rather than
live.

It would stop being latent the moment someone adds a `.venastine/settings.json` with
a `compaction` block and trusts the repo for a manual test — at which point several
suites would start reading configuration nobody wrote for them, and the failure would
present as unrelated tests changing behaviour together.

Not fixed here because nine of the twelve sites belong to §19 and §20, and rewriting
another section's fixtures inside a §21a review is the kind of scope creep that makes
a focused pass unreviewable. The fix is a shared fixture that initializes against a
tmp_path project with the harness root pointed at the real one —
`tests/test_config_loader.py`'s `_redirect_roots` is most of it already.

## 11. Two provider facts about sampling parameters are unverified (open, needs network)

Found while redesigning ROADMAP §10's ensemble mode. Both are recorded rather
than resolved because neither can be settled offline, and neither blocks
anything: §10's revisit removed the harness's only caller of `temperature`.

**(a) `ENSEMBLE_TEMPERATURE = 1.0` may have been a no-op on the very providers
§10 said it worked on.** `_sampling_kwargs` sends `temperature` only when
explicitly given, so omitting it means "provider default" — and 1.0 is the
documented default for OpenAI Chat Completions and Google's
`GenerateContentConfig`. If that is right, the "raised temperature" that was
§10's entire diversity mechanism sent the value the provider would have used
anyway, and the diversity a run actually got came from the provider's own
nondeterminism rather than from anything this repo configured.

The constant is deleted, so nothing depends on the answer. It is recorded
because the **shape** generalises and will recur: *a config value that reads as
a delta but is sent as an absolute is not checkable by any test that mocks the
provider.* Nothing in a 1400-test offline suite could have caught it.

**(b) `config.MODELS_REJECTING_SAMPLING_PARAMS` lists only Anthropic names.**
OpenAI's own reasoning models restrict `temperature` the same way, and
AGENTS.md's example command is `--provider OPENAI --model gpt-5.1`. So §16's
guard — added specifically to stop a sampling parameter reaching a model that
rejects it — likely never fired for the OpenAI model the docs suggest.

Now moot for ensemble mode, and the set is still correct for what it claims
(it is documented as deliberately incomplete, and the failure mode is a loud
400 rather than a wrong answer). But `_sampling_kwargs` is a live backstop for
the next caller that wants sampling variation, and it will consult this set.

**Suggested trigger:** the next time anything passes a `temperature`, or the
next time a live API key is available for either provider, check both and
either extend the set or record that it was checked and is right.

---

## 12. `effective_compaction()` reports no provenance (open, deferred)

D27's third implementation note, unbuilt, and the only one of its three that
was dropped without being recorded as a decision:

> **Surface the effective values.** A `/config` or startup line showing which
> values are in force **and where each came from** turns "compaction feels too
> aggressive" from a mystery into a one-line answer. Cheap now, and the
> alternative is debugging a number three files away from where it was set.

D27's *decision* holds — every value the record names is overridable through
`settings.json`, and `_COMPACTION_DEFAULTS` covers all five plus two more. What
is missing is the ability to say **which tier** a value came from. Until fix
batch 15 the only thing that still claimed otherwise was the function's own
docstring, which opened "The compaction values actually in force, **and where
they came from**" and returned a flat dict (audit #91, item 2).

**Why it is more than adding a field.** Provenance is not omitted, it is
destroyed by the merge:

```python
values = {key: getattr(config, attr) for key, attr in _COMPACTION_DEFAULTS.items()}
values.update(get_settings().get("compaction") or {})   # user and project, already flattened
values.update({k: v for k, v in (overrides or {}).items() if v is not None})
```

Each `update` overwrites without recording who won, and the user/project tiers
were already merged into one dict upstream by `_load_merged_settings`.

**Two decisions belong to whoever builds it, and they are why this is deferred
rather than done.**

1. **The return shape.** Twelve call sites subscript the flat dict —
   `core/compaction.py` at four places, `tui/app.py`, `config_loader` itself,
   and six in tests — so `{key: (value, tier)}` breaks every one of them. A
   parallel `sources` dict, or a second function, does not.
2. **Where a user would read it.** There is no `/config` command; §21 shipped
   `/memories`, `/forget`, `/summary` and `/ref`. The only thing on this path
   today is the headroom advisory, deliberately gated to fire once at startup
   because `effective_compaction` sits on `should_compact()`'s path and runs
   once per step of every turn.

Provenance with nothing to display it would be the same defect in a new place —
a value nothing consumes, promised by a docstring — so the two halves want
building together. D27's argument is entirely about the display.

Recorded here rather than only in a batch log: it was already noted under "Not
fixed" in two batches' DEVLOG entries, which is where someone looks when they
already know it exists.

---

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
  fix-at-the-consumer shape AGENTS.md names by name, and it is worth
  reading as the recurring lesson rather than a one-off: a per-case opt-out
  added to a shared assembly point usually means the condition belongs
  inside it.
