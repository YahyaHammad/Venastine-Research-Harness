# Roadmap v3 — Everything after §48

**Purpose of this document:** the specification and locked decision record for work begun after
`ROADMAP_v2.md` §48. It opens with §49, the shell-sessions work: background and monitor sessions that
outlive a turn and wake the agent, interactive sessions, a second container runtime, WSL and SSH
backends, and harness-held secrets.

**The title was narrower than the document for two sections before it was widened** (batch 122).
It read "Command execution: sessions, runtimes and backends", which §49 and §50 are and §51 and
§52 are not -- those are network-tool paging and redaction ordering. §53-§59 are further still:
a network budget, the prompt box, the transcript, the shell classifier, model selection, the system
prompt and two TUI affordances. The alternative was a fourth document, and it was declined for
SS21's own reason in reverse: a `§N` already names one section across three documents, and a fourth
buys nothing a reader of the index does not already get. v3 is "after §48", and the title says so.

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

- **§49. Shell sessions, the container runtime, and where a command can run** — **(COMPLETE: slice 0, Podman, BUILT in batch 93; slice 1 COMPLETE -- its foundations in batch 94, its five tools, the sleeping subagent and the CLI wait loop in batch 95, the TUI's behaviour half in batch 97 and its display half in batch 98; slice 2, interactive sessions, BUILT in batch 100; slice 3, WSL, BUILT in batch 101; slice 4, SSH, BUILT in batch 102; slice 5a, the secret store, BUILT in batch 103; slice 5b, sudo, BUILT in batch 104)** (the shell was one-shot and blocking, so a test suite could not outlive a turn and nothing could wake the agent; a machine with Podman and no working Docker had no sandbox at all)
- **§50. Declared network for a shell command** — **(BUILT in batch 99)** (the network detector reads command TEXT against a fourteen-word list, so a command it cannot see is auto-approved into `--network none` and fails with no way for the user to have allowed it)
- **§51. Paging for network tool results** — **(BUILT in batch 105)** (`fetch_url` and `arxiv_search` truncate with no offset, so the agent cannot reach past the first page without shelling out to a raw HTTP request)
- **§52. Redact before the cut, and a bound on the held body** — **(BUILT in batch 106)** (§51 cut the page out of the body before the redactor saw it, so a credential straddling character 5,000 reached the corpus whole; and the body it began holding was unredacted, in a cache whose entry count nothing bounded)
- **§53. A rolling bound on what the harness pulls and pushes** — **(NOT BUILT: requested batch 122, specified, not designed)** (nothing anywhere counts a byte over the wire, so a user on a metered connection has no ceiling and a tool that fetches something enormous is bounded only by one tool's own per-request cap)
- **§54. `@` names a file, and the tree is read once** — **(NOT BUILT: requested batch 122, specified, not designed)** (naming a file in the prompt means typing its path exactly, from memory, with no completion and no way to say "this folder")
- **§55. The whole command, and what it printed** — **(NOT BUILT: requested batch 122, specified, not designed)** (a shell call shows a 140-character digest and its output is never rendered at all, so what the agent ran and what came back are both off screen)
- **§56. A decision model as the shell classifier** — **(NOT BUILT: requested batch 122, specified, not designed)** (the gate decides from a word list and a token scan, so every judgement it cannot express as a rule costs an approval prompt, and the rules cannot be tuned per user)
- **§57. One model for the subagents** — **(NOT BUILT: requested batch 122, specified, not designed)** (an agent definition can pin a model and nothing can set one for the subagents as a class, so a cheap child runs on the parent's expensive model)
- **§58. Who the agent is when you ask it** — **(NOT BUILT: requested batch 122, specified, not designed)** (the chat system prompt is "You are a helpful assistant.", so the model knows nothing about the harness it is running in)
- **§59. Pick it instead of typing it** — **(NOT BUILT: requested batch 122, specified, not designed)** (removing a memory means reading a uuid out of the transcript and typing it back, one at a time; switching agent means typing an exact name)

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


### What was measured before slice 4 was designed (2026-09-23)

Against a real `sshd` -- `openssh-server` in the WSL Ubuntu distro on port 2222, stood up for this
(owner-authorised), with a key-auth identity and a deliberately encrypted one. Every line below was
run. Three of them changed the argv this slice builds, and one of them changed a decision.

- **`ssh` has no `execve` path, and the quoting still holds.** Ten adversarial tokens -- embedded
  quotes, backslashes, a trailing backslash, `%PATH%`, backticks, `$(id)`, `; | &`, a newline, glob
  characters -- round-trip BYTE-IDENTICALLY through `list2cmdline` -> `ssh` -> the remote login shell
  -> `bash -c`, with one `shlex.quote` boundary. Two tokenisers this process does not own sit between
  the ends, which is why it was measured rather than argued.
- **Killing the local `ssh` does NOT kill the remote command.** A `sleep 400` outlived its client
  every time: with no pty there is no hangup to deliver. `-tt` does end it -- and merges the remote's
  stderr into stdout and turns every `\n` into `\r\n`, measured, which would silently change what
  every command and every session reports. So: no pty, and the in-guest `timeout -k` is the bound
  (it worked with no client alive, rc 124 after 5.3 s).
- **Without `BatchMode=yes`, an encrypted key with no agent HANGS** -- 20 s and still waiting, because
  `ssh` is trying to prompt for a passphrase on a terminal this process does not own. With it, the
  same call fails in 0.12 s. This is the measurement the whole key-only decision rests on.
- **`_scrubbed_env()` alone breaks Windows OpenSSH: rc 255 with an EMPTY stderr.** SS36's
  no-reason-at-all shape, and it would have been diagnosed as an unreachable host. Bisected over
  twelve candidate variables, `PROGRAMDATA` is the only one that changes the answer.
- **`LogLevel=ERROR` swallows the reason a connection failed.** A refused port answered rc 255 with
  nothing on stderr; at the default level it says so. The default is silent on success -- only the
  remote's own stderr arrives -- so it is what a refusal is built from.
- **Nothing local crosses into the remote by itself.** The remote environment is twelve variables and
  every one is the server's; not even `LANG` crossed, because the client's default `SendEnv` is empty.
  Unlike WSL's `WSLENV`, this is not a leak that scrubbing closes -- but what a client sends is
  `SendEnv`'s business, and `SendEnv` lives in a config file this harness CANNOT redirect: measured,
  Windows OpenSSH resolves the user's profile from the login token, so setting `HOME` and
  `USERPROFILE` does not move it. `-F none` closes it from the other side.
- **Two `ssh.exe` on the machine this was built on**, and PATH picks the one the OS did not ship:
  `shutil.which("ssh")` answers a git-for-Windows MSYS2 `OpenSSH_10.3p1`, beside Windows'
  own `OpenSSH_for_Windows_9.5p2`. They agree on every happy path and DISAGREE ON FAILURES, which is
  the half a refusal is made of: a refused connection is `ssh: connect to host ... Connection
  refused` from one and `banner exchange: Connection to UNKNOWN port -1: Connection refused` from the
  other. SS38's GnuWin32 `cat` on a third route.
- **Every authentication failure says the same thing.** An encrypted key with no agent, an unknown
  user, and no key offered at all all answer `Permission denied (publickey)`. So the reason a model
  is given has to be composed here; quoting `ssh` would say one thing about three different fixes.
- **A `cd` guard is loud both ways.** A missing remote directory and an unreadable one (`/root`) both
  exit with the chosen code and print the marker; a present one runs normally.
- **The remote answers what it is.** `uname -s`, `command -v bash timeout` -- bounded, and the basis
  for refusing a remote that is not POSIX rather than sending it POSIX shell lines.

### What was measured before slice 5a was written (batch 103)

Nothing below was designed from documentation. Slices 2, 3 and 4 each had an assumption killed by a
probe before a test was written against it, and every time the assumption was inside what the tests
would have asserted.

- **`hashlib.scrypt` refuses any parameter worth using under its DEFAULT `maxmem`** --
  `ValueError: [digital envelope routines] memory limit exceeded` for N=2**15, 2**16 and 2**17, and
  only N=2**14 passes. So `maxmem` is passed explicitly and derived from the parameters. Without
  that measurement this module would have raised on the first unlock on every machine, in a message
  that reads as a bad parameter rather than a library default -- which is how it gets "fixed" by
  lowering N.
- **The cost, here:** N=2**15 r=8 p=1 is 142 ms, N=2**14 is 73 ms, N=2**16 is 286 ms. The shipped
  parameter is a measurement and it lives in the envelope, so raising it later does not strand a file.
- **Zeroing a `bytearray` really clears the bytes** -- read back through `ctypes.string_at` at the
  same address, before and after. **And `bytes(buf)` and `AESGCM.decrypt` both return IMMUTABLE
  copies that nothing can zero**, which is why SS60's claim is "shortens a window" rather than
  "closes one".
- **`InvalidTag` carries no message at all.** A wrong passphrase, a flipped tag bit and an edited
  KDF parameter are indistinguishable from the exception -- which is the behaviour the store wants,
  and is why `WrongPassphrase` says nothing about which it was.
- **Forced askpass WORKS on both `ssh` binaries on this machine** -- Windows OpenSSH 9.5p2 and
  Git-for-Windows OpenSSH 10.3p1 -- through a `.cmd` (or `.bat`) shim, with the prompt arriving as
  `argv[1]`.
- **The key-passphrase prompt is TRUNCATED mid-path:**
  `Enter passphrase for key 'C:\Users\...\19552cfb-c47c-4e1a-b': `. Matching it against the
  identity file's path would have failed for any path longer than about sixty characters, which is
  most of them. The prefix is what is dependable.
- **The password prompt has TWO forms, one per authentication method:**
  `user@host's password: ` (`password`) and `(user@host) Password: ` (`keyboard-interactive`) -- and
  **a server offering both serves the second**, so a matcher knowing only `password` would refuse a
  real server's ask.
- **`NumberOfPasswordPrompts=1` does not mean one ask.** A wrong password produced TWO askpass
  invocations, because the two methods each get their own budget. This killed the one-shot listener
  design (SS64) and is the reason a refused connection is quarantined rather than retried (SS63).
- **Stacked `AuthenticationMethods publickey,password` works** -- two invocations, the key
  passphrase then the account password, rc 0, on both binaries. That is the owner's case.
- **`BatchMode=no` with no askpass HANGS the Windows build for 20 s+**, and does not hang the Git
  build (3.1 s, refused). SS47's measurement reproduced, and the reason the anti-hang guarantee may
  not rest on "ssh will give up".
- **A CRLF private key is refused by the MSYS2 `ssh` and accepted by the Windows one**, with
  `error in libcrypto: unsupported` -- a message naming neither the file format nor the line endings.
  Found by writing the probe's key files from PowerShell, which was this batch's own mistake and is
  recorded because the message sends a reader looking at the wrong thing entirely.
- **The WSL gate already auto-approves everything under `AUTO_APPROVE_SANDBOX_FALLBACK`** --
  measured against the real gate: `rm -rf /home` and `sudo apt-get install` both answer `asks=False`
  on `backend: "wsl"`. Pre-existing, not slice 5's doing, and the reason SS65 puts the root step
  above both opt-ins.

### What was measured before slice 5b was written (batch 104)

The probe killed one recorded reason outright and changed the design. Everything below was run, not
read.

- **Ubuntu ships `sudo-rs 0.2.13`, not classic sudo** -- a different implementation of the same
  command, and the one the WSL route will actually call on this machine. It carries `-k`, `-S`,
  `-p` and `--`, so the shape survives; nothing about its behaviour transferred for free, and two of
  the measurements below differ between it and classic `Sudo 1.9.17p2` in the kali distro.
- **`sudo -k -S` consumes EXACTLY one line**, so the password and the command's own input can share
  one pipe. Fed `"<pw>\nPAYLOAD\n"`, a root `cat` printed `PAYLOAD` and nothing else. This is what
  makes stdin a viable transport at all.
- **THE FINDING THAT CHANGED THE DESIGN: `-k` does NOT stop the password reaching the command.**
  SS66 said it did. `sudo -S` reads stdin only when it has to AUTHENTICATE; under a NOPASSWD rule it
  reads nothing, and the password is then the command's own standard input. Measured on **both**
  implementations, **with `-k` and without it**: the root `cat` printed the password. The warm-ticket
  trap SS66 actually named could not be reproduced on either -- so the recorded reason was wrong
  twice over, and the control is the explicit `exec 0</dev/null;` guard (SS72), not the flag.
- **The container is already root and has no `sudo`** -- `docker run --rm python:3.13-slim id` answers
  `uid=0(root)`, and `command -v sudo` answers nothing. SS66's refusal rested on this and it was an
  assertion until now.
- **A WSL command inherits the harness's own stdin today.** `_run_wsl` passed no `stdin=` at all;
  driven with this process's stdin replaced by a pipe, the command in the distro read the string out
  of it. That is §29 N1's second reader, on a route written before the rule (SS70).
- **Empty `-p ""` survives `wsl.exe`'s re-parse.** `list2cmdline` writes `-p ""` and the distro sees an
  empty prompt; an empty argv element across that boundary is exactly the class that breaks silently.
- **No `requiretty`, and sudo over a non-tty SSH connection works** -- rc 0, uid 0, with the password
  on `ssh`'s stdin. The `cd --` guard still fires first and still answers 125 plus the marker for a
  missing remote workspace, so nothing is elevated before the workspace is known.
- **`sudo` preserves the working directory**, so `--cd` and the remote `cd --` keep their meaning.
- **`timeout` inside `sudo` kills a root child** (rc 124 at 3 s, nothing left behind), on both routes.
  Outside works too; inside is the order that does not depend on sudo forwarding a signal.
- **The password is in no process table, no `/proc/*/cmdline` and no environment** -- checked on the
  distro and on the remote. Two earlier readings said otherwise and **both were the probe's own
  fault**: the first `grep`'s argv carried the needle, and the second matched `SUDO_COMMAND`, which
  holds the COMMAND TEXT. A measurement whose needle is in its own haystack measures the probe.
- **`-p ''` leaves a bare `\n` on stderr with sudo-rs and nothing at all with classic sudo**, so a
  rooted command's stderr gains one newline on Ubuntu.
- **A wrong password: rc 1**, one to two attempts against a closing pipe, then
  `sudo: Authentication required but not attempted`. `passwd_tries` is 3 by default, which is what
  SS71's quarantine is sized against. Neither distro has `pam_faillock` or `pam_tally2` in its auth
  stack, so the lockout risk is a property of the REMOTE and not of this machine.

### Decisions (SS1–SS72)

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
| **SS46** | **The user enables SSH and configures the one host; the agent asks for it per call.** `allow_ssh_backend` joins the frozen posture, shipped `false`, and `backend` widens to `"container" | "wsl" | "ssh"`. The host is flat scalars -- `ssh_host`, `ssh_user`, `ssh_port`, `ssh_identity_file`, `ssh_remote_workspace`, `ssh_host_key`, `ssh_binary` -- every one a `HARNESS_AUTHORITY_KEYS` entry. SS35's split, unchanged: which machine this harness may reach is the user's question, which command needs it is the agent's. A named map of hosts is a later addition this shape does not block |
| **SS47** | **Authentication is a key file or a running `ssh-agent`, and the harness holds no secret.** `BatchMode=yes` and `PreferredAuthentications=publickey`, so `ssh` can never prompt on a terminal this process does not own -- an encrypted key with no agent FAILS and the refusal names `ssh-add`. Measured, and this is why it is a decision rather than a default: without `BatchMode` the same call hangs for as long as anyone waits. Not holding the secret is better than holding it safely, and the user already has an agent that does. SS3's secret subsystem arrives in slice 5 with sudo, its second consumer, which is also why it is not built here for one |
| **SS48** | **Nothing on the SSH route is auto-approved,** and it is ONE explicit step in the gate rather than a profile tuned to fail `auto_approved`. Two independent reasons, either sufficient. First, `_within` resolves against the LOCAL filesystem: on a remote it answers a true question about the wrong machine, and SS45 -- one slice old -- is what a path check that cannot resolve actually costs, which is not a loud failure but a confident approval. Second, ssh has no `execve` path (SS51), so the "argv with no shell" property that EP5 builds the INERT tier out of does not exist here at all. One step rather than a synthesized profile because the two opt-ins above the capability rule (`auto_approve_fallback` and `contained`) each answer BEFORE it, so a profile would have had to be tuned to lose three tests instead of one. `never` still means never (SS1): this widens no mode |
| **SS49** | **The remote's host key is PINNED in config, and an unpinned or changed key is refused.** `ssh_host_key` carries the `ssh-keyscan` line itself rather than a fingerprint, because a fingerprint is a hash and `known_hosts` needs the key; the `#` banner `ssh-keyscan` prints above it is ignored, so pasting the whole output works. The harness writes it to a file it owns and passes `UserKnownHostsFile` plus `StrictHostKeyChecking=yes`, never touching `~/.ssh/known_hosts` -- its trust set is exactly what the user pinned. No trust prompt and no trust store: the user is the enumerator (SS35), and a first-use prompt is a question a headless run has nobody to answer |
| **SS50** | **The remote is POSIX, disclosed rather than assumed.** The probe asks it what it is -- `uname -s`, `command -v bash timeout` -- once per process and bounded, and a remote that does not answer as POSIX is REFUSED rather than sent POSIX shell lines. This closes the register's "disclosure of the remote binary": the approval notice and the result name the host, so "it ran somewhere" is never the record. A **Windows OpenSSH remote is deferred and recorded** (owner, this round): its shell is not `bash`, there is no `timeout`, and its paths are not POSIX, so every line of this slice would be wrong there. It is a separate measurement, and a machine with no WSL to stand a test server up in is why the note is owed rather than the case being quietly assumed to work |
| **SS51** | **One quoting boundary, `shlex.quote`, and there is no argv mode on this route.** `ssh` joins its trailing argv with spaces and the remote LOGIN SHELL re-parses, so EP5's "argv, no shell" property -- which `_docker_argv` and `_wsl_argv` both keep -- cannot exist here. Stated rather than papered over, and it is the second reason for SS48. The command is quoted once and run through `bash --norc --noprofile -c`, keeping the other two routes' property that what runs does not depend on the remote user's dotfiles; `-F none` keeps the same property on the LOCAL side, where `ssh` would otherwise read a per-user config carrying `ProxyCommand`, `LocalCommand` or `SendEnv` from a path this harness cannot redirect. Verified with SS43's own corpus on this route, because a tokeniser nobody owns is what #157 was |
| **SS52** | **The remote working directory is REQUIRED and its failure is loud.** `ssh` has no `--cd`; the command is `cd -- <quoted> || { marker; exit 125; }` inside the one quoted string, and BOTH halves are checked -- 125 alone is inside `timeout`'s vocabulary (124/125/126/127) and a marker alone is a string any command may print. `ssh_remote_workspace` empty REFUSES rather than defaulting to the login home: SS44's `--cd` measurement is the precedent, and here it is worse, because a command that silently ran in the wrong place ran it on a filesystem this process cannot look at afterwards |
| **SS53** | **Bounds are what SSH actually offers, and what it does not offer is stated.** `ConnectTimeout`, and `ServerAliveInterval`/`ServerAliveCountMax` so a dead network ends a session rather than hanging it. The wall clock is the in-guest `timeout -k` (SS50 verifies `timeout` exists). No memory, CPU or pid limit -- SS44's rule on a third route, and here not even a `ulimit` to be theatre about, since the remote's resources are the remote's. **A kill does not reach the remote command, and that is measured and disclosed rather than fixed with a pty:** ending the local `ssh` leaves the far side running until the backstop fires, and `-tt` would end it at the cost of merging stderr into stdout and CRLF-ing every line. A process-group kill on the remote needs a second connection and the remote pid, and stays an OPEN item in the register below, where it already was |
| **SS54** | **`requires_network` is ignored on this route, and `.venastine/` is refused.** The network belongs to the remote and no `--network none` reaches it, so the field is a fiction here and the tool description says so instead of implying a control. The protected-segment refusal is SS40's answer reached by a different road: WSL is refused because it demonstrably CAN write this harness's own authority files, and SSH because nothing here can demonstrate that it cannot -- `ssh localhost` is this machine, a remote that mounts this one looks identical from here, and an unanswerable question is not a yes |
| **SS55** | **SSH is UNCONTAINED (SS1, unchanged) and an enabled SSH backend is on the badge,** with its own `unsafe_reasons()` pair for SS41's reason. Three weakenings now, and they are different in kind, not degree: the fallback is the host when there is no container, WSL is a Linux userland beside a running one, and this is a machine that is not this machine at all. The pair names what that means -- no isolation, no limits, credentials this harness did not issue -- and the one trap a model actually walks into: **`write` saves LOCALLY while the shell runs remotely**, so a file just written is not there |

| **SS56** | **One harness-held secret store, encrypted at rest under a master passphrase.** scrypt -> AES-256-GCM, at `~/.config/venastine/secrets.json`, written 0600 through `credentials.py`'s existing `os.open`-with-mode writer. **The property claimed, precisely:** the file is useless without the passphrase, so a shell the AGENT asks for -- on WSL, on SSH, on the host fallback -- reads ciphertext and nothing else. **Not claimed:** any defence against arbitrary in-process Python, which `security/posture.py` already concedes can rebind anything here; against a keylogger; or against a crash dump taken while the store is open. An OS keystore (DPAPI, libsecret, Keychain) was rejected for exactly the property that makes it convenient -- it decrypts for any process running as this user, and the agent's shell is a process running as this user |
| **SS57** | **`cryptography` becomes a declared dependency,** and this is bookkeeping rather than an installation: measured, it is already in the hard closure via `google-genai` -> `google-auth>=2.14.1` -> `cryptography>=38.0.3`, with no extra marker. What the declaration buys is that a dependency this project's security rests on is pinned by this project rather than by whatever `google-auth` resolves to. It adds one `THIRD_PARTY_NOTICES.md` row (Apache-2.0 OR BSD-3-Clause, permissive, so the no-copyleft claim holds) and a SECOND entry in the paragraph that said `google-re2` was the only dependency shipping compiled native code. A floor and a ceiling rather than an exact pin, like `rich`: an exact pin would fight `google-auth`'s own floor. Rolling an AEAD by hand is refused -- the stdlib has `scrypt` and no cipher, and this repository ships a `cryptography-verification` skill whose whole subject is not doing that |
| **SS58** | **A secret's NAME says which machine it unlocks.** `ssh.<host>.key_passphrase`, `ssh.<host>.login`, `ssh.<host>.sudo`, `wsl.<distro>.sudo`. Separate entries rather than one value per host, because they are consumed by different processes at different moments -- `ssh`'s own authentication, versus `sudo -S` on the far side's stdin -- and either can rotate without the other. `/secrets set` OFFERS to write one value to the paired name, so a stacked setup costs a keypress instead of a retyped password, and rotation stays a visible edit rather than one value silently breaking two things with one diagnosis. The name is built from `ssh_host`, not from the target, so editing `ssh_user` does not orphan the entry the user typed |
| **SS59** | **Asking for a secret is an AC1 request kind, not a sixth channel.** `SECRET` joins the five in `core/interaction.py`, with `SAFE_DEFAULTS[SECRET] = None`. Its own kind rather than a flag on `QUESTION`, because the KIND is what tells a shell how to RENDER and here the rendering IS the substance: a shell that ignored a flag would show the passphrase in clear while every plumbing test passed. None rather than `""`, because the empty string is what a dismissed modal, a torn-down screen and a shell with no branch all produce -- and an empty password handed to `sudo` or `ssh` is an authentication ATTEMPT made on nobody's behalf, which on a remote account is a step toward a lockout. BOTH shells render it (AGENTS.md's rule, audit #7): the TUI gets the first masked modal in this project, the CLI gets a masked read on its ONE stdin reader -- `getpass` is refused because it reads stdin itself, which is the second reader audit #100 exists to prevent |
| **SS60** | **Unlocked plaintext lives in process memory and nowhere else, and everything that is not "still working normally" locks.** The triggers: `/secrets lock`, quit, `atexit`, an exception escaping any `guarded()` block (BaseException, so a KeyboardInterrupt counts), the askpass listener's bad token / unrecognised prompt / over-budget / connection error / deadline, and the store file changing on disk under a running process. **A crash is a lock because there is nowhere else the plaintext is** -- and that sentence is only true if it is never on disk, never in an environment, never in an argv and never in a log, which are four TESTS rather than four claims. Plaintext is held in `bytearray` and zeroed on lock; **the limit is measured and stated rather than papered over**: `bytes()` and `AESGCM.decrypt` both return IMMUTABLE copies nothing can zero, so this shortens a window rather than closing one, and a crash dump taken while the store is open may still hold what was live. A refused AUTHENTICATION does NOT lock: one typo must not cost the master passphrase, and the attacker such a lock would defend against is not involved -- see SS63's quarantine instead |
| **SS61** | **A live secret VALUE is redacted from tool output unconditionally, whatever `redact_tool_outputs` says.** The master switch governs pattern GUESSES about other people's credentials, and a user who turns it off has chosen to see those raw. This is different in kind: a value the harness itself put into a process, coming back out of one, and handing it to the model is the harness leaking its OWN secret. It sits beside the depth cap, which `redaction_enabled()` already documents as staying fail-closed because it is structure rather than content judgment. Applied in `redact_output_text` -- above the switch, so both its consumers get it -- and again in `logging_setup`'s formatter, which is the fourth sink and the one that KEEPS what it writes. The values never leave `security/secrets.py`: the redactor calls it rather than asking for a list, so exactly one module holds plaintext |
| **SS62** | **A secret has no `__str__`.** It is carried in a wrapper whose `str`, `repr` and `__format__` are all the marker, which is unhashable, unpicklable, uncopyable and not JSON-serialisable. The bytes come out through ONE accessor, `reveal()`, which makes every real use greppable and every accidental one impossible. The cheapest control in the batch and the one preventing the largest class of leak, because every other control assumes the value stays where it was put. `reveal()` is named for what it does rather than called `value` so the call site shows that a copy was just made which `lock()` cannot reach |
| **SS63** | **SS47 is AMENDED, not discarded.** Its property was never `BatchMode` -- it was that **`ssh` can never prompt on a terminal this process does not own**. `BatchMode=yes` guaranteed that by refusing to prompt at all; `SSH_ASKPASS_REQUIRE=force` guarantees it by sending every prompt to a helper that answers immediately and cannot block. **With nothing stored for the host the argv is slice 4's, option for option and in order**, pinned against the literal `84c886b` shipped -- a user on key-and-agent auth is not moved by this slice at all. With something stored, `BatchMode` comes off (it suppresses askpass too, so the two are mutually exclusive), `NumberOfPasswordPrompts=1` bounds the retries within one method, and the preference list is `publickey,keyboard-interactive,password` because a server offering both serves the SECOND. **A refused connection that used stored credentials QUARANTINES them for the run** rather than being retried: measured, one refused call makes TWO authentication attempts, so retrying every command would walk a real account into a lockout at four attempts a call. The refusal names `/secrets set <name>` |
| **SS64** | **The secret reaches the askpass helper over a loopback connection authenticated by a single-use token, never in an environment variable.** `ssh` passes its whole environment to the askpass child, and that environment is readable by any process running as this user -- the property that got an OS keystore rejected for the store itself. So the environment carries a TOKEN and a port; the helper forwards `(token, prompt)`; the harness decides which secret answers and serves it. **The helper holds no secret and no policy** -- it is a pipe with a password on it, which is what makes it safe to leave in a temp directory. **What this buys, stated precisely rather than oversold:** a reader of `ssh`'s environment gets a token rather than a password and must then win a race during one connection, instead of reading a value at rest. Someone who can do that could have read the password at the moment it mattered anyway, so this is a real narrowing and not a boundary. **The listener is NOT one-shot**, and that is the correction the probe forced: a wrong password produces two asks despite `NumberOfPasswordPrompts=1`, because keyboard-interactive and password are separate methods with separate budgets, and a stacked host legitimately asks twice. It bounds the number of serves instead |
| **SS65** | **(slice 5b) A command the harness will run through sudo ALWAYS asks,** above both opt-ins, in every mode except `never` -- the same shape and position as SS48's SSH step. This amends SS3's "approval follows `shell_approval_mode`" for root specifically, and the reason is measured rather than argued: `AUTO_APPROVE_SANDBOX_FALLBACK` already answers for the ENTIRE WSL backend (`rm -rf /home` on WSL asks=False today, because `containment_for` returns UNCONTAINED and the fallback opt-in sits above the capability rule), so a root step below it would be dead code on the backend where root is most reachable. The same measurement corrects the WSL `unsafe_reasons()` pair, whose last sentence is untrue in that configuration |
| **SS66** | **(slice 5b) Sudo runs on WSL and SSH only,** through `sudo -k -S -p '' --` with the password on stdin -- SS3 unchanged, with `-k`. **The reason recorded here was WRONG and is corrected in batch 104:** `-k` does not stop a
password line reaching the command. What it buys is that one approval buys exactly one authentication,
so a ticket warmed by an approved command cannot quietly authorise a later one. The control that stops
the password becoming the command's input is SS72's stdin guard. The container is refused BECAUSE it already runs as root there, so sudo is neither installed nor needed and offering it would be a control that does nothing. The host fallback is refused and RECORDED rather than assumed: on Windows the host shell is PowerShell and `sudo.exe` elevates through a UAC consent dialog that cannot take a password on stdin at all, which is a different mechanism and a separate measurement |
| **SS67** | **The root step fires on the DECLARATION or on a literal escalation token, whichever comes first.** `declared_root(params)` is `declared_network`'s sibling -- only literal `True`, read off the model's raw input before Pydantic, for #157's reason -- and it is OR'd with a raw `command.split()` check for `sudo`, `doas`, `pkexec` and `su`. The OR is the whole point: MEASURED, `sudo apt-get install` on WSL answered `asks=False` under the fallback opt-in, so a step reading only the flag would be dead code exactly where root is most reachable. It is a TOKEN check and never a parser (G2), and it fails in the safe direction -- a command that merely mentions the word costs one prompt, the same trade `_command_touches_protected` already makes. It only ever ADDS a prompt: the harness supplies a password for a DECLARED call and for nothing else, so a spelling that slips past reaches a `sudo` with no password and fails |
| **SS68** | **No stored sudo password means the call is REFUSED, naming the entry.** `wsl.<distro>.sudo` and `ssh.<host>.sudo` (SS58), with the exact `/secrets set` line in the refusal, and read by the gate as "there is nothing to approve" rather than as a prompt (§32 A7). The store's contents are the opt-in for root and there is **no `allow_root_commands` key**: a second switch over one decision is the ratchet G3 removed, and "is there a sudo secret" is greppable in a way a config flag is not. **The cost, recorded rather than hidden:** a host with passwordless sudo cannot use `needs_root` at all, even though it would work |
| **SS69** | **Sudo wraps the SHELL, not the first word.** `sudo -k -S -p '' -- bash --norc --noprofile -c <command>`, so the WHOLE command line runs as root -- pipes, redirects and `&&` included. Prefixing only the first word gives the classic half-root surprise (`sudo echo x > /etc/f` writes as the user) and would make the approval notice a lie about what was approved. Argv mode is dropped on this path and that weakens nothing: argv mode exists so an AUTO-APPROVED inert command has no shell between the classifier and the executor (SS39/SS43), and a root command is never auto-approved |
| **SS70** | **A WSL one-shot gets `DEVNULL`, or a `PIPE` carrying only the password.** `_run_wsl` passed no `stdin=` at all, so the command inherited the harness's console -- measured, a command in the distro read a string written to this process's stdin. That is §29 N1's second reader on a route written before the rule. The rooted call gets a pipe the harness alone writes to, which is the reading of N1 slice 2 already made for an interactive session. The container, inert and host-fallback one-shots have the same gap and are **recorded, not changed**: they are not on this batch's route, and a shipped behaviour change to three of them is not something sudo's needs justify |
| **SS71** | **A refused sudo password is quarantined for the run, like a refused SSH credential.** `_note_ssh_auth_failure`'s shape and its reason: `passwd_tries` defaults to three, and a model that retries a wrong password walks a real account towards `pam_faillock` on the distributions that have it. The harness stops offering the value and says which entry to fix. **It does NOT lock the store** -- one wrong password must not cost the master passphrase, which is the trade SS63 already made |
| **SS72** | **The command behind sudo has its stdin explicitly closed, and THAT is the control.** `exec 0</dev/null; ` in front of the command inside the rooted shell. `sudo -S` reads stdin only when it must authenticate; under NOPASSWD it reads nothing and the password is then the command's own input -- measured on sudo-rs 0.2.13 AND classic Sudo 1.9.17p2, with `-k` and without it, on both the WSL and the SSH route. It costs nothing real: every one of these routes already hands a command DEVNULL, so the guard preserves that contract rather than changing it. It is absent when no root is asked, because a guard that cannot fire still reads as protection (batch 103's lesson, applied forward) |

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
4. **SSH** (SS46-SS55) -- BUILT, batch 102. A command, a background
   session or a monitor on another machine entirely, asked for per call
   and refused unless the user turned the backend on and pinned the
   host's key. It is the first backend where NOTHING runs without asking
   (SS48) -- every other one runs a read-only in-workspace command
   unasked, and the reason this one cannot is that "inside the
   workspace" is a question about a filesystem this process cannot
   resolve. SS45, one slice old, is the evidence for what a path check
   that cannot resolve is worth.
   Measuring first killed one design and corrected three lines: killing
   the local `ssh` does NOT end the remote command, and the pty that
   would fix it merges stderr into stdout and CRLFs every line, so the
   bound is the in-guest `timeout` and the gap is disclosed (SS53);
   without `BatchMode` an encrypted key with no agent hangs forever
   (SS47); and the scrubbed environment alone breaks Windows OpenSSH
   with an EMPTY stderr, which is SS36's shape exactly.
   The harness-held SECRETS are NOT here (owner, batch 102): this slice
   authenticates by key or agent and never holds one, and SS3's secret
   subsystem lands in slice 5, where sudo is its second consumer and it
   gets built once against two.
   `shell_interactive` does NOT reach SSH, and here the reason is
   measured rather than unmeasured: a remote pty is the only way to get
   a prompt back, and it is exactly what destroys the stream.
   Next: slice 5, sudo.
5. **The harness-held secret store** (SS56-SS64) -- **BUILT**, batch 103. `/secrets` holds named
   entries encrypted at rest under a master passphrase the user types once a session; an
   encrypted SSH key and a password-authenticated host both work, and a stacked
   `publickey,password` host works too. SS47 is amended rather than discarded: with nothing
   stored the argv is slice 4's, option for option, and with something stored the anti-hang
   guarantee moves from `BatchMode=yes` to `SSH_ASKPASS_REQUIRE=force`. The secret reaches
   `ssh` over a token-authenticated loopback connection and is never in its environment
   (SS64). Measuring first killed the one-shot listener -- a wrong password asks TWICE,
   because keyboard-interactive and password are separate methods with separate budgets --
   and corrected the prompt matcher, whose key prompt OpenSSH truncates mid-path.
   Next: slice 5b, sudo.
6. **Sudo** (SS3, SS65-SS72), the store's second consumer -- **BUILT**, batch 104. `needs_root` on
   `shell`, `shell_background` and `shell_monitor`; the harness prefixes `sudo -k -S -p '' --` on WSL
   or SSH and writes the stored password to stdin and nowhere else. A human is asked EVERY time, above
   both opt-ins, and the step fires on the command text as well as the declaration -- because the
   measured behaviour was that `sudo apt-get install` on WSL asked nobody. The container is refused
   for already being root (measured: uid 0, no `sudo` in the image) and the host fallback is refused
   and recorded. Measuring first corrected SS66's own stated reason: `-k` does NOT stop the password
   line reaching the command, a NOPASSWD rule does, and the control is SS72's explicit stdin guard --
   measured on two sudo implementations, with the flag and without it.
   §49 is complete.

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
- **SSH: CLOSED in batch 102, except where noted.** `write`-saves-locally is disclosed in the tool
  description, the approval notice and the badge (SS55); network is always on and `requires_network` is
  stated to be a fiction here (SS54). **The register's own plan for "inside the workspace" was NOT
  adopted:** a lexical test against a configured remote directory is exactly the shape SS45 had just
  closed, so nothing is auto-approved at all (SS48), which needs no remote path resolver and is the
  stronger answer. INERT tokens do NOT reach the remote single-quoted as its own case -- there is no
  INERT case here -- but the quoting itself is as the register expected, and measured: one
  `shlex.quote` boundary, ten adversarial tokens byte-identical (SS51). Host keys are PINNED in config
  rather than trusted at a prompt (SS49), which the register asked for and the owner overturned this
  round for a reason the register did not have: a first-use prompt cannot be answered headless.
  `ssh localhost` being the host is why a protected segment is refused (SS54). The connection library
  question is answered by not taking one -- the system `ssh` as argv, no new dependency, and therefore
  no change to `THIRD_PARTY_NOTICES.md`; multiplexing turns out not to matter, since a session holds one
  connection for its life and key/agent auth re-prompts for nothing. The host configuration's shape is
  one host in flat scalars (SS46). Disclosure of the remote binary is SS50.
  **Still open, and recorded rather than decided:** process-group kill on the remote, which needs a
  second connection and the remote pid -- measured, the local kill does not reach it and the in-guest
  `timeout` is what bounds it (SS53); a Windows OpenSSH REMOTE, whose shell, tools and paths are none
  of the things this slice sends (SS50); the harness-held secrets, moved to slice 5 with sudo; and an
  interactive session over SSH, refused on a measured basis rather than an unmeasured one.
- **The secret store: CLOSED in batch 103** (SS56-SS64). Everything the register asked for is
  built: a request kind through `core/interaction.py` (SS59), both shells rendering a masked field,
  redaction by VALUE whatever `redact_tool_outputs` says (SS61), process memory only and cleared at
  quit (SS60). Two things the register did NOT ask for and the owner did: it is **encrypted at
  rest** under a master passphrase (SS56), and **SSH login passwords and key passphrases are in it
  too** (SS63) rather than waiting for sudo, because stacked `publickey,password` auth is a real
  configuration and the two are one security class. **Still open, and recorded rather than decided:**
  migrating the EXISTING plaintext credential stores (`providers.json`, `.env`) behind the same
  passphrase, which is a different decision with a different cost -- it would put an API key behind
  a prompt on every launch; and an idle re-lock timer, declined by the owner in favour of the
  explicit lock plus SS60's triggers.
- **Sudo: CLOSED in batch 104** (SS65-SS72). Everything the register asked for is built, and one thing
  it asked for was wrong: it said `-k` stops a cached ticket passing the password line to the command,
  and that is not what `-k` does. The register's own sentence was carried forward from SS66 and both
  are corrected -- the trigger is a rule that needs no authentication (NOPASSWD), `-k` does not help,
  and SS72's explicit `exec 0</dev/null;` is the control. Separate values for WSL and SSH are SS58's
  naming, unchanged. There is no badge ENTRY for sudo and deliberately so: root is always-ask, so it
  is not a weakening the badge should list, and whether a sudo password is stored is mutable at
  runtime, which UN1 keeps out of the frozen posture. The two backend pairs gained a clause instead.
  **Still open, and recorded rather than decided:** sudo on the host fallback, which on Windows means
  `sudo.exe` and a UAC consent dialog that cannot take a password on stdin -- a different mechanism and
  a separate measurement; and a passwordless-sudo host, which SS68 refuses rather than serves, because
  the stored secret is what turns root on at all.
- **Docker rootless on cgroups v1** ignores limits the same way SS24 describes for Podman, and is not checked.
  Recorded, not decided.

### Rejected

- **A Python `re` pattern engine,** for the GIL measurement above.
- **A `tool_approvals` field for the start tools** (SS16).
- **A third copy of the routing ladder** for sessions: `containment_for` and `run_sandboxed` are already one
  decision written twice (EP6).
- **`tool_call_id` on a wake row.**
- **Treating WSL or an SSH host as contained,** and **auto-filling sudo prompts.**
- **An SSH connection LIBRARY** (batch 102). `paramiko` is LGPL-2.1 and `asyncssh` EPL-2.0, either of
  which would be the first copyleft entry in a `THIRD_PARTY_NOTICES.md` that asserts every dependency
  is permissive, and both put a crypto stack inside this process. The system `ssh` as argv is the same
  discipline `_docker_argv` and `_wsl_argv` already use. What a library would have bought -- one
  persistent connection -- is worth less than it sounds: a session holds one connection for its whole
  life either way, and key/agent auth means a one-shot command's extra handshake prompts for nothing.
- **A lexical remote-workspace check** to give SSH an auto-approved tier (SS48).
- **An `allow_root_commands` config key** (batch 104). Root needs an off switch and it has one: the
  stored password. A frozen-posture flag over the same decision is the ratchet G3 removed, and it
  would have been a switch a reader could set to `true` without ever being asked for a password --
  two controls, one of them decorative.
- **Prefixing `sudo` to the first word only** (batch 104), which is what a reader expects
  `sudo <command>` to mean. It produces the half-root surprise -- `sudo echo x > /etc/f` writes as the
  unprivileged user -- and it would make the approval notice untrue about the thing that was approved.
- **Reading `sudo` out of the command text and supplying a password for it** (batch 104). The token
  check exists to ADD a prompt, never to arm anything: a harness that answered any `sudo` it spotted
  would be feeding a held password to a line it did not construct.
- **Trusting `-k` to keep the password away from the command** (batch 104), which is what SS66 and the
  gap register both said it did. Measured false on two sudo implementations.
- **An OS keystore for the secret store** (batch 103). DPAPI on Windows, libsecret and Keychain
  elsewhere would need no passphrase and would work headless. They were rejected for the property
  that makes them convenient: they decrypt for any process running as this user, and the agent's
  own shell on WSL, SSH or the host fallback is a process running as this user. It would have
  defended against a stolen backup and not against the threat this harness actually has.
- **`ssh-add` to unlock an encrypted key** (batch 103), the obvious alternative to askpass for the
  passphrase. It mutates the user's agent past the harness's lifetime -- the key stays loaded after
  the process exits -- and askpass needs no such side effect. The existing agent path keeps working
  untouched for anyone already using it.
- **A one-shot askpass listener** (batch 103), killed by measurement: one connection makes more than
  one prompt.
- **Taking a password from `--secrets set <name> <value>`** (batch 103). An argv is readable by every
  process on the machine and lands in the shell history, so the value would have leaked before the
  store ever encrypted it. Every value is read at a masked prompt.
- **A pty (`-tt`) to make a kill reach the remote** (SS53): measured, it merges stderr into stdout and
  CRLFs every line, so it would pay for termination with every command's output shape.
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

### What was measured before it was built (batch 105)

Every slice of §49 had an assumption killed by its probe, and §51 kept the record: **two of the four
decisions above are corrected by measurement rather than implemented.**

- **A re-fetch is not always a seek, and the first measurement of it was wrong.** Fetching three URLs
  twice BACK TO BACK reported all three byte-identical -- a measurement taken over milliseconds, which is
  not the interval a model pages over. One of the three also returned 126 bytes, an error page rather than
  the article, and calling that "stable" measured nothing. Re-measured across **45 seconds** with five
  real URLs and a guard against a body too short to be the page: four identical, and
  `news.ycombinator.com` **not**. Its first difference landed at byte 2,019 -- inside page 1 -- and the
  text at 5000..10000 differed between the two fetches. A model that read page 1 and asked for page 2
  would have been handed two halves of two different documents, spliced at an arbitrary point, with
  nothing in the result able to say so. A news front page is exactly what a research run reads.
- **Paging by re-fetch costs the document once per page.** 14 pages of an 88 KB `docs.python.org` page is
  1.2 MB off the wire to read 65 KB; `bbc.com/news` is 382 KB a page, 5.3 MB for the same 65 KB.
- **The byte cap bites on ordinary pages, not pathological ones.** `docs.python.org/3/library/os.path.html`
  is 88,230 bytes against a 65,536-byte read limit, so roughly a quarter of it is unreachable; `bbc.com/news`
  is 381,981 bytes, of which 17% is reachable. Whatever the cap is set to, the result has to say when it bit.
- **The cap splits a multi-byte character.** `_bounded_body` slices RAW bytes, so a 3-byte character
  straddling 65,536 decodes to one U+FFFD under `errors="replace"`. True before §51; §51 puts it at a
  boundary the model sees.
- **arXiv's `start` does what PG3 assumed.** `start=0` and `start=5` over `all:transformer attention`
  returned disjoint id sets -- zero overlap -- and the feed carries `opensearch:totalResults` (261,387),
  `startIndex` and `itemsPerPage` beside the entries. So a result can say how many papers it did NOT
  return, which is what PG4 needs and what a count of what came back cannot give.
- **`web_search` CAN page, which PG1-PG4 assumed it could not.** `ddgs 9.14.4` takes `page: int = 1` on
  its search path and accepted it live. **But its pages are not a partition:** page 2 of a live query
  overlapped page 1 by one result, where arXiv's `start` overlapped by none. The two providers get
  different wording and neither claims the other's property.
- **`source_corpus` DROPPED page 2, and this was reproduced before it was fixed.** Driving `SourceCorpus.add`
  with a page-1 then a page-2 `fetch_url` result returned 1 and then **0**: `_store` keys by URL and keeps
  the longest text, and `len(existing.text) >= len(cleaned)` is 5000 >= 5000. The near miss is worse than
  the miss -- a page 2 one character longer REPLACED page 1 outright, leaving the corpus holding the middle
  of a document and attributing it to the whole URL. Nothing in PG1-PG4 mentions this, and it is the defect
  that would have made paging worthless: the model reads four pages and the grounding pass scores the first.

### Decisions (PG1-PG9)

| # | Decision |
|---|---|
| **PG1** | **Mirror the `read` tool's shape**, which already solved this for files: an `offset`, a count clamped to a maximum with a NOTE rather than an error, and a message naming the offset to use next. One paging vocabulary across the tools that have one |
| **PG2** | **`fetch_url` gains `offset`**, and `MAX_CONTENT_BYTES` rises with the reachable window so the byte bound cannot silently cap the page count. The cost is stated to the model: a page is a re-fetch, not a seek into something already held. **CORRECTED IN BATCH 105 ON BOTH COUNTS -- see PG5 and PG6.** The re-fetch was measured to splice two different documents together, and raising the byte cap would have weakened a security bound (§31 H7, #55) to buy convenience. The offset is the half that survives |
| **PG3** | **`arxiv_search` exposes `start`**, which the provider already implements, so paging results costs nothing but the parameter. **Measured true, and it extends further than it was written:** `web_search`'s provider implements paging too (`ddgs` takes `page`), which this record assumed it did not -- so that tool gains one on the same reasoning, with the measured caveat that its pages overlap where arXiv's do not |
| **PG4** | **A truncated value says how to get the rest.** The present `truncated: true` tells the model something was lost and nothing about how to recover it, which is what sends it to the shell |
| **PG5** | **A page comes from a HELD body, not a second fetch, and this AMENDS PG2.** `offset=0` always issues the request and refreshes the entry; `offset>0` reads it. PG2's reason was never wrong about the cost, it was wrong about the correctness: a re-fetch of a page that moved makes `offset=5000` a splice of two documents, and no key in the result could have said so. Measured on `news.ycombinator.com` across the interval a model actually pages over. Reuses `_net_common.TTLCache`, which both other network tools already carry, so no new machinery and no new dependency, and the held body costs at most the byte cap per URL for 300 s. Keyed by BOTH the requested spelling and the one that answered, because a redirect means the model may page with either. **`is_url_permitted` still runs on every call, cache hit included** -- a cache consulted before the blocklist is a blocklist with a hole in it, reached by a second call rather than by a redirect, which is #54 one layer up |
| **PG6** | **`MAX_CONTENT_BYTES` does NOT rise, and this DECLINES PG2's first clause.** 65,536 bytes is a security bound (§31 H7, #55) whose own comment says "the only thing that actually bounds this is refusing to read past a cap"; a cap raised whenever it is inconvenient is not a cap. What PG2 was actually protecting against -- a byte bound silently capping the page count -- is fixed by saying so, which is cheaper than removing the bound. Paging reaches every page INSIDE the window, and at the edge the result says the document was cut at the read limit and no further page exists, rather than naming an offset that would return nothing (§32 A7's burn-a-turn class). The cost is stated rather than hidden: measured, roughly a quarter of an ordinary `docs.python.org` page and 83% of `bbc.com/news` is past the bound and stays unreachable |
| **PG7** | **The corpus CONCATENATES pages for one URL.** `_store` learns that a `fetch_url` result carries an offset and extends the held text instead of competing with it on length, bounded by the existing `MAX_DOCUMENT_CHARS` -- whose own comment already says it is "not a second truncation of the tools that exist". Not in PG1-PG4 at all, and without it paging is worthless: measured, page 2 was DROPPED and the grounding pass would have scored page 1 however far the model read. One URL still means one document, so `output_writer`'s sources directory and the citation attribution are untouched. Overlap is one subtraction against the highest absorbed offset, held in the SOURCE's character positions and not in held characters -- the two coincide at offset 0, which is why the distinction has to be stated rather than discovered |
| **PG8** | **A per-item truncation names its recovery route, which is PG4 made concrete.** An arXiv abstract cut at 600 characters and a search snippet cut at 300 carried NO marker at all, so "the paper does not mention X" and "the first 600 characters do not" read identically to the model. Each gains a flag, and the abstract gains the abs URL -- a route `fetch_url` can take. **The caps do not move.** They bound model-facing text from a source nobody here wrote, which is #131's lesson, and a knob to raise them would be a switch over a decision already taken (G3) |
| **PG9** | **A page parameter is part of the cache key.** `arxiv`'s key was `keywords|category|max_results|sort_by` and `web_search`'s `query|num_results`. Adding a parameter that changes the response without adding it to the key serves page 1 to every request for page 2, silently -- the collision `_net_common`'s own docstring warns about between the two tools, arriving instead between two pages of one query. The same batch fixed the matching test-isolation hole: neither cache was cleared between tests, so a test could be served an entry an earlier one left |

### Gap register -- closed in batch 105

- **The result shape held.** `fetch_url` added `offset`, `chars_available` and `message` and changed
  nothing: `url` and `content` are what `source_corpus._add_fetch_url` reads, and `truncated` computes
  to precisely what it did before at every size on both sides of the page boundary. The register's
  constraint was the right one; what it did not anticipate is that the breakage would be SEMANTIC rather
  than structural -- the same URL now returns different text per call, which is what broke the corpus.
- **The caching question is DECIDED, and against the register's framing.** It asked whether a page should
  be cached to save a second fetch, i.e. treated it as a cost question. Measured, it is a correctness
  question first: a re-fetch of a page that moved returns a page 2 that does not continue page 1. PG5
  holds the body for 300 s in one process, keyed by both URL spellings, and the memory cost is bounded by
  PG6's refusal to raise the byte cap -- the two decisions bound each other.
- **A secret split across a page boundary is rejoined by the concatenation, and redaction cannot see it.**
  `redact_output_text` runs per page, as it ran per document before -- so a credential spanning
  characters 4,990 to 5,010 is half in page 1 and half in page 2, and PG7's append puts them back
  together in the corpus. Recorded because the corpus is now the one place in the system where the halves
  become adjacent, which was not true before §51. **CLOSED IN BATCH 106 (§52), and the fix is not the one
  proposed here.** This entry proposed re-redacting the whole held text on every append; §52 redacts at
  the PRODUCER instead, so the halves never exist to be rejoined, which also fixes the two tool results
  the model itself reads and which this entry conceded no corpus-side fix could reach. Its claim that
  "neither half matches a pattern" is corrected there too: measured, it is true of seven of the ten
  patterns and false of the three whose quantifiers are open-ended.
- **The held body cache is not swept.** `TTLCache` expires on READ and never sweeps, which is the shape
  both other network tools already have -- but they hold result lists and this holds bodies, so the cost
  of the same design is different. A run touching many URLs accumulates up to the byte cap for each until
  something reads the key again, and nothing reads a key nobody pages. **CLOSED IN BATCH 106 (§52, RB5
  and RB6):** a sweep on write, and an entry cap, both in the shared class. This entry said the eviction
  policy was "a decision about a cache this batch only needed to make correct", which was the right call
  at the time and is what made it a one-paragraph decision rather than a rushed one.
- **Still open, and recorded rather than decided:** a reachable window larger than 65,536 bytes, which
  would need the security bound re-argued rather than raised (PG6), and is the owner's call rather than a
  batch's; `web_search`'s overlapping pages, which are the provider's behaviour and are disclosed rather
  than corrected, since de-duplicating across pages would mean holding a result set the tool does not
  otherwise keep; and a cross-run or on-disk page cache, deliberately not built -- the held body is
  process-local and expires, like the two caches beside it.

---

## §52. Redact before the cut, and a bound on the held body

**Built in batch 106**, from §51's own gap register. Both entries it left open turn out to be the same
mistake seen twice: §51 introduced a **cut** and a **retention**, and left the first in the wrong order
and the second without a bound.

### What was already decided, twice, before §51 got it wrong

This is not a new principle. The repository had locked "redact before you cut" on two other surfaces, and
one of them states the exact mechanism that defeats the redactor:

- `AGENTS.md`: **"The param digest is redacted BEFORE truncation. ... Truncating first cuts a credential
  below the 20 characters its pattern needs."**
- `tui/app.py`'s diff block runs redaction **"on the WHOLE texts, because the redactor works on whole
  strings and a secret that reached the block would be just as readable split across two wrapped rows as
  on one"** -- with its own measured example: `ci:pw@host` is untouched alone and redacted inside
  `https://ci:pw@host`, because the userinfo pattern anchors on `//`. More context means more redaction.

§51's paging is a third cut, and the first to put the redactor after it.

### What was measured (batch 106)

- **The leak is real, reproduced END TO END before anything was changed.** A `ghp_` token planted at
  characters 4,980-5,020 was driven through the real `registry.dispatch` -- so through
  `check_output_policy`, the redactor's actual caller -- for page 1 and page 2, and both results were fed
  to a real `SourceCorpus`. Page 1 ended `...piscing elghp_a1B2c3D4e5F6g7H8`, page 2 began
  `i9J0k1L2m3N4o5P6q7R8lorem ipsu`, **neither carried a redaction marker**, and the corpus held the token
  whole at character 4,980 of a 10,000-character document. `artifact_entries()` carried it too.
- **Which is to say: to `sources/<sha256>.txt` and to the embedding provider.** `output_writer._write_sources`
  writes `document["text"]` to that file under a docstring calling it "the text itself, redacted", with
  `PERSIST_SOURCE_TEXT` defaulting to `True`; `source_scoring.source_passages` chunks the same text into
  the windows `EmbeddingScorer` sends to `client.embeddings.create`. The rejoined credential went to both.
- **All ten patterns are defeated, and NOT in the same way.** Sweeping every interior cut position:

  | Pattern | Cuts leaving the whole secret in the clear | Rate per boundary |
  |---|---|---|
  | `ghp_…{36}` | all 39 | 0.78% |
  | `AIza…{35}` | all 38 | 0.76% |
  | `<password>…</password>` | all 34 | 0.68% |
  | `-----BEGIN … PRIVATE KEY-----` | all 30 | 0.60% |
  | `password = "…"` | all 26 | 0.52% |
  | `AKIA…{16}` | all 19 | 0.38% |
  | `sk-ant-…{20,}` | 26 of 60, offsets 1-26 | 0.52% |
  | `sk-…{20,}` | 22 of 30, offsets 1-22 | 0.44% |
  | `//user:pass@` | 18 of 46, offsets 7-24 | 0.36% |
  | `xox[baprs]-…{10,}` | 14 of 27, offsets 1-14 | 0.28% |

  The fixed-length tokens, the PEM literal and both credential shapes are defeated by EVERY interior cut,
  because a prefix of a fixed-length token is not that token. The three open-quantifier patterns are
  defeated only near their head -- past that a long enough prefix still matches on its own, which leaves
  `[REDACTED]` followed by the token's tail in the clear: a signpost rather than a clean miss. §51's gap
  register said "neither half matches a pattern"; that is true of seven of the ten.
- **Producer-side redaction is cheaper than what it replaces, for the case that matters.** Redacting a
  whole 64 KB body once is **1.75 ms**; redacting its fourteen pages separately is **2.23 ms**, and one
  page is 0.20 ms. So the worst case -- a model that reads page 1 and stops -- pays **+1.54 ms**, against
  a `REQUEST_TIMEOUT_S` of 8,000 ms and a real network round trip. A fully paged document is 0.48 ms
  faster than before.
- **The cache grows and releases nothing.** 100 `fetch_url` fetches of a capped page held **6.3 MB**, and
  not one entry was ever dropped: `TTLCache` expired only inside `get`, and nothing read those keys again.
  A stale entry ten thousand seconds past a 300-second TTL stayed resident through a write to a different
  key. Per entry the cost is bounded at ~64 KB whatever the encoding -- 21,845 chars for CJK, 16,384 for
  astral -- and a redirect's two keys share one `str` object rather than copying it, so **the entry count
  was the only unbounded dimension**. `MAX_ITERATIONS` is 50, which bounds a pass and not a process.
- **Redaction does not disturb the multi-byte boundary.** 65,536 is not divisible by 3, so a CJK body
  decodes with a U+FFFD at its last character; `redact_output_text` leaves the string byte-identical.
- **Cross-run reuse is consistent, not a defect.** The cache is process-scoped, so a resumed thread can
  page a body an earlier run fetched -- but only inside the 300 s TTL, and `is_url_permitted` still runs
  on that call. That is exactly the freshness contract PG5 states. Recorded so it is not re-investigated.

### Decisions (RB1-RB7)

| # | Decision |
|---|---|
| **RB1** | **`fetch_url` redacts the whole body BEFORE it is cut or held.** One `redact_output_text` call on the decoded body, above both the cache and the slice, so the redactor sees the document exactly once while it is still one string. Everything below that line -- the held copy, page 1, and every page a later offset cuts out of it -- is a slice of already-clean text. Not a new principle but a third application of one the record already carries twice; §51 is the first place it was applied in the wrong order |
| **RB2** | **`fetch_url` reports its own redaction, once per fetch, naming the URL and never the match.** Redacting at the producer means `check_output_policy` finds nothing left to alter, so #49's "nothing this function does is silent" would stop covering this tool with nothing to announce that it had. One line at the fetch is better observability than what it replaces -- a fully paged document previously warned up to fourteen times, once per page -- and it keeps that function's own rule about never echoing what was found, since saying so would put the credential in `app.log` and every sink a WARNING reaches. The URL is named because it is what makes the line actionable, and `logging_setup`'s formatter redaction is the sink-side guard for a URL carrying userinfo, exactly as it already is for the `fetch_url failed for %s` line |
| **RB3** | **Every offset the tool speaks is a position in the REDACTED body, and that is the only position space it exposes.** `offset`, `chars_available` and the message all count redacted characters. Self-consistent, because a caller only ever passes back an offset the tool named. Two consequences stated rather than left to be discovered: a document containing credentials reports a smaller `chars_available` than its source has, and the same URL reports different totals with `REDACT_TOOL_OUTPUTS` on and off |
| **RB4** | **`source_corpus` gains nothing, and this DECLINES §51's own proposed fix.** The register proposed re-redacting the whole merged text on every append. RB1 removes the need -- a `fetch_url` page now arrives already clean, so the corpus's entry-side pass is the no-op its docstring always described -- and re-scanning up to 20,000 characters per page would buy no coverage while still leaving unfixed the model's own two tool results, which the producer-side fix does close. The entry-side pass STAYS, because `arxiv_search` and `web_search` do not redact at their producers and rely on it |
| **RB5** | **`TTLCache` sweeps expired entries on write.** Its docstring said a sweep "would be machinery for a problem neither tool has"; that was true of two callers holding result lists and false of the third, which holds bodies. The claim is corrected in place rather than left standing next to code contradicting it. `get` keeps its expire-on-read unchanged -- that path is what makes a post-TTL query a real request again, and a cache that only swept would serve a stale entry until something else happened to write |
| **RB6** | **`TTLCache` carries an entry cap, `MAX_ENTRIES = 128`, and evicts the OLDEST.** Derived rather than picked: `MAX_ITERATIONS` is 50 and a redirecting fetch stores two keys for one body, so 100 keys is the most one pass can reach and the cap sits above it. That headroom is the requirement, not a nicety -- a page-2 request that misses degrades to a re-fetch, which is precisely the spliced document PG5 exists to prevent, so an eviction firing in ordinary use would trade a bounded memory cost for an unbounded correctness one. Oldest-first uses `dict` insertion order, which is age order because every entry is stamped as it is written, so no second ordering has to be kept consistent; and a re-stored key is popped first so that refreshing an entry does not leave it next in line to be thrown away. In entries rather than bytes because a generic cache cannot measure what a caller hands it -- for the caller that made this necessary the two are interchangeable, since PG6's refusal to raise `MAX_CONTENT_BYTES` is what holds each body to ~64 KB |
| **RB7** | **§51's "neither half matches a pattern" is corrected, not repeated.** Measured true for every fixed-length vendor token, the PEM literal and both credential shapes; false for `sk-`, `sk-ant-` and `xox`, where a prefix of twenty or more characters still matches and what leaks is the token's tail beside a `[REDACTED]`. Recorded because the narrower claim is the one the tests pin, and because a register entry that overstates a gap is as hard to act on as one that understates it |

### Gap register

- **The fix is at the producer, so it does not reach a consumer that fetches differently.** RB1 covers
  `fetch_url`, which is the only tool that cuts a document into pages. `arxiv_search` and `web_search`
  truncate per item and their items are far shorter than any credential-bearing span, so the same defect
  does not arise -- but a future tool that returns a long body and slices it would have to make the same
  choice, and nothing mechanical forces it to. Recorded rather than solved: a lint for "slicing text that
  has not been redacted" would have to understand provenance, which is more machinery than one comment in
  the place a third such tool would be written.
- **The model's transcript is still where two pages sit side by side unredacted in principle.** RB1 makes
  each page clean, so there are no halves left to rejoin -- but that is a property of the producer, not a
  guarantee the transcript enforces. If some other producer ever hands the model two adjacent fragments,
  no redactor sees them together. Unchanged by §52 and stated because §51's register raised it.
- **`MAX_ENTRIES` is a count and the thing it bounds is bytes.** For `fetch_url` the two are equivalent
  only because `MAX_CONTENT_BYTES` holds an entry to ~64 KB; for `arxiv` and `web_search` the entries are
  much smaller, so the same cap is far looser for them than for the caller it was derived from. Accepted:
  a byte-accounting cache would need every caller to declare a size function, which is a cost paid by
  three callers to bound one.
- **Nothing sweeps on a process that stops writing.** The sweep runs on `set`, so a cache that is written
  once and then only read holds its entries until each is read past its TTL -- better than before, and
  still not a timer. Deliberate: a background sweeper is a thread, and a thread is a lifecycle this
  module does not have.

---

## §53. A rolling bound on what the harness pulls and pushes

**Added in batch 122, from the owner's request** for rolling-window network I/O limits, unlimited by
default: a ceiling for users on a metered connection, and defence in depth against a tool pulling or
pushing something enormous. Specified here to the depth the owner's answers support; the design
round that locks the remaining shapes happens when it is picked up (the §49 cycle).

### What the code does today

- **Nothing counts a byte.** There is no rate limiter, quota, budget or byte accounting anywhere in
  the tree. `tools/builtin/fetch_url.py`'s `MAX_CONTENT_BYTES = 65_536` is a per-request read cap on
  one tool, and `_net_common.MAX_ENTRIES = 128` bounds a cache's entry count. Neither is a window and
  neither is a total.
- **Four callers reach the network through this process**, and all four already import
  `tools/builtin/_net_common.py`: `fetch_url` (`httpx.stream`), `arxiv` (`httpx.get`),
  `core/reasoning/scholar.py` (`httpx.get`) and `web_search` (`ddgs`). That shared import is the
  seam, and it exists for the cache rather than for this.
- **`ddgs` accepts no client.** Measured against the pinned 9.14.4: `DDGS.__init__` is
  `(proxy, timeout, *, verify, api_url, spawn_api)`. There is a proxy argument and no transport and
  no client, so `web_search`'s bytes cannot be counted at the socket without standing up a local
  proxy -- which is a process, not a counter.
- **The provider SDKs are three different answers.** `anthropic` and `openai` accept a custom
  `httpx` client; `google-genai==1.0.0` is the pin whose `ThinkingConfig` this project already works
  around, and what it accepts has to be measured rather than assumed.
- **Everything a shell command does is invisible here.** A container, WSL or SSH command opens its
  own sockets; `stdio` MCP servers are separate processes. The only existing lever is the binary one
  §50 built: `--network none` or not.

### Decisions (IO1-IO7)

| # | Decision |
|---|---|
| **IO1** | **Unlimited by default, and a limit is THREE meters that are enabled separately** -- tool traffic, provider API traffic, and MCP HTTP/SSE traffic. The owner's reason is the whole of it and it is not a preference: *"networked tools always pull data from the internet, provider and mcp might actually be local."* A single switch over all three would charge a local Ollama and a local MCP server against a cap bought for a mobile tether. Tool traffic is the one that is on when a limit is set at all; the other two are opt-in |
| **IO2** | **The meter lives in `tools/builtin/_net_common.py`, beside `TTLCache`.** It is the one module every network-backed tool already imports, including `core/reasoning/scholar.py`, which reaches across package boundaries to get it. A second home would be a second place to forget, which is this project's canonical bug shape stated as a placement rule |
| **IO3** | **Three tools are counted at the byte and `web_search` is an ESTIMATE that says so.** `fetch_url`, `arxiv_search` and the scholar lookup go through this process's own `httpx` and can be counted exactly. `ddgs` has no client seam (measured, above), so its figure is the size of what it returned and is labelled an estimate wherever it is shown. Stating the weaker number as an estimate is the point: a cap that silently undercounts one of its four inputs is a cap a user will discover is wrong by overrunning their data |
| **IO4** | **The provider meter is per-SDK and names which of its figures are exact.** Exact where a custom `httpx` client can be passed; derived from token counts, and announced as derived, where it cannot. One number presented as the same kind of fact for three SDKs that cannot all produce it is the instrument-that-lies shape batches 118 and 119 already paid for twice |
| **IO5** | **What CANNOT be metered is written down rather than discovered** -- in the `config.yaml` comment, in `SECURITY.md` and in the message a user sees when they set a limit. Subprocess traffic (container, WSL, SSH), `stdio` MCP children, and anything else with its own socket. A control that reads as a total and is not one is worse than no control, which is the argument `SS44` already makes about a `ulimit` the command can remove |
| **IO6** | **Exceeding the window REFUSES the call**, with a reason the model can read and a transcript line the user can see. Never a silent truncation and never a quiet stall: a truncated body is the spliced-document failure `PG5` exists to prevent, and a stall is indistinguishable from a hung network |
| **IO7** | **The window ROLLS; it does not reset per run or per turn.** A per-run reset is defeated by a loop of short runs, and the thing being protected -- a data allowance -- is measured against wall-clock time rather than against this process's idea of a unit of work |

### What must be settled before this is built

- The window's length, and whether it is one number or one per meter.
- Whether ingress and egress get separate ceilings or share one.
- Whether the budget is process-wide or per agent / per run, and what a subagent inherits.
- Whether the figure joins `UsageLine` in the sidebar, which is where the two existing
  "how much has this cost" instruments already live.
- Whether a refusal stops the one tool or every network tool for the rest of the window.

### Gap register

- **The estimate and the exact figure are added together.** IO3 is honest about which input is which,
  and the total is still a mixed quantity. Accepted for now: the alternative is two totals, and a user
  reading a data cap wants one number.
- **A limit protects the allowance, not the machine.** It cannot stop a shell command, which is where
  an agent would reach for a large download if the tool route refused it. §50's declared-network flag
  is the control on that path and it is binary.

---

## §54. `@` names a file, and the tree is read once

**Added in batch 122, from the owner's request:** `@` in the prompt box offers the project's files
with completion, carries the path relative to the project root, allows folders, hides gitignored
files by default, and does **not** load the file into context -- it tells the agent to read what was
named.

### What the code does today

- **The completion machinery already exists and is good.** `tui/widgets.py`'s `SlashSuggest` (batch
  55, window sliding added in 56) does a row-budgeted, keyboard-navigable, sliding-window suggestion
  panel above the prompt, and `PromptInput` drives it through `SuggestionsChanged` /
  `SuggestionsMoved` / `_complete()` with a dismiss latch and the api-edit-versus-keystroke seam in
  `load_text`. `SlashCommand.complete` (batch 84) is the hook a second namespace plugs into: it
  already offers rows whose `name` is the whole line after the slash.
- **The root is `tools/builtin/file_ops.WORKSPACE_ROOT`**, which is `os.path.realpath(config.WORKSPACE_DIR)`
  captured at import -- the project itself when `workspace_is_project` is true.
- **Nothing in this tree shells out to `git`.** Not once, in any module. There is no gitignore parser
  and no dependency that provides one.
- **The prompt-fragment pattern exists**: `with_refs` (`core/loop.py:167`) appends thread state to a
  system prompt at turn time, announcing what it is and what it is not.

### What was measured (2026-10-03)

- **`git ls-files --cached --others --exclude-standard` on this repository: 326 files in 18 ms.**
  Without `--exclude-standard` it is 985. So the ignore filter removes two thirds of the tree, and
  the whole read costs eighteen milliseconds -- which settles the "read it once at startup" budget
  rather than leaving it to be felt.
- The same call answers tracked AND untracked-but-not-ignored files in one invocation, and honours
  nested `.gitignore` files, `.git/info/exclude` and the user's global excludesfile, none of which a
  hand-rolled matcher gets for free.

### Decisions (AT1-AT8)

| # | Decision |
|---|---|
| **AT1** | **`@` is a SECOND TRIGGER into the panel that exists, not a second panel.** One widget, one row budget, one set of keys, one place where a resize is pinned against the transcript's scroll. Two panels would be two answers to "how many entries fit", which is the split `SlashSuggest`'s own docstring refuses for the slash list |
| **AT2** | **The tree is read ONCE at startup, and refreshed only when the agent's `write` tool creates a file.** The owner's rule and the reason is latency: probing on every `@` keystroke, or walking on every prompt, pays for a directory the user is not browsing. A file the USER creates mid-session needs a restart to appear in the panel, and the tool still reads it fine -- the agent has live filesystem access either way, so the staleness costs completion, never capability |
| **AT3** | **The ignore list comes from `git ls-files --cached --others --exclude-standard`** (owner decision), measured at 326 files in 18 ms here. Exact git semantics, including the three ignore sources a hand-rolled matcher would silently disagree with. Off git -- no binary, no repository, or a non-zero exit -- an `os.walk` with a small built-in skip list answers instead, and the panel SAYS which source answered. A user who thinks a file is hidden and is wrong about why is the failure this names rather than hides |
| **AT4** | **Ignored files are hidden by default and there is an option to show them.** The default is the owner's; the option exists because `.gitignore` is not an access-control list and a user who wants `dist/` has a real reason |
| **AT5** | **Every entry is the path relative to the root, never the bare basename.** A basename repeats across subfolders and the panel would offer two identical rows. Folders are offerable as entries in their own right |
| **AT6** | **A mention does NOT load the file.** The path stays in the user's message text and a short fragment tells the model to read what was named -- `with_refs`'s shape, appended at turn time. The owner's reason is the size of the files, and there is a second: forcing a read makes `@` cost tokens the user did not ask to spend, which makes it the wrong affordance for "I mean this one" |
| **AT7** | **The fragment is assembled where the other thread-state tiers are** (`with_goal` / `with_refs` / `with_todos` in `core/loop.py`) and MUST NOT move into `with_catalogs`. That function feeds `pass_prompt()`, so a file mentioned in a chat session would land in all ten research passes. §19's K6, and this is its fifth instance -- see TECHNICAL_DEBT 19 |
| **AT8** | **`git` is a new external dependency on this path, and that is stated.** It is the first subprocess call to git this project makes. It is also optional by construction (AT3's fallback), so the dependency is on the QUALITY of the list, never on the feature working |

### What must be settled before this is built

- Whether the startup read is eager or lazy on the first `@` -- 18 ms here, and this repository is
  326 files.
- The cap on offered entries, and what the panel does in a 50,000-file checkout.
- Whether `@` matches on the basename, on the whole relative path, or on both.
- What a mention of a since-deleted file does: pass through, or say so in the prompt box.
- Whether a folder mention expands to its files in the fragment or is named as a folder.

---

## §55. The whole command, and what it printed

**Added in batch 122, from the owner's request**, after seeing another TUI render a shell call in
full with its output beneath it: a way to see the whole command -- and ideally what it returned --
without leaving the transcript, retracted by default and unmistakably part of the tool call rather
than of the model's prose.

### What the code does today, and the premise it corrects

- **The one-line collapse is real and it is ONE producer.** `safety/policy_enforcement.py:500`
  (batch 96) does `text = " ".join(text.split())` inside `param_digest`, after the redaction and
  before the truncation, with a 60-character cap per value and 140 overall. Three consumers read it:
  the TUI's `▸ name  digest` line, `core/replay.py`'s replay of the archive, and the research
  pipeline's tool rows.
- **The output is not collapsed -- it is NEVER RENDERED.** `tui/app.py:3322-3360` handles
  `tool_result` and writes to the transcript only on FAILURE (`✗ name error`) and, for `write` and
  `edit`, a diff block. A successful tool's result goes to the model and nowhere else. So the half of
  this section that reads as "un-hide what is collapsed" is in fact new rendering.
- **Two of the four shell tools are already openable.** `registry.opens()` returns `("thread",)` or
  `("session",)` and arms a ctrl+click (§47 NA6, §49 SS14), so `shell_background` and `shell_monitor`
  lines can already be opened into a view. Plain `shell` cannot.
- **Thinking is a global switch, not a per-block fold.** `tui.show_thinking` and `/thinking` turn
  reasoning on or off for the session; there is no expand/retract on an individual span. So the
  analogy in the request describes a behaviour this TUI does not have yet in either place.

### What was measured

- **`RichLog` cannot rewrite a row it has drawn.** `Transcript`'s own docstring states it and the
  widget is built around it -- `_entries` exists precisely because a `/theme` switch needs
  `rerender()` to replay the whole log. `reflow()` is the variant that leaves a streaming span open,
  added in batch 112 for resize. So an in-place expand/retract is not a repaint; it is a full replay,
  and a replay moves the scroll position.

### Decisions (TR1-TR5)

| # | Decision |
|---|---|
| **TR1** | **This is TWO problems and both are in scope.** The command is abbreviated and the output is absent. Shipping the first alone leaves the more useful half missing, and the owner's request names both |
| **TR2** | **`param_digest` does not change.** Whatever is built is a SECOND rendering beside the digest, never a loosening of it. Three consumers read that function and the multi-line command it collapses once drew its remaining lines at column 0 underneath the tool line, outside the indent -- in all three. One producer, one job |
| **TR3** | **Retracted by default, and unmistakably the tool call's.** The owner's reason is that the user has already seen a complex command in the approval modal. The attribution half is the harder half and it is a requirement rather than a nicety: a block of shell output that reads as the model's own prose is the untrusted-content confusion the system prompt spends a paragraph on |
| **TR4** | **The mechanism is deliberately NOT chosen here, and the constraint that bounds the choice is now measured.** The owner deferred it, and the measurement above is what the two candidates have to be judged against: an inline expand costs a `reflow()` of the whole transcript and moves the reader's position, while a ctrl+click view reuses `registry.opens()` and the view surface §47 and SS14 already built. The second is the owner's own fallback proposal. Recording the measurement rather than the verdict is the point -- the decision is cheap to make with it and was being made without it |
| **TR5** | **Whatever is chosen applies to all four shell tools, or the record says why not.** `shell`, `shell_background`, `shell_monitor` and `shell_interactive` differ in when their output exists, not in whether a user wants to see it, and four surfaces for one question is the drift EP6 names |

### What must be settled before this is built

- Inline expand against a ctrl+click view, with TR4's measurement in hand.
- How much output is kept and where. `core/shell_sessions.py` already keeps a session's first 10,000
  and last 190,000 characters in memory (SS12); a one-shot `shell` keeps nothing today.
- Whether the retained output survives a restart, and what SS18's "what the agent was given" header
  means for a one-shot call.
- Whether `core/replay.py` renders the same thing from the archive, which is the drift TR2 guards.

---

## §56. A decision model as the shell classifier

**Added in batch 122, from the owner's request:** an optional System One decision model as a second
opinion on shell approval, so the gate is not limited to what can be written as a rule. The owner's
own security constraint arrived with the request and is locked below as JV4.

### What the model is

Verified against current sources rather than from the request. **Jev** is TypeSafe AI's "System One"
decision model, released 2026-09-15: it takes a piece of state plus named, typed questions and
returns typed values with calibrated probabilities, never prose. The wire is
`POST <base>/v1/systemone` with `{"model": ..., "state": ..., "questions": {...}}` and bearer auth;
the question types are **Choice** (categorical), **Score** (bounded numeric) and **Noul** (yes/no
probability). Published latency is 70-500 ms.

The ecosystem matters for the configuration shape. **Ollama 0.35 serves `/v1/systemone` beside
`/v1/chat/completions` on the same base**, and **OpenJev** and **openjev-sglang** implement the same
wire against open models. So one URL shape -- base plus `/systemone` -- covers hosted, self-hosted
and local, and the coexistence case the owner raised falls out of it rather than needing a rule.
A provider that exposes the capability only through an OpenAI-shaped chat endpoint is a different
request body, which is what JV7 is for.

### What the code does today

- **`_shell_approval_check` (`tools/builtin/shell.py:450`) is already a ladder read top to bottom**,
  with the mode switch first, then the refusals and the always-asks (`UNAVAILABLE`, SS40's
  protected-segment refusal, SSH, root, protected path segments), then the two visible opt-ins
  (`AUTO_APPROVE_SANDBOX_FALLBACK` and `contained`), then the capability rule. Both opt-ins sit
  BELOW the protected-segment check, deliberately, so one opt-in cannot cover the directory the
  read-only mount exists to protect.
- **`capability.APPROVAL_MODES` is a validated enum** (`always`, `tiered`, `contained`, `never`),
  validated at import by whoever owns the setting.
- **`classify_command` is ONE object with TWO consumers** -- the gate reads it to decide whether to
  ask, and `run_sandboxed` reads the same object to decide how to run. Audit #157 is what it cost
  when those two asked separately.
- **The owner's `;` question, answered by measurement rather than by reading.** There is no
  `&&`-splitting anywhere in the tree to extend to `;`: both characters are in
  `_SHELL_METACHARACTERS` (`security/sandbox.py:1533`), so both force `_is_inert` to False. What Q2
  actually changed is that `_needs_network` reads EVERY whitespace token of a non-inert command
  rather than its first word, and `_escapes_workspace` already reads every token after the first as
  a path. **Measured 2026-10-03:** `echo hi && curl http://evil` and `echo hi; curl http://evil`
  both classify `SANDBOXED_NET`, `network=True`, `writes=True`; `ls -la && rm -rf /tmp/x` and the
  `;` spelling both classify `SANDBOXED`, `writes=True`. `tests/test_shell.py:1156` already
  parametrises the `;` spelling beside the `&&` one. **There is no loophole and nothing to build**,
  and it is recorded here rather than left as a question someone re-opens.

### Decisions (JV1-JV10)

| # | Decision |
|---|---|
| **JV1** | **It is a NEW `shell_approval_mode`, not a rewrite of the gate.** `APPROVAL_MODES` is already the enum the ladder switches on, and a second switch over the same ladder is the ratchet G3 removed. One mode, validated at import like its four siblings, and every existing mode behaves exactly as it does today |
| **JV2** | **Everything above the opt-ins stays above this one.** `UNAVAILABLE`, SS40's refusal, the protected-segment check, SS48's SSH-always-asks and SS65/SS67's root-always-asks all answer BEFORE the decision model is consulted. This is not caution added for a new feature; it is where the two existing opt-ins already sit, and it is the whole reason a prompt-injected verdict cannot reach `.venastine/` or `sudo` |
| **JV3** | **The hardcoded classifier still decides what RUNS.** The decision model answers one question -- "must a human say yes" -- and never picks the backend, the containment or the `--network` flag. `classify_command` is one object with two consumers for #157's reason, and a second opinion that chose the runtime would reopen exactly that gap in the module written to close it |
| **JV4** | **The classifier is given the command and the project path, and NEVER the rationale.** The owner's constraint, and the threat is concrete: a sufficiently capable rogue agent writes a rationale that argues a dangerous command into a safe verdict, and a verdict that auto-approves is arbitrary execution on the host. The project path is included because containedness is not answerable without it. Nothing the agent wrote as prose reaches the classifier |
| **JV5** | **Four metrics, extensible: containedness, general risk, workspace modification, networkedness.** They are separate because they come apart -- `rm -rf` in the workspace is high modification and may be low risk, and a networked command may be perfectly contained. Each gets its own threshold. `Score` for the graded ones and `Noul` for networkedness, which is a yes/no the model still answers with a probability |
| **JV6** | **The user sets each threshold and chooses what a failure means:** auto-reject, or raise the approval modal. Both are the owner's, and the second is the one that makes the feature adoptable -- a user can run it as a filter long before they trust it as a decision |
| **JV7** | **`classifier_style` is EXPLICIT (`systemone` or `openai_json`) and nothing is probed** (owner decision). A startup probe is a network call at launch and a guess about a security control, and it guesses wrong exactly when a proxy answers 200 to everything. The native style sends the real body; the OpenAI style sends a schema-constrained chat request and parses it into the same verdict object |
| **JV8** | **Thresholds and endpoint live in `config.yaml` under `HARNESS_AUTHORITY_KEYS`; the API key lives in `providers.json`.** The split already exists and this respects it: a number that decides whether a human is asked is harness authority, and a credential belongs in the gitignored file. Neither file learns the other's job |
| **JV9** | **The `config.yaml` warning states the distinction precisely rather than repeating either side of it.** The vendor's "mathematically impossible to hallucinate" claim is about TYPE conformance -- constrained decoding cannot emit a value outside the schema -- and says nothing about the judgement being correct. The model still reads text an attacker may have influenced, and a confident wrong number is exactly the failure mode a threshold acts on. The warning says that, in those terms |
| **JV10** | **A classifier that is slow, unreachable or malformed NEVER auto-approves.** It falls back to asking. Which mode it falls back to is open; that it fails closed is not, and it is the one property this whole section rests on: an attacker who can make the endpoint unreachable must not thereby widen what runs unasked |

### What must be settled before this is built

- The TUI command's name. `/classifier` is the obvious parallel to `/model`; something naming the
  decision rather than the mechanism may read better.
- Which mode JV10 falls back to -- `tiered` is the shipped default and asks about anything that can
  run code.
- Whether the verdict is shown inside the approval modal when one is raised, and whether it is
  persisted with the call so `core/replay.py` can render it.
- Whether a per-call latency of 70-500 ms on every shell command is acceptable, and whether the
  verdict is cached per exact command string.
- Whether the four metrics are fixed or user-extensible in config.

### Gap register

- **This widens what runs unasked, by design, and the widening is the feature.** JV2's ordering is
  what bounds it. Anyone adding a step to `_shell_approval_check` later has to decide where it sits
  relative to this one, and the answer for every refusal and every always-ask is "above".
- **A local model's judgement is not the hosted one's.** The same thresholds against a 0.8B local
  model and against `jev-latest` are two different policies wearing one set of numbers. Worth saying
  in the config comment beside JV9's warning.

---

## §57. One model for the subagents

**Added in batch 122, from the owner's request** for a `/submodel` command so subagents can run on a
cheaper model than the main agent, and for per-agent model and provider selection.

### What the code does today -- the second half is already built

- **`AgentDef` carries `model` and `provider`** (`core/config_loader.py:228-229`) and both are
  honoured in three places: a `/agent` chat turn (`tui/app.py:2659-2660`), a spawned subagent
  (`agents/subagent_tool.py:238-240`) and compaction (`core/compaction.py:1196`). The rule is the
  same in all three: `agent.model or <inherited> or config.MODEL_NAME`.
- **`/model` already knows about the conflict** and prints a note when the active agent pins its own
  pair (`tui/app.py:4786`), naming what the switch does and does not reach.
- **There is no failover anywhere.** `core/provider_errors.py` classifies transients and backs off;
  nothing in the tree ever answers on a different provider than the one that was asked.

So the per-agent half of the request exists. What is new is `/submodel` and the question of what
happens when a provider is down.

### Decisions (SM1-SM4)

| # | Decision |
|---|---|
| **SM1** | **Per-agent model and provider are BUILT, and this section says so rather than rebuilding them.** The request described them as missing; they are honoured in three call sites. Recording that is the useful half of this section |
| **SM2** | **Precedence: an agent's own pin, then `/submodel`, then the session model.** (Owner decision.) It is the rule already written at every one of those three call sites, and the alternative would mean a session command silently downgrading an agent that was pinned to a strong model for a stated reason |
| **SM3** | **`/submodel` names the agents it does not reach**, exactly as `/model` already does for the active agent. A command whose effect is invisible for the agents that pin their own is the silent-no-op shape this project keeps closing |
| **SM4** | **No automatic cross-provider failover.** A run that answers on a model the transcript does not name is the drift class this record is full of, and the existing behaviour -- retry the transient, then fail with the provider's own reason -- is both honest and already built. A user whose provider is down switches with `/model`, which takes effect on the next turn |

### What must be settled before this is built

- Whether `/submodel` is remembered across launches. `/model` is (RM3), in the user-tier store beside
  `settings.json`, never in it.
- What a submodel with no API key does. `/model` warns rather than refuses, for the local-endpoint
  case, and the same argument applies.
- Whether `/submodel` also applies to the compactor and the pipeline reviewer, which are agents that
  run without being spawned by a chat turn.
- Whether the sidebar's agent panel shows the submodel.

---

## §58. Who the agent is when you ask it

**Added in batch 122, from the owner's request:** a short persona so the model knows what harness it
is in -- name, purpose, that a research mode exists, and which model is underneath -- without being
told to deny or conceal what it actually is, and without bloating the system prompt.

### What the code does today

- **The chat system prompt is one sentence.** `DEFAULT_SYSTEM_PROMPT` (`core/loop.py:81`) is
  literally `"You are a helpful assistant."` plus the shared untrusted-content paragraph. §32's A8
  (#72) already records why: everything else in a chat prompt is generated or thread state -- the two
  catalogs, `with_goal`, `with_memories`, `with_refs`, `with_todos`, active skill bodies -- so the one
  paragraph a chat turn most needs was the one part no tier supplied.
- **It is a module constant, evaluated at import.** The live model is not: `/model` changes it
  mid-session and an agent definition can pin its own (§57).
- **The research passes have their own opening line**, `prompts/universal_system_prompt`, which begins
  "You are one stage in a multi-pass, self-critiquing research pipeline."
- **Five sites assemble a chat system prompt**: the TUI chat turn, `core/loop.py`,
  `agents/subagent_tool.py`, `core/compaction.py` and `core/reasoning/review.py`.

### Decisions (PR1-PR5)

| # | Decision |
|---|---|
| **PR1** | **Composed at TURN TIME, never baked into the constant.** The persona names the live model and the live model is a session fact, so a string built at import is wrong the moment `/model` is used or an agent pins its own. It is a `with_persona(...)` beside `with_goal` / `with_refs` / `with_todos`, which is where the thread-and-session tiers already live |
| **PR2** | **Chat only.** The ten research passes open with "you are one stage in a pipeline", and a persona there would contradict the first line of their own prompt. §19's K6 -- the SIXTH instance, after memories (M13), refs (M19), todos (J11's fourth) and §54's AT7, which is this batch's other one |
| **PR3** | **A few sentences, and leanness beats completeness.** The owner's rule: better to miss a detail than to grow the system prompt. The token cost is measured against a real turn before the wording is fixed, not estimated |
| **PR4** | **It does not tell the model to deny or conceal its underlying identity.** The owner's rule, and it is also the right one: an instruction to misdescribe itself is the one thing in a persona that a user discovering it would read as deception, and the underlying model's name is already in the sidebar and the status line |
| **PR5** | **`with_persona` is the FIFTH `with_*`,** and TECHNICAL_DEBT 19 -- the unresolved question of whether the convergence counts four tiers or five -- moves with it rather than gaining a sixth member in silence |

### What must be settled before this is built

- The exact wording, and its measured token cost on a real turn.
- Whether an agent with its own body gets it too. `system_prompt_for` already appends
  `## Agent: <name>` plus the body, which is an identity of its own.
- Whether the compactor and the reviewer get it. They are chat-shaped runs that nobody converses
  with.

---

## §59. Pick it instead of typing it

**Added in batch 122, from the owner's request:** a multi-select modal for removing memories instead
of typing uuids, and `shift+tab` to cycle the active agent. Two requests, one idea -- both replace an
exactly-typed string with a selection, and both reuse a widget that already exists for exactly that.

### What the code does today

- **`/memories` prints uuids into the transcript and `/forget` takes one** (`memories/tui_commands.py`).
  By id and not by substring, deliberately: a substring matching two memories would have to pick one,
  and picking silently deletes something the user did not name. **It does not confirm**, and it does
  not need to: reading a uuid out of the transcript and typing it back IS the deliberation, which is
  exactly the friction this section removes.
- **Multi-select modals already exist.** `GrantPickerScreen` and `SubagentSignoffScreen`
  (`tui/screens.py:344`, `:403`) both build a `SelectionList` and return the ticked subset.
- **`shift+tab` is reverse focus today, and focus means something here.** `RichLog.can_focus` and
  `TextArea.can_focus` are both True (measured against the pinned textual 8.2.8), so the transcript
  is a focus stop and `RichLog` binds the scrolling keys -- tabbing to it is how the transcript is
  scrolled from the keyboard. `app.tcss` records that batch 59 refused a `VerticalScroll` sidebar
  partly because it "would add a tab stop", so tab stops are already treated as scarce here.
- **`PromptInput` already claims `tab` conditionally** -- `Binding("tab", "complete")` with
  `check_action` returning `None` when the panel is shut, so the key reaches the focus system the rest
  of the time. That is the precedent for taking a key only while it means something.
- **The roster is long.** Eleven built-in agents (`agents/builtin/`), plus the user and project tiers,
  plus "default", so a blind cycle over `manager.names()` is twelve or more stops.
  `/agent` lists them and `ctrl+g` opens a picker already.

### Decisions (UI1-UI4)

| # | Decision |
|---|---|
| **UI1** | **`/forget` gains a multi-select modal and the command keeps working.** `SelectionList` through `ModalScreen`, the `GrantPickerScreen` shape, returning the ticked set to the same `storage.forget_memory`. The by-id command is not replaced: it is what a script and a transcript-reader use, and it is the only route that works when the list is long |
| **UI2** | **`shift+tab` cycles the active agent, and it SPENDS reverse focus.** Stated rather than discovered: the transcript is a focus stop and keyboard scrolling goes through it. `tab` is left alone, so the transcript stays reachable forward -- which is what makes the trade acceptable rather than a loss |
| **UI3** | **The cycle walks a SHORT ORDERED roster, not `manager.names()`.** Twelve-plus stops is a list, not a cycle, and the thing the request describes is moving between two or three working modes. What the roster is and where it is configured is open; that it is not the full catalogue is not |
| **UI4** | **The modal confirms, and `/forget <id>` does not start to.** A new requirement rather than a preserved one: today the uuid is the confirmation, and UI1 exists to take that away. Ticking five rows and pressing enter is one keystroke away from deleting five durable memories with no second thought, where the command it replaces cost a careful copy each. The command keeps its current behaviour, because nothing about it changed |

### What must be settled before this is built

- UI3's roster: a config list, the agents the user has used this session, or a declared field on
  `AgentDef`.
- Whether the cycle includes "default" as a stop, and where it sits in the order.
- Whether the memory modal shows scope and category as columns, and whether it can filter.
- Whether `shift+tab` is bound at the app level with `priority=True` -- which is what `ctrl+c`
  needs in order to beat the focused text box -- and what that does under a modal.
