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

- **§49. Shell sessions, the container runtime, and where a command can run** — **(IN PROGRESS: slice 0, Podman, BUILT in batch 93; slice 1 COMPLETE -- its foundations in batch 94, its five tools, the sleeping subagent and the CLI wait loop in batch 95, the TUI's behaviour half in batch 97 and its display half in batch 98; slice 2, interactive sessions, BUILT in batch 100; slice 3, WSL, next)** (the shell was one-shot and blocking, so a test suite could not outlive a turn and nothing could wake the agent; a machine with Podman and no working Docker had no sandbox at all)
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

### What was measured before slice 3 (2026-09-23)

Through the argv this codebase builds, not in the abstract. Two of these
changed the design and one of them closed a hole that was already open.

- **A symlink the distro makes inside the workspace is invisible to Windows.**
  It is stored as an LX reparse point (`0xa000001d`): `os.path.exists` False,
  `os.path.islink` False, `os.path.realpath` returns the path UNCHANGED. So
  `_within` said "inside the workspace" and `head -1 out_link` in the distro
  returned `root:x:0:0:root:/root:/bin/bash` -- the host's real
  `/etc/passwd`, through a command auto-approved as INERT. SS45.
- **Argv round-trips byte-exactly.** `x "y" z`, `a\\b`, `a\"b`,
  `pct %PATH% and ^caret`, a trailing backslash, `` dollar $HOME tick `id` ``
  and `semi ; pipe | amp & sub $(id)` all came back identical through
  `list2cmdline` -> `wsl.exe -e printf %s`. There is no `cmd.exe` on this
  route, so batches 37 and 39's quoting-bypass class does not reappear.
- **`WSLENV` carries a secret across, and the scrubbed environment stops it.**
  Windows variables do not reach the distro by themselves; the ones named in
  `WSLENV` do, and `WSLENV` is inherited. With it set to a probe secret's
  name the unscrubbed run printed the secret in the distro and
  `env=_scrubbed_env()` printed nothing, while `id` and `pwd` still worked.
- **`--cd` with an untranslatable path does not fail, it RELOCATES.**
  `--cd \\127.0.0.1\C$` exits 0, warns on stderr, and runs in the user's
  Linux home. A nonexistent directory and an unmapped drive DO fail
  (`4294967295`, `Wsl/ERROR_FILE_NOT_FOUND` and `ERROR_PATH_NOT_FOUND`), so
  the silent case is exactly the one a guess would miss. `wslpath -a -u`
  answers with an exit code instead, and `--cd /mnt/c/...` works.
- **Interop cannot be turned off.** `env -i /mnt/c/Windows/system32/cmd.exe
  /c echo` printed: it is binfmt_misc, not `PATH`, so no environment or argv
  choice removes it. Windows `PATH` is also appended to the distro's.
- **The harness's own authority files are writable from the distro**
  (`test -w config.yaml`, `test -w ~/.config`, and
  `/mnt/c/Users/<user>/.config/venastine` present). SECURITY.md's "fixed
  rather than documented" claim about reaching the install tree holds for
  the container and not here.
- **`wsl.exe -l -q` answers in UTF-16LE**, and a bad distro exits
  `4294967295` writing its complaint to STDOUT in the same encoding. Read as
  UTF-8 the listing is NUL-riddled and matches no name, so a probe that
  guessed the encoding would report "no distro" on a machine with three.
- **`wsl.exe` is always present** at `C:\Windows\system32\wsl.exe`, with
  or without a distro, so `shutil.which` is not a probe. A stopped distro
  starts in about 2.3 s and warns on stderr while doing it.
- **The toolchain is there and the backstop works.** `script`, `timeout`,
  `setsid`, `stdbuf` and `stty` are all in `/usr/bin`; `timeout -k 5 3` over
  a `sleep 60` exited 124 after 3.2 s; exit codes propagate exactly (1, 42).
- **Killing the Windows process kills the Linux side,** a backgrounded
  grandchild included -- checked with `pgrep -a sleep` as a bare argv. An
  earlier attempt asked through `bash -c` and matched its own parent's
  command line, reporting a survivor that was the question itself.

### Decisions (SS1–SS45)

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
| **SS25** | **An interactive session does not block the user's prompt,** and dies at an IDLE timeout as well as the wall-clock cap. SS2's block exists because the agent is waiting on work and cannot act until it lands; an idle REPL is not work, and a block on one would stop the user typing for as long as the agent kept a shell open. No amendment to SS2 is needed: it already names background and monitor |
| **SS26** | **Every send is bounded,** and returns on whichever of FOUR comes first: the shell is READY again (SS34's sentinel), a line matched an RE2 pattern the call supplied, output went quiet for an idle interval, or the wall bound expired. The result NAMES which one ended it, because they mean different things -- and `ready` ends a wait for a pattern that is never coming, which the three-bound version would have held to the bound |
| **SS27** | **ANSI sequences are stripped in `core/`, with no terminal-emulator dependency.** Stripping precedes the buffer and every match, and carries a partial sequence across reads: measured, a 128 KB burst arrived in 74 chunks at arbitrary boundaries, so a sequence split across two is the ordinary case and not the corner. Full-screen programs that paint with cursor motion are not supported, and the tool description says so rather than leaving it to be discovered. There is no echo-removal clause: SS33 turns the echo off, so there is none to remove |
| **SS28** | **Container route only.** An open with no runtime is refused with the reason. A pipe-only session with no pty behaves differently in ways the model cannot see -- no prompt, block-buffered output, `isatty` taking another path -- which is the drift class §46 (EP6) and #157 are about. WSL joins in slice 3, where it was measured working |
| **SS29** | **The open call's approval covers its inputs,** and its prompt says so in words -- except that an input naming a protected path segment still asks, reusing `_command_touches_protected`. This is the existing policy shape: that check already sits above both the fallback opt-in and the `contained` opt-in in `_shell_approval_check` |
| **SS30** | **A control send (interrupt, EOF) needs no approval,** above the mode check and so ungated under `always` too. SS11 already ships `shell_kill` ungated -- destroying the whole session needs no human -- so interrupting one command inside it cannot need one, and it is the agent's only escape from a wedge. Measured: `\x03` frees both a `sleep 60` and a `cat` holding stdin, in about 48 ms |
| **SS31** | **An interactive session's end never wakes the agent on its own;** it is held and delivered with the user's next message, the route SS19 already built. With no prompt block (SS25), a wake would be a turn starting under the user's cursor |
| **SS32** | **Input text is never classified.** `cd`, aliases and a REPL mean the text is not an argv, so `classify_command` is not asked: the profile is synthesized `runs_code=True, writes=True, measured=True` and the session inherits the tier its OPEN call was approved at. Network is fixed when the session opens (NW4) |
| **SS33** | **The pty echoes nothing, and is sized** -- `stty -echo rows 50 cols 1000` with bash's line editing off. Measured (batch 100): the pty is 0x0, so readline has no width and a 200-character command echoes back as `\r<xxxx`, its horizontal-scroll marker. The plan's rule (strip the echo only when the output starts with exactly what was sent) correctly declines to touch that, which would have delivered the marker to the agent as output. Turning the echo off makes the problem not exist rather than parsing it back out, at any length; the sizing is belt and braces for a program that turns echo back on |
| **SS34** | **The prompt is a token this process minted,** so "the shell is ready for input" is a fact rather than an inference from silence. Measured: a 3-second command returns after 0.4 s of quiet with only its echo back, so quiet cannot be told from still-running; with a sentinel `PS1`, a finished command's output ends with it, a running one's does not, and a half-typed command sitting at `PS2` shows as quiet-but-NOT-ready, which is a wedge the agent must clear. It counts only at the very END of the buffer, so a command that prints the token is not a false ready. ITS LIMIT, measured and stated rather than papered over: with the echo off there is nothing on the stream when a command STARTS, so a prompt still standing from the last command is indistinguishable from an idle shell. A send therefore waits for the NEXT prompt and requires it to settle, which is right in every case but one -- a line typed while another command is still running is queued by the terminal, and if what it queues behind is silent the prompt between them settles and is reported as the answer. Separating those needs a terminal emulator, which SS27 declines to be, so the tool tells the agent not to send until it has `ready` |
| **SS35** | **The user enables WSL and the agent chooses it per call.** `allow_wsl_backend` joins the frozen posture, shipped `false`; when it is on, `shell`, `shell_background` and `shell_monitor` take `backend: "container" | "wsl"`. Two different questions with two different owners: whether this machine may run agent commands outside a container is the user's, and which command needs a Linux userland is the agent's. A process-wide switch would answer both at once and give up the container for every command in order to serve one |
| **SS36** | **The distro comes from config, never from the agent.** `wsl_distro: ''` means the first name `wsl.exe -l -q` lists, which is WSL's own default; a name that is not installed is refused with the installed names. A distro is a fact about the machine, not about a command -- and the list on the machine this was built on contains `docker-desktop`, which is Docker's own internal distro. Validated because `wsl.exe -d NoSuchDistro` exits `4294967295` and writes its complaint to STDOUT in UTF-16 (measured), so an unvalidated name fails in a way that reads as the command failing rather than the configuration being wrong |
| **SS37** | **A call that asked for WSL and cannot have it is REFUSED, never served by another backend,** and the refusal says which of the two it is -- the backend is turned off, or no distro answered. SS28's argument, on a second backend: the gap between what the approval prompt named and what actually ran is the drift class §46 (EP6) and #157 are about. `_unavailable_message` therefore answers a WSL call about WSL and never offers it the container's install instructions, which is the shape that makes a model retry the same call |
| **SS38** | **Everything the WSL route runs, runs in WSL -- `HOST_READ` included,** and the branch sits ABOVE `HOST_READ` in `_route` rather than below it. Measured: `cat /etc/passwd` asked for on WSL classifies HOST_READ, and the old ladder would have run it through `_run_inert` on WINDOWS against `C:\etc\passwd`, with whichever `cat` is first on the user's PATH -- which on the machine this was built on is a GnuWin32 one, so it would have run rather than failed. The tier keeps its whole meaning; what it must not do is pick a machine the call did not ask for |
| **SS39** | **The classifier learns nothing about WSL path forms.** Measured over a generated corpus resolved both ways -- `_within` on Windows against `realpath -m` in the distro -- there is no token that reads as inside the workspace on Windows and outside it in Linux. Every disagreement is the other way (`/mnt/c/<workspace>/notes.md`, `C:/Windows/win.ini`, `\etc\passwd` all read as outside on Windows and inside or outside consistently in Linux), which costs an approval prompt and never skips one. A second path resolver in the one module whose soundness rests on refusing to parse (G2) is what #157 came from. The cost is stated in the tool description, with "use paths relative to the workspace" |
| **SS40** | **A protected segment on the WSL route is REFUSED, not asked.** In the container `.venastine/` is mounted `:ro`, so a write fails with EROFS and a read is a documented risk a user may accept; on WSL there is no mount at all, a write SUCCEEDS against this harness's own authority files, and D17's hash notices only at the next launch. So the shell's usual answer is the wrong one here and the file tools' answer is the right one. The token check is exactly sound where it bites -- an INERT command carries no metacharacters, so `command.split()` IS the argv -- and a non-inert command meets a human on top. The gate returns "do not ask" for it, beside the `UNAVAILABLE` branch and for that branch's reason: a call that will be refused must not spend a human decision first |
| **SS41** | **WSL is UNCONTAINED (SS1, unchanged) and an enabled WSL backend is on the badge.** `unsafe_reasons()` gains its own pair, so the banner, the launch WARNING and the sidebar say it from one source (UN3). Its own pair rather than folded into the fallback's, because they are different weakenings: the fallback is the host when there is no container, and this is a Linux userland the agent can ask for while a container is running. Reported when it is ENABLED, not when it is used -- a badge describes what this process may do, and a user who reads it after the fact has read it too late. The approval notice says the same thing in the place the decision is actually made |
| **SS42** | **The WSL process gets the scrubbed environment, and `WSLENV` is not in it.** Measured, and it is a real leak rather than a theoretical one: Windows environment variables do NOT cross into the distro by themselves, but `WSLENV` names the ones that do and is itself inherited -- with `WSLENV=VEN_PROBE_SECRET` set in the parent, the unscrubbed run printed the secret inside the distro and the scrubbed one printed nothing. `_SAFE_ENV_KEYS` does not name `WSLENV`, so passing `env=_scrubbed_env()` is the whole fix, and `wsl.exe` still works without what it takes away |
| **SS43** | **INERT runs as an argv through `wsl.exe -e`; everything else through `bash --norc --noprofile -c`.** The argv half is EP5's rule on a new route and SS39 RESTS on it: `execve` does not expand `~` or `$HOME`, and a shell between the classifier and the executor would be the third tokeniser in the gap #157 came through twice. The no-dotfiles half keeps the container's property that what runs does not depend on a user's `.bashrc` -- measured: the shell reports NOT_LOGIN, zero aliases and no `BASH_ENV`. Measured alongside: seven adversarial argv strings (embedded quotes, backslashes, `%PATH%`, a trailing backslash, backticks, `$(id)`) round-trip byte-identically through `list2cmdline` and `wsl.exe`, so the quoting-bypass class of batches 37 and 39 does not reappear on this route |
| **SS44** | **WSL gets no resource limits, and no working-directory guesswork, and both are stated rather than papered over.** There is no `--memory`, `--cpus` or `--pids-limit` equivalent: they belong to the whole WSL VM, and a `ulimit` inside the shell is removable by the command it is meant to bound -- a control that reads as safety without being one, which is the failure SECURITY.md already names for the insecure fallback. The workspace is translated by `wslpath` and the POSIX path is what `--cd` is given, because `--cd` with an untranslatable Windows path does not fail: measured, `--cd \\127.0.0.1\C$` exits 0, warns on stderr and runs the command in the user's Linux HOME. Kill needs no process-group machinery -- measured, ending the Windows process ended both a foreground child and a backgrounded grandchild |
| **SS45** | **`_within` answers False for a link this platform cannot follow.** The hole, found by measuring rather than by reading: a symlink created inside the workspace FROM the distro is stored as an LX reparse point (`0xa000001d`) that Windows does not understand -- `os.path.exists` False, `os.path.islink` False, and `os.path.realpath` returns the path UNCHANGED rather than following it or raising. So `_within` answered True for a name that reads `/etc/passwd` in the distro: `cat escape` classified INERT, was AUTO-APPROVED under `tiered`, and returned the host's real password file. No analysis of the token's TEXT can see it, because the token is `escape`. The rule is the platform's own admission rather than a list of tags -- a component that `lexists` and does not `exist` is one whose lstat succeeded and whose stat did not, which for a path means exactly a link that could not be followed. Deliberately NOT "is a reparse point": a OneDrive placeholder is one, and its path means what it says. It applies on EVERY route, not only WSL, because `_within` answers one question and a predicate that answered it per backend would be the drift it exists to prevent |

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
  `stdin=DEVNULL` -- amended in batch 100: an INTERACTIVE session's stdin is a PIPE. N1's reason
  survives the constant, because it is about a child INHERITING THE CONSOLE, and a pipe that only
  the harness writes to is not a second reader of the terminal.
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
- **Slice 2's numbers** (batch 100), all measured rather than picked: a 600 s idle timeout; a 0.3 s
  quiet interval, against a measured 50 ms round trip; a `wait_s` of 10 s by default and 120 s at most,
  so a send cannot outlive the turn it is in; the sentinel is `VEN<8 hex>>`, minted per session. The
  wrapper is `script -qfec` inside the container rather than `docker run -t`: the pty is then made in
  the container, so the client never needs a real terminal and docker never mangles the stream.
- **An interactive session counts toward the 4-live cap but not toward the prompt block** (batch 100).
  They are two questions asked of one list: the cap is about resources, which an idle shell does hold,
  and the block is about whether the agent is waiting on work, which SS25 says it is not. `_live_locked`
  takes the kinds to count, and the two callers pass different ones.
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
2. **Interactive sessions** (SS25-SS34) -- BUILT, batch 100. A shell held open inside the
   container with a pty, input sent to it and the reply read back, its state surviving across the
   agent's own turns. It is the first session kind whose stdin is not `DEVNULL`, the first that does
   not block the user's prompt, and the first whose end is held rather than woken for.
   Two things the plan assumed were wrong, and the probe found both before any code was written:
   the pty is 0x0 so a long command's echo comes back mangled (SS33), and a quiet return cannot be
   told from a command still running (SS34). Neither would have been caught by the tests, because
   both were in what the tests would have been written against.
3. **WSL** (SS35-SS45) -- BUILT, batch 101. A command, a background
   session or a monitor in the user's own Linux userland, asked for per
   call and refused unless the user turned the backend on. It is the
   first backend with NO isolation of any kind -- SS1 always said so,
   and building it is what made the sentence operational: uncontained
   for every tier, on the badge whenever it is enabled, and a protected
   segment refused outright rather than asked about, because the
   read-only mount that made "ask" tolerable does not exist here.
   Measuring first found a hole that was ALREADY OPEN and had nothing to
   do with WSL being a backend: a symlink the distro writes into the
   workspace is invisible to Windows, so `_within` vouched for a name
   that reads `/etc/passwd` and the command was auto-approved (SS45).
   `shell_interactive` does NOT reach WSL yet: its stream shape there is
   unmeasured, and batch 100 is the evidence that measuring it is a
   batch rather than a step in one.
   Next: slice 4, SSH.
4. **SSH and the secrets it needs.**
5. **Sudo** (SS3).

Then background subagents as their own section (SS10).

### Gap register for the later slices

- **Interactive:** a hung read would wedge the turn, so every send needs a bounded wait. Input is not a
  command -- aliases, `cd` and a REPL mean the text is not the argv -- so every input is `runs_code`, and
  network is fixed when the session opens. The per-input gate is per tool call, so no deferred approval
  exists. The four that were open are CLOSED in batch 100: lifetime is an idle timeout beside the
  wall-clock cap (SS25); ANSI is stripped in `core/ansi.py` with no emulator dependency (SS27); Ctrl+C
  needs no approval (SS30); the live view is slice 1's, unchanged -- read-only, kill the only control,
  because a user typing into the agent's container desynchronizes the agent from a session it is still
  reasoning about.
- **WSL: CLOSED in batch 101.** SECURITY.md's claim is narrowed to the container route and the
  exception stated; `.venastine/` is REFUSED on this route rather than asked about (SS40), which is
  more than the register asked for and less than D17's hash would have had to catch; `ran_on` gains
  `wsl` and one `WHERE_RAN` mapping now serves both prose surfaces; INERT runs through `wsl.exe -e`
  as an argv (SS43); the workspace goes through `wslpath` and never through `--cd`'s own translation,
  which relocates silently (SS44); distro selection is a config key validated against the installed
  list (SS36); and the backend joins the frozen posture as `allow_wsl_backend` (SS35), on the badge
  (SS41). Still open, and recorded rather than decided: WSL1, whose filesystem and process semantics
  differ and which nothing here has measured; and an interactive session on WSL, which waits for the
  batch that measures its stream.
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
