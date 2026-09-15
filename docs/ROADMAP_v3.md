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

- **§49. Shell sessions, the container runtime, and where a command can run** — **(IN PROGRESS: slice 0, Podman, BUILT in batch 93; slice 1 next)** (the shell was one-shot and blocking, so a test suite could not outlive a turn and nothing could wake the agent; a machine with Podman and no working Docker had no sandbox at all)

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
- Adjacent user-role messages are merged into one above the provider branch split, one rule for every
  provider: Google's translator batches only consecutive tool results.
- After the wake limit, `/new`, `/threads` and `/resume` stay refused while sessions live; CLI EOF with live
  sessions kills and exits without asking.
- Session stderr is merged into stdout; a wake carries a shared tail up to `MAX_READ_CHARS` and up to 50
  matched lines per session with a count; the host-fallback session's `RLIMIT_CPU` is its effective
  timeout; the kill key is chosen by elimination against the live bindings and opens a picker when more
  than one session is live; the session view is redacted for display; `_viewing` widens to hold a session;
  the last 20 finished sessions are kept in memory; the listing, output and kill tools see only the caller's
  own thread's sessions; session containers carry a per-process label, `--sig-proxy=false` and
  `stdin=DEVNULL`.
- **A `docker` command that is really Podman** (the podman-docker shim) is checked as Podman: its `info`
  output is Podman's, and SS24 would otherwise be skipped under the Docker name.
- **`sandbox_docker_image`, `AGENT_SANDBOX_IMAGE`, `is_docker_available` and `_run_docker` keep their
  names**, and now mean the container route whichever runtime serves it. Renaming the key breaks
  `config_update.py`'s merge of a user's own value, and the function names are patched at about thirty-five
  test sites.

### Slices

0. **Podman** (SS22–SS24) -- BUILT, batch 93.
1. **Background and monitor sessions** on the container route and the host fallback (SS2, SS5–SS20).
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
