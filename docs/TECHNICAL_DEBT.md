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
- **Fixed, batch 145 (S14-01, S14-03, S14-04):** the flag closed the
  SEQUENTIAL re-arm, which is what r1-1 was, and left two things beside it.
  The re-arm that is CONCURRENT with the release had no guard at all —
  `_release_permission_channel` reads-and-clears, so a release landing
  while `_permission_channel` is still `None` puts nothing anywhere and the
  ask publishing a moment later parks on a queue with no writer; the shared
  `_blocking_modal` now re-tests the flag after publishing, which puts the
  check in the one place all seven asks reach. And the release itself lived
  only in `exit()`, which is every DELIBERATE route out and not the
  involuntary one, so a crash left a worker parked for the full 600 s on a
  non-daemon thread holding `App.run()`; `on_unmount` releases too, and the
  ordering that makes it work — textual dispatches `Unmount` before it
  waits for the thread workers — is pinned rather than assumed. Also the
  marshal: `call_from_thread` sat outside the `try` whose `finally` clears
  the field and resumes the turn clock, so a raise left both.
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
- **Second instance, batch 145, and this one was found rather than
  foreseen.** A PARAMETRISATION can empty, which skips without anything in
  the test looking conditional.
  `test_logging_setup.py::test_the_three_shapes_are_what_discriminates` is
  batch 144's guard on `REDACTION_CASES`, parametrised over the rows whose
  labels are in `SHAPE_CASE_LABELS` — so a mutation that dropped those rows
  reported `1 skipped` and the parity tests it protects went on passing,
  which is precisely the vacuity it exists to prevent. Caught by a mutation
  control, not by reading. `test_the_three_shapes_are_all_still_in_the_list`
  is the proof-that-it-runs beside it, and it covers a renamed label as well
  as a dropped row. **Worth a sweep**: every `@pytest.mark.parametrize` whose
  argument list is a COMPREHENSION over another list can do this, and the
  suite has more than one.

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

## 9. `MAX_TOKEN_BUDGET` counts the prompt once per step (closed, batch 27)

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

**RESOLVED (batch 27, #4).** The owner answered the question: it means cost, and it
should usually mean nothing at all — the counter is now uncapped by default on every
path, the three config constants died (both 1M envelopes existed only because the
250k chat value was misread as bounding pass context), and the only cap is the
user's settings.json `max_token_budget`, resolved through a sentinel default so an
explicit `None` stays genuinely uncapped. The size side gained real instruments:
`ModelResponse.turn_new_tokens` (outputs plus positive input deltas, inherited first
prompt excluded, mid-turn compaction shrinks clamped) and `.turn_billed_tokens`,
plus `memory.billed_tokens` for thread-since-resume display. The entry's stale
worked numbers were from the pre-§21a budget: measured at 100k they read
20k->5 / 50k->2 / 100k->1; at §21a's 250k, 20k->9 / 50k->5 / 100k->3 — the model in
this entry was right at both budgets; two of its three numbers were simply copied
from the older one. Consumers: TUI usage line, CLI early-stop figures, orchestrator
truncation messages.

## 10. `config_loader.initialize(".")` in tests is CWD-dependent (closed, batch 28)

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

**RESOLVED (batch 28, #5).** The shared fixture is `real_harness_tier` in root
conftest: HOME/USERPROFILE redirected to tmp — which empties BOTH the user tier
and the trust store, since both resolve through `expanduser("~")` at call time —
HARNESS_ROOT deliberately left real so the shipped `.md` files still parse, and a
fresh tmp project returned for each site's own `initialize(str(project))`, kept
in place rather than folded into the fixture because several sites initialize at
a specific moment relative to writing files. All literal `"."` sites converted.
One correction to this entry's own numbers: it said twelve sites across five
files; an in-issue re-measure found sixteen across seven; at fix time the count
was **nineteen across eight** — the pattern kept spreading after being recorded,
which is the strongest argument for having fixed it rather than re-counting
again. The pin lives in test_load_skill.py
(`test_the_shared_fixture_loads_real_and_isolates_everything_else`) because
test_config_loader.py's own `_redirect_roots` is autouse and points HARNESS_ROOT
at an empty dir — exactly the state the pin exists to forbid.

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

**Rechecked in the review marathon (batch 138), and both halves still hold.**
(a) Nothing in the harness passes a `temperature`: `git grep "temperature="`
over tracked production code returns only `temperature=None`,
`temperature=temperature` forwarding, and the WARNING's own format string at
`core/client.py:608`. `_sampling_kwargs` is the backstop AGENTS.md says it is
and the threading through `core/loop.py` is intact. (b)
`config.yaml:118-124`'s `models_rejecting_sampling_params` is still six
Anthropic names and nothing else -- `claude-fable-5`, `claude-mythos-5`,
`claude-opus-4-7`, `claude-opus-4-8`, `claude-opus-5`, `claude-sonnet-5` -- so
§16's guard has most likely still never fired for the OpenAI model AGENTS.md's
own example command names.

Nothing to add beyond the re-verification, which is not the default outcome:
this file's rule is that an entry is evidence rather than instruction, and
batches 126 and 127 each found one wrong about its own defect. This one is
accurate. Neither half can be settled offline, so it stays open for the reason
it was filed.

---

## 12. `effective_compaction()` reports no provenance (closed, batch 111)

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

**RE-SCOPED (batch 107), not closed -- and the second of its two blockers is gone.** This entry
says "There is no `/config` command". There is: `/config` shipped in batch 84, which is item 20
above. More than that, `config_edit.explain` already speaks provenance fluently -- it names the
environment variable when one is set, the remembered `/model` pair, and a `--provider/--model`
pin, each with a sentence about which one is in force this session.

So what remains is narrower and has a home. `ConfigRow.outranked_by` returns a **static
possibility**: "A settings.json compaction key outranks this one, so a project or user
settings.json **can** win over whatever is written here." The merge in `effective_compaction`
knows whether one DOES, which tier it came from and what the value is -- and destroys all three,
one `update()` at a time. The work is a parallel `sources` dict (which, as this entry already
argues, breaks none of the twelve subscripting call sites) and one sentence in `explain` that
says "is" where it now says "can".

The first blocker stands unchanged and so does the display argument. What changed is that the
display exists, so the two halves no longer have to be built together.

**RESOLVED (batch 111), with the first blocker honoured rather than worked around.**
`effective_compaction`'s return is still flat, so none of the twelve subscripting call sites
moved. Provenance is beside it:

- **`_load_merged_settings` returns `(values, sources)`**, where `sources` maps a dotted setting
  to the tier that last wrote it -- `{"compaction.strength": "project"}`. Built in the same pass
  as the merge, because this entry's own sentence about the defect is the reason it has to be:
  provenance "is not omitted, it is destroyed by the merge", so the only moment the two tiers are
  still distinguishable is while they are being combined. A second function re-reading both files
  afterwards would be two traversals that must agree, which is how the first one drifts.
- **`initialize` stores it beside the values** as `_state["settings_sources"]`, read through a
  new `settings_sources()` accessor that mirrors `get_settings()` and is empty pre-init for the
  same reason.
- **`_compaction_merge(overrides)` walks the ladder once and returns both**; `effective_compaction`
  keeps every validation and warning it has and returns the values, and `compaction_sources()`
  returns `{key: tier}` over the seven keys. One ladder rather than two, because a provenance
  report that disagrees with the value it describes is worse than none -- it is a confident wrong
  answer to "why is this number what it is".

**The owner chose to name the tier** -- `config.yaml` / `user` / `project` / `override` -- rather
than collapsing both files into one "settings.json". A project `settings.json` arrives with a
directory you cloned, which is D17's whole premise, so *which* file is the useful half. Recorded
per KEY, not per file: a value the user set and the project did not stays `user`, and a test
asserts exactly that, because a per-file answer passes every single-file test and gets this one
wrong.

**`explain()` says "is" where it said "can"**, and the sentence covers every row with a
`settings_key` rather than only the seven compaction ones -- `settings_overrides()` already maps
them all onto the same lookup, so restricting it would have meant writing a second, narrower one
in order to stay quiet about `subagent_review`. The old sentence is kept for the case where
nothing speaks, where it is the whole truth.

**One trap found while writing it, and pinned.** The obvious value to quote is `row.in_session`,
and it is wrong: for a compaction key that is `config.COMPACTION_*`, bound at import from
`config.yaml`, because the settings merge happens inside `effective_compaction` and never reaches
the `config` module. So these rows have THREE values, not batch 85's two, and the one named "the
session's" is the one that is not in force. The line reads the settings tier directly, and
`test_it_quotes_the_settings_value_and_not_the_imported_one` asserts the difference -- including
an assertion that the difference exists, so the test cannot go vacuous if `config.yaml` ever
ships the number it uses.

Not built, and not needed by the above: a `sources` entry for any tier below `settings.json`
other than the file itself. There is none.

---

## 13. `spawn_subagent` has no `available_check` where `load_skill` does (closed, batch 110)

`load_skill` declares `available_check=load_skill.has_skills`, and its
docstring gives the reason in full: with no skills discovered "the system
prompt carries no catalog, so this tool's only possible answer is 'Unknown
skill'. Advertising it anyway is the same defect shape as the fetch_url one
§15 exists to fix: a schema the model can see, choose, and never get value
from." `pin`, `unpin` and `remember` each carry one for the same kind of
reason.

`spawn_subagent` does not, and its schema tells the model to supply an
"Agent name exactly as listed in the Available agents catalog." Until batch
51 that catalog was empty on every default install, so for the whole life of
the project an attended run advertised a tool whose only possible answer was
`Unknown agent`. Nothing failed; the model could choose it, and the turn
spent a step learning it could not.

**Why it is not fixed here.** Batch 51 shipped two spawnable agents, so the
default install now has a catalog and the live instance of the problem is
gone. What remains is the asymmetry: a user whose agents are all
non-spawnable — or who deletes ours — is back in the original state, and the
declaration that would say so is one keyword argument. Adding it changes what
a run advertises, which is a behaviour change rather than a roster addition,
and it belongs to whoever is next in `tools/registry.py` rather than to a
batch about agent files.

**The check itself is already written**, twice: `agent_catalog_text()` returns
`""` in exactly this condition, and `with_catalogs` already tests it before
appending. An `available_check` reading the same function — the way
`has_skills` reads `skill_catalog_text` rather than a parallel check — would
keep "advertised" and "catalogued" from drifting apart, which is the property
that comment is really about.



**RESOLVED (batch 110).** `subagent_tool.has_agents` is declared, and it reads
`system_prompts.agent_catalog_text()` -- the same function prompt assembly uses, for the reason
`has_skills` gives about `skill_catalog_text`: a parallel check is a second answer to "is there a
catalog", free to drift from the first, and drift is what this entry is about.

Two things the entry did not anticipate, both worth keeping.

**It is roster-wide, not per-run, and the docstring says so.** `available_check` takes no context,
so it can only answer "is there any spawnable agent at all". A run whose `spawn_targets` empties
the list is still `refusal_reason`'s business and `agent_catalog_text(targets)`'s -- the same rule
read from the two sides it has. A reader who assumed the new check covered both would believe a
per-run restriction was being enforced at advertisement time.

**Four tests were resting on the old answer**, three in `test_agents.py` and one in
`test_research_authorization.py`. None of them is about the roster: each asserts something about
the headless filter or about grant policy, against a registry whose `spawn_subagent` was
advertised for a reason none of them named. They state the roster now. What caught the change was
`test_a_run_that_can_ask_IS_offered_spawn_subagent`, a control written so that the negative
assertions beside it "cannot pass against a registry that stopped advertising spawn_subagent
anywhere" -- which is exactly what this batch did to them, and the best argument in the tree for
writing that kind of control.

## 14. The indent-verbatim rule does not cover fences (open, deferred batch 58)

Batch 58 made a line indented four or more columns render **verbatim**: no list marker, no
table row, no inline marks, so a four-space code sample in an answer is drawn as it was
written. `tui/markdown.verbatim` is the rule and `_scan`, `_is_row` and `list_item` are the
three places that ask it. It applies to the **line-level** constructs only. A fence is exempt,
and deliberately.

Fence splitting is not a line rule. `split_blocks` splits the whole text on ``` before anything
looks at a line — which is what keeps a table inside a fence as source code — and §26 and §38
both pin that ordering: the open-fence hold in `safe_commit_limit` counts fences in the text
COMMITTED SO FAR rather than in the line being classified. "Indented means verbatim" cannot
reach it without moving the fence split into the line scan.

The visible consequence: an indented ``` inside an indented code sample opens a real code block
instead of drawing three backticks. That is also the RIGHT answer whenever the indentation is a
list rather than a code sample — a fenced block inside a bullet is common and should keep its
highlighting — so the two cases want opposite behaviour, and telling them apart needs the list
context this batch chose not to track (see the `verbatim` docstring for why: it would put state
across the commit boundary).

**Revisit if** someone reports a code sample that swallowed the rest of an answer. The fix is a
list-context state machine in `split_blocks`, which is the same machinery the indent rule
declined to add.

**Rechecked in the review marathon (batch 138).** Still true as written, read
rather than re-driven: `tui/markdown.py:157` (`verbatim`), `:278`
(`_is_fence_line`) and `:297` (`split_blocks`) are unchanged, and the fence
split still runs before any line rule, per the §26 and §38 pins. No new
measurement, and the entry's own revisit trigger -- a code sample swallowing
the rest of an answer -- was not observed on any path the marathon exercised.
So this stays owner-deferred rather than becoming a fixing agent's item.

## 15. Nothing re-renders on resize, so a pre-wrapped construct freezes (closed with item 23, batch 112)

`Transcript` pre-wraps three things rather than letting Rich soft-wrap them, because each needs
a prefix on every rendered row: the diff gutter (§41), the thinking bar (§38) and, since batch
58, a list item's hanging indent. There is no `on_resize` handler, so those rows keep the width
they were drawn at while paragraphs and tables — which Rich wraps — reflow. Widen the terminal
mid-session and the transcript is wrapped at two widths.

Pre-dates batch 58 by two constructs; lists made it a third rather than a new problem.

The fix is small and its risk is not: `on_resize` → `rerender()` re-lays the whole transcript,
loses the scroll position, and fires per event while a window edge is dragged, so it needs
debouncing and a scroll-anchor. **Revisit** if resizing mid-session becomes a normal thing to
do, or alongside any work that touches `rerender()` anyway.

**FOLDED INTO ITEM 23 (batch 107).** These are one defect seen from two angles, and two entries
for one defect invite two half-fixes. This one (batch 58) noticed that three PRE-WRAPPED
constructs freeze on resize; item 23 (batch 90) measured that **every row already drawn** keeps
the width it was drawn at, pre-wrapped or not, because RichLog renders at write time and stores
Strips. 23 is the general statement and carries the measurement, the `rerender()` hazard and the
prescription, so it is the one to read. This entry is kept for the three constructs it names,
which are where the symptom is most visible.

**CLOSED WITH ITEM 23 (batch 112).** A resize now re-lays the whole
transcript, so the three pre-wrapped constructs this entry names re-wrap with
everything else; all three are cases in the resize row-equality class
precisely because they are where the symptom was most visible. The two risks
this entry raised are answered where 23 records them: the redraw is debounced
on `REFLOW_DEBOUNCE_S` rather than run per event, and the scroll position
keeps its fraction rather than being lost.

## 16. A URL in a tool line is not clickable (closed, batch 65)

Batch 58 armed bare `http(s)://` URLs in an assistant body for ctrl+click and left tool lines
out: `write_role("tool", …)` reached a plain `Text` and never went through
`markdown.inline_spans`, so `▸ fetch_url https://…` — the place a URL most obviously appears —
was inert while the same URL in prose was not. The entry said **revisit if the asymmetry is
reported**, and it was.

The prediction that the mechanism was already there (`_append_spans` with `block=False`) was
half right, and the wrong half is the interesting one. **Routing a tool line through the prose
grammar eats characters**: measured, `▸ shell  git log --format=%h  # `date`` loses its
backticks to a code span and `▸ mcp__x__y  name~=*test*  ~~old~~` renders as `name~=test  old`.
A digest that no longer shows what ran is worse than an inert URL, so batch 65 added
`markdown.link_spans` — URLs and nothing else — and `LINKED_ROLES` to say which lines get it
(`tool`, `pipeline_tool`, `tool_error`).

Two things the entry did not anticipate. `param_digest` caps a value at 60 characters, which is
shorter than most real URLs, so the untruncated target rides beside the line and a span
resolves against it — under the rule that **the visible text tells you the origin, and the
origin is where it goes**. And the work turned up a live defect in batch 58 itself: a URL
carrying userinfo (`https://accounts.google.com@phish.example/x`) was armed and opened
`phish.example`. See DEVLOG batch 65.

**Reopened and closed again in batch 96, one layer out.** The resolution above was measured
working — the click opens the whole URL — and the report was still true: ctrl+click went to a
*truncated* address. The cause is not in the harness at all. `https://host/a/b…` minus its
ellipsis is itself a valid URL, so the TERMINAL's own link detector reads a shorter address out
of the drawn text, and it never sees `_links`. Nothing the harness stores can fix a decision
another program makes from the pixels. So batch 96 stopped producing the mismatch: a value
carrying a URL is capped at 200 rather than 60, the visible text is the target again for an
ordinary URL, and the elision above still governs everything past the wider cap. The lesson is
the one this file keeps recording — *the fix belongs where the divergence is produced* — with a
twist worth keeping: here the producer was ours and the consumer was not.

## 17. Block quotes render as written (open, deferred batch 58)

`> quoted text` gets no treatment: the marker is drawn and a wrapped row returns to column 0,
which is the same defect batch 58 fixed for list items.

Deferred by owner decision rather than by cost. The reading that works best is a bar down the
left — and the transcript already draws one of those for a **thinking span** (`│ `, bracketed
by `╭`/`╰`). Two bar-prefixed blocks a few rows apart, distinguished only by glyph and colour,
risks a quoted source reading as the model's own reasoning, which is a worse failure than a
quote that wraps flat. The conservative alternative — keep the `>` and hang the indent under it
— was judged too weak a signal to be worth the machinery it needs (a fifth entry in the commit
cap, a palette role, and a hold).

**Revisit if** models in use start quoting heavily, or if the thinking span's own furniture
changes enough that the collision goes away.

**Rechecked in the review marathon (batch 138).** Read and agreed, with one
thing to add: the collision rationale still holds on the current tree. The
thinking span's furniture is unchanged (`THINKING_BAR` at
`tui/widgets.py:68`), so the reason a quote bar would read as the model's own
reasoning is still live, and the deliberate absence still sits alongside
horizontal rules, heading levels and setext headings per `ARCHITECTURE.md`
4.16. Owner-deferred by decision, not by cost, and that is still the right
label.

## Accepted risks noted in the review, deliberately not "fixed"

- **The container `sudo` refusal's REASON depends on a user-settable tag**
  (recorded batch 117). `security/sandbox.py` refuses root in the container
  because "the pinned image runs as uid 0 and ships no `sudo` at all" --
  measured, and true of the image it was measured against. But
  `SANDBOX_DOCKER_IMAGE` is configurable (`config_schema.py` maps
  `AGENT_SANDBOX_IMAGE`), and `sandbox.py` says in its own words that "a tag
  is not an image", so an operator pointing it at a non-root image with
  `sudo` gets a refusal whose stated reason is false. It fails CLOSED -- the
  call is refused, nothing escalates -- so this is a wrong sentence rather
  than a hole, and it is recorded instead of fixed because re-probing the
  image for a uid would buy a correct sentence at the price of a container
  round trip on a path that is refusing anyway.
- **`defusedxml`'s `forbid_dtd` is off, so an external DTD DECLARATION is
  accepted** (recorded batch 117). `<!DOCTYPE feed SYSTEM "file:///etc/passwd">`
  parses under `defusedxml.ElementTree` -- and under stdlib
  `ElementTree` -- and is FETCHED by neither on CPython, because expat does
  not resolve external DTDs by default. So nothing leaks today. It is
  written down because "parsed defused" does not mean "no DTD", and the
  thing standing between that declaration and a read is an expat default
  rather than anything in this repo.
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
- **Three doc gates walk the filesystem where two ask git, and the
  split is deliberate** (recorded batch 138, from the review marathon's
  S19-12, severity `S4`, `stability`). `tests/test_docs_consistency.py:632-647`
  defines `_SKIP_DIRS` without `review/` or `.qwen/`, and gitignore has nothing
  to do with it -- so `test_no_source_file_carries_double_encoded_text`,
  `test_the_url_blocklist_is_defined_exactly_once` and the two citation gates
  via `_production_python()` all read untracked content, while
  `test_the_documented_doc_read_bound_is_not_understated` asks `git ls-files`.
  Measured during the marathon: `review/` held 20 markdown files, roughly
  700 KB of reviewer prose, every one of them read by the mojibake gate on
  every run of the suite.

  **The reviewer argues this should NOT be fixed, and the argument is the
  entry.** The filesystem is the right domain for three of the four questions,
  for a reason stronger than convenience:
  `test_the_url_blocklist_is_defined_exactly_once` exists because
  `tools/builtin/.ipynb_checkpoints/web_search-checkpoint.py` carried an
  **empty** `BLOCKED_DOMAINS` beside the real one and "a reader auditing
  #48/#53/#54 had no way to tell from the grep which was live". A grep does not
  care whether a file is tracked; neither does a code-search tool, nor an agent
  reading this repository. A gate that asked git would have reported clean on
  the exact artifact it was written for -- the same trap "`.gitignore` does not
  untrack" records at `docs/ARCHITECTURE.md:2012`. The same holds for mojibake:
  a file that renders wrong renders wrong whether or not it is staged. And the
  cost was already paid down -- `docs/REVIEW_MARATHON.md` measured this before
  the marathon opened and converted it into two rules of engagement (probes
  live outside the repository, reports are plain UTF-8), each written "with the
  measurement beside it, because the next reader's instinct will be that
  gitignore covers it." Nineteen reviewers were told.

  What is worth adding, and goes in the file rather than in the protocol:
  `_SKIP_DIRS`' definition should say the set is deliberately *not* gitignore
  and that the three walkers answer a filesystem question on purpose, pointing
  at `_readable_docs_on_a_clean_checkout` as the one that asks git and why. Two
  of the four checks already carry that reasoning; the shared helper does not,
  which is why each new reader re-derives it. Adding `review` and `.qwen` to
  `_SKIP_DIRS` costs nothing today but removes the property that a `.py`
  dropped anywhere in the worktree is scanned as production code, which is a
  cheap tripwire worth keeping -- and that is the owner's call, which is why
  this is accepted with an argument rather than recommended.

## ROADMAP_v2's index skipped four sections (closed 2026-09-10)

Found while §47 slice 8 was repairing the record. `ROADMAP_v2.md`'s index then
had entries up to §31 and none for §32 through §47. Sections §32–§38 and
§41–§45 were backfilled since, leaving §39, §40, §46 and §47 absent from the
document a reader consults to find out what is outstanding -- which is the
exact complaint #129 raised about missing status markers, one level up: those
entries read as outstanding, these read as nonexistent.

The check does not catch it because it asserts a FLOOR (at least fifteen
entries) and then checks markers on the entries it found. That was the right
shape for the drift it was written for and is blind to this one.

**Repaired 2026-09-10** by adding the four missing rows in the index's
existing style. The prescription stands: the floor should become "one entry
per `## N.` heading", which is the version that could not drift again -- now
a consistency pin rather than a suggestion.

## 18. CI workflow follow-ups (open, deferred 2026-09-10)

Recorded rather than done: the lint/SAST/matrix rollout (lint.yml,
bandit.yml + baseline, gitleaks.yml + config, pip-audit.yml,
dependabot.yml, codeql.yml, compat.yml, tests.yml hardening) was verified
locally on Windows only. Each item below is green-elsewhere work, not a
defect found.

- ~~**Confirm the new jobs green on Linux runners.**~~ **Answered by the
  push of 2026-10-01** (batch 115), which took twenty commits up at once
  and gave every one of these jobs its first run off this machine.
  Everything green except Gitleaks (the green half is the owner's
  report; the local `gh` token was expired, so the run log was not
  read -- the Gitleaks half was reproduced here against the 8.30.1
  binary the workflow pins) -- and **the prediction in this bullet
  was half right.** There was a first red, but it was NOT platform-shaped:
  it was `tests/test_session_tools.py:40`, a planted fake secret added
  2026-09-17 with no fingerprint in `.gitleaksignore`, which only a `push`
  could surface because that is the one trigger the job has. The baseline
  needed nothing, so the warning here held without being used, and
  bandit's `./`-joined filenames -- "verified from source, not from a
  run" -- are now verified from a run. The gap it exposed was in
  AGENTS.md's "Before calling a change done", which named ruff and bandit
  and not gitleaks; that is fixed there rather than here, because the
  checklist is where a batch looks.
- ~~**arXiv feed hardening (Bandit B314/B405, baselined).**~~ **Done in
  batch 116.** `tools/builtin/arxiv.py` parses the Atom feed with
  `defusedxml.ElementTree` now, pinned `defusedxml==0.7.1`. Two things
  were measured first and both changed the code. (1)
  `defusedxml.ElementTree.ParseError` IS the stdlib `ParseError` -- the
  same object -- so the empty-redirect-body path and the retry loop's
  `except` are untouched. (2) `EntitiesForbidden` and its siblings
  subclass **ValueError, not ParseError**, so they would have escaped
  that `except` and propagated out of `run()`; they get their own handler
  that returns immediately rather than joining the retry tuple, because a
  declared entity refuses identically every time and "failed after 3
  attempts" would name a network problem that is not there.
  **The two baselined rows were removed from
  `.github/bandit-baseline.json` BY HAND** -- 40 deleted lines, nothing
  else touched, verified by a round-trip that reproduces the file byte
  for byte before editing it. Never regenerate it here.
  **Two things found in the baseline while doing it.** Its B314 row says
  `arxiv.py:141` and the call was at line 182 -- bandit matches anyway,
  so baseline line numbers are not an index into the file. And the file
  is a HYBRID: all 136 `results` are `./` (Linux, the half bandit
  compares, correct) while 199 of 200 `metrics` keys are `.\` (Windows).
  The metrics block is not used for baseline comparison, so it is
  cosmetic and was left exactly as found; "fixing" it by regenerating
  would break the half that works.
- **pip-audit threshold.** The job fails on any finding because 2.10.1
  has no severity filter and the tree was clean at all levels. Revisit
  HIGH+-only if a LOW advisory starts taxing PRs; the escape hatch is a
  reasoned `--ignore-vuln` in pip-audit.yml, never silence.
- **Bandit `-s B101` revisit.** Skipped because all 6037 hits are pytest
  asserts and production carries zero (measured). If production ever
  gains an `assert`, that skip starts hiding exactly what it is for.

## 19. `with_goal` uncounted in the `with_*` tier convergence (open, deferred 2026-09-10)

`core/loop.py` assembles five prompt tiers outside `with_catalogs()`
(`with_goal`, `with_catalogs`, `with_memories`, `with_refs`, `with_todos`),
but J11's "converging the four" counts K6/M13/M19/J11 and omits `with_goal`
-- while ROADMAP_v2's own J11 row names goal in the merge ("goal/skills/
memories into one assembly point"). Either the count is four disclosure
tiers with goal deliberately separate (then say so in the AGENTS J11 bullet
and the ROADMAP J11 row alike) or it is five (then the AGENTS bullet, the
ROADMAP row and `tests/BREAKING_CHANGES.md`'s "FOURTH instance" move
together, or the next fix creates the next finding). The tier pin in
`tests/test_docs_consistency.py` waits on the answer, which is an owner
call about what a tier is, not a measurement.

**A FIFTH member is now specified** (batch 122). ROADMAP_v3 §58 (PR1, PR5)
adds `with_persona` beside the four, for the same reason the other four are
outside `with_catalogs`: it is a session fact rather than a pass's
instruction, and it names the live model, which `/model` changes. So the
answer here is no longer "four or five" but "five or six", and PR5 says
explicitly that this item moves with it rather than the family quietly
gaining a member. ROADMAP_v3 §54 (AT7) is the other claimant: an `@` file
mention is thread state and must not reach `pass_prompt()` either, which
makes §19's K6 trap five instances deep.

**Rechecked in the review marathon (batch 138), which found two errors in the
issue that carries the other half of this family (#9).** The defect itself is
real and unchanged -- `with_goal(prompt, memory)`, `with_todos(prompt,
memory)`, `with_memories(prompt, agent=None)` and `with_refs(prompt, memory)`
still take three different signatures, and `with_memories` is still the only
one behind `if system_prompt is None` (`core/loop.py:2144-2152`), which is the
load-bearing asymmetry. But:

1. **The file in issue #9 is wrong.** It says "there are now four prompt-tier
   appenders in `prompts/system_prompts.py`". All four are in
   **`core/loop.py`** -- lines 98, 109, 146 and 168. `git grep "^def with_"`
   over tracked production code returns those four plus `with_skill_catalog`
   and `with_catalogs`, which are the two that genuinely live in
   `prompts/system_prompts.py`. A reader following the issue to that file
   finds the one function all four of these carry a docstring warning them
   *not* to move into.
2. **Half of it has been answered since.** The complaint about "four parallel
   assembly points" was discharged by ROADMAP_v3 §49:
   `RunAgentLoop._conversation_prompt` (`core/loop.py:2118-2152`) is now the
   single assembly point for all four, shared by a user's turn and a wake turn
   precisely so "a wake turn cannot run under a prompt missing the goal, the
   references or the checklist a user's turn has." What remains open is only
   the signature and conditionality divergence -- which is what the issue's
   title and this entry say, so the title is still accurate while the body
   over-states the scope.

The K6 hazard the family is really about is intact and defended in place: each
of the four carries a `MUST NOT move inside prompts.system_prompts.with_catalogs()`
docstring naming its own instance, and `memories/manager.py:24`,
`skills/manager.py:119` and `project_init/manifest.py:20` repeat it from the
other side.

## 20. Restart-required config editor (closed, batch 84)

`config_schema.load(path)` now never touches the live cache -- `path`
validates a candidate, `force` only re-reads the live document -- which is
the seam a future TUI `/config` editor uses. Not built: editing YAML in the
TUI, validating via the candidate path, then requiring a restart to apply
(UN1/UN2: posture is frozen at import, so live reload would need a re-freeze
protocol for `HARNESS_AUTHORITY_KEYS`, in-flight pipeline/pass handling, and
cache clears for `effort_levels`/`context_window`). Owner chose restart-
required over immediate-apply; the command itself is future work.

**RESOLVED (batch 84).** `/config` is built, on the seam this item
describes. What shipped differs from the sketch above in one place, and
the owner chose it: the nine `HARNESS_AUTHORITY_KEYS` are WRITABLE from
the command, behind a confirmation naming what each key permits, rather
than refused. Nothing else about the posture argument moved -- the write
goes to the file and takes effect at the relaunch, so no frozen value is
edited under a session that has already read it, and no unattended route
(settings file, environment variable, tool call) gained anything.

Not built, and not needed by the above: live reload. The re-freeze
protocol, the in-flight pass handling and the `effort_levels` /
`context_window` cache clears this item lists are all consequences of
applying a change in place, which the restart makes unnecessary.

## 21. L4 broad docs sweep remainder (open, deferred 2026-09-12)

Remediation-only done: agent frontmatter (`build.md`/`test.md`), `SECURITY.md`
path example, `README.md` config.yaml-only table keys to lowercase (plus
`tool_compute_timeout_s` 15 -> 20 to match `config.yaml`), and the
`BREAKING_CHANGES.md` flip row to `config.yaml`. Left untouched by design:
runtime `config.UPPER` references in code context (`agent.max_steps or
config.MAX_ITERATIONS`, `config.ToolPermissions()`, `config.MODEL_CONTEXT_
WINDOWS.get()`, etc.) which remain correct via the shim, and historical
`ROADMAP.md`/`ROADMAP_v2.md`/`DEVLOG.md` prose recording what was true then.
A literal every-occurrence rewrite would make correct docs incorrect and
rewrite locked history (D-numbers stable, ARCH §4.1 locked, `test_every_
decision_id_cited_in_production_code_resolves`, README approval-table check).

**RESOLVED in part (batch 83).** The one genuine remediation leftover this
item did not name -- `README.md`'s ceilings sentence, which pointed at
`TOOL_COMPUTE_TIMEOUT_S` rather than the `config.yaml` key -- is fixed, along
with four `CONFIG_ARCHITECTURE.md` entries that told the reader to set a
value in `config.py` in the present tense. What stays deferred is unchanged
and is the larger half: runtime `config.UPPER` references in code context,
which remain correct through the shim, and historical `ROADMAP.md` /
`ROADMAP_v2.md` / `DEVLOG.md` prose recording what was true when written.

## 22. The bandit baseline has drifted (closed, batch 107 -- it had not)

`bandit` with the CI job's exact flags and its exact pin exits **1**, and has
since before the `config.yaml` migration. The unmatched findings are
overwhelmingly in files that migration never touched -- 59 in
`security/sandbox.py`, 32 in `tests/test_credentials.py`, 9 in `tui/app.py`,
across 17 untouched files. `.github/bandit-baseline.json` was generated on
2026-09-10 against 80,761 lines; the tree is now past 82,500.

Batch 83 contributes exactly two findings, both in
`tests/test_config_loader.py`: B404 for `import subprocess` and B603 for the
`subprocess.run` in `test_re_importing_config_re_reads_the_file`. A subprocess
is the right design there -- the test's subject is module-table surgery that
would contaminate the session if done in-process -- so the answer is a
baseline refresh, not a code change.

**Deliberately not done in batch 83** (owner decision). Regenerating a
security baseline is its own change with its own review, and folding it into a
documentation and validation round would sweep in ~250 unrelated findings and
hide both. Whoever takes it should first establish whether the baseline is
merely stale or was generated under a different bandit, since a version
mismatch unmatches findings wholesale and looks identical.

**Measured in batch 96: it is neither stale nor a version mismatch. It is the path
separator.** Bandit records `os.path.join(".", f)`, which is `./x.py` on Linux and `.\x.py` on
Windows, and it matches a baseline entry on the filename among other fields. The checked-in
baseline is the Linux form CI produces, so on this machine *nothing* matches and a local run
reports the whole tree as new — which is exactly the "~250 unmatched findings" above, and why
the same baseline is green in CI on the same commit. So the local exit 1 is a platform
artifact and not evidence about the baseline's freshness.

Two consequences for whoever picks this up. A local `bandit` run is **not** a check of the
baseline here; convert the filenames to the Windows form first (batch 96 did this against a
scratch copy to verify two new entries actually matched). And a baseline regenerated on
Windows would be checked in with `.\` filenames and would silently match nothing in CI, which
is the more expensive half of this to learn by accident.

**CLOSED (batch 107), by measurement rather than by work.** With the baseline's filenames
converted to the Windows form, `bandit` with the CI job's exact flags and pin exits **0** over
the tracked tree -- every finding matches. Batch 106 measured that three ways and batch 107
re-measured it before closing. So this entry's original premise, that the baseline had drifted
and wanted regenerating, was never true: what it measured was the path separator, which batch 96
identified and which the paragraph above already records.

What survives is the procedure, and it is already in `AGENTS.md` and `bandit.yml`. Nothing here
needs doing.

## 23. The transcript does not reflow on a terminal resize (closed, batch 112)

Rows already drawn keep the width they were drawn at when the terminal is
resized. RichLog renders at write time and stores Strips; its `on_resize`
only replays writes deferred before the widget's first size and never
re-wraps a stored row. Measured headless, with the console sized to match
the terminal: an answer streamed at 120 columns drew rows 114 wide, and after
widening to 200 the same rows were still 114 wide against a 198-column panel,
while new rows used the full width. A restart's replay puts it right, and so
does `/theme`, the only production caller of `Transcript.rerender()`.
`Transcript`'s class docstring has named this a known edge since §38, scoped
to "a terminal resize mid-answer" -- it is wider than that: every row drawn
before the resize keeps its width.

Found in batch 90, while fixing the hidden-pane wrap, which has the same
signature -- rows at a stale width, healed by a restart -- and a different
cause (`Transcript._region_width`). That one is fixed; this one is not.

**Deliberately not done in batch 90** (owner decision). Reflowing on resize
means redrawing from `_entries`, and `rerender()` is not safe to call at an
arbitrary moment: it starts with `flush_stream()`, which closes an open answer
span, so a resize mid-turn would split the live answer into two entries under
two `venastine ›` labels -- the thing §38's one-entry-per-span rule exists to
prevent. A fix needs a redraw that leaves an open span open, debounced so a
window drag is not one replay per event, and a decision about where the
scroll position lands. That is its own change.

**RESOLVED (batch 112).** `on_resize` coalesces a redraw, and the redraw is
`reflow()` rather than `rerender()`: the replay that does not flush. Every
obstacle this entry names was real, and each is met rather than avoided.

* **The open span stays open.** `_uncommit_partial_line` gives the tail of
  `_stream_text` after its last newline back to `_pending` and moves
  `_entries[-1]` with it, so a `/copy` or a `/theme` in the same window still
  agrees with the screen; the replay draws what is left; and `_commit_ready`
  re-cuts the tail at the new width. That last step is not tidying. The
  handed-back line is usually the WHOLE of an open span, because a model
  sends a paragraph before it sends a newline, so a replay on its own would
  leave the reader watching their half-written answer vanish until the next
  delta arrived. `_render_entry` gains `closed`, which only the reflow ever
  passes False: the one piece of furniture a replayed open span must not
  draw is reasoning's `-- done thinking` delimiter.
* **Debounced** on `REFLOW_DEBOUNCE_S` (0.15s), and keyed on `_wrap_width()`
  CHANGING rather than on the event -- a `Resize` arrives for a height change
  too, and no wrap moves.
* **The scroll keeps its FRACTION.** The row count changes with the width, so
  an exact restore is not available at any price, and a resize is
  involuntary: landing where `/theme` lands would lose the place of anyone
  reading back through the session.

**One thing had to be measured rather than reasoned about.** RichLog DEFERS
its auto-scroll to after the next screen refresh (`scroll_end(...,
immediate=False)`), so a scroll position computed and set during the reflow
was overwritten a frame later by the last write's pending scroll -- the
reader landed at the bottom however carefully it was worked out. `auto_scroll`
is off for the duration of the redraw and both restores pass `immediate=True`.

**Pinned as the two row-equality classes' rule carried across a resize**: the
rows after a widen are the rows the same entries produce when written at that
width to begin with. Over prose, a wrapping list item, a diff and a thinking
span -- which are item 15's three pre-wrapped constructs plus the ordinary
case -- with a companion test asserting that the two panel widths really do
wrap differently, since otherwise every case could pass by drawing the same
rows twice. And the hazard this entry parked the item on, asserted directly:
one assistant entry and one `venastine` label after a resize mid-answer.

**The debounce is asserted on its mechanism, not by waiting it out.** One
`pilot.resize_terminal` costs ~100ms on this box, so a ten-step drag takes
1.06s -- longer than any debounce short enough to sleep through in a test,
and under eleven xdist workers the margin is whatever the box happens to be
doing. That is a scheduling lottery rather than a test. The debounce is
pushed past the end of the test instead, and what is checked is that each
resize STOPPED the pending redraw before arming its own.

## 24. A newline after a row that filled the width draws a blank row live (closed, batch 112)

When the last row of a line fills the transcript exactly, the width rule
commits the whole buffer -- the cut lands after a trailing space that
overflows by its own cell, and `_split_committable` keeps nothing back. If
the model's NEXT delta begins with the newline, the newline rule commits a
chunk that is only `"\n"`, and both commit paths draw it as a blank row:
`_write_stream_chunk` deliberately ("a chunk that is only a newline is the
blank line between paragraphs"), and `_write_thinking_lines` as a bar with
nothing beside it. A replay of the same entry has no such row, because there
the newline simply ends the line.

Measured in batch 90 on a 160-column pane, streaming a row of exactly the
wrap width plus a space, then `"\n"`, then more text: an answer drew 5 rows
live and 4 replayed, and thinking drew 7 live and 6 replayed. It predates
the batch -- the answer path was not touched by it. It is rare in practice:
the row has to end on the edge AND the newline has to arrive in the
following delta, and a `/theme` or a restart silently removes the row, which
is the "rerender reflows what it was only meant to recolour" symptom
`TestAStreamedAnswerRendersLikeAWrittenOne` exists to catch; none of its
cases happens to land a row on the edge.

**Deliberately not done in batch 90** (owner decision). The fix is to
remember that the last committed row ended exactly at the width and treat
one immediately following newline as the end of that line rather than a
blank one, on both paths. The answer path's single-trailing-newline and
blank-line-between-paragraphs rules are pinned, and getting this wrong
swallows a real paragraph break, so it wants its own cases in both
row-equality classes rather than riding along with a width fix.

**RESOLVED (batch 112), and NOT by the prescription above.** That one
remembers, at the DRAWER, that the last row ended at the width, and swallows
one newline that follows it. The mechanism turns out to be narrower than the
symptom. The only way the width rule can drain the buffer is
`width_split`/`plain_split` cutting after the last space that FITS and that
space being the buffer's final character -- a row that filled exactly, then a
space that overflows by its own cell. A cut that keeps nothing back has
bought nothing, so it commits nothing; the newline then arrives in the SAME
chunk and simply ends the line. Two lines, both paths, no new state, and
nothing for item 23's reflow to remember to clear.

**Where it is not, and why.** Not in `markdown.plain_split`, which shares
`_plain_cut` with `plain_wrap` -- the REPLAY's wrap, and the thing this whole
surface is measured against. Not in `Transcript._split_committable` either,
for a reason that only appears in the caller: `_commit_ready` hands it the
CAPPED prefix, so a drained prefix is not a drained buffer, and holding there
would make a paragraph sitting in front of a held table wait for the table to
close -- the regression the cap was built to remove.
`limit >= len(self._pending)` is that half of the rule and has a test of its
own (`test_a_drained_prefix_is_not_a_drained_buffer`, an unclosed `**` doing
the capping).

**`plain_wrap` has refused to draw this row since batch 90** -- "a cut at the
very END of the line closes the last row rather than opening an empty one" --
which is the replay side of the identical shape, fixed while the commit side
went on producing it. They are now one rule stated in two places, which is
what the row equality exists to keep true.

Reproduced before anything changed, at this entry's own numbers: 5 rows live
against 4 replayed for an answer and 7 against 6 for reasoning, on an
84-column pane where the entry measured a 160-column one. Pinned in BOTH
row-equality classes as the entry asks, with the text built from the measured
width rather than written as a literal -- a literal stops reproducing the day
the padding changes and goes on passing -- and beside each one the control:
a real blank line after an edge row is still drawn, so the fix cannot quietly
become "swallow every newline after a full row".

## 25. A model call is not retried once its output is on screen (closed, batch 113)

Batch 91 retries a model call that fails transiently -- a dropped connection,
a 429 or 5xx, an error sent inside a stream that had already opened -- but a
run someone is WATCHING (a TUI chat turn, a research pass) only while the
failing attempt has streamed nothing. A drained run (a subagent, a one-shot,
a JSON-retry continuation) is retried whatever it had streamed, because
run_to_completion discards its deltas. So the TUI turn that fails mid-answer
still fails exactly as it did before the batch, with the half-answer on
screen and `[error: ...]` under it.

**Deliberately not done in batch 91** (owner decision). Retrying after
visible output means taking the partial answer back: a LoopEvent telling the
shells to discard the span (core/events.py has no error variant, on purpose
-- consumers rely on a real exception propagating), and a transcript able to
retract rows RichLog has already stored as Strips, plus the entry log and
the label that span opened. Both are real designs of their own.

### RESOLVED in batch 113

Both halves of that last paragraph turned out to be smaller than they read,
and for opposite reasons.

**"A transcript able to retract rows" was already built.** Batch 112 gave
the widget `_replay()` -- redraw from `_entries` -- and `_drop_last_entry()`,
which keeps the `_links`/`_opens` side tables in step with the log. The
retraction is those two in a loop plus the buffer clears. The entry log and
the label are not extra work either: the label is re-derived by the replay
from the role sequence, which is §43's RM1, so one turn still shows one
`venastine`.

**The event was the real design**, and the objection recorded here does not
apply to it. `core/events.py`'s "no error variant" rule is about where a
failure is HANDLED -- it has to stay an exception so `orchestrator.py`'s
failure-path persistence still fires. A retraction is not a failure turned
into data: it says the rows drawn so far are being replaced, and the
exception still propagates unchanged when the retries run out. `retract`
is a ninth `LoopEvent` field, display-only and deliberately NOT mirrored
onto `ModelResponse`, because a consumer that drains has drawn nothing and
would be asked to undo something that never happened.
`test_pipeline_events.py` is where that had to be argued, and is.

**WHAT THE ENTRY GOT WRONG: a research pass was never a drawn run.** This
item names "a TUI chat turn, a research pass" as the watched runs, and
`core/reasoning/orchestrator.py` translates a pass's `token_delta` into a
throttled character total and nothing else -- "only the volume escapes,
never the content". A pass was nonetheless refused its retry, because
`drained` was the proxy for "nothing reaches a screen" and a pass yields
events. So the gate was protecting a counter. `Transcript` is the only
surface in the repository that draws model text; `cli.py` is not a
`token_delta` consumer at all.

**`drained` is gone.** Its only reader was the clause this item is about,
and one rule now covers every caller. Keeping it as an inert parameter was
considered and rejected: a knob that does nothing is re-derived wrongly by
the next reader.

**AND THE BOUNDARY IS NOT THE SPAN**, which is the part the entry could not
have known. "Discard the span" is the obvious rule and it is wrong:
`_write_thinking_chunk` closes an open ANSWER before it draws, so one
attempt with interleaved reasoning leaves several entries behind with only
the last of them open -- measured as three, from text then reasoning then
text. The retraction truncates to a MARK instead, armed by the first delta
of a span and cleared by a flush. The first rule tried was "take the mark at
every flush", and the test written to pin its stated cost found it wrong
instead: every `write_*` opens with `flush_stream()`, so a line written
mid-attempt moved the mark past the attempt's own text and the retraction
deleted the unrelated line while keeping the half-answer.

## 26. An unset `AGENT_WORKSPACE` makes the harness its own project (closed, batch 107)

Measured in batch 96, from an owner report after launching a fresh install on a new machine:
the harness asked to trust its **own** `AGENTS.md`.

With `AGENT_WORKSPACE` unset and the harness launched from its install tree, `WORKSPACE_DIR`
defaults to `./workspace`, which resolves inside the harness — and `check_workspace` *exempts*
it, deliberately, via `WRITABLE_INSIDE_HARNESS` (`workspace`, `output`), which is what keeps
the shipped layout usable. Launch therefore proceeds, and `main.py` then resolves
`project_path` to `os.getcwd()` because `WORKSPACE_DIR_EXPLICIT` is False. That is the install
tree. So D17 trust, `.venastine/`, `/init`'s destination and project-scoped memories all point
at the harness, and the trust prompt lists the harness's own `AGENTS.md` (measured:
`content_files` returns `['AGENTS.md']`, `is_trusted` False).

**The disparity is the defect.** Naming the same directory explicitly is already REFUSED
(`AGENT_WORKSPACE=<harness root>`, measured), as is any non-exempt subfolder. Only the
implicit path gets through, so the guard's own rule is enforced when you say it and skipped
when you don't.

**Owner decision (2026-09-17): refuse to launch and say `AGENT_WORKSPACE` has to be set** —
"this way there is no disparity once the harness is running." Recorded rather than built,
because batch 96 was scoped to CI and the transcript defects; it lands after §49 slice 1.

Prescription for the implementing batch. Guard in `main.py` **after** `project_path` is
resolved (it is currently computed right below `check_workspace`), returning 2 like the
existing refusal and naming the variable in the message. Scope it to `project_path ==
harness_root()` — equality, not containment — so an explicitly chosen `<harness>/workspace`
keeps working and only the install tree itself is refused. **Budget for the blast radius:**
about thirty `main.main([])` calls in `tests/test_cli.py` run from the repo root and would hit
the new refusal; extend the shared `startup` fixture rather than editing thirty tests, taking
the seam from the existing `check_workspace` monkeypatch in that file.

**RESOLVED (batch 107).** `security/protected_paths.check_project()` answers for the implicit
route what `check_workspace` already answered for the explicit one, and `main.py` calls it
immediately after the project path is resolved. Exit 2, the reason on stderr, equality rather
than containment so `<harness>/workspace` and `<harness>/output` keep working.

Three things this entry got wrong, and the first is the expensive one.

**The blast radius does not exist.** Measured: **18 call sites in 16 tests**, `main.main` appears
in **no other test file**, and the `startup` fixture's first act is `monkeypatch.chdir(tmp_path)`
-- so fifteen of the sixteen resolve their project to `tmp_path` and never reach the guard at
all. Exactly one test runs from the repo root, and it builds its own stubs. The fixture needed no
change. The lesson generalises: **a blast radius counted by grepping for the call is not the same
as one counted by asking what the call SEES**, and here the difference was a factor of eighteen.

**The prescribed position would have kept the symptom.** The guard was to go "after
`project_path` is resolved". That point is two lines above `load_project_config` -- and the
reported symptom, the harness asking to trust its own `AGENTS.md`, is produced INSIDE that call
by `_ensure_workspace_trust`. A refusal there prints after the prompt it exists to prevent. It is
also below `create_db_and_tables`, so a refused launch would have left a database behind, which
is the exact thing audit #101 moved that call to stop and which the comment four lines above it
argues for. So the project-path resolution moved up instead, to just under the `check_workspace`
block, and both orderings are asserted at `main()` rather than left to be inferred.

**`--secrets` is refused too, and it is the one exception that would be defensible.** The owner
chose one rule with no exceptions (2026-09-26). Of the five early-exit commands `--memories`,
`--forget`, `--summary` and `--init` are project-scoped and refusing them is the point --
`--memories` from the install tree lists the HARNESS's memories. `--secrets` manages the
user-tier store under `~/.config/venastine` and has no project dependency whatever. If anyone
ever wants one exception, that is where it goes; the cost of not having it is typing
`AGENT_WORKSPACE=./workspace` in front of one command.

Reproduced before it was fixed, from the install tree with the variable unset:
`describe_project_content` returned `['AGENTS.md']`, `is_trusted` was False and
`load_project_config` received the harness root -- after which the run proceeded to `run_chat`
and exited 0. See also **item 28**, which this made user-visible.

## 27. The container-runtime probe leaks across the whole session (closed, batch 107)

`security/sandbox.py:278` memoises a **live** `docker`/`podman` subprocess probe in the module
global `_runtime_probe`, set once per process and never reset. `_reset_runtime_probe()` exists
two functions below it (`sandbox.py:306`), is documented "Tests only", and is used by exactly
ONE autouse fixture -- scoped to `TestDockerAvailable` in `tests/test_shell.py`, whose own
docstring gives the general reason: "otherwise the first test's answer would be the only one any
of them measured." Nothing in `tests/conftest.py` resets it.

So the first test anywhere in a session that reaches a live probe fixes the answer for the
~5,900 that follow, and `_probe_container_runtime` allows up to **10 seconds per daemon** when
one is unresponsive. On the WSL box, which has both `docker` and `podman` on PATH, the answer
therefore depends on machine load rather than on anything the suite controls.

**Measured in batch 106, as a controlled comparison across four full WSL runs.** The failing
set is nine either way and its membership MOVES:

| Tree | New test files | The container-dependent tests that failed |
|---|---|---|
| §52 | included | `test_declared_network` x3 |
| HEAD | ignored | `test_session_backends` x2 |
| §52 | ignored | `test_session_backends` x2 |

Source held constant and the test files varied: the cohort changes. Source varied and the test
files held constant: the cohort is identical. **The variable is collection order**, which any
new test file perturbs -- so adding a file to this suite changes which pre-existing flaky tests
fail on WSL, and a batch's WSL run cannot be compared to a previous batch's by name.

The failure reads as a real defect and is not one: `_shell_approval_check` returns `False` at
`if containment == UNAVAILABLE`, so a test asserting "this call asks for approval" sees
"auto-approved". `wsl-verifies-posix-behaviour`'s baseline table -- eight named tests -- is
therefore not a stable list, and both HEAD and §52 produced a member outside it
(`test_question_tool::test_the_discuss_button_defers` and `test_declared_network` respectively).

**The fix is one autouse fixture in `tests/conftest.py`**, the same shape as batch 105's
`clear_network_tool_caches` and for the same reason -- a module-level memo with a `_reset`
helper called in one place instead of globally.

**It was NOT done in batch 106, and the reason is a cost that has to be measured first.**
Resetting before every test means every test that reaches a live probe re-probes, at up to 10
seconds per unresponsive daemon. If more than a handful do, the fix adds minutes to every run
on every platform to stabilise a cluster that is already understood. The implementing batch
should **count the live probes first** -- patch `_probe_container_runtime` to increment a
counter and run the suite once -- and then choose between the blanket autouse fixture and a
session-scoped probe stubbed to a fixed answer, which costs nothing and makes the cohort
deterministic but stops the suite from ever exercising the real detector.

**RESOLVED (batch 107), for all three probes rather than the one this entry names.** The autouse
fixture is `isolate_sandbox_probes` in `tests/conftest.py`.

**THE HEADLINE IS NOT THE ONE THIS ENTRY WROTE. It was never a WSL quirk.** Counting the live
probes turned up something the four WSL runs could not see: **a full Windows run at HEAD failed
four tests** -- the same three `test_declared_network` cases the WSL cohort keeps producing, plus
`test_wsl_backend`'s container notice -- for one reason, that **Docker Desktop was not running
that morning**. The same commit was green the day before with the daemon up, and batch 106's
"5910 passed, zero failures" was measured in that state. So the suite's result on the primary
platform was a function of whether a background service happened to be started, and every green
run recorded in this repository inherited that condition silently.

**This entry also undercounted itself twice.** `security/sandbox.py` memoises **three** live
probes, not one: `_runtime_probe`, `_wsl_probe` and `_ssh_probe`, each with a `_reset_*` helper
used only by fixtures inside the file that tests it, and `tests/conftest.py` reset none of them.
And the container probe's ceiling is not "10 s per daemon": `_probe_container_runtime` can make
**three** `info` calls -- `docker info`, then `podman info`, then `_why_podman_cannot_limit`'s
`info --format` -- so ~30 s. It named ONE class-scoped reset fixture; there are two.

**The census, and it licensed a better fix than either option offered.** From a pytest plugin
loaded with `-p`, so no repository file was edited while the suite ran: 34 calls into the three
probe functions across a full run, of which **two** were live container probes (0.98 s together),
both from incidental callers. Every other container call came from the two classes that patch
`subprocess.run` and measure their own mocks. **So nothing exercises a live probe on purpose**,
which is what makes this entry's stated cost of a stubbed answer -- "stops the suite from ever
exercising the real detector" -- illusory. The detector is exercised against mocks, by tests
that reset the memo themselves, and those keep working.

**Which constant, chosen by measuring rather than by taste.** Running the affected files under
each candidate: `RuntimeProbe("docker")` gives 1026 passed, and "no runtime available" gives
four failures. A working Docker is what this suite has always silently assumed -- and
`known_runtime()` returning `docker` rather than `podman` is the other half, since
`test_session_backends` asserts the CLI by name in the argv. That also explains the WSL cohort
exactly: measured there, the box answers `RuntimeProbe(name='podman')` because both runtimes are
on its PATH and Docker's daemon does not reply, so every incidental caller inherited an answer
the suite was not written for. Windows and WSL now agree because neither is asked.

Neither option this entry named was taken. A blanket reset makes every incidental caller
re-probe; a session-scoped live probe still lets a loaded machine decide. The `_reset_*` helpers
run before the constant is installed rather than the globals being assigned, because two of them
own more than a memo. The fixture is **setup-only**, which `clear_network_tool_caches` is not,
and the asymmetry is forced: `monkeypatch` is created by an earlier autouse fixture and undone
after this one, so a teardown would call `_reset_wsl_probe` while `test_wsl_backend`'s `wsl_on`
still has `_wsl_workspace` replaced by a plain lambda with no `cache_clear`. Softening that call
to a `getattr` to suit a fixture is what `ARCHITECTURE.md` warns against, and a teardown buys
nothing: the next test's setup installs the answer before anything can read one.

Not swept in, and named so it is not rediscovered: `_ssh_auth_rejected` and `_sudo_rejected` are
two more process-global memos in the same file, reset only in `test_ssh_backend.py` and
`test_sudo.py`. They are quarantine state rather than probes -- nothing spawns to fill them, and
blanket-clearing them would mask a test that depends on one -- so they stay as they are.

## 28. `workspace_dir` in `config.yaml` names a workspace but not a project (closed, batch 111)

`WORKSPACE_DIR_EXPLICIT` is `"AGENT_WORKSPACE" in os.environ` -- the **presence of the variable**
(`config_schema.py`), which is correct and deliberate: the shipped default `./workspace` is a
subdirectory of the launch directory, so a value test would move the project one level down for
everyone who set nothing, and `CONFIG_ARCHITECTURE.md` records that reasoning at length.

But `workspace_dir` is also an ordinary `config.yaml` key, settable with `/config`. Someone who
names their project there gets the workspace they asked for and a project path of `os.getcwd()`.
Two routes to what reads like one setting, doing different things, and only one of them documented
as doing the second.

**Item 26 made this user-visible rather than latent.** Before batch 107 the mismatch cost a
misplaced `/init` and a trust prompt in the wrong directory. Now it costs a **refused launch**: set
`workspace_dir` in `config.yaml`, launch from the install tree, and the harness refuses and tells
you to set a variable you reasonably believe you already set. The refusal message names the trap
explicitly for that reason, which is mitigation and not a fix.

**Not fixed in batch 107** (owner decision, 2026-09-26). Making `WORKSPACE_DIR_EXPLICIT` true for a
`config.yaml` value would move the project path for everyone already using the key -- a behaviour
change with users, inside a batch about something else. Whoever takes it decides first what "named"
means: *any* value, which moves the project for every reader of a shipped `config.yaml` whose
`workspace_dir` is already `./workspace`, or *differs from the shipped default*, which is a
comparison against `config_schema`'s default and quietly makes `./workspace` un-nameable. That
choice is the whole decision; the code after it is one line.

**RESOLVED (batch 111), and by a third option neither of the two above.** The owner took
`workspace_is_project`, a second `config.yaml` key shipped `false`, and
`config_schema.derived_values` now reads `cfg.workspace_is_project or "AGENT_WORKSPACE" in
os.environ`. It takes `cfg` to do it, which costs nothing: there is exactly one caller,
`as_module_namespace`, and it already holds the parsed config.

**Both options this entry offered turn out to be worse than they read here, and the second is
worse for a reason the entry did not have.**

- *Any value* is not a spectrum, it is always true: `config.yaml` ships `workspace_dir:
  ./workspace` written out at line 497, so "the file names a workspace" is a fact about every
  install that has never been edited. That moves the project for the entire existing population,
  which is the one thing the presence rule exists to prevent.
- *Differs from the shipped default* needs a shipped default to compare against, and **there is
  no such thing in this codebase**: `HarnessConfig` carries no field defaults at all -- every one
  of its ~150 values is required and comes from the file. So the comparison needs either the
  repo's first schema default or a literal `"./workspace"` written a second time in Python, which
  is precisely the stale-second-copy shape `shipped_defaults()`'s own docstring exists to refuse
  ("EXISTS SO A TEMPLATE CANNOT QUOTE A STALE DEFAULT"). It also makes `./workspace` un-nameable,
  which this entry already knew.

A second key has neither cost. Nothing moves, because the default is `false`; no value becomes
un-nameable; and the two keys now say two different things out loud instead of one key saying
half of one. **`main()` was not touched** -- it still reads one boolean, because the OR lives in
the config layer, which is where a question with two spellings should be answered once.

**NOT an authority key**, and the reason is now a test rather than a paragraph
(`test_posture.py::test_no_authority_key_has_an_environment_override`). An authority key is one
NO UNATTENDED SURFACE reaches -- measured, `ENV_OVERRIDES` and `HARNESS_AUTHORITY_KEYS` are
disjoint today. The identical effect is already reachable through `AGENT_WORKSPACE`, an ordinary
override, so gating the file key would have put the confirmation on the louder of two routes. It
is also strictly less dangerous than the `workspace_dir` beside it, which is not gated either and
whose own comment names the sharper hazard.

**The refusal message named two routes and a trap; it now names three routes.** It used to close
with "Setting workspace_dir in config.yaml is not enough on its own" -- accurate, and no use at
all to the person reading it, who had just written the directory they meant into the file the
refusal is about. Reproduced before the change and again after (`probe28.py`): without the key,
`check_project` refuses and `main()` resolves the install tree; with it and no variable anywhere,
the project is the named workspace and there is no refusal.

`/config` says it too, for both keys and in both directions -- `workspace_dir` states whether it
IS the project in this session and names the companion when it is not, and `workspace_is_project`
lists what moves with it. The shipped state is the one that surprises people, so it is the one
stated rather than implied.

**And the first version of that sentence broke the rule the module it lives in exists for.** It
read `find("workspace_is_project").in_file` and said "It IS the project here" -- but *here* is
the session, and `in_file` is not the session: `/config workspace_is_project true` writes the
file and leaves the running process exactly where it was until a restart. That is batch 85's
whole subject, stated in `KeyRow`'s own docstring, and the new line was in the same file. It now
reads the session for "is it", the file for "what is written", and adds a line when they differ
-- the shape `pending` already has for a row about itself. Caught by reading the diff back rather
than by a test, which is why the mutation table gained an eighth row that reinstates exactly that
bug (`pair-reads-the-file-for-the-session`, killed).

## 29. Session ownership is skipped for a `None` thread, and the two lookups disagree (closed, batch 110)

Found by batch 108's review of the unpushed range; not a defect today, and recorded because the
asymmetry is a trap rather than because anything is wrong now.

`core/shell_sessions.py::_owned_locked` answers "is this session this thread's":

```python
if owner_thread is not None and session.owner_thread != str(owner_thread):
    return None
```

So `owner_thread=None` means **no ownership check at all** -- any session with that id is returned.
`list_for` one function down asks the same question the other way, `s.owner_thread == str(owner_thread)`,
where `None` stringifies to `"None"` and matches nothing. One value, two opposite readings, in two
functions that sit next to each other.

**It is unreachable from the tools, which is why this is an item and not a fix.** `shell_input`,
`shell_output` and `shell_kill` all pass `memory.thread_id`, and `core/memory.py` sets that from
`create_thread()` or from a thread id it has already validated -- it is never `None`. The `None`
arm exists for the two internal sweeps, `kill_owned` and `kill_by_span`, which have already
filtered by owner themselves and pass no thread deliberately.

**What makes it worth writing down.** The tool layer's whole ownership story is "the owning thread
is the injected `memory`'s, never a param -- a model that could name the thread could reach another
conversation's sessions" (`tools/builtin/shell_sessions.py`). That guarantee rests on
`memory.thread_id` never being `None`, which is a property of a different module and is asserted
nowhere. A future caller that passes a thread id it happens not to have -- a replay path, a
reconstructed session, a subagent whose run raised -- gets cross-conversation access with no error.

**Prescription.** Make the internal sweeps say what they mean, with an explicit sentinel or a
private `_locked_by_id` that does no ownership check, so `_owned_locked` can require a thread and
`None` stops being a skeleton key. Then pin it: a test that `_owned_locked(id, None)` returns
`None`, and one that the two lookups agree about the same argument. The change is small; what it
buys is that the ownership boundary stops depending on a fact about `core/memory.py` that nothing
checks.


**RESOLVED (batch 110), with one correction to the entry above.** The `None` arm is not only the
two internal sweeps. `kill`'s own docstring said *"owner_thread None is the user's kill, which may
name any session"*, and the TUI's stop relies on it -- so the authority is real, and the fix is to
name it rather than to remove it.

`core.shell_sessions.ANY_OWNER` is that name: a module-level sentinel with a `__repr__`, so a
traceback says which authority was exercised. `_owned_locked` lets `ANY_OWNER` past and compares
everything else, `None` included, which makes it agree with `list_for` about the one input the two
read in opposite directions. And **`kill`'s `owner_thread` lost its default** -- four call sites in
the tree, eight in the suite -- because a default meaning "skip the check" makes the safe call the
one a caller has to remember.

Pinned in `tests/test_shell_sessions.py` and `tests/test_session_tools.py`: a missing thread owns
nothing through `_owned_locked`, `kill`, `output` and `send`; the two lookups agree about `None`
and about a foreign thread; `ANY_OWNER` still works, which is what the sweeps and the user's stop
need; and at the tool layer a memory with no thread sees nothing rather than every session in the
process. The `send` assertion reads its exact message on purpose -- `send` refuses a background
session on its KIND two lines further down, so "an error came back" would pass with no ownership
check at all.

## 30. `test_tui.py` is the floor on how fast the suite can run (closed, batch 114; cause corrected and the faster mode adopted in batch 110)

Batch 109 put the suite on `pytest-xdist` with `--dist loadfile`, which sends a whole file to one
worker. That mode was chosen for coverage rather than speed -- four places in the suite depend on
one test running immediately after another in the same process, and three of them would go
*silently vacuous* under xdist's default `load` rather than failing. The cost of the choice is that
**the slowest single file sets the wall time whatever the worker count.**

Measured, `--durations=0` on a serial run, aggregated per file:

| File | Seconds | Share |
|---|---|---|
| `tests/test_tui.py` | 127.8 | **30.9%** |
| `tests/test_agent_navigation.py` | 26.3 | 6.4% |
| `tests/test_live_output.py` | 22.7 | 5.5% |
| `tests/test_interactive_sessions.py` | 22.2 | 5.4% |
| `tests/test_tool_budgets.py` | 20.6 | 5.0% |

413s of measured test time over a 127.8s floor is a **ceiling of 3.2x**, and `-n auto` already
measures 3.18x. So the suite is not worker-starved and adding workers makes it worse -- `-n logical`
(16) ran consistently slower than `-n auto` (11), because past the floor the extra processes only
contend. **Anyone trying to speed the suite up by raising the worker count is turning a dial that
is already at its stop.**

`test_tui.py` is 431 tests and 423 Textual pilot sites; the time is the pilots, not any one test
(its slowest is 2.95s). Two ways out, and they are different decisions:

**Split the file.** Purely mechanical, no test changes, and it raises `loadfile`'s ceiling directly:
split at class boundaries into three or four files of roughly equal pilot count and the floor drops
towards the next file down, ~26s. Nothing in `test_tui.py` is an ordered pair, so the split is safe;
what it costs is that the suite's largest file stops being findable by name.

**Or make `--dist loadscope` viable**, which schedules by class and measured **1:38 (4.38x)** against
loadfile's 2:15 -- without touching `test_tui.py` at all. It is not green today, and exactly one
thing stops it:

> `test_config_edit.py::TestTheFileAndTheSessionAreTwoThings` has two tests
> (`test_changes_accumulate_across_keys`, `test_pending_lists_nothing_on_an_untouched_file`) that
> compare `in_session` against `in_file`. `in_session` resolves through `config_schema.current()`
> to `load()`, whose body is `if _cached is None: _cached = validate(read_document(None), ...)` --
> and `read_document(None)` reads the module's `CONFIG_PATH`, which the test's own `_copy_config`
> has just monkeypatched to a throwaway copy. So when that class is the FIRST thing to touch
> `config_schema` in a process, the "session" value is read from the same file the test then writes
> to, both sides agree, and the distinction the tests exist for disappears. Under `loadfile` it
> passes only because eighteen earlier classes in the file have already filled `_cached` from the
> real document.

**That is a latent fragility in its own right, independent of xdist**: the same two tests are
wrong-by-luck for anyone running that class on its own. The fix is one line -- establish the session
document explicitly (`config_schema.current()`) *before* `_copy_config` repoints `CONFIG_PATH`, so
the test states the precondition it has been silently inheriting. Worth doing on its own merits; it
happens also to unlock the faster mode.

**Prescription, in order.** Fix the `test_config_edit.py` precondition because it is a real defect.
Then re-measure `loadscope` over three runs and compare the failure set by cause, not by count --
`loadscope` splits every file by class, so it is a wider change than it looks and the two tests
above are proof that reading the suite does not find all of these. Only then decide between the
modes. The measurement harness is in the batch 109 scratchpad (`bench109.py`, `tally109.py`).

---

**BATCH 110: THE CAUSE ABOVE IS WRONG, the prescription was aimed at the wrong file, and
`loadscope` is now the default. Each of those came from running something rather than reading it.**

**The cause.** The entry says the two failing tests are "wrong-by-luck for anyone running that
class on its own". They are not: the class passes standing alone, six of six in 1.7s, and passes
again with the full suite collected behind it. What actually fails is a **cross-file leak, with no
xdist involved**:

```
pytest tests/test_storage_e2e.py tests/test_config_edit.py     # 4 failed, 125 passed
```

`test_storage_e2e.py`'s module-scoped `real_storage` pops `config` out of `sys.modules` and
re-imports it under `APP_DB_PATH`, which runs `config_schema.load(force=True)` -- deliberately, and
`config.py`'s comment names this fixture as the reason the flag is there. `config_schema` is not in
the swap list and cannot be, so the teardown restores `sys.modules` and the SCHEMA cache keeps
`db_path = <tmp>/e2e.db` for the rest of the process.

Four tests, not two. The extra pair is the interesting part: `TestTheLoaderCacheDoesNotLeakBetween
Tests::test_two_still_sees_the_document_this_process_started_on`, whose class exists to catch this
exact class of leak, and `test_the_shipped_file_is_untouched`, which fails with *"something in this
run wrote the real file"* -- an accusation about a file nothing had touched. The suite is green only
because collection is alphabetical and `test_config_edit` sorts first, the same sentence
`restore_config_schema_cache` already uses about a different pair. **That fixture cannot cover
this**: it is function-scoped, so pytest builds the module-scoped one first and the "before" it
captures is already poisoned.

Fixed by saving and restoring `config_schema._cached` in `real_storage`, beside the `APP_DB_PATH`
it was read under. Pinned by a module-scoped sentinel that `real_storage` REQUESTS -- which is what
buys it the last word, since a fixture is finalised in reverse setup order -- so it can assert the
cache is the object the module started with after the swap has put it back. Nothing else inside
a module can watch its own module-scoped teardown. Verified as a real kill: with the restore
removed it becomes a teardown error naming the cause.

(The first version of that pin was a child process running the two files in the failing order. It
worked, and it cost two NEW bandit findings -- B404 for the `subprocess` import, B603 for the call
-- in a project with zero `# nosec` and a standing rule against regenerating the baseline. The
sentinel asserts the mechanism instead of the consequence and costs nothing.)

**The measurement, taken again with the leak fixed.** Alternated so thermal drift cancels instead
of accumulating in whichever mode ran second, and compared by failure SET rather than count:

| Configuration | Runs | Wall | vs serial |
|---|---|---|---|
| `-n0` serial | 2 | 8:58, 9:13 | 1.0x |
| `-n auto --dist loadfile` | 3 | 2:58 / 3:04 / 3:07 | 3.0x |
| **`-n auto --dist loadscope`** | 8 | 2:09 - 2:22 | **4.2x** |
| `-n logical --dist loadscope` | 5 | 2:13 - 2:19 | 4.0x |

Every run green, and the same `passed`/`skipped` pair within a configuration. This box is busier
than the one batch 109 measured on, which is why serial reads 9:06 where 109 read 7:10 -- the RATIO
travels, and loadfile-to-loadscope is 1.37x here against 1.38x there.

**`-n logical` is still not faster, and a first reading here said it was.** That reading compared
three logical runs against ONE `-n auto` run of 2:27 taken in a busier window. Alternated properly:
`-n auto` 2:09 / 2:10 against `-n logical` 2:13 / 2:13 / 2:14, so about 3% SLOWER. Lowering the
floor narrowed batch 109's gap without changing its sign. One sample on one side of a comparison is
not a measurement, and this one reached three files before the next five runs corrected it.

**The floor moved, and so does this entry's prescription.** Re-aggregating the same `--durations=0`
capture by loadscope's grouping rule -- the CLASS for a test in one, the MODULE for a test that is
not:

| Mode | The floor | Seconds | Share | Ceiling |
|---|---|---|---|---|
| loadfile | `tests/test_tui.py` | 124.5 | 32.4% | 3.09x |
| **loadscope** | **`tests/test_tui.py`'s 28 module-level tests** | **79.7** | **20.7%** | **4.83x** |

So the lever is no longer "split an 8,144-line file". It is the tests in `test_tui.py` that sit
outside any class, which loadscope must keep together because a classless test is grouped by
module. Gathering them into classes -- or moving them into a file of their own -- drops the floor
towards the next group down (`test_mcp_client.py`, 15.9s) and costs one tree entry rather than
four.

**HOW MANY OF THEM: 183 test functions, 243 collected node ids. Batch 110 wrote "28" here and it
was wrong by a factor of six and a half** -- corrected in batch 111 by re-running `tally110.py`
against the same `bench109-base.txt` capture and counting the distinct node ids in the
`tests/test_tui.py` module scope, rather than re-reading the sentence. The 79.7s and the 20.7%
are unaffected; they were aggregated by the script and always right. What changes is the PRICE of
the prescription: gathering 183 functions into classes is a 183-site edit in the suite's largest
file, not an afternoon's tidy, and whoever takes it should know that before starting. The same
correction shape as this entry's cause a batch earlier -- a number nobody re-derived, sitting
next to a measurement that was taken properly.

The upside may be larger than "15%" too, and that figure should also be treated as unmeasured.
It comes from 4.2x against a 4.83x ceiling; but the ceiling is `total / floor`, and with the
floor at the next group down the suite becomes worker-bound rather than floor-bound
(384.5s over 11 workers is 35s), so what is left is fixed overhead nobody has measured
separately. Somewhere between 15% and 35%, and the only way to know is to do it and run it.

**AND A SECOND HAZARD, found on the sixth run rather than by reading -- write this one down.**
Under loadscope a FILE is no longer one unit. A file holding module-level tests AND a class is two
groups, and a non-function-scoped fixture in it is set up twice on any worker that takes both.
`test_storage_e2e.py` was exactly that, and its second `real_storage` raised
`Table 'conversationthread' is already defined` -- the failure its own module docstring predicts
("there can be only one swap"). **Five `-n auto` runs were green and a `-n logical` run was not**,
so it is a scheduling lottery: the two groups landing on one worker is the accident, not the
failure. `TestConcurrentWriters` is flattened to module level and
`test_this_file_declares_no_test_class` pins it. `test_storage_e2e.py` was the only file at risk,
and the rule the next one inherits is: **a module- or session-scoped fixture's file must hold no
test class.**

**This sentence used to carry a count, and the count cited the grep that refutes it** (corrected
batch 151, S19-01). It read "`real_storage` is the suite's only non-function-scoped fixture
(`grep -rn 'scope="module"' tests/`)", and running that grep returns TWO -- `real_storage` and the
`_schema_cache_is_put_back` this very entry's batch 110 added beside it. It was one of four live
copies of that claim, in four different phrasings, which is why batch 128's sentence-grep found
none of them; the other three were `pytest.ini` twice and `tests/test_tui.py`. All four are now the
RULE without the number, and the rule is enforced for every file rather than described for one by
`test_every_file_with_a_module_scoped_fixture_declares_no_test_class`.

**Nothing remains open in this item.** The `test_config_edit.py` precondition this entry
prescribed was never the defect, and the mode decision it was blocking is taken.

**This paragraph said the 183 classless tests in `test_tui.py` remained open** (corrected batch
151, S19-01). Batch 114 is the batch that classed them, this entry's own header says so, and the
RESOLVED section immediately below says so again -- so a closed item's body named as its one piece
of open work the thing it was closed for. Found while confirming the count above, in the same
entry, which is the argument for reading a register entry whole rather than to the first claim that
answers the question.

---

### RESOLVED in batch 114

The 183 classless tests are 41 classes now, cut along the section banners the file already carried,
and `test_tui.py`'s module group is gone: **292 node ids in one indivisible unit before, zero
after.** Nothing moved and nothing was renamed -- the same 308 test names collect, the file still
reports 432 tests plus the one guard below, and the only change to a test is `self` and four columns
of indentation.

**THREE THINGS THIS ENTRY GOT WRONG.** Each was found by running something rather than re-reading
the entry, which is the same way batch 110 corrected its cause and batch 111 corrected its count.

**1. "Or moving them into a file of their own" is not a second route to the same place.** The
prescription above offers "gathering them into classes -- or moving them into a file of their own"
as alternatives. Only the first works. loadscope groups a classless test by its MODULE, so 183
classless tests in a new file are one group of exactly the same weight under a different name. The
sentence would have sent the next reader to do the mechanical half of the job for none of the
benefit, and it is the cheaper-looking of the two.

**2. The lever is not the count of functions.** This entry prices the work as "a 183-site edit" and
reads as though the floor falls with the count. It does not: the floor is the largest INDIVISIBLE
group, and a parametrised function's node ids share one group whatever class holds them.
`test_every_modal_keeps_its_decision_on_screen` is 3 sizes x 18 screens = **54 node ids, 33.5% of
the entire module group, in one function** that no class boundary can split. It is the floor now,
and it is why splitting finer was considered and rejected: `-n auto` is 11 workers on this box and
the serial suite is 572s, so the ideal worker share is ~52s and this group already sits under it.
Going below a bound that has stopped binding buys nothing.

**3. The 79.7s / 20.7% figures were stale, and re-measuring them needed a control.** Aggregated per
banner from a `--durations=0` serial run of the file, the module group read 349s and the whole file
456s against this entry's 79.7s and 127.8s. The file had not tripled: the BOX was **3.07x slow**,
established by re-timing `test_live_output.py` at 156.92s against the 51.18s batch 113 measured on
the same machine hours earlier. Corrected to that scale the module group is ~114s and the file
~148s, which is ordinary growth. The distribution is unaffected, because every banner was timed
inside the one window -- but no absolute second from that capture should be quoted.

**AND ONE THING IT NEVER SAID: CI GAINS NOTHING FROM THIS.** `-n auto` is 70% of the cores, so a
4-core runner takes 3 workers and its ideal share is ~190s -- already above the 114s floor, so the
floor was never what bound CI. The entire payoff is on developer machines, which is where the suite
runs many times per batch and where 11 workers made the floor the binding constraint. An entry about
suite speed that does not separate the two audiences invites the next reader to expect a green tick
to arrive sooner.

**WHAT IT BOUGHT: 1.61x, and the ranges do not overlap.** Measured the way this entry's own
prescription asks for -- alternated, three runs a side, so drift cancels instead of accumulating in
whichever side ran second -- with `AGENT_WORKSPACE` unset and the whole suite each time:

| | run 1 | run 2 | run 3 | mean |
|---|---|---|---|---|
| before | 2:17.9 | 2:07.9 | 2:28.6 | 138.1s |
| after | 1:10.7 | 1:40.6 | 1:26.3 | 85.9s |

The slowest run after is still faster than the fastest run before, which is what makes this a result
rather than two averages. Against a serial run of 8:41 taken the same evening, **6.1x where it was
3.8x**. This entry guessed "somewhere between 15% and 35%, and the only way to know is to do it and
run it"; it is **38%**, and the guess was right about the method.

**A FILE-LEVEL BEFORE/AFTER IS THE WRONG INSTRUMENT, and two control runs were needed to see it.**
Running `test_tui.py` ALONE, the pre-change file leaves ten workers idle once its twenty small
classes finish, so its 292 pilots run against almost no competition; the post-change file runs
eleven at once, and `pilot.pause()`'s own docstring measures a **46x** swing in how long it takes
with a busy sibling. Control runs of the file alone read 6:06 and 3:00 against post runs of 3:42 and
3:15 -- no signal, with 2x variance inside the control by itself. In the full suite the other
workers are never idle either way, which is why the table above is full-suite runs and why a
file-level number should not be quoted for this.

**THE SCATTER IS THE RISK, AND IT WAS CHECKED RATHER THAN ARGUED.** Classing 183 previously
serialised tests spreads them over workers, so one that depended on a predecessor goes red -- or
goes quietly vacuous, which is worse and is the reason `loadscope` was chosen over `load` in the
first place. Batch 110 asserted nothing in this file was an ordered pair; that was a reading. **All
62 classes were run standing completely alone** -- 62 processes, 251s, every one green. Two tests
did fail on the first parallel run of the file alone
(`test_quitting_by_gesture_releases_a_blocked_worker`,
`test_what_does_not_fit_the_payload_block_can_be_scrolled_to`); both pass alone, both passed on
re-run, both control runs were green, and six full-suite runs of the changed tree since -- three parallel, one serial, one on Linux and the final one -- have not reproduced either.
That is the contention lottery this suite has by construction -- `settle` polls against a 10s
deadline -- and not something the classing introduced.

**WHAT THE PARTITION ACTUALLY IS.** 40 banner lines in the file; 36 of them own module-level tests,
plus the region above the first one. Four of those sections have a module-level helper, a data
block or an existing `Test` class sitting BETWEEN their own tests -- `type_into_prompt`,
`REVIEW`/`ASKS`, `_syntax_token_theme` and `TestEventsUnderAnOpenModal` -- and no single class can
span one. Nothing was hoisted to make it span: the interloper is the section's own seam, so each of
those four yields two classes. 37 owners + 4 seams = **41 new classes**, and the file now holds 62
`Test` classes in all.

**Pinned** by `TestThisFileIsGroupedForLoadscope::test_every_test_here_is_inside_a_class`, which
parses the file with `ast` and is itself inside a class, because a module-level test there would be
the thing it forbids. Verified as a real kill rather than assumed: a stray module-level test makes
it name the stray and tell the reader that the section banner above it names the class to file it
under. It is the mirror of `test_storage_e2e.py::test_this_file_declares_no_test_class`, which
forbids the opposite thing in the file next door for the opposite reason.

**`tests/test_themes.py` is the larger remaining classless population, and it was left alone on a
measurement rather than on a guess.** 319 node ids and not one class -- MORE than test_tui.py's 292,
so a reader ranking by count would start there. It runs 38.5s on the 3.07x box, which is ~12.5s, far
under the floor this batch leaves behind, and classing it would lower nothing. `test_math_tools.py`
(123 classless) is 4.5s and `test_compaction.py` (68) is 6.5s. Recorded so the count does not send
the next reader at the wrong file.

## 31. A positive control measures interpreter startup as well as the thing it guards (closed, batch 114)

`tests/test_tool_budgets.py::TestAComputeToolIsBoundedByAClockThatCanStopIt::test_the_probe_would_notice_a_child_that_kept_going`
is the control beside the kill test: the test above it asserts that a file
STOPPED growing, which is what it would assert if the child had never
started, so this one runs the same handler and watches it keep going. It
spawns `python -m tools.isolation` as a subprocess, sleeps a FIXED 1.0s,
reads `st_size`, sleeps 1.0s again and asserts the size grew.

**Seen failing once, in batch 112's serial full run**, with
`AssertionError: assert 0 > 0`. Zero, not "did not grow": the child had not
produced its FIRST tick 2.0s after `Popen` returned, and the probe writes
every 0.05s once it is running. So what timed out was a cold interpreter
start plus the probe module's imports, on a box eight minutes into a
one-core run -- not the budget clock the test is about. The message it
prints ("an unkilled child did not keep growing the file, so the test above
is asserting nothing") describes a defect that was not the one present, and
the child's stderr is captured but never shown, so a real crash would read
identically.

**Not reproducible on demand.** The class ran green three times
consecutively (~14s each), the file green (31 passed), the parallel full
suite green, and a second serial full run green (6041 passed, 7:27). It is
a load-dependent flake, which is why it is written down rather than chased.

**The shape of the fix, when someone takes it:** poll for the first tick
under a generous deadline and measure growth from THERE, so the window the
test actually cares about starts when the child starts; and put
`proc.stderr` into the assertion message, so a child that died is
distinguishable from a child that was slow. Both are changes to the test,
not to `tools.isolation` -- there is no evidence of a defect in the thing
under test.

**Deliberately not fixed in batch 112** (owner decision, 2026-09-30). It
was found BY that batch's verification and has nothing to do with its
subject, and a test edit means re-running both full suites; carrying it as
a register entry keeps it owed without widening a batch that was planned
around the transcript.

### RESOLVED in batch 114

Folded into the batch that had to re-run both full suites anyway, which is
the only reason 112 deferred it.

The window starts when the CHILD starts now: the test polls for the first
tick under a 30s deadline and measures growth from there, so what it times
is the probe's 50ms interval rather than a cold `python -m tools.isolation`
start. And `proc.stderr` is in the failure message, read AFTER the kill --
`.read()` on a live pipe blocks until EOF, which is also why both failure
paths go through `pytest.fail` rather than an `assert` whose message would
be built while the child is still running.

Verified by driving the new path rather than by reading it: pointing the
request at a function the probe module does not have makes the test fail
with *"the child wrote no first tick in 2s ... that is a child which never
started, not a budget which failed to stop one"*, followed by the child's
own `AttributeError` traceback. The old code answered the same situation
with `assert 0 > 0` and a sentence about the budget clock.

**One thing measured and deliberately left.** The sibling above it,
`test_a_timed_out_call_is_STOPPED_not_merely_abandoned`, has the same
cold-start exposure at `assert settled > 0` -- but its window IS the 2.0s
budget under test, so it cannot poll past it, its message ("the child never
ran, so nothing was measured") is truthful about what happened, and the
budget is pinned by `assert "2s limit" in result["error"]` two lines up.
Raising it would move a number the test asserts on. Named here so the next
reader finds the exposure recorded rather than discovering it in a full
run.

## 32. A wall-clock ceiling set below the worst case of the thing it times (closed, batch 116)

**Found by CI, on a commit that changed only documentation.** The `tests`
job went red on `de4bcac` with:

```
FAILED tests/test_pilot_wait.py::TestItWaitsOnTime::test_a_passing_wait_costs_only_what_it_needs
AssertionError: paid for the timeout it did not need: 1.010s
```

**The test was wrong, not the suite.** A satisfied predicate costs exactly
one `pilot.pause()` -- `settle` checks the predicate, pauses once to
quiesce, returns -- and `settle`'s own docstring, fifty lines above the
implementation, measures a pause at **21ms idle and 1021ms with a busy
sibling, floored at 20ms and capped at 1000ms**. The ceiling was `< 1.0`,
below the cap of the thing it timed. It has been that way since the test
was written (`46772fd`), which is why this is not a batch 114 regression.

**AND THE FIRST FIX WAS WRONG IN THE SAME WAY, which is why this entry was
rewritten.** Batch 116 replaced 1.0 with a flat 2.0 and called it "one
full capped pause plus margin". `Pilot.pause()` is
`await self._wait_for_screen()` -- **timeout 30.0s**, raising
`WaitForScreenTimeout` -- followed by `wait_for_idle(0)`, so 1000ms is the
IDLE HALF ONLY and one pause is bounded at ~31s. 2.0 fixed the observed
failure and was not a derived bound. The ceilings are relative now:
each test times one bare `pilot.pause()` and takes the cap from
`inspect.signature(wait_for_idle)` rather than restating a number this
repo has now got wrong twice.

**The mechanism is simpler than the figure that was cited, too.** CI's
1.010s is not the 1021ms busy-sibling measurement: `wait_for_idle` checks
`elapsed_time >= max_sleep` only AFTER sleeping one 20ms
`SLEEP_GRANULARITY` tick, so reaching the 1.0s cap costs 1.0s plus part of
a tick. That is checkable from six lines of textual's source, where the
46x figure needed a load test.

**What batch 114 did do is raise the odds.** Classing `test_tui.py` turned
one serialised group into 41 schedulable ones, so on a 3-worker runner TUI
work now overlaps where it used to queue. The defect was always reachable;
114 made it likely enough to see. That is also the honest limit of batch
114's "every class green standing alone" check: standing alone proves no
test depended on a PREDECESSOR, which is the opposite of proving that
tests which never ran at the same time are safe to run at the same time.

**The grep found a worse one that had never failed.** AGENTS.md's "grep
for every other call site sharing the root cause" turned up
`test_it_stays_a_count_and_not_a_deadline`: `pump(pilot, 3)` is THREE
pauses, worst case ~3.0s, against a 2.0s ceiling. More exposed than the
one that fell over, and silent.

| test | operation | original | batch 116's first try | now |
|---|---|---|---|---|
| `test_a_passing_wait_costs_only_what_it_needs` | 1 pause | **1.0** | 2.0 | `one_pause + PAUSE_CAP + 0.5` |
| `test_it_stays_a_count_and_not_a_deadline` | 3 pauses | **2.0** | 4.0 | `one_pause + 3*PAUSE_CAP + 0.5` |

`PAUSE_CAP` is `wait_for_idle`'s `max_sleep` default, read from the
signature. The additive slack is for the swing BETWEEN two consecutive
pauses -- 21ms against 1021ms -- which is the one thing a calibration
cannot predict. Note what a flat `one_pause + 1.0` would have been: an
idle calibration against a capped measured pause gives 1.021s, THINNER
than the 2.0 it replaced.

**A third site was checked and left.** `test_tui.py:1263`
(`test_effort_lookup_does_not_block_the_ui_thread`) budgets 3.0s for one
`pilot.press`, which is 3x one capped pause, and its sibling thread blocks
on an `Event` rather than burning CPU. It does not share the defect.

**Neither ceiling was widened into uselessness, and that was driven rather
than argued.** Each was mutated against the regression its own message
names: `settle` made to pay its timeout reads 30.004s against the new 2.0,
and `pump` given a deadline reads ~10s against the new 4.0. Both go red.

**The rule this leaves, in the form the second attempt earned.** A
wall-clock upper bound should not restate a constant it does not own.
Prose beside a helper is a snapshot of one measurement, and this entry is
the proof: the 1000ms figure was accurate about `wait_for_idle` and was
read twice as though it described `Pilot.pause`. Measure the operation in
the test, read the library's bound from the library, and keep the written
figure for explaining WHY rather than for arithmetic.

**Two sites knowingly left**, both passing, neither implicated, and both
carrying the same incomplete model: `test_pilot_wait.py:133` (a 0.5s
deadline plus one pause, ceiling 2.0) and `test_tui.py:1263` (one
`pilot.press`, ceiling 3.0). Named here so the next reader finds them
written down rather than in a run.

## 33. `_parse_atom_feed` trusts the structure of a feed it calls hostile (closed, batch 117)

**Found by an independent review of `077cf1b..HEAD`; the lines predate the
epic (`7cf0759`, 2026-07-24).** `tools/builtin/arxiv.py` dereferenced
`entry.find(...).text` with no guard, on a feed this module's own header
calls attacker-influenced -- and which batch 116 had just taught to refuse
hostile ENTITIES, one function away.

**The review reported three failure modes. Measured by calling the
function, there are six, and the two it cited as GUARDED EXAMPLES are both
exposed:**

| case | raises |
|---|---|
| no `<id>` | `AttributeError` |
| no `<title>` | `AttributeError` |
| author with no `<name>` | `AttributeError` |
| **no `<summary>`** | `AttributeError` -- the `or ""` guards `.text`, not a missing ELEMENT |
| **empty `<published/>`** | `TypeError: not subscriptable` -- the `is not None` guards the element, not empty text |
| **empty `<title/>`** | `AttributeError` on `.strip()`, a different mode from a missing title |

Two shapes wearing three exception types, which is why one helper (`_text_of`)
replaced six inline spellings rather than three guards being added.

**None of them reached `run`'s handlers.** It catches
`DefusedXmlException` and `(httpx.HTTPError, ET.ParseError)`;
`AttributeError` and `TypeError` are neither, so all six escaped to
`dispatch()`'s generic wrapper and failed the whole search over one
malformed entry.

**An entry with no id or title is now skipped, not raised** -- those two are
what make it citable -- with one WARNING naming its INDEX, because the id is
what might be missing. A missing summary, an empty published date and a
nameless author are kept; the author is dropped from the list rather than
inserted as `None`, which would render as an author called None.

Driven both ways: a five-entry page whose third entry has no title returns
four results, and reverting the guard turns nine of the ten new tests red.

## 34. `_DANGEROUS_FLAGS` guarded nothing it claimed to (closed, batch 117)

`security/sandbox.py`'s `classify_command` reports `writes=False` for an
inert command and says the table "guards the entries that have a writing
mode". Measured against the shipped `INERT_COMMANDS`, three things were
wrong, and the one the review found was the least of them.

**The table was entirely dead.** Its three keys were `find`, `sort` and
`diff`. `find` and `sort` are not on the inert list, and `diff`'s value was
an empty `frozenset()` -- `if dangerous:` is falsy for one, so the check was
skipped for the only key that was reachable. The review called the `=`-form
gap "currently unreachable" because "find/sort were removed from
INERT_COMMANDS, so today no inert command consults the table"; that reason
is wrong -- `diff` is inert and IS tabled -- and the true reason is the
empty set.

**Two inert commands write, and had no entry at all.** `tree -o FILE` and
`date -s TIME`. Both added.

**And one cannot be expressed by a flag table at any point.** GNU
`uniq IN OUT` writes to its SECOND POSITIONAL argument. `uniq` was removed
from `config.INERT_COMMANDS` instead, because teaching the classifier which
argument positions are outputs is precisely the shell-parsing
`_escapes_workspace` argues it must never do (G2). The alternative --
leaving an entry that cannot fire -- is how `diff`'s empty set came to read
as coverage.

**The `=` spelling is now caught** by comparing a token's head
(`token.split("=", 1)[0]`) as well as the whole token, so
`sort --output=/tmp/x` and `sort -o=out.txt` no longer pass.

**WHAT THIS WAS NOT, and the entry should be read with it.**
`_escapes_workspace` reads every token after the first as a path, so a write
aimed outside the workspace was refused throughout -- measured:
`uniq in.txt /etc/passwd` is caught. What the table decides is whether an
IN-workspace write is reported as `writes=False`, and the workspace is
writable by design. So this is the profile telling the truth, not an escape,
and it is filed as such.

## 35. The response cache was the one thing the registry promised and did not have (closed, batch 117)

`tools/builtin/_net_common.py`'s `TTLCache` had no lock. The comment beside
`spawn_subagent`'s `parallel=True` -- the only tool that declares it --
claims "everything the child's run touches was made concurrency-safe first:
the budget's lock, the serialised question, the per-future authorization".
The child reaches this cache through `arxiv_search` and `fetch_url`, and it
was the item on that list without one.

A corrupt dict was never the risk: single dict operations are atomic under
CPython. `set` is a read-modify-write -- pop, assign, sweep, then evict
while over the cap -- so two children storing at once can lose an entry and
evict in an order neither asked for. `GrantBudget.take()` is the pattern it
should have followed from the start, and now does.

`_sweep` is called from `set` with the lock held and must never take it; the
lock is a plain `Lock`, so re-entering would deadlock the writer. Pinned by
a test that simply makes a write with something expired to sweep and asserts
it returns at all.

## 36. The SSH `known_hosts` file was removed only by a test helper (closed, batch 117)

`security/sandbox.py`'s `_write_known_hosts` mkstemps a file and stores the
path; the only `os.unlink` was in `_reset_ssh_probe`, which no production
code calls -- `tests/conftest.py` and `tests/test_ssh_backend.py` are its
only callers. One file per process rather than per call, since the path is
reused once set, so it never grew unbounded; but every SSH-using run left a
`venastine_known_hosts_*` in TMPDIR.

Registered with `atexit` now, per path, swallowing every error: the pinned
host key is public so this is hygiene rather than exposure, and a cleanup
that raises during interpreter shutdown turns a tidy-up into a traceback on
the way out.

## 37. A stored secret could contain the newline that ends the sudo line (closed, batch 117)

`security/secrets.py`'s `put()` validated a non-blank name and
`MIN_SECRET_LENGTH`, and nothing else. `sandbox._sudo_password_line` is
`secret.reveal() + b"\n"` and `sudo -S` reads exactly one line, so a value
containing a line break delivered the text BEFORE it and silently dropped
the rest.

It failed closed -- the wrong password, and `_SUDO_STDIN_GUARD` keeps the
remainder off the command line, so there was no injection -- but it failed
*opaquely*: the entry is quarantined for the run by
`_note_sudo_auth_failure` and nothing says why. `put()` rejects `\n` and
`\r` with the constraint named, which is the one place it can still be
explained to whoever is typing. Other whitespace is untouched: a passphrase
with spaces or a tab survives one line and must keep working.

No stored secret had to change: all 41 `put()` call sites in the suite use
single-line constants.

## 38. The live tok/s figure read `~0` on the turns it exists for (closed, batch 118)

**Owner-reported from a running session**, with three candidate causes
offered. Two were wrong and the third was right but incomplete, and the
arithmetic is what sorted them out.

**`~0` means "fewer than two characters per active second".** The figure is
`round((chars / CHARS_PER_TOKEN) / active)` with `CHARS_PER_TOKEN = 4`, so
anything under 0.5 tok/s prints as 0. And `rate()` returns None at exactly
zero characters -- it prints nothing rather than a zero -- so **seeing `~0`
proves some text arrived.** That rules out the empty cases immediately and
points at a turn that produced a little text over a long time.

**A: "providers that do not expose tok/s" -- ruled out.** The live figure
never consults provider usage; it divides streamed characters by a constant,
which is what the tilde admits. The completion line does use the provider's
count, and where there is none it prints the time and makes NO token claim
rather than a zero -- pinned by `test_a_zero_count_is_treated_as_no_count`.
Neither instrument can show a zero because a provider was silent.

**B: "approval waits are counted in the turn" -- ruled out, and fixed in
batch 61.** `elapsed()` subtracts blocked time and `rate()` divides by it.
Two routes drive it: the tick reads `len(screen_stack) > 1`, so every modal
counts including ones added later, and the permission path calls
`set_blocked` directly because `tui.animations: false` leaves no tick. Every
human wait funnels through one `_permission_channel`. Pinned by
`test_the_rate_ignores_time_spent_waiting_on_a_human`.

**C: "thinking tokens are not counted" -- CONFIRMED.** `core/loop.py` yields
`LoopEvent(token_delta=...)` and `LoopEvent(thinking_delta=...)` as separate
events, `add_output_chars` had exactly one call site, and it was
`token_delta`. Thinking is billed as output by the providers that produce it,
so the completion line's exact figure already included it while the live
estimate did not -- the two instruments disagreed about their own numerator.

**And two causes the report did not have, the first of them probably the
largest.**

* **Tool execution.** `start`/`stop` bracket the whole turn and the only
  pause was for a human, so a two-minute shell command or fetch was active
  time with zero generation.
* **Tool-call arguments.** `StreamToken` carries `text_delta`,
  `thinking_delta` or `final_response`, so tool-call JSON never streams as a
  delta -- generating a large call added denominator and no numerator.

**The structural fault, stated once: the numerator was a subset of
generation and the denominator was nearly all wall clock.** `tui/meters.py`'s
own docstring says it exists because "the screen is most static exactly when
the harness is busiest", and thinking plus tool execution is what busiest
means here. The feature missed its own primary case.

**Fixed on both sides, because fixing one would have left it wrong.**
Thinking and tool-call arguments join the numerator; tool execution leaves
the denominator via `set_tool_running`, mirroring `set_blocked`. `elapsed`
is untouched -- a two-minute tool IS part of how long the turn took and the
transcript line says `took` -- so there are two clocks now and the module
says which question each answers.

**Two things that would have been bugs, and are tested as such.**

* **Depth, not a boolean.** `_dispatch_parallel` runs a response's tool
  calls together, so several `tool_call_start` events can be outstanding
  before the first result. A boolean un-pauses on the first one.
* **The two pauses must not overlap.** Approval is asked for from inside a
  tool call, so a human wait sits inside a tool window; `elapsed` already
  removes the human wait, and subtracting the tool window across it would
  take the same seconds twice and could drive the denominator negative. The
  tool clock closes when a modal opens and reopens when it closes.

The reported case, as arithmetic: 100 characters over 60 seconds of which 50
were a tool read `round(25/60) == 0` and now reads 2.

## 39. A turn COUNT could hold a thread of any size, with no way out (closed, batch 119)

**Owner-reported from a running session, and reproduced against that
session's own database** -- `C:\Software\...\app.db`, thread
`b652eb71-9a79-48b5-a8e8-8cc6767d770a`, with the two blocked attempts in
`logs/app.log` at 23:09:15 and 23:15:39 on 2026-10-01. The report: a 262k
window on `OPENROUTER/qwen/qwen3.8-27b:free`, the thread crossed 230k, and
both automatic and manual compaction answered *"Context is full but the most
recent turns fill it entirely"* -- through a switch to a 1M-window model and
through a restart. No way to compact the thread and no way to continue it.

**THE THREAD, MEASURED.** 182 rows, 728,953 chars, and exactly **three**
user turns:

| turn | rows | chars | proxy tokens |
|---|---|---|---|
| 0 | 0..3 (4) | 1,013 | 253 |
| 1 | 4..145 (**142**) | 673,246 | **168,311** |
| 2 | 146..181 (36) | 54,694 | 13,673 |

`compactable_span()` opened with `cutoff = len(starts) - keep_recent_turns`
= `3 - 3 = 0`, then `if cutoff < 1: return None`. **The token floor on its
own would have folded everything before turn 2: 168,564 proxy tokens, 92.5%
of the thread**, leaving the last completed turn and the current one
verbatim. A good fold existed and a turn count vetoed it.

**WHY NOTHING THE OWNER TRIED COULD HAVE WORKED, which is the part worth
keeping.** `compactable_span(memory, current_turn_start, overrides)` takes
no model, no provider and no window -- so a 1M-context model recomputes the
identical `3 - 3 = 0`. A restart re-reads the same archive. `/compact`
accepts only `--strength`, so no invocation can reach the floors, and
`keep_recent_turns` is settings-only. The state was genuinely terminal from
inside the app, and the owner's observation that it survived a restart was a
correct reading of a structural fact rather than a cache bug.

**AND THE ONE ESCAPE THAT LOOKS LIKE ONE IS A TRAP.** `keep_recent_turns: 2`
in settings.json yields `cutoff = 1`, which folds turn 0's **253 tokens**,
writes a checkpoint, and then answers `no-progress` forever, because
`advances()` is satisfied against a watermark that freed 0.1%. A successful
compaction that cannot be repeated.

**THE FIX IS AN AMENDMENT TO M5, not a correction of it.** M5 names three
floors and says the earliest wins; only two of them can measure what they
hold. The current turn is **structural** -- folding it rewrites the question
mid-answer. `keep_recent_tokens` is a **size**, so it holds exactly what it
says and can never be what holds a thread that is over the trigger.
`keep_recent_turns` is a **count**, and a count cannot tell three small
turns from three enormous ones. So the count applies while it still leaves
something foldable and otherwise yields -- all the way to the size-aware
floors, not one turn at a time, for the no-progress reason above. The
guarantee becomes size-based: the current turn verbatim, plus at least
`keep_recent_tokens` of completed turns verbatim.

**Two things that lied, fixed in the same batch because they are why the
report named the wrong cause.** The notice said *"Context is full"* where
the trigger is a fraction of the window and 32k was unspent; and it blamed
SIZE where a count was responsible, so its only diagnostic pointed away from
itself. The TUI usage line read `ctx 230k/223k` -- the denominator was the
TRIGGER and the window appeared nowhere outside `/window`, which is what led
the owner to conclude the harness treats the two as one number. The logic
always distinguished them; nothing on screen did.

**Still blocked afterwards, deliberately:** a thread whose only turn is the
one still running. The boundary must be a turn start (M4), so a 142-row
single turn has no legal fold. That is item 40.

## 40. One turn can be the whole thread, and a turn cannot be folded (closed, batch 120)

The residue of item 39, and the owner's own worst case: if the trigger fires
during turn 1 of the thread above, `starts` is `[0, 1]`, the only legal
boundary folds turn 0's 253 tokens, and every later evaluation answers
`no-progress`. 142 rows and 168k tokens inside one turn, unfoldable, because
M4 requires the fold boundary to be a turn start and M5 forbids folding the
current turn at all.

Both rules have reasons that survive. M4's is a wire error: a `tool_result`
with no preceding `tool_use` is an HTTP 400 on Anthropic. M5's is that
summarizing what the model is reasoning about right now rewrites the
question mid-answer.

What makes a fix possible is that M5's reason does not cover the whole turn.
A 142-step turn is not reasoning about step 3's tool output, and the thing
that must survive verbatim is the turn's own **user message** plus the
recent steps. A boundary immediately before an `assistant` row satisfies M4
by construction -- D20 persists the assistant row carrying its `tool_use`
before dispatch and the answering rows after, so every earlier call is
answered there -- and `pinned_through()` is the precedent for re-adding a
row the watermark swallowed. Owner-approved for batch 120.

**CLOSED, and the shape is the one sketched above.** `_sub_turn_span()`
runs only when no turn-aligned boundary exists -- a turn-aligned fold needs
no row re-added and no amendment to M5, so it stays preferred and every
thread that folded before batch 120 folds in exactly the same place. The
boundary is an **assistant row**, and M4 holds there by construction rather
than by checking: inside a turn the archive is `user, (assistant, tool+)*`,
because D20 persists the assistant row before dispatch and every answering
row after it returns. The turn's request comes back through
`pinned_through()`, which already owned "what must be re-added from inside
the covered span, with the span made whole"; it is **one index added to
that function's existing `keep` list**, so the existing positional sort
places the request correctly among any pinned rows -- a sibling function
would have had to choose an order, and either choice is wrong half the time
(a pinned row from an older turn belongs before the request, one from
inside this turn belongs after it).

**Measured on the reported thread**, in the state batch 119 cannot reach --
the trigger firing during turn 1, `starts == [0, 1]`:

| | folds | share of the thread |
|---|---|---|
| batch 119 alone | 253 proxy tokens | 0.1% |
| with sub-turn folding | 163,910 proxy tokens | **97.2%** |

M4 checked rather than asserted, by walking both sides of the cut: the
folded span holds 89 calls and 89 results, the kept tail 6 and 6, with
nothing unanswered and nothing orphaned on either side.

**No schema change, and no checkpoint written before this lies about its
shape.** Whether a watermark is mid-turn is derived from the rows -- the
first KEPT row is an `assistant` row rather than a `user` row -- so a
pre-batch-120 checkpoint, which is always turn-aligned, re-adds nothing and
renders exactly as it did.

### The scenario this entry measures was the one the fix did not admit (batch 134)

Corrected rather than rewritten, because the entry is evidence and getting it
wrong twice in the same place is the thing worth recording. The case tabled
above -- *"`starts` is `[0, 1]`, the only legal boundary folds turn 0's 253
tokens, and every later evaluation answers `no-progress`"* -- is a thread with
TWO user rows, and batch 120 gated its fix behind `cutoff < 1`.
`completed_turns()` is `len(turn_start_ids) - 1`, so it is at least 1 for any
thread with a second user row: on the very shape this entry describes, the
sub-turn path was unreachable and the boundary was the useless turn-aligned
one. The 97.2% tabled here was never reached by a two-turn thread.

What batch 120 did fix is the narrower claim `AGENTS.md` states -- *"a thread
whose only turn is the one still running then has no legal fold at all"* --
and the two were allowed to disagree for fourteen batches. Measured in 134,
one 40-step turn: alone it folds 90.0% through the sub-turn path; behind a
single short exchange the identical steps fold **0.0%**, and then answer
`no-progress` forever at `logger.debug` with `kind=None`, so no notice is
appended and nothing appears on screen. A sweep of every legal
`keep_recent_tokens` x `keep_recent_turns` pair reached a sub-turn boundary 0
times in 30 accepted settings.

It also reinstated the trap item 39 names and says it avoided -- *"A
successful compaction that cannot be repeated"* -- on the DEFAULT
configuration with no settings edit at all.

Batch 134's gate is "no turn-aligned fold WORTH A MODEL CALL" rather than "no
turn-aligned fold": the turn-aligned boundary is kept when it frees at least
`keep_recent_tokens`, which is the tail the fold exists to make room behind,
and otherwise the sub-turn path runs exactly as it does for a single-turn
thread. That branch is untouched, so everything this entry closed stays
closed.

## 41. `arxiv_search` output is unbounded, and one result was 29k tokens (closed, batch 126)

Found while measuring item 39's per-step growth. `max_read_chars` (50,000
chars = 12,500 proxy tokens) bounds reads and sandboxed stdout, and the
thread's file reads respect it -- the largest is 31,940 chars. The largest
tool row in the thread is **116,996 chars, ~29,249 proxy tokens**, and it is
an `arxiv_search` result: `{'results': [{'arxiv_id': ...`.

That single step added **31,733 proxy tokens** to the prompt, which is what
the compaction headroom has to absorb and is the measurement that set
`compaction_min_headroom_tokens`. It is also larger than the headroom a bare
0.90 fraction leaves under a 262k window, which is why the fraction is
clamped rather than bare (see `compaction.headroom_for`).

**THE MEASUREMENT ABOVE IS SOUND AND THE DIAGNOSIS BELOW IT WAS WRONG IN
FOUR WAYS.** It read: *"No cap, no truncation notice, no paging. The fix is
presumably the shape `max_read_chars` already has, applied where the results
are assembled."* Three of those four claims were already false on the day
they were written. This entry dates to batch 119/120; §51 shipped in batch
**105**, fourteen batches earlier:

| The entry said | Already true since batch 105 |
|---|---|
| no paging | `start`, `total_results`, and `"Use start=N to see more"` (PG3, PG9) |
| no truncation notice | `summary_truncated` + `full_summary_url` (PG8, PG4) |
| no cap | `MAX_SUMMARY_CHARS = 600` caps the one field anyone would expect to be large |

It described the **pre-§51 tool**. And the fix it proposed was the wrong
*shape*, not merely aimed at the wrong place: a character cap over the
assembled result cuts a structured dict mid-structure, which is PG5's
spliced-document failure, and PG8 had already settled that **per-item** caps
with a named recovery route are the answer for this tool.

### What was actually unbounded, measured in batch 126

**The field it never named was `authors`.** Everything the tool already
bounds -- summary 600, `published` 10, the ids and URLs, the dict's own keys
-- ceilings near **16,900 characters** at the schema's maximum
`max_results=20`, against the 116,996-char row recorded above. The capped
fields could not have accounted for it, which is a thing the entry could
have derived from the constants without a network call.

Re-measured live against `all:LHCb observation branching fraction` at
`max_results=20`:

| | before | after |
|---|---|---|
| `len(str(result))` | **223,191** | **24,004** (-89.2%) |
| characters in `authors` alone | 204,871 (**91.8%** of the row) | -- |
| highest author count in one entry | **2,832** (`1411.4413v2`) | 25 kept, real total reported |
| entries over the cap, of twenty | 11 | 11, each flagged |

So the register's own figure was **about half** of what an ordinary physics
query produces today. Hyperauthorship is routine on arXiv, and it does not
arrive the way it was first looked for: the most-authored paper
(`1503.07589`) returns **two** `<author>` elements, because arXiv lists it
under its collaborations, while LHCb-style entries list every individual.

**A second defect the entry did not see at all.** `_call_arxiv_api` was
`httpx.get(...)` then `return response.text`, which is the exact shape
`fetch_url.py`'s own comment calls out as what ROADMAP_v2 §31 (H7)/#55
exists to prevent -- httpx buffers the entire body before anything slices
it, and a Content-Length check is not the fix because a hostile server can
omit it or lie about it. It was the **last `httpx.get` in production code**.

### The fix

`tools/builtin/arxiv.py` only. `MAX_FEED_BYTES = 2_097_152` with the read
streamed and refused past the cap -- derived from the heaviest *legitimate*
feed measured, 838,560 bytes, so the bound clears it 2.5x, because a cap
under the largest real feed is a false refusal. The refusal is **not
retried**, for the reason already written beside it for a declared entity.
`_MAX_AUTHORS = 25` (`scholar.py`'s number for the same question) with
PG8's full shape: `authors_truncated`, `author_count` carrying the real
total, and `full_authors_url`. `MAX_TITLE_CHARS = 300` and
`MAX_CATEGORIES = 20` get a flag and **no** recovery route, which is a
decision rather than an omission -- measured, the longest of sixty real
titles was 188, so a title past 300 is a malformed feed and pointing the
model at its abs page answers a question nobody asked.

### Not fixed here, and recorded rather than widened into

- `safety/policy_enforcement.py`'s `_walk` bounds DEPTH (`_MAX_SCAN_DEPTH`,
  failing closed) and not BREADTH, so a 2,832-author list was 2,832 leaf
  redaction passes per call. Bounding at the producer removes this instance;
  whether the walk itself should bound breadth is a separate question about
  a shared safety path, and is item 46.
- `categories` keeps a `None` for a `<category>` with no `term`, where
  `authors` drops a nameless entry with a comment saying why `None` in a
  rendered list reads as a value. Pre-existing and left alone; item 47.

## 42. A small window makes `effective_compaction()` raise (closed, batch 127)

Pre-existing, and found while tabling batch 119's trigger against small
windows. `warning_margin_tokens` is 8,000 and the loader refuses a pair
where it is not strictly below `trigger_tokens` -- correctly, since
otherwise the warning arrives after the compaction it warns about. But the
trigger is now derived from the window, so any window small enough to put
the derived trigger at or below 8,000 turns every `thresholds()` call into a
`ValueError`: `should_compact()` runs at the top of every step.

True before batch 119 as well (a 8,192-token window derived 6,963 against
the same 8,000) and not made materially worse by it (6,144).

### The diagnosis was right and two of its details were wrong (batch 127)

The defect is real, reachable and worse than the entry implies. Both
`_maybe_compact` call sites are bare `yield from` (`core/loop.py:1571`,
`:1828`), so this is not a mistimed fold: **every turn on such a model dies
at the turn boundary before the first model call.** The harness is unusable
there, which the entry's "turns every `thresholds()` call into a
`ValueError`" states without drawing the conclusion.

**The boundary is `w <= 10,666`, not "~9.4k".** Bisected against the code.
`headroom_for`'s cap holds the trigger at `0.75*window` at the small end, so
the raw derivation clears 8,000 only from 10,667 up. The heading carried the
wrong number; it now carries none.

**The route was the one route that was covered.** The entry says *"reachable
by anyone who types `/window 8k`"*. `/window` **refuses** anything at or
below `compaction_pipeline_backstop_tokens` = 20,000 (`tui/app.py:5561`),
which is stricter than 10,666 -- so that path was guarded, by a check
written for a different reason. Of `context_limit`'s four sources,
`MODEL_CONTEXT_WINDOWS`' smallest entry is 200,000 and
`DEFAULT_CONTEXT_WINDOW` is 256,000. **Source 1 -- what the provider SAID --
has no guard at all,** and 8,192 is the commonest real window a self-hosted
or gateway endpoint reports. The eager validation existed on the command a
human types (`/trigger`, `tui/app.py:5635`, whose comment describes this
exact failure) and was absent on the value a server asserts.

**And the test that covered this region checked the other floor.**
`test_the_floor_cannot_eat_a_small_window_whole` iterates 8,192 -- the
failing window -- and asserted `trigger > 0` while its docstring named
`effective_compaction()` raising *"or one under keep_recent_tokens"*. Of the
two dependent floors, it named the one that **never bites first**: the cap
clears `keep_recent_tokens` (4,000) from a window of 5,334 up and
`warning_margin_tokens` (8,000) only from 10,667 up. One field checked where
two had the same shape, which is item 41's error in a different file.

### The fix

`derived_trigger` floors above `max(warning_margin_tokens,
keep_recent_tokens) + 1`, **read** through `effective_compaction()` -- the
same resolution the validator reads, so a literal cannot drift from the
number it exists to satisfy. Both validators are covered, including the one
shielded today, because the defect is the class and not the instance.

The margin-scaling the entry proposed was **not** taken: it changes the
meaning of a setting a human may have written an absolute into, and the
floor answers the defect without that. Containment at the `_maybe_compact`
sites was rejected outright -- compaction would then silently never run on
that model, which is the quiet-failure class this project keeps closing.

Below ~10,667 the returned value is the floor rather than `0.75*window`, so
the fraction stops describing it; that is stated in `derived_trigger`'s
docstring rather than left to be discovered. Cost: one extra
`effective_compaction()` merge on `should_compact()`'s path, measured at
**3.267us** against `thresholds()`'s 4.918us -- 66% of it in relative terms,
**0.065ms per step** of a research run in absolute ones, against **4.696ms**
for one ordinary tool result's redaction pass.

## 43. The npm launcher reads stdin on first run, then waits with the console inherited (open)

`bin/venastine.mjs` opens a Node `readline` on `process.stdin` for the
first-run install consent (`ask()`, line 194), then runs the harness under
`spawnSync` with `stdio: 'inherit'` and stays alive for the child's whole
session. That is batch 121's shape exactly, one process out: two readers
on one terminal, with the second one being a Textual app.

PROBABLY BENIGN, which is why it is recorded rather than changed.
`rl.close()` calls `rl.pause()`, which calls `input.pause()`, so libuv is
asked to stop reading before the spawn. The residual risk is that on
Windows libuv's console reader runs on its own thread with a blocking
read, and `uv_read_stop` cannot cancel one already in flight -- so the
guarantee is "asked to stop", not "stopped", and it is the same guarantee
batch 121 declined to rely on for the Python side.

Only reachable on the FIRST run after an install, which is also the launch
least likely to be diagnosed as this. If it is ever fixed, the fix is
`process.stdin.pause()` plus `unref()` after `rl.close()`, and the
measurement is batch 121's: inject keys with `WriteConsoleInputW` and
count what the app's `on_key` sees.

**Rechecked in the review marathon (batch 138), as a seam rather than an
owned finding.** This is the THIRD stdin reader on one terminal, beside
`main.py`'s `_StdinReader` and Textual's own console reader -- which is the
frame that makes it worth keeping open even though it is "probably benign".
The assessment above was deliberately not re-measured: the entry itself
prescribes the measurement, and no Python module can fix a Node reader, so a
reviewer of the harness's own shells can name this and nothing more. Item 49
is the fourth instance of the same one-reader-at-a-time property (§29 N1), on
the sandbox's one-shot executors.

## 44. `ask()`'s drain can only drop what the pump already took (open)

`ask()` drops stale input before each gated prompt, because "a line typed
after a question timed out was an answer to THAT question". It does that
by draining the QUEUE -- so a line still sitting in the terminal's own
buffer when the pump starts is not stale to it, and can answer the next
question.

PRE-EXISTING and unchanged by batch 121, which moved `readline` off the
pump and left `ask` alone. The race was always there at the FIRST `ask()`
of a process: `_start()` returns before the pump has read anything, and
the drain that follows sees an empty queue either way.

The honest fix is an OS-level flush -- `termios.tcflush(fd, TCIFLUSH)` and
`FlushConsoleInputBuffer` -- which would also make the drain
non-racy rather than best-effort. That is a change to a locked §29 safety
property and wants its own batch, with the measurement being a line typed
before the prompt is printed rather than after.

**Rechecked in the review marathon (batch 138).** Still true exactly as
written, re-read at `main.py:706-710`: the drain drops only what the pump
thread has already taken from the terminal, so a line sitting in the
terminal's own buffer when `_start()` fires can answer the next gated
question. Batch 121 (inline `readline`, `_release_terminal`) is still out of
scope for this entry by its own statement, and the honest-fix direction above
is untouched. Nothing to add -- and item 49 now cites this entry for the half
it explains: a one-shot child reading the terminal directly is consuming input
the drain was never able to reach.

## 45. A view policy module in `core/` that only one shell reads (open)

Found while building ROADMAP_v3 §55 (TR14), and PRE-EXISTING: it is §49's,
not this batch's.

`core/session_view.py` is in `core/` for an explicit reason, stated in its
own docstring and inherited from `core/replay.py`: *"D12 makes the CLI a
permanent fallback, so what may be shown for a session is a policy decision
that must not differ between the two shells. Each shell decides how to
paint the entries; this decides what they are."*

**`main.py` has never imported it.** There is no CLI route to a session
view at all -- not for a live session, not for SS18's rebuilt one -- so the
placement currently buys the ARGUMENT rather than the behaviour. Batch 125
added `core/call_view.py` beside it on the same terms, with the same gap.

RECORDED RATHER THAN FIXED, and the reason is scope rather than
indifference. `core/replay.py` genuinely has two readers, which is what
makes the convention real; closing this one means designing what a CLI
session or call view IS -- a slash command, a `--show-call` flag, something
printed at the end of a turn -- and that is a decision about the CLI's
surface rather than a wiring job. Doing it inside §55 would have made that
section partly a §49 repair.

The honest statement of the current state, for anyone reading the
convention and believing it: `ViewEntry` is a shell-independent shape, the
policy is in one place and is correct, and exactly one shell asks for it.
A second shell can be added without moving anything, which is the whole
value the placement was bought for.

**Rechecked in the review marathon (batch 138) by two scopes independently**,
which is itself the signal: the compaction/sessions reviewer and the headless
shell reviewer arrived here from opposite ends. Confirmed still true --
`grep -n "session_view\|call_view" main.py` returns nothing, so there is still
no CLI route to a session view, live or rebuilt. Two things to add:

- The sentence above about `core/call_view.py` sitting beside it "on the same
  terms, with the same gap" is now load-bearing in the other direction. §55
  TR9 made `session_view.output_lines` **public** specifically so
  `core/call_view.py` could share it rather than re-implement the
  one-entry-per-line rule. So the two modules have a real shared dependency,
  which makes the convention slightly more than an argument even with one
  shell: a second reader of the policy exists, it is just not a second shell.
  `core/call_view.py:30-37`'s own docstring records the same gap from its side
  ("ONLY THE TUI PAINTS THEM TODAY").
- `core/session_view.stored_entries` is reached only from `tui/app.py:2206`
  and `live_entries` only from `:2318`, both keyed by the model's call id --
  which `core/replay.py` already puts on the tool line for either shell. So
  the CLI side needs no new data, only a surface. That narrows "designing what
  a CLI session view IS" to the question of the command, which is what this
  entry says the real cost is.

## 46. `check_output_policy`'s walk bounds depth and not breadth (closed, batch 127 -- breadth is the wrong axis)

Found while closing item 41, and PRE-EXISTING. `_walk` descends into lists
and dicts with `_MAX_SCAN_DEPTH = 12`, which fails CLOSED on depth -- a
deliberate decision with a test behind it. Nothing bounds how WIDE a level
may be, so every string in a container is one leaf call running the full
pattern set: measured, one `arxiv_search` entry carried **2,832** author
names, so one tool result was 2,832 redaction passes.

Batch 126 removed that instance by bounding the producer, which is the right
fix for `arxiv_search` and not a fix for this. The walk runs on every tool
result, including `structured_content` from an MCP server nobody here wrote,
and that is the case where a wide level is not a quirk of one feed.

NOT OBVIOUSLY A DEFECT, which is why it is recorded rather than changed. The
cost is linear and the work is real -- a credential in element 5,000 is
exactly as much a leak as one in element 1 -- so a breadth cap would have to
fail closed the way the depth one does, and "refuse a result for being wide"
needs its own argument. The measurement to take first is the wall-clock cost
of a deliberately wide result, which nobody has.

### The measurement, and why BREADTH is the wrong axis (batch 127)

Taken. The measured instance is **negligible**: 2,832 leaves of an ordinary
author name cost **4.70ms**, against a network call that took hundreds of
milliseconds to produce them. Per-leaf cost is **1.66us** for a short string.

But the per-leaf figure is not a constant, and that is the finding. Holding
the payload at **2,000,000 bytes** and varying only the shape:

| leaves | chars/leaf | cost |
|---|---|---|
| 20 | 100,000 | 19.65ms |
| 200 | 10,000 | 19.58ms |
| 2,000 | 1,000 | 24.40ms |
| 20,000 | 100 | 59.74ms |
| 200,000 | 10 | **351.20ms** |

And holding leaves at 2,832 while varying bytes: 4.89ms at 10 chars each,
**289.58ms** at 10,000 each. So the cost is `a*leaves + b*bytes` with **both
terms real** -- an 18x spread at constant bytes, and a 59x spread at constant
breadth.

**That is what makes a breadth cap the wrong instrument, and it is a
stronger objection than the cost one this entry anticipated.** A breadth cap
does not bound the function: 20 leaves of arbitrary length passes any
breadth cap and is unbounded in cost. A **total-size** bound does bound it,
because leaves cannot exceed bytes -- so one instrument closes both terms
and the other closes neither. The entry proposed the cap that cannot work,
which is item 41's error again: the right defect, the wrong fix shape.

CLOSED AS RECORDED. The depth bound stays as it is, no breadth bound is
added, and `_walk` is unchanged. What the measurement actually found is a
**producer-side** gap -- `structured_content` from an MCP server has no size
bound of any kind, which is where the unbounded cost enters and is item 48
rather than a widening of this one. "Fix at the producer" is what this
module's own docstring says, and batch 126 did exactly that for the feed
that produced the 2,832 names.

## 47. `categories` keeps a `None` where `authors` drops one (closed, batch 127)

Found while closing item 41. `_parse_atom_feed` builds `categories` as
`[c.get("term") for c in ...]`, so a `<category>` with no `term` attribute
contributes `None`. Two lines above it, `authors` drops a nameless
`<author>` with a comment stating the reason: the list is rendered into a
citation and `None` in it reads as a value.

Both lists come from the same attacker-influenced feed and reach the model
the same way, so the two should not disagree about this. Trivial to fix and
deliberately NOT folded into batch 126, whose scope was the two bounds --
a one-line change to an untested branch is still a change to what the model
is handed, and it wants its own test rather than a ride.

### Fixed in batch 127

`all_categories` gained the filter `all_authors` already had, and the
comment above both now states the rule ONCE for the pair rather than being
copied -- the rule was already written two lines above the list that did not
follow it, which is how they came to disagree.

Both lists drop the malformed element **before** the cut, so the cap counts
values the model can use: filtering after the slice would let a nameless
element consume one of the 25 or 20 places.

TESTED IN `test_untrusted_content.py`, NOT `test_paging.py`, because the
author half of the same claim has lived in that file's malformed-feed table
since batch 117 -- a table row for the category case, a direct test beside
`test_a_nameless_author_is_dropped_not_rendered_as_None`, and a third test
asserting the RULE over both lists so that fixing one and not the other
fails. That third test is the one this item would have been caught by.

## 48. An MCP result has no size bound of any kind (open)

Found while measuring item 46, and PRE-EXISTING: it is §17's, not batch
127's.

`mcp_client/client.py:209` puts a server's `structured_content` into
`out["result"]` untouched, and `mcp_client/` contains **no size constant at
all** -- not a byte cap, not a breadth cap, not a depth cap. Every other
model-facing third-party text in this project is bounded at the producer:
`fetch_url`'s `MAX_CONTENT_BYTES` (§31 H7, #55), `arxiv_search`'s
`MAX_FEED_BYTES` and field caps (batch 126), `MAX_SUMMARY_CHARS` (§51 PG8).
MCP is the one producer that is not, and it is the one whose payload is
written by a third party nobody here reviewed.

**CORRECTION (batch 144).** "Every other model-facing text is bounded at the
producer" was false when it was written: the six math tools were a second
unbounded producer, and S06-02 measured a 14-symbol truth table returning
3,440,679 characters -- 49x `compaction_min_headroom_tokens` -- from 27
characters of input. Batch 143 bounded them at `config.MAX_READ_CHARS`, so
the sentence is true again now. It is corrected here rather than only in the
DEVLOG because this entry is what a later reader consults to decide whether
the SHAPE question below has a precedent, and "MCP is the only one" was the
argument for treating it as unprecedented. The precedent now exists and it
chose REFUSE over truncate, at a config value rather than a new constant.

**Measured, as part of item 46.** `check_output_policy` costs
`a*leaves + b*bytes`, and both terms are unbounded here: 200,000 short
leaves cost **351ms** and 2,832 leaves of 10,000 chars cost **290ms**, so an
unbounded result is unbounded redaction time on the dispatch path, before
the text is also unbounded context. A total-size bound is the one instrument
that closes both terms, since leaves cannot exceed bytes.

RECORDED RATHER THAN FIXED, and the reason is that the bound's SHAPE is a
real decision rather than a wiring job. #131 requires bounding this; PG8 and
§31 H7 give two different answers for how (per-item caps with a named route,
versus refusing to read past a ceiling), and neither transfers cleanly to
arbitrary JSON from an arbitrary server. Batch 126's own argument applies:
truncating a structured document is PG5's splice, and refusing one outright
is a tool the user configured becoming unusable. The measurement to take
first is what real MCP servers actually return, which nobody here has.

**AMENDED by the review marathon (batch 138): this entry names the wrong half
of its own defect.** Two scopes arrived here, and one of them measured it.

The `Where` above names exactly one site -- `structured_content` into
`out["result"]`. That is the OPTIONAL half: `structured_content` is an optional
field on `mcp.types.CallToolResult` (typed `Any`, verified against the
installed `mcp==2.0.0`) and a server need never send it. The half this entry
does not name is `out["content"]` -- the `"\n".join(...)` of every text block
at `mcp_client/client.py:194-197` -- and that is the path **every** MCP tool
result takes, including every result from every server that has never heard of
structured content.

Driven through the real `_normalize` and then the real `check_output_policy`
the dispatch path runs:

| text block in | `out["content"]` chars | `check_output_policy` |
|---|---|---|
| 1 MB | 1,048,576 | 11 ms |
| 8 MB | 8,388,608 | 93 ms |
| 32 MB | 33,554,432 | 369 ms |

Linear in bytes with no knee, because there is nothing to knee against. The
cost model above is `a*leaves + b*bytes`; the text path is the pure-`bytes`
term, and 32 MB of it is roughly 8M tokens of context behind a 369 ms
redaction pass on the dispatch thread. Control, from the producers this entry
names as having done it right: `fetch_url.MAX_CONTENT_BYTES` is 65,536, so the
unbounded path is 512x that at 32 MB and has no ceiling at all. The other
claim here -- "`mcp_client/` contains no size constant at all" -- was verified
exactly by enumerating every upper-case name containing `MAX`, `BYTES`,
`CHARS`, `LIMIT` or `SIZE` in all three modules: none in `client`, `config` or
`registration`.

**So the disposition stands and the prescription needs two words.** The bound
must be on the **total** normalized result, taken in `_normalize` before
either key is filled -- not on `structured_content`. This entry already argues
for that ("a total-size bound is the one instrument that closes both terms,
since leaves cannot exceed bytes") and then names only one term in its
`Where`, which invites a fix that bounds the optional field and leaves the
universal one open. A test asserts that a 10 MB text block and a 10 MB
structured payload come back the same bounded size, and that the truncation
says so in the result.

Severity is `S2` on the measuring scope's reading rather than `S3`: behaviour
is wrong today on the path a normal run reaches the moment any MCP server is
configured -- no unusual configuration, only a verbose tool. Still not `S1`,
because nothing is exploitable past resource exhaustion and the model is
handed too much rather than something false. Unchanged: choosing the number,
or the truncate-versus-refuse question, is what this entry is deferred for,
and §31 H7 and §51 PG8 giving two different answers is the stated reason.
# The review marathon's deferred findings (items 49-86)

`docs/REVIEW_MARATHON.md` scoped a pre-merge review of `navigable-agents` into
19 independent read-only scopes. 142 findings; the six `BLOCKS-MERGE` ones were
fixed in batches 130-135 and the two owner decisions in 136-137.

Thirty-five findings were dispositioned `DEFER`, and they become **34** items
here, 49-82: one of the thirty-five carried `DEFER` and `KNOWN` both, and its
half folded into item 48 above rather than opening a new entry. The last two
were not reported by anyone -- **item 83** came out of batch 137's fix pass and
**item 84** out of batch 138's own mutation pass -- which is the argument for
running one on a bookkeeping batch at all. **Item 85** came out of that batch's
full-suite run, from declining to re-run a flake until it went green, and
**item 86** out of batch 141's own gate catching a mistake batch 140 had
already shipped past it.

**Filed as GitHub issues in batch 159**, after PR #200 merged, which is when
`docs/REVIEW_MARATHON.md`'s disposition table says a `DEFER` finding becomes
one. Every entry from 49 to 114 that was still open got an issue, and the number
is under its heading; #262 is the index. Each was re-checked against `main`
before filing, and four did not match: 49, 77 and 80 were partly out of date and
carry a correction, and 106 is closed. 88 is closed as superseded, and 97 and
102 were not filed, each for the reason in its entry.

**Each entry below has to stand on its own, and that is deliberate.** The
reports live in `review/`, which is gitignored -- so unlike every other item in
this file, there is no durable document behind these to go back to. A `git clean
-xdf` removes the only other copy. Where an entry says something was *measured*,
the measurement is reproduced here rather than cited.

Three things carried over from the reports' own conventions, because they change
how an entry should be read:

- **`suspected` means no failure path was constructed**, and the reporter said
  so rather than dressing a reading up as a measurement. Items 57, 61, 69, 76
  and 80 are labelled that way. They are recorded because the property the
  design claims is not the property that holds, not because something is known
  to break.
- **The *Not* line is load-bearing.** Where a report explicitly refused to
  propose something, that refusal is kept -- it exists to stop the next reader
  re-proposing a change the first one already considered and rejected.
- **An entry is evidence, not instruction** (this file's own rule, learned
  twice in batches 126 and 127). Item 83 is a third instance: the triage note
  that recorded it was wrong about its own mechanism, and the correction is in
  the entry.

## 49. Three one-shot executors inherit the harness's console stdin (open, deferred batch 138)

**Issue:** #203.

**Corrected when filed (batch 159).** Re-checked on `main` before filing: batch
142 (S02-03's fix) already gave `_run_subprocess_fallback`
`stdin=subprocess.DEVNULL`, so the insecure fallback is closed. Two executors
remain -- `_run_inert` and the container one-shot in `_run_docker` -- and the
issue is filed for those two. The paragraphs below are as measured in batch 138.

**S01-02, severity S3, `stability`.** `security/sandbox.py:2124-2146`
(`_run_inert`), `:2532-2547` (`_run_docker` one-shot), `:2851-2874`
(`_run_subprocess_fallback`) spawn with no `stdin=`. Three sibling routes point
the other way, all read from the same file: the WSL one-shot uses `DEVNULL` or a
one-line `PIPE` for root (`:2622-2645`, the SS70 fix), the SSH one-shot the same
(`:2752-2766`), and every session `DEVNULL` by default (`:3176-3202`).

The failure path needs Docker down, which is when these three routes serve. On
the shipped `tiered` default, `shell("cat")` -- a bare read with no file
argument, a plausible model mistake -- classifies INERT, is auto-approved with no
prompt, takes `ROUTE_INERT_HOST`, and execs with the terminal as its stdin. It
then sits until `SANDBOX_TIMEOUT_SECONDS` consuming whatever the user types: on
the CLI, type-ahead meant for the next prompt, only partly mitigated by `ask()`'s
drain (item 44, which drops what the pump already holds and not what a live child
is consuming); under the TUI, a second reader beside Textual's input thread,
which is the keystroke-loss class batch 121 measured for the trust prompt. §29's
N1 is "one reader at a time", and it is violated on three executors.

RECORDED RATHER THAN FIXED, and already twice: `docs/ROADMAP_v3.md`'s SS70 row
and `AGENTS.md` both say "the container, inert and host-fallback one-shots have
the same gap and are recorded, not changed". This entry is the register half of
that deferral, with the reachability traced.

**Shape:** `stdin=subprocess.DEVNULL` on the three spawns, matching the three
routes that have it; a test asserting a bare `cat` with Docker down returns
without consuming a staged stdin line. Needs a `tests/BREAKING_CHANGES.md` row,
because a command that legitimately reads the terminal today gets EOF after the
change -- which is the shipped-behaviour change the SS70 deferral was protecting.

**Not:** the WSL/SSH/session paths (already correct); a timeout or a prompt (the
command was already approved, so the defect is the descriptor and not the
decision).

## 50. `run_isolated` buffers the child's whole output before bounding it (open, deferred batch 138)

**Issue:** #204.

**S01-03, severity S4, `performance`.** `tools/isolation.py:257-284` runs the
child with `capture_output=True` and no streaming cap; `:192-195` and `:220-226`
length-check stdout and excerpt stderr only once both pipes are in memory.

The child-side bound is real and is the one #55 required: `_child_main`
(`:287-310`) refuses to emit a result payload over `MAX_CHILD_OUTPUT_BYTES`
(4 MB). But the transport is not the payload. A compute child that prints
voluminously -- progress spam, a giant traceback, a warning flood, all ordinary
misbehaviour for a path whose purpose is containing misbehaving calls -- is held
in full in the parent up to the wall clock. `RLIMIT_AS` caps the child on POSIX,
not the parent's buffers; on Windows the wall clock is the only bound anywhere,
which is H3 as stated.

Filed for the comment rather than the cost: the module's docstring claims the
size is "bounded on the CHILD side, where the size is known before anything is
written", which is true of the result dict and not of the two pipes carrying it,
and a reader of that sentence concludes there is nothing left to bound. No math
tool emits unbounded text today and the timeout bounds the accumulation, which
is why this is S4.

**Shape:** cap the transport rather than the payload rule -- stream
`communicate` through a bounded accumulator (stop reading past N bytes, kill,
report truncation), or cap stderr independently since only its tail is ever
read. `tools/isolation.py` only.

**Not:** lowering `MAX_CHILD_OUTPUT_BYTES` (the result bound is correct);
thread-based collection (the module's process decision, H2, stands).

## 51. A session's refused credential is never quarantined (open, deferred batch 138)

**Issue:** #205.

**S02-05, severity S3, `security`.** `security/sandbox.py:1047`
(`_note_ssh_auth_failure`) and `:1419` (`_note_sudo_auth_failure`) are called
only from `_run_ssh_process` (`:2782-2783`) and `_run_wsl_process` (`:2648`).
Both read the subprocess's `stderr` -- and a session has none to read, because
`_popen_session` sets `stderr=subprocess.STDOUT` (`:3199`) so the stream belongs
to the session manager, for the stated reason that "a monitor has to see lines in
the order the program wrote them". So `_ssh_auth_rejected` and `_sudo_rejected`
can only ever be set by a one-shot command.

The quarantine exists for a measured harm, stated at `sandbox.py:985-993`: "one
refused call makes TWO authentication attempts (the keyboard-interactive method
and the password method each get their own prompt budget), so a harness that
retried on every command would walk a real account into a lockout at four
attempts a call." `_note_sudo_auth_failure` names `pam_faillock` for the same
reason. A model starting background sessions against a host whose stored password
is wrong is exactly "a harness that retried on every command", and the protection
does not reach it. S3 rather than S2 because the one-shot path still quarantines,
so the first ordinary `shell` call to that host closes the door for the rest of
the run; the gap is open only while every call is a session.

Deferred because it is structural rather than an omission. The stderr a session
would be judged on belongs to another layer, so the fix needs a decision about
who inspects a session's first bytes and when -- and that layer is
`core/shell_sessions.py`.

**Shape:** settle first whether the sandbox should be *told* about a failed
authentication by whoever reads the stream rather than reading it itself. A
callback the session manager invokes on the session's first lines keeps the
quarantine in one place; a second copy of the matching in
`core/shell_sessions.py` does not.

**Not:** changing the matching strings or the quarantine's one-way lifetime.

## 52. Sixteen of twenty-two credential shapes survive `redact_output_text` (open, deferred batch 138)

**Issue:** #206.

**S05-03, severity S3, `security`.** `safety/policy_enforcement.py:208-223`
(`_SECRET_PATTERNS`), `:287-306` (the three credential shapes), `:382-398` (the
composition). Driven through the real `redact_output_text`, with `ghp_`,
`xoxb-`, `sk-`, URL-userinfo and `<password>` shapes all caught as the control
that the probe detects redaction when it happens:

- **Survives:** `github_pat_` (the current fine-grained format), `gho_`/`ghs_`
  (OAuth and server flows), `sk-proj-` (current OpenAI project keys), `sk_live_`
  (Stripe), `npm_` tokens, `Authorization: Basic`, `Bearer`/JWT, an AWS secret
  key in an assignment, and the `api_secret`/`token` keywords.
- **Partly:** a PEM block is caught by its header line only. The base64 body --
  the key material -- survives verbatim.

The module's contract is "known secret patterns" as defence-in-depth over every
tool result; the set covers the 2024 vendor formats and misses their current
siblings plus two structural cases. Reachable whenever third-party text (a
fetched page, a search snippet, an MCP result) carries one.

Deferred because breadth is a product decision with documented exclusions on
both sides, not a regex edit: the `password|passwd`-only keyword set and the
quoted-only assignments are deliberate (`policy_enforcement.py:262-268` --
`token`/`secret`/`key` appear in ordinary prose, unquoted values out of scope),
and every addition buys false positives in a research tool that reads security
literature.

**Shape:** grouped additions, each with a dated source -- (a) vendor prefixes
`github_pat_`, `gho_`/`ghs_`/`ghu_`, `sk-proj-`, `sk_live_`/`rk_live_`, `npm_`;
(b) structural: the PEM body under a caught header, `Authorization:` schemes,
the JWT compact shape. Each wants this file's test idiom -- a whole-string case
plus a split-across-cut case.

**Not:** keyword widening past `password`/`passwd`, and unquoted values. The
comment at `:262-268` already refuses both; it is cited here so the next reader
does not re-propose them. No change to `_SECRET_PATTERNS`, which stay the input
side's whole vocabulary by decision.

## 53. `resolve=False` checks dotted IP literals and no other numeric form (open for the BEHAVIOUR, comment corrected batch 156)

**Issue:** #207.

**S05-04, severity S4, `coherence`.** `tools/builtin/web_search.py:114-123`
carries the comment "An IP LITERAL is still checked"; `resolve=False`'s semantics
are at `safety/policy_enforcement.py:144-150`. Driven through the real
`is_url_permitted` with `resolve=False`, which is what the filter `_normalize`
uses: `http://127.0.0.1/` and `http://169.254.169.254/` are refused, while
`http://2130706433/` (decimal for 127.0.0.1), `http://0x7f.0.0.1/` and
`http://0x7F000001/` are **allowed** -- `ipaddress.ip_address` parses none of
them, so the "name is taken on trust" branch takes them. All five are refused
with `resolve=True`, which is the path `fetch_url` always takes.

No SSRF follows: the only socket-opener in the scope resolves, so the cost is
one wasted tool call and one confusing refusal. The defect is the overclaim, in
the file that decides what the model is shown.

**Shape:** either teach the `resolve=False` path a literals-only resolution
(numeric forms resolve locally, so the latency reason for `resolve=False` does
not apply to them), or narrow the comment to "a dotted-decimal or bracketed IPv6
literal is still checked". Test: the three non-dotted spellings through
`_normalize`'s real filter, asserting dropped.

**THE COMMENT HALF IS DONE (batch 156)** and the behaviour half is what stays
open. §73 rewrote `_normalize` for S05-01 and S05-02, and leaving a claim
measured false in the function being rewritten is how a batch creates defect
shape 9 in the file it is repairing -- so the comment now says "a dotted-decimal
or bracketed IPv6 literal", which is what the code does. Nothing about the
FILTER changed: the three numeric spellings are still allowed there, still
refused by the only socket-opener in the scope, and the test this item asks for
is still unwritten. Re-measure before fixing; a comment that is now right is the
reason a later reader might think the behaviour is too.

**Not:** any change to `fetch_url`, which is correct; DNS at filter time for
names, where the documented latency tradeoff stands; any change to
`is_url_permitted`'s `resolve=True` path.

## 54. `probability_stats._build_distribution` is dead and is a second answer (open, deferred batch 138)

**Issue:** #208.

**S06-12, severity S4, `coherence`.** `tools/builtin/probability_stats.py:62-73`
against `:76-107`. `_build_distribution` has no caller: the one call site
(`:187`) is to `_build_distribution_with_warnings`. The two functions build the
same five distributions from the same dict with the same defaults, and the live
one also collects the warning strings.

This is shape 1 in its plainest form -- a value derived in two places, where the
bug is not that they drift but that nothing can tell you they have. They agree
today. A sixth distribution, or a changed default, added to the live function
alone would leave the dead one wrong with nothing red, and the next reader cannot
tell which is authoritative without grepping for callers.

**Shape:** delete `_build_distribution`. If a no-warnings entry point is wanted
it is `_build_distribution_with_warnings(...)[0]`.

**Not:** changing the defaults, the warning strings, or
`_build_distribution_with_warnings`' signature.

## 55. `**`-unpacking is admitted by the math node allowlist (open, deferred batch 138)

**Issue:** #209.

**S06-13, severity S4, `security`, harm `suspected`.**
`tools/builtin/_math_common.py:185` has `ast.keyword` in `_ALLOWED_NODES` for
ordinary keyword arguments, and `f(**mapping)` is also `ast.keyword` (with
`arg=None`). So the `**` form passes `_validate_ast` and
`safe_parse('sin(**Dict())')` reaches evaluation, where it fails with "sin takes
exactly 1 argument (0 given)" -- a refusal from SymPy rather than from the guard,
which is the distinction
`test_safe_parse_refuses_the_injection_payload_class` exists to make.

The module's closure argument enumerates what is absent, and `AGENTS.md` repeats
it: "`JoinedStr`, `Lambda`, the comprehensions, `NamedExpr`, `Starred` and
`Dict`/`Set` are all absent too, by being unlisted rather than by being
recognised." `Starred` is the `*args` form. The `**kwargs` form is spelled with
`keyword`, which is listed -- so one of the two unpacking constructs is closed by
the node list and the other only by the unavailability of a string-keyed mapping.

**`suspected`, and the reason is the interesting part.** `**` needs a mapping
with `str` keys. `ast.Dict` is absent, string literals are refused, and
`sympy.Dict` -- the one allowlisted name with `keys()` -- sympifies its keys into
`Symbol`s, which `**` rejects with "keywords must be strings". The construct is
admitted and inert. It is filed because the property that makes it inert is not
the property the design claims.

**Shape:** in `_validate_ast`'s existing `ast.Name` loop, refuse an
`ast.keyword` whose `arg is None`. One clause, and it makes the comment's
enumeration true. Dropping `ast.keyword` outright is smaller still -- measured,
keyword arguments do not survive `auto_symbol` anyway
(`safe_parse('Symbol("a", positive=True)')` fails at evaluation) -- but that is a
decision about future expression support, which is why this is deferred.

**Not:** adding `ast.Dict` or `ast.Starred`; touching
`_STRING_ARG_CONSTRUCTORS`.

## 56. Nothing imports `env_secrets`, so `.env` is read by nothing (open, deferred batch 138)

**Issue:** #210.

**S07-07, severity S4, `coherence`.** No production module imports
`env_secrets`, so `get_env_secret` has no caller and the import-time
`load_dotenv` at `env_secrets.py:57` never runs. `git grep -l env_secrets`
returns the module, one test file and a dozen-odd documents -- this entry is now
one of them -- and **no production module.** Re-measured in batch 138: the only
`import env_secrets` anywhere is inside two string literals in
`tests/test_credentials.py`, which are subprocess snippets. `git grep -n "GITHUB_API_TOKEN\|NVD_API_KEY"` finds the two
names only in `.env.example` and in unrelated MCP test fixtures.

One of the four documents is right and three read as though the mechanism is
live. `docs/ARCHITECTURE.md:522` is the honest one: "a GitHub token for **a
future** GitHub tool, an NVD/NIST key for **a future** CVE-lookup tool" -- a
module shipped ahead of its consumers, recorded as such, which is not a defect.
`AGENTS.md:82` states it in the present tense, and so does `docs/PRIVACY.md:21`,
which lists `.env` under *What is stored locally* as holding tool API keys.
`.env.example` goes further and tells the user what will happen:
"`env_secrets.get_env_secret()` will raise a clear error at the point of use if a
tool needs a key you never set". No tool needs one, so nothing raises and nothing
reads.

Shape 2 in its mildest form -- wired, documented, never executed end to end --
and the mildness is why it is S4. The user-visible cost is that someone who
follows `.env.example` puts a GitHub token on disk believing the harness will use
it, and `docs/PRIVACY.md`'s deletion section does not mention `.env` either, so
nothing tells them to remove it.

**Shape:** the choice is the owner's and either answer is cheap. Either the three
present-tense claims gain `ARCHITECTURE.md`'s "for a future tool" clause -- three
sentences, module untouched -- or `.env` is wired to something, which is a feature
and not this marathon's business. Item 57 is a reason to prefer the first until
the second is actually wanted.

**Not:** deleting the module. It is referenced by `package.json`'s `files`, by a
bandit baseline entry and by two pinned tests in `tests/test_credentials.py`
whose own `tests/BREAKING_CHANGES.md:2826` row explains what reverting them
re-breaks; and the `usecwd=True` fix it carries is correct and worth keeping.

## 57. `env_secrets` turns an untrusted `.env` into process environment at import (open, deferred batch 138)

**Issue:** #211.

**S07-08, severity S3, `security`, harm `suspected`.** `env_secrets.py:55-57`:

```python
ENV_FILE = os.environ.get("AGENT_ENV_FILE")

load_dotenv(ENV_FILE or find_dotenv(usecwd=True))
```

`load_dotenv` writes every name it finds into `os.environ` for the whole
process, and `find_dotenv(usecwd=True)` walks *up* from the working directory, so
a `.env` above it is in scope too. A `.env` is an ordinary file in a repository
and inside no trust boundary: `core/workspace_trust.py` gates `.venastine/` and
`AGENTS.md`, and nothing gates `.env`.

Several security-relevant knobs are read from `os.environ`, and most at *call*
time, which is after any import: `AGENT_SECRETS_FILE` (`security/secrets.py:281`
-- where the encrypted store lives), `AGENT_LOG_FILE` (`logging_setup.py:170`),
`AGENT_LOG_LEVEL` (`logging_setup.py:98`), `VENASTINE_REDACT_OFF`
(`security/posture.py:274`) and `AGENT_PROVIDERS_FILE` (`credentials.py:17`, at
import).

`VENASTINE_REDACT_OFF` is the one that makes this worth recording.
`AGENTS.md:1464` states the decision: it disables pattern substitution and is
"deliberately **not a settings.json key** (G7's reason)." The point of keeping it
out of `settings.json` is that a cloned repository must not be able to turn
redaction off. A cloned repository can ship a `.env`.

**`suspected` is the honest label: no live failure path exists.** Nothing imports
`env_secrets`, so `load_dotenv` never executes and none of the above happens
today. The finding is that the first module to import it -- which `.env.example`
and `AGENTS.md:82` both invite, and which is the whole point of the module
existing -- silently converts an untrusted file into process environment, and
nothing in the module, the decision record or the trust layer would surface it.
Item 56 is why it is still open.

**Shape:** whichever module first needs a tool key calls a function rather than
importing a side effect -- `load_dotenv(..., override=False)` inside
`get_env_secret`, or an explicit `load_tool_secrets()` the entry point calls at a
known moment. Then there is a place for the half that matters: a `.env` may
populate tool keys and must not be able to set `AGENT_*` or `VENASTINE_*` names.
`dotenv_values()` returns the mapping without touching `os.environ`, which makes
that filter a two-line comprehension rather than a policy.

**Not:** claiming any of this happens today. Whether `.env` should join D17's
content hash is a separate question for the trust layer and for the owner.

## 58. One env-var misconfiguration warns and the other raises (open, deferred batch 138)

**Issue:** #212.

**S07-10, severity S4, `ux`.** `logging_setup.py:93-107` (`_resolve_level`)
against `:190-199` (the `OSError` fallback), reached from `main.py:2495`. An
unwritable `AGENT_LOG_FILE` prints a warning and continues, with the stated
reason that "logging to a file is non-essential". An unparseable
`AGENT_LOG_LEVEL` does not:

```
$ AGENT_LOG_LEVEL=verbose python -c "from logging_setup import configure_logging; configure_logging()"
raises at startup: ValueError invalid literal for int() with base 10: 'verbose'
```

`main.py:2495` calls `configure_logging()` with no handler around it, and its own
comment explains that the call sits there because "nothing above logs anything"
-- so the traceback is the whole user experience, and the one subsystem that
would have reported the problem legibly is the one that failed to start. Shape 6:
a handler present for the unreachable failure and absent for the reachable one,
forty lines apart in one function, where the reachable one is a typo in a
documented knob. `verbose`, `trace` and `warn` are all plausible things to type;
`warn` happens to work, because `logging.WARN` exists.

The counter-argument is in the code and is why this is deferred rather than
fixed. `:100-103`: "fall back to parsing as an int ('20'). Anything else raises
`ValueError`, which is the right failure mode for a misconfigured env var." The
raise is deliberate, and **the owner may reasonably mark this ACCEPTED on the
strength of that comment.** Not a `DECISION` finding, because a comment is not a
locked decision in `AGENTS.md` or a ROADMAP id.

**Shape:** if it is to change, keep the raise and make it legible -- catch the
`ValueError` in `_resolve_level`, print the same style of warning the `OSError`
branch prints (naming the variable, the value and the accepted forms), and fall
back to the `INFO` default. The two branches then agree about what a bad
environment variable costs.

**Not:** having `configure_logging` validate anything else.

## 59. The config schema checks shape where fitness also matters, unevenly (open, deferred batch 138)

**Issue:** #213.

**S08-04, severity S4, `coherence`.** `config_schema.py:392` (`ssh_port: int`,
un-ranged), `:355`/`:361` (`db_path`, `workspace_dir`: bare `str`, no emptiness
check), `core/config_loader.py:74` (`max_token_budget: (int, type(None))`, no
sign check). `PositiveInt` is `strict=True` and refuses `max_read_chars=True`;
the three fields above accept anything their lax type allows. Driven against the
real `config_schema.validate`:

```
ssh_port=True: ACCEPTED       ssh_port=-1: ACCEPTED
ssh_port=99999: ACCEPTED      db_path empty: ACCEPTED
workspace_dir empty: ACCEPTED
_type_ok(-5, int) = True      _type_ok(0, int) = True
```

Downstream, `max_token_budget: 0` resolves through `spend_cap()`
(`config_loader.py:1247`, no validation) into `_run()`, whose check is
`total_tokens_used >= max_total_tokens` (`core/loop.py:1742-1743`) -- true after
the first billable call, so every turn dies with `token_budget_exceeded` having
spent one model call, reporting a budget problem for a configuration typo.
`ssh_port: True` coerces to port 1 and fails at the SSH call rather than at
startup.

Separately and in the same entry because it is two lines: the trust-rationale
comments at `core/config_loader.py:1390-1393` and `main.py:2172` still say a
project's settings "multiply pipeline cost via `ensemble_n`". `ensemble_n` is
vestigial and warned-ignored (`core/reasoning/orchestrator.py:1015-1028`), the
only roster key (`ensemble_models`) is rejected by name, and the live multiplier
is `ensemble_mode` over the `config.yaml` roster. Shape 9; item 21 does not cover
it, which was checked by reading that entry.

**Shape:** `ssh_port: PositiveInt` or an explicit port range; `min_length=1` on
`db_path`/`workspace_dir`; reject `max_token_budget <= 0` at load in
`_validate_settings` (it is a settings key, not a schema key), with the
compaction-relationship errors as the wording model; reword the two rationale
comments to `ensemble_mode`. One test per check, each demonstrated red against
the ACCEPTED lines above.

**Not:** the `ensemble_models`/`critic_model`/`embedder_model` looseness, which
is deliberate and documented at `config_schema.py:271-283` and validated
downstream with a better error; `default_effort`, which is already cross-checked;
`tui.effort`, which downstream per-model validation owns.

## 60. The loader reads `.md` files the trust hash refuses to (open, deferred batch 138)

**Issue:** #214.

**S08-05, severity S4, `stability`, harm `suspected`.**
`core/config_loader.py:363-364` is `open(path)` plus `f.read()` with no
regular-file guard. `core/workspace_trust.py:114-126` -- on the same files --
skips non-regular files with `<non-regular file>`, reads in chunks, and states
the FIFO rationale in its own comment.

Every `initialize()` and every trust-prompt listing (`_catalog_entries` ->
`_parse_md_file`) opens and fully reads each discovered `.md`. On POSIX, a cloned
project containing `.venastine/skills/x.md` as a symlink to a FIFO -- git stores
symlinks, `os.walk` lists the link, `open()` follows it -- would block
`initialize()` with no timeout, pre-grant inside `describe_project_content` and
post-grant inside `_discover`, while `_content_hash` sails past it by design.

**`suspected` because Windows cannot construct a FIFO**, so this was not driven.
The regular-file half *was* measured and is adequate: parsing a 100 MB skill body
costs 0.66 s and hashing a 100 MB covered file costs 0.08 s. This entry is only
about the non-regular half.

**The question for the owner**, which is why this is deferred: should
`_parse_md_file` take the hash's guard, or is a FIFO at a `.md` path out of
threat scope because whoever can plant one locally can already write
`.venastine/` directly? The asymmetry matters only for the clone-carried symlink
shape, where that local-write equivalence does not hold pre-grant.

**Shape:** an `os.path.isfile`-style guard, or `stat.S_ISREG` on the opened fd to
close the replace-between-check window, before `open()` in `_parse_md_file` --
warning and skipping like every other malformed definition. Test on POSIX CI: a
FIFO at a skill path initialises with a warning instead of hanging, with a
subprocess timeout as the harness, since a removed guard hangs rather than fails.

**Not:** capping regular-file reads. Measured adequate at 0.66 s per 100 MB;
inventing a cap would add a failure mode to the largest legitimate manifests.

## 61. A parallel batch widens the window between "the tool ran" and "the row is durable" (open, deferred batch 138)

**Issue:** #215.

**S10-06, severity S3, `stability`, harm `suspected`.**
`core/loop.py:1196-1244` (`_dispatch_parallel`) against `:1286-1296`
(`_settle_one`). #42's rule is that `memory.add_tool_result()` runs *before* the
`tool_result` event is yielded, "so emitting first made the row's durability
depend on a consumer continuing to read". On the sequential path that is
airtight: `_dispatch_one` has no `yield` between the dispatch returning and
`_settle_one` persisting, so the window is zero yields wide.

In a parallel group it is not. Workers park their outcomes in futures and
`_settle_one` is reached only after `_dispatch_parallel` returns, which is after
*every* worker has finished. In between, the frame yields each worker's narration
out through `_run_steps` (`:1208`). Abandonment at one of those yields drops
`settled_calls` for calls whose tools have already run -- up to
`subagent_max_parallel` (3) of them -- and D20 has already written their
`tool_use` blocks, so M4's pairing breaks for a whole batch: the defect #42
closed, arriving through NA10/NA11's reordering instead of through the yield
order.

Abandonment itself is reachable: `tui/app.py:5280` calls `events.close()` on app
shutdown and that chain reaches `_run`, so `GeneratorExit` is delivered at
whatever yield `_run` is suspended at. **`suspected` because the trigger is a
conjunction:** the `tool_call_start` events are emitted before anything is
submitted (`:1784-1787`), so abandoning there loses nothing. What remains is a
`permission_request` or an `approval_reused` notice pushed by one worker while a
sibling's tool has already completed -- so it needs a human in the loop, or a
reused sign-off, in the same batch as a finished call, at the moment the consumer
goes away. A second consequence of the same shape, not a defect in itself:
`GeneratorExit` there unwinds through `with ThreadPoolExecutor(...)`, whose
`__exit__` is `shutdown(wait=True)`, so `close()` blocks for as long as the
slowest tool in the batch takes.

`docs/ARCHITECTURE.md` 4.8 states the guarantee as a property of ordering --
"`memory.add_tool_result()` runs BEFORE the `tool_result` `LoopEvent` is yielded"
-- which is still literally true per call and no longer sufficient, because the
persist for call *n* now waits on call *n+2*.

**One adjacent claim is already false and should be corrected in the same place.**
`docs/ARCHITECTURE.md` 4.8 says abandonment is "reachable in the TUI (`_pump`
drops the generator on any `post_message` exception)". There is no `_pump` in
`tui/`; the only one in the tree is `_StdinReader._pump` at `main.py:627`.
`tui/app.py:2948-2959` drains with a plain `for event in generator`, catches
`BaseException`, and never closes the generator -- so abandonment still happens,
by garbage collection rather than by the named mechanism. Shape 9, in the
paragraph documenting this finding's own containment rule.

**Shape:** the question is whether the durability rule or the ordering rule
gives. Two shapes keep both: settle each future into a per-call slot as it
completes and persist in model order once a *prefix* is contiguous; or keep the
batch and wrap the drain so `GeneratorExit` persists whatever outcomes are in
hand before re-raising, which answers #42 without touching NA11 at all. A test
drives a two-call parallel batch, closes the generator at an intra-batch yield,
and asserts one `tool_result` row per tool that actually ran.

**Not:** reopening NA9/NA11's ordering decision, which is locked and whose reason
is stated; treating the `shutdown(wait=True)` block as a separate finding, since
it is a consequence of the same window.

## 62. A truncated fold is measured against the length it no longer has (open, deferred batch 138)

**Issue:** #216.

**S11-07, severity S4, `correctness`.** `core/compaction.py:1047-1049`, in
`compact()`. `_truncate_oldest` shortens the span to the one-call input budget,
and the two lines after it update `original` and leave `target` alone:

```python
segment_text, truncation_notice = _truncate_oldest(segment_text, budget)
if truncation_notice:
    original = len(segment_text)
```

`target` was computed at `:1021` from the pre-truncation length, so the compactor
is told "the original is `len(truncated)` characters; your summary must be at
most `ratio * len(untruncated)` characters". The adjacent forced-chain branch,
eight lines up at `:1039-1040`, recomputes both -- which is what makes this a
slip rather than a decision.

`compaction_strength` is documented as "the fraction of the original character
count a fold aims for" (`config.yaml:848-856`), and after a truncation it names
the fraction of a length that is no longer the input. The fold is looser than the
strength asked for, by the ratio of the two lengths. Past a span of roughly
`budget / ratio` characters the target exceeds the text itself and the
`len(summary) <= tolerance` check in `_summarize` passes trivially, so the fold
can return something no smaller than what it replaces. With the shipped strength
3 (ratio 0.15) and `_input_budget` = `window - 20,000`, the degenerate case needs
a first compaction on a window below roughly 23,500 -- a range the same scope
measured as already unusable for an unrelated reason (`_dependent_floor()` puts
the derived trigger above the window there, so `used` can never reach it; that
one is `FIX-NOW` and not in this block) -- so in practice the symptom is a looser
fold rather than a useless one.

**Shape:** recompute `target` beside `original`, matching the branch above. One
line. `summarize_thread` needs nothing: its target is
`config.SUMMARY_TARGET_CHARS`, an absolute, which is why the same two lines there
are already correct.

**Not:** changing `_truncate_oldest`, the budget, or the target ratios.

## 63. Batch 119's yield logs a standing condition per step (open, deferred batch 138)

**Issue:** #217.

**S11-09, severity S4, `ux`.** `core/compaction.py:701-705`, in
`compactable_span()`. The yield is reported with `logger.info` inside a function
called once per `compact()`, which `_maybe_compact` calls at the turn boundary
*and* after every step while the thread is over the trigger. So on any thread at
or below `keep_recent_turns` turns and over the trigger, the line repeats per
step for the life of the conversation. Observed on all four evaluations of one
probe run, with the correctly-levelled DEBUG line beside it as the control.

This is the logging shape #44 removed from this module and that then came back.
`compact()`'s own docstring: "REPORTABILITY LIVES WITH THE CALLER. How often this
function runs -- once at the turn boundary plus once after every step -- is
run-scope knowledge the loop has and this module lacks, which is why the
standing-condition WARNINGs moved out of it into `_maybe_compact`'s once-per-run
guard (#44)." `_maybe_compact`'s docstring names the symptom: "up to
MAX_ITERATIONS + 1 identical lines per turn on a thread that stays over trigger
and fully floored, which is how a reader gets trained past the warnings that do
not repeat." The yielded-count line is a standing condition by that definition --
it describes a state of the thread, not an event.

S4 because INFO does not reach the transcript through `TranscriptLogHandler`
(which routes WARNING and above), so it is a log-volume problem rather than a
screen one. Recorded because it is the same module's own rule, and because the
diagnostic is genuinely useful once.

**Shape:** move it where the run scope is, the way #44 did -- have
`compactable_span` report the yield in its return path's vocabulary, or demote
the line to DEBUG and let `_maybe_compact` say it once per run under the existing
`notices` dedup, which is the mechanism already there for exactly this.

**Not:** the yield itself, which is batch 119's fix and correct; the
`_sub_turn_span` INFO at `:715-719`, which fires at most once per thread per fold
and is not a standing condition.

## 64. Two scoring helpers raise where the scope says nothing raises (open, deferred batch 138)

**Issue:** #218.

**S12-03, severity S3, `stability`, harm `suspected`.**
`core/reasoning/scholar.py:329-345` (`author_standing`'s authorship loop) and
`core/reasoning/source_scoring.py:421` (`l2_normalize`), against that module's
"nothing here raises" contract at `source_scoring.py:28-35`. Driven against the
real functions with synthetic malformed input:

```
l2_normalize(['a','b'])
  -> TypeError: can't multiply sequence by non-int of type 'str'
ScholarLookup.author_standing({'authorships': ['BAD']})
  -> AttributeError: 'str' object has no attribute 'get'   (scholar.py:331)
```

Either propagates through `_apply_embeddings`/`_apply_scholar` -- no `try` at
either call in `score_grounding_sources` -- through `_ground_and_score` into the
pipeline's `except`, flipping a run that had already grounded and scored into
durable `status='failed'`.

Both stages are optional by design and both document give-up semantics. A
malformed-but-successful response is the fourth failure mode neither handles:
`embed_texts` converts transport and provider errors to `EmbeddingError` but
passes a well-formed envelope with non-numeric contents straight through
(`_check_vectors` checks batch length, dimension uniformity and emptiness, never
element type), and `author_standing` trusts every authorship entry to be a dict.

**`suspected` because the triggers are third-party malformation** -- a buggy
embedder, a changed OpenAlex shape -- rather than model output: reachable by
direct call, not observed from a live provider. Verified contained nearby, which
is what makes these two the uncontained remainder: `embed_texts` wraps every
provider exception (`core/client.py:1493-1499`), `ScholarLookup._get` catches
fetch and parse failures per attempt (`scholar.py:247-261`), the writers degrade
loudly and never fatally, and ensemble-candidate, reviewer-missing,
reviewer-transient and re-synthesis failures are all contained with a trace.

**Shape:** guard at the trust boundary in each helper, matching the file's
posture of drop, clamp or trace and never raise -- an element-type check in
`_check_vectors` or `l2_normalize` (a non-numeric vector scores 0.0 or disables
the scorer with `failed_reason`, reusing the SQ10 fallback), and an `isinstance`
guard per authorship entry (skip non-dicts, count the skip in the stage's
coercion report). Test: malformed input yields fallback scores plus one trace
line, and the run completes.

**Not:** retrying a malformed payload -- unlike model JSON, a provider shape will
not fix itself on retry; changing the give-up-on-first-outage policy, which is
load-bearing (SQ10).

## 65. A second `connect_all()` abandons a generation of MCP children silently (open, deferred batch 138)

**Issue:** #219.

**S13-09, severity S4, `stability`.** `mcp_client/client.py:355-384`
(`connect_all`) and `:471-525` (`disconnect_all`). `connect_all()` rebinds
`self._ready`, `self._shutdown`, `self._done` and `self._task`
unconditionally, so a second call abandons the first generation: the old manager
task is still parked on `await self._shutdown.wait()` and the event it waits on
has been replaced, so nothing can ever set it. Driven over the same in-process
transport `tests/test_mcp_client.py`'s `client` fixture uses:

| after two `connect_all()` and one `disconnect_all()` | |
|---|---|
| generation 2 `_done` set | True |
| generation 1 `_done` set | **False** |
| generation 1 `_shutdown` ever set | **False** |
| generation 1 exit stack unwound | **False** |
| straggler WARNING emitted | **none** |

Control: one `connect_all` and one `disconnect_all` leaves `_closed ==
['probe']` with no straggler warning, correctly.

The warning is silent for a second, independent reason: `stragglers` is
`sorted(set(self.clients) - self._closed)`, both keyed by **server name**, and
`_closed` is never cleared by `disconnect_all`. Generation 2's clean close
records `'probe'`, which is the key generation 1 would have had to be missing
from. So the one diagnostic that exists reports success, and `_shutdown_loop()`
then stops the loop with generation 1's task frozen on it holding its children --
the exact state `disconnect_all`'s docstring says the function exists to prevent
("the failure mode is orphaned subprocesses accumulating across sessions").

**Unreachable today, and that is stated rather than hidden:** `grep -rn
"connect_all" --include=*.py` outside `tests/` and `mcp_client/` returns exactly
one call site, `main.py:2310`, reached once per launch. There is no second-connect
path in production. What makes it worth recording is which future makes it live:
F7 (`docs/ROADMAP_v2.md:3056`) says "runtime `/mcp` management is recorded as a
candidate future section, deliberately not built here." Runtime MCP management is
reconnect, and reconnect is this.

**Shape:** make a second `connect_all` on a live client either raise or
disconnect first -- a guard on `self._task is not None` is enough, and raising is
the better half of D24's trade here, since silently discarding a generation of
child processes is the failure that cannot be seen. Separately,
`disconnect_all` should clear `self._closed` along with `self.clients` and
`self._catalogs` at `:522-524`; leaving it is what makes the straggler check
unable to see a second cycle. If F7 is ever built, the straggler set needs a key
that is not the server name.

**Not:** building reconnect or the `/mcp` command F7 defers; anything about
`_ensure_loop`/`_shutdown_loop`'s module-level globals, which are a related
single-instance assumption and would be the same future section's problem.

## 66. A `_blocking_modal` comment states a park that does not happen (open, deferred batch 138)

**Issue:** #220.

**S14-07, severity S4, `coherence`, textual 8.2.8.** `tui/app.py:4214-4221`, the
comment above `_blocking_modal`'s `set_blocked` call, states as a measurement:

> DIRECT, never call_from_thread: this runs on a worker, and a quitting app has
> no message pump left to marshal into -- measured, that parks the worker
> forever and breaks the shutdown release this method exists to keep working

and the next statement, `:4223`, marshals through `call_from_thread` anyway.
Driven: a worker that marshals after an unhandled UI exception has torn the app
down gets `RuntimeError` back **immediately** -- `raised RuntimeError after
0.00s` -- because `_loop` is None by then (`textual/app.py:1818`, reset at
`:2303`). It does not park. There is a narrower window, between the pump stopping
and `_shutdown()` finishing, in which the loop is still running and the marshal
would instead succeed against a dying app; no case was found in which it parks.

The conclusion the comment draws is still correct, for a reason it also gives
("the meter touches no widget, so there is nothing to marshal"), so no behaviour
follows from the error. What follows is a reading: this is the sentence someone
consults when deciding whether the shutdown release is still load-bearing and how
urgent a parked worker is. The comment one block up, at `:4005-4009`, is an audit
(#108) correcting exactly that class of mistake in exactly that place. The same
trap, fourteen lines down, still open.

**This is also why the `_blocking_modal` `get()` park is the real cost and the
marshal is not.** A fixer starting from this comment goes looking for an
unbounded park the measurement says is not there, and the 600 s on `channel.get`
is one line further on.

**Shape:** narrow the sentence to what is measurable -- a marshal after teardown
raises, and the reason to call `set_blocked` directly is that it touches no widget
and must not be able to fail on the shutdown path. Cite the probe's shape so the
next reader can re-take the measurement when the textual pin moves, which is what
D22 asks of every claim about the installed package.

**Not:** changing the direct `set_blocked` call, which is right.

## 67. `_cmd_secrets` says "would deadlock" where it would raise (open, deferred batch 138)

**Issue:** #221.

**S14-08, severity S4, `coherence`, textual 8.2.8.** `tui/app.py:6520-6524`,
`_cmd_secrets()`'s docstring: "a command handler runs on the UI THREAD, and the
blocking asks are drained by that same thread -- so calling one here would
deadlock the app on the first `/secrets`, not fail it."

It would not deadlock. `_blocking_modal` reaches `self.call_from_thread(...)` at
`:4223`, and textual's second guard raises at once -- `if self._thread_id ==
threading.get_ident(): raise RuntimeError("The call_from_thread method must run
in a different thread from the app")` (`textual/app.py:1821-1824`). The raise then
travels out through `commands.dispatch` and, before batch 135, exited the app; it
now reaches batch 135's containment. Either way the symptom is loud, not wedged.

The command's design is right and the rule it states is the right rule; only the
stated consequence is wrong, and it is wrong in the direction that makes the
hazard sound unrecoverable when it is in fact loud. Worth correcting because the
sentence is the pattern the next command author copies, and "it would fail
loudly" versus "it would deadlock" changes whether they trust a test to catch it.

**Shape:** one word -- it would *raise*. Keep the rule and keep
`action_pick_thread` named as the pattern.

**Not:** restructuring `/secrets`, which is sound.

## 68. `AGENTS.md`'s `_blocking_modal` bullet is split by eight unrelated ones (open, deferred batch 138)

**Issue:** #222.

**S14-09, severity S4, `docs-drift`.** `AGENTS.md:2673-2674` and `:2708-2711`.
The bullet in *The response channel* that documents the TUI's one
`_blocking_modal` opens at 2673 -- "**`tui/app.py` has one `_blocking_modal`.**
Four asks did the same six lines," -- and ends mid-clause. Its tail is at 2708,
after eight unrelated bullets and a heading: "and the `_permission_channel` field
each published is what `_release_permission_channel` reaches for when the app
exits with a modal open. It is no longer the loop's channel -- it is this app's
parked-worker queue."

So the sentence a reader meets is "Four asks did the same six lines," followed by
"**Slice 2 -- the two tools the channel existed for.**", and the explanation of
what the field is for appears to belong to J13's whole-list write.

`AGENTS.md` is the agent-context file and `CLAUDE.md` says its decisions "have
already been broken once by someone who reasoned from the code alone", so the one
paragraph explaining why `_permission_channel` is a single published field is
worth being readable -- it is the fact three other TUI-lifecycle findings all
turn on. Nothing mechanical checks prose continuity, and item 7's narrowness rule
("a claim earns a check by having already drifted, and only if the truth is
something the suite can compute") says nothing should.

**Shape:** move the stranded tail back onto its own bullet, above the *Slice 2*
heading. A hand fix, not a new gate.

**Not:** adding a check for it.

## 69. `_ask_lock`'s no-timeout argument covers half the critical section (open, deferred batch 138)

**Issue:** #223.

**S14-10, severity S4, `coherence`, harm `suspected`.**
`core/interaction.py:70-77` against `tui/app.py:4223-4232`. NA12's lock
serialises every question at the producer and is given no deadline on this
argument:

> NO TIMEOUT, deliberately. Every waiter is bounded by the holder's own deadline:
> the TUI parks for at most ATTENDED_APPROVAL_TIMEOUT_S and a run-backed CLI
> prompt carries the same, so the queue drains on its own.

`core/interaction.ask` holds the lock across `channel.ask(request)`, which for
this shell is `_ask_blocking` -> `ask_*_blocking` -> `_blocking_modal`. The
timeout the argument rests on is armed at `tui/app.py:4232`, and it is reached
only after `self.call_from_thread(self.push_screen, ...)` at `:4223` returns --
and that call waits on `future.result()` with no timeout of its own. So the
holder is bounded by `ATTENDED_APPROVAL_TIMEOUT_S` for the second half of
`_blocking_modal` and by nothing at all for the first.

**`suspected`: no failure path was constructed.** The post-teardown marshal
raises rather than parking (item 66), and no reachable state was found in which
`push_screen` fails to complete. What is demonstrated is only that the sentence's
bound does not cover the whole section it is offered about -- and because that
sentence is the whole reason a process-wide lock has no deadline, a reader
checking whether NA12 can wedge the harness concludes it cannot from a premise
covering part of the section.

**Shape:** state the bound as what it is -- the holder is bounded once it is
parked on the queue, and the marshal before it is bounded by the UI thread
remaining responsive. If that reads too weak, the narrow fix is in the shell
rather than the leaf module: `_blocking_modal` could do the marshal before taking
anything NA12 cares about, or the lock could be acquired around the park alone.

**Not:** adding a timeout to `_ask_lock`, which would put a `config.py` number
into a leaf module that deliberately imports nothing first-party.

## 70. Three widgets measure the terminal in characters, not cells (open, deferred batch 138)

**Issue:** #224.

**S15-02, severity S4, `ux`.** `tui/diffs.py:95` (`wrap_source` breaks on
`len(text)`), `tui/widgets.py:2535` (`Transcript._write_padded` pads with
`ljust(total)`), `tui/widgets.py:986` (`AgentPanel._fit` truncates on `len`), and
`:1160` (`SessionPanel._redraw` through the same `_fit`). For single-cell text the
two measures coincide, which is why the suite is green. For double-width text
they part, and the row the widget sized is not the row the terminal draws:

```python
body = chr(12354) * 20          # 20 chars, 40 cells
rows = diffs.wrap_source(body, 20)
assert len(rows) == 1           # len says it fits; the terminal needs 2 rows
assert cell_len(rows[0]) == 40  # Rich soft-wraps the overflow itself
```

The padding that carries a diff row's background then lands on the first visual
row only -- the same reason `_render_diff` pre-wraps at all -- so a wrapped
wide-char addition renders half-green. The sidebar twin: ten wide chars (`len`
10, `cell_len` 20) pass `_fit(text, 18)` unchanged and overflow the 18-cell box
the panel laid out against. Controls in the same probe: the same calls with ASCII
of equal `len` behave as labelled (a 250,000-char ASCII line wraps to 3,125 rows
in 107 ms), so the probe discriminates the measure rather than the function.

**The transcript's own streaming wrap does not share this defect** --
`markdown.wrap_display`, `plain_wrap` and `display_len` all measure `cell_len`,
which is how the two halves can disagree about where a row ends. That is also why
this is not coerced upward: the path that must keep live rows equal to replayed
rows already measures correctly, and these are diff blocks and sidebar rows
rather than the answer stream.

**Shape:** measure the three sites in cells -- `wrap_source` over `cell_len` cut
points (it can share `markdown._cut_points` semantics without importing marks,
since diff bodies are literal source and the cut set is every character boundary,
which is what `plain_wrap` already does for thinking text), cell-aware padding
instead of `ljust`, and `_fit` truncating on `cell_len` with the ellipsis still
appended by hand (`Text.truncate()` is a no-op on a wrapped line, per batch 55's
own note). Tests: a wide-char case beside each existing ASCII width pin,
asserting row count, padded cell width and fitted cell width.

**Not:** any change to `markdown.wrap_display`/`plain_wrap`/`display_len`, which
are correct; font or combining-mark policy beyond `cell_len`, which is the
terminal's own answer; truncation of diff *content*, where the 40-row cap stays
the bound.

## 71. A live chat turn shows neither reasoning nor tool lines; a replay of it shows both (open, deferred batch 138)

**Issue:** #225.

**S16-04, severity S4, `ux`.** `main.py:384-415` (`run_chat` drains through
`run_to_completion()` and prints notices plus `Agent:` text only), against
`:100-112` (replay prints `Thinking:` and tool lines) and `:1706-1738` (the
research CLI prints pass, tool, activity and trace lines live). Three renderings
of one run: live chat shows the answer only, replayed chat shows the answer plus
the Thinking block plus the redacted tool lines, live research shows tool lines
and volume but no content.

`core/events.py:81-86` explicitly permits a draining surface to ignore `retract`
("still correct about failures") -- that is the documented silence. There is no
equivalent sentence for `thinking_delta` or tool-call progress on the headless
path, so this silence is accidental rather than decided. D12 (the CLI is a
permanent fallback, never silently less capable where it matters) and T5 (both
shells replay through `core/replay.py` so they cannot disagree about what may be
shown) are the invariants: live chat and replayed chat already disagree inside
one shell, and chat-live and research-live disagree about tool visibility.

S4 because nothing executes wrongly and nothing is lost -- the archive has
everything and replay proves it. Deferred because the fix is a CLI-surface design
choice and the current behaviour is merely quiet, never incorrect.

**Shape:** either stream chat turns (iterate the loop generator, print tool-call
one-liners and a thinking indicator, mirroring `run_research`'s volume-only rule
for passes) or record the quiet as deliberate beside the `retract` sentence in
`core/events.py` and the `_run()` drain comment. `main.py` only, plus tests
asserting a live tool line and no live thinking content.

**Not:** changing what is persisted or replay policy (T3/T4 stay); `retract`
handling in the CLI, explicitly not required; any TUI work. Do not leak thinking
content into research-style volume lines.

## 72. The existing hub document is read, diffed, embedded and printed whole (open, deferred batch 138)

**Issue:** #226.

**S17-03, severity S3, `performance`.** `project_init/generator.py:171-186`
(`_read_existing_context`), `:279-288` (`build_diff`), `:387-396` (`say(diff)`),
`:189-219` (`_task` embeds the whole file), called from `:366-370`. There is no
bound anywhere on this path, while every other read on it is capped: tool reads at
`INIT_READ_CHARS` (20 KB, `tools/builtin/project_docs.py:201,288`), manifest
headings at 4 KB (`manifest.py:190-194`), fact files at 64 KB (`manifest.py:91-93,
337-348`). The direct `open().read()` bypasses all of it. Measured on a
5,400,009-byte `AGENTS.md` in a temp project:

```
FILE_BYTES: 5400009
READ_LEN:   5400008
DIFF_LEN:   5400079
TASK_LEN:   5400283
```

No truncation at any stage. Control: `read_project_doc` on the same file returns
at most 20,000 characters with a resume offset. Trigger is `/init` in a project
whose hub is huge -- a hostile clone, or a legitimate 5 MB hand-grown
`AGENTS.md`. The cost is paid three times over (model input billing, diff
compute, transcript render) with no notice and no cap, and the megabyte-scale row
into `RichLog`/stdout is the TUI freeze shape, arriving through a shell command.

Deferred because the fix needs a design choice this review should not pre-empt:
the revise-instruction arguably needs the whole file, so "cap like a tool read"
changes what the model sees. The honest fix is probably cap-plus-disclosure --
read the head, state the cut in both the task and the shown diff -- and that
wording belongs to the fixer.

**Shape:** bound the existing-context read with an explicit "truncated, N of M
characters shown" carried into both `_task` and the shown diff, or page it
through the same offset mechanism the tool offers. Test: a hub above the cap
produces a bounded task, a bounded shown diff and a notice stating the cut;
below the cap, byte-identical to today.

**Not:** changing tool read caps or stub rendering; a manifest listing cap, since
each listing line is already small and the count is bounded by the directory
rather than by this file.

## 73. `_tree` is the one manifest section with no second screen (open, deferred batch 138)

**Issue:** #227.

**S17-04, severity S4, `coherence`.** `project_init/manifest.py:147-160`
(`_SECRET_FILENAMES`, `_is_secret`), `:247-271` (`_tree`), `:231-236` (the
comment). `_root_documents` asks the tool's own predicate; the tree asks only
"not dotted, not `_is_secret`", and `_is_secret` covers five exact names plus
`.env*`. The comment claims this "stops a credential FILENAME (which can name a
service, an account or an environment) reaching the model at all". It stops five
of them. Measured, one file per name in a temp project:

```
TREE_HAS_ID_RSA: True        TREE_HAS_PEM: True
TREE_HAS_APIKEY: True        TREE_HAS_SECRET_KEY: True
TREE_HAS_SECRETS_TXT: True   TREE_HAS_ENV: False
READABLE: id_rsa False   deploy-key.pem False   secret.key False
```

So `.env*` and the five named files are screened, everything else is listed, and
none of it is readable -- the read side holds, which caps the harm at filenames.
The shape-9 version of the defect rather than a live leak.

`secrets.txt` being both listed and readable is the locked I2 tradeoff (`.txt` is
a readable extension by decision, not oversight) and is deliberately not filed;
operators should not keep secrets in `.txt`.

**Shape:** either extend the screen to the conventional secret shapes (key
material suffixes, bare `id_rsa`/`id_ed25519`, `*.key`) with a test per shape, or
narrow the comment to name exactly the set it covers.

**Not:** changing what is readable; content redaction; `_DOC_EXTENSIONS`.

## 74. Two headless runs build prompts with the attended catalog defaults (open, deferred batch 138)

**Issue:** #228.

**S17-06, severity S4, `test-quality`.** `project_init/generator.py:235-237` and
`core/compaction.py:1267-1268` call `system_prompt_for` with bare defaults, while
`core/loop.py:2040-2042` (pass entry), `core/reasoning/orchestrator.py:613-614`
(JSON retry), `core/loop.py:2130-2137` (chat), `agents/subagent_tool.py:281-284`
(child) and `core/reasoning/review.py:545-548` (reviewer) all forward
`advertisement_facts`. `with_catalogs` defaults are the attended answer
(`callable_only=False, granted=None`, `system_prompts.py:242-248`), and both the
initializer and the compactor run are headless in the loop's sense.

Nothing diverges today because both agents' contexts already exclude both catalog
tools -- `initializer.md` declares `allowed_tools: ["read_project_doc"]` and
`compactor.md` declares `[]` -- so each catalog is suppressed on the context axis
before headlessness matters. But that makes #68's fix rest on the *contents* of
two `.md` frontmatters, and no test drives either prompt under headless facts:
widening either `allowed_tools` to admit a gated tool silently reopens #68 ("819
characters instructing the model to use what the run hid") on exactly these two
runs. The reviewer was already moved to forwarding for this reason
(`review.py:137-143`: "the day somebody widens pipeline-reviewer's
allowed_tools"); these two were not. An unpinned invariant guarding a previously
paid-for S2.

**Shape:** forward headless facts at both call sites the way the reviewer does --
or explicitly document the reliance on context with a test asserting both
catalogs stay suppressed for these two agents under headless facts, so a widening
goes red. Test: build each prompt with `callable_only=True` and assert no
`load_skill`/`spawn_subagent` invitation survives.

**Not:** changing `with_catalogs` defaults, which the TUI call sites depend on;
changing either agent's tools.

## 75. The issue template and the prose that governs it are checked against neither (open, deferred batch 138)

**Issue:** #229.

**S18-10, severity S4, `docs-drift`.** `.github/ISSUE_TEMPLATE/audit-finding.yml`
against `docs/CONTRIBUTING.md:78-82,140`. `:140` states the relationship and the
obligation -- "section *Issue format* in this file is the prose source; the
templates are its machine-readable copy. **Change both together, or the taxonomy
drifts.**" Nothing checks either direction, and three divergences exist:

1. The prose's label rule says `enhancement` is where "most open findings sit".
   Across the 126 severity-tagged issues, `S3` + `bug` is **51** -- the largest
   single combination -- against `S3` + `enhancement` at 9 and `S3` +
   `documentation` at 16, with four carrying no label.
2. The corpus contains an `[S0]` severity and a `complexity` area the template's
   dropdowns do not offer. Both appear only on issues predating the template
   (added 2026-08-27), which is the defensible answer, and it is written down
   nowhere.
3. `docs/CONTRIBUTING.md:78` makes a label part of the format -- "**Labels -- set
   one:**", three options and a rule for choosing -- and `audit-finding.yml`
   declares no `labels:` key at all, while both its siblings do (`docs.yml:4`,
   `enhancement.yml:4`). So the one form whose findings carry a severity is the
   one that applies no label, which is consistent with the corpus's four
   unlabelled issues and means the prose's label rule has no machine-readable
   copy at all rather than a drifting one.

The drift the quoted sentence predicts has happened in the direction it did not
anticipate: not the template falling behind the prose, but the prose falling
behind what people filed. Nothing is wrong on any path -- a filer who picks the
wrong label is relabelled, which `docs/CONTRIBUTING.md:115` explicitly invites --
which is the S4 line.

**Deferred because the correction needs an owner's judgement.** The prose's label
rule is *prescriptive* and the corpus is *descriptive*, and whether 51 `S3`+`bug`
issues mean the rule is wrong or that the rule was not followed is a decision
about how this project wants to label things.

**Shape:** three parts, smallest first. Add `labels: ["bug"]` or
no-label-by-design to `audit-finding.yml`, whichever the owner's answer is, so
the prose rule has a machine-readable copy. Write the `[S0]`/`complexity` answer
into `docs/CONTRIBUTING.md` *Issue format* as two sentences (both predate the
template, the template is the arbiter, use `S1` and `coherence`). Then reconcile
the label rule with the corpus in whichever direction the owner picks. The check
that would stop a recurrence belongs in `tests/test_docs_consistency.py`: parse
the two dropdowns out of `audit-finding.yml` and assert the severity and area
lists match the enumerations in the prose. That covers the
template-against-prose half mechanically; the prose-against-corpus half cannot be
checked offline and should not be -- it is a judgement, not a count.

**Not:** changing the severity or area enumerations, which match between prose
and template today and are the half of the taxonomy that has not drifted;
`config.yml`'s contact links or the PR template, both correct.

## 76. The CodeQL job contains no mechanism that can fail it (closed, batch 158 -- PR #200 measured the check)

**S18-11, severity S4, `coherence`, harm `suspected`.**
`.github/workflows/codeql.yml:5-8,47-78`. The header states the intent --
"security red blocks the change that introduced it; quality red on the weekly run
is triaged, not reverted around" -- and `github/codeql-action/analyze@v3` uploads
SARIF to code scanning, has no fail-on-severity input, and exits 0 whatever the
alerts say. The blocking is done by GitHub's own "Code scanning results" check,
which is produced **on pull requests only** and whose failure threshold is a
repository setting. On a direct push -- which is how this branch was built, 91
commits -- alerts are recorded and nothing fails. Every `codeql` run in the
visible history is a `push` run, and every one is `success`.

So the header's first clause is true at most for pull requests. PRs are used here
(199 of them), which is why this is small, but it rests entirely on a setting
outside the repository and nothing in the tree records that it is relied on. This
scope's first question is "does each job actually FAIL the build when its tool
finds something?", and CodeQL is the one job whose answer is "not by itself"; the
other six answer yes and were each checked.

**`suspected` deliberately:** whether the PR check is configured to fail could
not be established. `gh api repos/.../branches/main/protection` returns `403
Resource not accessible by personal access token`, and the code-scanning
default-setup configuration is not readable either. The claim is bounded to what
was measured -- the workflow contains no gating and push runs cannot fail -- and
the rest is for the owner to confirm in two clicks.

**Shape:** do not add tooling. Add two sentences to `codeql.yml`'s header saying
where the blocking actually comes from: that `analyze` only uploads, that the
failing check is GitHub's "Code scanning results" on pull requests, and that its
threshold is a repository setting -- so a future reader does not look for a flag
that does not exist. Then confirm the setting (Settings -> Code security -> Code
scanning -> "Check failure" severity) and name the chosen threshold in that
comment, so it is recorded in the tree.

**Not:** a post-`analyze` step querying the code-scanning alerts API to fail push
runs. It is the only route and is more moving parts than S4 is worth;
recommended against. The security/quality split itself is sound.

**CLOSED (batch 158), by the measurement this entry said it lacked.** PR #200
was the first pull request whose analysis found anything, and its check failed:
`13 new alerts including 13 high severity security vulnerabilities`. So the PR
check IS configured to block, at least on high. One correction to the entry
above: the check is named `CodeQL`, not "Code scanning results". The shape was
followed as written -- `codeql.yml`'s header now says `analyze` only uploads,
names the `CodeQL` pull-request check as the gate and its threshold as a
repository setting, and records what PR #200 measured -- with no tooling added
and push runs still ungated. The exact threshold is still unread, because
`code-scanning/alerts` returns 403 for the token that measured this, and the
header says so rather than naming a value nobody saw. ROADMAP_v3 §75, A67. The
thirteen alerts themselves were all false positives; §75 and A66 carry how they
were cleared.

## 77. The suite's one `integration` test runs in no workflow (open, deferred batch 138)

**Issue:** #230.

**Corrected when filed (batch 159).** The `integration` marker has covered FOUR
tests since batch 150, not one, and still nothing in `.github/workflows/` passes
`-m integration`. The issue is filed with the plural.

**S19-06, severity S3, `test-quality`.** `.github/workflows/tests.yml:20-23`
against `pytest.ini:3-6`. The workflow says:

```
# `-m integration` is deliberately NOT run: pytest.ini already excludes it
# from the default invocation, and it currently fails for an unrelated reason
# (issue #10 -- the child-process detector greps a truncated `ps` line). Add
# it here when #10 closes.
```

Issue #10 closed 2026-08-24, and the fix is in the tree at
`tests/test_mcp_integration.py:68-76`, whose comment opens "#10. 'ww' disables
procps' line-width heuristic". The condition the workflow set for adding the job
was met and the job was not added. Checked across every workflow, not only this
one: `compat.yml:67`, `:91` and `publish.yml:88` all take `pytest.ini`'s `-m "not
integration"`, and nothing anywhere passes `-m integration`.

What is therefore unobserved is stated by `pytest.ini:3-6` itself: "the one
integration test spawns a real stdio MCP server to prove §17 AC8 (no orphaned
children), which cannot be shown in-process."
`tests/test_mcp_integration.py`'s docstring is more specific -- "the failure mode
is orphaned subprocesses accumulating across sessions. That is invisible to every
other test here." So the one guarantee that cannot be shown in-process is pinned
by exactly one test that runs only when a human types the command.

Shape 2 applied to a test rather than a feature. S3 because nothing is wrong
today and the property is at least checkable on demand, with the in-memory
transport in `test_mcp_client.py` covering the bridge, normalisation and the
cancel-scope fix. Deferred because adding a CI job that spawns real subprocesses
is a decision about what CI is allowed to do, not a repair -- item 18 (*CI
workflow follow-ups*, open) is the natural home, and this is reported separately
only because none of its four bullets mentions the `integration` marker.

**The fixture is not the work**, which was checked so a fixer knows:
`tests/stdio_server_fixture.py`'s three lines of API -- `from mcp.server import
MCPServer`, `@mcp.tool()`, `mcp.run()` -- all resolve against the pinned
`mcp==2.0.0`, and `run()` defaults to `stdio`, the transport the test's
`ServerConfig` asks for.

**Shape:** a separate job in `tests.yml`, not a flag on the existing one, because
that job's whole premise is `pytest.ini:3`'s "no process, no socket" and mixing
them loses the statement:

```
  mcp-integration:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps: <the same four fresh-clone steps>
      - run: python -m pytest -m integration -q -n0
```

`-n0` because the suite is one test and xdist would spawn a worker per core for
it. The by-hand equivalent is `python -m pytest -m integration
tests/test_mcp_integration.py -q -n0 -p no:cacheprovider`. Then replace
`tests.yml:20-23`'s comment with a sentence saying the job exists. **If the
decision is that CI must not spawn subprocesses, the comment still has to
change** -- it currently gives a reason that has expired, which is worse than
giving the real one.

**Not:** moving the test out of the `integration` marker. `pytest.ini:3-6`'s
offline default is a locked decision and the marker is what implements it.

## 78. `FakeStorage` makes eleven mirror claims and two are compared (open, deferred batch 138)

**Issue:** #231.

**S19-07, severity S3, `test-quality`.** `tests/test_fake_storage_mirror.py:126`
and `:167` against `tests/conftest.py:1251` (`pinned_through`) and `:1275`
(`_turn_opener_index`). `FakeStorage` makes about eleven separate claims, in its
own docstrings, that a method mirrors a named function in `storage.py`. The file
issue #123 created to turn those claims into checks compares exactly two:
`_reconstruct` against `storage._to_neutral`, and `_split_at` against
`storage._split_at`.

Unchecked, in rough order of how much algorithm each duplicates:
`pinned_through` (`conftest.py:1251`, claiming it mirrors production "INCLUDING
the tool-result re-inclusion that makes the pinned span whole (#88) and batch
120's turn opener"), `_turn_opener_index` (`:1275`), `mark_turn_failed`
(`:1316`), `list_threads`/`_activity` (`:1137`, claiming the COALESCE ordering
"exactly as production's ORDER BY does"), `child_threads` (`:1100`, with the id
tiebreaker), `list_memories` (`:1341`, AC6 filtering), `history_through`
(`:1288`), `thread_preview` (`:1170`) and the three `#31` plain-dict claims.

The two in the middle are the ones worth naming, because they are not shape
claims -- they are transcriptions of a published algorithm.
`tests/conftest.py:1251-1273` is a line-for-line copy of `storage.py:814-889`:
the same `keep` list, the same `have`/`need` sets, the same `not pinned and role
== "tool"` re-inclusion, the same `sorted(set(keep))`. `:1275-1286` is a
line-for-line copy of `storage.py:786-812`. Diffed by hand, both agree today, so
this is a missing guard and not a live divergence.

The fake's own docstring states the invariant the arrangement breaks, about
itself: "a fake that returns the pinned rows alone cannot represent the orphaned
`tool_use` that fix exists to prevent ... and **a test passing against it would
prove only that the fake and the code agree.**" It avoided that by copying the
algorithm -- which makes "the fake and the code agree" true only for as long as
someone keeps copying. `AGENTS.md:2865` makes it a convention ("if you change
`storage.py`'s reconstruction, change the fake identically") and
`test_fake_storage_mirror.py:22-25` says why a convention is not enough: "this
file is the comparison. It turns a convention somebody has to remember into a
check." It turned two of eleven.

The measurement that makes this more than tidiness is #123's own, quoted at
`:12-16`: mutating `_reconstruct`'s assistant branch to emit `"tool_calls": []`
survived the whole suite (1406 passed), and so did `_split_at` returning
`len(rows)` for an unfound watermark. There is no reason to expect a mutation of
`pinned_through`'s re-inclusion loop to behave differently, and the defect it
would reintroduce is audit #88 -- an assistant turn reaching the provider with a
`tool_calls` block whose matching `tool` row was left behind, which
`storage.py:839` calls "an HTTP 400 on Anthropic -- on a thread that was working
a moment ago."

S3 and not S2 because the two implementations agree today and the real one is
additionally pinned against real SQLite in `test_storage_e2e.py`. Deferred
because this is new test code with a negative control per method, which is a
batch rather than a patch.

**Shape:** a third class in `test_fake_storage_mirror.py`, using the shape that
file already chose and for its stated reason ("pure functions -- no Session, no
database"): `storage._turn_opener_index` is pure, so it compares directly over a
table of `(rows, span)` pairs -- turn-aligned, mid-turn, empty span, span ==
rows, and a span whose kept prefix has no `user` row at all.
`storage.pinned_through` is not pure (it reads `_ordered_rows`), so the honest
comparison extracts the keep-set computation into a module-level helper taking
`(rows, span, neutral)` -- the same move `_to_neutral` and `_split_at` already
represent, and what makes them comparable. The alternative, driving both sides
through `test_storage_e2e.py`'s `real_storage`, needs no production change but
puts the comparison in the module-scoped-fixture file, which `pytest.ini:54-57`
constrains. For the remaining nine, add comparisons only where the fake
re-derives logic (`list_threads`' COALESCE, `child_threads`' tiebreaker,
`list_memories`' AC6 filter) and leave the `#31` plain-dict claims alone -- the
existing reconstruction comparison covers those transitively.

**Not:** `save_checkpoint`'s divergence. `AGENTS.md:2865-2867` names it as
deliberately current and argues the cost is zero today because the guard is
pinned against real SQLite and the fake's version is only ever used to arrange a
compacted view. That argument is sound and is not reopened.

## 79. `test_the_tree_names_every_production_module` quantifies over a typed list (open, deferred batch 138)

**Issue:** #232.

**S19-08, severity S4, `test-quality`.** `tests/test_docs_consistency.py:378-430`.
The gate iterates `_MAPPED_DIRS`, sixteen directory names someone typed:

```python
_MAPPED_DIRS = (
    ".", "bin", "scripts", "core", "core/reasoning", "prompts", "security",
    "safety", "mcp_client", "agents", "skills", "memories", "project_init",
    "tui", "tools", "tools/builtin",
)
```

A module in a directory the tuple does not name is never looked at, so the gate
reports "every production module is in the tree" while quantifying over a
hand-written list. The failure path is ordinary: add `plugins/loader.py`, forget
the tree entry in `docs/ARCHITECTURE.md`, forget `_MAPPED_DIRS`, and the gate
stays green -- which is the state the production half was in before batch 51
added this test, when it "had quietly lost eight modules and two whole
directories ... and bin/ and scripts/ entirely" (`:390-396`).

**Measured: there is no live gap.** Walking the worktree with the same
`_SKIP_DIRS` and excluding `tests/`, every directory holding a non-`__init__.py`
`.py` or `.mjs` is already in the tuple. The control that proves the probe can
see a directory when there is one: removing `"tools/builtin"` from the probe's
copy makes it report that directory with fifteen modules. So this is a latent
hole in a gate, not a missing module.

The file's own header states the rule it breaks: "every check here compares a
document against a live value -- the collected session, the registry, the
permission dataclasses -- **never against another hand-written number.**" The
sibling gate one function up shows what "live" means here:
`test_the_tree_states_a_correct_count_for_every_collected_test_file` takes its
domain from `_collected_nodeids(request)`, so a new test file cannot hide from it.
The production half took a list instead.

**Shape:** derive the directory set from the walk and keep the tuple as an
*exclusion* list rather than an inclusion one -- walk `_walk_project()`, skip
`tests/`, require a tree entry for every `.py`/`.mjs` found, with a short named
allowlist for anything deliberately unlisted. That inverts the failure: an
unlisted directory becomes a test failure naming it. If the tuple is kept, the
cheap mitigation is to assert the reach -- `assert not <dirs with production
files outside the tuple>`, which is four lines of the probe above.

**Not:** auditing `docs/ARCHITECTURE.md`'s tree contents, a document-wide sweep
already covered for the test half in both directions; the fence split at `:406`,
which reads the document's first fenced block and would be a finding if the
`assert entries` guard at `:409` were absent -- it is present, so a moved or
reformatted tree fails loudly.

## 80. Five once-per-process globals are reset only by local fixtures (open, deferred batch 138)

**Issue:** #233.

**Corrected when filed (batch 159).** `_sudo_rejected` has been reset generally
since batch 142 (S02-10's fix: `isolate_sandbox_probes` calls
`sandbox._reset_sudo_quarantine()`), so FOUR globals remain, not five. The
sharpest instance -- `clear_client_cache` missing `_effort_levels_cache` -- is
unchanged.

**S19-09, severity S4, `test-quality`, harm `suspected`.**
`tests/conftest.py:462` (`clear_client_cache`) and `:681`
(`isolate_sandbox_probes`). That second fixture is the one batch 107 wrote after
measuring that the suite's result depended on whether Docker Desktop happened to
be running, and its docstring states the general problem and, in the same breath,
that the problem was general and the fix was not: "the only resets were fixtures
scoped to single classes in the files that test the probes themselves ... **with
nothing applying it generally.**"

Five module globals are still in exactly that state, enumerated by parsing every
production module for module-level mutable assignments and cross-checking against
the ten autouse fixtures:

| global | what it holds | reset by |
|---|---|---|
| `core/client.py:556 _effort_levels_cache` | `(provider, model) -> effort levels`, once per process | `test_client_effort.py:43`, `test_context_window.py:37`, `test_sdk_conformance.py:250` -- local only |
| `core/compaction.py:121 _context_window_warned` | models already warned about | `test_compaction.py:64` only |
| `core/loop.py:95 _headless_notices_shown` | hidden-tool sets already logged | `test_agents.py:271`, `:298` only |
| `security/permissions.py:96 _dynamic_approval_defaults` | D15 runtime per-tool approval defaults | `test_mcp_client.py:107` only |
| `security/sandbox.py:1323 _sudo_rejected` | backends whose stored sudo password was refused | `test_sudo.py:116`, `:118`, `:862` only |

Eight other globals of the same kind *are* reset generally, which is what makes
these five stand out rather than being an absence of policy: `_client_cache`,
`_context_window_cache`, `_context_window_failures` and `core.session`'s
`_overrides` by `clear_client_cache`; `config_schema._cached` by
`restore_config_schema_cache`; the loader state by `clear_config_loader_state`;
`scholar._cache` by `clear_scholar_cache`; and `arxiv._cache`,
`web_search._cache`, `fetch_url._body_cache` by `clear_network_tool_caches`.

**The sharpest instance is inside `clear_client_cache` itself.** It clears three
of `core/client.py`'s four once-per-process caches by name and misses the fourth,
declared six lines above one of the three it does clear.
`core/client.py:557` describes `_effort_levels_cache` as "populated lazily; the
Anthropic branch queries the API once per process rather than per call", and
`:563-565` describes `_context_window_cache` as "same lazy, once-per-process
posture as the effort cache above". Two caches with one stated posture, and the
fixture that exists to stop that posture leaking clears one. The leak the fixture
names -- "so a previous test's queued fake responses, or a window it primed, do
not leak into the next one" -- is available through the cache it misses:
`clear_client_cache` drops `_client_cache` so the next test builds a fresh fake
client, but the effort answer the *previous* fake client produced is still cached
under `(provider, model)` and is returned without the new client being asked.

The harm is the one batch 107 measured for the sixth global and
`tests/test_probe_isolation.py` states as general: "collection order decides who
runs the one live probe", so "which tests fail there is decided by collection
order" and "adding a test file moved the cohort". Under `--dist loadscope` the
cohort is also decided by which groups a worker happens to take, which
`pytest.ini:36` and `AGENTS.md:68` both call a "scheduling lottery".

**`suspected`: no currently-failing path was constructed.** Each of the five has
a local fixture in the file that tests it, so the tests that assert on
first-time behaviour are protected today. What is unprotected is any future test
elsewhere -- and for `_effort_levels_cache`, any test anywhere that drives the
real `effort_for` and then expects a differently-configured fake client to be
asked. Twelve test files reference `effort_for` and most patch it; it was not
established that any reaches the real function.

**Shape:** two steps, cheapest first. (1) Add
`core.client._effort_levels_cache.clear()` to `clear_client_cache`'s two halves
and name the fourth cache in its docstring -- two lines, and it closes the one
instance where a fixture already owns the file and misses a sibling. (2) For the
other four, `isolate_sandbox_probes` is the precedent: one autouse fixture that
resets process-scoped memos, each entry carrying the reason it is there. The
argument for doing it generally is the one that fixture already made and won; the
argument against doing all four at once is that each has a *meaning* --
`_sudo_rejected` is a security quarantine, and `_headless_notices_shown` is
deliberately keyed on the hidden set rather than on a flag
(`core/loop.py:88-94`) -- so a blanket reset should be a reviewed list, not a
loop over module attributes. A gate is also available and in this project's
idiom: a test asserting that every once-per-process memo named in a list is
cleared by an autouse fixture, the way `test_no_test_module_defines_its_own_settle`
did for `settle`. That is a larger decision than the fix.

**Not:** a reset for `prompts/system_prompts.py:12 passes_source_files` (loaded
once from disk, never mutated per test) or `mcp_client/registration.py:52
_registered` (checked: no test calls `register_all` against the global registry,
so it cannot leak).

## 81. Twenty-four env-gated skips are the guarantees nobody here has observed (open, deferred batch 138)

**Issue:** #234.

**S19-10, severity S3, `test-quality`.** `tests/test_ssh_backend.py:72`,
`tests/test_sudo.py:82`, `tests/test_wsl_backend.py:66`, against
`.github/workflows/tests.yml` and `compat.yml`. Driven:

```
$ python -m pytest tests/test_wsl_backend.py tests/test_ssh_backend.py \
      tests/test_sudo.py -n0 -p no:cacheprovider -q -rs
409 passed, 24 skipped in 32.08s
```

Every one of the 24 is env-gated on an `all()` over variables **no workflow
sets** -- `grep -rn "VEN_TEST_" .github/workflows/` returns nothing. Of the 34
skip sites in the suite, the platform ones are covered by the union of two
workflows (`tests.yml` runs everything on `ubuntu-latest`; `compat.yml`'s
`windows-subset` runs `test_shell.py` and `test_posture.py` on
`windows-latest`), and the by-design ones in `test_docs_consistency.py` are
narrowed invocations. The three uncovered rows are the 18 env-gated SSH skips,
the 6 env-gated sudo-over-SSH skips, and the 3 gated on a live `wsl.exe` probe
that short-circuits on non-Windows.

`tests/conftest.py:639-670`'s `_sandbox_probe_defaults` closes the other door on
the same guarantees: the autouse `isolate_sandbox_probes` installs
`WslProbe(None, "...did not answer, so WSL is not installed...")` and
`SshProbe(None, reason="no host is configured...")` for **every** test, with the
stated reason that "WSL and SSH go the other way for the same reason -- off by
default in `config.yaml`, so 'nothing configured' is the state the suite
assumes." That is a correct default, and it means the suite's other ~5,900 tests
cannot observe a configured WSL or SSH backend either. So the live paths of
ROADMAP_v3 §49 slices 3, 4 and 5a -- SS35-SS64, two whole execution backends, an
SSH credential store and sudo-over-SSH -- are covered by 24 tests gated on
variables nothing sets, plus 3 gated on a background service, inside a suite that
otherwise pins the "nothing configured" branch everywhere.

S3 because these are guards that do not guard on a reachable shipped path --
`config.yaml` can turn either backend on. Not S2, because nothing has been shown
to be wrong and the 409 passing mocked tests do cover argv construction, path
translation and the notices. Not S4, because the shape is "a set of guarantees
nobody has observed" rather than a stale claim. Deferred because standing up an
SSH host and a WSL distro in CI is an infrastructure decision; item 18 is where
CI follow-ups live.

**Shape:** the useful output is a written statement rather than a test, because
the fix is infrastructure. (1) Write the inventory where a reader will meet it:
`AGENTS.md`'s Testing section states the offline contract and the fresh-clone
requirement but not that 24 backend tests are env-gated and no automatic run sets
the variables. One paragraph naming the variables, the count and what is
therefore unverified turns a silent skip into a known gap -- which is what this
project did for the `markitdown` and `providers.json` cases. (2) If any of it is
to be covered, SSH is the cheap half: an `ubuntu-latest` job can `ssh-keygen`,
add its own key to `~/.ssh/authorized_keys` and point `VEN_TEST_SSH_*` at
`localhost`, exercising the real client, the real host-key handling and the real
path translation without a second machine. WSL needs a `windows-latest` runner
with a distro installed and is a larger decision.

**Not:** turning the env gates into hard failures. A contributor without an SSH
host must still be able to run the suite and the skip is the right mechanism;
what is missing is that nothing in CI ever supplies the variables.

## 82. The approval-timeout fixture is pinned by its value, not its effect (open, deferred batch 138)

**Issue:** #235.

**S19-11, severity S4, `test-quality`.** `tests/conftest.py:353-373`
(`shrink_approval_timeout`) and `tests/test_pilot_wait.py:287-302`, against
`tui/app.py:4232`. The autouse fixture that stops an unanswered modal from
stalling the suite for ten minutes sets `config.ATTENDED_APPROVAL_TIMEOUT_S = 5`,
and its docstring states the contract the whole arrangement rests on: "**read at
call time, so patching the module attribute is enough.** Nothing asserts on the
value or on the rendered '600s' string." The pin is:

```python
assert config.ATTENDED_APPROVAL_TIMEOUT_S <= 10, (
    "an unanswered modal will stall the suite instead of failing it -- "
    "is the shrink_approval_timeout fixture still autouse?")
```

That asserts the fixture **ran**. It does not assert that the production reader
at `tui/app.py:4232` -- `answer = channel.get(timeout=config.ATTENDED_APPROVAL_TIMEOUT_S)`
-- still reads the attribute at call time. Move that expression into a default
argument on the path to `_blocking_modal` and the default binds at import, before
any fixture runs: the fixture keeps setting the attribute, the pin keeps passing,
and an unanswered modal goes back to parking for 600 seconds.

**That is not a hypothetical mutation.** It is recorded twice as having actually
happened, in the sibling reader. `docs/ARCHITECTURE.md` §11: "**a default
argument is evaluated at import, which can silently disable a fixture.** ...
writing `timeout=config.ATTENDED_APPROVAL_TIMEOUT_S` as a *default* takes that
away without turning anything red -- **the fixture keeps running and stops
working.**" `docs/ROADMAP_v2.md:2049` (N2) says what was done: "the default is a
**sentinel**, not the config constant." So `main.py:548` got a `_DEFAULT_TIMEOUT`
sentinel resolved in the body *and* a test stating the contract. The TUI reader --
the one the fixture's docstring actually names, and the one whose 600 s park
`docs/ARCHITECTURE.md:818` calls "indistinguishable from forever under pytest" --
got neither. The consequence is the failure mode `:1860` describes: mutations and
dismissal bugs present as a hung suite rather than a red test, which batch 23
paid for twice.

S4 because nothing is wrong today and the harm is a loss of failure legibility
rather than of correctness. It is here because the fixture's own docstring makes
a claim about production that nothing checks, and because the exact regression has
a recorded precedent one module over.

**Shape:** assert the effect rather than the value, in the test that already owns
this fixture -- patch `config.ATTENDED_APPROVAL_TIMEOUT_S` to ~0.05, call the
real `_blocking_modal` path against a channel nothing puts to, and assert it
returned in well under a second. A `queue.Empty` arriving promptly is the
observable; a 600 s park is the regression. The existing value assertion stays --
it answers a different question ("is the fixture still autouse") and its failure
message is the right one for that. If driving the modal is too heavy for
`test_pilot_wait.py`, the structural alternative is to copy N2's remedy: give
`_blocking_modal` a `_DEFAULT_TIMEOUT` sentinel resolved in the body and pin
*that* with the same test N2 wrote for `main.py`. That removes the mutation
rather than detecting it, which is the better trade and is already this project's
chosen answer for the sibling reader.

**Not:** reviewing `_blocking_modal`'s dismissal paths, the `on_timeout`
narration, or whether every dismissal produces a boolean -- item 5 is open as
that brief. The `settle`/`pump` pair was read and is sound.

## 83. The spawn sign-off has no reachable configuration on the shipped roster (closed, batch 139 -- ruled a deliberate scope and recorded)

**Not from any report.** This one came out of the batch-137 fix pass (A16,
`spawnable` becoming a refusal), when three tests broke because they drove
`subagent_tool.run` with `grill-me` and retargeting them had nowhere to go.
Severity `S3`, `coherence`: a consent mechanism that cannot be reached by the
configuration that ships. It was true before batch 137 and is a property of the
shipped whitelists rather than of A16.

**Measured, after the triage note for it got the mechanism wrong.** That note
said the gated tools a sign-off would offer "are globally denied". They are not.
Driven against the real roster with `HOME` redirected:

```
SPAWNABLE: 6 -> ['build', 'explore', 'general', 'review', 'test', 'writer']
candidate_approvals: []  for all six
WIDE-OPEN CONTEXT candidate_approvals: ['write_project_doc']
  write_project_doc is_tool_allowed(bare ToolContext) = True
does any spawnable agent declare write_project_doc? build no, explore no,
  general no, review no, test no, writer no
synthetic spawnable agent declaring write_project_doc -> ['write_project_doc']
```

So the real mechanism is narrower and sharper than "globally denied":
**`write_project_doc` is the only gated-and-grantable tool there is, global
policy allows it, and not one of the eleven shipped agents declares it** --
spawnable or not. The other candidates never reach the list at all: `shell` and
the file tools carry a per-call `approval_check`, which §25 R2 filters out of
every grant path by name, and `remember` plus `spawn_subagent` are
`GRANT_NEVER` by M17 and R4.

**And "on a stock install" understates it, which a second measurement
settled.** Driven again with every tool globally enabled -- the permissions
dataclass swapped for one that answers `True` to everything:

```
=== STOCK ===                  === EVERY TOOL GLOBALLY ALLOWED ===
  build     []                   build     []
  explore   []                   explore   []
  general   []                   general   []
  review    []                   review    []
  test      []                   test      []
  writer    []                   writer    []
                                 (no agent) ['write_project_doc']

the whole gated-with-no-approval_check population:
  remember             grantable=True  policy=never
  spawn_subagent       grantable=True  policy=never
  write_project_doc    grantable=True  policy=signoff_only
```

An operator cannot make this reachable, because global permissions are not
what gates it -- the agent's own `allowed_tools` declaration is. The candidate
set is `(the agent's declared tools) ∩ {write_project_doc}` for every
configuration of `config.ToolPermissions`, so **the §18 sign-off cannot fire for
any shipped agent under any configuration.** What changes it is a declaration,
and that is a roster edit rather than a setting.

**The structural statement, which is the one worth taking to a decision.**
`GRANT_SIGNOFF_ONLY` has **exactly one member in the entire registry** --
`write_project_doc` (`tools/registry.py:1030`) -- and that policy value exists
for this sign-off and nothing else. #133 put it there to separate the two grant
paths, and argued the separation from attendedness rather than from the tool:
against the pipeline, "one `--grant-tools write_project_doc` authorises 15
document names ... for the rest of a ten-pass run that is reading
attacker-controlled web pages"; for the §18 sign-off it "stays offered: a human
is answering in the moment, about one named agent, for one turn." So the one
policy value created to make a tool offerable in this sign-off, and the one
tool that holds it, together describe a mechanism no shipped agent can reach.
The argument is sound. It has no instance.

`grill-me` was the one agent with a non-empty candidate set, and only because it
declares `allowed_tools: None` -- no opinion, so global policy applies and it
inherited `write_project_doc`. It is `spawnable: false`. **So before A16 the
shipped spawn sign-off fired only when a non-spawnable agent was spawned, which
is exactly the defect A16 closes.** The synthetic control proves the mechanism
itself works: a spawnable agent that declares `write_project_doc` offers a
sign-off immediately.

**Half of this was already pinned, and framed as intended**, which is why the
entry exists rather than a bug report. `tests/test_shipped_roster.py:225-247` and
`:293-300` already assert `candidate_approvals(child) == []` for `explore`,
`review`, `build` and `test`, and argue it as a design property: "the one yes
that starts the spawn creates no standing authority, and each non-inert command
still reaches the person who gave it." That argument is sound per agent. What
nothing pinned is the *global* claim -- that it holds for all six, which is what
turns a narrow sign-off into an unreachable one. `writer` and `general` were the
two unmeasured ones, and `writer` is the striking case: the agent whose name most
suggests it would hold `write_project_doc` declares no writing tool at all.

**Batch 138 pins the global claim** rather than changing behaviour, because which
way to resolve it is the owner's: `TestTheSpawnSignoffIsUnreachableOnTheShippedRoster`
sweeps every spawnable agent and goes red the day one declares a grantable gated
tool -- at which point this entry is what the reader should come back to.

**RULED (c) BY THE OWNER, batch 139: the subset picker is for
operator-configured installs, and that is now written where the mechanism is
described rather than only here.** Three answers were offered and they were not
equivalent. (a) Accept it -- a sign-off with nothing to offer is correct
fail-closed behaviour, and this becomes an *Accepted risks* note. Not chosen:
the picker is not merely narrow, it is unreachable, and that belongs beside the
mechanism rather than in the register alone. (b) Give one spawnable agent
`write_project_doc`. Not chosen, and **it was never a roster tweak**:
`writer` is the only plausible candidate, its frontmatter says "No
`write`/`edit` by omission: this agent returns prose; whoever delegated to it
decides where it lands", and its body has a section headed *Return prose, not a
file operation* ("Where you put it is the delegator's decision, not yours"). So
(b) reverses a stated decision the way A16 did and would want its own batch and
its own decision record; R4's compounding-authority argument does not reach
`write_project_doc`, so the objection is the agent's contract rather than the
permission. (c) Decide the sign-off is for
operator-enabled installs only and say so where §18 describes it, which costs two
sentences and no behaviour. **This is what shipped.**

**What batch 139 did.** Four places now carry the scope, each chosen because it
is what a reader consults when deciding whether the picker is something they
would see: `docs/ROADMAP_v2.md` §23's AC1b status (the record of what
shipped), `AGENTS.md`'s R13 bullet and a new sibling to its "`candidates()` is
empty on a default install, by design" bullet, `tools/registry.py`'s
`GRANT_SIGNOFF_ONLY` registration comment, and
`agents/manager.py`'s `candidate_approvals` docstring.
`tui/screens.py`'s `SubagentSignoffScreen` was deliberately left alone -- its
docstring describes the screen accurately and its own comment already says the
no-candidates branch is reachable ("either can be reached from inside a run").
It is the one place that got this right.

Two tests pin it, both driving the **production entry point** rather than the
helper, which is the gap that let this exist:
`test_request_payload_offers_nothing_for_every_shipped_agent` (what
`core/loop.py` actually asks the registry for) and
`test_the_picker_is_reachable_only_for_an_agent_that_declines_to_narrow`, which
registers an MCP-shaped tool and measures all three cases -- the six narrowed
agents offer nothing, an agent declaring no `allowed_tools` offers it, and one
naming it outright offers it. So "operator-configured" is a measurement rather
than a sentence.

**One claim in the batch-138 version of this entry did not survive
inspection, and it was mine.** It said the picker branch "is only ever tested
with inputs that cannot occur", citing
`SubagentSignoffScreen("researcher", ["web_search"])`. Read in context, those
four sites are parametrised SCREEN-SHAPE tests -- they assert which CSS dialog
renders and which button takes focus, across every modal the app has -- so a
placeholder agent and a placeholder tool are the right arguments and assert
nothing false. What was genuinely missing was an end-to-end test from the real
roster, and that is what the two above are. The criticism was aimed one layer
off.

**Not:** widening `candidate_approvals` to stop excluding per-call
`approval_check` tools. §25 R2 excludes them deliberately, and the stated reason
is that leaving them in "would make the sign-off notice promise authority the
grant does not carry, which is worse than not offering it."

## 84. The test that pins R2's grant filter cannot fail from its removal (open, found and filed batch 138)

**Issue:** #236.

**Not from any report either.** Batch 138's mutation pass turned this up while
scoring the guard for item 83. Severity `S4`, `test-quality`.

Replace `if registry.grantable(name)` with `if True` in
`AgentManager.candidate_approvals` (`agents/manager.py:166-170`) and measure
which tests notice:

```
test_grants.py -k never_offers_a_param_gated   A1 -> SURVIVES  (1 passed)
test_grants.py (all 56)                        A1 -> SURVIVES  (56 passed)
test_research_authorization.py -k offers_a_subset_of_what_the_signoff
                                               A1 -> KILLS     (1 failed)
```

`tests/test_grants.py:1069-1087` is
`test_the_signoff_never_offers_a_param_gated_tool`, and its docstring states
exactly the property the mutation removes: "`candidate_approvals()` filters by
`grantable()`, so shell/write/edit can never appear in the modal the user
ticks. Checked against every shipped agent AND against a child with no tool
restriction at all, **which is the widest case the filter ever sees**." It
cannot fail from the filter's removal.

**Why, measured.** The test asserts an empty intersection with `{"shell",
"write", "edit", "read"}`, and all four are denied by global config on a stock
install. `registry._advertised` (`tools/registry.py:164-172`) consults
`is_tool_allowed` FIRST, so those four never reach `headless_hidden` at all,
and `grantable()` is never asked about them. Setting `wide.allowed_tools =
None` removes the AGENT-level narrowing and not the global denial, so "the
widest case the filter ever sees" is not the widest case: the configuration
the filter is actually for is a non-grantable tool that global policy
**allows**, and the shipped config has none.

So the property is genuinely pinned -- by
`test_research_authorization.py::TestCandidates::test_the_pipeline_offers_a_subset_of_what_the_signoff_does`,
which registers its own tools and therefore has one. That test is about the
pipeline's subset relation; it kills this mutation incidentally. The test
written to pin the filter is the one that cannot.

This is the vacuity family `docs/REVIEW_MARATHON.md` asks reviewers to look
for -- "a test that cannot come out the other way" -- in a test whose
docstring claims the opposite, and the S19 scope did not find it because
nothing in the suite was mutated during the marathon. It is recorded here
rather than fixed because batch 138 is a bookkeeping batch; the fix is small
and should not be mistaken for a large one.

**Shape:** three lines in the existing test. Register a param-gated tool that
global policy ALLOWS -- the `gated_pair` fixture in the same file already
builds exactly this (`mcp__t__paramwise`, with `approval_check=lambda name,
params: True`, named `mcp__*` so it gets the approval-required default without
editing config dataclasses) -- and assert it is absent from
`candidate_approvals` while the plain sibling is present. The present-sibling
half is what makes it non-vacuous: it proves the helper is answering rather
than empty.

**Not:** removing `grantable()` from `candidate_approvals`. It is load-bearing
on an operator-enabled install, which is the whole point above, and R2's
argument for it is stated at `tools/registry.py:414-433`. Also not widening
the four names the existing assertion uses: they are the right names for the
claim "the modal the user ticks never offers a per-call tool", and the gap is
the missing allowed case, not the chosen denied ones.

## 85. Six pilot tests assert worker-thread state after a raw `pause()` (closed, batch 151; ONE of the six needed it, and this entry was wrong about why)

**Found by batch 138's full-suite run**, which is the third batch to pay for
this and the first to measure it. Severity `S3`, `test-quality`.

`tests/test_tui.py::TestTheQuitGesture::test_quitting_by_gesture_releases_a_blocked_worker`
failed once in a 635-second run -- against the 86 seconds `README.md` documents
-- and passed in every other configuration tried: alone (10 passed), in its own
class under `-n0`, three times under a narrowed `-n auto` selection, and once
with the whole of `tests/test_tui.py` under `-n auto --dist loadscope` (443
passed). **Not reproduced**, and that is stated rather than dressed up.

What the test does:

```python
await pilot.press("ctrl+c")
await pilot.pause()
await pilot.press("ctrl+c")
await pilot.pause()

assert channel.puts == [False], (
    "quitting by ctrl+c left the worker blocked on the permission channel")
```

`channel.puts` is written by a **worker thread** -- the quit gesture has to
reach `exit()`, which calls `_release_permission_channel()`. The test waits for
that with two raw `pause()` calls, and `AGENTS.md`'s rule says that is exactly
what `pause` is not for: *"`await pump(pilot, n)` gives the loop `n` turns and
is for NEGATIVE assertions only ('prove X did not happen'), where there is no
predicate and a deadline would make every such site pay its full timeout."*
`tests/conftest.py:249-250` carries the measurement that makes it matter:
`pilot.pause()` costs **21ms with nothing burning CPU in-process and 1021ms
with one busy sibling thread -- a 46x swing** -- because its exit condition is
a process-wide CPU heuristic. A 7x-slower suite run is the condition that
measurement describes.

**Measured: six tests in that file have this shape** -- their last wait before
a positive assertion is a raw `pause()`/`pump()` rather than a `settle()`
predicate, AND the thing asserted is state only another thread can produce:

| test | assertion |
|---|---|
| `test_quitting_with_a_prompt_open_releases_the_blocked_worker` | `channel.puts == [False]` |
| `test_quitting_by_gesture_releases_a_blocked_worker` | `channel.puts == [False]` |
| `test_a_dead_worker_leaves_a_traceback_in_the_log` | `matching` (a log record) |
| `test_resuming_refreshes_the_todo_panel` | `spy.called` |
| `test_a_failure_listing_threads_does_not_kill_the_tui` | `running` |
| `test_the_request_carries_the_thread_on_screen` | `request.thread_id == thread` |

The two `channel.puts` ones are a pair -- the same ARCHITECTURE §4.14 property
("every dismissal must put a boolean on the channel") reached by two different
keys -- so whichever of them a given run schedules against a busy sibling is
the one that fails. **A first pass over the file counted 119 of 319 tests
asserting positively after a raw `pause()`, and that number is wrong to act
on:** almost all of them assert synchronous state (`app.theme == "ember"`
after a command dispatch) which no other thread is involved in producing. The
cross-thread subset is the risk, and it is six.

**Why this is `S3` and not `S4`.** Nothing in production is wrong. But the
failure mode is the one `docs/ARCHITECTURE.md:1860` names and batch 23 paid
for twice: a real defect in a dismissal path presents as a flaky suite rather
than a red test, so the next person to see this failure does what the last two
did -- re-runs it, gets green, and learns to discount the file. That is the
cost item 7's narrowness rule is usually protecting against, arriving from the
other direction.

**Shape:** replace the trailing `pause()` with the predicate the assertion
already states -- `assert await settle(pilot, lambda: channel.puts == [False])`
-- in the six sites above. `settle` exists for this, the file imports it
already, and the two quit tests use it correctly fifteen lines earlier for
`isinstance(app.screen, PermissionScreen)`. No new mechanism, no new fixture,
and the negative-assertion `pump` sites stay exactly as they are. Cheap enough
to fold into whichever batch next touches `tests/test_tui.py`.

**Not:** a retry plugin, or raising the pause count. Both make the symptom
rarer and the diagnosis harder, and `pytest-rerunfailures` is not a dependency
of this suite. Not auditing the other 113 positive-assertion-after-pause sites:
they are synchronous, and widening this entry to them would turn an actionable
six into a file-wide sweep nobody will do.

---

### RESOLVED in batch 151 -- at one site, because five did not need it

**The shape above is real and the reason given for it is not.** The rule has
two halves: *the last wait before a positive assertion is a raw
`pause()`/`pump()`* -- a source pattern, true at all six -- and *the thing
asserted is state only another thread can produce*, which is a claim about
the code and is **false at five of the six**. Each producing call was read in
`tui/app.py`:

| test | what produces the asserted state | verdict |
|---|---|---|
| `test_quitting_by_gesture_releases_a_blocked_worker` | a POSTED key, handled by the message pump | **needs a predicate** |
| `test_quitting_with_a_prompt_open_releases_the_blocked_worker` | `exit()` calls `_release_permission_channel()` inside itself | synchronous |
| `test_a_dead_worker_leaves_a_traceback_in_the_log` | `on_worker_state_changed` is called by hand; `logging` fills `caplog` on this thread | synchronous |
| `test_resuming_refreshes_the_todo_panel` | `switch_to_thread()` calls `refresh_todo_panel()` directly | synchronous; the `pump` serves the NEGATIVE assertion between them |
| `test_a_failure_listing_threads_does_not_kill_the_tui` | every read from `action_pick_thread()` down is on the UI thread, by that function's own comment | synchronous; both assertions negative |
| `test_the_request_carries_the_thread_on_screen` | `restart_for_config()` calls `self.exit(RestartRequest(...))`, which sets `return_value` before returning | synchronous; the `async with` exit awaits shutdown besides |

**Driven rather than read.** `Pilot.pause()` was replaced with a bare
`asyncio.sleep(0)` -- the idle heuristic gone, the loop still able to turn --
and **all six passed**. `settle` keeps working under that patch because it
polls, which is what lets the probe tell the two populations apart instead of
merely breaking everything; the probe's reach was proved by sabotaging the
same monkeypatch and watching the tests fail inside `Pilot.pause`. So at five
sites there is nothing for a predicate to wait for, and adding one would be
ceremony implying a race the code does not have.

**Two of the six were never candidates at all**, and this entry's own
instrument says so: `test_resuming_refreshes_the_todo_panel` and
`test_a_failure_listing_threads_does_not_kill_the_tui` use `pump` ahead of
NEGATIVE assertions, which is the one thing `AGENTS.md` says `pump` is for.

**What was changed.** The gesture test -- the one that actually failed, in
batch 138's 635-second run -- takes the predicate, and the correct diagnosis
goes in beside it: the state is produced by the MESSAGE PUMP, not by another
thread. A key is posted, `press` can return before the binding fires, and
`pause()` ends on `wait_for_idle(0)`, a process-wide CPU heuristic with a 46x
spread. Its pair lost its `pause()` instead of gaining a predicate, because
asserting with no wait at all is what pins the property ARCHITECTURE 4.14
depends on -- a quit dismisses the prompt synchronously.

**What this entry is worth keeping for.** The six were found by pattern-
matching source, and the pattern was the right place to look: it found the
test that failed. What it could not do is say which of its hits were races,
and five of six were not. A prescription over a population wants its
population measured before it is applied -- which is the same lesson
TECHNICAL_DEBT 97 records from the other end, where the arity assumption that
failed was in a test whose docstring had already reasoned carefully about
timing.

### A NEIGHBOURING defect, found in batch 157 -- and the instrument above cannot see it

**`pause()`'s 46x cost has two consequences and this entry documents one.**
The one above is a positive assertion read before the message pump has run.
The other is a **WALL-CLOCK TIMER that expires while the test is inside a
`pause()`**, and batch 157 paid for it: five of the ten tests in
`TestTheQuitGesture` depend on `QUIT_CONFIRM_S`, the 2.0s window
`_disarm_quit` is armed on, and none of the five pinned it. A loaded run
closes the window between the two presses, the second `ctrl+c` re-arms instead
of quitting, and the assertion fails -- which is what
`test_a_second_press_inside_the_window_quits` did in a 373-second suite run
against the ~71 the previous batch measured.

**It is not this entry's shape, and the probe above is why we can say so.**
With `Pilot.pause` patched to a bare `asyncio.sleep(0)` the original assertion
PASSES, so the pump reaches the binding inside one loop turn and there is no
race here to add a predicate for. Batch 157's first attempt was exactly that
predicate, read off this entry, and it was **inert**: `settle` polls for state
that never arrives once the window has closed, so it turned a fast failure
into a slow one. The fix is `mocker.patch("tui.app.QUIT_CONFIRM_S", 30)`, and
the class already contained both halves of that idiom -- the tests that are
ABOUT expiry pin it to 0.05 to force it, and one already pinned it to 30 to
prevent it.

**The sharp part: the instrument that finds one of these defects HIDES the
other.** Making `pause()` instant is the opposite of the condition a
wall-clock expiry needs, so a timer-dependent test passes under the sleep(0)
patch with its defect intact. Shrinking the window instead reproduced the
failure deterministically and named the population at the same time -- 5 of
10, where the mechanical source scan said 18 across the file and the sleep(0)
patch said 35, neither of them this. **A probe is a hypothesis, and a passing
probe only clears the hypothesis it encodes.**

Fixed in batch 157, with the control stated as a relation rather than a flip:
with the ambient `QUIT_CONFIRM_S` forced to 0.001s -- which killed 5 of 10
before -- all ten pass, because each of the five now pins its own.


## 86. The decision-id gate scans `.py` only, and this project ships `.mjs` (open, found and filed batch 141)

**Issue:** #237.

**Found by the gate catching me**, which is the useful half. Severity `S4`,
`test-quality`.

`_CITATION` is `(?<![A-Za-z0-9_\-])([A-Z]{1,2}\d{1,2})(?![A-Za-z0-9_])`, so a
marathon finding id like `S18-02` reads as the decision id `S18`, which
resolves nowhere. Batch 131 hit that and wrote the rule into
`tests/BREAKING_CHANGES.md`: cite the BATCH in code, and leave finding ids in
DEVLOG, that file and the triage index.

Batch 140 then shipped **four** of them anyway -- in
`scripts/prepublish-check.mjs` (three) and `bin/venastine.mjs` (one) -- and
`test_every_decision_id_cited_in_production_code_resolves` said nothing,
because `_production_python()` yields `.py` and stops:

```python
for name in sorted(filenames):
    if name.endswith(".py"):
        yield os.path.join(dirpath, name)
```

The fifth citation, in `scripts/bandit-count-check.py`, turned the gate red
immediately. Same batch, same mistake, same session -- caught in the `.py`
file and invisible in the two `.mjs` ones. All five are corrected in batch
141; the gap is this entry.

**It is a gap by this repository's own definition of production code**, not by
mine. `test_the_tree_names_every_production_module` sweeps `.py` **and**
`.mjs`, so the tree gate and the citation gate disagree about what the project
is made of -- and the npm launcher plus the publish gate are exactly the files
a contributor is most likely to annotate with a finding id while fixing one.

Severity `S4`: nothing misbehaves, the citations were prose, and the ids
resolved nowhere rather than somewhere wrong. It is here because a guard whose
domain is narrower than its name is this file's recurring subject (item 79 is
the same shape in the tree gate), and because the evidence that it matters is
a batch three commits old.

**Shape:** the Python half works by `tokenize`, extracting comments and
docstrings so a citation inside a string literal is not read as prose. A
`.mjs` half needs the equivalent for `//` and `/* */`, which is the whole
cost -- maybe thirty lines, and it must not treat a URL's `//` as a comment
start. Two cheaper options, both honest: run the existing `_CITATION` over the
raw `.mjs` text and accept that a citation inside a JS string would also be
flagged (there are none today, and one would be a strange thing to write), or
assert the narrower property that no `.mjs` file contains a marathon finding
id at all, which is the rule batch 131 actually wrote down. The second is four
lines and closes the observed failure; the first is closer to the gate's name.

**Not:** widening `_production_python()` itself. Two gates share it, and the
test-name gate has no business reading JavaScript -- a `.mjs` file naming a
pytest test is not a thing that happens. Give the citation gate its own
domain.

## 87. `.gitleaksignore` pins its blessings by LINE NUMBER (open, found and filed batch 144, measured again batch 145)

**Issue:** #238.

The secret scan's allowlist is a list of fingerprints, and a fingerprint is
`<path>:<rule>:<line>`. So a blessing is attached to a line number, not to the
text it blesses, and **any edit that moves a blessed line re-fires the scan**.

Measured in batch 144, which is what filed this: six names added to
`tests/test_policy_enforcement.py`'s import block pushed five already-blessed
fake fixtures down by six lines, and `gitleaks detect --source . --no-git -c
.github/gitleaks.toml` went from five findings to ten. The change had no
business touching anything above those fixtures.

**This is not a bug in the ignore file, and the re-firing is deliberate** — the
file's own comment says it is, "so a real secret pasted beside a fake one
cannot hide behind this file". The cost is that it is paid by every future
batch that edits above a fixture, and the two ways of paying it are both bad:
re-number five blessings (which is the thing the comment is guarding against
becoming routine), or restructure the edit to avoid the lines. Batch 144 did
the second — the imports moved inside the tests that use them, and the
fixtures are built by concatenation so no line holds a whole token — and
batch 145 paid it again in a smaller way.

**What would close it:** the pinned gitleaks version has no content-addressed
ignore, so the fix is not a config change. The candidates are (a) move every
fabricated credential in the suite behind a builder that no rule matches, so
there is nothing to bless and `.gitleaksignore` can be emptied; (b) generate
the ignore file from a scan as a build step, which trades the surprise for a
generated artefact nobody reads; or (c) keep paying it and write down that it
is a known cost of editing those three files. (a) is the only one that
removes the class, and it is a sweep over the suite's fixtures rather than a
fix — hence the register.

**Not:** adding fingerprints as they fire. "Adding a fingerprint for a REAL
credential is never the fix; rotation is", and a routine of re-blessing is
how a real one gets blessed by accident.

**And the blessings only exist in ONE scan mode (measured batch 149).** A
no-git fingerprint is `<path>:<rule>:<line>`, which is the form this file
holds; a COMMIT-mode fingerprint is `<sha>:<path>:<rule>:<line>`. So
`.gitleaksignore` cannot match anything a history scan finds, and a history
scan of this repository **can never be clean**: run without `--no-git` it
reports the same five planted fixtures at the line numbers they had in the
commits that introduced them, which is this item's line-number pinning
reaching back through every commit at once. Both workflows pass `--no-git`
and both are clean, so nothing in CI is affected -- the cost is a cycle for
anyone who runs the tool by hand without the two flags CI uses:

    gitleaks detect --source . --no-git -c .github/gitleaks.toml -v

Batch 149 paid that cycle and reported "leaks found: 5" from a bare
invocation before checking what the five were. They were the redaction
tests' own needles: `sk-abc123def456...`, `AIzaSyA1234567890abcdef...` and
an RSA header whose body is literally `MIIEpA...`. Worth recording because
the wrong form is the one that looks MORE thorough, which is the same
footgun `pip-audit` has (item 88's entry, and batch 148's correction).

## 88. Six workflows are not on `main`, so a branch's CI is reconstructed by hand (closed, batch 159 -- superseded when PR #200 put the six workflows on `main`)

**Closed in batch 159, superseded rather than fixed.** PR #200 merged on
2026-10-15, and `main` now carries all six workflows, so a branch's lint, secret
and dependency checks run on GitHub again (PR #200's own run passed all 20). The
local reconstruction this entry asked for is no longer the only check. The flag
trap it records still applies to a run by hand -- copy the invocation out of the
workflow file, never from memory.

`navigable-agents` carries six workflow files that `main` does not, so GitHub
runs none of them for this branch and every batch's lint, secret and
dependency checks are run locally from invocations typed out of the workflow
YAML. That has cost a cycle at least twice, most recently in batch 144: the
first `bandit -b` run of the batch reported **9,165 new findings**, which was
not a regression but a missing `-s B101,B105,B106` — the baseline is produced
with those three skipped, so a comparison without them reports every `assert`
in the suite as new. The figure was implausible enough to be obvious. A
subtler mismatch would not be.

The four that have to agree with CI exactly:

```
ruff check .
bandit $files -s B101,B105,B106 -b .github/bandit-baseline.json -q
gitleaks detect --source . --no-git -c .github/gitleaks.toml -v
pip-audit -r requirements.txt -r requirements-test.txt
```

**What would close it:** one script in `scripts/` that reads the invocations
out of the workflow files rather than restating them, so the local run cannot
drift from the YAML by a flag. It is small, and it is deliberately NOT part of
batch 145: the build/CI surface (S18) closed at batch 142, and a script that
runs CI locally is a build-surface change that wants its own batch and its own
tests — including the one that matters, that the script fails when the YAML
and the script disagree.

**Not:** merging the workflows to `main` to make CI run. That is the owner's
call and the branch is not merged yet; the point of this entry is the local
reproduction, which is needed either way for the batches before the merge.

## 89. The transcript's ANSI strip is silent, and nothing reads the count it keeps (open, filed batch 145)

**Issue:** #239.

S15-01's fix removes every terminal control sequence at every transcript
ingress, and the owner's ruling was that the text says nothing and the log
says it once. Both halves shipped: `Transcript.take_stripped_count()` is
read-and-clear and `tui/app.py` logs one INFO line per turn.

What is NOT built is anything that acts on it. A model emitting a CSI is
anomalous — the transcript's grammar has no use for one, and the plausible
sources are a tool result carrying a program's raw output, a fetched page, or
an attempt at drawing something the user did not ask for. One INFO line in
`logs/app.log` is an audit trail for someone already reading the file, which
is "nobody, mid-run" in `TranscriptLogHandler`'s own words about exactly this
problem.

**What would close it:** a decision about whether a stripped sequence is worth
the user's attention, which is a UX question and not a defect. The options
were measured and named in batch 145 — a visible inline marker was considered
and declined, because it enters `_entries` (so `/copy` and the replay carry
it), changes `display_len`, and a model can type the marker itself, which is
the spoof `[REDACTED]` already accepts. The middle option that was NOT
explored: a once-per-session system line the first time a turn strips
anything, which costs no per-line grammar and is not spoofable by typing,
since the count comes from the stripper rather than from the text.

**Not:** raising the record to WARNING to make it visible. `TranscriptLogHandler`
is WARNING+ and routes into the transcript, so that is the inline marker
arriving by a route nobody chose, with a `[warning]` prefix on an ordinary
coloured answer.

## 90. A hardlink is a second name for a file, and the one hard deny cannot see it (open, found and filed batch 146)

**Issue:** #240.

`file_ops._protected_path_error` resolves a path before testing it for a
protected segment, which is what catches a symlink and a junction into
`.venastine/` -- both measured, both refused. A HARDLINK is not a link to
anything: `os.path.realpath` returns the path it was given, `os.path.islink`
is False, and the segment test sees an ordinary name inside the workspace.

Measured in batch 146, and no report names it -- it was found by asking the
S17 report's question (a document name that is a link) of the S04 report's
file. A `write` through a hardlinked name put `{"mcpServers": ...}` into a
protected `settings.json` whose direct spelling is refused, and that is the
file whose own rationale says it "names commands the harness spawns at
startup".

**The WRITE half is closed** and not by the deny: `json_store.write_text_atomic`
replaces the name rather than writing through it, so the link breaks and the
other name keeps its bytes. `write`, `edit` and `write_project_doc` all go
through it, and `tests/test_file_ops.py::TestAHardlinkCannotReachTheProtectedFile`
asserts on the protected file's bytes rather than on the answer the tool
returned, because the refusal still says None and that is the honest state.

**The READ half is open.** A `read` through a hardlink into `.venastine/`
serves the file, and `edit`'s read half sees it too (though `edit` returns no
content, so what leaks there is a match count). Reached only by someone who
has enabled `read` in `config.py` AND has a pre-existing hardlink -- git does
not store hardlinks, and a hardlink across a container's bind mount fails --
so this is the lowest-reachability item in the surface and the highest
consequence per unit of it.

**What would close it:** the same decision S02 owns for a link it cannot
resolve, which the S04 report names and declines in the same breath -- "a
guard that cannot resolve a path should refuse it", across `file_ops`,
`protected_paths` and `security/sandbox._within` together. The two mechanisms
available are both bad on their own: enumerating a file's other names is
per-platform (`FindFirstFileNameW` on Windows, a tree walk on POSIX), and
refusing every file with `st_nlink > 1` denies ordinary ones. Worth noting
that the ro-mount already covers the sandboxed-shell route, so the file tools
are the whole of this.

**Not:** adding `st_nlink` to the refusal. It would refuse a legitimate layout
to protect against one the write path already handles, and it would read as
the deny working when the deny is what cannot see this.

## 91. A closed issue cited in a docstring has nothing keeping it honest (open, found and filed batch 146)

**Issue:** #241.

`tools/builtin/project_docs.py`'s module docstring stated a credential
residual in the present tense -- a plaintext `<password>` element, a Gradle
`password "..."` and a `user:token@host` URL are "NOT" redacted out of a
`read_project_doc` result -- and closed with "Filed against the redactor
rather than fixed here."

It was filed, as #167, and #167 closed on 2026-08-23.
`safety/policy_enforcement.py` has carried `_URL_USERINFO_RE`,
`_XML_PASSWORD_RE` and `_ASSIGNMENT_RE` ever since, and all three shapes are
redacted today -- driven on all four file types in batch 146, with a
credential-free document passing through byte-identical as the control.

The paragraph was wrong for seven weeks, and the cost was not hypothetical:
the claim propagated into `docs/REVIEW_MARATHON.md` as a scope brief and into
a review scope's launch prompt as a question to go and verify, so a reviewer
spent a probe re-measuring a leak that did not exist. A security residual that
reads as OPEN is the most expensive kind of stale comment.

`tests/test_docs_consistency.py` already gates two kinds of citation from
production code: a decision id must resolve in the record, and a test name
must exist. An ISSUE NUMBER is gated by nothing, and there are many of them --
every `#NNN` in a comment is an assertion about a GitHub issue's state that no
test has ever read.

**What would close it:** the same shape as the decision-id gate, which is
`gh issue view` rather than a local file -- so it is a NETWORK check, which
this suite does not do, or a cached manifest, which is a second source of
truth about an issue's state. The narrower and probably right version: gate
only the citations that make a CLAIM about state ("filed as", "closed by",
"open in"), which is a grep for a handful of phrasings rather than for every
`#NNN`, and fail when the issue named is closed while the sentence says it is
open. That needs the manifest either way.

**Not:** removing issue numbers from comments. They are the most useful kind
of citation in the tree -- the question is only what keeps them true.

## 92. An atomic doc write can fail where a truncating one succeeded (open, filed batch 146)

**Issue:** #242.

`write_project_doc`, `write` and `edit` write through
`json_store.write_text_atomic` since batch 146, which is what stops a
hardlinked name being written through (item 90). The rename it ends with has a
failure mode the truncating write did not, and `json_store` already documents
it from the credential stores' side: on Windows a plain `open()` takes no
`FILE_SHARE_DELETE`, so any other process merely READING the destination makes
`os.replace` fail with `PermissionError WinError 5` -- an editor, a sync
client, a backup, a virus scanner.

So `/init` writing `AGENTS.md` while the user has it open in an editor can now
return "Could not write ..." where it previously overwrote the file. The
destination is correctly untouched and the temp file is swept, which is the
right failure; it is simply a failure that did not happen before, on the one
write path reachable with no config edit, at the moment a user is most likely
to have the document open -- they were just shown a diff of it.

**What would close it:** nothing, on the current trade. The alternatives are
worse: writing through the name again re-opens item 90's write half, and a
retry loop around `os.replace` turns a deterministic refusal into a timeout.
What is worth doing is making the MESSAGE say what to do, since
`PermissionError` on a rename reads as a permissions problem and the actual
remedy is "close the file" -- which needs the errno distinguished, and WinError
5 arrives as `PermissionError` for several unrelated reasons.

**Not:** a Windows-only branch that falls back to a truncating write. That is
item 90 reintroduced on the platform where a case-insensitive filesystem makes
the rest of this surface hardest to reason about.

## 93. An environment override that is SET BUT EMPTY is honoured as a value (open, found and filed batch 146)

**Issue:** #243.

`config_schema.py` merges the environment over the YAML with
`override = os.environ.get(variable)` and `if override is not None`, so an
empty string is a value. For `AGENT_WORKSPACE` that means
`workspace_dir = ""`, and `os.path.realpath("")` is the LAUNCH DIRECTORY.

Measured:

```
AGENT_WORKSPACE = ''
config.WORKSPACE_DIR            = ''
file_ops.WORKSPACE_ROOT         = C:\Projects\Venastine Research Harness
_is_within_workspace(config.py) = True
_file_approval_check('write', config.py) = False      <-- auto-approved
```

...against the properly-unset behaviour, which is the control:

```
AGENT_WORKSPACE = None
config.WORKSPACE_DIR            = ./workspace
_is_within_workspace(config.py) = False
_file_approval_check('write', config.py) = True       <-- asks
```

**Two guarantees go at once**, because `WORKSPACE_DIR_EXPLICIT` reads the
variable's PRESENCE and never its value (batch 111, item 28, deliberately):
an empty value is present, so the workspace is ALSO taken as the project
path. So a launch with `AGENT_WORKSPACE=""` from a user's home directory
makes the home directory the agent's auto-approved workspace and the project
whose documents `read_project_doc` and `write_project_doc` are scoped to.

**How it was found, which is the part worth keeping**: the per-batch
verification step re-runs the affected tests with `AGENT_WORKSPACE` unset,
because this shell has it set and CI does not. Written as
`AGENT_WORKSPACE= python -m pytest ...`, which sets it to empty rather than
unsetting it -- and `test_posture.py`'s
`test_writing_config_py_still_needs_a_human` and its `config.yaml` sibling
both went red. Those two tests are the whole answer to "is moving the
posture into a data file a weakening?", in their own docstring's words, and
an empty variable is enough to answer yes. `env -u AGENT_WORKSPACE` is the
correct invocation and the suite is green under it.

**What limits it today.** `protected_paths.check_workspace` refuses a
workspace that IS the install tree, and that refusal fires for this case
when the launch directory happens to be the install tree -- measured. For
any other launch directory it returns None, so there is nothing between an
empty variable and a home-directory workspace. `write` and `edit` are also
globally denied, which leaves the live consequences as the sandbox's
read-write mount plus auto-approval of non-inert commands inside it -- which
is exactly the reasoning `check_workspace`'s own message gives for refusing
the install tree -- and the project scope of the two `project_doc` tools.

**What would close it:** a decision rather than a line, which is why this is
filed and not fixed. `if override is not None` is deliberate for SOME keys:
an empty string is a meaningful value for a prefix or a suffix, and the
merge is generic over `ENV_OVERRIDES`. The candidates are a per-key rule
("these are paths, and an empty path is not a value"), a blanket
`if override` (which silently stops honouring an empty value everywhere,
including keys where it might be wanted), or validating `workspace_dir`
after the merge rather than at the merge. The third is probably right and is
the only one that keeps the generic merge generic -- `check_workspace`
already exists as the place a bad workspace is refused, and "empty" is a bad
workspace in exactly its sense.

**Not:** treating this as a posture bug. The posture is frozen and correct;
what moved is which directory the posture is ABOUT, one layer below it.

## 94. The CLI's Ctrl+C stops a turn on the FIRST press, with nothing between a stray press and a stopped turn (open, filed batch 147)

**Issue:** #244.

CN10 gave the CLI "Ctrl+C stops the turn", and the TUI's equivalent gesture
has two protections that the CLI has neither of:

- **A copy check.** `tui/app.py`'s `_copy_selection` runs first, and the
  rule its docstring states is *"ctrl+c copies when there is something to
  copy, and starts a quit when there is not. A press that copies never arms
  and never quits"*. Batch 57 measured that, because `Input` and `TextArea`
  bind ctrl+c to `copy` and a priority app binding pre-empts them
  completely -- the clipboard stayed empty.
- **A confirmation.** It takes two presses inside `QUIT_CONFIRM_S` (2.0s),
  and the first one says what the second will do.

In the CLI the first press cancels the running turn. **The copy check
cannot be ported**: the selection belongs to the terminal, not to the
process, so the harness cannot ask whether there is something to copy.
Which terminal is in use decides whether a press with a selection even
reaches the process -- Windows Terminal and a QuickEdit conhost copy
instead of signalling, and others do not -- so the behaviour of this key
varies by terminal in a way the harness cannot see or state.

**Why it is filed rather than fixed.** It is the owner's reason for §64's
A21, pointed at the binding A21 deliberately did not touch: a press aimed
at copying must not be able to end a model turn. Reversing or qualifying
CN10 is a locked-decision change and wants its own batch with its own
record -- and the candidates are not equivalent:

- **Two presses inside a window**, the TUI's shape. Costs a second press
  for everyone who meant it, and the first press has to print something,
  which during a streaming answer competes with the stream the CLI is
  writing to the same stdout.
- **A different key**, which the CLI cannot do: there is no key reader
  during a turn. The CLI blocks in `run_to_completion`, so a signal is the
  only thing that can reach it.
- **Leave it**, on the reasoning that the CLI is a fallback (D12) and the
  TUI is where someone works all day -- which is defensible and is the
  reason this has not been urgent.

**What is NOT at risk**, measured in batch 147: a stray press no longer
tears a thread in half. §64's A21 made the wake turn's handler defer rather
than unwind, and CN10's own handler already stops the turn at a BOUNDARY
and writes every row it owes (CN4), so the cost of a stray press is a lost
turn rather than an unresumable thread.

## 95. A credential straddling the output buffer's DROPPED MIDDLE is still recoverable from two pages (open, measured and filed batch 148)

**Issue:** #245.

A23 (ROADMAP_v3 §65) put the redactor above every cut a session's output
goes through, and there is one cut it cannot be put above. `OutputBuffer`
keeps the first `shell_session_output_head_chars` (10,000) and the most
recent `shell_session_output_tail_chars` (190,000); everything between is
**discarded as it arrives**. A credential that straddled that point has one
half at the end of the head and the other at the start of the tail, and the
middle between them was never kept by this process -- so no later pass can
rejoin the pattern, however early it runs.

**Why this is a leak and not merely a limit.** Both halves are SHOWN. The
pages are separated by a stated gap (`{"from": 10000, "to": N}`), and a
reader who can ask for both pages -- which the model can, that is what
`shell_output`'s `offset` is for -- holds the two fragments the redactor
could not match. It is the same shape as the paging defect A23 fixed, with
the difference that here the text in between genuinely does not exist.

**Measured, batch 148.** `OutputBuffer(100, 100)` fed a 1,400-character
document reports `total_chars=1400`, hands back 200 characters in four
pages, and states the gap at every page that reaches it. With a secret
planted across the eviction point, each page is individually clean and the
join is not.

**What a fix would have to decide**, which is why it is filed rather than
done:

- **Redact the STREAM**, in `append`. That is the one place both halves are
  present. But the monitor matches on raw output by design
  (`core/session_wake.py`'s header states the rule: redact what is returned,
  not what is matched), so redacting here would stop a credential-shaped
  line waking an agent at all -- and a pattern can straddle an `append`
  boundary too, since a pty delivers arbitrary chunks, so this would need
  the carry `_wait_after_input` already keeps for matching.
- **Keep a boundary window.** Hold the last ~200 characters of the head
  beside the tail purely so the join can be scanned once at eviction time.
  Cheap and bounded, and it makes `total`, `page` and the gap arithmetic
  describe something that is neither the raw stream nor the kept text.
- **Say so in the gap notice.** The honest minimum: the gap already tells
  the reader that output is missing, and it could say that what sits on
  either side of it has not been scanned as one string. Costs nothing and
  fixes nothing.

**Not urgent, and the reason is the shape of the leak:** it needs a
credential to straddle one specific character position in a session that
overflowed a 200,000-character buffer, and the harness's own tools do not
page across the gap on their own -- the model has to ask. Severity S3 by
the marathon's scale, on the same reasoning S11-05 used for the cut A23 did
close.

## 96. A window below ~50,000 tokens cannot hold one bounded read in its headroom (open, measured and filed batch 148)

**Issue:** #246.

A24 (ROADMAP_v3 §65) made `derived_trigger` say so when the DEPENDENT
FLOOR leaves no usable headroom, and ruled deliberately against warning in
the neighbouring case: a window where the floor does not bind but the
headroom CAP leaves less room than one bounded tool read.

Measured, with the shipped settings:

| window | headroom | one bounded read | what binds it |
|---|---|---|---|
| 8,192 | 191 | 12,500 | the floor (A24 warns) |
| 10,666 | 2,665 | 12,500 | the floor (A24 warns) |
| 16,384 | 4,096 | 12,500 | `COMPACTION_MAX_HEADROOM_FRACTION` |
| 32,768 | 8,192 | 12,500 | `COMPACTION_MAX_HEADROOM_FRACTION` |
| 64,000 | 16,000 | 12,500 | fine |

So on a 16,384 or 32,768-token window compaction fires, folds, and the next
`read_file` of a 50,000-character file can still overflow the window --
which is the failure `headroom_for`'s docstring exists to prevent ("There is
no context_length_exceeded handler anywhere in `core/` -- grep for one. The
provider raises, `core/loop.py` retries the identical oversized prompt three
times, and the turn dies. This number is the only thing standing between a
thread and that").

**Why A24 excluded it.** The cap is deliberate -- it is what keeps the floor
from eating a small window whole, and what keeps the derived trigger
positive at the bottom of the range -- and there is no setting the user can
change to buy more headroom there. A warning naming
`warning_margin_tokens` and `keep_recent_tokens`, which is what A24's line
names, would be naming the wrong thing, and it would fire on every stock
install of a 32k model.

**What a fix would have to decide:**

- **Bound the READS instead**, which is the direction that actually fits:
  `max_read_chars` is a setting, and a window this small could carry a
  smaller one. That is a cross-cutting change -- `file_ops`, `shell_output`,
  `fetch_url` and the session tail all read it -- and it changes what a tool
  returns rather than when a thread folds.
- **Say it once per model anyway**, with wording that names the window
  rather than the two settings: "this model cannot hold one full tool read
  after a fold". Honest, unactionable, and on by default.
- **Leave it**, on the reasoning that a 16k-window deployment is already
  outside what the rest of the harness is sized for -- which is the same
  reasoning that made S11-03 severity S3 rather than S2.

Recorded here rather than in a warning, so that the next person reading
`headroom_for`'s floor-and-cap argument finds the measurement instead of
re-deriving it.

## 97. Two timing-sensitive tests failed in SLOW full-suite runs and neither reproduced (the resize half fixed batch 151; the secrets half open, and deliberately untouched)

**Not filed as an issue (batch 159), deliberately.** The open half is an
unreproduced flake that this entry says must not be touched until it recurs; an
issue would invite exactly the change the entry warns against. This entry stays
the measurement the next occurrence is compared against.

Batch 148 ran the full suite five times. The three that finished in 85-100
seconds were green. The two that were slowed by other work on the same box
-- 792s with WSL, bandit and pip-audit competing, 366s with a previous run's
workers still exiting -- each produced exactly one failure, in a different
file, in a test that passes alone:

| run | wall clock | failure | alone |
|---|---|---|---|
| 1 | 100s | (none) | -- |
| 2 | 792s | `test_secrets.py::TestTheEnvelope::test_it_does_not_say_WHICH_part_was_wrong` | 95 passed |
| 3 | 100s | (none) | -- |
| 4 | 366s | `test_live_output.py::TestAResizeRelaysTheTranscript::test_a_drag_re_arms_rather_than_stacking` | 13 passed |
| 5 | 85s | (none) | -- |

**The condition is the one item 85 already measured**: `pilot.pause()` costs
21ms with nothing burning CPU in-process and 1021ms with one busy sibling
thread, *"a 46x swing ... A 7x-slower suite run is the condition that
measurement describes."* These two runs were 7.9x and 3.7x slower than the
documented 100 seconds. What is new here is that the condition reaches two
files item 85 does not cover, through two assumptions neither of which is
about a worker thread.

**The resize one is diagnosable from its output.** The test fires three
`resize_terminal` calls and asserts three timers were armed; it got two.
`armed` is `[Timer(30), Timer(30)]`, so the handler ran correctly for every
message it received -- textual simply delivered two `Resize` messages for
three calls. **The assumption is one message per call**, and the pump is
free to coalesce when the app is behind, which is exactly what a loaded box
produces. The docstring is already careful about the thing it refused to do
("asserted on the MECHANISM rather than by waiting a debounce out ... under
eleven xdist workers the margin is whatever the box is doing, which is a
scheduling lottery rather than a test"); what it did not question is the
count. **Shape:** assert on the INVARIANT rather than the arity -- every
armed timer but the last was stopped, and `_reflow_timer` is the last one --
which is what the other two assertions already say and what holds for any
number of delivered resizes above one.

**The secrets one is NOT diagnosable**, and that is stated rather than
dressed up:

```
    for mutate in (lambda e: e.update(n=1 << 14), ...):
        locked_store.write_text(json.dumps(edited), encoding="utf-8")
>       with pytest.raises(secrets.WrongPassphrase):
E       Failed: DID NOT RAISE WrongPassphrase
```

It failed on the FIRST of three mutations -- the one editing the KDF cost
parameter down to `MIN_SCRYPT_N` -- so `unlock(MASTER)` RETURNED on an
envelope whose `n` no longer matched the one the key was derived under.
Either `secrets.SCRYPT_N` was already `1 << 14` when `locked_store` created
the store, which makes that mutation a no-op and the unlock correct, or the
edited file was not what `unlock` read. Nothing in the output distinguishes
them.

What was checked and ruled out:

- **Batch 148 did not touch it.** Nothing in that diff reaches
  `security/secrets.py`, `AGENT_SECRETS_FILE` or `hashlib.scrypt`; the one
  piece of shared test state it changed is the root logger's LEVEL, which
  cannot reach key derivation.
- **The only test that lowers `SCRYPT_N`**
  (`test_an_existing_store_keeps_its_own_parameters`) is in the SAME class,
  LATER in the file, and restores it with `monkeypatch` --
  and `--dist loadscope` keeps a class's tests in collection order, so it
  cannot have run first.
- **No other file in `tests/` mentions `SCRYPT_N`** (grepped).
- **`unlock()` caches no envelope**: it re-reads and re-parses the file on
  every call, so a stale in-process copy is not available as an explanation.

**Why both are filed rather than fixed.** The resize fix is small and belongs
with item 85's six in whichever batch next touches those files -- doing it
here would mean changing a test to chase a failure on a surface this batch
is not about. The secrets one must not be touched at all yet: changing a test
to chase a failure it has not shown again is how a real defect gets hidden
behind a louder fixture, and this entry is the measurement the next
occurrence gets compared against.

---

### The resize half, RESOLVED in batch 151

`test_a_drag_re_arms_rather_than_stacking` asserts the INVARIANT now --
`stopped == armed[:-1]` and `view._reflow_timer is armed[-1]`, which were
already there and hold for any number of delivered resizes -- with a floor of
**two** rather than an exact three. Two and not nothing: one delivery cannot
show coalescing, so `assert armed` alone would have let the test go vacuous
in precisely the direction that hides the bug. The docstring carries the
measurement, because the arity was the one claim in that test about textual's
DELIVERY rather than about this widget, and the rest of it had already
reasoned carefully about timing without questioning the count.

**The secrets half is untouched**, as this entry asks. It has not recurred;
if it does, the comparison is the measurement above.

## 98. Pass 3a is asked about the FACTUAL claims and applied to every claim (open, measured and filed batch 149)

**Issue:** #247.

Gate D0 routes claims by type: factual claims go to 3a/3b, synthesis and
speculative claims go straight to Pass 4. `pass3a_input` is built from
`factual_claims` alone and says so. But the payload is validated against
`[c.id for c in run.claims]` and applied by `_apply_grounding(run.claims,
...)`, so a grounding entry naming a speculative claim lands on it.

**Measured: it moves no tier.** `score_claim`'s non-factual branch is
`capped_critic - assumption_penalty`; `grounding_component` is not in that
formula at all (E9/B10 state this, and B10 exists because both fields used
to be recorded on a speculative claim while being inert). A speculative
claim scored MEDIUM before a `status: "grounded"` was applied to it and
MEDIUM after.

**What it does change is the artifact.** The claim then carries
`grounding_status: "grounded"` and whatever sources the entry named, into
`/claims`, into `output/<run_id>/` and into the stored record -- asserting
that a speculative claim was grounded, which gate D0 says cannot happen.
A reader auditing the routing sees a counter-example to it.

**Why it is filed rather than fixed in batch 149.** §66 narrowed Pass 6b
because its apply and its re-tier disagreed about which claims were in
play, which produced a STALE TIER. 3a has no such disagreement -- Pass 5
tiers every claim immediately after, from the data 3a left -- so the fix
here would be a scope change with no measured consequence behind it, and
S12-02's own report says not to make it ("3a/3b apply-then-tier the whole
factual set symmetrically, so no staleness arises there"). The shape, if
it is ever taken: `_ground_and_score(run, ..., claims=factual_claims)`,
which is now a one-argument change, plus `claim_ids` to match -- and the
decision to make first is whether a 3a entry about a non-factual claim is
worth a corrective retry or a dropped-and-traced entry.

## 99. Pass 6a drops an unknown claim id in silence, where 6b now says so (open, filed batch 149)

**Issue:** #248.

§66 (A26) gave Pass 6b a trace line for an entry naming a claim outside the
round's batch, on `review.py`'s rule: a finding a stage cannot apply is
dropped AND traced, because dropping in silence is indistinguishable from
applying. Pass 6a, twenty lines above it, does the same drop and says
nothing -- `claim = next((c for c in flagged if c.id == rev["claim_id"]),
None)` followed by `if claim:`.

The asymmetry is now the only thing wrong with it, and 6a's own comment
argues that the silence is contained rather than correct:

> An omission, an empty list and an unknown id are indistinguishable here
> -- the lookup returns None and the `if claim:` skips in silence -- which
> is why the bound cannot be data the model supplies.

That reasoning is about the LOOP BOUND, which the per-round retry counter
settled, and it is sound. It is not a reason for the reader of a trace to
be unable to tell an omission from a misdirected revision. 6a's checkpoint
line reports *"revised N of M claim(s) in this retry round"*, so a drop is
visible as a count but not as a cause.

**Not fixed in batch 149** because it is not one of the surface's findings
and the batch had already grown past both of them; the fix is
`_note_entries_outside_the_batch(run, flagged, revisions)` with the message
reworded for a revision, which is the same helper.

## 100. An MCP tool's description is read as harness-authored instruction (open, filed batch 150)

**Issue:** #249.

**What it is.** A tool schema's `description` is third-party text that the
model reads as part of the harness's own instruction to it. Batch 150 bounded
it at the producer (§67 A31) -- whitespace collapsed, capped, prefixed with
`[MCP: <server>]` -- which closes the size and the forged-section halves.
What it does not do is say to the model *this text came from somewhere
else*.

**Why it is filed rather than fixed.** It is a design decision about what a
tool schema IS, not a bug in one. `prompts/untrusted_content` exists and is
the envelope this project uses for exactly this class of text, but wrapping
a tool description in it changes what EVERY MCP tool reads like to the
model -- including the tools a well-behaved server advertises, which is all
of them in practice. S05 owns the untrusted-content envelope and S13 named
this as the seam rather than proposing it. The `[MCP: <server>]` prefix
means provenance is at least STATED today, which is why the finding it came
from is S3 and not S2.

**What would settle it.** Whether a tool schema is harness instruction or
third-party data. If it is data, the envelope applies and the prefix is the
wrong mechanism; if it is instruction, the bound is the whole remedy and
this item closes. Nobody has needed to decide yet because no shipped
configuration has an MCP server at all.

## 101. `requires_approval` can still return a non-boolean (open, filed batch 150)

**Issue:** #250.

**What it is.** `security.permissions.requires_approval` ends in
`return tool_level or context_level`, and `context_level` comes from
`context.approval_overrides.get(tool_name, False)`. Batch 150 typed those
values where agent definitions are PARSED (§67 A28), so a frontmatter
`web_search: "false"` now skips the file with a warning. A `ToolContext`
built programmatically is not parsed by anything, so
`ToolContext(approval_overrides={"web_search": "false"})` still makes the
function return the string `'false'`.

**Why it is filed rather than fixed.** Measured, and nothing is widened: the
return is used in boolean context everywhere, approval ORs, and every falsy
value is already indistinguishable from an absent key -- so the worst a
non-boolean does is tighten. It is a type claim a docstring makes and the
signature does not enforce.

**The shape the fix would take is already in this tree**, which is the
reason to record it: `core/config_loader.py`'s `max_steps` guard REPAIRS at
parse time and `core/loop._run` RAISES for "a programmatic caller passing a
bad value directly [who] has no file to lose, only a bug worth naming". The
same pair would apply here. The reason it is not done in batch 150 is that
`requires_approval` is on the hot path of every dispatch and every
advertisement, and adding a raise to it for a case no caller in the tree
produces is a cost with no demonstrated consequence -- which is B4's rule,
applied to this item rather than against it.

## 102. Under WSL, a test that spawns a subprocess cannot import this project's dependencies (open, measured and filed batch 150)

**Not filed as an issue (batch 159), deliberately.** It is an environment note
rather than a code defect, as stated above, and there is nothing for an issue to
track to closure.

**What happens.** `tests/test_config_loader.py::test_re_importing_config_re_reads_the_file`
fails under WSL and passes on Windows and in CI. The subprocess it spawns
dies with `ModuleNotFoundError: No module named 'pydantic'`, so the
assertion reports a return code of 1 and names none of the cause.

**Why.** On this WSL box the project's dependencies are installed into the
**user site**, not a venv:

```
/home/<user>/.local/lib/python3.14/site-packages/pydantic/__init__.py
```

Python locates the user site from `HOME` at interpreter startup. The suite
redirects `HOME` for test isolation, and the PARENT process is unaffected
because it imported everything before the redirect -- but
`subprocess.run([sys.executable, "-c", program, ...])` inherits that
environment, and the CHILD resolves its user site from the redirected
`HOME` and finds nothing there. Reproduced directly, outside pytest:

```
$ python3 -c "import pydantic"                     # ok
$ HOME=/tmp/nohome python3 -c "import pydantic"    # ModuleNotFoundError
```

**It is not a code defect and not a CI risk.** The test asserts a real
property (popping `config` out of `sys.modules` under a changed environment
must produce a `config` that sees the change) and the production code it
guards is correct. CI installs dependencies into the runner's own
environment rather than a user site, so `HOME` is not what finds them
there. Confirmed pre-existing: the same test fails the same way against a
detached worktree at `00d73a1`, which is the commit before batch 150.

**Why it is filed rather than fixed.** The clean fixes each have a cost
worth stating:

- **Pass `PYTHONPATH`/`PYTHONUSERBASE` through to the child.** Smallest, but
  it teaches one test about an environment detail, and the next subprocess
  test has the same problem with no shared answer.
- **Stop redirecting `HOME` for subprocess-spawning tests.** Exactly wrong:
  the redirect exists because a probe that mounts the real app writes the
  user's real preferences, which this project has already paid for once.
- **Install into a venv on the WSL box.** Fixes it for everybody and is an
  environment change rather than a repository one, so it cannot be done by
  a commit.

**What this item is really for.** `WSL verifies POSIX behaviour` is a
standing part of per-batch verification here, so this failure appears on
every batch that re-runs `test_config_loader.py` there, and each time it
reads like a regression in whatever that batch touched. It is not. Anyone
seeing one WSL-only failure in this test should check this entry before
spending the cycle -- and should still check that the failure is THIS one,
because a second WSL-only failure would be a different claim.

## 103. The range gate reads a per-section sub-range as a claim about the whole family (open, measured and filed batch 151)

**Issue:** #251.

**What happens.** `test_a_claimed_range_covers_its_whole_family` reads
**every** `Xn-Xm` on `AGENTS.md`'s documentation-map line and compares each
one's upper bound against the highest id that family defines. That line
carries two kinds of range: the family claim (`A1-A39 from section 32`) and
per-section parentheticals saying where each id lives (`A28-A33 in section
67`). The gate cannot tell them apart, so a parenthetical whose top is not
the family's maximum reads as a range that "stops short":

```
AssertionError: AGENTS.md's map claims a range that stops short of its family:
  A: map claims through A33, record defines A39
```

**Why it is not a defect in the gate's purpose.** The property it exists for
is real and was paid for: E13 and E14 sat outside the index that says where
decisions live, and the two checks either side of this one both pass on a
range that stops short -- the forward check asserts every claimed id is in
the record (true of a subset) and the reverse asserts every family is named
(E is named). Neither looks at the endpoint. Being loud is correct.

**What it costs.** One cycle per batch that adds a decision family's next
range. `A28-A33 in section 67` passed only while A33 was the highest A
defined; batch 151 added A34-A39 and the same unchanged sentence became
"short". The failure names a FAMILY and a number, not the parenthetical that
produced it, so the reader's first move is to check the family claim -- which
is correct -- and find nothing wrong with it.

**Why it is filed rather than fixed.** The fix is a one-line distinction and
the question is which line:

- **Read only the first range per family**, which is the family claim by
  position. Cheapest, and it silently stops checking a second genuine family
  claim if one is ever written.
- **Read only ranges outside parentheses.** Closer to the intent and a
  parser of English punctuation, which is what this file's own
  `_qualifier` grammar already is -- and that grammar has its own
  discrimination test for exactly that reason.
- **Write every per-section entry as a LIST**, which is what sections 63,
  64, 65 and 66 already do and what batch 151 made section 67 and 68 do.
  Costs nothing and is what the document looks like today.

**The third is in force and it is a CONVENTION**, which is why this entry
exists: batch 151's own A34 is that a convention about a document is what
produced five copies of one sentence. Nothing checks that the next
per-section entry is written as a list, so the next range to be added will
re-enact this, and this entry is what the reader should find when it does.

**Not:** deleting the check, and not relaxing it to the highest range on the
line. Both give back the property E13 and E14 paid for.

## 104. Displayed untrusted text is filtered for controls and not for bidi or zero-width characters (open, filed batch 152)

**Issue:** #252.

**What is not covered.** `core/ansi.for_display` removes escape sequences
in both encodings, every control below U+0020 except newline and tab, DEL,
and the 8-bit range U+0080-U+009F. It does not touch the bidirectional
overrides (U+202A-U+202E, U+2066-U+2069) or the zero-width and invisible
characters (U+200B-U+200F, U+FEFF). Those can make a terminal draw text in
an order the bytes do not have -- the published Trojan Source class -- so
a model-supplied headline could read as one command and be another.

**Why it is not the same answer.** A control character is never legitimate
content: there is no text in which a backspace or a CSI introducer is part
of what the author meant to say. A bidi mark is the opposite -- it is
ordinary content in Arabic and Hebrew text, and a filter that deletes it
corrupts correct text to protect against incorrect text. The honest
options are to render it visibly (`<U+202E>`), to refuse the value, or to
leave it; section 69 A45's reasoning picks none of them because the
decision needs a position on what this shell does with non-Latin text,
and that position does not exist yet.

**What makes it bounded rather than urgent.** The approval headline is
printed one line per `splitlines()` segment with a `  $ ` prefix, so a
reordering is confined to one visible line and the prefix still arrives
first. The params block renders through `json.dumps`, which escapes
nothing in this range but does quote it, so the bytes are visible there in
the ordinary case. And a bidi override reorders what is shown without
erasing it, where CSI `2K` erased.

**Not:** adding the range to `_NOT_PRINTABLE` without deciding the
question above. That would be the cheap half of an answer, in the module
whose docstring states the cost of the C1 deletion it already does.

## 105. The decision-id gate reads a Unicode control-range name as a decision citation (open, measured batch 152)

**Issue:** #253.

**What happens.** `test_every_decision_id_cited_in_production_code_resolves`
treats any `LETTER+NUMBER` token in production python as a citation of the
decision record. `core/ansi.py` is prose about Unicode, so it says `C0`
and `C1` -- and measured, this batch:

- `C0` resolves nowhere, so the gate FAILED on a correct sentence about
  the control range (batch 152 reworded its own additions to
  `U+0000-U+001F` to get past it)
- `C1` resolves to ROADMAP_v2's **C1**, "the permission prompt is bridged,
  not inlined", which has nothing to do with the 8-bit control range the
  sentence is about -- and the gate is SILENT, because resolution is all
  it checks

So one half of one sentence is a false failure and the other is a false
pass, in the same file, about the same subject. Batch 145 wrote the `C1`
sentences and they read as citations to the gate; they are not.

**Why it is not fixed here.** The two candidate fixes both cost something
real. An exemption list of non-citation tokens is a maintained copy of
exactly the kind this marathon keeps deleting. Requiring citations to name
their namespace (which `AGENTS.md` already asks for -- `gate D0`,
`section 21 AC6`) would be the principled answer and means auditing every
bare id in production, which is a surface of its own rather than a line.

**What a reader should know meanwhile.** `C0`/`C1` in `core/ansi.py` mean
the Unicode control ranges. The gate cannot tell, and a sentence about
Unicode that trips it is not drift.

## 106. Two private names AGENTS.md documents may be stale, and the gate that would say so costs too much here (closed, batch 159 -- both names measured as explained)

**Closed in batch 159: both names are explained, so neither is stale.**
Re-checked before filing. `_replace_via_keyboard` is Textual's own symbol -- a
method in `textual/widgets/_text_area.py` in the installed pin -- which puts it
in the same class as `_check_bindings` and `_watch_theme` above. And both
`AGENTS.md` mentions of `_last_response` (`:2643`, `:2745`) are past tense: they
record the attribute batch 48 removed ("`_last_response` was the second
buffer"), which a NAME index is right not to find. With no unexplained name
left, nothing waits on the stronger gate, and the reason it was not built stands
as written.

**What was measured.** Section 69 A44's gate resolves a documented private
identifier by substring match over every `.py`. The stronger candidate --
`tokenize` NAME tokens, which a comment or a string cannot produce --
flags eight of AGENTS.md's 131, and six are explainable:
`_check_bindings` and `_watch_theme` are Textual's symbols,
`__builtins__` is CPython's, `_compacting` is prose about a design
`core/compaction.py` deliberately did not take (`_is_compacting` and
`_set_compacting` are what exist), and `__` is a convention rather than a
name.

**The two that are not explained:**

- **`_last_response`** (`AGENTS.md:2620,2705`) appears in `tui/app.py`
  only inside a comment and in `tests/test_research_legibility.py` only
  inside a docstring. If the attribute exists it is assigned somewhere a
  NAME token would see; the index says it is not.
- **`_replace_via_keyboard`** (`AGENTS.md:750`) appears once in the tree,
  in a `tui/widgets.py` docstring that quotes *its* docstring -- so the
  thing being quoted should be a function and is not one the index can
  find.

Both are plausibly renames that left the document behind, which is the
same defect S16-03 was and one the current gate cannot see.

**Why the gate was not made stronger.** A coherence gate that fails on a
CORRECT reference to somebody else's symbol trades one false claim for
three. Closing this properly means either resolving upstream names (a
dependency on installed packages, in a gate that must work offline) or an
exemption list, and the choice is a decision rather than an edit. The
NAME-token probe is `probe152g.py`'s shape and takes about ten lines to
rebuild.

**Not:** widening A44's pattern to dotted paths while this is open. That
is the boundary A44 states, and `mutate152.py`'s D4 is the must-survive
control that would turn red when somebody crosses it.

## 107. `is_trusted()` raises for an unreadable file, and every caller pays for that differently (open, measured batch 153)

**Issue:** #254.

**What was measured.** `core/workspace_trust.is_trusted` hashes every path
`content_files()` lists -- the root `AGENTS.md` and everything under
`.venastine/` -- and `_content_hash` opens each one with no guard. A file an
editor holds open, a sync client is mid-write on, or whose mode the user
changed makes it raise. Batch 153 made `/init`'s two call sites total
(A47); the other three callers were measured rather than changed:

- `core/config_loader.initialize` (`:1130`) -- reached at startup through
  `main.load_project_config`, which catches `(ValueError, OSError)` and
  `SystemExit(1)`s with `Configuration error: [Errno 13] Permission
  denied`. **Contained, and the message names an errno rather than the
  file**, so the user is told the configuration is broken and not which
  file to unlock.
- `main._ensure_workspace_trust` (`:2360`) -- the same `try`, same exit.
- `main._start_mcp` (`:2488`) -- inside that function's own broad `try`,
  which reports per server and does not stop the harness.

**Why it was not fixed here (A48).** The fail-closed alternative is
`is_trusted` answering False for an unanswerable question, which is this
module's own posture for a damaged store (`_load_trust_store` returns `{}`
and says it fails CLOSED in both senses). Applied to a content READ it
changes startup from "refuse to start, name a configuration error" to
"start degraded, print the ordinary not-trusted notice" -- and that notice
is indistinguishable from the one an ordinary untrusted project gets, so
the louder behaviour is arguably the right one for a security control. It
is a D17 posture decision with its own blast radius, not a side effect of
repairing one command.

**The cheap half, if only one thing is done:** `load_project_config`'s
message could name the path, since the `OSError` carries `filename`. That
is a one-line change and does not touch the gate.

## 108. `generate()`'s other pre-write filesystem calls are still outside its `try` (open, measured batch 153)

**Issue:** #255.

**What was measured.** Batch 153 made the trust capture at
`project_init/generator.py` total because it sits before the write `try`.
Three siblings still do: `manifest.is_blank(root)` and `propose_kind(root)`
(the kind question's inputs, both directory walks) and `_config_plan(root)`
(`os.path.exists` per name, which does not raise). `_read_existing_context`
and `manifest.detect_facts` are already covered -- the first catches
`OSError` itself, the second runs inside the `try`.

So an `OSError` from a walk -- a directory the user cannot list, a path
that stopped being one mid-run -- still leaves `generate()` as an `OSError`.
**`main.run_init_command` catches `InitError` only**, measured by driving
it, so on the CLI that is a traceback; the TUI's worker catches `Exception`
and degrades to an error line.

**The two shapes of answer, neither taken:** widen the CLI handler to
`OSError` (one line, and it makes the two shells agree about containment,
which is what `project_init/generator.py`'s own docstring says this module
exists to guarantee for policy), or move the pre-write calls inside a
`try` that raises `InitError`. The first is a claim about every future
escape; the second is a claim about these three. Doing both is probably
right and is a decision rather than an edit.

**Not:** a blanket `except Exception` in `run_init_command`. The TUI has
one and says why it is acceptable there (a worker thread whose death takes
the app); the CLI has a process to end cleanly and a traceback is more
useful than a swallowed bug.

## 109. Nothing resolves the test names `tests/BREAKING_CHANGES.md` cites (open, measured batch 153)

**Issue:** #256.

**What was measured.** Every row in the guard log names the test that
would break -- that is the column's whole job -- and no gate checks that
the name exists. Batch 153 renamed three tests for S17-05 and found their
rows by `grep`, which is the practice TECHNICAL_DEBT item 7 already
records as the thing that gets forgotten.

The same batch found one row naming a symbol that resolves nowhere:
`project_docs.CONTEXT_FILENAME` (`tests/BREAKING_CHANGES.md:2937`),
reworded to past tense there. **That is batch 152's S16-03 shape, one
document over and PUBLIC rather than private**, so section 69's A44 gate
-- which is deliberately scoped to backquoted identifiers with a leading
underscore, in `AGENTS.md` -- cannot see it.

**Why it is not a ten-line gate.** The column is prose, not a field: a row
may name one test, two, a parametrised id (`test_x[<tier>]`), a class, a
file, or a phrase ("Every test that writes or reads the old path -- 20-odd
across four files"). Pinning it means deciding what counts as a citation
first, which is A44's own lesson: the resolution rule was chosen by
measuring three candidates against the real corpus, and two of the three
failed on correct references. Counting `test_[a-z_]+` tokens and
resolving them against the collected session is the probe worth writing
before the gate.

**What it would have caught.** Not this batch's defect -- the three names
were correct until this batch renamed them. It catches the NEXT rename,
which is the half nobody is present for.

## 110. The thread-kind NULL fallback is in three readers and pinned by none (open, measured batch 154)

**Issue:** #257.

**What was measured.** `get_thread`, `child_threads` and `list_threads`
each read `getattr(t, "kind", None) or THREAD_KIND_CHAT`, and what that
absorbs is a NULL on a row written before the column existed -- the
additive migration never backfills (M7). S09-03 was the third copy of a
comment saying it absorbed something else (a database whose ALTER has not
run, which raises `OperationalError` at the `SELECT` instead), and batch
154 corrected the wording.

**Nothing asserts the behaviour.** A search of the suite for a thread row
with a NULL `kind` read back through any of the three finds nothing:
`tests/test_schema_migration.py` asserts that the column is ADDED and
`tests/test_storage_e2e.py` asserts that the §47 lineage columns are not
backfilled, but no test puts a NULL in `kind` and asks the picker what it
says. So the fallback that three readers carry has no demonstrated
consequence -- B4's test -- and deleting the `or THREAD_KIND_CHAT` from
all three would be green.

**Why it is not two lines.** The honest home is
`tests/test_storage_e2e.py`, because the question is what a real row with
a real NULL reads as, and that file's one module-scoped swap is the only
place a real row exists. One test there would cover all three readers,
which also makes it the moment to decide whether the explanation belongs
in one place with two pointers (today: a full paragraph in `get_thread`, a
corrected comment in `list_threads`, nothing in `child_threads`).

**What it would have caught.** Not S09-03, which was about the stated
reason rather than the behaviour. It catches the cleanup that reads three
defensive `getattr`s, finds no test behind them, and removes them --
which, on a database carrying pre-§27 rows, puts every one of those rows
back in the picker as `None`.

## 111. `pipeline_storage`'s write and read API has never run against real SQLite (open, measured batch 154)

**Issue:** #258.

**What was measured.** `tests/test_storage_e2e.py` is the one place with a
real `sqlmodel`, and its `_SWAPPED` tuple is `("sqlmodel", "config",
"database", "storage")`. `core.reasoning.pipeline_storage` is not in it,
so `PipelineRunRecord`, `create_pipeline_run`, `update_pipeline_run` and
`load_pipeline_run` are exercised only against the conftest's fake --
including the two NULL guards on `pass_threads_json` and
`candidates_json`, which exist for a column added by an `ALTER` that the
fake has no notion of.

`classify_legacy_pass_threads` is unaffected and stays that way on
purpose: it takes a DBAPI connection precisely so its SQL is testable
without the ORM, and batch 154's two new e2e tests use that seam.

**Why it is not an entry in a tuple.** Adding the module to `_SWAPPED`
registers `PipelineRunRecord` on the real `SQLModel.metadata`, which must
happen before `create_db_and_tables()` for the table to exist -- so the
fixture's ordering becomes load-bearing in a second way, in the file whose
own docstring says "there can be only one swap". The measurement worth
having first is whether a real round trip finds anything the fake does
not; `json.loads(None)` raising is the guard most likely to differ.

**What it would have caught.** Nothing in this batch -- S09-01 lives at
the DBAPI seam, which is now driven for real. It is the standing risk that
the persistence layer's own tests prove the fake and the code agree.

## 112. The trust prompt says every project agent's description enters every system prompt, and most never do (open, measured batch 155)

**Issue:** #259.

**What was measured.** `_catalog_entries`' section header reads *"these
descriptions enter EVERY system prompt in this project:"*, and
`prompts/system_prompts.py` filters the agent catalog to `spawnable`
agents. Project tier does not declare `spawnable` by default and
undeclared is `False` (§32 A3/A4), so most listed project-agent
descriptions never enter any prompt at all. The skill half of the same
sentence is accurate -- `skill_catalog_text()` lists every skill.

**Which direction it is wrong in.** Over-disclosure. The prompt claims a
reach larger than the real one, so a person who reads it carefully
refuses on grounds that were stronger than the facts. That is the
fail-safe direction for a consent screen, which is why this is an entry
and not a fix: the sentence should be split (skills: all of them; agents:
the spawnable ones, with the rest reachable by `/agent`), and splitting it
is a wording question somebody should answer on purpose rather than
mid-batch.

**What it would have caught.** Nothing in batch 155. It is the half of
S08-03 the finding itself declined, recorded so the next person reading
that header does not re-derive it.

## 113. A trust-prompt entry shadowed by a harness or user definition is displayed as the project's (open, measured batch 155)

**Issue:** #260.

**What was measured.** `_catalog_entries` parses every `.md` under the
project's `.venastine/{agents,skills}` and shows each one's normalised
name and description. `_tier_dirs`' precedence is harness > user > project
(D29), and `_discover` is first-wins -- so a project definition whose name
collides with a harness or user one is displayed in the consent screen
while the builtin or user definition is what actually loads.

**Why it is not simply a bug.** The grant genuinely covers the file: its
bytes are in the content hash, and a later rename of the shadowing
definition makes the project's copy live with no new prompt. So listing it
is right and describing it as the text that will be used is wrong, and the
fix is a per-entry note ("shadowed by the builtin of the same name") that
needs the tier resolution `describe_project_content` does not currently
do. Over-disclosure again, like item 112.

**What it would have caught.** Nothing in batch 155; S08-03 names it and
declines it for the same reason.

## 114. `AgentManager`'s two `set()` calls have no belt, where `max_steps` does (open, measured batch 155)

**Issue:** #261.

**What was measured.** `AgentManager.active_context` and `child_context`
each build `set(agent.allowed_tools)`, which is the single place
`AgentDef.allowed_tools` (an `Optional[list]`) becomes
`ToolContext.allowed_tools` (declared `Optional[set[str]]`). Before batch
155 an unhashable element raised `TypeError: unhashable type: 'list'`
there, measured through the real `AgentManager`. A55 closed every
file-borne route at the loader and deliberately left these two alone.

**The asymmetry with `max_steps`.** That field has a belt in
`core/loop._run` which raises rather than repairing, for a stated reason:
a programmatic caller passing a bad value has no file to lose, only a bug
worth naming. `allowed_tools` has the same shape and no belt, so a caller
constructing an `AgentDef` directly still gets a `TypeError` from a set
comprehension rather than a message naming the field.

**Why it was not done here.** B4. Nothing in the tree constructs an
`AgentDef` outside `_parse_md_file`, so a guard there would have no
demonstrated consequence -- and a second answer to a question the loader
already answers is the fix-at-the-consumer shape AGENTS.md names. The
measurement worth having first is whether any caller ever will.

## Review-marathon arrivals at open issues with no register entry

Two `KNOWN` findings landed on GitHub issues that are not mirrored here. Both
were re-verified against the marathon's base commit; neither is a new defect.
Recorded in the tree because `review/` is not. Both issues carry this
re-verification as a comment since batch 159, made against `main` at
`f836602`.

- **Issue #12** `[S3][coherence]` *ensemble disagreement is scored rather than
  routed to verification*, from the research-pipeline scope. Verified still
  true: `confidence_scoring.py:55, 264-280, 301-306` applies
  `DISAGREEMENT_PENALTY_FACTOR * (1 - consistency)` to factual claims,
  `orchestrator.py:1356-1371` routes D2 only on `retry_count` and never on
  disagreement, and no verification pass consumes `consistency_score`. Nothing
  to add beyond the issue's own rearchitecture analysis (pure-code gates versus
  a new model call).
- **Issue #8** `[S3][ux]` *the CLI has no slash-command layer*, from the
  headless-shell scope. Verified still true: `main.py` dispatches no
  `/command` at all -- `run_chat` treats every non-empty line as a message,
  and three of the parser's own comments cite the absence as the reason
  `--memories`, `--ref` and `--init` are flags (`main.py:1940-1959`,
  `:1975-1982`). One thing to add: **item 71 above is the live-rendering half
  of the same D12 debt**, so the two should be read together -- a shell with no
  command layer and no live narration is less capable in two ways that compound.
