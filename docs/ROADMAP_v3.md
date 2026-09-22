# Roadmap v3 — Command execution: sessions, runtimes and backends

**Purpose of this document:** the specification and locked decision record for work begun after
`ROADMAP_v2.md` §48. It opens with §49, the shell-sessions work: background and monitor sessions that
outlive a turn and wake the agent, interactive sessions, a second container runtime, WSL and SSH
backends, and harness-held secrets.

**Why a third document rather than §49 in `ROADMAP_v2.md`** (owner decision, SS21). The owner judged
that appending to v2 after its long run would read as more of the same revision, which this is not.
**Section numbering continues at §49**, so a `§N` names one section across all three documents and every
existing `ROADMAP_v2 §N` citation keeps its single meaning. Decision ids stay globally unique for the same
reason: `tests/test_docs_consistency.py` reads this file as part of the record beside `ROADMAP.md` and
`ROADMAP_v2.md`.

**Relationship to the other roadmaps:** `ROADMAP.md` (§1–§12) and `ROADMAP_v2.md` (§13–§48) are fully
built. Conventions are unchanged: decisions are locked and not re-litigated without cause, a built section's
decision record is append-only, and a deviation is recorded as an owner decision in `DEVLOG.md`.

**Section numbers below are stable** — other documents cross-reference them by number.

---

## Index

- **§49. Shell sessions, the container runtime, and where a command can run** — **(IN PROGRESS: slice 0, Podman, BUILT in batch 93; slice 1 COMPLETE -- its foundations in batch 94, its five tools, the sleeping subagent and the CLI wait loop in batch 95, the TUI's behaviour half in batch 97 and its display half in batch 98; slice 2, interactive sessions, next)** (the shell was one-shot and blocking, so a test suite could not outlive a turn and nothing could wake the agent; a machine with Podman and no working Docker had no sandbox at all)
- **§50. Declared network for a shell command** — **(BUILT in batch 99)** (the network detector reads command TEXT against a fourteen-word list, so a command it cannot see is auto-approved into `--network none` and fails with no way for the user to have allowed it)
- **§51. Paging for network tool results** — **(RECORDED in batch 96, not built)** (`fetch_url` and `arxiv_search` truncate with no offset, so the agent cannot reach past the first page without shelling out to a raw HTTP request)

---

## §49. Shell sessions, the container runtime, and where a command can run

**Added in batch 93, from the owner's request** for background, monitor and interactive shells; WSL and
SSH execution beside the container; an SSH key passphrase, remote password and sudo password entered once
and never seen by the agent; one concurrency cap; a sidebar panel for sessions like the agent panel;
sessions that outlive the turn and re-wake the agent; a block on user input while they run; and subagent
use. It was designed across six question rounds with a second agent's review merged in, and is built in
slices. The owner added Podman (slice 0) after the plan was approved.

### What was measured before anything was designed (2026-09-15)

- A pty **inside** the backend, driven over plain host pipes, holds a session: `docker run -i ... script
  -qfec "bash --norc --noprofile -i" /dev/null` fed bytes from `subprocess.Popen` returned `/dev/pts/0` and
  kept `cd` across inputs; `wsl.exe -d Ubuntu -e script ...` did the same (`/dev/pts/5`). Interactive
  sessions need no host-side pty library (stdlib `pty` does not exist on Windows). The output carries the
  echoed input and ANSI sequences.
- `python:3.13-slim` carries util-linux `script`, `setsid` and coreutils `stdbuf` and `timeout`.
- **WSL is not an isolation boundary.** From Ubuntu, `/mnt/c` is automounted and the harness's own
  `config.yaml`, `providers.json` and `~/.config/venastine` are all writable; interop is on, and
  `cmd.exe /c echo` ran on Windows. Killing `wsl.exe` did kill its Linux child.
- **CPython `re` holds the GIL.** `re.match(r"(a+)+$", "a" * 25 + "b")` took 1.88 s, and a ticker thread's
  largest gap was 1.98 s: every thread froze. Each extra character doubles it, so about 35 is half an hour.
- coreutils `timeout` inside the container ends a session with no harness alive: `timeout -k 2 3` over two
  sleeping children exited 124 after 3.6 s, and a child ignoring TERM was killed (137) after 5.5 s; the
  container was removed both times.
- `google-re2` 1.1.20251105 is BSD-3-Clause, ships wheels for cp310–cp314 on Windows (x64, x86, arm64),
  macOS 13–15 (arm64, x86_64) and manylinux_2_28 (x86_64, aarch64), none for musl or older glibc, and its
  wrapper releases the GIL while matching.
- **Podman runs the argv the Docker route builds, unchanged** (podman 5.7.0, rootless, cgroups v2, systemd,
  controllers cpu/memory/pids delegated, WSL Ubuntu): writable workspace, nested `:ro` bind refusing writes,
  `--network none`, argv mode, `--label` with `ps -q --filter label=`, kill by name with `--rm` removal,
  `--sig-proxy=false`, the in-container `timeout -k` backstop, `image inspect --format {{.Id}}`. The limits
  land: `memory.max` 64 MiB, `pids.max` 20, `cpu.max` one CPU. Files a rootless container writes are owned
  by the host user, where Docker's are root's.
- The short name `python:3.13-slim` resolved under Podman with no terminal **only because** that distro's
  `shortnames.conf` aliases `python`; its `registries.conf` names no search registries, so an image without
  an alias would not resolve. Podman's and Docker's local IDs for the tag differed because the tag moved
  between the two pulls (2026-08-25 against 2026-09-01), not because of the runtime: a tag is not an
  identity (EP7).
- `podman info --format "{{json .}}"` carries a top-level `host` object with `cgroupVersion`,
  `cgroupControllers` and `security.rootless`; Docker's has no `host` key.

### What the code could not do

- `core/loop.py` answers every `tool_use` in the same step (D20, persist-before-emit, NA11): no tool can
  finish later, and nothing adds a message to a thread without a user turn.
- `spawn_subagent` blocks its parent until the child returns; parallelism exists only within one response
  (NA9).
- Output policy runs only inside `registry.dispatch`.
- A headless run hides a tool only when `approval_needed(name, {}, ctx)` asks, so under `never` a research
  pass would be offered a session tool nothing could ever wake.
- `pinned_through` treats any kept row's `tool_call_id` as an answered call.
- Session children would inherit stdin today, which is a second reader beside the CLI's one (§29 N1), and a
  console Ctrl+C reaches every child.
- The host fallback applies `RLIMIT_CPU = sandbox_cpu_seconds` (30).

### Decisions (SS1–SS24)

| # | Decision |
|---|---|
| **SS1** | **WSL and SSH are UNCONTAINED backends.** Under `tiered` and `contained` only a read-only in-workspace command runs unasked; `never` asks nothing and `always` everything; the badge names the backend. Neither withholds network or confines the filesystem, and WSL reaches the harness's own authority files |
| **SS2** | **Input is blocked while a background or monitor session is live, and the kill switch is not.** A plain prompt and every command refused while `_busy` are refused, between turns too; modal answers, non-interruptive commands, a kill command and key, and quit stay usable. After a set number of consecutive wakes with no user input, waking stops, the plain-prompt block lifts, and held results arrive with the user's next message |
| **SS3** | **Sudo is harness-run elevation only.** The agent marks a command as needing root and the harness runs it through sudo with the password on sudo's stdin -- never argv, the environment, or a shell the agent controls, where a fake prompt or a `sudo` function captures it. Approval follows `shell_approval_mode` |
| **SS4** | **Slices, in order:** background and monitor; interactive; WSL; SSH; sudo |
| **SS5** | **A wake is a persisted harness turn.** A user-role `MessageLog` row marked by a column (batch 91's rule: a column, never a role) starts a turn. Resume and compaction see an ordinary turn; the transcript draws a harness line, not `you ›`; the content passes the real `check_output_policy`; the row never carries `tool_call_id` |
| **SS6** | **Quitting with live sessions confirms, then kills and records** a harness line in each owning thread. No re-attach |
| **SS7** | **A monitor wakes on every matching line.** Matches arriving during a running turn coalesce into one wake; exit and timeout also wake; the shapes are `matched`, `exited`, `timed_out` and `killed` |
| **SS8** | **Monitor patterns use RE2 (`google-re2`),** pinned exactly, imported by one module. An invalid pattern is refused before approval; the live stream is matched line by line; what is returned is redacted, not what is matched. Every legal document is updated with the pin |
| **SS9** | **A subagent with live sessions sleeps inside its spawn call,** is woken there, then answers its parent. Nothing new crosses to the parent (D6); its sessions count toward the cap |
| **SS10** | **Background subagents are their own section after slice 5,** reusing SS5 |
| **SS11** | **Separate tools, each with its own `tool_permissions` flag shipped false:** `shell_background`, `shell_monitor`, `shell_sessions`, `shell_output`, `shell_kill`. Reading output and killing need no approval |
| **SS12** | **Defaults** (config.yaml, editable through `/config`, not frozen posture): a 3600 s timeout cap, 4 live sessions process-wide, 10 consecutive wakes, and each session's output kept in memory as its first 10 000 and last 190 000 characters |
| **SS13** | **A timeout above the cap is clamped in one place,** and the started result says so |
| **SS14** | **The sidebar shows live sessions only;** a finished session opens by ctrl+click on its inline tool-call line (NA6's pattern, resolved at press time) |
| **SS15** | **Host-fallback sessions follow the one-shot rule,** and are killed by process group at timeout, kill and quit |
| **SS16** | **The two start tools join `shell` in the `shell_approval_mode` exemption from `tool_approvals`.** A second switch over the gate is the ratchet G3 removed. An import-time assert ties the exemption to `approval_check is shell._shell_approval_check` |
| **SS17** | **A start call is refused unless a wake consumer is registered for the run** -- the TUI chat turn, CLI chat, a subagent -- before any approval prompt and again in the handler. Research passes never start sessions, whatever the mode |
| **SS18** | **After a restart, a session's view shows what the agent was given:** command, rationale, final status and the redacted excerpts from its saved wake rows, under a header saying full live output is not kept past the process |
| **SS19** | **A kill the user makes between turns does not wake the main agent;** it is delivered with the user's next message. A subagent's sessions still wake the subagent |
| **SS20** | **A subagent reaching the consecutive-wake limit** has its remaining sessions killed, gets one final wake carrying the killed results, then returns |
| **SS21** | **This record lives in `ROADMAP_v3.md`, continuing at §49** |
| **SS22** | **Podman is the container runtime when Docker cannot run a container and Podman can.** Docker is probed first; if it is not installed or does not answer, Podman is probed; Docker wins when both answer; the answer is probed once per process. It is the same container route with a different CLI, not a new backend |
| **SS23** | **The shipped image is fully qualified,** `docker.io/library/python:3.13-slim`, so it resolves under Podman without a short-name alias. A value a user set is carried as written |
| **SS24** | **Podman that cannot enforce the resource limits is not used.** Rootless on cgroups other than v2, or without the cpu, memory and pids controllers delegated, means `--memory`, `--cpus` and `--pids-limit` would not bind; the harness then behaves as if no runtime exists (the fallback rules apply) and the log and the unavailable message say why |

### Adopted defaults (implementation-level; the owner may overturn any)

- A quit row for a thread whose turn is still in flight is recorded on the thread and written at its next
  turn start or resume, so it never lands between a `tool_use` and its `tool_result`.
- Wake rows are a `wake` transcript role in `META_ROLES`, retire the `venastine ›` label like `user`, replay
  as their header line, and are labelled `harness:` for the compactor.
- **Adjacent user-role messages are NOT merged** (amended in batch 94). The plan adopted a merge above the
  provider branch split. Measured against `core/memory.py`, a compacted thread already sends two
  consecutive user messages to every provider -- M8's summary, then the first turn of the kept tail -- so
  a wake row beside a user message is not a new shape on the wire, and a merge would have changed the
  request of every compacted thread for no case that needed it.
- After the wake limit, `/new`, `/threads` and `/resume` stay refused while sessions live; CLI EOF with live
  sessions kills and exits without asking.
- Session stderr is merged into stdout; a wake carries a shared tail up to `MAX_READ_CHARS` and up to 50
  matched lines per session with a count; the host-fallback session's `RLIMIT_CPU` is its effective
  timeout; the kill key is chosen by elimination against the live bindings and **always opens a picker,
  including for a single session** (amended in batch 97 -- this said "when more than one session is live",
  which made one key mean two things and put the destructive one on the case that needed no confirmation;
  a one-row list costs a keypress and removes the mistyped-key case, and until the panel lands it is also
  the only thing on screen that says what is running); the session view is redacted for display;
  `_viewing` widens to hold a session;
  the last 20 finished sessions are kept in memory; the listing, output and kill tools see only the caller's
  own thread's sessions; session containers carry a per-process label, `--sig-proxy=false` and
  `stdin=DEVNULL`.
- **The session panel is process-wide** (owner, batch 98), like the agent panel it sits under and like
  ctrl+b's picker. SS11 scopes the five TOOLS to the caller's own thread, which is a rule about what the
  model may touch; what the person may SEE is a different question, and a panel that hid a subagent's
  sessions would disagree with the kill picker about what is running.
- **A live session view polls its buffer on a timer** (owner, batch 98), under `_sync_view_poll`'s existing
  discipline: while the session is live and not one tick afterwards. Measured: the sink fires when a session
  MOVES -- starts, matches, finishes -- and not when it prints, so a background session's view would sit
  frozen between its first line and its last. Compared by the buffer's own character count, not by the length
  of the tail drawn, because a session past the tail bound has a tail that stops changing length while its
  content goes on moving.
- **A rebuilt view takes its command and rationale from the stored tool CALL** (owner, batch 98), and is keyed
  by call id rather than session id. Measured: the saved wake record carries only `{id, call_id, shape}` and
  the row's text carries the command but never the rationale, so the call -- the exact params the model sent
  -- is the only honest source for both, and it needs no widening of what is persisted. It also covers a
  session pruned from memory in the SAME process (SS12's twenty), so the in-process and after-restart routes
  are one route. Keyed by call id because a session id is a per-process counter: last week's `s1` is not this
  process's `s1`, and looking one up by id across a restart would draw a different session with confidence.
- **`ReplayEntry`'s fourth slot carries `(kind, call id)`** (batch 98), where it carried a bare id armed only
  for threads. A `▸ shell_background` line opens a session, which is read from the manager or rebuilt from
  the archive rather than replayed -- so the kind has to survive to the click, and deriving it there from the
  tool name would be the second copy the slot exists to prevent. `registry.opens()` is the one question both
  readers ask.
- **A `docker` command that is really Podman** (the podman-docker shim) is checked as Podman: its `info`
  output is Podman's, and SS24 would otherwise be skipped under the Docker name.
- **`sandbox_docker_image`, `AGENT_SANDBOX_IMAGE`, `is_docker_available` and `_run_docker` keep their
  names**, and now mean the container route whichever runtime serves it. Renaming the key breaks
  `config_update.py`'s merge of a user's own value, and the function names are patched at about thirty-five
  test sites.

### Slices

0. **Podman** (SS22–SS24) -- BUILT, batch 93.
1. **Background and monitor sessions** on the container route and the host fallback (SS2, SS5–SS20).
   Foundations BUILT, batch 94: the RE2 pattern engine, the wake row, the session backends, the manager
   and the wake builder. The five tools, the subagent asleep in its spawn and the CLI's wait loop BUILT,
   batch 95. The TUI's BEHAVIOUR half BUILT, batch 97: the turn is a wake consumer (`consuming()` around
   the drain, the wait loop on the turn worker, held results delivered with the user's next message), the
   input block and its one refusal funnel, `/kill` and ctrl+b, and the quit confirmation (SS2, SS6, SS19).
   Until batch 97 a session could not be started from the TUI at all -- SS17 refuses a start with no wake
   consumer, and nothing under `tui/` had ever entered `consuming()`.
   The TUI's DISPLAY half BUILT, batch 98 (SS14, SS18): the sidebar panel fed by a `SessionActivity` sink,
   the tool-call line that opens the session it started, the live view polled from the manager's buffer, and
   the rebuilt view for a session this process no longer holds. **Slice 1 is complete.**
   It cost one widening below the TUI: `ReplayEntry` carries `(kind, call id)` where it carried a bare id,
   and `registry.opens()` is the one question both readers ask -- a `▸ shell_background` line could not
   otherwise be armed in a REPLAY, so a session opened until you restarted and then went quiet.
   Next: slice 2, interactive sessions.
2. **Interactive sessions.**
3. **WSL.**
4. **SSH and the secrets it needs.**
5. **Sudo** (SS3).

Then background subagents as their own section (SS10).

### Gap register for the later slices

- **Interactive:** a hung read would wedge the turn, so every send needs a bounded wait. Input is not a
  command -- aliases, `cd` and a REPL mean the text is not the argv -- so every input is `runs_code`, and
  network is fixed when the session opens. The per-input gate is per tool call, so no deferred approval
  exists. Open: lifetime, ANSI stripping against a terminal-emulator dependency, whether Ctrl+C needs
  approval, the live view.
- **WSL:** SECURITY.md's "fixed" claim about reaching the install tree and `~/.config/venastine/` holds for
  the container only; `.venastine/` writes are detected at the next launch by D17's hash, not prevented;
  `ran_on` gains `wsl`; INERT runs through `wsl.exe -e` as an argv; paths through `wslpath`; open: distro
  selection, and the backend joining the frozen posture.
- **SSH:** `write` saves locally while the shell runs remotely; network is always on; "inside the
  workspace" is lexical against a configured remote directory; INERT tokens reach the remote login shell
  single-quoted, which is exact because quotes and backslashes are already rejected; host keys need a
  policy and a trust prompt; `ssh localhost` is the host. Secrets live in process memory only, are fed
  through stdin or library callbacks, are redacted by value whatever `redact_tool_outputs` says, need a new
  request kind in both shells, and are cleared at quit. Open: the connection library and its supply-chain
  review, since Windows OpenSSH does not multiplex; the host configuration's shape; disclosure of the remote
  binary; process-group kill on the remote.
- **Sudo:** `/usr/bin/sudo -k -S -p '' --`, where `-k` stops a cached ticket passing the password line to the
  command; separate values for WSL and SSH; a badge entry.
- **Docker rootless on cgroups v1** ignores limits the same way SS24 describes for Podman, and is not checked.
  Recorded, not decided.

### Rejected

- **A Python `re` pattern engine,** for the GIL measurement above.
- **A `tool_approvals` field for the start tools** (SS16).
- **A third copy of the routing ladder** for sessions: `containment_for` and `run_sandboxed` are already one
  decision written twice (EP6).
- **`tool_call_id` on a wake row.**
- **Treating WSL or an SSH host as contained,** and **auto-filling sudo prompts.**
- **Podman as a separate backend.** It serves the same route with the same argv, measured.
- **Qualifying the image only under Podman,** which is a silent rewrite of a configured value (SS23).

### What slice 0 costs, stated

- With Docker missing or stopped, launch pays for up to two probes, each bounded at 10 s, once per process.
- A rootless Podman that cannot enforce the limits is refused rather than used, so that machine has no
  container route until its cgroups are fixed.
- Unmeasured: Podman on Windows (`podman machine` and `C:\` bind paths), rootless cgroups v1, and SELinux
  relabelling of bind mounts.

---

## §50. Declared network for a shell command

**Added in batch 96, from an owner report**, and recorded rather than built: the batch was scoped to CI and
two transcript defects. It is the first §49-adjacent section because it is the same subject -- where a
command runs and what it is allowed to reach -- and because it must cover the session tools §49 just built.

### What was measured (2026-09-17)

- `_needs_network` is a **lexical** test: it reads the command's tokens against
  `config.network_allowed_commands` (14 words -- `pip`, `curl`, `wget`, `git`, `npm`, `apt`, `cargo` and so
  on) and answers from the words alone. It refuses to parse, deliberately (G2).
- A command it DOES recognise is never auto-approved: `network=True` makes the tier `SANDBOXED_NET`, and
  `auto_approved` returns False under `contained` and `tiered` alike. That half works.
- A command it cannot see -- `python3 - <<EOF ... urllib.request ...`, an inline `node` fetch, a script that
  opens a socket -- profiles `network=False`, is **auto-approved** under `contained`, and then runs under
  `--network none`. The user is never asked, so there is no answer they could have given.
- The model is never told. `shell`'s schema describes where a command runs and says nothing about egress, so
  a failed connection reads to the agent as a broken network rather than a withheld one.

### Decisions (NW1-NW5)

| # | Decision |
|---|---|
| **NW1** | **The agent declares intent with a `requires_network` boolean**, beside `rationale`: a thing it states, not a thing the harness infers from text it has refused to parse |
| **NW2** | **The flag is OR'd into the existing single `network` fact, never an override.** `network = _needs_network(...) or requires_network`. It can only ADD egress, so a model cannot clear the flag on `curl` to dodge a prompt, and the one fact keeps its two consumers (the gate and the runner) reading the same value -- the property #157 cost us when they drifted |
| **NW3** | **Nothing new gates it.** Once the fact is true the existing ladder does the work: tier `SANDBOXED_NET`, `auto_approved` False under `tiered` and `contained`, so a modal appears exactly where the settings allow one. Under `never` nothing asks and under `always` everything does, which is the owner's "only force a modal where the user's settings would have produced one" |
| **NW4** | **The session tools take it too** (owner's addition). `shell_background` and `shell_monitor` classify through the same `classify_command` and `start_sandboxed` already passes `profile.network` into the container argv, so the flag reaches a session unchanged. Interactive inherits it in slice 2, where network is fixed when the session opens |
| **NW5** | **The schema says what the flag means**, because a parameter the model cannot see the point of is one it will not set: that a command needing the network must say so, and that without it the command runs with networking off |

### Gap register -- closed in batch 99

- Threads through `classify_command` and its three production callers (`shell.run`, `shell._shell_approval_check`
  and the session start handler), which must agree or the gate and the runner diverge again. **Done**, and
  measured: `run_sandboxed`'s `if profile is None` fallback is a fourth site, but it has no production
  caller (`shell.run` is the only one and it always passes a profile) and it never sees a tool's params,
  so it stays the conservative answer.
- The approval notice should say that egress was REQUESTED, not merely that the tier needs it. **Done**:
  the reason gains "and the call declared it needs the network".
- Open: whether a declared-network command that the detector also recognises should read any differently on
  the prompt (probably not -- one fact, one sentence). **Settled as written** -- when the detector already
  recognised the command the prompt reads as one sentence, and the extra clause appears only where the
  declaration is the reason the fact is true.

### What was built (batch 99)

- **`declared_network(params)` is THE coercion**, and a function rather than a `.get()` at each site. The
  gate reads the model's tool-call input before Pydantic has validated anything, so the value may be any
  JSON; only literal `True` counts, because the flag can only ADD egress and a lenient coercion would let
  `"false"` grant network. The param models use `StrictBool`, so a string is refused at run time with an
  error naming the field and nothing executes -- the gate and the runner cannot end up disagreeing about
  what ran, which is the property this section exists to keep.
- **The declaration is OR'd in on an UNMEASURED profile too.** NW2 says the flag can only add, with no
  exception, and an exception would be one more rule to remember for two profiles that always ask a human
  anyway.
- **Measured through the real gate: `contained` is the mode where the flag changes the approval answer.**
  Under `tiered` a non-inert command already asks because `runs_code` is true (§48, CE1), so there the
  declaration changes the tier, the argv and the prompt but not whether a human is asked. Under `never`
  and `always` the mode still decides, which is NW3's whole claim.

---

## §51. Paging for network tool results

**Added in batch 96, from an owner report**, recorded rather than built.

### What was measured (2026-09-17)

- `fetch_url` returns `body[:5000]` with a `truncated` flag and **no offset**, so there is no second page to
  ask for. A second bound sits under it: `MAX_CONTENT_BYTES = 65_536` stops the read, so even with an offset
  nothing past 64 KB exists to page into without fetching again.
- `arxiv_search` **hardcodes `"start": 0`** -- the arXiv API's own paging parameter -- and caps each abstract
  at `MAX_SUMMARY_CHARS = 600`.
- `web_search` caps each snippet at `MAX_SNIPPET_CHARS = 300`.
- The workaround the agent is left with is a raw HTTP request through `shell`, which is the surface §50 is
  about: it converts a reading task into a code-execution one.

### Decisions (PG1-PG4)

| # | Decision |
|---|---|
| **PG1** | **Mirror the `read` tool's shape**, which already solved this for files: an `offset`, a count clamped to a maximum with a NOTE rather than an error, and a message naming the offset to use next. One paging vocabulary across the tools that have one |
| **PG2** | **`fetch_url` gains `offset`**, and `MAX_CONTENT_BYTES` rises with the reachable window so the byte bound cannot silently cap the page count. The cost is stated to the model: a page is a re-fetch, not a seek into something already held |
| **PG3** | **`arxiv_search` exposes `start`**, which the provider already implements, so paging results costs nothing but the parameter |
| **PG4** | **A truncated value says how to get the rest.** The present `truncated: true` tells the model something was lost and nothing about how to recover it, which is what sends it to the shell |

### Gap register

- `fetch_url`'s result shape is consumed by the grounding passes and by `output_writer`'s sources directory;
  added keys are safe, changed ones are not.
- Open: whether a page should be cached for the length of a run so a second page is not a second fetch of
  the same body, and what that would cost in memory for a 64 KB-per-URL window.
