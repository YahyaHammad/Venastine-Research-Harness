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
  is a HYBRID: all 138 `results` are `./` (Linux, the half bandit
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
`test_this_file_declares_no_test_class` pins it. `real_storage` is the suite's only
non-function-scoped fixture (`grep -rn 'scope="module"' tests/`), so it was the only file at risk --
and the rule the next one inherits is: **a module- or session-scoped fixture's file must hold no
test class.**

**What remains open in this item:** the 183 classless tests in `test_tui.py`, and nothing else. The
`test_config_edit.py` precondition this entry prescribed was never the defect, and the mode
decision it was blocking is taken.

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

## 41. `arxiv_search` output is unbounded, and one result was 29k tokens (open)

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

No cap, no truncation notice, no paging. The fix is presumably the shape
`max_read_chars` already has, applied where the results are assembled.

## 42. A window under ~9.4k makes `effective_compaction()` raise (open)

Pre-existing, and found while tabling batch 119's trigger against small
windows. `warning_margin_tokens` is 8,000 and the loader refuses a pair
where it is not strictly below `trigger_tokens` -- correctly, since
otherwise the warning arrives after the compaction it warns about. But the
trigger is now derived from the window, so any window small enough to put
the derived trigger at or below 8,000 turns every `thresholds()` call into a
`ValueError`: `should_compact()` runs at the top of every step.

True before batch 119 as well (a 8,192-token window derived 6,963 against
the same 8,000) and not made materially worse by it (6,144). Unreachable
with the shipped roster and `DEFAULT_CONTEXT_WINDOW` at 256k; reachable by
anyone who types `/window 8k` for a small local model. The margin should
probably scale with the trigger the way the headroom now scales with the
window.

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
