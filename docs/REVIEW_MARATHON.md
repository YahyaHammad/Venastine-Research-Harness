# REVIEW_MARATHON.md — the pre-merge stability and security review

**Status: all 19 scopes reported; the `BLOCKS-MERGE` set is fixed (batches
130-135).** See § *Run log* at the bottom for the per-scope outcome, and
`review/TRIAGE.md` for the merged index the remaining fixes come from.

## Why this document exists

`navigable-agents` is 91 commits and 250 files ahead of `main`. The merge brings essentially the
whole harness with it, so what needs reviewing is the **tree as it stands**, not a diff against a
branch point nobody has looked at since. This document scopes that tree into 19 independent review
scopes, says what to look for in each, and fixes one reporting format so that **one** agent can
apply every fix sequentially afterwards without re-reading the code to work out what a finding
means.

Three properties the design exists to guarantee, and each one costs something that is paid here
deliberately:

1. **No scope needs another scope's output.** A reviewer can start on any scope at any time. Where a
   defect genuinely spans two scopes, both reviewers report it and name the other; § *Triage* merges
   them. A duplicate pair is information — it says two readers reached the same place independently.
2. **No reviewer can break another reviewer's work.** Reviewers are read-only on the repository.
   They may run narrowed tests and write their own report; they may not edit a tracked file, mutate
   git state, or run the full suite. § *Rules of engagement* is the complete list.
3. **The tree is frozen while the marathon is open.** Every report is measured against one commit, so
   every `file:line` in every finding resolves against the same tree. The base commit is in the run
   log.

## Where the reports go, and why they are not in this repository

Each scope's report is written to **`review/<scope-id>-<slug>.md`**, which is **gitignored**. The
reports are not committed. Two reasons, both measured rather than assumed:

- A tracked `.md` file at depth 0–3 is counted **exactly** by
  `tests/test_docs_consistency.py::test_the_documented_doc_read_bound_is_not_understated`. A reviewer
  adding a report file would turn the suite red for every other reviewer, and reviewers cannot edit
  `AGENTS.md` to fix the count.
- `read_project_doc` is registered with permission `True`, so it is advertised to **every** run —
  plain chat and all ten research passes, not only an agent that asked for it. An itemised,
  prioritised list of this harness's exploitable defects is the single worst document to put inside
  that reach while the defects are still open.

The precedent is already in the tree: `.qwen/reviews/` is gitignored, and the gate's own docstring
names it as one of the directories it deliberately does not count.

What **is** committed is this file, the triage outcome, and the fix batches themselves — the record
of what was found and what was done about it, which is the part a later reader needs.

---

## Rules of engagement

**Read `AGENTS.md` before anything else.** It is the agent-context file for this repository and it is
not optional reading: it records design decisions that are *locked* rather than defaults to
re-derive, and `CLAUDE.md` says several of them "have already been broken once by someone who
reasoned from the code alone." A finding that contradicts a locked decision is a `DECISION` finding
(see § *Disposition*), not a fix.

### A reviewer MAY

- Read any file in the repository, and any git history: `git log`, `git show`, `git grep`,
  `git diff <sha>..<sha>`.
- Read the issue history, which is where the last review pass went: `gh issue list --state all`,
  `gh issue view <n>`. 126 of those issues are severity-tagged findings against this code, and
  the four still open are listed below.
- Run **narrowed** tests:
  `python -m pytest tests/test_X.py -n0 -p no:cacheprovider -k "<expr>"`.
  `-n0` because the suite's default is `-n auto` (70% of cores) and several reviewers share this
  machine; `-p no:cacheprovider` because `.pytest_cache` is shared by every session in this
  checkout.
- Write throwaway probe scripts **outside the repository**, in its own temp directory, and run them.
  This is not tidiness — see the next section.
- Write its own report to `review/`.

### `review/` is gitignored, but it is not invisible to the suite

Measured while this document was written, and the reason two of the rules above are phrased the way
they are: `tests/test_docs_consistency.py` has a `_walk_project()` helper whose `_SKIP_DIRS` set is
`{.git, __pycache__, node_modules, .venv, venv, .pytest_cache, output, logs, build, dist,
.ipynb_checkpoints}`. **`review/` is not in it, and gitignore has nothing to do with it.** Three
gates walk the worktree rather than asking git:

- `test_no_source_file_carries_double_encoded_text` reads every `.md` it finds. A report containing
  double-encoded text — UTF-8 read as cp1252 and re-encoded, which is what a copied-and-pasted
  terminal dump can be — turns **the owner's** suite red, in a file the owner did not write.
- `_production_python()` yields every `.py` outside `tests/`, which feeds
  `test_every_decision_id_cited_in_production_code_resolves` and
  `test_every_test_name_cited_in_production_code_resolves`. **A probe script left inside the
  repository is scanned as production code**, so a probe that mentions `D14` or names a test counts
  as a citation from production and must resolve.

So: probes live outside the repository, and a report is plain UTF-8. Neither rule is about neatness;
each one is a way a read-only reviewer can still break the suite for everyone else.

### A reviewer MUST NOT

- Edit, create or delete **any tracked file**. Not a typo fix, not a comment, not a test.
- Run `git add`, `commit`, `checkout`, `restore`, `stash`, `clean`, `worktree`, `rebase`, `reset`,
  `push`, or anything else that changes git state. A `git checkout -- <path>` in this repository has
  already destroyed an uncommitted fix once.
- Run the **full suite**, or `pytest -m integration` (it spawns real subprocesses).
- Run `pip install`, `npm install`, or change any installed version.
- Regenerate `.github/bandit-baseline.json`, or add a fingerprint to `.gitleaksignore`. Adding a
  fingerprint for a real credential is never the fix; rotation is.
- Read another scope's report before finishing its own.
- Commit anything, ever — including `Screenshot 2026-09-17 092252.png`, which must never enter the
  history.

### Four environment facts, each of which has cost a cycle here before

- **A probe script must print ASCII only.** The console is cp1252; one unicode character crashes the
  script mid-loop, and whatever it was writing never gets written.
- **Line endings are mixed per file.** `AGENTS.md`, `CLAUDE.md`, `README.md`,
  `tests/BREAKING_CHANGES.md` and `docs/ARCHITECTURE.md` are bare LF; `requirements.txt` and
  `tests/test_docs_consistency.py` are CRLF; `docs/DEVLOG.md` is mixed. A script that matches on
  `\n` finds nothing in half the tree.
- **A probe that mounts the real application writes the user's real preferences** unless `HOME` is
  redirected first. Only pytest is protected; a bare script is not.
- **`AGENT_WORKSPACE=C:\workspace` is set in this shell.** Any probe that depends on the unset path
  — which is what CI has — must `unset` it in a subshell, or it measures the wrong configuration.

---

## The finding format

**Where it comes from, since none of it is invented here.** The severity namespace, the `area` enum
and the four body sections are `.github/ISSUE_TEMPLATE/audit-finding.yml` and
`docs/CONTRIBUTING.md` § *Issue format*. The report-FILE shape — a machine-readable summary
line, prefixed finding ids, an explicit coverage statement — is
`.qwen/reviews/2026-08-04-223903-local.md`, the one prior marathon-scale review in this tree. And
the structural precedent for this whole document is **issue #15, still open**: *"[tracker] Audit
Pass 1 — coverage tracker (read this first)"*, a 22-unit review whose body states the same rule
this one does — *"Findings are documented here as issues. Nothing is fixed during this pass."*
Its `[unit N]` issues are what the 19 scopes below are, and the run log at the bottom of this file
is what its checklist was.

What the template does NOT tell you is what the issues actually look like, which was measured
against all 126 severity-tagged ones rather than assumed. See § *Filing a finding as a GitHub
issue*: the body convention in practice is a bolded provenance line, the violated invariant quoted,
then descriptive `##` sections carrying driven output.

A report is markdown. It opens with **one machine-readable line**, so the triage index can be built
without parsing prose:

```
SUMMARY: S03 | s1=0 s2=2 s3=5 s4=3 | blocks-merge=1 fix-now=4 defer=4 accepted=1 decision=0 | known=2 | base=<sha>
```

Then one block per finding. Ids are globally unique by scope prefix — `S03-01`, `S03-02`, … — so a
finding can be cited across scopes and in a commit message without ambiguity.

````markdown
### [S03-02] S2 | security | BLOCKS-MERGE | demonstrated

**Where** — `security/permissions.py:142`, `is_tool_allowed()`
**Measured against** — <base sha> on navigable-agents

**What happens** — the concrete trigger. The exact call, the `file:line`, and the filesystem /
network / model state that reaches it. Never "could be a problem" and never "an attacker might".
A table of inputs to outputs, or a four-line code block, beats a paragraph.

**Why it's wrong / severity** — the invariant it breaks, named: a design-decision id (`D14 forbids
widening`), a layer boundary from `AGENTS.md` § *The layers…*, a pinned test, or a docstring
sentence quoted verbatim. Then the real harm. Then why this severity and not the one below it.

**Evidence** — one of:
  * `probe` — the script and the output it produced, with the control that proves the probe can
    still detect the thing when it IS there.
  * `test-run` — the exact pytest invocation and its result.
  * `read-only` — traced by reading, not run.

**Suggested shape (not a patch)** — the fix in words: the boundary or data shape that closes it
structurally. Name the files that would change and what a test would assert. Keep any sketch to a
few lines. This is deliberately not a diff, so the shape can be accepted without bikeshedding it.

**Not doing** — what is explicitly out of scope for this finding, so the fixing agent does not
widen it into a refactor.
````

### Severity — `S1`–`S4`

The existing audit-severity namespace, defined by the `#15` tracker and documented in
`docs/CONTRIBUTING.md` § *Issue format*. Use the **lowest** that still describes the impact.

| | |
|---|---|
| `S1` | critical — exploitable, or silently produces a wrong answer a user would act on, or loses data |
| `S2` | high — behaviour is outright wrong today on a path a normal run reaches |
| `S3` | medium — wrong on a reachable but unusual path, or a guard that does not guard what it claims |
| `S4` | low — coherence, dead policy, a claim made false by a change elsewhere |

**Always write it as "severity `S1`" in prose, never bare.** `S1`–`S4` is *also* the id range of a
design-decision family in `docs/ROADMAP_v2.md` (from the §14–§18 review). `AGENTS.md`'s namespace
table is the arbiter, and it exists because an id that resolves to two places is a cross-reference
that fails silently — you read the wrong one and it makes sense.

### Area — pick one, do not invent one

`security` · `correctness` · `coherence` · `test-quality` · `docs-drift` · `performance` · `ux` ·
`stability`

These are the eight values `.github/ISSUE_TEMPLATE/audit-finding.yml` already uses. Between them
they carry every dimension this marathon covers.

### Disposition — what `main` should do about it

| | |
|---|---|
| `BLOCKS-MERGE` | the merge should wait for this |
| `FIX-NOW` | fix in this marathon's fix pass, but it does not hold the merge |
| `DEFER` | real, recorded, not now — goes to `docs/TECHNICAL_DEBT.md` as a new item |
| `ACCEPTED` | the reviewer argues it should **not** be fixed, and gives the argument. The register already has an *Accepted risks noted in the review, deliberately not "fixed"* section for exactly this |
| `OWNER-DECISION` | see below |

Severity and disposition are **two fields because they are two questions.** "Only `S3` but I want it
fixed before main" and "`S2` and I accept it" are both sayable, and a single field cannot say either.

### `DECISION` findings — when a locked decision looks wrong

If a reviewer concludes that a *decision* is wrong rather than its implementation, that is a finding
of class `DECISION` with disposition `OWNER-DECISION`:

```
### [S03-07] DECISION | OWNER-DECISION | argued
**Decision** — D14, `docs/ROADMAP_v2.md` §…
```

These are collected into their own section of the triage index for the owner to rule on. **They never
enter the fixing agent's queue.** The reason is in `CLAUDE.md`: these decisions have already been
broken once by someone reasoning from the code alone, and a single reviewer's reading is not the
right authority to reverse one.

### `KNOWN` — already recorded, in EITHER of two places

Before reporting, check both. If the finding is already known, report it as `KNOWN` with its
identifier and stop:

```
### [S13-03] KNOWN | TECHNICAL_DEBT 48
### [S16-02] KNOWN | issue #8
```

**`docs/TECHNICAL_DEBT.md`, open items: 11, 14, 17, 18, 19, 21, 43, 44, 45, 48** (plus its
*Accepted risks* section).

> **That list is what the nineteen reviewers were told, and batch 138 made it stale on
> purpose rather than wrong.** The triage filed the marathon's own `DEFER` set as register
> items **49-84**, so the register now runs to 84 and a reader checking `KNOWN` against this
> paragraph would miss thirty-six entries. It is kept as written because it is the only
> account of the instruction the reports were produced under -- the register itself is the
> current list, and `docs/TECHNICAL_DEBT.md`'s batch-138 block names which entries came
> from here.

**And four audit findings are still OPEN as GitHub issues** — which the first version of this
document did not say, so nineteen reviewers would each have rediscovered them:

| issue | title | scope it lands in | register |
|---|---|---|---|
| **#6** | `[S3][correctness]` Two provider facts about sampling parameters are unverified | S10 | **the same finding as `TECHNICAL_DEBT 11`**, word for word |
| **#8** | `[S3][ux]` The CLI has no slash-command layer | S16 | not in the register. **Still true, measured:** `main.py` dispatches no `/command` at all, and three of its own comments cite the absence as the reason `--memories` and `--ref` are flags |
| **#9** | `[S3][coherence]` Four `with_*` prompt tiers repeat one pattern with four signatures (J11) | S10 | same J11 family as `TECHNICAL_DEBT 19`, which is about `with_goal` being uncounted rather than about the four signatures |
| **#12** | `[S3][coherence]` Ensemble disagreement is scored rather than routed to verification | S12 | not in the register; see §10's revisit (E1—E14) |

All four are `S3` and labelled `enhancement`, and that is not a coincidence — it is exactly the
class this marathon will file and not fix. See § *Filing a finding as a GitHub issue*.

Nineteen reviewers independently rediscovering item 48 is nineteen findings of no value, and a
report that says "`KNOWN`, item 48, and here is the one thing the entry gets wrong" is worth more
than either.

One warning about the register, learned twice: **an entry is evidence, not instruction.** Batches
126 and 127 each found the entry itself wrong about where its own defect came from. Measure the
claim before building on it.

### Filing a finding as a GitHub issue

**Nothing is filed until after the merge, and then only the tail.** Owner decision, and it follows
from the disposition field: anything `BLOCKS-MERGE` or `FIX-NOW` is fixed before `main` sees it, so
filing it would open and close an issue in the same week. What gets filed is the part that outlives
the marathon — `DEFER`, and `ACCEPTED` where the argument is worth a public record. **The
disposition decides the destination:**

| disposition | destination |
|---|---|
| `BLOCKS-MERGE` | fixed in the fix pass, before the merge. Not filed |
| `FIX-NOW` | fixed in the fix pass. Not filed |
| `DEFER` | a new `docs/TECHNICAL_DEBT.md` item now, and an issue after the merge |
| `ACCEPTED` | an *Accepted risks* entry with its argument; an issue only if a later decision is wanted |
| `OWNER-DECISION` | the owner. Never filed by an agent |

The four findings still open from Pass 1 — #6, #8, #9, #12 — are all `S3` and all
`enhancement`. That is the shape of what gets filed.

**The mapping, field by field.** A report finding converts mechanically; it does not need rewriting:

| in the report | in the issue |
|---|---|
| severity + area | the title prefix: `[S2][security]` |
| the one-line claim | the rest of the title, as a concise *imperative-mood* summary |
| **Where** | the bolded lead line, in `file.py:142` form. `docs/CONTRIBUTING.md` calls that convention "exact and expected in an issue body", and the corpus uses it everywhere — `safety/policy_enforcement.py:198-212`, `sandbox.py:134`. The live idiom also names the unit: **"Unit 5 (dispatch, permissions, safety)."** — write the scope id and name there instead |
| **What happens** | `## What happens`, or — as every issue in the corpus actually does it — a section with a *descriptive* name and the driven output pasted in a fence: `## Driven, through the real check_output_policy`, `## Hole 1 ` + the hole, `## The three routes, measured` |
| **Evidence** | folded into that same section. The house idiom for a probe is "driven through the **real** `registry.dispatch` with nothing monkeypatched", and an inputs-to-outputs table is the house form for the result |
| **Why it's wrong / severity** | `## Why it's wrong / Severity`, opening with the invariant quoted verbatim from the docstring, `AGENTS.md` or the decision record it violates |
| **Suggested shape (not a patch)** | `## Suggested shape (not a patch)` |
| **Not doing** | `## Not doing` |
| **Measured against** | the footer: `Measured against <sha> on navigable-agents.` |
| **Disposition**, **Confidence** | marathon-local. Not filed |

**Two values in the corpus are historical, and dated:** `[S0]` (issues #13, #18, #36, #52, all
2026-08-09/10) and `[S3][complexity]` (#23, #38, #39, #56, #65, #84, all 2026-08-09/12). The issue
template that defines the dropdowns was added **2026-08-27**, after every one of them, and it
enumerates `S1`—`S4` and the eight areas only. So the template is the arbiter: use `S1` where
the old corpus would have said `S0`, and the nearest live area — usually `coherence` — where it
would have said `complexity`. Knowing they exist is for reading the old issues, not for writing new
ones.

**The label, from the corpus rather than from the rule.** `docs/CONTRIBUTING.md` says `enhancement`
is for "`S3`/`S4` polish" and that "most open findings sit here". Measured across the 126 tagged
issues, that is not what happened: `S3` + `bug` is **51** of them, the single largest combination,
against `S3` + `enhancement` at 9 and `S3` + `documentation` at 16; four carry no label at all. So
label on whether behaviour is **wrong today** (`bug`), whether it is drift between code and docs
(`documentation`), or whether nothing misbehaves yet (`enhancement`) — and treat the severity
as independent of it. The divergence between that rule and the corpus is itself a finding, and it
belongs to S18.

### Two closing sections, both required

A report without these is incomplete and goes back:

- **Checked and found sound** — what was examined and is correct, named specifically. This is the
  only thing that distinguishes a quiet scope from an unreviewed one, and it is what stops the next
  marathon re-sweeping the same ground. The review doctrine in `agents/builtin/review.md` is
  explicit about this: *"If the work is sound, say so plainly and stop. Manufacturing findings to
  justify the review is worse than a short review, because it costs the reader the ability to tell
  your serious findings from your filler."*
- **Not checked** — what the reviewer did not reach, and why. An honest coverage statement is worth
  more than a padded finding list.

---

## The defect shapes this codebase has already produced

`docs/ARCHITECTURE.md` § 11 is 45 entries of real bugs found in this repository, each with the
general shape it belongs to. Hunting a shape that has already occurred here finds more than hunting
generically. These nine are the recurring ones; read § 11 itself for the cases.

1. **A value derived in two places.** The bug is not that they drift — it is that nothing can tell
   you they have. Four lists once answered "what counts as a project file" and no two agreed.
2. **Wired, documented, and never executed end to end** on the shipped configuration. Three recorded
   instances: `fetch_url` registered and denied for its entire life; ensemble mode built against a
   model that rejects its mechanism; `settings.json` made inert by argparse defaults. "Built" and
   "runs" are different claims.
3. **Containment is a property of a layer, not of a function.** `dispatch()` turning a handler's
   exception into an error dict is why a broken tool cannot kill a run — and the layer above it
   parsed tool arguments with a bare `json.loads`. When you find one layer that has containment, ask
   what the layer above does with the same class of failure.
4. **A denylist or defensive default that admits an unknown entry.** `PIPELINE_UNGRANTABLE` held two
   names, a third tool was registered later, and the entire grant menu became the one built-in that
   overwrites files in your repository. Nobody edited the policy — which is why nothing surfaced it.
   A defensive default is only safe where absence is impossible.
5. **An allowlist widened without asking which axis it is indexed on.** Adding five build manifests
   to `read_project_doc` sounded like "can a pass read arbitrary Python" (no) — the question nobody
   asked was that a basename matches **at any depth**. Drive the matcher; do not reason about the
   list.
6. **A handler present for the unreachable failure and absent for the reachable one.** `/init`'s
   write loop caught a denial that cannot fire while disk-full took a path that did not exist. Worth
   asking of any `except` clause: what actually raises this, and what raises the thing next to it?
7. **The four vacuity shapes.** A green test that is evidence of nothing:
   - input that cannot **discriminate** (three filenames against a `sorted()` call on NTFS);
   - input that cannot **arrive** (a usage chunk has `choices: []`, so the line under test is never
     reached);
   - a test that **supplies the model's move**, and so has authority over the model's options —
     any assertion about what the model *could* do is then circular;
   - a set made **smaller or empty**, which turns every "x not in set" assertion about it into a
     tautology.
   What catches these is mutating the line and asking which test should have gone red.
8. **Advertised-and-uncallable, and its inverse.** A prompt told the model to call a tool the same
   run had just hidden from it, and logged both facts. The mirror image: `--grant-tools` printed that
   it had authorised something and sent the model byte-identical tools, for three sections.
9. **A claim in a comment or a document made false by a change somewhere else.** This marathon's own
   seed finding is one: see S19.

---

## The 19 scopes

Every tracked production module is assigned to exactly one scope. Verified mechanically against
`git ls-files '*.py'`: 114 production modules, 107 assigned by name, and the remaining seven are
zero-byte `__init__.py` files — `core/`, `core/reasoning/`, `prompts/`, `safety/`, `security/`,
`tools/builtin/` and the package marker in `mcp_client/` (which is 15 lines and belongs to S13).
No module is in two scopes.

The **test files** listed per scope are a starting point, not a boundary. `git grep -l <module>
tests/` is how to find the rest, and `docs/ARCHITECTURE.md` § 3's tree states a gated test count for
every test file.

### S01 — the shell gate and command execution

**Files** `tools/builtin/shell.py` · `tools/builtin/shell_sessions.py` · `core/shell_sessions.py` ·
`security/capability.py` · `security/askpass.py` · `tools/isolation.py`

**Focus**
- The tiered approval decision: G1–G7 (§28), as amended by §46 EP4–EP8 (routing and disclosure),
  §48 CE1–CE7 (what `tiered` approves) and ROADMAP_v3 §49 SS65–SS72 (root commands). The question
  for each tier is not "is the rule right" but *what reaches the host without a human in the loop*.
- Argument and flag parsing against the dangerous-flag guard. `TECHNICAL_DEBT 34` records that
  `_DANGEROUS_FLAGS` "guarded nothing it claimed to"; it closed in batch 117. Check the fix holds for
  the shapes it did not enumerate.
- Sudo line construction. `TECHNICAL_DEBT 37` was a stored secret containing the newline that ends
  the sudo line. Closed in 117 — ask what else crosses that boundary unescaped.
- Session lifecycle: orphaned children, PID reuse, what a kill leaves running, and whether a session
  can outlive the thread that owns it.
- `tools/isolation.py` is the wall clock that can actually stop the work (H2/H3). It is shared with
  S06 — name the seam rather than both of you owning it.

**Tests** `test_shell` · `test_sudo` · `test_grants` · `test_shell_sessions` ·
`test_interactive_sessions` · `test_session_backends` · `test_session_tools` · `test_tool_budgets` ·
`test_askpass` · `test_pid_scanner`

### S02 — the sandbox and its execution backends

**Files** `security/sandbox.py` · `security/posture.py`

**Focus**
- Backend selection across container, WSL and SSH, and the runtime probe's cache.
  `TECHNICAL_DEBT 27` was a probe leaking across a whole session; closed in 107.
- What enforces the limits **where `RLIMIT_*` does not exist**. `tools/isolation.py`'s docstring
  states the asymmetry plainly — "on Windows there is no rlimit and no cheap equivalent, so the
  clock runs alone" — and H3 records it. The question is whether the clock alone actually reclaims
  the work, and what a child that ignores its termination does.
- Path translation between Windows, WSL and a remote host. A path that round-trips wrong is a
  boundary that stopped being a boundary.
- A runtime that disappears **mid-session**, not at startup.
- The ceilings `README.md` states for sandbox time, memory, pids and CPU seconds are gated against
  live config by `test_every_documented_ceiling_matches_the_live_config` — so the open question is
  whether the live value is *enforced*, not whether it is documented.

**Tests** `test_posture` · `test_wsl_backend` · `test_ssh_backend` · `test_declared_network` ·
`test_probe_isolation` · `test_session_backends`

### S03 — permissions, the registry, and tool dispatch

**Files** `security/permissions.py` · `tools/registry.py` · `tools/base.py` · `tools/context.py` ·
`security/protected_paths.py`

**Focus**
- The **order** of the global permission check, the approval callback, a pre-flight grant, and a
  refusal check. `project_docs.py`'s docstring states the property the order creates:
  `dispatch("write", …)` raises before the approval callback is ever consulted. Prove the order,
  do not read it. D14 forbids a `ToolContext` widening anything.
- `schemas()` decides advertisement from context **and** `callable_only` **and** `granted`, while
  `is_allowed` answers policy. Gotchas #68 and #156 are the two directions this seam fails in.
- `PIPELINE_UNGRANTABLE` and `grant_policy` as denylists — shape 4 above. D24's remedy was declare
  per tool, raise at import on omission *and on a misspelled value*.
- Runtime `register`/`unregister` (D15, for MCP): a stale tool name is a reachable state, by the
  registry's own comment.
- Budget accounting, and the response cache `TECHNICAL_DEBT 35` added in batch 117.

**Tests** `test_registry_permissions` · `test_permission_context` · `test_grants` ·
`test_catalog_advertisement` · `test_tool_budgets` · `test_tool_failure_containment` ·
`test_tool_arguments` · `test_spawn_preflight`

### S04 — the file and documentation tools

**Files** `tools/builtin/`: `file_ops.py` · `project_docs.py` · `pin.py` · `remember.py` ·
`todo.py` · `ask_user.py` · `load_skill.py` · `get_time.py`

**Focus**
- Path resolution: traversal, symlinks, junctions, UNC paths, case folding, and a path that resolves
  inside the project only because of where the process happens to be.
- `_DENIED_SEGMENTS` is `security.protected_paths.PROTECTED_SEGMENTS` unioned with six more, and that
  one fact has three consumers. Check all three agree, by driving them.
- **The matcher's axis, not its entries** (shape 5). Ten entries match on basename at any depth; five
  match at the project root only.
- `max_read_chars` and what a truncated read reports about having been truncated.
- `write_project_doc` derives its path from the document's *name*, never from a path parameter (I1).
- ~~The residual `project_docs.py`'s own docstring names and files against the redactor rather than
  fixing locally: a plaintext `<password>` element, a Gradle `password "…"` and a `user:token@host`
  URL are **not** redacted out of a tool result~~ — **ANSWERED, and the answer was that the claim was
  false** (S04-05, fixed batch 146). All three shapes are redacted: #167 closed on 2026-08-23 and
  `safety/policy_enforcement.py` has carried `_URL_USERINFO_RE`, `_XML_PASSWORD_RE` and
  `_ASSIGNMENT_RE` ever since. The docstring was never updated, so a security residual read as OPEN
  for seven weeks and propagated here as a scope brief and into S04's launch prompt as a question —
  which is what spent a probe re-measuring a leak that did not exist. The instruction that found it
  ("verify it is still true, and that it is still filed") is left standing because it is the
  instruction that worked. `docs/TECHNICAL_DEBT.md` 91 carries what is NOT built: nothing keeps an
  issue number cited in a docstring honest, the way the decision-id gate keeps a decision id honest.

**Tests** `test_file_ops` · `test_project_path_guard` · `test_pin_tool` · `test_remember_tool` ·
`test_todo` · `test_question_tool` · `test_load_skill`

### S05 — network tools and content policy

**Files** `tools/builtin/`: `fetch_url.py` · `web_search.py` · `arxiv.py` · `_net_common.py` ·
`safety/policy_enforcement.py`

**Focus**
- SSRF against the URL blocklist, which `test_the_url_blocklist_is_defined_exactly_once` pins to a
  single definition. The blocklist being singular is not the same as it being sufficient: redirects,
  DNS rebinding, IPv6 forms, decimal and octal literals, `0.0.0.0`, link-local and cloud metadata
  addresses, and a host that resolves differently on the second lookup.
- `defusedxml` in the feed parser. `TECHNICAL_DEBT 33` was `_parse_atom_feed` trusting the structure
  of a feed its own comment calls hostile; closed in 117.
- **Producer-side size bounds.** `MAX_SUMMARY_CHARS` and `MAX_SNIPPET_CHARS` exist because
  model-facing text from a source you did not write must be bounded where it is produced.
  `TECHNICAL_DEBT 41` was one arxiv result at 29k tokens; closed in 126.
- `check_output_policy` scans every value in a tool result (#47). `TECHNICAL_DEBT 46` concluded
  breadth is the wrong axis — so the live question is which credential shapes the patterns miss, and
  that question belongs here rather than in S04 where it surfaces.
- The untrusted-content envelope: what `prompts/untrusted_content` promises and whether the wrapping
  can be escaped by the content.

**Tests** `test_fetch_url` · `test_net_common` · `test_policy_enforcement` ·
`test_untrusted_content` · `test_redact_before_cut` · `test_declared_network`

### S06 — the math and compute tools

**Files** `tools/builtin/_math_common.py` · `symbolic_math.py` · `linear_algebra.py` ·
`probability_stats.py` · `geometry.py` · `discrete_math.py` · `logic.py`

The smallest scope and the highest consequence per line: this is the one place in the harness where a
model-supplied *string* is parsed and evaluated.

**Focus**
- `safe_parse`'s AST allowlist. `_ALLOWED_NODES` and `_ALLOWED_NAMES` are both gated for *size* by
  `test_agents_md_states_the_real_sympy_allowlist_sizes` — the gate says nothing about whether the
  membership is right. Ask whether any permitted node reaches an attribute, a call to something that
  evaluates its argument, a dunder, a comprehension scope, or a format expression.
- `_SAFE_GLOBALS` is built from `dir(sympy)` at import time, and batch 128 measured that importing
  `probability_stats` first therefore puts a live **module object** (`sympy.stats`) into it, against
  the module's own stated rule. It was measured inert four ways: `stats` is not in `_ALLOWED_NAMES`,
  the bare word splits into `a*s**2*t**2` rather than resolving, and attribute access is refused.
  **Re-derive it rather than trusting this paragraph** — the name gate is the only thing making the
  globals dict's contents not matter, so the finding is about what happens if that gate ever moves.
- The compute bound is a **killable subprocess**, not a thread and not a signal:
  `tools/registry.py` routes every `BUDGET_COMPUTE` handler through `isolation.run_isolated`, which
  sets `RLIMIT_CPU` inside the child on POSIX and relies on the outer wall clock alone on Windows.
  The thread design was rejected because "a watchdog that times out returns an error while the core
  stays busy." Ask what happens to a child that does not die, and what the parent reports meanwhile.
- Expression-size exhaustion *before* the parse: input length, nesting depth, and an expression that
  is cheap to parse and catastrophic to evaluate.

**Tests** `test_math_tools` — the only one. `docs/ARCHITECTURE.md`'s gated tree says **131 tests**;
`tools/registry.py`'s comment, arguing for isolating compute tools at dispatch rather than inside
each tool, says "the 123 tests in test_math_tools.py". One of those is stale. Which one, and whether
the number is load-bearing for the argument the comment makes, is a finding in this scope — a
worked instance of shape 9.

### S07 — secrets, credentials, and logging

**Files** `security/secrets.py` · `credentials.py` · `env_secrets.py` · `logging_setup.py`

**Focus**
- At-rest encryption and key derivation for the harness-held secret store (ROADMAP_v3 §49 slice 5a,
  SS56). The `.gitignore` entry for `secrets.json` is belt and braces; the encryption is the actual
  protection, so it is what needs reading.
- The four `os.chmod` calls in the tree. On Windows a POSIX mode is approximately a no-op, so a
  permission that is the protection on one platform is decoration on another.
- **Whether a secret reaches somewhere it was never meant to be:** a log line, a traceback, an
  exception message, a tool result, the TUI, a crash report, or the npm tarball.
- `json_store`'s atomic write and versioned read. Batch 108 moved the two credential stores
  (`providers.json`, `secrets.json`) off in-place `O_TRUNC` writes — verify both actually take that
  path now, and what a crash between temp-write and rename leaves.
- `logging_setup.py` is here, rather than with the build tooling, because the load-bearing question
  about it is whether a credential ends up in a log file.

**Tests** `test_secrets` · `test_credentials` · `test_logging_setup` · `test_askpass` ·
`test_redact_before_cut`

### S08 — config, schema, and workspace trust

**Files** `config_schema.py` · `config.py` · `core/config_loader.py` · `core/workspace_trust.py` ·
`config_edit.py` · `config_update.py`

This is where **files a project ships** are parsed. Everything here reads input the user of this
harness did not write.

**Focus**
- Agent and skill frontmatter validation. Two recorded failures to measure against: gotcha #131, a
  YAML block scalar in a `description` that *leaves its bullet* and forges a top-level
  `## Available tools` section into the system prompt of every run in that project — reached by one
  `y` at a trust prompt that showed 199 characters, none of them the payload; and a non-string
  element in `additional_tools` that parses clean because only `isinstance(tools, list)` is checked,
  then crashes `/skill` with an `AttributeError` that Textual's `_handle_exception` turns into the
  whole TUI exiting.
- `config.MAX_CATALOG_TEXT_CHARS`, and whether the **normalised** value is what the consent screen
  displays. Showing something other than what gets injected invites trust in a string that is not
  the one used.
- The D17/WS9 trust content hash: which files are in it, what a grant covers, and what changing a
  covered file does on the next launch. The root `AGENTS.md` is inside it.
- `HARNESS_AUTHORITY_KEYS`, and the deliberate absence of env overrides for harness-tier values —
  that absence is a security property, per `CONFIG_ARCHITECTURE.md`, so the question is whether
  anything reintroduces one.
- `config_update.py`'s three-way merge, which runs before `main.py` on an npm update and is the only
  thing standing between an upgrade and the user's settings.
- The loader cache's leak between tests, which `test_config_edit.py` has an ordered class for.

**Tests** `test_config_loader` · `test_workspace_trust` · `test_config_edit` · `test_config_update` ·
`test_mcp_config` · `test_shipped_roster` · `test_catalog_text`

### S09 — persistence and memory

**Files** `storage.py` · `database.py` · `json_store.py` · `core/memory.py` ·
`memories/manager.py` · `memories/tui_commands.py` · `core/reasoning/pipeline_storage.py`

**Focus**
- `ensure_columns()` is **additive-only and never backfills** (gotcha #45). Every migration question
  follows from that: a column added today is `NULL` for every row written before it, and a
  classification that must cover old rows cannot live in `ensure_columns()`.
- SQLModel tables register on `SQLModel.metadata` at **import** time, and `create_db_and_tables()`
  creates only what is registered when it runs. A fresh database once had no `pipelinerunrecord` at
  all while every developer's long-lived `app.db` did — and `*.db` is gitignored, so a clone got
  nothing. Those explicit side-effect imports in `main.py` are load-bearing; check nothing has
  "tidied" one away and that the guard still raises.
- Concurrent writers, and the `-wal`/`-shm` sidecars that §47 slice 8 made permanent fixtures of a
  running harness (NA18).
- Unbounded row growth, and what a kill mid-write leaves.

**Tests** `test_storage_e2e` · `test_storage_reads` · `test_schema_migration` · `test_memories` ·
`test_memory_write_through` · `test_memory_injection` · `test_memory_compaction` ·
`test_pipeline_storage` · `test_fake_storage_mirror` · `test_embeddings`

### S10 — the turn loop, the provider client, and cancellation

**Files** `core/`: `loop.py` · `client.py` · `cancel.py` · `provider_errors.py` ·
`model_windows.py` · `approval.py` · `events.py`

**Focus**
- **Containment, asked of every layer** (shape 3). The recorded case is in this scope: the
  accumulated tool-call `arguments` string parsed with a bare `json.loads` at a call site inside no
  `try`, so a response cut mid-arguments by the token limit took a `JSONDecodeError` out of
  `call_model_stream`, out of `_run()`, and flipped a nine-pass run to `status='failed'`. That is one
  instance; the scope is every other place the loop trusts the shape of what came back.
  **ANSWERED, and it found a second instance of the same shape** (S10-03, fixed in batch 147):
  `registry.dispatch` raises on purpose for two things and `_dispatch_one` caught one of them,
  and the one it let through was the one a MODEL can cause. Asking "what does the layer above do
  with the same class of failure" found it; asking the question of the FIX found defect shape 9
  inside it, because `_dispatch_parallel`'s containment comment named a case
  `registry.parallel()` guarantees it can never see.
- Retry once output is already on screen. `TECHNICAL_DEBT 25` closed in 113.
- A cancelled turn and the rows it still owes — the most recent commit before this marathon is
  literally "A turn you can stop, and the rows a stopped turn still owes."
- Window accounting, `default_context_window`, and the fallback a model with no known window takes.
- Provider wire formats existing in **exactly two functions**, which is a stated contract and has
  been broken by a helper before (gotcha #39, a "provider-neutral" function that was OpenAI-shaped).
- Partial, empty and out-of-order stream chunks.

**Tests** `test_streaming_loop` · `test_loop_tool_dispatch` · `test_loop_stop_conditions` ·
`test_cancel` · `test_model_call_retry` · `test_provider_errors` · `test_client_translation` ·
`test_client_streaming` · `test_client_effort` · `test_context_window` · `test_parallel_calls` ·
`test_sdk_conformance` · `test_spend_and_size`

### S11 — compaction, sessions, and replay

**Files** `core/`: `compaction.py` · `session.py` · `session_view.py` · `session_wake.py` ·
`replay.py` · `agent_activity.py` · `pipeline_models.py`

**Focus**
- `effective_compaction()`'s provenance and the small-window raise. `TECHNICAL_DEBT 42` closed in
  127 — **and that entry's own diagnosis was wrong in two details**, which the batch recorded. Measure.
  **MEASURED, and the fix for that raise produced the outcome the entry rejected** (S11-03, batch
  148): the floor is applied with no reference to the window, so at or below 8,000 the trigger it
  returns is ABOVE the window and compaction can never fire -- silently, while the only line the
  user sees says a threshold is approaching, because `warn_at` collapses to 1 down there. Item
  42's own fix section rejected exactly that ("compaction would then silently never run on that
  model, which is the quiet-failure class this project keeps closing") for the containment it
  declined. A24 is the loud half, and it is narrower than the report asked for: it fires only
  where the FLOOR bound the trigger, because above that the headroom cap did and the two settings
  the line names are not the remedy (item 96).
- Redaction ordering relative to the cut (`test_redact_before_cut` names the invariant).
  **ANSWERED, and the answer is four cut sites** (S11-05, fixed in batch 148 as §65's A23).
  A session's output is cut by the producer at `max_read_chars`, by the wake's per-session
  division, by the view's display budget and by `shell_output`'s paging, and the redactor ran
  after every one of them. The three credential SHAPES leak at each tail cut; the PAGING route
  leaks all seven, including the fixed-length vendor tokens, because the model asks for the
  adjacent page itself and holds both halves. Two of those sites this scope assigned to S01 and
  S16, and S01 never measured them, which is why the severe half was still open. The honest limit
  is register item 95: a stream has no whole document, and the buffer's dropped middle is a cut
  nothing above it can see across.
- Pinning, and what a pinned message does to the arithmetic.
  **ANSWERED TWICE** (batch 148). To the FOLD: when every row newly swept by the watermark is
  pinned the segment is empty, and the `all-pinned` branch tested the configured STRATEGY rather
  than the segment -- so the shipped `chain` fell through it and the compactor was handed its own
  previous summary to shorten, 2,489 characters back as 23, reported as a successful fold of zero
  messages (S11-02). To the CAP: #89's cap is a fraction of the trigger, and `pin_measurements`
  read the configured trigger while batch 44 moved the firing one onto the model -- so the cap was
  250% of the trigger at an 8,192-token window, where one pin can floor a thread permanently
  above it, and 8% at 262,144 (S11-06, ruled as A22).
- **Whether a cut can orphan a tool result from its call** and hand the next request a transcript the
  provider will reject — the failure that looks like a provider outage.
- Turn folding and the turn-count bound: `TECHNICAL_DEBT 39` (a turn count holding a thread of any
  size with no way out) and 40 (one turn being the whole thread) closed in 119 and 120.

**Tests** `test_compaction` · `test_loop_compaction` · `test_memory_compaction` ·
`test_shell_compaction` · `test_redact_before_cut` · `test_session_wake` · `test_session_overrides` ·
`test_session_display` · `test_agent_activity` · `test_thread_legibility` · `test_pipeline_models`

### S12 — the research pipeline

**Files** `core/reasoning/`: `base.py` · `orchestrator.py` · `confidence_scoring.py` ·
`review.py` · `json_retry.py` · `payload_validation.py` · `authorization.py` · `events.py` ·
`output_writer.py` · `scholar.py` · `source_corpus.py` · `source_scoring.py`

A prior review concentrated ten high-confidence findings in this scope's consent and correction state
machine. Most were fixed; the shapes are the brief.

**Focus**
- **Stage containment:** an *optional* stage's transient provider error or unrecoverable JSON
  propagating into the pipeline's `except` and flipping a completed ten-pass run to durable
  `status='failed'`, exit 1, no artifacts.
- **Mutate-then-commit ordering:** a stage that mutates claims *before* re-synthesis, so a failed
  re-synthesis persists corrected claims beside a stale report and an "accepted and applied" trace.
  Copy-then-commit is the shape; check which stages have it.
- An agent-context restriction that binds on **turn 1 only**, so retry and refinement continuations
  run with no context and re-advertise the tools the agent definition excluded. Authority compounding
  across continuation turns is the class.
- Validation of model-shaped payloads: unhashable values where a dict key is expected, non-string
  where a string is assumed, and a field whose absence is indistinguishable from its emptiness.
  **ANSWERED, and the unhashable value was not where this bullet looked for it** (S12-01, fixed
  in batch 149 as §66's A25). A dict KEY was already guarded -- `_check_ids` runs after the type
  guards, which is `review.py`'s rule and was copied deliberately. The unhashable values that
  reached pure code were inside a declared CONTAINER: `entities` typed as an array still
  accepted `[["madrid"]]` and `[{"name": "madrid"}]`, and Pass 3a builds
  `sorted({e for c in factual_claims for e in c.entities})` -- so the hash raised in a stage
  that makes no model call. Six crash shapes and three silent wrong numbers, each driven through
  the real validator and the real consumer; the three silent ones are all a STRING being
  iterable, which is also the third of this bullet's own shapes, one level down.
- Artifact durability versus what the documents claim about it — including a summary line that
  asserts where the full record is while the write that would have put it there was swallowed.
- Gates `D0`, `D1` and `D2` are **pure code** routing between passes, no model call. Write them as
  `gate D0`, never bare: `D1` and `D2` are also design decisions.

**Tests** `test_orchestrator` · `test_review` · `test_json_retry` · `test_payload_validation` ·
`test_research_authorization` · `test_research_legibility` · `test_pipeline_events` ·
`test_confidence_scoring` · `test_source_scoring` · `test_source_corpus` · `test_scholar` ·
`test_output_writer` · `test_truncated_pass` · `test_ensemble_guard` · `test_critic_routing` ·
`test_granted_calls_artifact` · `test_rationale`

### S13 — agents, skills, and MCP

**Files** `agents/manager.py` · `agents/subagent_tool.py` · `agents/tui_commands.py` ·
`skills/manager.py` · `skills/tui_commands.py` · `mcp_client/client.py` · `mcp_client/config.py` ·
`mcp_client/registration.py` · `mcp_client/__init__.py`

**Focus**
- **`TECHNICAL_DEBT 48` is open: an MCP result has no size bound of any kind.** Start there. An MCP
  server is third-party code that can return anything at all, and §44's producer-bound rule exists
  for exactly this.
- `allowed_tools` narrows an agent; it does not stop the global check, which runs first and
  unconditionally. Prove which one refuses, for a tool in neither list and a tool in both.
- Spawn preflight, recursion depth, and whether a child inherits or resets a budget.
- The async bridge's subprocess lifecycle and orphan prevention (§17 AC8). This is pinned by the
  suite's **one** `integration` test, which reviewers may not run — so reason about it, read
  `mcp_client/client.py`'s own docstring about the inner bound and the outer wall clock, and report
  the exact command the fixer should run.
  **ANSWERED, and the instruction worked exactly as intended (S13-08, fixed batch 150).** The
  reviewer reported the command; the fixer ran it (1 passed in 9.66s) and then added the two cases
  the one test could not be. The reasoning was right about the gap and could not settle the
  question: the orphan-prevention path was pinned only over the in-process transport, which cannot
  observe an orphan, and both paths turned out to be CLEAN with real children. This is the clearest
  case the marathon has produced for a reviewer naming a command instead of guessing an outcome
  — and for a `read-only` confidence tag meaning what it says.
- Tools registered at runtime from a config a project ships: `registration.py` imports `permissions`
  and `tools.base` but **not** `tools.builtin` — a near-miss `tools/registry.py`'s uniqueness claim
  survives, and which a grep reports as a contradiction. It is not one.

**Tests** `test_agents` · `test_agent_navigation` · `test_spawnable` · `test_spawn_preflight` ·
`test_session_subagent` · `test_skills` · `test_load_skill` · `test_mcp_client` · `test_mcp_config` ·
`test_shipped_roster` · (`test_mcp_integration` — **do not run**, `integration` marker)

### S14 — TUI lifecycle and consent

**Files** `tui/app.py` · `tui/screens.py` · `tui/commands.py` · `tui/preferences.py`

**Focus**
- **`TECHNICAL_DEBT 5` (TUI shutdown/consent lifecycle) is open.** It is the brief for this scope.
- Quitting while an ask is outstanding. The recorded failure: a one-shot permission-channel release
  against a queue that is re-armed per ask, so the successor ask parks unboundedly in Textual's
  `call_from_thread` → `future.result()` on a **non-daemon** worker, and the process hangs rather
  than exits.
- **Any unhandled exception reaching Textual's `_handle_exception` exits the whole application.**
  That makes every path that can raise while a modal is up a crash path, and it is why a parsing
  defect in S08 lands in this scope as a TUI crash.
- What the modal approves versus what `dispatch()` re-checks afterwards. Clearing an approval flag is
  not the same as granting the call (gotcha #70) — and the resulting denial reported a cause that was
  not the cause.
- Blocking work on the UI thread, and a second reader on one stdin (shared with S16 — name the seam).
- The modal sweep is 3 sizes × 19 cases, gated by
  `test_agents_md_states_the_real_modal_parametrisation`. That is the shape of the current coverage,
  not a guarantee of its depth.

**Tests** `test_tui` · `test_tui_edge_cases` · `test_session_tui` · `test_attended` ·
`test_pilot_wait` · `test_diff_view` · `test_paging` · `test_themes`

### S15 — TUI rendering

**Files** `tui/widgets.py` · `tui/markdown.py` · `tui/themes.py` · `tui/meters.py` ·
`tui/diffs.py` · `tui/ravens.py` · `tui/history.py`

Everything here renders text that nobody on this machine wrote: model output, tool results, fetched
pages, file contents, an MCP server's reply.

**Focus**
- ANSI and control characters in rendered text, and whether the sanitisation `core/ansi.py` provides
  (S16) is actually on every path into the transcript.
- The curated markdown grammar: `tui/markdown.py` owns the grammar and `tui/widgets.py` owns the
  painting. What escapes the grammar, and what a malformed construct does to the rest of the frame.
- Unbounded render: row count, a single enormous line, a table with 10,000 columns, a code fence that
  never closes.
- Reflow on resize. `TECHNICAL_DEBT` 15, 23 and 24 all closed in batch 112 — and 15 and 23 are the
  *same* defect filed twice, which is itself the lesson. Verify rather than assume.
- The grammar's deliberate absences — block quotes and fence-aware indent-verbatim —
  `TECHNICAL_DEBT 14` and `17`, both **open**. Report as `KNOWN`.

**Tests** `test_markdown_render` · `test_live_output` · `test_meters` · `test_themes` ·
`test_harness_rows` · `test_history` · `test_diff_view` · `test_paging`

### S16 — the headless shell and shared view code

**Files** `main.py` · `core/interaction.py` · `core/call_view.py` · `core/ansi.py` ·
`core/line_pattern.py`

**Focus**
- **CLI/TUI parity.** `core/events.py` explicitly permits a surface that ignores `retract` entirely
  and is "still correct about failures", and `main.py` is that surface. The question is what *else*
  the headless path silently ignores, where silence is not a documented decision.
- The argparse-defaults shape: §14 once shipped a settings file that was inert because argparse
  supplied a default for every flag it could have overridden.
- `_StdinReader`: two readers on one stdin swallow every typed answer, which made a consent walk
  unanswerable with a human present. Ask how many readers can exist.
- `ask()`'s drain — **`TECHNICAL_DEBT 44` is open**: the drain can only drop what the pump already
  took.
- `core/ansi.py` and `core/line_pattern.py` are shared with S15. Own the sanitiser; let S15 own
  whether it is called.

**Tests** `test_cli` · `test_session_cli` · `test_interaction` · `test_question_tool` ·
`test_call_view` · `test_line_pattern` · `test_attended` · `test_e2e` · `test_thread_refs`

### S17 — project scaffolding and prompts

**Files** `project_init/generator.py` · `project_init/manifest.py` · `project_init/doc_sets.py` ·
`project_init/config_files.py` · `project_init/tui_commands.py` · `prompts/system_prompts.py`

**Focus**
- **Four lists answered one question and no two agreed** (#96, #94): `manifest._MANIFEST_FILES`,
  `detect_facts`' stack branches, `_root_documents.interesting`, and `project_docs`' allowlist. The
  fix was one table and one predicate — verify that is what is there now, by driving them rather
  than reading them.
- The write loop's error paths (#95). It caught a denial that cannot fire while `_write` raises for
  every `OSError`, so disk full, permissions and a read-only mount all took a branch that did not
  exist — the list of files already created was discarded and `_settle_trust` was never reached,
  **leaving a project the user had trusted a moment ago untrusted with nothing saying so.** Ask of
  every `except` here: what raises this, and what raises the thing next to it?
- `doc_sets.py` writes stubs bearing the names of this project's own documents. Check what that does
  to the readable-doc set and to `read_project_doc` inside a scaffolded project.
- Prompt assembly, and text that instructs a run to use something it cannot call (#68) — the recorded
  case was 819 characters, 19% of a pass's system prompt, naming a tool the same run had hidden.

**Tests** `test_project_init` · `test_project_path_guard` · `test_prompt_tier_boundary` ·
`test_catalog_text` · `test_untrusted_content`

### S18 — build, CI, packaging, and supply chain

**Files** `.github/workflows/` (8: `bandit`, `codeql`, `compat`, `gitleaks`, `lint`, `pip-audit`,
`publish`, `tests`) · `.github/bandit-baseline.json` · `.github/gitleaks.toml` ·
`.github/dependabot.yml` · `.github/ISSUE_TEMPLATE/` · `.github/PULL_REQUEST_TEMPLATE.md` ·
`requirements.txt` · `pyproject.toml` · `package.json` · `bin/venastine.mjs` ·
`scripts/prepublish-check.mjs` · `pytest.ini` · `.gitignore` · `.gitattributes` ·
`.gitleaksignore` · `config.yaml` · `.env.example` · `providers.json.example`

**Focus**
- **Does each job actually fail the build when its tool finds something?** This is shape 2 applied to
  CI, and there is a live instance of the mechanism: piping bandit to `tail` makes `$?` *tail's*, so
  "7 leaks found" exits 0. Read each job for the same class of mistake.
- Action pinning: a tag is mutable, a SHA is not.
- The bandit baseline is committed in **Linux path form** (`./x.py`), so on Windows every one of its
  entries misses and all its issues read as new. The question for this scope is whether CI's result
  and a local one can ever agree, and whether anything would notice if the baseline silently stopped
  matching.
- `.gitleaksignore` fingerprints are `file:rule:line` — so an absolute `--source` makes every entry
  unmatchable. Measured in batch 128: 7 findings absolute, 0 relative, same bytes.
- ~~`publish.yml`'s **three independent brakes** (no push or tag trigger, `dry_run` defaults true, no
  `NPM_TOKEN`) must all still be there, and `scripts/prepublish-check.mjs` is the gate it runs.~~
  **FALSE WHEN WRITTEN, corrected batch 141 (S18-06).** PR #197 "Arm npm publish" added
  `push: tags: ['v*']` in September 2026, `dry_run` governs `workflow_dispatch` only, and the
  repository is already public. One brake survives -- the absent `NPM_TOKEN` -- and a fixer
  acting on the struck text above would have reverted a merged owner decision. `AGENTS.md`
  § *Distribution* has the state; `prepublish-check.mjs` is still the gate, now beside an
  inlined secret scan.
- ~~`package.json`'s `files` allowlist is the **third** copy of "keep local secrets out of a
  distribution", after `.gitignore` (→ `git push`) and `.gitattributes export-ignore`
  (→ `git archive`). Three copies of one rule is three chances to disagree.~~
  **THERE WERE FIVE, and batch 140 made it one.** S18-08 counted them: the two git
  channels, `prepublish-check.mjs`'s `FORBIDDEN`, `never_named` in the suite, and the two
  documents that each claimed three. `package.json`'s `neverDistribute` is now the single
  list all three channels read, and the disagreements it closed were real: `secrets.json`
  was in neither npm-side list, and the database test missed the `-wal` sidecar.
- **`docs/CONTRIBUTING.md` says it is the prose source and `.github/ISSUE_TEMPLATE/` its
  "machine-readable copy", with "change both together, or the taxonomy drifts".** Both halves are
  checkable and one is already drifting. **Seeded, measured, do not re-derive:** the file's label
  rule says `enhancement` is where "most open findings sit", and across the 126 severity-tagged
  issues `S3` + `bug` is 51 of them against `S3` + `enhancement` at 9. Separately, the corpus
  contains an `[S0]` severity and a `complexity` area that the template's dropdowns do not offer
  — both only on issues predating the template, which is the defensible answer, and it is
  written down nowhere. Nothing mechanically checks the template against the prose, or either
  against what people filed.
- Dependency floors and ceilings versus what `pip-audit` reaches, and whether `requires-python` is
  enforced at install time rather than discovered at runtime.
- `pytest.ini` is shared with S19. Own the CI invocation; let S19 own the suite semantics.

**Tests** `test_docs_consistency` carries the npm, launcher and version gates
(`test_the_npm_package_and_pyproject_state_the_same_version`, `test_every_npm_allowlist_entry_exists`,
`test_the_npm_allowlist_cannot_ship_a_secret`,
`test_the_launcher_never_sets_the_two_variables_that_would_break_it`,
`test_the_launcher_and_python_agree_about_the_config_state`).

### S19 — test infrastructure and suite-wide integrity

**Files** `conftest.py` (root) · `tests/conftest.py` · `tests/test_docs_consistency.py` ·
`tests/_isolation_probe.py` · `tests/stdio_server_fixture.py` · `pytest.ini` (seam with S18)

Every other scope judges its own tests. This scope owns the machinery they all run on.

**Focus**
- **The four vacuity shapes** (shape 7), swept across the suite's infrastructure rather than its
  cases. The question for a fixture is what it makes unobservable.
- **Autouse fixtures that reinstall clean state for every test** — which is why three ordered
  couplings in this suite go *silently vacuous* rather than red when `--dist` groups them apart.
  `pytest.ini` names all three. Ask what else depends on order and has no such protection.
- The `--dist loadscope` grouping contract, and its two stated costs: a future coupling between two
  *classes* in one file goes vacuous, and a non-function-scoped fixture in a file that also has
  classes is set up twice on a worker that takes both.
- The root `conftest.py`'s import-time SDK stubs. They are in `sys.modules` before collection, which
  is load-bearing and also means a guard test that checks only the first dotted segment reports a
  false positive — `google` alone is a namespace package protobuf puts there regardless.
- The skips. The suite reports 46 of them; the question is which are permanently true on this
  platform and therefore which guarantees nobody here has ever observed.
- **Whether each gate in `test_docs_consistency.py` can fail.** The repo trusts those gates *instead
  of* re-reading its own documents, so a vacuous gate is worse than no gate.
  **ANSWERED (S19-03, fixed batch 151), and the answer was in the gatekeeper rather than in a
  gate.** Every individual gate could fail; what could not was `_was_narrowed`, which decides
  whether any of them asserts at all and did so by comparing `markexpr` against a string literal
  copy of `pytest.ini`'s addopts. Change the marker expression -- two lines, because
  `--strict-markers` -- and four gates skip while one stops running anywhere, silently. The guard
  written for exactly that failure direction shared the literal with the thing it guarded, so it
  asserted that two copies of one string agreed. The lesson for a future scope is the one this
  bullet half-states: ask what decides whether a check RUNS, not only whether it can fail once it
  does.

**Seeded findings — already confirmed, do not spend time re-deriving:**

1. `pytest.ini` says, twice (in the `loadscope` rationale and in its "two things it costs" list),
   that `real_storage` is the suite's **only** non-function-scoped fixture. `tests/test_tui.py` says
   it a third time. All three are false, and have been since batch 110: `test_storage_e2e.py` also
   declares `_schema_cache_is_put_back` with `scope="module"`, and
   `grep -rn 'scope="module"' tests/` returns both. Batch 128 corrected this sentence in `AGENTS.md`
   and in `tests/BREAKING_CHANGES.md` and **missed these three copies** — a batch whose entire
   subject was that claim.
2. `pytest.ini` says `tests/test_tui.py` "is 62 classes and so 62 groups, where it was 21". It is 71
   classes, 64 of them `Test*`.

Report them with their severity and shape, and use them as the pattern: a claim in a comment made
false by a change somewhere else, in the file that configures how the whole suite runs.

**BOTH FIXED in batch 151, and both seeds were undercounts in one direction and overcounts in the
other.** Seed 1 named three copies; there are **five** -- the register's (`TECHNICAL_DEBT` item 30,
the sharpest, because it cites the refuting grep in the same sentence) and `AGENTS.md`'s, which is
the copy batch 128 *corrected* to "the suite's ONLY two" and which is therefore true today and
false the moment somebody adds a third. The count is deleted at all five and the rule is gated for
every file by `test_every_file_with_a_module_scoped_fixture_declares_no_test_class`. Seed 2's digit
was right -- and the half-sentence beside it, "where it was 21", is **correct** rather than drift:
21 pre-existing classes plus the 41 the classless tests became is the 62 the same sentence states,
so the reading that it contradicts `AGENTS.md`'s 41 would have put an error into a consistent
sentence. `pytest.ini` states no count at all now; `AGENTS.md`'s is the gated one.

**Tests** `test_docs_consistency` · `test_probe_isolation` · `test_config_edit` (the loader-cache
class) · `test_storage_e2e` · `test_fake_storage_mirror`

---

## Triage and the fix pass

1. **All reports land first.** The tree is frozen until then, so every finding shares one base
   commit and every `file:line` resolves.
2. **A triage index is built** at `review/TRIAGE.md`: every finding sorted by severity, then
   disposition; duplicates merged across seams with both reporters named; `KNOWN` items folded back
   into `docs/TECHNICAL_DEBT.md` with whatever the report adds to the entry; `DECISION` findings in
   their own section for the owner.
3. **One agent applies the fixes, sequentially, severity-first across all scopes**, grouped into
   batches that share a surface so each commit stays coherent. Each batch follows this repository's
   existing convention, which is not optional and is what the marathon's value depends on:
   - a `docs/DEVLOG.md` entry;
   - a `tests/BREAKING_CHANGES.md` row for every new or changed guard — what breaks it, the symptom,
     the fix;
   - a **negative control** for every new test, proving it goes red when the thing it guards is
     broken, restored from bytes held in memory rather than `git checkout`;
   - a mutation pass;
   - the full suite green before the batch is called done;
   - a new `docs/TECHNICAL_DEBT.md` item for everything dispositioned `DEFER`, and an *Accepted
     risks* entry with its argument for everything `ACCEPTED`.
4. **The merge can happen after the `BLOCKS-MERGE` set is clear**, with the rest carried in the
   register. That is the decision this whole document exists to inform.

---

## Run log

The base commit is the commit that added this file. Every report's *Measured against* line carries
it, and the tree stays frozen against it while the marathon is open.

| Scope | Reviewer | Date | Report | Findings (s1/s2/s3/s4) |
|---|---|---|---|---|
| S01 shell gate | agent | 2026-10-08 | `review/S01-shell-gate.md` | 0/0/2/1 |
| S02 sandbox & posture | agent | 2026-10-08 | `review/S02-sandbox.md` | 0/2/5/3 |
| S03 permissions & registry | agent | 2026-10-08 | `review/S03-permissions-registry.md` | 0/1/0/1 |
| S04 file & doc tools | agent | 2026-10-08 | `review/S04-file-doc-tools.md` | 0/1/3/2 |
| S05 network & content policy | agent | 2026-10-08 | `review/S05-network-content.md` | 0/0/3/1 |
| S06 math & compute | agent | 2026-10-08 | `review/S06-math-compute.md` | 1/3/4/5 |
| S07 secrets & logging | agent | 2026-10-08 | `review/S07-secrets-logging.md` | 0/2/3/5 |
| S08 config, schema, trust | agent | 2026-10-09 | `review/S08-config-trust.md` | 0/0/3/2 |
| S09 persistence & memory | agent | 2026-10-09 | `review/S09-persistence.md` | 0/0/2/2 |
| S10 loop, client, cancel | agent | 2026-10-09 | `review/S10-loop-client.md` | 0/4/3/0 |
| S11 compaction & sessions | agent | 2026-10-09 | `review/S11-compaction-sessions.md` | 0/2/4/3 |
| S12 research pipeline | agent | 2026-10-09 | `review/S12-research-pipeline.md` | 0/1/2/0 |
| S13 agents, skills, MCP | agent | 2026-10-09 | `review/S13-agents-skills-mcp.md` | 0/1/6/1 |
| S14 TUI lifecycle | agent | 2026-10-10 | `review/S14-tui-lifecycle.md` | 0/3/3/4 |
| S15 TUI rendering | agent | 2026-10-09 | `review/S15-tui-rendering.md` | 0/1/0/1 |
| S16 headless shell | agent | 2026-10-09 | `review/S16-headless-shell.md` | 0/0/2/2 |
| S17 scaffolding & prompts | agent | 2026-10-10 | `review/S17-scaffolding-prompts.md` | 0/0/3/3 |
| S18 build, CI, supply chain | agent | 2026-10-10 | `review/S18-build-ci-supply-chain.md` | 0/1/6/4 |
| S19 test infrastructure | agent | 2026-10-10 | `review/S19-test-infrastructure.md` | 0/0/5/8 |

**All 19 scopes reported.** 142 findings, 130 severity-tagged: 1 severity `S1`, 22 `S2`, 59 `S3`, 48 `S4`. The *Reviewer* column says `agent` because the report format carries no reviewer field -- the column is not unfilled, there was nothing to fill it from.

The triage index is `review/TRIAGE.md` (gitignored, for the reasons in § *Where the reports go*). **The `BLOCKS-MERGE` set is clear as of batch 135**, fixed one surface per commit in batches 130-135, each with a `docs/DEVLOG.md` entry, a `tests/BREAKING_CHANGES.md` row per guard, a negative control, a mutation pass and a green full suite. So the merge can happen, with the rest carried in the register -- which is the decision this document exists to inform.

Both `OWNER-DECISION` findings were then ruled and fixed (136, 137), and batch 138 moved everything this document still owed into the register: the 35 `DEFER` findings as items 49-82, the `ACCEPTED` one into *Accepted risks*, and the 12 `KNOWN` ones folded back into the entries they arrived at. **That tranche went before the remaining fix work for one reason: `review/` is untracked, so until it landed, 35 measured findings existed in exactly one place that `git clean` would remove.** What is left is the 86 distinct `FIX-NOW` findings, grouped by the surfaces in `review/TRIAGE.md` § 3.

| batch | finding | surface | mutation |
|---|---|---|---|
| 130 | S02-01 | `security/sandbox.py` | 3/3 |
| 131 | S06-01 (the severity `S1`) | the four parsing math tools | 7/7 |
| 132 | S10-01 | `core/client.py` | 7/7 |
| 133 | S10-02 | `core/loop.py` | 2/2 |
| 134 | S11-01 | `core/compaction.py` | 5/5 |
| 135 | S14-02 | `tui/app.py`, `tui/commands.py` | 4/4 |
| 136 | S18-07 (owner ruling) | `requirements*.txt`, `pyproject.toml`, 5 CI jobs | 5/5 |
| 137 | S13-07 (owner ruling, **A16**) | `agents/subagent_tool.py` + 4 documents | 4/4 |
| 138 | the `DEFER`/`KNOWN`/`ACCEPTED` tranche | `docs/TECHNICAL_DEBT.md` 49-85 | 3/3 |

**This table stops at 138 and the work did not** (noted batch 150). Batches
139-156 cleared eleven more surfaces -- secrets/logging/json_store, `tui/`
lifecycle and rendering, file and doc tools, loop + client, compaction +
sessions, `core/reasoning/`, `agents/` + `skills/` + `mcp_client/`, test
infrastructure, the headless shell, and project scaffolding + prompts --
and none of them added a row here. **With batch 153 every per-surface
table is CLEAR**; batch 154 then cleared the persistence group (S09-01 to
S09-04), batch 155 the config-and-trust group (S08-01 to S08-03) and batch 156
the last three (S05-01, S05-02, S03-02). **The `FIX-NOW` queue is closed.** **`review/TRIAGE.md` is the live index**
and is updated per batch; the per-batch narrative is `docs/DEVLOG.md`, which
is where a row's worth of detail actually goes. The table is kept as the
record of the `BLOCKS-MERGE` tranche and the two owner rulings, which is
what it was built for, rather than extended into a third copy of a count
that would then drift -- which is the defect shape this marathon spends most
of its time finding. The paragraph above it says "what is left is the 86
distinct `FIX-NOW` findings", and that was true when it was written: **0
are left as of batch 156** -- a figure batch 152 had to correct before it
could state it, since this count and `review/TRIAGE.md`'s per-surface
tables had disagreed by four since batch 142. See that batch's DEVLOG
entry; the tables partition the 86 exactly and are what both now say.
