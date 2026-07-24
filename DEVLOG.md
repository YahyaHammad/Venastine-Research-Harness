# DEVLOG — Implementation Notes for Built Roadmap Sections

**Purpose of this document:** a complete account of what was implemented for each built ROADMAP section, what was followed verbatim, what was deviated from and why, and what was discovered during testing that the original spec did not anticipate. Companion to **ARCHITECTURE.md** (which describes the production code shape) and **ROADMAP.md** (which still holds the original specifications) — read both alongside this file.

Section numbers below match ROADMAP.md's section numbers exactly. When a section is implemented, its corresponding ROADMAP.md entry gets a short "Built" subsection pointing here for full detail, and ARCHITECTURE.md gets a §4.N file-contract entry for any new file(s) created.

**Index:**
- 0. Chat-trace context — how this document's decisions were made
- 2. `logging_setup.py`
- 4. Test suite

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

None beyond the deviations above.

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
