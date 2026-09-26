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

## 12. `effective_compaction()` reports no provenance (open, re-scoped batch 107)

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

---

## 13. `spawn_subagent` has no `available_check` where `load_skill` does (open, noted batch 51)

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

## 15. Nothing re-renders on resize, so a pre-wrapped construct freezes (open, folded into item 23 in batch 107)

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

- **Confirm the new jobs green on Linux runners.** Bandit's checked-in
  baseline uses the `./`-joined filenames Linux produces
  (`manager.py:253` joins `os.path.join(".", f)`; verified from source,
  not from a run) and Gitleaks/CodeQL/compat have never executed outside
  this machine. First red on `push` is expected to be platform-shaped;
  do not "fix" it by regenerating the baseline blindly (bandit.yml names
  the procedure and its precondition).
- **arXiv feed hardening (Bandit B314/B405, baselined).**
  `tools/builtin/arxiv.py:141` parses an attacker-influenced Atom feed
  with stdlib `ElementTree`. Accepted-risk for the rollout; the fix is
  `defusedxml` (or `defuse_stdlib`), with `tests/test_mcp_client.py`-style
  version-pinned re-verification.
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

## 23. The transcript does not reflow on a terminal resize (open, 2026-09-14)

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

## 24. A newline after a row that filled the width draws a blank row live (open, 2026-09-14)

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

## 25. A model call is not retried once its output is on screen (open, 2026-09-14)

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

## 28. `workspace_dir` in `config.yaml` names a workspace but not a project (open, 2026-09-26)

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
