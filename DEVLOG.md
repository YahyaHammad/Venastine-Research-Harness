# DEVLOG — Implementation Notes for Built Roadmap Sections

**Purpose of this document:** a complete account of what was implemented for each built ROADMAP section, what was followed verbatim, what was deviated from and why, and what was discovered during testing that the original spec did not anticipate. Companion to **ARCHITECTURE.md** (which describes the production code shape) and **ROADMAP.md** (which still holds the original specifications) — read both alongside this file.

Section numbers below match ROADMAP.md's section numbers exactly. When a section is implemented, its corresponding ROADMAP.md entry gets a short "Built" subsection pointing here for full detail, and ARCHITECTURE.md gets a §4.N file-contract entry for any new file(s) created.

**Index:**
- 0. Chat-trace context — how this document's decisions were made
- 1. Interactive CLI entry point
- 2. `logging_setup.py`
- 3. Malformed-JSON recovery (+ §5 minimal core pulled forward)
- 4. Test suite
- 5. Durable `PipelineRun` persistence (§5 proper)
- 6. `tools/builtin/file_ops.py`
- 7. `tools/builtin/shell.py` + `security/sandbox.py`
- 8. `safety/policy_enforcement.py`
- 9. Google Gemini support
- 11. Critic-model routing
- 12. `/output/<run_id>/` file-writing system

---

## 0. Chat-trace context — how this document's decisions were made

The two implemented sections (§2, §4) below were each delivered through a structured pre-implementation clarification cycle, NOT by an agent improvising. The pattern was the same each time:

1. The user flagged a ROADMAP section as the target and linked the relevant existing files (ARCHITECTURE.md, ROADMAP.md, plus the file(s) to be built or updated).
2. The agent read all referenced files, read every file that touched the surface area to be implemented, and verified any claims against the actual code (a stated goal of the user: "you may review everything I said instead of taking it for granted").
3. The agent surfaced explicit ambiguities as concrete `question` calls BEFORE writing any code. Each question came with multiple options, a recommendation, and a one-paragraph rationale per option.
4. The user selected one answer per question.
5. The agent drafted a finalized plan reflecting ONLY the user's chosen options, and surfaced a second (smaller) round of sub-decisions if the user's first-round answers exposed narrower choices that needed settling.
6. Once all decisions were locked, the plan went to build mode and the agent implemented it.

**Why this process matters for future agents picking up this work cold:**

- Every deviation from ROADMAP documented in this file was explicitly chosen by the user, not inferred. If you disagree with a deviation, you're disagreeing with a recorded decision, not a stray guess — the right move is to surface the conflict to the user and re-ask, not silently override.
- Each section below records the question dimensions that were asked, so a future agent continuing partial work can see whether their question was already answered (and how) before re-asking.
- The "ask before implementing" pattern is the user's stated preference. New roadmap sections should follow the same clarification cycle, not skip it. Treat the answers recorded here as prior decisions not to be re-litigated without cause — same weight as any other architectural commitment in ARCHITECTURE.md.

---

## 1. Interactive CLI entry point

### 1.1 What we built

`main.py` rewritten from a hardcoded example query to an argparse CLI with two modes:

- **Chat mode** (default): `input()`/`print()` loop with tool use. Thread id printed after the first response for later `--thread` resume. Ctrl+C / Ctrl+D to exit.
- **Research mode** (`--mode research "query"`): runs `run_deep_research_pipeline`, prints final report + trace + run id.

Flags: `--thread UUID` (resume), `--mode {chat,research}`, `--provider` (default: `DEFAULT_PROVIDER` from `core/loop.py`), `--model` (default: `config.MODEL_NAME`). Positional `query` (optional in chat mode — used as the first message; required in research mode).

`configure_logging()` wired as the very first call inside `if __name__ == "__main__":` — this unblocks §2's acceptance criterion (tool retry warnings now appear on stderr).

Prerequisite fix: `run_agent_conversation` in `core/loop.py` now accepts `thread_id: Optional[UUID] = None` and passes it to `ConversationMemory(thread_id=thread_id)`. `ModelResponse.thread_id` was already present (§3 work) — no change needed there.

### 1.2 Questions we asked & your answers

Two questions before implementation:

| # | Dimension | User's choice |
|---|---|---|
| 1 | Sequencing: basic CLI first (then Textual TUI later), TUI now, or both in one pass? | **Basic CLI first** — get the project usable now; the TUI replaces the I/O layer later. The argparse/dispatch/logging scaffolding carries forward. |
| 2 | TUI vision: what does "feature rich" mean? | **Textual-based app** — full TUI framework with panels, scrollable history, sidebar, live streaming. Planned as a separate effort. |

### 1.3 What we followed verbatim from ROADMAP §1

- argparse with `--thread`, `--mode`, `--provider`, `--model` flags. ✓
- UUID validation via `uuid.UUID(value)` with a clear CLI error for malformed input. ✓
- `DEFAULT_PROVIDER` imported from `core/loop.py`, not duplicated. ✓
- Chat mode: `input()`/`print()` loop, thread id printed after first response, `[stopped early: ...]` for non-complete stops. ✓
- Research mode: `run_deep_research_pipeline` → print report + trace. ✓
- `configure_logging()` as the first line, `create_db_and_tables()` immediately after. ✓
- Prerequisite fix: `thread_id` parameter on `run_agent_conversation`, `ConversationMemory(thread_id=thread_id)`. ✓

### 1.4 What we deviated from §1, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| Positional `query` arg for research mode only | Also works in chat mode as the first user message (then the loop continues) | Natural convenience — `python main.py "hello"` starts a chat with "hello" as the first turn. No spec language prohibits this; the spec just doesn't mention it. |
| (spec silent on `build_parser()` separation) | `build_parser()` is a standalone function, argparse setup separated from `if __name__ == "__main__":` | Testability — `tests/test_cli.py` exercises parser defaults and UUID validation without triggering `configure_logging()` / `create_db_and_tables()` side effects. |
| (spec silent on a Textual TUI) | CLI documented as the permanent fallback; Textual TUI planned separately | User's choice (Q1/Q2). The CLI's scaffolding (argparse, mode dispatch, logging wiring) carries forward to the TUI entry point. |

### 1.5 Acceptance criteria status

ROADMAP §1's acceptance criterion: *"a person can run `python main.py`, have a multi-turn conversation with tool use working, exit, and resume the exact same thread later with `--thread <uuid>` and see prior context intact."*

**Implementation complete; end-to-end verification requires a real API key** (the test suite is offline). The wiring is verified by 7 tests in `tests/test_cli.py`: thread_id passthrough (×2), UUID validation (×2), parser defaults (×3). The resume-shape bugs that previously blocked `--thread` (§4 Discovery 2, §3.8 tool-result fix) are both fixed — a resumed thread now reconstructs the identical neutral shape for every role.

§2's acceptance criterion is also unblocked: `configure_logging()` is now called in production code.

### 1.6 Test changes

- `tests/test_cli.py` (NEW): 7 tests — thread_id passthrough with/without the parameter, `_uuid_type` valid/invalid, parser defaults, research-mode parsing, `--thread` flag parsing.
- `tests/test_e2e.py` (NEW): 2 end-to-end tests — chat mode (multi-turn with tool use, thread resume, write-through persistence verified via `fake_storage`) and research mode (report + trace + run id printed). Chat mode uses the `fake_storage` fixture because the fake sqlmodel's `select().where().order_by()` chain doesn't support the full query `get_session_history` needs; the storage layer itself is covered by `test_memory_write_through.py`.
- Full suite: 88 tests, ~0.50s, zero network/API keys.

---

## 2. `logging_setup.py`

### 2.1 What we built

A single function, `configure_logging()`, in a new project-root file `logging_setup.py` (sibling to `config.py`). Not under `core/`, since logging is cross-cutting infrastructure rather than conversation-loop-specific.

Signature:

```python
def configure_logging(
    level=None,
    *,
    log_file=None,
    max_bytes=1_000_000,    # 1 MB
    backup_count=3,
) -> None
```

Behavior:
- Configures the root logger.
- Attaches a `StreamHandler(stream=sys.stderr)` AND a `RotatingFileHandler` for the log file.
- Both handlers share one `logging.Formatter` with the ROADMAP-mandated format string.
- Idempotent: a repeated call clears any pre-existing root handlers (closing them defensively) before attaching fresh ones, so test runs that invoke `configure_logging()` more than once don't stack duplicate handlers and double-emit lines.
- Parent directory of the log file is auto-created if missing.

Environment overrides:
- `AGENT_LOG_LEVEL` (default `"INFO"`) — accepts int, named string (`"DEBUG"`), or numeric string (`"20"`).
- `AGENT_LOG_FILE` (default `"logs/app.log"`) — overridden by the `log_file=` kwarg if provided.

### 2.2 Questions we asked & your answers

Six questions were asked before writing `logging_setup.py`:

| Q | Dimension | User's choice |
|---|---|---|
| 1 | Log destination: stderr only, or stderr + rotating file? | **Stderr + rotating file** — tool retries survive CLI exit and process close. |
| 2 | Log level: hardcoded `logging.INFO`, or env-overridable? | **Env-overridable** via `AGENT_LOG_LEVEL`. DEBUG-at-runtime without code edits. |
| 3 | Surface tool-retry warnings into `PipelineRun.trace`, or keep separate? | **Do NOT touch `PipelineRun.trace`** — two separate logs for two audiences. `run.log(...)` stays as today. |
| 4 | Wire `configure_logging()` into `main.py` now, or defer to ROADMAP §1? | **Only logging_setup.py, leave main.py alone** — keep this task laser-focused on §2; wiring lands when §1 (the CLI) is built. |
| 5 | Where to put rotation constants (`max_bytes`, `backup_count`)? | **No config.py changes** — keep defaults inside `logging_setup.py` as function defaults. |
| 6 | Verify the new `configure_logging()` works before close-out? | **No verification, just write** — main.py isn't being wired in this task, so the §2 acceptance criterion (run main.py + retry → timestamped WARNING) is not yet testable. |

### 2.3 What we followed verbatim from ROADMAP §2

- File location: project root, sibling to `config.py` (NOT under `core/`).
- Format string: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`.
- Datefmt: `%Y-%m-%d %H:%M:%S`.
- NOT auto-configured on import — `configure_logging()` is an explicit call site's responsibility. This avoids surprise handler configuration in tests and library-style usage.
- Module docstring explicitly avoids implying auto-config: "Not called automatically on import, so tests and library-style usage of this project don't get surprise handlers forced on them."
- File name is `logging_setup.py`, NOT `logging.py` — the §11 gotcha about a root-level `logging.py` shadowing Python's stdlib `logging` module was the reason for the rename, and the new file keeps that name.

### 2.4 What we deviated from §2, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| `stream=sys.stderr` (stderr only) | stderr + `RotatingFileHandler` writing to `logs/app.log` (1 MB × 3 backups) | User's choice — post-run inspection of tool retries that survive terminal close. Without file output, the entire point of "see tool retry warnings after the run" disappears once the CLI exits. |
| `level: int = logging.INFO` (hardcoded default) | `level=None` → reads `AGENT_LOG_LEVEL` env var (default `"INFO"`) | User's choice — runtime DEBUG flip without code edits. Accepts int, name string, or numeric string to handle all plausible env var styles. |
| (spec silent on idempotency) | `configure_logging()` clears root handlers before attaching, and closes them defensively | Repeated calls are realistic (test runs, REPL sessions); without clearing they'd stack and double-emit. Defensive close handles a handler in a weird state without failing loudly. |
| (spec silent on `config.py` integration) | No `config.py` changes — `max_bytes`/`backup_count` are function defaults in `logging_setup.py` | User's explicit choice — keeps logging self-contained in one file (mirrors §2's "logging subsystem kept in one file" framing). If tuning these becomes a real need, they can be promoted to `config.py` later without architectural friction. |
| "main.py calls `configure_logging()` as its very first line inside `if __name__ == "__main__":`" | main.py NOT touched | User's explicit choice — wiring deferred to ROADMAP §1's CLI work. The implementation is correct; only the call site hasn't been added. |

### 2.5 Acceptance criteria status

ROADMAP §2's acceptance criterion: *"running `main.py` and triggering a tool retry (e.g. force a `web_search` failure) shows a timestamped WARNING line on stderr, not silence."*

**Partially deferred.** The implementation matches the original spec's behavior modulo the deviations above, but the acceptance criterion itself depends on ROADMAP §1's `main.py` rewrite being landed. Until then, `logging.getLogger(__name__).warning(...)` calls in `web_search.py` / `arxiv.py` / `fetch_url.py` go nowhere visible — the SDK-side logger configuration (set up by `configure_logging()`) never fires because nothing calls `configure_logging()` in production code.

**Known gotcha worth flagging:** until §1 is implemented, the practical effect of §2 to an end user is zero. This is documented in the new §2 "Built" subsection in ROADMAP.md and should be visible to whoever picks up §1.

### 2.6 Discoveries during testing

No additional issues surfaced during §2's implementation. The deviations from the original ROADMAP §2 spec — all user-decided via the Q&A in §2.2 — are the only divergence; see §2.4 for the full deviation table. Two minor defense-in-depth improvements to `logging_setup.py` were applied after a post-implementation review: the handler-cleanup `except` was narrowed from bare `Exception` to `(OSError, IOError)`, and `log_file` is now normalized via `os.path.abspath` before being passed to `os.makedirs` / `RotatingFileHandler`. Neither changes behavior in the normal path.

---

## 4. Test suite

### 4.1 What we built

10 new files, no production code changes:

```
pytest.ini                              # testpaths=tests, --strict-markers
conftest.py                             # ROOT -- import-time SDK stubs
tests/
├── conftest.py                         # make_model_response, FakeStorage fixture, client-cache reset
├── BREAKING_CHANGES.md                 # what-breaks-it / symptom / fix tables per file
├── test_confidence_scoring.py          # 5 tests (3 ROADMAP verbatim + 2 general)
├── test_client_translation.py         # 14 tests (tool & message translation, both providers)
├── test_loop_stop_conditions.py       # 3 tests (ROADMAP verbatim)
├── test_orchestrator.py               # 4 tests (full pipeline mocked)
├── test_registry_permissions.py       # 8 tests (allow/deny/approval enforcement)
├── test_math_tools.py                  # 14 tests (symbolic equivalence + safe_parse injection regression)
├── test_memory_write_through.py        # 7 tests (write-through contract + storage-path-mismatch net)
└── test_loop_tool_dispatch.py         # 5 tests (_run dispatch branch)
```

**Full suite passes: 60 tests, ~0.55s.**

### 4.2 Questions we asked & your answers

Six questions were asked before writing the test suite:

| Q | Dimension | User's choice |
|---|---|---|
| 1 | Stub strategy: full stub conftest (per ROADMAP §4), per-test minimal stubs, or skip SDK stubs entirely? | **Full stub conftest (follow ROADMAP §4)** — locks in the contract, isolates test runs from real network/SDK behavior. |
| 2 | Coverage scope: strict ROADMAP §4 (6 files), or §4 + 2 additions (`test_memory_write_through`, `test_loop_tool_dispatch`)? | **ROADMAP §4 + memory & loop dispatch** — both load-bearing for every downstream roadmap item indirectly. |
| 3 | Math test expectations: symbolic equivalence, strict subset + string equality, or string equality + pin SymPy? | **Symbolic equivalence** — `sympy.simplify(actual - expected) == 0` absorbs future SymPy serialization drift across versions. |
| 4 | Test bootstrap mechanism: `conftest.py + pytest.ini`, or also `tests/__init__.py` + filterwarnings + asyncio markers? | **`conftest.py + pytest.ini`** — minimal, no asyncio (no async code in this project). |
| 5 | `test_memory_write_through` approach: real sqlite via swap fixture, or monkeypatch storage and skip real DB? | **Monkeypatch storage, skip real DB** — keeps the storage-path-mismatch catch intact without messy pop-and-reimport dance against the fake sqlmodel. |
| 6 | `pydantic` (and `sympy`): follow ROADMAP §4 literally and fake `pydantic` too, or stub 6 modules and keep pydantic + sympy real? | **Stub 6 modules, keep pydantic + sympy real** — math tools need real validation (`Field`, `Literal`, `field_validator`) to compute real answers in `test_math_tools.py`. |

A seventh question arose mid-implementation when `tools/registry.py` failed to import (`ModuleNotFoundError: No module named 'sympy'`):

| Mid | sympy was pinned in `requirements.txt` as `sympy==1.13.3` but NOT actually installed in this env — install, or stub-and-skip math tests, or stop and ask? | **Install sympy into the env** — aligns the dev env with what `requirements.txt` says is the contract. Lets `test_math_tools.py` run for real, lets `tools.registry` import for real. |

### 4.3 What we followed verbatim from ROADMAP §4

- The directory structure under `tests/` matches §4's enumeration exactly: `tests/test_confidence_scoring.py`, `test_client_translation.py`, `test_loop_stop_conditions.py`, `test_orchestrator.py`, `test_registry_permissions.py`, `test_math_tools.py`.
- `tests/conftest.py` provides a `make_model_response(text, tool_calls=None, usage=None)` helper that builds a `ModelResponse` directly, bypassing SDK mocking entirely. Per §4's spec language.
- The three confidence-scoring regression cases (§4's verbatim "must include at minimum these exact cases") are present verbatim:
  - Well-grounded factual claim, zero critic severity → `HIGH`. ✓
  - Ungrounded factual claim → `UNVERIFIED`, tested with `critic_severity=0.0` specifically to prove grounding alone is disqualifying. ✓
  - Clean speculative vs. flagged speculative → **must land on different tiers** (the cap-swallows-penalty bug regression). The test even asserts the raw_score gap is exactly 0.15 (one assumption flag × `ASSUMPTION_FLAG_PENALTY=0.15`), pinning the cap-before-penalty invariant tight. ✓
- The three loop stop-condition tests (§4's verbatim "should include") are present verbatim:
  - Task completion (no tool calls) → `stop_reason == "complete"`. ✓ (also asserts the assistant message does NOT get added, matching `core/loop.py:56-58` short-circuit)
  - `max_steps` exhaustion → `stop_reason == "max_steps_reached"`, call count equals EXACTLY `max_steps`. ✓
  - Token budget exceeded on FIRST call → `stop_reason == "token_budget_exceeded"`, exactly 1 call, response's tool_calls NOT dispatched (asserts `registry.dispatch` mock wasn't invoked). ✓
- ROADMAP §4's acceptance criterion — `pytest` runs the full suite with **zero network access and zero real API keys required** — is verified: `providers.json` mtime is unchanged after a full test run, and the file's `API_KEY` fields are all empty by inspection. The full run completes in ~0.55s.

### 4.4 What we deviated from §4, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| "fake `openai`/`anthropic`/`google.genai`/`sqlmodel`/`pydantic`/`httpx`/`ddgs` modules into `sys.modules`" | Faked 6 modules: `openai`, `anthropic`, `google` (parent), `google.genai`, `sqlmodel`, `httpx`, `ddgs`. **Kept `pydantic` real, not faked.** | Math tools (`symbolic_math.py`, `linear_algebra.py`, etc.) need real Pydantic validation (`Field`, `Literal`, `field_validator`) to actually compute the results `test_math_tools.py` asserts against. Faking `pydantic` would mean reimplementing validation in the fake or skipping math-tool coverage — both worse than the small drift risk of letting real `pydantic` reach into the test runtime. `pydantic` is pure-Python with no network/auth/IO; the only concern §4 had was import-time, but real `pydantic` works fine for import. |
| (spec silent on `sympy`) | `sympy` is real (not faked) | Same reasoning as `pydantic`: pure-Python, no network; `test_math_tools.py` needs to compute real results via SymPy. |
| Six test files in §4's directory | Same six + **two added** (`test_memory_write_through.py`, `test_loop_tool_dispatch.py`) | `core/memory.ConversationMemory`'s write-through contract and `core/loop._run()`'s tool-dispatch branch are both load-bearing for every downstream roadmap item (§3 touches `orchestrator.py`; §5 touches `base.py` and the orchestrator; §10 touches `confidence_scoring.py` and the orchestrator). NEITHER was directly tested by any of §4's six named files. Adding the safety net now (before more hands touch those files) is the whole point of doing §4 at this stage in the user's stated order. |
| "Add to `requirements.txt`" | `requirements.txt` NOT changed | `pytest==9.1.1` and `pytest-mock==3.15.1` were already installed in the dev env (verified via `pip list`); the spec's "add pytest to requirements.txt" assumed a fresh install from a clean checkout. **Warrants follow-up** — see Discoveries below. |
| "A `stub_provider_sdks` fixture" | Stubs land at IMPORT TIME in the root `conftest.py`, not via a per-test `stub_provider_sdks` fixture | Fixtures run at test-time — too late. Pytest's collection-time import of e.g. `test_loop_stop_conditions` already triggers `core.loop`'s import, which already triggers `core.client`'s import of real `openai`/`anthropic`. Import-time install in root `conftest.py` (which pytest loads before collecting any test module) is the standard escape hatch — the spec's "fixture" framing just wasn't quite the right mechanism. The fake modules themselves are exactly the shape §4 asks for. |
| "Known-correct answers" for math tools (mechanism not specified) | `sympy.simplify(actual - expected) == 0` — symbolic equivalence, not string equality | SymPy's `str()` output drifts across versions (e.g. `sqrt(2)` vs `2**(1/2)`). Pinning tests to today's string outputs would silently break on a future SymPy upgrade. Symbolic equivalence is mathematically exact and version-proof. |
| (spec silent on `tests/BREAKING_CHANGES.md`) | New file `tests/BREAKING_CHANGES.md` documenting fragile-mock maintenance points | Added at the user's explicit request after the agent flagged `test_orchestrator.py` as the most fragile test in the suite (its mock dict is keyed by pass_id strings, which ROADMAP §3 and §10 will both touch). The file has per-area "what breaks it / symptom / fix" tables so the next agent maintaining the suite has a deterministic first stop when something fails after a planned refactor. |

### 4.5 Acceptance criteria status

ROADMAP §4's acceptance criterion: *"`pytest` runs the full suite with zero network access and zero real API keys required (everything mocked), and the three regression cases above are present verbatim, not paraphrased into something weaker."*

**Fully satisfied:**
- Full suite passes: 60 tests, ~0.55s.
- Zero network access verified via `providers.json` mtime unchanged across a full run plus empty `API_KEY` fields by inspection.
- The three confidence-scoring regression cases are present verbatim.
- The three loop stop-condition cases are present verbatim.
- The `make_model_response(text, tool_calls=None, usage=None)` helper exists in `tests/conftest.py` per §4's spec.

### 4.6 Discoveries during testing

Two discoveries that weren't deviations — they surfaced during implementation and warrant follow-up outside this work's scope:

#### Discovery 1: `sympy==1.13.3` pinned but NOT installed

`requirements.txt` pins `sympy==1.13.3` but the dev environment did NOT have `sympy` installed when test implementation started. `import tools.registry` failed at module top:

```
File ".../tools/builtin/symbolic_math.py", line 15, in <module>
    import sympy
ModuleNotFoundError: No module named 'sympy'
```

This is **not** a defect of the test suite or the test plan — it's a defect of the dev environment not matching `requirements.txt`. Resolved by `pip install sympy==1.13.3` (per user's explicit decision).

**Why this matters for future agents:**
- A fresh checkout that runs `pip install -r requirements.txt` will install sympy correctly (the pin is correct). But a checkout that DIDN'T install from a clean requirements run will fail at `import tools.registry` before any test runs.
- The ROADMAP §4 acceptance criterion's literal phrasing ("zero network access, zero real API keys required") assumed tests run on an environment that matches `requirements.txt`. If a future reader is debugging "the test suite won't even collect," check installed packages first — `pip list | findstr /i sympy`.

**Warrants follow-up:** the §4 "Built" subsection in ROADMAP.md flags this so it's discoverable. Whether to also pin pytest in `requirements.txt` (see the matching deviation above) is a related open question.

#### Discovery 2: Real bug in `core/memory.py` resume behavior

While implementing `test_memory_write_through.py`, the test cycle exposed a real production bug in `ConversationMemory`'s resume behavior:

- `add_assistant_message(response)` persists the rich neutral entry dict — `{"role": "assistant", "text": response.text, "tool_calls": [...]}` — as `storage.save_message`'s `content` argument (the dict gets JSON-encoded into the row's content column).
- `storage.get_session_history(thread_id)` returns each row as `{"role": <role>, "content": <json_decoded content>, "name": <name>, "tool_call_id": <tcid>}` — meaning the rich assistant entry dict comes back NESTED under `content`, NOT restored to its original top-level shape.
- A `ConversationMemory(thread_id=...)` resumed with prior assistant turns therefore has `self._messages` of shape `{"role": "assistant", "content": {the rich dict}, "name": None, "tool_call_id": None}` — losing the top-level `text` / `tool_calls` keys that the in-memory representation originally used.

**This affects ROADMAP §1's `--thread <uuid>` CLI flag.** A user resuming a prior thread would NOT see prior assistant turns' `text` content as the agent loop expects. The next model call would receive messages shaped one way (`role`+`content`+nested dict) while the rest of the loop expects another (`role`+`text`+`tool_calls` top-level). At minimum, prior assistant text wouldn't be visible; at worst, the next `call_model` would translate the wrong shape and confuse the provider.

**Why we did NOT fix this in scope:**
- This is a production code bug discovered while writing tests; the user's stated scope was §4 alone ("building the safety net before more hands touch those files") — not fixing bugs the safety net catches.
- A real fix has design choices: align `add_assistant_message` and `get_session_history` symmetrically, OR add a `_normalize_history()` step in `ConversationMemory.__init__` on resume. Each has implications worth a separate design conversation.

**How this is currently documented for future agents:**
- `tests/test_memory_write_through.py`'s module docstring has a "NOTE ON RESUME-SHAPE BEHAVIOR" section describing the asymmetry and what the tests assert (the CURRENT lossy behavior, not the ideal).
- `tests/test_memory_write_through.py::test_resume_existing_thread_loads_messages_from_storage` asserts the CURRENT behavior — assistant message reaches resume as `{"role": "assistant", "content": {...}}`, and the test reaches into `content["text"]` rather than top-level `text` to verify the assistant text round-trips.
- `tests/BREAKING_CHANGES.md` §4 documents this exact behavior and the test update needed if a future fix lands.
- The roadmap "Built" subsection for §4 (in ROADMAP.md) flags this as a real production bug affecting §1's `--thread` acceptance criteria.

**Recommendation for whoever picks up §1:** before §1's `--thread <uuid>` acceptance criterion (`python main.py --thread <uuid> and see prior context intact`) is attempted, the `core/memory.py` resume asymmetry needs fixing OR the §1 acceptance criterion needs weakening to "see prior USER messages intact" (since user messages DO round-trip correctly — they're plain strings, so JSON-encode → JSON-decode restores them perfectly). The full multi-turn resume vision needs the assistant-turn shape fixed first.

---

## 3. Malformed-JSON recovery (+ §5 minimal core pulled forward)

### 3.1 What we built

ROADMAP §3 (bounded malformed-JSON retry) plus the minimal core of ROADMAP §5 (pipeline-run persistence) needed to make a failed run durable, plus two prerequisite bug fixes that §3's mechanism depends on. Production files touched/created:

```
core/client.py                       # ModelResponse gains thread_id: Optional[UUID]
core/loop.py                         # _run() always-persist fix; continue_conversation(); thread_id set on all 3 entry points
core/memory.py                       # _normalize_resumed_history() resume-shape fix (closes §4 Discovery 2)
config.py                            # MAX_JSON_RETRIES = 2
core/reasoning/base.py               # PipelineRun gains run_id: Optional[UUID]
core/reasoning/pipeline_storage.py   # NEW -- PipelineRunRecord + create_pipeline_run + update_pipeline_run
core/reasoning/orchestrator.py       # _run_pass_with_json_retry; 7 call sites wrapped; §5 try/except wrap
conftest.py                          # fake sqlmodel upgraded: construction + in-memory CRUD (add/commit/get)
```

Test files touched/created (suite now 74 tests, ~0.55s):

```
tests/test_json_retry.py             # NEW -- 7 tests for _run_pass_with_json_retry (incl. the crux test)
tests/test_pipeline_storage.py       # NEW -- 4 tests for create/update_pipeline_run + inner-storage-failure caplog
tests/test_orchestrator.py           # 4 -> 7 tests: + failure-path, + success-path, + JSON-retry-via-continue
tests/test_loop_stop_conditions.py   # updated: complete path now persists assistant msg (0 -> 1)
tests/test_loop_tool_dispatch.py     # updated: 2 assistant msgs now persisted (1 -> 2)
tests/test_memory_write_through.py   # updated: resume now returns uniform top-level text/tool_calls shape
```

### 3.2 Questions we asked & your answers

Five initial questions, a follow-up round of four, and one final correction. The decisions below are locked; do not re-litigate without cause.

| # | Dimension | User's choice |
|---|---|---|
| 1 | Retry entry point: reach into `_run()` directly, or add a public `continue_conversation()` wrapper? | **Public `continue_conversation(thread_id, message, ...)`** — no private-method reach-through from the orchestrator. ROADMAP §3 explicitly offered this as the clean alternative. |
| 2 | Include the model's failed output in the retry context? | **Yes** — the model must see what it got wrong. Mechanism: the `_run()` always-persist fix (below) puts the failed turn in the thread automatically; the corrective message ALSO quotes the first 200 chars as a salience cue (belt-and-suspenders, kept for long-output cases). |
| 3 | Corrective message content? | Parse error + first 200 chars of failed output + explicit "respond with ONLY valid JSON, no prose, no code fence" instruction. |
| 4 | Retry budget? | `MAX_JSON_RETRIES = 2` in config.py → 3 total attempts (1 initial + 2 corrective). Mirrors `MAX_PIPELINE_RETRIES`. |
| 5 | Which passes get wrapped? | The 8 JSON-emitting passes (0, 2, 3a, 3b, 3c, 5, 6a, 6c). Pass 1 and Final synthesis stay on bare `_run_pass()` — plain text, nothing to parse. |
| F1 | Pull §5 minimal core forward now, or defer entirely? | **Pull forward the minimal core** — without it, an unrecoverable JSON failure (the exact case §3 produces) loses the whole run. A `PipelineRunRecord` row with status + snapshot makes the failure inspectable. Per-pass checkpointing and `load_pipeline_run()` stay deferred to §5 proper. |
| F2 | `_run()` persistence bug — fix now or work around with a `prior_assistant_response` param? | **Fix the bug** (move `add_assistant_message` up before branching). This is a one-line move that also repairs §1's `--thread` resume path — strictly better than threading a workaround parameter through `continue_conversation`. |
| F3 | Resume-shape bug (§4 Discovery 2) — fix now? | **Fix now** via `_normalize_resumed_history()` in `ConversationMemory.__init__`. §3's retry re-enters a thread via `continue_conversation`; if the resumed assistant turn came back in the lossy nested-`content` shape, `_messages_for_provider` would translate the wrong shape and the retry would silently misbehave. |
| F4 | Mock target in `test_orchestrator.py` — switch to `_run`? | **No switch needed.** The plan originally assumed a `prior_assistant_response` design that would have bypassed `run_deep_research_mode`. With the `_run()` fix + `continue_conversation` design, the orchestrator still calls `run_deep_research_mode` for the initial attempt, so the existing mock (keyed on `run_deep_research_mode`) still intercepts the happy path. Retries go through `continue_conversation`, exercised separately in `test_json_retry.py`. |
| C1 | Final correction: drop the `prior_assistant_response` parameter entirely. | Confirmed — the `_run()` always-persist fix makes it unnecessary. `continue_conversation` has a clean signature; the failed output reaches the model via the resumed thread history, not a parameter. |

### 3.3 What we followed verbatim from ROADMAP §3

- Bounded retry with a corrective follow-up in the SAME pass-thread (not a fresh thread per attempt). ✓
- `MAX_JSON_RETRIES = 2` added to `config.py`, not hardcoded in the orchestrator. ✓
- Corrective message quotes the parse error and instructs "ONLY valid JSON." ✓
- Every JSON-emitting call site (`_parse_json_response(_run_pass(...))`) replaced with the retry wrapper. ✓ (8 sites: Pass 0, 2, 3a, 3b, 3c, 5, 6a, 6c.)
- A trace line is recorded when a retry happens (`run.log(...)` equivalent — the wrapper appends to the `trace` list passed in). ✓
- Acceptance criterion — "mocked response returns malformed JSON first, valid JSON second; pipeline completes on the corrected response; trace records the retry" — satisfied by `test_json_retry.py::test_malformed_then_valid_recovers_in_one_retry` and `test_orchestrator.py::test_json_retry_path_calls_continue_conversation_on_malformed_json`. ✓

### 3.4 What we deviated from §3 / §5, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| §3's reference impl builds its own `ConversationMemory` and calls `RunAgentLoop._run()` directly in a loop | `_run_pass_with_json_retry()` calls `run_deep_research_mode()` for attempt 1, then `continue_conversation()` for retries | User's choice (Q1). Keeps the orchestrator off private methods; `continue_conversation` is the sanctioned re-entry primitive. The reference impl's manual `memory`/`_run` plumbing is exactly what `continue_conversation` encapsulates. |
| §3's reference impl returns the PARSED object (`_parse_json_response(response.text)`) | Wrapper returns the raw validated TEXT; the caller still does `_parse_json_response(...)` on it | Keeps `_run_pass_with_json_retry` symmetric with `_run_pass` (both return `str`), so the 7 call sites are a mechanical `_run_pass` → `_run_pass_with_json_retry` swap with the outer `_parse_json_response(...)` left in place. Less churn, identical behavior. |
| §3's corrective message: `f"That response could not be parsed as JSON: {e}\n\nRespond again with ONLY valid JSON, no prose, no code fence commentary."` | Adds the first 200 chars of the failed output as a located excerpt | User's choice (Q2/Q3) — salience cue for long outputs. The full failed turn is already in context via the always-persist fix; the excerpt points the model at the relevant span. |
| §5 schema: separate `claims_json` / `trace_json` / `coverage_gaps_json` / `final_report` string columns; `status` ∈ {running, complete, failed}; `created_at` / `updated_at` | **Separate columns, matching the spec** (`claims_json` / `trace_json` / `coverage_gaps_json` as `json.dumps`'d strings, `final_report` as plain text); `status` ∈ {running, **complete**, failed}; but **`started_at` / `finished_at`** instead of `created_at` / `updated_at` | The first cut bundled everything into one `snapshot_json` blob, but that was reverted to the spec's separate columns on review. The blob's rationale ("less to maintain", "deserializes cleanly") didn't hold: `load_pipeline_run()` has to return the same four-piece shape regardless of storage, so the unpacking just moves from write-time to read-time; a single blob makes one serialization bug corrupt ALL fields atomically, defeating the crash-resilience purpose; and it broke with this codebase's own `MessageLog` convention (separate typed columns; JSON-encode only the genuinely-variable shapes). `status='complete'` (not `succeeded`) aligns with `ModelResponse.stop_reason`'s existing `"complete"` literal for a normal non-error finish — two words for one idea is an avoidable consistency tax. `started_at`/`finished_at` is the ONE kept deviation: a run has a genuine start and end, and `finished_at = None` while `status='running'` carries real meaning a generic `updated_at`-on-every-write can't express. |
| §5 `update_pipeline_run(run_id, run, status="running")` always writes | `update_pipeline_run(run_id, run, status=None)` — status optional; `finished_at` set only when status is terminal | The minimal core only ever passes a terminal status (`complete`/`failed`), so behavior is identical today. The `Optional[str] = None` signature is kept (rather than the spec's `status="running"` default) because of a concrete future failure mode: when §5 proper bolts on per-pass checkpointing via a data-only `update_pipeline_run(run.run_id, run)` call, a defaulted status would silently reset a `complete`/`failed` record back to that default. `None` = "don't touch status unless told to," making that extension safe by construction rather than by remembering to always pass a value. |

### 3.5 The two prerequisite bug fixes (load-bearing for §3)

Both were discovered during planning, confirmed against the code, and fixed because §3 cannot work correctly without them:

**Fix A — `_run()` always-persist (`core/loop.py`).** `memory.add_assistant_message(response)` moved from the tool-calling-only branch up to immediately after `call_model()` + token-count update, before any stop-condition branching. Previously, a plain-text final answer (stop condition 1) and a budget-exceeded response (stop condition 3) were never persisted — a resumed thread silently lost its last assistant turn. §3's retry depends on the failed assistant turn being in the thread when `continue_conversation` re-enters it; §1's `--thread` resume depends on it for plain-text answers. This closes the loop on §4 Discovery 2's "the assistant turn is dropped" half.

**Fix B — resume-shape normalization (`core/memory.py`).** `_normalize_resumed_history()` runs on the rows `get_session_history()` returns, lifting the assistant entry's `text`/`tool_calls` keys out from under the nested `content` wrapper back to top level (and dropping `None`-valued keys the storage round-trip adds). Without it, a resumed assistant turn has shape `{"role":"assistant","content":{...}}` while a fresh one has `{"role":"assistant","text":...,"tool_calls":[...]}` — `_messages_for_provider` would translate the wrong shape on the retry call. This closes §4 Discovery 2's "the shape is lossy on resume" half. Verified safe against real `storage.py` (which only adds `name`/`tool_call_id` when truthy, so the None-dropping is a no-op there; it matters for the `FakeStorage` test fixture).

### 3.6 Acceptance criteria status

- **§3:** fully satisfied (see §3.3). Malformed-then-valid recovers; trace records the retry; all-fail raises `ValueError` after `MAX_JSON_RETRIES + 1` attempts.
- **§5 minimal core:** satisfied for the scope pulled forward — a run that fails mid-pipeline leaves a durable `PipelineRunRecord` with `status='failed'` and the partial state in its separate columns (`test_orchestrator.py::test_pipeline_failure_marks_run_failed_and_persists_partial_snapshot`); a clean run lands `status='complete'` (`..._success_marks_run_complete`). Per-pass checkpointing and `load_pipeline_run()` remain for §5 proper.

### 3.7 Discoveries during review

A post-implementation review (pyflakes + manual trace) surfaced the following. Items marked FIXED were corrected in the same work; items marked OPEN are flagged for follow-up.

- **FIXED — duplicate test functions in `test_memory_write_through.py`.** The three `test_add_*` functions were accidentally defined twice (the second set shadowed the first, so pytest ran each once and the suite still passed — a silent dead-code trap). Removed the duplicate block.
- **FIXED — dead imports** introduced by this work: `select` in `pipeline_storage.py`; `ConversationMemory` and an unused `continue_calls` local in `test_json_retry.py`; `patch`, `Claim`, `PipelineRun` in `test_orchestrator.py`. Removed. (A follow-up pass also cleaned pre-existing unused `pytest`/`ModelResponse` imports in `test_loop_stop_conditions.py` and `test_loop_tool_dispatch.py`.)
- **FIXED — `snapshot_json` blob reverted to separate columns (post-review).** The first cut stored the whole run in one `snapshot_json` JSON column via a recursive `_to_jsonable()` serializer. Review rejected this (see §3.4 for the full rationale: no real complexity saving, single-point-of-failure for crash-resilience, inconsistent with the `MessageLog` separate-columns convention). `pipeline_storage.py` now uses `claims_json` / `trace_json` / `coverage_gaps_json` / `final_report` as four separate columns per the original §5 spec; `_to_jsonable()` is deleted. This was done before §5 proper builds `load_pipeline_run()` on top of the shape — the cheap window, before it would have become a migration.
- **FIXED — `status='succeeded'` → `'complete'` (post-review).** Aligned with `ModelResponse.stop_reason`'s existing `"complete"` literal for a normal non-error finish (one concept, one word across the codebase).
- **FIXED — inner storage-failure logging (post-review).** The orchestrator's inner except (when `update_pipeline_run` itself fails) now logs at ERROR via a module logger instead of `run.log(...)`. `run.log()` was inert here: the propagated exception carries no reference back to the local `run`, and persistence already failed, so a trace entry would never reach anywhere durable. `logger.error` reaches stderr via Python's lastResort handler even before `logging_setup` is wired in (§1), and is testable via pytest's `caplog`. Covered by `test_pipeline_storage.py::test_inner_storage_failure_propagates_original_exception_and_logs_run_id`, which asserts the ORIGINAL exception propagates AND the log message names the failing `run_id`.
- **OPEN (low) — trace-line phrasing on the final failed attempt.** The retry trace line always says "retrying with corrective message," including on the last in-loop failure that is followed by the post-loop `_parse_json_response` raise (no actual retry follows that one). The line is still accurate for the attempts where a retry DID happen; only the boundary case reads slightly oddly. Not worth a behavior change; documented here so a future reader doesn't "fix" the loop structure thinking it's a bug.
- **OPEN (low) — `vars(c)` claim serialization is shallow.** Every claim-serialization site (orchestrator passes 3a/3b/5/6a/6c/final synthesis, and `pipeline_storage.update_pipeline_run`) uses `vars(c)`, which is correct only while every `Claim` field stays JSON-native. If a nested-dataclass field is ever added to `Claim`, ALL those sites must migrate to `asdict(c)` together, not piecemeal — hardening one site alone would just relocate the fragility and create two serialization mechanisms for the same object. A migration note now lives on `Claim` in `base.py`. (Supersedes the earlier `_to_jsonable` None-dropping concern, which is moot now that the blob is gone.)
- **OPEN (info) — `datetime.utcnow()` deprecation warnings.** `pipeline_storage.py` uses `datetime.utcnow()` to match the existing `storage.py` convention (which already emits the same deprecation warning on Python 3.13). Deliberately consistent with the codebase rather than introducing a divergent timezone-aware style; migrate both files together if/when the project moves off naive UTC.
- **OPEN (info) — columns store model-produced claim text verbatim.** Malicious/garbage content a model emits ends up in `claims_json` / `trace_json`. JSON encoding makes this storage-safe (no injection), but any future UI that renders these columns must HTML-escape them. Not a §3-introduced risk; flagged for the §5/§1 UI work.

### 3.8 Follow-up fix: tool-result resume-shape bug (post-review, separate session)

A subsequent extensive review (checking consistency of everything built against ARCHITECTURE.md/ROADMAP.md/this file, prompted by a project handoff) found that §4's `add_tool_result()` had the **identical root cause** as the assistant-row bug §3.4/§3.7 fixed, left unaddressed: both methods persisted the whole neutral entry dict (`{"role": ..., ...}`) to `storage.save_message`, and the read-side fix (`_normalize_resumed_history()`) only special-cased `role == "assistant"`. A resumed tool-result entry came back with `content` nested under itself — `{"role": "tool", "content": {"role": "tool", "tool_call_id": ..., "content": "..."}, "tool_call_id": ...}` — verified by direct reproduction against the real `storage.py`, not just traced on paper. This is reachable in production: Pass 3a is both JSON-retry-wrapped (§3) and tool-using (grounding), so a malformed-JSON response following a tool call in that pass would resume with a corrupted tool-result turn feeding into `_messages_for_provider()`.

**Root cause of why it went uncaught:** `test_resume_existing_thread_loads_messages_from_storage` asserted the resumed tool entry's `role` and `tool_call_id`, but never its `content` — the identical assertion that *was* written for the assistant row (`assert "content" not in second.messages[1]`) was never applied to the tool row.

**Fix — moved to the write side, removing `_normalize_resumed_history()` entirely** rather than adding a third special case: `add_assistant_message` now persists only `{"text": ..., "tool_calls": ...}`; `add_tool_result` now persists only `str(result)` — neither passes the redundant `"role"` key (already carried by `save_message`'s own `role=` argument) to storage at all. `storage.get_session_history()` now reconstructs the neutral shape directly, per `msg.role`, so no post-hoc normalization step exists in `core/memory.py` for any role, present or future. `tests/conftest.py`'s `FakeStorage.get_session_history()` was updated to mirror this reconstruction — it previously did a naive passthrough that relied on `core/memory.py`'s (now-removed) normalization step, and would have silently diverged from the real `storage.py` if left as-is.

**Regression tests added:** `test_memory_write_through.py` now asserts the tool row's resumed `content` matches its fresh-session shape exactly (the specific assertion that was missing), plus a new assertion on the tool row's *saved* content in the non-resume write-through test. Verified via direct reproduction against real `storage.py` for both the tool-row fix and (as a non-regression check) the assistant-row path, before considering this closed.

**The general lesson, worth keeping in mind for future fixes in this file:** when a bug's fix is a per-case branch added to a normalizer/translator/dispatcher, that's usually a sign the fix belongs one layer down, at the point where the divergent cases are produced — not one layer up, at the point where they're consumed. Fixing the write side once closes a bug for every current *and future* case; patching the read side closes it only for the case that happened to be noticed first.

---

## 5. Durable `PipelineRun` persistence (§5 proper)

### 5.1 What we built

The remainder of ROADMAP §5 on top of the minimal core that was pulled forward with §3 (see §3.4 above for Phase 1). Two pieces:

1. **Per-pass checkpointing** — a data-only `update_pipeline_run(run.run_id, run)` call (no status argument) after every `run.log(...)` trace line in `orchestrator.py`. 16 sites total: Pass 0, 1, 2, D0, 3a, 3b, "3a/3b skipped" (else branch), 3c, 5, 4, D1, 6a (while loop), 6c (while loop), D2 per-claim (for loop inside while loop), 6b, Merge. The only `run.log(...)` NOT followed by a data-only checkpoint is "Final synthesis complete.", which is immediately followed by the terminal `update_pipeline_run(run.run_id, run, status="complete")` — adding a data-only checkpoint right before it would be a redundant write of identical data.

2. **`load_pipeline_run(run_id) -> dict`** — the read API in `pipeline_storage.py`. Returns 9 fields: `id`, `user_query`, `status`, `started_at`, `finished_at`, `claims` (list of raw dicts), `trace` (list of strings), `coverage_gaps` (list of dicts), `final_report` (plain text). Raises `ValueError` for a nonexistent `run_id`, matching `update_pipeline_run`'s convention.

Production files touched: `core/reasoning/pipeline_storage.py` (docstring update + `load_pipeline_run`), `core/reasoning/orchestrator.py` (docstring update + 16 checkpoint calls).

Test files touched: `tests/test_pipeline_storage.py` (docstring update + import + 3 new tests), `tests/test_orchestrator.py` (docstring update + import + 2 new tests + 1 updated test).

### 5.2 Questions we asked & your answers

Three questions before implementation:

| # | Dimension | User's choice |
|---|---|---|
| 1 | `load_pipeline_run` return shape: include `started_at`/`finished_at`, or follow the spec's original 7-field return dict? | **Include them** — the schema already has them (locked Phase 1 deviation), `finished_at=None` while running carries real meaning, and the spec's return shape predates the deviation. |
| 2 | Checkpoint granularity: after EVERY `run.log(...)` literally (including D2's per-claim lines inside the while loop), or one per logical pass/step? | **Every `run.log(...)` literally** — the spec's stated intent is "fine-grained recoverability without a separate design decision." Every checkpoint does write new data (the trace grew by one line), and the SQLite writes are cheap. |
| 3 | `load_pipeline_run` claims: raw dicts (spec) or reconstructed `Claim` dataclass instances? | **Raw dicts**, with an addition: the docstring carries "raw dicts now; if/when programmatic pipeline resume is built, that function reconstructs Claim objects itself, using the same field-filtering pattern `_claim_from_json` already established." |

### 5.3 What we followed verbatim from ROADMAP §5

- Checkpoint placement: "right after each `run.log(...)` call already in the function." ✓ (16 sites, every `run.log(...)` except the one immediately before the terminal update.)
- `load_pipeline_run` return shape: dict with `id`, `user_query`, `status`, `claims` (json.loads), `trace` (json.loads), `coverage_gaps` (json.loads), `final_report` (plain text). ✓ (Plus `started_at`/`finished_at` per Q1.)
- `load_pipeline_run` raises `ValueError` for a nonexistent `run_id`. ✓
- Acceptance criterion: crash after Pass 3b → `load_pipeline_run` returns claims populated, not empty. ✓ (Both the `except Exception` path and the hard-kill `BaseException` path.)

### 5.4 What we deviated from §5, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| `update_pipeline_run(run.run_id, run, status="running")` for per-pass checkpoints | `update_pipeline_run(run.run_id, run)` — no status argument | Phase 1's locked deviation: `status` defaults to `None`, not `"running"`. A data-only checkpoint must not touch status — passing `status="running"` would silently reset a `"complete"`/`"failed"` record if a checkpoint somehow fired after a terminal transition. `None` = "don't touch status unless told to." |
| (spec silent on hard-kill behavior) | `except Exception` block does not catch `KeyboardInterrupt`/`SystemExit` — the per-pass checkpoint is what makes the hard-kill case recoverable | Not a deviation from the spec's letter (the spec says "kill the process" as an acceptance-criterion alternative), but worth documenting: the checkpoint's value is precisely that it survives a `BaseException` that bypasses the except block. The hard-kill test (`test_hard_kill_bypasses_except_block_checkpoint_survives`) proves this. |
| (spec's return dict has 7 fields) | Return dict has 9 fields (adds `started_at`, `finished_at`) | User's choice (Q1). The spec's return shape was written before the `started_at`/`finished_at` deviation was locked in Phase 1. |
| Checkpoint after "Final synthesis complete." | No data-only checkpoint here — the terminal `status="complete"` update immediately follows | The terminal update writes the same data plus the status transition. A data-only checkpoint right before it would be two writes of identical data columns with no intervening mutation. The spec's "after every pass" intent is satisfied by the terminal update itself. |

### 5.5 Acceptance criteria status

ROADMAP §5's acceptance criterion: *"kill the process (or raise a forced exception) mid-pipeline after Pass 3b completes; confirm `load_pipeline_run(run_id)` returns a record with `status="failed"` (or `"running"` if you didn't hit the except branch) and `claims` populated with whatever was captured through Pass 3b, not empty."*

**Fully satisfied:**
- `test_crash_after_pass_3b_leaves_claims_populated_via_load`: forced `RuntimeError` on Pass 3c → `status="failed"`, 3 claims with grounding/critic fields populated, trace through Pass 3b, empty final_report. ✓
- `test_hard_kill_bypasses_except_block_checkpoint_survives`: `KeyboardInterrupt` on Pass 3c (bypasses `except Exception`) → `status="running"`, `finished_at=None`, 3 claims from the last checkpoint, trace through Pass 3b. ✓

### 5.6 Test changes

- `tests/test_pipeline_storage.py`: 4 → 7 tests. Three new: round-trip (create → update → load, all 9 fields), nonexistent `run_id` raises `ValueError`, just-created record returns empty defaults with `finished_at=None`.
- `tests/test_orchestrator.py`: 7 → 9 tests. Two new (the acceptance-criterion pair above). One updated: `test_pipeline_success_marks_run_complete` now asserts the LAST call has `status="complete"` and all preceding calls have `status=None` (data-only checkpoints), plus at least one data-only call exists.
- Full suite: 79 tests, ~0.53s, zero network/API keys.

### 5.7 Discoveries during implementation

- **The hard-kill test initially failed** because patching `create_pipeline_run` to return a bare UUID (without calling through to the real implementation) skipped the actual record creation in the fake sqlmodel. The first per-pass checkpoint then raised `ValueError: No pipeline run found with id ...`. Fix: use the same spy-through-to-real pattern as the crash test (call `real_create(user_query)`, capture the returned `run_id`). This is a test-construction issue, not a production bug — the production code always calls the real `create_pipeline_run`.

---

## 9. Google Gemini support

### 9.1 What we built

Full Google Gemini provider support in `core/client.py` — tool schema translation, message history translation, and the `call_model` GOOGLE branch — replacing the three `NotImplementedError` stubs. Verified against the installed `google-genai==1.0.0` SDK by inspecting Pydantic model fields at runtime (not guessed from docs).

**`_tools_for_provider` GOOGLE branch:** wraps all tool schemas in a single `types.Tool(function_declarations=[...])` containing one `types.FunctionDeclaration` per schema. The `input_schema` dict is passed directly as `parameters` — the SDK's Pydantic validation auto-converts it to `types.Schema` (including string-to-enum conversion for the `type` field, e.g. `"object"` → `Type.OBJECT`).

**`_messages_for_provider` GOOGLE branch:** builds a `{tool_call_id: function_name}` lookup by scanning all assistant messages in the neutral history (needed because Google's `FunctionResponse` requires `name` but the neutral tool-result shape only has `tool_call_id`). Translates: user → `Content(role="user", parts=[Part(text=...)])`, assistant → `Content(role="model", parts=[...])` with text and `function_call` parts, tool result → `Content(role="user", parts=[Part(function_response=FunctionResponse(name=looked_up, response={"result": ...}))])`.

**`call_model` GOOGLE branch:** calls `client.models.generate_content(model=..., contents=..., config=types.GenerateContentConfig(system_instruction=..., tools=...))`. Parses `response.candidates[0].content.parts` manually — `response.text` raises `ValueError` when function_call parts are present (confirmed by reading the SDK source). Generates a UUID via `uuid4()` when `FunctionCall.id` is `None` (the SDK field is optional). Usage from `response.usage_metadata.prompt_token_count` / `candidates_token_count`.

**Root `conftest.py` fake expansion:** the Google fake was expanded from a no-op `Client` to include a `types` submodule with all 7 classes the production code constructs (`Tool`, `FunctionDeclaration`, `FunctionCall`, `FunctionResponse`, `Part`, `Content`, `GenerateContentConfig`), plus `models.generate_content()` on the client with a `set_responses()` queue for canned responses. `google.genai.types` registered in `sys.modules`.

### 9.2 Questions we asked & your answers

No clarifying questions were needed — the ROADMAP §9 spec explicitly flagged which shapes needed SDK verification vs. which were confirmed safe. The verification was done by inspecting the installed `google-genai==1.0.0` package's Pydantic model fields at runtime, confirming every field name and type before writing code.

### 9.3 What we followed verbatim from ROADMAP §9

- `client.models.generate_content(model=..., contents=..., config=GenerateContentConfig(system_instruction=..., tools=...))` call pattern. ✓
- System prompt via `system_instruction` in `GenerateContentConfig` (third distinct pattern). ✓
- Roles: `"user"` and `"model"` (NOT `"assistant"`). ✓
- Tool schema: `Tool(function_declarations=[FunctionDeclaration(name=, description=, parameters=)])`. ✓
- Message translation: user→text Part, assistant→model role with text + function_call Parts, tool result→user role with function_response Part. ✓
- Response parsing: iterate parts manually (spec warned `response.text` may not work with tools — confirmed it raises `ValueError`). ✓

### 9.4 What we deviated from §9, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| `parameters=schema["input_schema"]  # may need conversion to types.Schema, verify` | Passed raw dict directly — SDK auto-converts | Verified at runtime: `FunctionDeclaration(parameters={"type": "object", ...})` produces a valid `types.Schema` with `Type.OBJECT`. No manual conversion needed. |
| (spec silent on `FunctionCall.id`) | Generate `uuid4()` when `fc.id` is `None` | SDK field is `Optional[str]` with default `None`. Our neutral shape requires `id` for tool-call/result matching. |
| (spec silent on `FunctionResponse.name` lookup) | Build `{tool_call_id: name}` dict from assistant messages | Neutral tool-result shape has `tool_call_id` + `content` but no `name`. Google's `FunctionResponse` requires `name`. Lookup avoids changing the neutral shape or `core/memory.py`. |
| (spec silent on conftest fake expansion) | Expanded Google fake with `types` submodule + `models.generate_content()` | The old fake was a no-op `Client` because the GOOGLE branch raised `NotImplementedError`. Now that the branch is implemented, the fake needs the types the production code constructs and a `generate_content` method for `call_model` tests. |
| `from google import genai` (existing import) | Added `from google.genai import types as genai_types` | The production code needs `genai_types.Tool(...)`, `genai_types.Part(...)`, etc. Explicit import is clearer than `genai.types.Tool(...)`. |
| (spec silent on `response.text` behavior) | Parse parts manually, never call `response.text` | Reading the SDK source confirmed: `response.text` raises `ValueError` if any part has a non-text, non-thought field (like `function_call`). |

### 9.5 Acceptance criteria status

ROADMAP §9's acceptance criterion: *"a real (not mocked) call to Google with one tool available successfully round-trips: model requests the tool, the tool dispatches, the result gets translated back into a `FunctionResponse`-shaped turn, and a second call produces a final answer. Verified with a real API key."*

**Partially satisfied offline.** The translation shapes and response parsing are verified by 8 offline tests (tool schema, 4 message translation, 3 call_model). The end-to-end round-trip with a real API key requires credentials and network access — not testable in the offline suite. The SDK shapes were verified against the installed `google-genai==1.0.0` package at implementation time, which addresses the spec's concern about "the exact object construction above was explicitly NOT verified against current SDK behavior."

### 9.6 Test changes

- `tests/test_client_translation.py`: replaced `test_tools_google_not_implemented` with `test_tools_google_wraps_in_function_declarations`; replaced `test_messages_google_not_implemented` with 4 positive tests (user, assistant role, assistant tool call, tool result name lookup); added 3 `call_model` tests (text-only, tool call with UUID generation, preserved id); updated round-trip test to include Google. Net: +6 tests (14 → 20).
- `conftest.py`: expanded Google fake (not a test file, but required for the new tests).
- Full suite: 205 tests.

---

## 11. Critic-model routing

### 11.1 What we built

A config-driven routing layer in the orchestrator that sends the critic/grounding passes (3a, 3b, 6c) to a different model than the generator, preventing a model from checking its own output with the same blind spots.

**`config.py`:** added `CRITIC_MODEL: dict | None = None`. When set, it must be a dict with `"provider_name"` and `"model"` keys (e.g. `{"provider_name": "OPENAI", "model": "gpt-5.1"}`). `None` (the default) means no special routing — every pass uses the same provider/model.

**`core/reasoning/orchestrator.py`:** after `run.run_id = create_pipeline_run(user_query)`, the function resolves:

```python
critic_provider = config.CRITIC_MODEL["provider_name"] if config.CRITIC_MODEL else provider_name
critic_model = config.CRITIC_MODEL["model"] if config.CRITIC_MODEL else model
```

Then Pass 3a, 3b, and 6c calls use `critic_model, critic_provider` instead of `model, provider_name`. All other passes (0, 1, 2, 3c, 5, 6a, Final synthesis) are unchanged. No changes to `_run_pass`, `_run_pass_with_json_retry`, `RunAgentLoop`, or `core/client.py`.

### 11.2 Questions we asked & your answers

No clarifying questions were needed — the ROADMAP §11 spec is a complete, no-decisions-left brief with exact code shown. The only style note: the spec uses `Optional[dict]` but the project's existing convention (e.g. `list[str] | None` in orchestrator.py) uses the modern `|` syntax, so `dict | None` was used instead.

### 11.3 What we followed verbatim from ROADMAP §11

- `CRITIC_MODEL` config constant with `None` default and example dict shape. ✓
- Resolve `critic_provider`/`critic_model` once near the top of `run_deep_research_pipeline()`. ✓
- Pass 3a, 3b, 6c use critic model; all other passes use main model. ✓
- Pass 6a stays on the generator model (revision reflects the generator's voice/style). ✓
- No changes to `_run_pass`, `RunAgentLoop`, or `core/client.py`. ✓
- Acceptance criterion verified via mock call log: 3a/3b/6c with `OPENAI`/`gpt-5.1`, all others with `ANTHROPIC`/`claude-test`. ✓

### 11.4 What we deviated from §11, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| `CRITIC_MODEL: Optional[dict] = None` | `CRITIC_MODEL: dict \| None = None` | Project convention uses modern `|` syntax (see `list[str] \| None` in orchestrator.py). Avoids importing `Optional` from `typing`. |
| (spec silent on docstring update) | Updated orchestrator module docstring to document §11 routing | The old docstring said "no critic-model-routing" which was now stale. |
| (spec silent on config deferred comment) | Removed `critic_model` line from the "Deferred" comment block in config.py | It's no longer deferred. |

### 11.5 Acceptance criteria status

ROADMAP §11's acceptance criterion: *"run the pipeline (mocked) with `config.CRITIC_MODEL = {"provider_name": "OPENAI", "model": "gpt-5.1"}` and an Anthropic `provider_name`/`model` for everything else — confirm (via the mock's call log) that Pass 3a/3b/6c calls were made with `provider_name="OPENAI"` while Pass 0/1/2/3c/5/6a/final synthesis were made with the Anthropic values."*

**Fully satisfied:** `test_critic_routing_sends_3a_3b_6c_to_critic_model` asserts exactly this. A second test (`test_no_critic_model_means_uniform_routing`) confirms the no-op path when `CRITIC_MODEL` is `None`.

### 11.6 Test changes

- `tests/test_critic_routing.py` (NEW): 2 tests — routing with CRITIC_MODEL set (asserts 3a/3b/6c use critic provider/model, all others use main), and no-op routing with CRITIC_MODEL=None (asserts uniform provider/model across all passes).
- Full suite: 199 tests.

---

## 12. `/output/<run_id>/` file-writing system

### 12.1 What we built

`core/reasoning/output_writer.py` (NEW): `write_run_artifacts(run) -> str` writes the full human-browsable artifact directory under `OUTPUT_DIR/<run_id>/`. File layout: `00_plan.md`, `01_raw_response.md`, `02_claims.json`, `03_grounding.json`, `03_critic.json`, `03_completeness.json`, `04_confidence.json`, `05_assumptions.json`, conditional `06_revisions.json`, `confidence_chart.png` (matplotlib bar chart), `trace.md`, `report.md`, graceful-skip `report.pdf`, plus `sources/` and `code/` placeholder subdirs.

Supporting changes: `config.py` gains `OUTPUT_DIR` (env-overridable, default `./output`); `main.py`'s `run_research` calls `write_run_artifacts` and prints the path; `requirements.txt` gains `matplotlib==3.10.3`; `.gitignore` gains `output/`.

### 12.2 Questions we asked & your answers

One question:

| # | Dimension | User's choice |
|---|---|---|
| 1 | matplotlib: hard dependency in requirements.txt, or optional (graceful skip)? | **Hard dependency** — the spec says to, and the acceptance criterion requires a real PNG chart. A fresh `pip install` should produce charts out of the box. |

### 12.3 What we followed verbatim from ROADMAP §12

- File layout: all numbered artifacts + trace.md + report.md + confidence_chart.png + conditional 06_revisions.json. ✓
- `sources/` and `code/` placeholder subdirs created. ✓
- `_write_confidence_chart`: matplotlib Agg backend, bar chart of tier distribution, UNVERIFIED_COVERAGE counted from `run.coverage_gaps`. ✓
- `_try_write_pdf`: graceful degradation stub (markdown + weasyprint not installed → silent skip). ✓
- `write_run_artifacts` NOT called from `run_deep_research_pipeline` — caller's decision. ✓
- `vars(c)` claim serialization (matches every other site). ✓
- `OUTPUT_DIR` env-overridable via `AGENT_OUTPUT_DIR`. ✓

### 12.4 What we deviated from §12, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| (spec silent on `run_id is None`) | `write_run_artifacts` raises `ValueError` if `run.run_id is None` | Without the guard, tests that bypass persistence would silently create a directory named `"None"`. In production this can't happen (§5's `create_pipeline_run` sets `run_id` up front). |
| Spec's `run_research` drops the trace/run_id printing | Kept the existing trace + run_id output, added the artifacts path line | The §1 CLI already prints these; dropping them would be a regression. The spec's version was written before §1 was built. |

### 12.5 Acceptance criteria status

ROADMAP §12's acceptance criterion: *"running the CLI in research mode produces a real directory under `./output/<uuid>/` containing every file listed above... and `confidence_chart.png` is a real, valid PNG showing the correct tier counts for that run."*

**Implementation complete; end-to-end filesystem verification requires a real API key** (the pipeline needs live LLM calls). The artifact writer itself is verified by 5 tests in `tests/test_output_writer.py` against `tmp_path`: file layout (all 12 files + 2 subdirs), content round-trip (claims JSON, trace, report), conditional revisions file, None-run_id guard, and PNG validity (magic bytes check). The research-mode e2e test verifies the `run_research → write_run_artifacts` wiring.

### 12.6 Test changes

- `tests/test_output_writer.py` (NEW): 5 tests.
- `tests/test_e2e.py`: research-mode test updated to mock `write_run_artifacts` and assert it's called + artifacts path printed.
- Full suite: 96 tests. First run ~6.85s (matplotlib font caching on first import); subsequent runs faster.

---

## 6. `tools/builtin/file_ops.py`

### 6.1 What we built

Three file-operation tools — `read`, `write`, `edit` — in `tools/builtin/file_ops.py`, registered as three separate `ToolSpec`s to match `config.ToolPermissions`'s three separate boolean fields (`read` / `write` / `edit`). This allows review/audit agents to be given read access without write or edit.

Supporting changes: `config.py` gains `WORKSPACE_DIR`, `MAX_FILE_SIZE_BYTES` (10 MB), `MAX_READ_LINES` (500), `MAX_READ_CHARS` (50 000). `tools/base.py` gains `approval_check: Optional[Callable[[str, dict], bool]]` on `ToolSpec`. `tools/registry.py` dispatch uses `approval_check` when present, falling back to `requires_approval()`.

### 6.2 Questions we asked & your answers

Four questions before implementation:

| # | Dimension | User's choice |
|---|---|---|
| 1 | markitdown: plain text only, or use for rich formats (PDF, DOCX, etc.)? | **Use markitdown for rich formats** — the dependency is already in requirements.txt; leverage it. |
| 2 | Within-workspace approval: config boolean can override, or always auto-approved? | **Config can override** — `ToolApprovals.write = True` forces approval even within workspace. |
| 3 | Read line-count behavior: hard error if count > max, or clamp + inform? | **Clamp + inform** — silently clamp to MAX_READ_LINES but include a note in the response. |
| 4 | File size guard: keep a hard reject for enormous files, or rely on truncation only? | **Yes, keep a size guard** — 10 MB hard reject before opening prevents memory exhaustion on the markitdown path. |

### 6.3 What we followed verbatim from ROADMAP §6

- Three separate tools matching `config.ToolPermissions`'s three boolean fields. ✓
- `write` creates or overwrites; parent directories created automatically. ✓
- `edit` uses unique-match str_replace semantics (0 matches → error, >1 → error). ✓
- Pydantic params models with `TOOL_SCHEMA` dicts in Anthropic native shape. ✓

### 6.4 What we deviated from §6, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| `_resolve_safe_path` hard-rejects paths outside WORKSPACE_DIR | `_resolve_path` resolves symlinks/`..` but never rejects; approval layer decides | User's requirement: outside-workspace paths should ask the user, not be hard-rejected. The approval model (within workspace = auto-approved unless config override; outside = always ask) replaces the hard reject. |
| Read tool reads entire file (up to MAX_FILE_SIZE_BYTES) | Read tool accepts `offset`/`count` params with MAX_READ_LINES/MAX_READ_CHARS truncation | User's requirement: pagination to avoid API context limits. Count clamped to max with a note (not hard error). Char truncation also noted. |
| Read tool is plain-text only (`open(path, "r")`) | Rich extensions (.pdf, .docx, .pptx, etc.) use markitdown; text extensions use `open()` | User's choice (Q1). markitdown was already a dependency. |
| (spec silent on approval model) | `ToolSpec.approval_check` field added to `tools/base.py`; file_ops provides `_file_approval_check` | Needed for path-dependent approval without violating the permissions/tools layering boundary. `security/permissions.py` stays generic; the tool defines its own approval policy. |
| `MAX_FILE_SIZE_BYTES = 1_000_000` (1 MB) | `MAX_FILE_SIZE_BYTES = 10_000_000` (10 MB) | Context protection is now handled by MAX_READ_LINES/MAX_READ_CHARS; the size guard is purely for memory protection (especially markitdown which loads the whole file). |

### 6.5 Acceptance criteria status

ROADMAP §6's acceptance criterion: *"attempting to read/write/edit a path like `../../etc/passwd` or an absolute path like `/etc/passwd` is rejected with a clear error, verified by an actual test."*

**Deviated per user decision:** outside-workspace paths are NOT rejected — they require user approval. The tests verify the approval model (within workspace + config False → no approval; outside workspace → approval required). A round-trip (write → read → edit → read) inside the workspace succeeds. 31 tests in `tests/test_file_ops.py`.

### 6.6 Test changes

- `tests/test_file_ops.py` (NEW): 31 tests — path resolution (5), approval check (4), read tool (9), write tool (4), edit tool (4), registry integration (5).
- Full suite: 131 tests.

---

## 7. `tools/builtin/shell.py` + `security/sandbox.py`

### 7.1 What we built

Cross-platform shell execution tool with a hybrid sandbox: Docker (default, strong isolation) or subprocess + hardening (fallback, weak isolation, requires explicit opt-in). Inert commands (read-only inspection from `config.INERT_COMMANDS` with no shell metacharacters and no path-qualified binary) bypass both backends via a lightweight subprocess.

`security/sandbox.py` (NEW): `detect_shell()`, `is_docker_available()`, `_is_inert()`, `_needs_network()`, `_scrubbed_env()`, `_run_inert()`, `_run_docker()` (with `Popen` + `docker kill` on timeout), `_run_subprocess_fallback()`, `run_sandboxed()` with `docker_available` parameter for TOCTOU safety.

`tools/builtin/shell.py` (NEW): `ShellParams`, `TOOL_SCHEMA`, `_shell_approval_check()` (layered: base config → inert → Docker → fallback gate), `run()` with TOCTOU safety net.

Supporting changes: `config.py` gains 10 sandbox constants (`SHELL_BINARY`, `ALLOW_INSECURE_SANDBOX_FALLBACK`, `AUTO_APPROVE_SANDBOX_FALLBACK`, `SANDBOX_DOCKER_IMAGE`, timeout/memory/CPU/PID limits, `NETWORK_ALLOWED_COMMANDS`, `INERT_COMMANDS`). `tools/registry.py` registers shell with `approval_check`.

### 7.2 Questions we asked & your answers

Seven questions across two rounds:

| # | Dimension | User's choice |
|---|---|---|
| 1 | Sandbox technology: Docker, subprocess+hardening, or hybrid? | **Hybrid** — Docker by default; subprocess fallback only if `ALLOW_INSECURE_SANDBOX_FALLBACK=True` with explicit safety gate. |
| 2 | Network access: no network by default, or allowed? | **No network by default** with configurable allowlist of commands that get network (pip, curl, git, etc.). |
| 3 | Command allowlist: no allowlist, blocklist only, or allowlist for inert + sandbox for rest? | **Allowlist for inert + sandbox for rest** — read-only inspection commands bypass the full sandbox. |
| 4 | Shell binary: auto-detect + config override, bash only, or agent chooses per call? | **Auto-detect + config override** — bash on Linux/macOS, pwsh on Windows. |
| 5 | Windows + Docker: Docker=bash with fallback=pwsh, Windows always fallback, or Windows Docker containers? | **Docker=bash, fallback=pwsh** — Docker runs Linux containers; pwsh requires the fallback. |
| 6 | Fallback approval: per-run by default with auto-approve config, or always auto-approved? | **Per-run by default** — `AUTO_APPROVE_SANDBOX_FALLBACK=True` to auto-approve. |
| 7 | Resource defaults: moderate, generous, or restrictive? | **Generous** — 60s timeout, 1GB memory, 30s CPU, 200 PIDs. |

### 7.3 What we followed verbatim from ROADMAP §7

- `security/sandbox.py` kept separate from `tools/builtin/shell.py` for modularity. ✓
- Scrubbed environment (no API keys, minimal PATH). ✓
- Wall-clock timeout on all paths. ✓
- `config.ToolPermissions.shell = False` and `config.ToolApprovals.shell = True` defaults unchanged. ✓

### 7.4 What we deviated from §7, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| Strict command allowlist as the security boundary; `shell=False`, pre-split args | Allowlist classifies inert vs non-inert; non-inert uses `shell=True` with bash/pwsh; sandbox is the security boundary | User's requirement: real shell execution (scripts, pipelines), not just read-only inspection commands. |
| Process-level sandboxing only; Docker explicitly deferred | Docker is the default backend; subprocess is the fallback | User's requirement: strong isolation by default. The ROADMAP's "usable, not production-grade" scope was overridden by the user's security priority. |
| Unix-only (`preexec_fn` + `rlimit`) | Cross-platform: Docker works everywhere; fallback uses rlimit on Unix, timeout-only on Windows | User's requirement: Windows support (PowerShell). |
| No network concept | `NETWORK_ALLOWED_COMMANDS` allowlist; `--network none` in Docker for non-matching commands | User's requirement: some commands need network (pip install, git clone). |
| `CPU_TIME_LIMIT_SECONDS = 5`, `MEMORY_LIMIT_BYTES = 256 MB`, `WALL_CLOCK_TIMEOUT_SECONDS = 10` | 60s timeout, 1GB memory, 30s CPU, 200 PIDs | User's choice (Q7) — generous defaults for real scripts. |
| (spec silent on fallback approval) | `AUTO_APPROVE_SANDBOX_FALLBACK` config; per-run approval by default | User's requirement: explicit safety gate for the insecure path. |
| (spec silent on TOCTOU) | `run_sandboxed` accepts `docker_available` param; `shell.run()` probes once and threads it through; safety net returns error if Docker died after approval | Security review finding: independent Docker probes in approval check vs execution allow silent downgrade to insecure fallback. |

### 7.5 Security review findings (post-implementation `/review --effort high`)

Five Critical and eight Suggestion findings from a high-effort security review. All fixed:

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | Critical | `find -delete` / `sort -o` in INERT_COMMANDS bypass sandbox | Removed `find`/`sort` from allowlist; added `_DANGEROUS_FLAGS` denylist in `_is_inert()` |
| 2 | Critical | Docker timeout kills CLI client but not container | Rewrote `_run_docker` with `Popen` + `--name sandbox-{uuid}` + `docker kill` on timeout |
| 3 | Critical | TOCTOU: approval check and execution probe Docker independently | `run_sandboxed` accepts `docker_available` param; `shell.run()` probes once |
| 4 | Critical | `_needs_network` matches keywords anywhere in command string | Now matches only the first word (command name) |
| 5 | Critical | `os.path.basename` allows attacker-planted `./ls` binary | Removed basename stripping; reject path-qualified tokens (`/`, `\`, `.` prefix) |
| 6 | Suggestion | `detect_shell()` at import time blocks on Windows | Computed once at module level with try/except fallback |
| 7 | Suggestion | `Optional[callable]` type error | Changed to `Optional[Callable[[], None]]` |
| 8 | Suggestion | `shell_binary` unused in `_run_inert` | Prefixed with underscore |
| 9-13 | Suggestion | Test gaps (or→and, convoluted assertion, detect_shell not patched, network=False untested, backend internals untested) | Fixed all; added 13 new tests |

### 7.6 Acceptance criteria status

ROADMAP §7's acceptance criterion: *"a command not on the allowlist is rejected before `subprocess.run` is ever called."*

**Deviated per user decision:** the allowlist is not the security boundary — the sandbox is. Non-allowlisted commands run inside Docker (or the fallback). The acceptance criterion is reinterpreted as: inert commands run without sandbox; non-inert commands run inside Docker with volume mount and network isolation. Verified by 44 tests in `tests/test_shell.py`.

### 7.7 Test changes

- `tests/test_shell.py` (NEW): 44 tests — detect_shell (3), docker available (3), _is_inert (7), _needs_network (6), scrubbed env (2), backend internals (8), routing (7), approval check (5), registry integration (3).
- Full suite: 175 tests.

---

## 8. `safety/policy_enforcement.py`

### 8.1 What we built

Content-level output policy applied after every tool call via `registry.dispatch()`. Two responsibilities:

1. **Centralized blocked-domain list** — `BLOCKED_DOMAINS` set with 3 default entries (well-known harmful domains). `is_domain_blocked(url)` helper used by both `web_search.py` and `fetch_url.py`.
2. **Secret redaction** — 7 regex patterns (OpenAI, Anthropic, GitHub, AWS, Google, Slack, PEM private keys). `redact_secrets(text)` replaces matches with `[REDACTED]`. `check_output_policy(tool_name, result)` scans `content`, `result`, `stdout`, `stderr` keys.

Supporting changes: `tools/registry.py` imports and calls `check_output_policy` in `dispatch()`. `tools/builtin/web_search.py` removes its local `BLOCKED_DOMAINS` and imports `is_domain_blocked`. `tools/builtin/fetch_url.py` adds `is_domain_blocked` check before fetching. `safety/__init__.py` created (was missing, caused import failures).

### 8.2 Questions we asked & your answers

Three questions:

| # | Dimension | User's choice |
|---|---|---|
| 1 | fetch_url enforcement: also check blocked domains, or ROADMAP spec only (web_search)? | **Yes, enforce in fetch_url too** — closes the gap where fetch_url could bypass the blocklist. |
| 2 | Default blocked domains: empty set, or small default list? | **Small default list** of well-known harmful domains; user maintains based on threat model. |
| 3 | Secret scan extensions: add stderr, extra patterns, recursive scanning? | **stderr + extra patterns** (AWS, Google, Slack, PEM keys). No recursive scanning. |

### 8.3 What we followed verbatim from ROADMAP §8

- `check_output_policy` called by `registry.dispatch()` after every tool handler. ✓
- `web_search.py` removes its own `BLOCKED_DOMAINS` and imports from centralized module. ✓
- Secret redaction as defense-in-depth on tool output. ✓
- `BLOCKED_DOMAINS` as a set, not per-tool. ✓

### 8.4 What we deviated from §8, and why

| Spec language | Actual implementation | Why |
|---|---|---|
| 3 secret patterns (OpenAI, Anthropic, GitHub) | 7 patterns (+ AWS `AKIA...`, Google `AIza...`, Slack `xox[baprs]-...`, PEM private key headers) | Defense-in-depth; these are common key formats that the ROADMAP's 3 patterns miss. |
| Scans `content`, `result`, `stdout` | Also scans `stderr` | Shell tool's stderr can contain leaked secrets via error messages. |
| `web_search.py` imports `BLOCKED_DOMAINS` and checks in `_normalize()` | `web_search.py` imports `is_domain_blocked(url)` helper | Cleaner API; handles URL parsing centrally. Also used by `fetch_url.py`. |
| (spec silent on fetch_url) | `fetch_url.py` also enforces blocked domains | User's requirement: all web-facing tools respect the blocklist. |
| `BLOCKED_DOMAINS: set[str] = set()` (empty) | 3 default entries with maintenance comment | User's choice (Q2) — non-empty list demonstrates the mechanism. |
| (spec silent on `safety/__init__.py`) | Created empty `__init__.py` | Was missing; `from safety.policy_enforcement import ...` failed without it. |

### 8.5 Acceptance criteria status

ROADMAP §8's acceptance criterion: *"a tool result containing a string matching one of the secret patterns comes back from `registry.dispatch()` with `[REDACTED]` in place of the match."*

**Fully satisfied:** `test_dispatch_redacts_secret_from_tool_output` registers a fake tool returning a planted OpenAI key, dispatches it, and asserts the key is replaced with `[REDACTED]`. 22 tests in `tests/test_policy_enforcement.py`.

### 8.6 Test changes

- `tests/test_policy_enforcement.py` (NEW): 22 tests — redact_secrets (9), is_domain_blocked (6), check_output_policy (6), registry integration (1).
- Full suite: 197 tests.

## 13. Streaming refactor — generator `_run()` + `call_model_stream()`

### 13.1 What we built

Converted `RunAgentLoop._run()` from a synchronous function returning one `ModelResponse` into a **generator** yielding `LoopEvent` objects (new `core/events.py`), so the TUI (§16), subagents (§18), and MCP (§17) can observe token deltas, tool calls, and permission prompts in real time. Added `call_model_stream()` to `core/client.py` (three provider streaming implementations), `collect_response()` (drain a stream to one `ModelResponse`), `StreamToken`, and `run_to_completion()` in `core/loop.py` so the three public wrappers stay synchronous and every existing caller still gets a `ModelResponse`.

### 13.2 Questions we asked & your answers

The §13 plan was validated by a high-effort self-review (9 finder agents + probe-based verifier + 2 reverse-audit rounds) before implementation was finalized. The review surfaced real defects that were fixed in the same pass rather than deferred — see §13.5. No open design questions remained; the two judgment calls (conservative `supports_stream_usage` defaults, and `approval_needed` as the single approval source) are recorded below.

### 13.3 What we followed verbatim from ROADMAP_v2 §13

- `LoopEvent` dataclass with the six fields and no `error` variant (exceptions propagate naturally; `orchestrator.py`'s failure-path persistence depends on a real exception). ✓
- `_run()` yields terminal `LoopEvent(final_response=..., stop_reason=...)` only on an actual stop condition; the loop does NOT terminate after each iteration. ✓
- D20 persist-before-branch preserved in the generator and regression-tested against the persisted `MessageLog` (AC6). ✓
- All three public wrappers (`run_agent_conversation`, `run_deep_research_mode`, `continue_conversation`) drain via `run_to_completion()`. ✓
- `permission_channel: queue.Queue | None` parameter; headless (None) denies approval-requiring tools by default. ✓
- D21 usage-or-raise for providers marked `supports_stream_usage`. ✓

### 13.4 What we deviated from §13, and why

- **`run_to_completion()` lives in `core/loop.py`, not a separate module** — it is tightly coupled to `_run()`'s generator shape; a separate module risked a circular import for no benefit.
- **Approval is decided by `ToolRegistry.approval_needed()`, not by `requires_approval()` alone in the loop.** The review found the loop pre-checking `requires_approval()` (config boolean) while `dispatch()` uses `spec.approval_check()` (path/command-dependent) — they diverge for `read/write/edit/shell`, silently denying an out-of-workspace path without ever yielding `permission_request`. `approval_needed()` is now the single source both call. When the user approves via the channel, `_run()` passes `approval_callback=lambda n, p: True` so `dispatch()`'s internal re-check doesn't deny an already-approved tool.

### 13.5 Review-driven fixes (found by the §13 self-review, fixed in this pass)

| Finding | Fix |
|---|---|
| Approval divergence (`requires_approval` vs `approval_check`) silently denied path-dependent tools | `ToolRegistry.approval_needed()` single source; loop + dispatch both use it |
| `test_streaming_loop.py` hit the real credential loader → failed in CI / fresh clone (providers.json gitignored) | autouse `api_initialization` patch in the test module |
| GOOGLE branch lacked D21 usage-or-raise and streaming-usage opt-in | `supports_stream_usage` read once at top; D21 raise added to the Google branch |
| Tool-call fragment reassembly overwrote `function.name` (last-write-wins) | accumulate `name_parts` like `arguments` |
| `providers.json.example` marked 8 providers `supports_stream_usage: true` unverified (a wrong `true` hard-breaks a provider) | conservative defaults: only OPENAI/ANTHROPIC `true` |
| `save_credentials()` dropped the flag on write | now persists `supports_stream_usage` |
| `_run()` had no `response is None` guard after the stream loop | raise a clear `RuntimeError` (parity with `collect_response`) |
| `test_..._ToolCallDenied_caught...` was vacuous (shell denied before dispatch) | now exercises the catch as a side-effect of the `approval_needed` fix (inert `ls` reaches dispatch) |
| No dedicated `permission_channel` test | added 2 tests (approve → dispatch with callback; headless → denial) |

The two remaining Suggestion-level findings were also addressed afterwards: the copy-pasted two-turn mock factories in `test_loop_tool_dispatch.py` (and the similar ones in `test_streaming_loop.py`) were consolidated into `tests/conftest.py::make_stream_sequence()`, and `collect_response()` — kept, since it is a ROADMAP deliverable for future streaming consumers — now has real callers: `test_client_streaming.py` drains most streams through it and tests its no-final raise contract. See §13.6.

### 13.6 Test changes

- `tests/test_streaming_loop.py` (NEW): 7 tests — AC2 event ordering, AC5 exception propagation, AC6 persistence ×3, permission_channel approve/deny ×2.
- `tests/test_client_streaming.py` (NEW): 9 tests — direct `call_model_stream()` coverage for all three provider streaming paths, D21 usage-or-raise (Google + OpenAI), OpenAI tool-call name/arguments fragment accumulation, `stream_options` gating, and `collect_response()`'s no-final raise contract. Most tests drain via `collect_response()`, giving the §13 helper real callers. Uses hand-rolled fake clients passed as the `client` argument (no root-conftest stub changes).
- `tests/test_loop_stop_conditions.py`, `test_loop_tool_dispatch.py`, `test_cli.py`, `test_e2e.py`, `test_json_retry.py`: migrated `core.loop.call_model` mocks to `core.loop.call_model_stream` and wrapped `_run()` in `run_to_completion()`.
- `tests/conftest.py`: added `make_stream_from_response()` and `make_stream_sequence()` helpers.
- `tests/BREAKING_CHANGES.md` §2 updated for the generator conversion.
- Full suite: 234 tests.

## 14. File syntax + config loader + workspace trust (ROADMAP_v2 §14)

### 14.1 What was built

- `core/workspace_trust.py` — D17 gate per the ROADMAP sketch, including both Rev. 3 hash corrections (sorted `dirs` in-place for deterministic descent; relative paths fed into the digest with `\0` separators). One deliberate deviation: the trust store path resolves at CALL time (`_trust_store_path()`), not as an import-time module constant as sketched — an import-time `expanduser` made the store unredirectable in tests. Relative paths are posix-normalized so the digest is identical on Windows and POSIX.
- `core/config_loader.py` — three-tier discovery (harness `<root>/{agents,skills}/builtin/`, trust-gated project `.venastine/`, user `~/.config/venastine/`), line-anchored frontmatter parse, D18 harness-collision rejection with warning, project-wins-over-user, `settings.json` merge, `CONTEXT.md` cache + per-agent opt-in, frontmatter-only skill catalog, startup cache (`initialize`/`reset`).
- `tools/builtin/load_skill.py` — view-only body retrieval; registered with declared `ToolPermissions.load_skill = True` / `ToolApprovals.load_skill = False` (D24).
- `prompts/system_prompts.py` — `with_skill_catalog()` / `pass_prompt()`; chat loop, every research pass, and the §3 JSON-retry path all assemble through them.
- `main.py` — startup trust flow, `--trust-project` flag, parser `--provider`/`--model` defaults moved to `None` with post-parse `resolve_runtime_defaults()` (CLI > settings > config.py), ensemble knobs forwarded to the pipeline (it already accepted them as kwargs — settings.json became the first way to enable ensemble mode without editing source).
- `requirements.txt` — `pyyaml==6.0.3` (was already a transitive dependency; pinned explicitly per the ROADMAP).

### 14.2 Decisions made in the clarification cycle (user-decided)

| Question | Decision |
|---|---|
| Semantics of the model requesting a full skill | View-only: body returned as a tool result. Activation (`additional_tools`, `/skill`) deferred to §19's SkillManager. |
| Where the catalog is injected | Chat AND research passes (user chose the wider scope; pipeline prompts gain the catalog via `pass_prompt()`). |
| settings.json runtime wiring | Wire existing knobs now (provider/model/ensemble); compaction keys parse but are consumed by §21. |
| Trust prompt UX | Interactive y/N on a TTY, `--trust-project` for scripts/CI, notice-and-skip on non-TTY (no CI hang). |

### 14.3 User amendments applied during planning

1. **Announce inertness rather than allow it.** Unknown settings keys raise `ValueError` at load (a typo must not masquerade as a setting); known-but-unimplemented `compaction.*` keys validate and warn once at startup ("not implemented yet (ROADMAP_v2 §21); ignored"). No silent no-ops at any point.
2. **Project-tier settings are a different grant.** Trusting a repo also lets it choose the provider and multiply pipeline cost via `ensemble_n`. Kept project-tier settings (per D19) but made the grant informed:
3. **The trust prompt shows settings.json verbatim** (plus the file list) before asking — `describe_project_content()`. Consistent with the project's "never silently do consequential things" posture (compaction visibility, `remember()` approval).
4. **The parser-defaults gap (found by the user):** with `default=DEFAULT_PROVIDER` on the argparse arguments, "user passed --provider X" is indistinguishable from argparse fill-in, so settings.json could never win — provider/model would look wired while inert (the `fetch_url` shape, but emitting nothing). Fixed by `default=None` + post-parse resolution; `tests/test_cli.py::test_build_parser_defaults` now asserts the None defaults.

### 14.4 Test changes

- `tests/test_workspace_trust.py` (NEW, 8): AC1 (untrusted until granted), AC2 (content change revokes), added-file revocation, path-in-hash (name-swap produces a different digest), hash determinism, store written under redirected home, `content_files` sorted/relative.
- `tests/test_config_loader.py` (NEW, 18): AC4 horizontal-rule regression, malformed-file skip-with-warning, untrusted project content absent, trusted loads, project-wins-over-user, D18 harness collision warning, catalog frontmatter-only (body text absent), settings precedence (trusted/untrusted), unknown key raises, unknown compaction key raises, wrong type raises, compaction warns unimplemented, pre-init `get_settings()` empty, AC5 CONTEXT opt-in (true/false/None agent, untrusted), agent field parsing.
- `tests/test_load_skill.py` (NEW, 6): body retrieval, unknown-skill error, D24 permission declaration regression, catalog appended to chat + every pass prompt, prompt unchanged without skills / before initialize.
- `tests/test_cli.py`: parser-defaults test updated for `None`; +6 tests (resolution precedence, trust flag grants, non-TTY skip without hanging, prompt shows settings verbatim, interactive y/n).
- `tests/test_e2e.py`: research-mode pipeline assertion extended with the new `ensemble_mode=None, ensemble_n=None` kwargs.
- `tests/conftest.py`: autouse `clear_config_loader_state` fixture so one test's `initialize()` can't leak into another test's prompt assembly.
- `tests/BREAKING_CHANGES.md` §9 added for the §14 break surface.
- Full suite: 272 tests.

## 15. Permission system refactor — "stricter wins" + dynamic registration (ROADMAP_v2 §15)

### 15.1 What was built

- `tools/context.py` (NEW) — `ToolContext` (`allowed_tools` / `approval_overrides` / `subagent_depth`), data only, no project imports. `subagent_depth` is carried now and consumed by §18.
- `security/permissions.py` — `is_tool_allowed(name, context=None)` (AND across layers, global check unconditional and first), `requires_approval(name, params, context=None)` (OR across layers), `_default_for_unknown_tool()` (named default: `mcp__*` allowed, everything else unknown denied), `assert_permissions_declared()` (D24). `ToolContext` is imported under `TYPE_CHECKING` only — the real dependency direction is `tools -> security`, and a runtime import would create a cycle the moment `tools/context.py` grows an import of its own.
- `tools/registry.py` — `schemas(context)`, `approval_needed(name, params, context)`, `dispatch(name, params, context, approval_callback)`, `unregister()` (D15, idempotent), and the D24 check at import after the static registrations. The unknown-tool `ValueError` guard and the `check_output_policy()` call are both preserved.
- `tools/base.py` — `ToolSpec.available_check: Callable[[], bool]`, an optional "do I have anything to act on right now?" predicate consulted by `schemas()` only.
- `core/loop.py` — `_run()`'s `allowed_tools` parameter replaced by `context: Optional[ToolContext]`; the three public wrappers gained a `context=None` kwarg so §18 does not have to reopen their signatures.
- `config.py` — `fetch_url` added to `ToolPermissions` (True) and `ToolApprovals` (False).

### 15.2 Decisions made in the clarification cycle (user-decided)

| Question | Decision |
|---|---|
| `fetch_url` defaults (D24 asked for explicit confirmation) | Permission `True`, approval `False`. Approval `True` would have left it unusable wherever there is no `permission_channel` to answer the prompt — the CLI chat loop and every research pass — reproducing the D24 bug with a different error string. |
| Where the approval OR-composition lives | Inside `ToolRegistry.approval_needed()`, not inlined in `dispatch()` as the §15 sketch shows. |
| `_run()`'s `allowed_tools` vs `ToolContext.allowed_tools` | Replace the parameter — one representation. |
| Should `schemas()` become policy-aware | Yes, filter on `is_tool_allowed(name, context)` now. |
| `unregister()` — §15 or §17 | §15, per D15 and AC7. |
| D24 check severity and placement | Import-time `RuntimeError`, helper in `security/permissions.py`, called from `tools/registry.py`. |
| `compaction` settings merge semantics (review finding F2) | Deep-merge across tiers. |
| §14 review findings to fix alongside | F1, F3, F4, F5 (plus F6/F7 as cleanup). |

### 15.3 Deviations from the ROADMAP sketch, and why

1. **The approval OR lives in `approval_needed()`, not inlined in `dispatch()`.** §15's `dispatch()` sketch was written against pre-§13 code and never mentions `approval_needed()` — which §13 introduced specifically as the single source of truth shared by `dispatch()` *and* `_run()`'s permission bridge (ARCHITECTURE §4.8). Implemented literally, `dispatch()` would have gained stricter-wins while the loop's bridge kept the old `approval_check`-as-full-bypass behaviour, so an agent's `approval_overrides` would be honoured on one path and silently ignored on the other. This is ROADMAP_v2's own "an abbreviated restatement of existing code is a proposal to delete whatever it abbreviates" rule applying to §15 one revision after it was written down.

2. **`schemas()` filters by policy** (not in the spec at all). The `fetch_url` defect's entire damage was that the schema stayed advertised while the call was denied, so the model kept choosing it. Fixing only the instance and not the shape would leave `read`/`write`/`edit` — all permission `False` by default — doing the same thing today. Advertised tool count under default config went 15 → 10.

3. **`dispatch()` distinguishes two denial causes.** A context restriction ("not available in this context") is something the model can route around by choosing a different tool; a global denial ("disabled by policy") is not. The pre-§15 loop produced the first message from its own membership check; folding that check into `is_tool_allowed` without splitting the message would have collapsed both into one string.

4. **F5 is solved with `ToolSpec.available_check`, not a special case inside `schemas()`.** The registry owns mechanism; teaching it what a skill catalog is would put policy in it. `load_skill` supplies the predicate itself, reading the same `skill_catalog_text()` the prompt assembly reads, so "advertised" and "catalogued" cannot drift apart.

### 15.4 §14 review findings fixed in this cycle

§14 was tested but never reviewed. The implementation matched its spec closely — both Rev. 3 hash corrections present, the call-time path-resolution deviation correctly recorded in §14.1, D18's collision rule correct. No security holes or data-loss bugs. Fixed here:

- **F1** — `config_loader.initialize()` raised out of `main.py` unhandled, so a typo in `settings.json` aborted the CLI with a stack trace. New `main.load_project_config()` catches `ValueError`/`OSError` (`json.JSONDecodeError` subclasses `ValueError`, so a corrupt `settings.json` or `trusted_projects.json` lands here too) and prints one line. The raising itself stays — an unknown key must never masquerade as a setting (§14 amendment 1); only the presentation changed. **The logging level is load-bearing:** the first version used `logger.exception()`, which writes the traceback through the root logger's stderr `StreamHandler` — reintroducing the exact output the fix exists to remove, with the clean message printed underneath it. Now `logger.debug(..., exc_info=True)`, so the trace is one `AGENT_LOG_LEVEL=DEBUG` away when someone wants it.
- **F2** — `_load_merged_settings` shallow-updated, so a project `compaction` block discarded the user's sibling keys. Now deep-merged. `compaction` is the only nested key in the schema, which is exactly why this was invisible: for every scalar setting, whole-value replacement *is* per-key override.
- **F3** — `_discover` warned only on harness collisions; same-tier duplicates resolved alphabetically-first-wins in silence. Every collision now warns, with the stronger D18 wording kept for the harness case.
- **F4** — the frontmatter block is anchored to offset 0, so a file with no frontmatter but two body horizontal rules reports the right reason for being skipped instead of failing an unrelated check downstream.
- **F5** — `load_skill` was advertised with an empty catalog (see 15.3.4).
- **F6** — `get_agents()`/`get_skills()` raised `RuntimeError` pre-`initialize()` while four sibling getters returned empty, so whether calling before startup was an error depended on which getter you picked. All six return empty now; §16/§18/§19 add entry points that will not call `initialize()`.
- **F7** (doc-only) — `ARCHITECTURE.md` §6 and `README.md` still described Google as stubbed (contradicting §4.7 of the same document), and `README.md` still said `main.py` runs one hardcoded query. Both predate §9 and §1 respectively.

### 15.5 Root-cause sweep (observed, deliberately not changed)

Every other `getattr(<dataclass>, name, <default>)` site sharing the `fetch_url` root cause, checked before calling this done:

- `tools/builtin/file_ops.py:84` and `shell.py:109,145` read `config.ToolApprovals` with the same silent-default pattern inside their own `approval_check`s. These are safe **by construction now** rather than by luck: D24 guarantees every statically registered tool has both fields. They are also correct to ignore the context — `approval_needed()` ORs their answer with the context-aware lookup, so a tool's own check only has to answer "does MY policy require approval", not compose layers.
- `core/reasoning/orchestrator.py:56-57` uses `getattr(config, "MAX_PIPELINE_RETRIES", 2)` for constants that definitely exist. Same shape, benign, outside §15's scope — recorded so it is not rediscovered as a finding later.
- `core/client.py`'s `getattr(usage_obj, ..., 0)` reads are D21's named failure mode and are already gated by `supports_stream_usage` on the streaming paths (§13).

### 15.6 Test changes

- `tests/test_permission_context.py` (NEW, 21): AC1 (global deny beats a permissive context, at both the permissions and dispatch layers) plus the two-message denial distinction; AC2 (a context tightens a lenient default, direct and through `approval_needed`); AC3 (`approval_overrides: {shell: false}` does NOT suppress shell's own check, using the real `_shell_approval_check`, plus synthetic OR tests in both directions); AC4 (`mcp__*` allowed by default, non-mcp unknown still denied, mcp still subject to a context restriction); AC5 (`check_output_policy` still redacts a real secret pattern through `dispatch`); AC6 (every registered tool declared in both dataclasses, `fetch_url` callable AND advertised, the check raises, mcp exempt); AC7 (unknown name and post-`unregister` both `ValueError`); plus `schemas()` filtering and the F5 catalog case.
- `tests/test_registry_permissions.py`: monkeypatch lambdas gained `context=None`; the two `schemas(list)` tests rewritten for `ToolContext`; +3 (globally-denied omitted, `available_check` omitted, `unregister` idempotent).
- `tests/test_loop_tool_dispatch.py`: `allowed_tools` → `context`; the disallowed-tool test rewritten to run the REAL registry, because mocking `dispatch` now stubs out the very code that performs the check; +3 (global-denial message, context forwarded to both registry calls, `context=None` passthrough).
- `tests/test_streaming_loop.py`, `tests/test_loop_stop_conditions.py`, `tests/test_e2e.py`: `context` kwarg renames and `dispatch(..., context=None)` assertions.
- `tests/test_config_loader.py`: +6 (F2 deep-merge and its untrusted counterpart, F3 same-tier and cross-tier warnings, F4 anchor, F6 pre-init getters).
- `tests/test_cli.py`: +3 for F1. **The first version of the F1 test could not fail** — pytest never runs `configure_logging()`, so the root logger has no console handler, and `"Traceback" not in err` passed while the bug was live. It now attaches a real stderr `StreamHandler` for the duration of the call; reverting the fix was confirmed to make it fail rather than assumed to. This is the repo's own "verify against production code paths, not the test double" lesson landing on a test asserting the right thing against the wrong environment.
- `tests/BREAKING_CHANGES.md` §10 added for the §15 break surface.
- Full suite: 307 tests (was 272).

### 15.7 Verified beyond the suite

- D24's check is live at import, not merely unit-tested: temporarily deleting `ToolPermissions.fetch_url` makes `import tools.registry` raise `RuntimeError: ... ['fetch_url']`.
- `fetch_url` is callable and advertised — `is_tool_allowed` True, `requires_approval` False, present in `registry.schemas()`. All three were false before.
- Advertised tools under default config: 10 of 15 registered. `read`/`write`/`edit` (permission False), `shell` (permission False), and `load_skill` (empty catalog) are correctly absent.
- `python main.py` with a typo'd user `settings.json` exits 1 with one line on stderr and no traceback.
