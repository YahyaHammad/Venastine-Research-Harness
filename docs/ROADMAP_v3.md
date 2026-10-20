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
- **§55. The whole command, and what it printed** — **(BUILT in batch 125)** (a shell call shows a 140-character digest and its output is never rendered at all, so what the agent ran and what came back are both off screen)
- **§56. A decision model as the shell classifier** — **(NOT BUILT: requested batch 122, specified, not designed)** (the gate decides from a word list and a token scan, so every judgement it cannot express as a rule costs an approval prompt, and the rules cannot be tuned per user)
- **§57. One model for the subagents** — **(NOT BUILT: requested batch 122, specified, not designed)** (an agent definition can pin a model and nothing can set one for the subagents as a class, so a cheap child runs on the parent's expensive model)
- **§58. Who the agent is when you ask it** — **(NOT BUILT: requested batch 122, specified, not designed)** (the chat system prompt is "You are a helpful assistant.", so the model knows nothing about the harness it is running in)
- **§59. Pick it instead of typing it** — **(NOT BUILT: requested batch 122, specified, not designed)** (removing a memory means reading a uuid out of the transcript and typing it back, one at a time; switching agent means typing an exact name)
- **§60. The Unified Harness Protocol** — **(NOT BUILT: requested batch 123, specified in slices, not designed)**
- **§62. The field that hid an agent instead of refusing it** — **(BUILT in batch 137)** (`spawnable` was an advertisement, so a spawn of a non-spawnable agent was refused by nothing: the catalog said absent and the tool said yes, and silence at the stranger tiers bought invisibility rather than unreachability)
- **§79. A demo nobody has to install** - **(IN PROGRESS: slice 1, `/connect`, BUILT in batch 163; slice 2, hosted modes, BUILT in batch 164; slice 3, the bridge, the bundle and the math tools' confinement, BUILT in batch 165; slice 4 designed, not yet built)** (the harness ran only where someone installed it, and connecting a model meant copying a template and editing JSON by hand; the owner asked for a public Hugging Face Space and ModelScope Studio kept current from `main`, a sign-in where a provider's own documentation invites one, and a private single-user mode with the full harness; HD1-HD10)
- **§78. A release tag that named one version while the run packed another** - **(BUILT in batch 162)** (`v0.3.0` and `v0.3.1` were pushed on commits whose `package.json` and `pyproject.toml` both said 0.2.0, and the `v0.3.1` publish run packed `@yahyahammad/venastine@0.2.0` and reported the prepublish check passed, because that check compared the two files with each other and nothing read the tag; it uploaded nothing only because no `NPM_TOKEN` exists. The owner ruled both tags releases whose bump was missed, left the files and the tags where they are, and put the comparison first in `scripts/prepublish-check.mjs`, before anything is packed; A78)
- **§77. The approval wait that gave up before the test that was answering it** - **(BUILT in batch 161)** (the `v0.3.1` tag run failed one pilot test, "tool never dispatched", after the same commit had passed twice: `conftest`'s autouse fixture shrank a modal's wait to 5s, half of `settle`'s 10s deadline, and 23 pilot tests act on an open modal inside that wait. Raised to 30s and pinned from below as well as from above; A77)
- **§76. Nine security findings the marathon deferred, and the answer each was waiting for** - **(BUILT in batch 160)** (the nine `security`-area issues batch 159 filed, each put to the owner with options and a recommendation: `**` unpacking refused by the math guard, an empty workspace refused before it resolves, a `.env` read and never loaded, the redaction vocabulary widened to the current vendor formats and three structural shapes, the word on each side of a session buffer's dropped middle moved into the gap, a hardlink into `.venastine/` refused by inode, a session's refused credential quarantined, bidi and zero-width characters revealed in approvals, and an MCP tool's description ruled third-party data in the system prompt; A68-A76)
- **§75. Thirteen alerts that read a name as a secret, and the gate nothing in the workflow could fail** - **(BUILT in batch 158)** (PR #200's first `CodeQL` check raised 13 high-severity alerts and every one was a false positive: ten came from a source heuristic that reads `args.secrets`, `_secret_twin_name()` and `KEY_PASSPHRASE` as secrets because of their names, and three were test assertions read as URL sanitizers; `tests/` is now skipped because it never ships, the eight in shipped code are dismissed with recorded reasons, and the same red check is the measurement TECHNICAL_DEBT 76 lacked -- the gate is GitHub's pull-request check, and the workflow header now says so)
- **§74. A gate that was green because of what one machine happened to contain** - **(BUILT in batch 157)** (CI's first run over batches 129-156 came back red on three check runs, and all three were one test: the push-channel gate drove `git check-ignore` in the WORKING TREE, where `logs/`, `output/` and `.ipynb_checkpoints/` exist because the harness creates them, so three directory-only patterns matched here and matched nothing in a fresh clone -- seventeen batches of green behind a branch whose gate had never met one; the same measurement found `__pycache__` declared for the git channel, carrying no rule in `.gitignore` at all, and the one value in the list that no sample could reach)
- **§73. Three cuts the redactor ran after, and the one result path that never passed it** - **(BUILT in batch 156)** (the review marathon's last three findings, closing its `FIX-NOW` queue: `web_search` and `arxiv_search` cut their third-party text and let `registry.dispatch` redact the piece that was kept, so a `ghp_` token straddling any of the three cuts survived at 36 of the 39 offsets it could straddle -- RB1's defect in the two tools RB1 did not reach, and §52's gap register had declined to solve it generally while two such tools were already in the tree; the per-LIST caps bounded 25 authors and not the 5,000 characters each could carry, and two of the six unbounded values were named by no finding, one of them the `arxiv_id` that both recovery URLs are built from, while the row COUNT both schemas advertise a bound for was enforced in neither, so a request for five results returned all 5,000 rows a provider chose to send; and `dispatch()`'s refusal early-return was the one result path that never passed the output redactor, which `docs/ARCHITECTURE.md` had already asserted it did -- measured, a credential the model put in an `agent_name` came back whole, on a path that also returns above the input scanner written for exactly that value)
- **§72. A consent screen the project decided the length of, and three fields a rule was stated over but never applied to** - **(BUILT in batch 155)** (the review marathon's config-and-trust surface, all three findings: the trust prompt -- the informed-consent gate for content that becomes instructions -- pasted `settings.json` and `mcp.json` whole, so 2 x 2.2 MB rendered as 4.5 MB in 30 lines with the `mcp.json` command the section exists to show at 99.9994% of the way in, while the section the finding cited as the bounded contrast was bounded on one axis of two and a 2 MB `AGENTS.md` written as one line walked straight through it; `_parse_md_file` states "ELEMENT TYPES TOO, not just the container" in a comment and `model`, `provider` and the elements of `allowed_tools` sat under it unchecked, so `model: [a, b]` loaded clean and raised `TypeError: unhashable type: 'list'` out of the effort cache; and a `config.yaml` hand-edited mid-session -- the workflow `explain()` recommends by name -- answered a `/config` with "the error is in logs/app.log", which is true and is an answer to a different question. The crash that finding reported had already been closed by batch 135 and was found by re-driving rather than by reading)
- **§71. A guard that compared two spellings of one id, and a cap whose two halves disagreed** - **(BUILT in batch 154)** (the review marathon's persistence surface, all four findings: tier 1 of the §27 thread classification -- the tier #82 calls authoritative over every clock comparison -- compared `str(thread_id)` as the producer records it against the hex spelling sqlmodel stores, so `'a-b' in {'ab'}` was False for every id there has ever been and the tier had never relabelled one row, while all four of its tests hand-wrote the same string on both sides; `max_injected_memories: 0` hid the `remember` tool and injected a "showing the 0 most recent of 2" header with a WARNING behind it on every turn, so the model was told that memories exist and are being withheld by a run that could never reach them; `list_threads` still carried the failure model batch 75 deleted from its two siblings; and the record's shape was stated as 9 fields and four data columns in thirteen live places after T2 and E13 each grew it by a column, including a BREAKING_CHANGES row promising a key-set assertion that did not exist)
- **§70. A trust grant that could not persist, and a file three documents kept promising about** - **(BUILT in batch 153)** (`/init`'s trust settle was put on both the success and the failure path by audit #95, and the settle itself can raise -- `grant_trust` writes the store and re-reads every file the grant covers -- from a call outside the `try` on both of them, so a trust-store I/O failure left both config files written, the project untrusted, the notice replaced by a traceback on the CLI, and on the failure path the "wrote N files before that" accounting #95 existed to add discarded with it; the pre-write capture had the same shape from the other end, where one unreadable file under `.venastine/` killed the command before it wrote anything, three functions below a sibling that catches the same error on the same file and degrades; and the context file section 44 deleted was still named as a live fact in fifteen places across eight files, eleven of them in the suite, including a promise that it "is never special-cased out of the hash" -- a guarantee about a file that is not in the hash because it does not exist)
- **§69. A consent prompt the model could repaint, and a refusal that read as a yes** - **(BUILT in batch 152)** (the CLI printed the security-critical half of every consent prompt with model-supplied terminal escapes in it -- the approval headline, the notice, the agent's stated reason, a reviewer's proposed correction and an MCP server's own description of its tool -- while the params block below them was safe because `json.dumps` renders an escape instead of emitting it; and `_signoff` returned the empty set for a line in which nothing named a candidate, which is the explicit `none` and which the loop spawns on, so a mistyped refusal authorised the spawn)
- **§68. Six places the suite described itself, and two guards that could pass while scanning nothing** - **(BUILT in batch 151)** (the suite's own machinery was described in six places the tree had made false: a count of non-function-scoped fixtures wrong since batch 110 in four phrasings, a grouping contract whose worked example was numerically wrong, a worker table naming the branch CI does not take, a stub inventory missing the one entry that is not an SDK, and an offline promise that eleven `wsl.exe` calls and three test bodies both contradicted -- while the guard deciding whether the whole suite was collected compared against a copy of `pytest.ini`, and the guard forbidding a sixth copy of `settle` globbed a cwd-relative path and passed from anywhere else having read nothing)
- **§67. The gate a child could turn off, and three strings nobody here wrote** - **(BUILT in batch 150)** (the child's approval overrides filtered the parent's dict and merged the agent's raw, so a `False` in a child's frontmatter turned off a gate the parent had set -- and the tool then left the sign-off notice as well as the gate; `disabled` had no safe constant and was given a named failure instead of one; a composed `mcp__<server>__<tool>` name nothing validated made every provider request malformed from one reachable server; a tool's description, title and input_schema arrived from third-party code uncapped and unnormalised into every model call; and the non-text placeholder fired only when no text survived, so a result with text AND an image dropped the image in silence)
- **§66. The type that stopped at the container, and a pass that answered about more than it was asked** — **(BUILT in batch 149)** (the payload boundary declared a field's container type and not the type of what it held, so three of six crash shapes survived it and three more scored a wrong number in silence; a boolean satisfied NUMBER, so `severity: true` charged the maximum critic penalty with no trace; and Pass 6b was asked about one batch, validated against every id in the run and applied to every claim in it, while the re-tiering beside it stayed narrow)
- **§65. Four guards measured against the wrong thing, and a cut the redactor ran after** — **(BUILT in batch 148)** (the shipped compaction strategy fell through the branch built for the state it reached, a floor applied with no reference to the window left models that can never compact saying nothing, the pipeline backstop fired EARLIER than the trigger it sits behind at every window below 200,000, #89's pin cap stopped tracking the trigger it is a fraction of, and a session's output was cut in four places with the redactor running after every one)
- **§64. Two numbers a provider promised and one key that meant two things** — **(BUILT in batch 147)** (an unknown tool name ended the turn and left the thread unresumable, a token count that was present and not a number defeated the guard that exists to catch exactly that, and Ctrl+C during a background session's wake turn unwound a model turn from wherever it landed)
- **§63. The write that did not land where the consent said** — **(BUILT in batch 146)** (a document NAME that is a link wrote outside the project under a consent naming the inside path, every write path translated line endings so the bytes that landed were not the bytes approved, and the one hard deny compared a path segment with exact case)
- **§61. Stopping a turn that is already running** — **(BUILT in batch 124)** (a turn going the wrong way is watched to its end or the harness is killed: nothing anywhere stops one, and both keys that look like they would end the process instead) (there is no way to drive this harness except by sitting in front of it, and the open standard that would change that has no concept of the approval gate this project is built around)

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
| **SS6-b** | **A quit stops the turn and the session wait, rather than waiting for a teardown that runs after it** (batch 145, S14-05/S14-06). `_wait_for_sessions` had three exits -- nothing live, the cancel token, `sessions.closing` -- and `closing` is set only by `SessionManager.close()`, reached from main.py's `finally`, which runs AFTER `run_tui()` returns; `run_tui()` is `App.run()`, which does not return while the worker is alive. So the only thing that would have stopped the loop ran after the thing it was waiting on, and the second ctrl+c answered "Quit and stop what is running?" with a hang. It reads the shutdown flag now, ABOVE the cancel branch, because a quit sets both and that branch promises results "with your next message" -- true for escape, false for a quit. `exit()` also asks the cancel token, which is what makes the line it prints true for a chat turn as well as a research run |
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
| **SS27** | **ANSI sequences are stripped in `core/`, with no terminal-emulator dependency.** Stripping precedes the buffer and every match, and carries a partial sequence across reads: measured, a 128 KB burst arrived in 74 chunks at arbitrary boundaries, so a sequence split across two is the ordinary case and not the corner. Full-screen programs that paint with cursor motion are not supported, and the tool description says so rather than leaving it to be discovered. There is no echo-removal clause: SS33 turns the echo off, so there is none to remove. **BOTH ENCODINGS since batch 145**, found by S15's question and fixed here because the stripper is shared: every sequence has an 8-bit form, `_sequence_end` keyed on ESC alone, and all seven C1 introducers reached the agent's buffer and the user's screen while the ESC spelling of each was removed. Not a judgement about them -- they were never considered. The rule is the one this entry already states, that a control must not reach a terminal, and which of two spellings a sender chose was never part of it |
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
| **SS61** | **A live secret VALUE is redacted from tool output unconditionally, whatever `redact_tool_outputs` says.** The master switch governs pattern GUESSES about other people's credentials, and a user who turns it off has chosen to see those raw. This is different in kind: a value the harness itself put into a process, coming back out of one, and handing it to the model is the harness leaking its OWN secret. It sits beside the depth cap, which `redaction_enabled()` already documents as staying fail-closed because it is structure rather than content judgment. Applied in `redact_output_text` -- above the switch, so both its consumers get it -- and again in `logging_setup`'s formatter, which is the fourth sink and the one that KEEPS what it writes. **Batch 144 made that one function rather than two copies, and found a fifth and sixth sink doing neither** (S07-01): `tui/app.py`'s log handler and its tool-error line applied the vendor patterns with NO value redaction, so the harness's own value reached the screen verbatim -- the exact case this decision calls unconditional. `redact_diagnostic_text` is the composition now, with five callers, and the two sites that had composed it by hand had also chosen opposite ORDERS (S07-05), which cost the tail of any stored value containing a vendor token. The values never leave `security/secrets.py`: the redactor calls it rather than asking for a list, so exactly one module holds plaintext |
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

### What batch 125 measured, which changed three of this section's own premises

Every line was run before anything was written.

- **THE OUTPUT IS ALREADY STORED, IN FULL, AND IT IS BYTE FOR BYTE WHAT THE MODEL RECEIVED.**
  `core/memory.add_tool_result` persists `str(result)` to the `messages` table keyed by
  `tool_call_id`, and `core/client.py:536` hands the provider that same `msg["content"]` -- the
  Anthropic, Google and OpenAI-compatible branches all send the string unchanged. So the retention
  question this section opened with does not exist: the archive is the record, it was redacted at
  dispatch by `check_output_policy`, and it survives a restart by construction. `SS18`'s promise
  reaches a one-shot call for free rather than needing a second mechanism. TR8.
- **WHAT IT COSTS INSTEAD IS READABILITY.** `str()` of a dict is a Python repr, so a shell result is
  stored as `{'stdout': 'hello\n', 'stderr': '', 'return_code': 0}` and the newline a reader came
  for is two characters. That is the whole of the price, and TR9 is what pays it.
- **THE VIEW SURFACE WAS MORE COMPLETE THAN TR4 KNEW.** `core/session_view.py` is already the
  shell-independent "what may be shown" policy module, returning `(role, text)` entries from two
  sources -- the live manager, or the archive keyed by CALL ID -- and `open_session_call` already
  resolves a tool line into a pane at press time, with the crumb, the disabled prompt and
  escape-to-close. THREE of the four shell tools reach it; the one-shot `shell` is the hole. So the
  ctrl+click half of TR4's choice was most of the way built and TR4 did not say so.
- **The `opens` plumbing is entirely generic.** `Transcript._arm_call` stylizes with
  `meta={"opens": (kind, id)}` and `on_click` posts `CallSelected(kind, id)`; neither inspects the
  kind. A third kind reaches the handler with **zero** widget changes.
- **`registry.opens()` was pinned as returning `()` in three tests** --
  `test_session_display.py` twice (`shell`, an unknown MCP name) and
  `test_interactive_sessions.py` once (`shell_input`) -- plus two more asserting the tuple a replay
  carries. Found by reading before the full run rather than by it, which is the batch-124 lesson
  about a fourth pinned site applied one batch later.
- **The archive's depth is already bounded, which is why TR9's fallback is not a hazard.**
  `check_output_policy`'s `_MAX_SCAN_DEPTH = 12` fails CLOSED, substituting a sentence rather than
  descending, so the repr `ast.literal_eval` is handed was depth-bounded before it was written.
- **`core/replay.py`'s `T4` is the constraint this section never cited, and it is the strongest
  one.** Tool results are skipped on a replay because "a grounding-heavy thread carries hundreds of
  kilobytes of fetched page text in its `tool_result` rows; replaying that would bury the
  conversation in the material the conversation was about." T4 is about what a replay PUTS IN FRONT
  of a reader; this section is about what a reader can ASK FOR. An inline expand would have been T4
  undone by a feature.

### Decisions (TR1-TR5)

| # | Decision |
|---|---|
| **TR1** | **This is TWO problems and both are in scope.** The command is abbreviated and the output is absent. Shipping the first alone leaves the more useful half missing, and the owner's request names both |
| **TR2** | **`param_digest` does not change.** Whatever is built is a SECOND rendering beside the digest, never a loosening of it. Three consumers read that function and the multi-line command it collapses once drew its remaining lines at column 0 underneath the tool line, outside the indent -- in all three. One producer, one job |
| **TR3** | **Retracted by default, and unmistakably the tool call's.** The owner's reason is that the user has already seen a complex command in the approval modal. The attribution half is the harder half and it is a requirement rather than a nicety: a block of shell output that reads as the model's own prose is the untrusted-content confusion the system prompt spends a paragraph on |
| **TR4** | **The mechanism is deliberately NOT chosen here, and the constraint that bounds the choice is now measured.** The owner deferred it, and the measurement above is what the two candidates have to be judged against: an inline expand costs a `reflow()` of the whole transcript and moves the reader's position, while a ctrl+click view reuses `registry.opens()` and the view surface §47 and SS14 already built. The second is the owner's own fallback proposal. Recording the measurement rather than the verdict is the point -- the decision is cheap to make with it and was being made without it. **RESOLVED in batch 125 as the ctrl+click pane -- see TR6**, and the deferral earned itself: the measurement that decided it was `T4`, which this section had not cited |
| **TR5** | **Whatever is chosen applies to all four shell tools, or the record says why not.** `shell`, `shell_background`, `shell_monitor` and `shell_interactive` differ in when their output exists, not in whether a user wants to see it, and four surfaces for one question is the drift EP6 names. **Overtaken by TR7**, which applies it to all THIRTY: the gap named here is not shell-specific, and four tools was the smallest honest scope rather than the right one |

### Decisions (TR6-TR15), batch 125

Four of these are the owner's, taken in the design round on 2026-10-05: the ctrl+click pane over an inline expand (TR6); every tool call rather than the four shell tools (TR7); parse the stored repr when it round-trips and fall back to it verbatim (TR9); and the TUI only, with the `D12` divergence recorded rather than fixed (TR14).

| # | Decision |
|---|---|
| **TR6** | **TR4's parked mechanism is the ctrl+click pane.** Decided on `T4` rather than on the `rerender()` cost, which is the measurement TR4 named and the weaker of the two: an inline expand moves the reader's scroll, but it ALSO puts tool results in the transcript, which `core/replay.py` refuses by name and for a reason that has not changed. The pane also answers both halves of TR1 with one affordance, because it draws the command through `check_output_policy` rather than through `param_digest` -- so TR2 holds without the pane having to work around it |
| **TR7** | **Every tool call opens its own call. No exception list.** `registry.opens()` returns a new `OPENS_CALL` as its FALLBACK, so the answer is never empty. The gap TR1 names -- a successful result is rendered nowhere -- was never shell-specific: `fetch_url`'s body, `arxiv_search`'s results and every MCP tool's output were equally invisible. Answered WITHOUT a registry lookup, deliberately, because `opens_thread` and `opens_session` are False for a tool this registry has never seen -- a runtime-registered MCP tool, or one removed between a thread being written and being replayed -- and neither is a reason to hide a stored result. The fallback is a property of the ARCHIVE, not of the registry. The cost is stated: arming no longer means "this line started something". The two `DIFFED_TOOLS` lines are armed too, and they are what makes the no-exception rule earn itself -- `§41`'s `X4` kept `content` off the transcript line and the diff that replaced it shows the CHANGE rather than the call, so the pane is the only place the untruncated call can be read |
| **TR8** | **Nothing new is retained.** Measured above: the archive already holds the exact string the provider was handed. There is no cap to choose, no eviction policy, no live-versus-stored split, and a restart costs nothing. `tests/test_call_view.py` pins BOTH halves of that measurement, so a change to `add_tool_result` or to a client translation branch fails there rather than in a pane quietly showing something the model never read |
| **TR9** | **`ast.literal_eval`, falling back to the raw string.** On success the dict's keys are drawn as labelled sections with strings exploded one entry per line; on failure the stored string is drawn verbatim, which is never WRONG because it is what the model read -- so the fallback needs no second mechanism behind it and is not a degraded answer. `literal_eval` evaluates nothing, and a hostile nesting cannot exhaust the stack because `_MAX_SCAN_DEPTH` already failed closed on depth before the string was written; what the fallback is actually for is a repr that was never a literal, `<Foo object at 0x7f...>`. Caught broadly on `_as_text`'s precedent: a display surface that raises on one odd row takes the whole pane with it |
| **TR10** | **A fourth pane, `#call-view`, and NOT for the reason the third had.** `#session-view` exists separately because the thread poll repaints `#thread-view` from the archive and would wipe anything else drawn there; nothing polls this one (TR11), so that argument does not apply and is not borrowed. The reason here is naming -- a pane called `#session-view` holding a `fetch_url` call is a name that lies -- and that `close_thread_view` resetting EVERY pane stays a statement rather than a hope, which is what makes a fourth pane cost one line. Same box in `app.tcss`, which is load-bearing: a hidden pane borrows the on-screen one's width |
| **TR11** | **No poll, and a running call says so.** A one-shot call produces nothing until it exits, so there is nothing to stream and the one view timer gains no third subject -- `_viewing_live_session` already answers None for any tag that is not `_VIEW_LIVE`, so the discipline was already written. `D20` persists the assistant turn with its `tool_use` blocks BEFORE anything runs, so a call clicked mid-flight shows its command and the sentence that its result is not in the archive yet, which is `open_agent_thread`'s said-rather-than-ignored rule |
| **TR12** | **An unresolvable thread or session falls back to the call view.** Two refusal sentences used to end those paths -- "that spawn has no thread to open -- it was refused" and its session sibling -- and both described exactly the state in which the call's own `{"error": ...}` answers the question, from the same archive the handler had just read. A line carries exactly ONE kind, so without this the three tools that open a thread or a session would have had no route to their own params and results at all. The refusal survives as the last resort, for a call that is not in the record |
| **TR13** | **Bounded at `session_view.tail_chars()`, which is `MAX_READ_CHARS`** -- already "how much output is one read" for `shell_output` and for a wake, so a second number here would be a second answer to one question. TWO BUDGETS of that one number, params and result separately, because a `write` whose `content` is a large file would otherwise eat the allowance before the result -- the half the reader opened the pane for. A cut section says HOW MANY characters are missing rather than that it was truncated, which is `PG4`'s complaint |
| **TR14** | **The entry-building lives in `core/call_view.py` for `D12`, and the CLI is not wired.** `core/session_view.py` is in `core/` precisely so both shells can render what a view shows, and `main.py` has never imported it -- the session view has been TUI-only since §49. This batch follows the rule for its own module and RECORDS the pre-existing divergence (TECHNICAL_DEBT 45) rather than quietly matching it or silently widening its own scope to fix it |
| **TR15** | **Params are redacted at view time; the result is re-run idempotently.** The two halves are NOT symmetric and that is the decision. The result was redacted inside `registry.dispatch`, so a second pass finds nothing and costs one walk -- run anyway, because that is what keeps the promise true of both sources rather than of one plus an assumption. The params were NEVER redacted at rest: `param_digest` redacts for DISPLAY and the archive keeps what the model sent, so this pane is the first place the raw arguments reach a screen. The tool name handed to `check_output_policy` for that pass is decorated `(call params)`, because its one WARNING says "result" and a credential found in the arguments would otherwise be reported as having been in the output |

### What settled the open questions

Each of the four was answered by a measurement rather than by a preference, which is why none of
them needed a second design round.

- **Inline expand against a ctrl+click view** -- the pane, TR6, on `T4` rather than on `reflow()`.
- **How much output is kept and where** -- nothing is kept, TR8. The question assumed a retention
  design because `core/shell_sessions.py` needs one for a LIVE session's in-memory buffer (SS12). A
  finished call has no live buffer to keep; it has an archive row that already holds the whole thing.
- **Whether the retained output survives a restart, and what SS18 means for a one-shot call** --
  it survives because it is the archive, and SS18's `NOT_KEPT` sentence turns out to be unnecessary
  here. That header exists because a live session's output is in memory and nowhere else, so a
  rebuilt session view genuinely cannot show what the session printed. A one-shot call's result was
  never anywhere but the archive, so there is nothing a restart loses and nothing to apologise for.
- **Whether `core/replay.py` renders the same thing** -- it renders the same LINE, armed with the
  same `(kind, call id)` from the same `registry.opens()`, so a replayed tool line opens exactly
  what a live one does. It still does not render the result inline, which is `T4` and is the point.

### Gap register

- **Ctrl+click is a mouse gesture and there is no keyboard route to a call.** The same is true of
  every `opens()` target since §47, so this batch inherits the gap rather than adding it -- but it
  inherits it for thirty tools instead of three, which makes it worth stating. A `/calls`-style
  picker over the current thread's calls is the obvious later answer, and `ThreadPickerScreen` is
  the shape.
- **Arming no longer means "this line started something".** TR7's stated cost. Every tool line is
  now underlined, and a reader who learned the old meaning has to learn a new one.
- **A result whose repr is not a literal reads badly and the header cannot say whether it failed.**
  TR9's fallback is honest and it is not pretty, and `_status` deliberately answers "finished"
  rather than searching the raw text for `error` -- a guess wearing the clothes of a fact is the
  instrument-that-lies shape batches 118 and 119 each paid for once.
- **`param_digest` is still the only thing the research pipeline's tool rows show.** It has three
  consumers (TR2) and this batch gave a second rendering to one of them. The pipeline trace and
  `core/replay.py`'s own CLI rendering are unchanged, which is TR14's divergence in its other form.

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

---

## §60. The Unified Harness Protocol

**Added in batch 123, from the owner's request** to find out what UHP compliance would take and to
record it if it is appropriate and possible. It is both, with one caveat large enough that it is
stated here rather than discovered during implementation: **UHP has no concept of approval**, and
the approval gate is the most heavily designed subsystem in this repository.

Specified in slices, none of them built. The design round that locks the remaining shapes happens
when a slice is picked up, which is the §49 cycle.

### What UHP is, verified against the specification

It is the **Unified** Harness Protocol, not Universal -- HarnessRouter's open standard, Apache-2.0.
Version **`2026-09-28`**, marked *"Draft standard -- stable enough to build on, versioned so it can
change safely"*. Three versions exist (`2026-08-11`, `2026-09-12`, `2026-09-28`): the standard is
days old and has moved twice in under two months, which is why UH6 slices it the way it does.

Eleven chapters plus a schema chapter carrying OpenAPI 3.1 and JSON Schema 2020-12, and a runnable
conformance suite of **76 checks in three cumulative classes**:

| class | checks | what it covers |
|---|---|---|
| **core** | 40 | capability discovery, version negotiation, authentication, the error envelope, harnesses, models, task execution streaming and not, the event stream, sessions, cancellation, reserved request fields |
| extended | +8 | session listing and inspection, file input, artifacts, download headers, path-traversal probes |
| full | +26 | harness create / update / delete, skill-folder round trip, MCP and disabled-tool persistence, session sharing, plugins |

The task surface is deliberately OpenAI-Responses-compatible: `POST /v1/responses`,
`GET /v1/responses/{id}`, `POST /v1/responses/{id}/cancel`, `DELETE /v1/responses/{id}`,
`GET /v1/harnesses`, and `GET /v1/uhp`, which is the one endpoint a server may serve unauthenticated.

### Why it fits this codebase unusually well

`core/events.py`'s `LoopEvent` maps almost field for field onto the SSE vocabulary a conformant
server MUST emit. This is not a coincidence worth leaning on -- both are descriptions of the same
loop -- but it does mean the translation is a mapping rather than a redesign.

| `LoopEvent` | UHP |
|---|---|
| `token_delta` | `response.output_text.delta` |
| `thinking_delta` | `response.reasoning_summary_text.delta` |
| `tool_call_start` `{id, name, input}` | `response.output_item.added` carrying a `function_call`, then `response.function_call_arguments.delta` / `.done` |
| `tool_result` `{id, result}` | a `function_call_output` item, matched by `call_id` |
| `stop_reason: "max_steps_reached"` / `"token_budget_exceeded"` | `status: "incomplete"` -- UHP's budget-truncation status, exactly |
| `final_response` | `response.completed` |
| `notice`, `retract` | nothing; both are display-only by their own decisions and correctly drop |
| **`permission_request`** | **nothing, and there is no equivalent to add it to** |

The harness descriptor is the shape of an `AgentDef`: `systemPrompt`, `mcpServers`, `skills`,
`plugins`, `disabledTools`, `maxStep`, `timeoutSeconds`, `environment`, `defaultModel`. UHP's
`environment` is §49's backend concept. A UHP session continued with `previous_response_id` is a
thread. UHP's "a server MUST NOT run two tasks concurrently in the same session" is `_busy`.

### The caveat, which is the whole of the risk

UHP's lifecycle defines exactly five statuses -- `in_progress`, `completed`, `failed`, `incomplete`,
`cancelled` -- with no pause, no awaiting-input, no `requires_action`, and terminal states are one
way. `security.md` says outright that the specification provides no human-in-the-loop system:
*"Tool calls are visible as they happen; a client can surface or gate them."*

**A UHP task cannot stop to ask.** Compliance therefore means serving the headless posture, and UH2
is the honest response to that rather than a fix for it.

### What was measured before any of this was written (2026-10-03)

Every line was run.

- **Per-child isolation works.** Four children spawned in parallel with distinct `AGENT_WORKSPACE`
  each resolved their own `WORKSPACE_DIR`, `OUTPUT_DIR` and `DB_PATH`, all four reporting
  `WORKSPACE_DIR_EXPLICIT: true`. **`OUTPUT_DIR` is DERIVED from `AGENT_WORKSPACE`** with no second
  variable (`config_schema.py:771`), `DB_PATH` takes `APP_DB_PATH`, and the log file takes
  `AGENT_LOG_FILE` (`logging_setup.py:133`). Three environment variables give a child complete path
  isolation, and that is what UH3 rests on.
- **Child cold start is ~3.8 s** for the full headless import set: `config` 0.46 s, plus storage
  1.1 s, plus `core.loop` 2.4 s, plus `tools.registry` 2.3 s, all of them together 3.8 s. UH4 is
  what turns that into a per-SESSION cost rather than a per-task one.
- **Two tools go dark headless under the shipped posture** -- `remember` and `write_project_doc` --
  and **both are grantable**, so a client can pre-authorise them per task. Thirty tools are
  registered and twenty-eight are reachable headless as shipped. The shipped posture disables
  `shell`, `write` and `edit` outright, so this picture changes when a user enables them; that has
  to be measured in the design round rather than assumed from this number.
- **`starlette`, `uvicorn` AND `sse-starlette` are already hard requirements of `mcp==2.0.0`** --
  `pip show mcp` lists all three, and `starlette 1.3.1` / `uvicorn 0.51.0` are installed in this
  environment now, by that path. Declaring them would add nothing to an install, which is exactly
  the argument `requirements.txt` already makes for `cryptography`.
- **SSE throughput, both candidate stacks, loopback, 2,000 events per client:**

  | | 1 connection | 4 connections | 16 connections |
  |---|---|---|---|
  | stdlib `ThreadingHTTPServer` | 14,529 ev/s, TTFB 37 ms | 12,067 ev/s, TTFB 9 ms | 19,688 ev/s, **TTFB 18 ms** |
  | `starlette` + `uvicorn` | 25,811 ev/s, TTFB 49 ms | 57,065 ev/s, TTFB 29 ms | 49,376 ev/s, **TTFB 244 ms** |

  uvicorn is 2.5 to 4.7 times faster in raw throughput and **thirteen times worse on tail latency at
  sixteen connections**, because one event loop serialises connection setup where threads accept in
  parallel. Against the real workload neither figure matters: sixteen sessions each streaming at
  500 tokens per second -- faster than any current model -- is 8,000 events per second, under the
  stdlib floor of 12,067. The HTTP layer is not the bottleneck in either stack. The model is, by
  orders of magnitude.
- **There is no HTTP server anywhere in the tree** -- no fastapi, flask, aiohttp or uvicorn import,
  no `http.server`, no `BaseHTTPRequestHandler`, in any module.
- **There is no cancellation of an in-flight turn.** `shell_kill` ends a shell session and ctrl+c
  quits the process; nothing stops a running chat turn or research pass. UHP core requires
  `POST /v1/responses/{id}/cancel` ending in `status: "cancelled"`, never `failed`, with partial
  output retained. **It is the one core capability with no existing machinery at all**, which is why
  it is slice 0.
- **One process is one project, one posture, one workspace.** `file_ops.WORKSPACE_ROOT` is captured
  at import; `security.posture` latches on first read ("reading is what closes the door"); the
  container-runtime probe, the tool registry and the agent, skill and memory managers are all
  module-level singletons. That is what makes the supervisor the right shape rather than a taste.

### The slices

**Slice 0 -- cancellation, and it is not UHP-specific.** A cooperative cancel token checked at the
loop's step boundary and threaded into `core/client.py`'s stream; partial output retained; a
terminal reason distinct from failure. One mechanism, three consumers: the TUI's stop key, the CLI,
and later the endpoint. It ships useful whether or not anything above it is ever built.

**Slice 1 -- the child protocol.** A headless run that emits `LoopEvent`s as JSON Lines on stdout
and takes a control channel for cancellation, isolated by the three environment variables measured
above. The child must not read stdin, which batch 121 made expressible: `_release_terminal()` is the
named handover, and a child that never starts a pump cannot deafen anything.

**Slice 2 -- the server, core conformance.** `GET /v1/uhp`, `GET /v1/harnesses`,
`GET /v1/harnesses/{id}`, `POST /v1/responses` streaming and not, `GET /v1/responses/{id}`,
`POST /v1/responses/{id}/cancel`, `DELETE /v1/responses/{id}`, the error envelope, bearer
authentication, `Idempotency-Key`, `session_busy`, and the event vocabulary with `sequence_number`
strictly monotonic and no gaps. Verified by running the suite's own `--class core`.

**Slice 3 -- extended conformance.** File input, artifacts, download headers, path-traversal probes,
session listing and inspection. The research pipeline's `output/<run_id>/` tree is already the
artifact store. **Full is out of scope and recorded as out of scope**: harness CRUD, plugins and
session sharing are a product, not a protocol obligation.

### Decisions (UH1-UH12)

| # | Decision |
|---|---|
| **UH1** | **A native server, core class first** (owner decision), with extended as slice 3 and full explicitly out of scope. The alternative was a separate runner wrapping the CLI, which is what HarnessRouter already does for other harnesses -- less work, and it leaves this harness non-compliant while something else speaks for it |
| **UH2** | **UHP mode is HEADLESS, and the operator is told so in words.** Approval-gated tools are hidden by the rule that already exists -- `response_channel is None`, §18's headless filter -- and a client pre-authorises per task through `metadata`, which is the `--grant-tools` route that already works with no terminal. The server says at startup what that means and names the tools it is withholding. The warning is the owner's explicit requirement rather than a nicety: serving the gate's headless behaviour over a network without saying so is exactly the invisibility this project keeps refusing, and the headless notice (§18) already exists for the same reason one level down |
| **UH3** | **A supervisor with one child process per session**, isolated by `AGENT_WORKSPACE`, `APP_DB_PATH` and `AGENT_LOG_FILE`. Measured rather than hoped: four parallel children, distinct workspaces, `OUTPUT_DIR` derived for free. Every singleton keeps its current meaning, the posture latch and D17's trust gate are untouched, and sessions genuinely run in parallel -- which is the owner's condition for the architecture being worth having. The alternative, unwinding the singletons, reaches into the three subsystems carrying the most locked decisions |
| **UH4** | **One child per SESSION, not per task.** The 3.8 s cold start is then paid once per session, and a `previous_response_id` continuation reuses a live child -- which is also what UHP requires in its own words: the new task runs "in the same session, with the same working directory and its files" |
| **UH5** | **stdlib `http.server` with threads, decided on architecture rather than on speed.** The server is a thin relay in front of the supervisor, and keeping it synchronous keeps true the property AGENTS.md states ("synchronous throughout, except the MCP bridge and the Textual event loop"). Recorded with both measurement tables above and with the fact that `starlette`, `uvicorn` and `sse-starlette` are ALREADY in `mcp`'s hard closure -- so this is reversible at zero install cost the day hand-rolled SSE proves the wrong trade, and the measurement that would justify reversing it is tail latency, where stdlib currently wins |
| **UH6** | **Cancellation is slice 0 and is not UHP-specific.** It ships usable from the TUI and the CLI whether or not the server is ever built. That is the deliberate hedge against a draft standard that has moved twice in two months: if UHP disappears, slice 0 was still worth doing, and no other slice is. **BUILT in batch 124, and recorded as §61 rather than here** -- this sentence is the reason: a capability both shells wanted anyway does not belong inside a protocol section, and §60 would otherwise read as part-built while none of the protocol exists |
| **UH7** | **A UHP harness object is an `AgentDef`,** with `base: "venastine"`. `systemPrompt`, `mcpServers`, `skills`, `disabledTools`, `maxStep` and `environment` already exist here as agent frontmatter, §17's MCP config, §19's skills, `ToolPermissions`, `max_steps` and §49's backends. Nothing new is invented to answer discovery |
| **UH8** | **One translation point from `LoopEvent` to the SSE vocabulary,** not one per endpoint and not one per consumer. Fix at the producer; `param_digest` with its three readers is the standing example of what the alternative costs |
| **UH9** | **The capability document is GENERATED from what is wired, never hand-written.** The specification's own MUST, and the reason is its own: *"A server MUST NOT advertise a capability it does not implement -- advertising is a promise, and a client that trusts it and receives a `404` has been lied to in a way it cannot recover from."* A hand-written capabilities object is a second source of truth for what the code does |
| **UH10** | **The protocol version is pinned and re-verified deliberately** -- D22's reasoning applied to a specification instead of a package. The conformance suite runs at a named version, and moving it is a step someone takes on purpose, because a draft that changes underneath CI makes CI's result depend on when it ran |
| **UH11** | **Off by default, and loopback by default.** It is a network listener on a machine that runs agent code. It is not in the npm default path, it does not start without being asked, and the posture badge says it is running (UN3's rule: a badge describes what this process may do, and a user who reads it afterwards has read it too late) |
| **UH12** | **The trust store is the one piece of shared mutable state between children.** `~/.config/venastine/trusted_projects.json` is a read-modify-write through `write_json_atomic`, and atomic replacement does not make concurrent read-modify-write safe -- two children granting trust can lose one update. Grants are serialised, and a child never grants trust on its own: the server is handed trusted projects or refuses, because D17's prompt is a human decision and there is no human on this path |

### What must be settled before any slice is built

- **TLS.** `architecture.md` says "All requests MUST be HTTP/1.1 or later over TLS", and no loopback
  exemption was found in the chapters read for this section. Read it out of the machine-readable
  schema first: it decides whether a local deployment needs a certificate story, and that is a large
  difference in what slice 2 costs.
- The exact `GET /v1/uhp` response body and the capabilities object, from the schema rather than
  from prose.
- Whether any core check requires CROSS-session concurrency. It decides whether UH3's supervisor is
  required for conformance or merely better, and `session_busy` is defined only for the same session.
- What `--mode research` is in this model. A ten-pass run is a task, but its artifacts and trace do
  not fit `output[]` cleanly. Probably a second harness `base`, and probably not in core.
- Whether `shell` under a headless run is denied at call time or hidden up front once a user enables
  it. Measured for the shipped posture, not for an enabled one.
- Who issues the bearer token and where it is stored. Not `providers.json`, which holds OUTBOUND
  credentials; this is an inbound one, and the two have different threat models.

### Gap register

- **The approval gate has no representation in the protocol, and a client gating by watching the
  stream is advisory rather than enforcing.** By the time a `function_call` event is visible the
  tool has already run. This is the protocol's gap and not this harness's, and it cannot be closed
  from inside a conformant server: the five statuses are closed and terminal states are one way.
  UH2 is the honest response, not a fix.
- **The standard is days old and has moved twice in two months.** UH6's slicing is the hedge and the
  only one available; a locked decision record written against a moving draft is the thing this
  project's conventions exist to prevent, so UH1-UH12 are deliberately about THIS codebase's shape
  rather than about the protocol's wording.
- **3.8 s cold start is client-visible on the first task of a session.** UH4 makes it once per
  session rather than once per task; it does not make it disappear, and a warm pool is the obvious
  later answer.
- **A `harness_error` is a `200` carrying `status: "failed"`.** This loop deliberately lets
  exceptions propagate -- `core/events.py`: "there is deliberately no error variant" -- so the server
  is where an exception becomes a status, and that boundary has to be exactly one place or the two
  will disagree about what failed.

---

## §61. Stopping a turn that is already running

**Built in batch 124**, from §60's UH6 -- which locked that cancellation ships first and on its own
merits, as *"the one slice worth building even if UHP disappears tomorrow"*. It is recorded here
rather than inside §60 for UH6's own reason: this is not UHP-specific, and a protocol section
claiming credit for a capability both shells wanted anyway would read as part-built while none of
the protocol exists.

UHP's requirement is met on the way past -- a cancelled task ends `cancelled`, never `failed`, with
partial output kept -- but nothing here is shaped by it.

### What the code did before this

- **Nothing could stop a turn.** Measured: no occurrence of `cancel` anywhere in `core/loop.py` or
  `core/client.py`. `shell_kill` ends a shell session (SS13) and that was the whole of it.
- **Both keys that looked like they would ended the process instead.** `tui/app.py` bound `ctrl+c`
  to `arm_quit` / `confirm_quit` and `escape` to `close_thread_view`. In the CLI a
  `KeyboardInterrupt` during a turn is not caught by that turn's `except Exception`, so it unwound
  to `main()`'s `finally` and exited 130 -- which the banner advertised, because exiting was the
  only thing the key did.
- **Abandoning the generator already worked, and was already correct.** `_run` catches `Exception`
  and not `BaseException`, because *"a GeneratorExit is an ABANDONED generator (#42, a TUI quitting
  mid-turn), not a failed run"*. `tests/test_pipeline_events.py` pins the pipeline half of the same
  behaviour. It is insufficient for three reasons and only three: it fires at a yield, so it cannot
  reach a socket read waiting for a first token or a tool call in progress; it leaves the thread
  with a user message and no reply; and it leaves a pipeline record at `running` for ever.

### What was measured (2026-10-04)

- **A cooperative token is FORCED, not preferred.** A turn runs in
  `run_worker(..., thread=True, name="agent-turn")` and Textual cannot cancel a thread worker. The
  key is pressed on the UI thread while the generator is mid-`next()` on the worker, and calling
  `close()` on a running generator from another thread raises
  `ValueError: generator already executing`. A flag is the only thing that can cross.
- **Closing the stream early already releases the connection.** `core/client.py` yields from inside
  `with client.messages.stream(...) as stream:`, so breaking the `for` unwinds the context manager.
  Nothing had to be written to avoid leaking a socket.
- **The loop had to grow its own accumulator.** That same branch builds its text from
  `stream.get_final_message()`, which a stream broken off part way never produces. There was no
  partial answer anywhere to keep until `_run_steps` began keeping one, which is why CN4 is a change
  to the loop and not merely to what it reports.
- **A fourth `stop_reason` is a new VALUE, not a tenth field.** `tests/test_pipeline_events.py`'s
  nine-field pin is untouched, and `main.py` and `tui/app.py` already print
  `[stopped early: <reason>]` for anything that is not `"complete"` -- so two display sites needed no
  change at all.
- **Escape was already expected.** `PromptInput` gates its own escape binding with `check_action` and
  says why: *"with no panel open they are the keys they have always been: tab moves focus, escape
  reaches whoever wants it."* CN8 is that sentence being taken at its word, so no `priority=True`
  was needed and the suggestion panel keeps the key while it is open.

### Decisions (CN1-CN12)

| # | Decision |
|---|---|
| **CN1** | **A cooperative `CancelToken` in `core/cancel.py`**, passed down beside `response_channel`, `activity` and `grant_budget` -- the shape this project already uses for an out-of-band object a run needs where a LoopEvent cannot reach. A LEAF module with no project imports, for `core/approval.py`'s reason. **Not a ContextVar:** threads do not inherit their parent's context, which `_dispatch_parallel` and `tui/app.py`'s `_consume` both already pay for, and a token the tool pool could not see would be a stop switch that silently missed the longest part of a turn. SET-ONCE, so the reason kept is the one that actually stopped the run |
| **CN2** | **Read at FOUR sites** -- the step boundary, inside the stream's token loop, between tool groups, and each shell's own drain. The step-boundary check sits ABOVE `response = None`, so what it reports is the previous step's real response rather than an empty one; the stream check is guarded on `response is None`, so a call that completed is never discarded; the group check is CN6. **Before the first token the SDK is blocked in a socket read and nothing cooperative can reach it** -- a stated limit, surfaced as "stopping", never discovered |
| **CN3** | **`stop_reason: "cancelled"`, and never `"failed"`.** `mark_turn_failed` is not reused and nothing raises: an exception would reach `_run`'s frame and stamp a failure on a turn nobody saw fail, which is the distinction that frame already draws for a GeneratorExit |
| **CN4** | **The partial answer is kept, text only.** `_run_steps` accumulates the deltas itself, because no provider branch can supply them after a cut. A half-formed tool call is DROPPED -- a truncated arguments blob is not a call. Empty text persists nothing: a turn stopped before its first token said nothing, and an empty assistant row is one every later request has to carry. This honours D20 rather than carving an exception out of it, and it keeps the transcript and the thread saying the same thing |
| **CN5** | **A synthesised response reports an EMPTY usage dict, not zeros.** The provider sends usage with its final message and a cut stream has none, so the honest statement is "not measured" -- and the two stay distinguishable, since `"input_tokens" in usage` is False here and True for a step that genuinely cost nothing. A zero is the instrument that lies, which batches 118 and 119 each paid for once. The CLI's stopped-early line reads that same empty dict and says the stopped step is not counted |
| **CN6** | **A tool call already running finishes, and the turn stops after it settles.** Nothing gains the power to kill a tool. The alternative was instant for `shell`, which has `KILL_USER`, and not for anything else -- one gesture with two speeds depending on which tool happened to be running. The shells say "anything already running finishes first" rather than pretending otherwise |
| **CN7** | **A child inherits the switch.** `cancel` joins `_INJECTABLE_PARAMS`, so `spawn_subagent` receives the parent's token and a stop reaches a run that happens INSIDE a tool call -- the one place a LoopEvent cannot. That list is `activity`'s own argument, one line up |
| **CN8** | **TUI: `escape`, app level, no `priority=True`.** The SECOND app-level binding on the key, working the way the `ctrl+c` pair does: whichever `check_action` refuses with **`False`** gives up the slot. `False` and never `None` is measured rather than stylistic -- `active_bindings` is keyed by KEY and skips a binding only on `is False`. Precedence falls out of machinery that already exists: suggestion panel, then thread view, then cancel. The one-sentence rule survives -- escape means back to the conversation, and stopping a turn is that |
| **CN9** | **The predicate is the live token, not `_busy`.** `_busy` is also true through the post-turn session wait, where SS2 keeps `ctrl+b` reachable precisely so the user can stop what is running there. Two keys meaning adjacent things would make neither clear, so escape stops the CONVERSATION and `ctrl+b` stops the SESSIONS, and neither quietly does the other's job |
| **CN10** | **CLI: Ctrl+C stops the turn; at the prompt it still exits.** A signal HANDLER rather than an `except KeyboardInterrupt`, and the difference is what the thread looks like afterwards: an interrupt unwinds from wherever it lands, which can be between a `tool_use` D20 has persisted and the `tool_result` that pairs with it. Installed per turn and restored in a `finally`, so `_await_sessions` keeps catching its own interrupt and Ctrl+C there still stops the SESSIONS -- the aim of that key in that place was already right |
| **CN11** | **Cancelling under an open approval modal needs no new path.** `core/interaction.decode()` already substitutes the declining default for *"a modal torn down by shutdown"*, so the call is denied and the token is seen at the next boundary |
| **CN12** | **The research pipeline is DEFERRED, not out of scope** (owner), and so are compaction and the `/grill-me` one-shot. See the next section for what the pipeline has to learn first |

### The decision that is a correctness property rather than a preference

CN4's `unrun` is the part of this section that would have been a defect rather than a missing
feature, and the repository had already named it from the other side.

D20 persists the assistant turn, with every one of its `tool_use` blocks, **before** anything runs.
A cancel that stopped between two tool groups would therefore leave a response whose calls were
recorded and whose results were not -- and `_settle_one`'s own comment says what that is:

> an abandoned generator left the `tool_use` D20 already wrote with no matching `tool_result`, which
> is M4's pairing broken from the other side and an unresumable thread.

So a cancelled turn **answers every call the model made**, including the ones it declined to run,
with the `{"error": ...}` shape a denied call already takes. `tests/test_cancel.py` asserts three
`tool_use` blocks produce three rows when only the first ever ran.

### What the deferral has to learn before it is lifted

`core/reasoning/orchestrator.py`'s `_check_truncation` reads

```python
stop = getattr(response, "stop_reason", None)
if stop in (None, "complete"):
    return
```

-- so **every** other value is truncation: with text, trace it and run the next pass; with no text,
raise. A pass returning `"cancelled"` would be read as degraded rather than stopped, and the
pipeline would continue into the next pass on a run the user had ended.

That reader is correct exactly as long as CN12 holds, because no pass can return the new value
today. It is written here, and in `core/events.py` beside the value itself, so that lifting the
deferral starts from the gap rather than finding it.

`tests/test_pipeline_events.py::test_abandoning_the_generator_leaves_the_record_running` moves with
it. Its assertion -- that an abandoned run honestly stays `running`, because *"flipping it to
'failed' would make an interrupted run indistinguishable from a broken one"* -- is the right answer
for abandonment and the wrong one for a cancellation, which has a status of its own to go to.

### Gap register

- **A cancel cannot interrupt a blocked socket read.** The window is from the request leaving to the
  first token arriving, which on a thinking model is tens of seconds and is exactly when a user is
  most likely to change their mind. Cooperative cancellation cannot close it; what the shells do is
  say they are stopping, so a slow stop reads as a stop rather than as a dead key.
- **A long tool call delays the stop by its own duration** (CN6). A `shell` call running a test suite
  is the realistic worst case, and `shell_kill` is the existing control for it.
- **A cancel during a retry backoff costs one more model call.** Batch 91's retry sleeps in
  `provider_errors.wait(delay)` and nothing checks the token across it, so the next attempt starts
  and the stop lands on its first token. Not a correctness problem -- the extra call is accounted
  and persisted like any other -- and deliberately left rather than fixed here, because the obvious
  fix interacts with the retraction: `partial` holds the failed attempt's text, which batch 113
  already told the screen to take back, so a stop that broke out of the backoff would have to
  discard it explicitly or persist something the reader watched disappear.
- **The cancelled step's spend is unmeasured and stays unmeasured** (CN5). It is reported as absent
  rather than as zero, which is the most that can honestly be said.
- **The TUI binding has no footer entry.** `show=False` is forced: the footer would need `None` from
  `check_action` to grey rather than reflow, and `None` would keep the key slot and shadow
  `close_thread_view`. One of the two had to give, and a wrong label on a live key is worse than no
  label.


## §62. The field that hid an agent instead of refusing it

**Built in batch 137**, from the review marathon's S13-07 -- a finding filed
`OWNER-DECISION` rather than as a defect, because the behaviour matched the
record exactly. §32's A3/A4 made `spawnable` an *advertisement*, and the code
implemented that faithfully. The owner ruled to reverse it.

### What the code did before this

- **`refusal_reason` never read the field.** It checks depth, then params,
  then `manager.get(name) is None`, then `spawn_targets` -- so
  `spawn_subagent` would spawn any agent the loader had discovered, by name.
  Measured across all three tiers with a real roster: `internal`
  (`spawnable: false`), `quiet` (project tier, field omitted) and `mine`
  (user tier, field omitted) were each absent from the catalog and each
  spawned.
- **So the two halves disagreed about membership**, and only one of them was
  ever shown to a model. For those three agents `agent_catalog_text()`
  answered "absent" and `refusal_reason` answered "spawn it".
- **The refusal's own message named the wrong test.** *"Unknown agent: … is
  not in the available agents catalog"* -- `manager.get` reads the ROSTER;
  `agent_catalog_text()` is the catalog. The one string a model ever sees
  about membership described a test that was not being run. That is
  `ARCHITECTURE.md` §11's shape 9 inside the function that owns the
  contract, and it was wrong whichever way this decision went.
- **Nothing pinned the old behaviour.** All 91 tests across
  `test_spawnable.py`, `test_spawn_preflight.py` and `test_agents.py` passed
  unchanged after the reversal, which is the clearest statement of the gap:
  the record said advertisement-only, the code did advertisement-only, and
  no test asserted that a non-spawnable agent could in fact be spawned.

### Design Decisions Record — §62 (A16)

| id | decision |
|---|---|
| **A16** | **`spawnable` is a REFUSAL, not only an advertisement**, reversing §32 A3/A4's "it decides what the model is TOLD exists and nothing else". `refusal_reason` refuses a spawn of any agent whose `spawnable` is not `True`, after the unknown-agent check and before `spawn_targets`. **A3's reading of silence is unchanged** -- undeclared at the user and project tiers still reads as `false` -- what changes is the consequence. A3 argues that "a stranger's silence must get the safe answer", and the measured consequence of reading as false was that the agent became INVISIBLE rather than unreachable: a project-tier agent omitting the field was off the prompt a user might read and callable by any text that named it, including the project's own root `AGENTS.md` under `use_project_context`, a skill body, or an injection in a fetched page. For the careless author the default was safe; for the adversarial one it was the quieter option, which is the opposite ranking from the one A3 is reaching for. **The sibling had already chosen this answer**: `spawn_targets` is a refusal, and the loader's own comment says so -- *"It is a REFUSAL, unlike `spawnable`'s advertisement."* Both fields describe a spawn that should not happen; one refused it and one hid it. And D24's rule ("advertising an uncallable tool is not harmless: the model keeps choosing it") has a mirror this design did not apply: a catalog that hides a REACHABLE agent is the same defect read the other way |

### What A16 deliberately does not change

- **`/agent` is untouched.** `agents/tui_commands.py` goes through
  `manager.get` and `manager.names()`, neither of which consults
  `refusal_reason`, so a human can still select `grill-me` or `plan` by
  hand -- which is the distinction A3 encodes and the half worth keeping.
  `test_a_non_spawnable_agent_is_still_reachable_by_hand` pins it and is
  unchanged by this batch.
- **The harness's own direct runs are untouched**, for the same reason:
  `compactor`, `initializer` and `pipeline-reviewer` are invoked by the code
  that needs them rather than through the `spawn_subagent` tool, and
  `refusal_reason` is wired only as that tool's `refusal_check`
  (`tools/registry.py`). All three are `spawnable: false` and all three
  still run.
- **`max_steps` still REPLACES rather than narrows**, and C6 still
  intersects a child's tools with its parent's. A16 is about which agents
  the spawn route admits, not about what one does once admitted.
- **The shipped roster needed no edits**, which was checked rather than
  assumed: eleven agents ship, six are `spawnable: true`, and the only
  `spawn_targets` declaration in the tree is `plan`'s four
  (`explore`, `review`, `writer`, `test`) -- all four spawnable. A
  `spawn_targets` entry naming a non-spawnable agent would now be a route
  that can never fire, so `test_every_shipped_spawn_target_is_itself_spawnable`
  sweeps the whole roster for that rather than asserting it of `plan` by
  name, which is how A13 broke.

## §63. The write that did not land where the consent said

**Built in batch 146**, from the review marathon's file-and-document-tools
surface: seven `FIX-NOW` findings across `tools/builtin/file_ops.py`,
`tools/builtin/project_docs.py` and `security/protected_paths.py`, plus one
defect no report names.

Three of the answers are DECISIONS rather than fixes, and the owner ruled all
three. Two of them were ruled against the reports' own suggested shapes,
because measuring the suggestion first is what showed the suggestion was
half of one.

### What the code did before this

- **A document name that was a link wrote outside the project.**
  `project_docs.write_run` joined the allowlisted name onto the project root
  and opened it -- no `realpath`, no `islink` -- while `read_run` in the same
  module resolved before deciding and had always done so. `approval_notice`
  rendered the unresolved join, so the consent named the inside path and the
  bytes went to the link target. git stores symlinks natively, so
  `ARCHITECTURE.md -> ~/.bashrc` in a cloned project needs no privilege to
  arrive.
- **The suggested fix for that admits the mechanism the report's own probe
  used.** The report drove the harm through a HARDLINK, because this machine
  cannot create symlinks, and then proposed resolving the destination.
  Measured: `os.path.realpath` returns a hardlinked path unchanged,
  `os.path.islink` is False, and `_resolve_in_project` answers "inside the
  project" -- correctly, because it is. A hardlink is not a link to
  anything; it is the file.
- **And the same hole reached the one hard deny**, which no report names. A
  `write` through a hardlinked name put an `mcpServers` object into a
  protected `settings.json` whose direct spelling is refused -- the file
  whose own rationale says it "names commands the harness spawns at
  startup". Found by asking S17's question of S04's file.
- **All three write paths translated line endings.** Mode `"w"` with no
  `newline=`, so every `"\n"` became `os.linesep`: `write` could not
  reproduce bytes it had just read, and `edit` replacing one span of an LF
  file rewrote every line ending in it. Measured on a six-line document, one
  line changed and five of six lines' bytes changed -- under an approval that
  had been shown a diff of the one.
- **The suggested fix for THAT breaks a case that was correct.** It said
  `newline=""` on the three writes alone would leave `edit` "preserving
  whatever the file already had". Driven across LF, CRLF and mixed files, it
  does the opposite: `edit` read with universal newlines, so `content` was LF
  in memory whatever the file held, and writing that back converted a CRLF
  file to LF. The same defect pointing the other way.
- **The one hard deny compared a segment with exact case,** and its docstring
  claimed the Windows case was handled upstream by `realpath`. True only for
  a path that already EXISTS. With no `.venastine/` yet, `.VENASTINE/mcp.json`
  resolved to itself, carried no known segment, was auto-approved, and
  created the protected directory under the spelling the guard did not know
  -- after which `realpath` canonicalised to the ON-DISK name and BOTH
  spellings were admitted in that project permanently.
- **`read`'s `lines_returned` counted the lines it CHOSE.** The character cut
  ran afterwards: 500 reported, 313 delivered, and the clamp branch's own
  message said "Use offset=500 to read further" -- 187 lines and 29,920
  characters reachable by no call the model could construct.
- **Both file_ops gates were the one pair in the tree not total over a JSON
  object.** They run ABOVE `dispatch()`'s containment, and
  `os.path.join(WORKSPACE_ROOT, None)` on a well-formed `{"path": null}`
  raised out of the registry: a lost turn alone, a durable `status='failed'`
  in a research pass, one call's error result in a parallel batch. Swept
  afterwards: 1,572 gate calls over 30 tools, and after the fix nothing
  raises for a wrong-typed value anywhere.
- **Six modules justified `params.get` with a mechanism that was false,**
  citing each other as the authority, and none of them mentioned the layer
  where a raise does still end the turn -- which was the layer in their own
  surface.

### Design Decisions Record — §63 (A17-A19)

| id | decision |
|---|---|
| **A17** | **A write REPLACES a name rather than writing through it**, via `json_store.write_text_atomic` -- so `os.replace` is load-bearing for a security property and not only for durability. Chosen over the report's containment-only shape, which admits a hardlink, and over refusing a destination with `st_nlink > 1`, which keeps the link and refuses a layout that may be deliberate. This is the house pattern already: `json_store` exists because four stores had four copies of the dance, and `mcp_client`'s -- holding a record of CONSENT -- wrote with a bare `open(path, "w")`, so the file tools' text write became the FIFTH consumer of one private helper rather than a sixth copy. What it costs is stated where the function lives and filed as register item 92: on Windows a destination another process merely has OPEN fails the rename, so a `/init` write of a document the user has in an editor can now fail where it previously overwrote. A write that fails loudly beats one that half-succeeds, and the alternative is item 90's write half reopened. **Resolution is still required and still does the other half** -- a symlink or junction pointing OUT is refused before any of this, in the sense the unknown-name branch means, and the approval notice renders the resolved path so a consent cannot name a file the bytes will not reach |
| **A18** | **A protected path segment is compared CASE-FOLDED on every platform**, in the one place the segment is spelled (`protected_paths.folded_segments`), read by all three consumers -- `file_ops`' deny, `project_docs`' wider denylist, and `shell`'s prompt, including its RAW-TOKEN branch, which `protected_segment` does not reach and which is the branch for a token `realpath` cannot help with. Unconditional rather than per-platform because it is the fail-closed direction and a case-insensitive mount on Linux is a real configuration, and because a platform test is a second thing the three consumers each have to get right -- which is the drift the "ONE LIST, THREE CONSUMERS" discipline in that file exists to prevent. **The cost is stated correctly rather than as the report states it:** for the file tools this list is the hard DENY, so a false positive costs a refusal with no prompt and no override, not "one prompt" -- a directory genuinely named `.VENASTINE` on a case-sensitive filesystem is now unreachable to `read`, `write` and `edit`. For the shell consumer, which only ever ASKS, the same fold does cost one prompt |
| **A19** | **`edit` matches the file's real bytes, and retries an LF-spelled span translated.** Reading and writing with `newline=""` makes the uniqueness contract a statement about the bytes on disk, which is what the contract implies and what keeps a mixed-ending file (this repository has one) byte-identical outside the replaced span. The retry is there because the STRICT version is unactionable, which was measured rather than argued: `read_run` does `f.read().splitlines()` and then joins with `"\n"`, so every file reaches the model as LF whatever it holds, and a model cannot see a CRLF file's endings through any tool here. A matcher that compared bytes only would refuse every multi-line edit of such a file, naming text the model has no way to produce. So an `old_text` containing `"\n"` that finds no match is retried with its endings translated, and `new_text` moves with it. **The schema's contract sentence changes with the fix**: "an exact, uniquely-matching span" becomes exact-modulo-line-endings, and the count is taken after the translation, so two CRLF-separated copies of an LF-spelled span are two matches and the edit is refused |

### What §63 deliberately does not change

- **`MAX_READ_LINES` and `MAX_READ_CHARS` are untouched.** The cut moved to a
  LINE boundary, because `offset` and `count` are in lines and a
  character-aligned cut makes the last line partial and the resume point
  lossy. The ceilings themselves are governed by
  `test_every_documented_ceiling_matches_the_live_config` and are fine.
- **One case still loses data, and says so.** A single line longer than the
  whole ceiling is delivered CUT, because keeping whole lines only would
  return nothing and leave the model nowhere to go. The message says the rest
  of that line is not reachable by offset -- the honest answer for a tool
  whose paging unit is a line, and preferable to a byte-offset parameter on a
  globally-denied tool.
- **The READ half of the hardlink is open**, as register item 90. Nothing
  available here distinguishes a hardlink from a file, so the deny cannot see
  it; the atomic write is what closes the write half, and the tests assert on
  the protected file's BYTES rather than on the tool's answer for exactly
  that reason.
- **`PROTECTED_SEGMENTS`' membership is unchanged**, and so is the rule that
  the file tools DENY while the shell ASKS. A18 is about the comparison, not
  about the list or about either consumer's policy.
- **No containment was added inside `registry.refusal_reason` or
  `registry.approval_needed`.** The S04 report declines that and names the
  reason on both sides: a swallowed refusal fails OPEN where a swallowed
  notice fails closed. The gates were made total instead, which is what
  `shell.py`'s own gate has said for a long time -- "total, like everything
  else the gate calls" -- and `tools/base.py` now carries the one copy of
  why, including the half about gates that none of the six copies had.

## §64. Two numbers a provider promised and one key that meant two things

**Built in batch 147**, from the review marathon's loop-and-client surface:
four `FIX-NOW` findings across `core/loop.py`, `core/client.py` and the
tests that could not see either, plus one finding under a finding that the
report asked for and did not have.

Two of the answers are DECISIONS rather than fixes, and the owner ruled
both. The second ruling went against all three options this batch offered,
for a reason that turned out to be the TUI's own measurement from batch 57
arriving at the CLI.

### What the code did before this

- **An unknown tool name ended the turn.** `registry.dispatch` raises
  `ValueError` for a name it does not hold -- deliberately, because the two
  exception types separate "you misnamed it" from "policy blocks you" --
  and `_dispatch_one` caught only `ToolCallDenied`. So the raise left the
  loop, `_run`'s frame stamped the thread `turn_failed`, and in a research
  pass the orchestrator's `except Exception` recorded `status='failed'`.
  The caller the registry frames as a programmer is, here, a language
  model: a name it invented, a name whose MCP server went away between
  turns while its schemas are still in the model's context, or a name a
  prompt catalog advertises and nothing registered.
- **And the thread was left unresumable**, which is the half that outlives
  the turn. D20 has already persisted the assistant row carrying the
  `tool_use` block before any dispatch happens, nothing writes a
  `tool_result`, and M4's pairing is broken with nothing in
  `core/compaction.py` or `core/memory.py` that repairs it --
  `_settle_one`'s own words for that state are "an unresumable thread".
- **The containment that was documented for this case could not be reached
  by it.** `_dispatch_parallel`'s NA14 comment said "what reaches here is
  the narrower set: an unknown tool's ValueError, or a failure in the
  approval machinery itself" -- but `registry.parallel()` answers False for
  a name it does not hold, so an unknown call is always a group of ONE and
  a group of one takes the sequential branch. Defect shape 9 inside the fix
  for defect shape 3.
- **A token count that was PRESENT and not a number defeated D21.** The
  GOOGLE branch read `getattr(usage_meta, "candidates_token_count", 0)`,
  and on the pinned `google-genai==1.0.0` every usage field is
  `Optional[int] = None` while a bare usage object is TRUTHY. So
  `if usage_meta:` passed, the default never fired, and `usage` carried
  None: D21's guard compares `== 0` and `None != 0`, so a stream that
  reported nothing did not raise even under the flag, and
  `core/loop.py`'s `usage_in + usage_out` died with a `TypeError` three
  layers from the cause.
- **The other two branches had the same defect for a reason no annotation
  shows**, which the report asked to have audited and which changed the
  shape of the fix. `anthropic.types.Usage.input_tokens` and
  `openai.types.CompletionUsage.prompt_tokens` are annotated `int` and
  REQUIRED -- and both SDKs parse wire JSON through pydantic's
  `construct`, which skips validation. Measured:
  `construct_type({"output_tokens": 7}, Usage)` yields
  `input_tokens=None`, and anthropic's own stream accumulator guards
  `input_tokens` with `is not None` against that same `int` annotation
  while assigning `output_tokens` unconditionally. So the fix is one reader
  for three branches rather than a line in the branch the defect was found
  in.
- **Ctrl+C during a wake turn unwound a model turn.** CN10 installs a
  signal handler per turn and restores it in `run_chat`'s `finally`, and
  `_await_sessions` -- which runs the wake turns -- is called after that
  `finally`. So the default handler was live, `KeyboardInterrupt` rose from
  wherever it landed, `_run`'s frame catches `Exception` rather than
  `BaseException` and passed it straight through, and a press that landed
  inside a wake's tool dispatch left exactly the unanswered `tool_use` CN10
  exists to prevent. Two docstrings described this key and each was correct
  when written: `_await_sessions` said "the interrupt is aimed at what is
  running, not at the conversation", and SS5 then made a wake a turn --
  after which `wake_conversation`'s own docstring says "it IS that run
  continuing".
- **The GOOGLE branch was covered only against a double that cannot refuse
  anything**, and the one assertion about the translated parameters
  encoded a model of the field no version of the SDK has had:
  `parameters == {"type": "object"}` is true of the conftest's fake, which
  stores the dict, and false of the real `FunctionDeclaration`, which
  builds a `types.Schema` whose `.type` is the enum `Type.OBJECT`. It was
  also the one input that could not discriminate -- `{"type": "object"}` is
  a fixed point of `_reduce_for_google`, so no reduction, however broken,
  could have failed it.

### Design Decisions Record — §64 (A20-A21)

| id | decision |
|---|---|
| **A20** | **Under `supports_stream_usage`, a token count that is not a number RAISES, and the coercion happens in one place for all three branches.** D21 says a stream must not silently degrade token accounting; what it compared was `input == 0 and output == 0`, which a PARTIAL report defeats without reporting anything usable. The flag is the per-provider statement that this provider DOES report usage, so a missing number is that promise broken, and the widened rule is what D21 already said rather than a new policy. Chosen over coercing to zero and leaving the comparison alone -- the report's shape -- because the reachable case the report itself names is `usageMetadata` omitting `candidatesTokenCount`, which that shape accepts silently with the output count as zero. **It cannot break a case that works today**: every input it raises on currently dies with a `TypeError` in `core/loop.py`, so the change is in the diagnosis, not in the outcome. Two messages are kept distinct, and the nothing-at-all wording is kept verbatim so the guards matching on it keep their meaning. With the flag FALSE only the coercion happens and both counts are zero, which is the opt-out's whole purpose -- `providers.json.example` ships GOOGLE false, so the blocked-completion shape still runs. `_stream_usage` returns two ints always, which is what makes a belt in `core/loop.py` unnecessary rather than merely unfashionable: fix at the producer |
| **A21** | **Ctrl+C in the session wait stops the SESSIONS and never the turn, and it stops them once the running turn has finished.** The key keeps the one meaning `_await_sessions` has always advertised; what changes is that it no longer delivers it by unwinding a turn. For the duration of each wake turn that function installs a handler of its own which DEFERS the interrupt -- CN10's own "the current step finishes first", pointed at the sessions instead of the turn -- and restores it in a `finally`, so a press while merely waiting still takes the `except KeyboardInterrupt` that was always there. **Ruled against binding the key to the turn**, which was this batch's recommendation, and the owner's reason is the TUI's own measurement: a user copying or pasting around a running turn can press ctrl+c by accident, which is why batch 57 settled on "ctrl+c copies when there is something to copy, and starts a quit when there is not" and why a quit there takes two presses. Also ruled against one key doing both jobs, which CN9 refused for the TUI (*"escape stops the CONVERSATION and `ctrl+b` stops the SESSIONS, and neither quietly does the other's job"*). A stopped wait returns to the prompt with the sessions killed and anything that arrives afterwards held for the next message, which is SS19 unchanged |

### What §64 deliberately does not change

- **`registry.dispatch`'s exception taxonomy is untouched**, and so is its
  unknown-tool raise. That raise is the right answer for a caller who can
  read a traceback; the loop is the layer that knows its caller is a model,
  which is why the containment is there and not in the registry. The
  specificity is `registry.is_registered`, asked on the exception path
  only: a bare `except Exception` would stop `dispatch()` being the
  fail-closed backstop it is documented as, and matching on the message
  would be a parser where G2 says a token check will do.
- **No pre-flight filter of `response.tool_calls` against the registry.**
  The S10 report declines it and names the reason: it would hide the name
  from the model instead of telling it, so the model would reissue the same
  call forever.
- **`continue_conversation` still takes no `cancel`**, and the research
  pipeline is still unstoppable through it. That is CN12's half and the S12
  scope's call; this section's finding only establishes that a WAKE turn
  could not be stopped, which it now can -- by the harness, at a boundary,
  never by this key. NOT filed as a register item, because CN12 already
  carries it: that deferral names "the research pipeline ... and so are
  compaction and the `/grill-me` one-shot", and this function's production
  callers are exactly `core/reasoning/json_retry.py`,
  `core/reasoning/review.py`, `core/reasoning/orchestrator.py` and
  `core/compaction.py` -- the things CN12 names and nothing else.
- **CN10 itself is unchanged**, including what Ctrl+C means during an
  ordinary CLI chat turn. The owner's reason for A21 applies to that
  binding too -- the CLI has no selection to check and no two-press
  confirmation, which is what the TUI added for exactly this -- and
  reversing a locked decision is its own batch with its own decision
  record. Filed as register item 94.
- **The conftest's fake `genai` still validates nothing.** Batch 132 ruled
  on that in the fake's own docstring ("This fake is NOT `extra="forbid"`
  and does not validate -- it cannot, and that is what
  `tests/test_sdk_conformance.py` drives the REAL package for"), and S10-07
  raised it again as an open question. The answer stands: the fake accepts
  anything and the conformance file is the only place that may assert about
  a pinned type's shape.

## §65. Four guards measured against the wrong thing, and a cut the redactor ran after

**Built in batch 148**, from the review marathon's compaction-and-sessions
surface: six `FIX-NOW` findings across `core/compaction.py`,
`core/session_wake.py`, `core/session_view.py` and `core/session.py` -- plus
two files the report assigned to other scopes, because the measurement said
the severe half of one defect was there and no other scope had measured it.

Three of the answers are DECISIONS rather than fixes and the owner ruled all
three. The third ruling NARROWED what the report suggested, which is the
first time in this marathon a ruling has made a fix smaller.

**Four of the six are one shape.** A guard computed against a quantity that
stopped being the quantity it was named for:

| guard | measured against | should be |
|---|---|---|
| the `all-pinned` branch | the configured STRATEGY | the segment it is about |
| the derived trigger's floor | nothing about the window | stated when it exceeds it |
| the pipeline backstop | a fixed 20,000-token margin | the trigger it sits behind |
| #89's pin cap | the configured trigger | the trigger that FIRES |

Each was correct when written. Each stopped being correct somewhere else:
batch 16 flipped the default strategy to `chain`, batch 127 raised the
floor, and batch 44 moved the trigger off the constant three of them still
read. That is the marathon's shape 9 four times over, in one file.

### What the code did before this

- **The shipped compaction strategy fell through the branch built for the
  state it reached.** `history_through` excludes pinned rows (AC2), so when
  every row newly swept by the watermark is pinned the segment is empty --
  and the `all-pinned` branch tested `if not segment and not chaining:`,
  while `chaining` is true under `compaction_strategy: chain` whenever a
  checkpoint exists. Measured through the real `compact()` with three pinned
  turns in a ten-turn thread: the compactor was handed
  `"Summary of everything before this point:\n<previous summary>\n\n"` and
  nothing else, at a 379-character target, and the stored checkpoint went
  from **2,489 characters to 23**. Reported as `status="folded"`,
  `kind="compaction"`, *"0 earlier messages compacted into a summary"* -- a
  success line for a durable 99% loss that compounds geometrically on every
  repeat. The no-progress guard twenty lines above names this exact harm as
  its own reason for existing ("chain would be handed an empty segment and
  summarize its own previous summary, degrading it a little on every
  attempt"); it catches a watermark that has not MOVED and cannot catch one
  that moved across a region which is entirely pinned.
- **A model that can never compact said nothing about it.**
  `_dependent_floor()` is `max(warning_margin_tokens, keep_recent_tokens) +
  1` = 8,001 under the shipped settings, and it is applied with no reference
  to the window. At or below 8,000 the trigger it produces is ABOVE the
  window, and `used` is the provider's own count of what it was sent, so it
  can never reach it. Measured on a 4,096-token window: `should_compact`
  answers `"warn"` at every value from 1 to 4,097 and `"compact"` at none of
  them, because `warn_at` collapses to 1 down there -- so the single line
  the user sees is *"Approaching the compaction threshold"*, from the first
  measured turn, forever, and false. TECHNICAL_DEBT 42's own fix section
  rejected exactly this outcome ("compaction would then silently never run
  on that model, which is the quiet-failure class this project keeps
  closing") for the containment it declined; the floor it chose instead
  produces it directly. `/window` refuses anything at or below 20,000 and
  the shipped table's smallest entry is 200,000, so the way in is source 1:
  what a provider SAID, which has no floor at all, and 8,192 is the
  commonest window a self-hosted or gateway endpoint reports.
- **The backstop fired EARLIER than the trigger it sits behind.** M6 is
  about ORDER -- a research pass is headless and unattended and each pass
  already returns a distillation, so routine compaction there spends on a
  judgment call nobody is watching -- and the backstop was a flat
  20,000-token margin while batch 44 made the ordinary trigger a clamped
  share. Measured: 4,000 tokens earlier at 64,000 and at 131,072, 11,808
  earlier at 32,768, and at or below 20,001 the old `max(1, ...)` clamped it
  to **1**, so every evaluation of every step of every pass asked
  `compact()`. The shipped table is why nothing caught it: every entry is at
  or above 200,000, where the two numbers were equal or already in the right
  order. `/window`'s own refusal message describes the clamped state
  precisely ("not a window anyone means to set") and guards the command
  rather than the number a server asserts -- the same asymmetry
  TECHNICAL_DEBT 42 found for the trigger and closed for the trigger alone.
- **#89's pin cap was 20,000 for every model.** `pin_measurements` computed
  `PIN_MAX_TRIGGER_FRACTION` times the CONFIGURED trigger, and since batch
  44 the trigger that decides when a thread folds is derived per model.
  Measured against the trigger that fires: **250%** of it on an 8,192-token
  window, where one pin can protect more than the whole trigger and floor
  the thread permanently above it -- the exact state #89 exists to prevent,
  reached without anyone typing a number -- and **8%** on a 262,144-token
  one, where `pin` refuses requests the stated policy allows. The cap's
  VALUE was unchanged by batch 44 and the PROPERTY was not, and `AGENTS.md`
  said the first thing in a way that implied the second ("so #89's cap is
  unchanged"). The stated obstacle -- "this function has no (provider,
  model)" -- was one declared parameter away: `parent_run` is already in
  `registry._INJECTABLE_PARAMS` and already carries both.
- **A session's output was cut in four places and the redactor ran after
  every one of them.** The tail is cut by the producer at `MAX_READ_CHARS`,
  cut again by the wake's per-session division, cut again by the view's
  display budget, and paged for `shell_output` -- and in each case the slice
  was taken first and handed to `check_output_policy` after. A pattern only
  matches what it can see whole. Swept through the real redactor at every
  interior cut position: the three credential SHAPES leak at each of the
  three tail cuts -- the ones where the pattern matches a
  field-name-plus-value construct and only the value is the secret, so the
  value survives whole inside the kept half -- and the leaked value travels
  into the wake text, the persisted harness row, the rebuilt view and
  `/copy all`. RB1 settled this ordering for `fetch_url` one section
  earlier; `§52`'s gap register declined to solve it generally and named the
  future tool it would happen to, and `§49` had already shipped that tool
  before the sentence was written.
- **`core/session.py` cited a symbol that has not existed since batch 75.**
  Its *MODULE STATE* paragraph claimed the "same posture as
  `core/compaction.py`'s `_compacting` flag" -- a flag batch 75 replaced
  with a `threading.local()`, deliberately NOT aliased, under a comment
  arguing against the sentence that sends a reader there. The behaviour was
  sound; the precedent had been reversed.

### The finding under the findings: the paging route

S11-05 named two more cut sites, assigned them to the S01 and S16 scopes,
and said the leak would be severity S2 "if the same ordering were shown to
be reachable on the paging route, where the model can ask for adjacent
pages -- that is S01's measurement to make". S01 never made it.

Measured here: **the paging route leaks all seven shapes, not three** --
including the fixed-length vendor tokens that survive every other cut,
because the model asks for the second page itself and holds both halves.
`ghp_` leaks at every interior offset, `AKIA` at every one, `AIza` at 3..40.
A page boundary is not a discard, which is what makes it different from a
tail cut: nothing is thrown away, the split is handed to the reader in two
pieces, and a reader that can request the next page is the one reader who
can put them back together.

So the scope went to all four sites on the owner's ruling, and the fix moved
to the producer for the three that read a tail: `_finish` redacts what it
stores, which covers the wake, the rebuilt view and `_kill_report` -- a
consumer that never passed through the wake's redaction at all.

### Design Decisions Record — §65 (A22-A24)

| id | decision |
|---|---|
| **A22** | **#89's pin cap is `PIN_MAX_TRIGGER_FRACTION` of the trigger that FIRES for this model, resolved the way `thresholds()` resolves it.** `pin_measurements` takes an optional `(model, provider_name)` and `tools/builtin/pin.py` declares `parent_run` to pass it; with no pair the binding-free read is the fallback, so a call from outside a run is unmoved. Chosen over adding the derived trigger to the existing `min()`, which closes only the unsafe direction and leaves the cap at 20,000 above a 53,000-token trigger -- 2% where the policy says 50% -- and over keeping an absolute 20,000 and correcting the two documents that call it a fraction. #89's own argument is about the trigger, and the property it bought is intact at every window: the cap is never more than half of what the thread folds at. **One behaviour changes direction**: with the pair, a RAISED session `/trigger` now raises the cap, because a raised override IS the trigger that fires; the old `min()` erred strict because it could not tell whether the number applied to the model in front of it |
| **A23** | **A session's output is redacted before every cut it goes through, at all four sites, and the layer that cuts is the layer that redacts.** The producer redacts what it stores (`_finish`), the wake redacts the whole tail before the per-session budget slices it, the view redacts before the display budget, and the paging route redacts the parts before a page is cut out of them -- `OutputBuffer.page` takes an optional `clean`, so the monitor's own search still matches on RAW output, which is the rule the design already recorded. **The head/tail seam is a cut too** and is redacted across: the parts are joined when nothing has been evicted, because that seam sits at 10,000 characters on every session, inside the first page of every read. **The offsets then describe the redacted document**, which is RB3's rule rather than a compromise -- 210 characters of drift on a measured 120,000-character buffer, and every offset a surface speaks is a position in the text the reader was given. Chosen over fixing only the two sites the report names, which leaves the boundary that produced them intact one layer down, and over a second redaction pass at each consumer, which is what RB4 declined for `source_corpus` and declined correctly. Cost measured: 6.5ms for a full 200,000-character buffer against 4.7ms for one ordinary tool result |
| **A24** | **The floor says so when it leaves no usable headroom, once per (provider, model), and only where the FLOOR is what bound the trigger.** Two wordings for one condition, because the remedy differs: at or above the window compaction can never fire, and just below it the thread folds with almost nothing to grow into. Both name `warning_margin_tokens` and `keep_recent_tokens`, since lowering them is the one remedy that works. **Narrowed from what the report suggested**, which was to warn whenever `window - trigger` is below one bounded read: measured, that also fires at 16,384 and 32,768, where the headroom CAP bound it, the cap is deliberate (see `headroom_for`) and the only remedy is a bigger model -- so a line naming two settings would name the wrong thing, on every stock small-window install. The floor binds at or below a 10,666-token window and nowhere above it, which is where this warning fires and stops. The excluded case is register item 96 rather than a silence. The dedup is a set of its own, keyed on the PAIR: the same model name behind a gateway and behind its own vendor can report different windows |

### What §65 deliberately does not change

- **The floor itself, and `context_limit`'s source order.** The floor stays
  because the raise it removed was worse, and WS6/WS10 lock the inversion
  that puts a provider's own number above the shipped table. Distrusting
  source 1 would defeat the case source 0 exists for; saying the
  configuration cannot work is the deliverable.
- **`COMPACTION_PIPELINE_BACKSTOP_TOKENS` is untouched**, and so is
  `_input_budget`'s own `max(1_000, ...)` floor, which is honest about being
  lossy. The backstop's margin is still what decides above 200,000 -- the
  ordering expression binds only where the margin would have broken the
  order, which on the shipped roster is one entry by one token.
- **The backstop is ordered against `derived_trigger`, not against the fully
  resolved working-set trigger.** It cannot raise, which matters on a path
  whose own posture is that a number it cannot compute must never become the
  reason a thread does not compact; and M6 speaks about the derived default
  rather than about a number a human typed. A configured trigger high enough
  to still invert the pair is one that is itself near or above the window,
  and a reachable backstop under an unreachable trigger is the right way
  round.
- **Which rows `history_through` excludes, and M9's re-inclusion.** Pinned
  rows are still never summarized and still come back verbatim; the
  `all-pinned` branch now reports the state instead of spending a call to
  reach it. An explicit `/compact` is refused there too, for the reason the
  no-progress guard refuses one: degrading a summary is not a tighter
  compaction of it.
- **`PIN_MAX_TRIGGER_FRACTION`, and what `pin` refuses beyond the cap it
  reads.** The fraction is the policy; this section only makes the
  denominator the number the policy names.
- **The monitor still matches on RAW output**, and `_match_slice` is
  deliberately left raw. Redacting what is SEARCHED would stop a
  credential-shaped line waking the agent at all, which is the rule
  `core/session_wake.py`'s header already states: redact what is returned,
  not what is matched.
- **`MAX_READ_CHARS` and the per-session division are unchanged.** The
  budget now counts characters the model will actually see, which is what a
  budget was for.
- **The eviction boundary is not closed, and cannot be by this shape.** Half
  of a credential that straddled the buffer's dropped middle was never kept
  by the process, so no later pass can rejoin it -- but both halves ARE
  shown, on either side of a stated gap, to a reader who can ask for both.
  A stream has no "whole document" to redact exactly once. Register item 95.

## §66. The type that stopped at the container, and a pass that answered about more than it was asked

**Built in batch 149**, from the review marathon's research-pipeline
surface: both `FIX-NOW` findings, **S12-01** (the S2) and **S12-02**, across
`core/reasoning/payload_validation.py` and `core/reasoning/orchestrator.py`
-- plus `tui/screens.py`, which no report reaches, because the record a
S12-01 crash leaves behind is openable and opening it exited the
application.

Three answers are DECISIONS and the owner ruled all three. Two of the three
rulings made the fix LARGER than the report asked for, and in both cases the
measurement is what moved them: the report's suggested shape was **measured
insufficient**, not merely improved on.

**The shape.** A declared type that stops at the container, in a module
whose entire job is to declare types:

| field | declared | what pure code does with it |
|---|---|---|
| `entities` | nothing | builds a set, then SORTS it |
| `asserted_by_candidates` | nothing | `len(set(...))`, then divides |
| `per_claim_flags` values | nothing | `len()`, then multiplies |
| `key_entities_or_subjects` | nothing | `len()` |
| `severity` | `NUMBER` | `1.0 - severity` |

The first four had no declared type at all and the fifth had one that a
boolean satisfies. B4's rule is that a check earns its place by having a
demonstrated consequence; every row above has one, and the module had
written the rule without finishing the inventory.

### What the code did before this

- **Six crash shapes and three silent wrong answers, all through the real
  `validate` and the real consumer.** The crashes: `entities` as null, a
  number or a boolean (`TypeError: 'NoneType' object is not iterable`, from
  Pass 3a's `sorted({e for c in factual_claims for e in c.entities})`),
  `asserted_by_candidates` the same at Pass 5's
  `len(set(claim.asserted_by_candidates))`, and a `per_claim_flags` value
  as a number or null at `w["assumption_flag_penalty"] *
  len(claim.assumption_flags)`. Each lands in a stage that makes **no model
  call**, so there is nothing to retry with, and the orchestrator's except
  block turns it into a durable `status='failed'` record.
- **And three that did not crash, which is worse.** A STRING is iterable, so
  `"entities": "madrid"` became six entities, each one letter, each one a
  thing the 3a prompt then asks the model to search for ("search each
  ONCE"). `"per_claim_flags": {"c001": "abc"}` charged a THREE-flag
  assumption penalty for one string. `"key_entities_or_subjects": "abc"`
  reported *"3 key entities anticipated"* from one word. And on an ensemble
  run `"asserted_by_candidates": "abc"` scored `len(set("abc")) / 2` = 1.5,
  which clamps to **full agreement** -- so a claim no candidate
  corroborated was scored as unanimous.
- **A declared container type does not close it, which is what the report
  assumed.** S12-01's suggested shape says "presence plus container type
  closes the crash". Measured with `entities` a LIST: `["madrid", 1947]`
  raises `'<' not supported between instances of 'int' and 'str'`,
  `[["madrid"]]` and `[{"name": "madrid"}]` raise `unhashable type`. The
  pure code downstream does not merely iterate what it is given; it HASHES
  and SORTS it, and a container type says nothing about that.
- **A boolean is a number in Python, and `severity` is subtracted from
  one.** `isinstance(True, (int, float))` is True, so `"severity": true`
  passed the boundary and Pass 5 computed `1.0 - True` = 0.0 -- the
  **maximum** critic penalty, the harshest value the field can take -- with
  no trace line and no corrective retry. `false` took the other extreme the
  same way. The exclusion already existed twice in the tree
  (`source_scoring._as_number`, `confidence_scoring.source_quality`), in
  both cases guarding a value that was supposed to have been rejected at
  this boundary.
- **Pass 6b was asked about one batch and applied to the whole run.** Its
  input is `[vars(c) for c in flagged]`; its payload was validated against
  `[c.id for c in run.claims]` and applied over `run.claims`, while the
  re-tiering on the next line was deliberately scoped to `flagged`. Driven
  on a two-claim run: a HIGH claim the payload never should have named came
  out with `grounding_status='ungrounded'` and `sources=[]`, still tiered
  **HIGH**, its breakdown still recording `source_count: 1` and
  `source_quality: 0.9` for the source it no longer had. That breaks B10's
  stated property -- that the breakdown alone lets a reader recompute the
  score -- and it is the same stale-field pair `review_override` handling
  was built to prevent, one field over.
- **The one line the report points at does not fix it.** S12-02's "Where"
  names `claim_ids=[c.id for c in run.claims]`, and narrowing that is the
  first thing anyone would do. Measured: a payload naming **the batch plus
  one extra id** -- the shape a misbehaving model actually sends -- passes
  `claim_ids=[c.id for c in flagged]` unchanged, because `_check_ids` makes
  a TOTAL mismatch the error and a partial one deliberately not (B5, and
  that policy is right: a model getting nine of ten ids correct must not
  cost a run nine passes). The apply is the fix site; the id narrowing is
  worth doing anyway, because it makes 6b behave as 6a already did when
  NOTHING matches.
- **`_apply_grounding` was not the only wide half.** `_ground_and_score`
  calls `score_grounding_sources(run, ...)` without its `claims` argument,
  and that function's own first line is *"Rewrite every claim's
  `grounding_sources` with computed scores"*. So every 6b round rescored
  claims D1 had already finished with. Driven with the corpus grown by that
  round -- which is what a grounding round does -- a claim the payload
  never named had its `quote_verified` rewritten from `None` to `True`.
  **The parameter that narrows it has existed since §45 and no caller had ever
  passed it.**

### The finding under the findings: the record a failure leaves is openable

S12-01's severity argument is that the failure is loud and contained --
"traced exception, durable failed record, no silent wrong report". The
durable record is the half worth following. The orchestrator's except block
calls `update_pipeline_run(run.run_id, run, status="failed")` with a
snapshot of the partial run, so the claim carrying `assumption_flags: 123`
is **persisted**, JSON keeps it byte-for-byte, and `/claims <run id>` is how
a person looks at a failed run.

`_render_claims` joined four model-supplied list fields with
`'; '.join(map(str, value))`. Measured against the real `ClaimsScreen` in a
minimal host app: `compose()` raises `TypeError: 'int' object is not
iterable` and the exception **propagates to Textual's handler**, which batch
145 already measured as exiting the whole application. The containment batch
145 added at the `/` dispatch site cannot catch it: `push_screen` returns an
`await_mount`, so `compose` runs in the message pump rather than in the
handler's call stack. One site, four fields, no second copy anywhere in the
tree.

### Design Decisions Record — §66 (A25-A27)

| id | decision |
|---|---|
| **A25** | **The boundary declares the type of a container's MEMBERS wherever pure code reads them, and a boolean is not a value of any other type.** Two new spec fields, `key_member_types` and `entry_member_types`, paired with the existing `key_types`/`entry_types` the same way `required`/`entry_required` are; a MEMBER is a dict's values or a list's elements, deliberately one concept, because Pass 4's flag lists live under claim-id keys and Pass 2's entities live in an array and the thing being declared is identical. Chosen over the report's container-only shape, which measurement showed leaves three of six crashes open, and over making the two consumers tolerant, which would put the knowledge of what a valid entity is in two modules -- the split this module exists to end. The vocabulary grew by exactly what the crashes needed and no further: Pass 4's flag LISTS are typed and the strings inside them are not, because `len()` is all that reads them, and Pass 3c's `gaps` keeps its container type with no member type for the same reason. Both are test-pinned as controls, so a later widening has to argue with a test rather than drift |
| **A26** | **Pass 6b applies to the batch it was asked about -- the ids it is validated against, the claims its entries resolve to, and the claims whose sources are rescored -- and an entry naming a claim outside the batch is dropped AND traced.** 6a's rule, with 6a's reason ("a revision naming a claim outside it would land on one already finished"), extended to the pass that re-validates. The trace line is `review.py`'s policy for the same shape: a finding a stage cannot apply is dropped and said out loud, because dropping in silence is indistinguishable from applying, and 6b's own checkpoint reports an EFFECT and so cannot carry it. **Chosen over keeping the wide apply and re-tiering everything 6b touched**, which also clears the stale pair -- the owner raised exactly this, since one claim's update can reach a global quantity. Measured, both halves: the coupling is real and lives in the rescoring stage, which is why that stage is narrowed rather than compensated for; and an unconditional global re-tier is reproducibly harmful -- a LOW claim that D2 exhausted, forced to UNVERIFIED by `_apply_fallback`, comes back as **LOW beside the annotation saying it could not be verified**, which is the pair `run_confidence_tiering`'s own docstring says the narrow call exists to prevent. Closing the channel beats recomputing after it |
| **A27** | **The claims view renders a model-supplied list field whatever shape it arrived in, and shows a non-list as the one value it is.** Four fields (`entities`, `fallacies`, `contradictions`, `assumption_flags`) plus the source COUNT, through one helper at one site. Not merely defensive: A25 closes the producer for every run from here on and does nothing for a record already on disk, and reading those records is this view's entire job. Shown rather than dropped, because the reader asked what the run recorded and `123` is the answer. Chosen over containing at the call site, which is batch 145's rule and cannot reach this one -- compose runs in the message pump -- and over filing it, which would leave an app-killing door open behind a producer fix |

### What §66 deliberately does not change

- **`_check_ids` stays a total-mismatch test (B5).** A partial match is still
  accepted everywhere, including 6b. The fix is in the apply precisely so
  that this policy does not have to change: nine correct ids out of ten
  still apply, and the tenth is now dropped with a line saying so.
- **Pass 3a keeps the wide default.** It is the first grounding, where every
  claim is in play, and `score_grounding_sources`' docstring already
  explains why narrowing its default to the factual set would be a second
  rule to keep in step with gate D0 for no gain. Only the 6b caller
  narrows. A 3a entry naming a non-factual claim still lands; measured, it
  moves no tier, and that is `docs/TECHNICAL_DEBT.md` 98 rather than a
  silence.
- **`source_span` gets no type.** S12-01's suggested shape asks for `str`;
  grepped, nothing in production reads the field except `vars(c)`
  serialisation, which is JSON-native whatever it holds. A check with no
  demonstrated consequence is what B4 forbids, and adding one here would
  have been the module breaking its own rule inside the batch that enforced
  it.
- **Pass 6a's silent drop stays silent.** Its `if claim:` skip is
  documented and bounded by the round counter rather than by data the model
  supplies. Now that 6b says what it dropped, the asymmetry is worth
  recording, and it is item 99.
- **No element-type language for flag strings, and no re-validation of
  assumption flags in 6b.** Both are the report's own "Not doing", and both
  are still right: `len()` is the only reader of a flag list, and 6b not
  re-running Pass 4 is a stated design gap (ARCHITECTURE 7.2, AGENTS.md)
  with its own decision still unmade.
## §67. The gate a child could turn off, and three strings nobody here wrote

**Built in batch 150**, from the review marathon's `agents/` + `skills/` +
`mcp_client/` surface: all six `FIX-NOW` findings -- S13-02, S13-03, S13-04, S13-05,
S13-06 and S13-08 -- across
`agents/manager.py`, `core/config_loader.py`, `mcp_client/config.py`,
`mcp_client/registration.py`, `mcp_client/client.py` and
`tests/test_mcp_integration.py`.

Six findings, all S3, and the thing they have in common is the one the
surface is named for: **every one of them is a boundary between this
project's code and somebody else's** -- a project-tier agent definition, a
hand-edited `mcp.json`, a server's own tool metadata, a server's own
result. Five of the six are a check that stops one step short of the
thing it is checking.

Four answers were DECISIONS and the owner ruled all four. **One ruling
contradicted the report's suggested shape outright**, which is new: until
now the fix pass had found a suggested fix too big (§66's A24), half of one
(S04-02, S17-01), aimed at something already closed (S06-10), or
insufficient (§66's A25 and A26). S13-03's was aimed at a defect that is
real and would have re-opened a different one, and a pinned test is what
said so.

### A28 -- the approval merge filters BOTH sides, and the values are typed

`AgentManager.child_context` composed the child's `approval_overrides` in
two lines: the parent's dict filtered to its `True` entries, then the
agent's own merged **unfiltered**. So a `False` in a child's frontmatter
overwrote the parent's `True`, and the function's own docstring says the
opposite one line up -- *"Only True entries carry -- a False is
indistinguishable from absent under D14's OR, so unioning can only ever
tighten, which is what makes it safe to do silently."* Decision S2, quoted
verbatim from the code that did not implement it.

Driven through the real discovery path and the real
`security.permissions.requires_approval`, with the global
`ToolApprovals().web_search` at `False` so the parent's entry was the only
thing asking:

| parent says | agent `.md` declares | child's overrides | `requires_approval` |
|---|---|---|---|
| `{web_search: True}` | `approval_overrides: {web_search: false}` | `{'web_search': False}` | **False** |
| `{web_search: True}` | nothing | `{'web_search': True}` | True *(control)* |
| nothing | `approval_overrides: {get_time: true}` | `{'get_time': True}` | True *(control)* |

`tests/test_agents.py` had three tests over four quadrants. The missing
one is the one the code got wrong.

**It did not heal one hop out**, either: a grandchild came back with `{}`,
because the next `if v` dropped the `False` rather than restoring the
`True`. The tightening was gone permanently, not suspended for one run.

**And the cost is larger than the gate**, which neither the report nor the
docstring says. `candidate_approvals` reads the same dict and is **both**
the sign-off notice the user reads and the grant the child receives -- its
own docstring calls keeping them one *"the whole substance of a
sign-off"*. Measured: the loosened tool left the candidate list entirely.
So the user was not asked at call time **and** not told at sign-off time,
which is the one combination the sign-off exists to prevent.

**Scope, measured rather than reasoned.** `active_context` builds
`dict(agent.approval_overrides)` unfiltered and keeps it: at depth 0 there
is no parent to overwrite, a `False` is inert under D14's OR, and the next
hop's filter drops it. Driven both ways and pinned as a control, because
"the other composer looks similar" is how a fix grows past its finding.

**The values are typed where they are parsed**, which is the batch's own
addition and the report's named seam into S08's file.
`core/config_loader.py` validated the mapping and not its values, so
`web_search: "false"` parsed to the STRING and `requires_approval`
returned `'false'` from a function documented to return a bool. Measured:
**no value widens a gate** -- approval ORs, so every falsy value is
already indistinguishable from an absent key and every truthy one
tightens -- so this is the type boundary, not an escalation. It is also
§66's S12-01 shape one file over, and the comment three fields below it
already states the rule for its two scalar booleans: *"Booleans are
VALIDATED, not coerced. bool("false") is True, so `use_memory: "false"`
would invert a deliberate opt-out with no warning -- and these two fields
are exactly the ones a restrictive agent definition uses to opt OUT."*
`approval_overrides` is the third such field; it opts IN to prompting.

**Not done:** the three-state override. `tests/BREAKING_CHANGES.md:249`
forbids it by name, and the finding is that the two-state rule was not
implemented, not that it is wrong. A `False` is still legal to write and
still inert once written.

### A29 -- `disabled` has no safe constant, so it gets a named failure

`mcp_client/config.py:_flag` refused to coerce and returned `False` with a
warning for any non-boolean. Its docstring said why: *"Failing closed is
the only safe direction for both flags: an unexpectedly-gated server
prompts, an unexpectedly-enabled one runs third-party code
unannounced."* True of `autoApprove`. For `disabled`, `False` means NOT
disabled -- so the sentence's own second clause was the outcome it
produced. Driven through the real `load_server_configs` and the real
`describe()` that the acknowledgement screen prints:

| `mcp.json` entry | `cfg.disabled` | the consent line, in full |
|---|---|---|
| `"disabled": "true"` | False | `srv [user] runs: rm -rf /data` |
| `"disabled": "yes"` | False | `srv [user] runs: rm -rf /data` |
| `"disabled": 1` | False | `srv [user] runs: rm -rf /data` |
| `"disabled": true` | True | the same line **plus `disabled`** *(control)* |

A server the operator wrote a key to stop, presented as live.

**THE REPORT'S SUGGESTED SHAPE WOULD HAVE RE-OPENED THE MIRROR DEFECT,
and a pinned test is what said so.** It proposes a per-key safe direction:
`disabled` falls to `True`. But
`tests/test_mcp_config.py::test_non_boolean_disabled_falls_back_to_false`
pins `"disabled": "false"` to a server that RUNS, and its docstring names
the alternative as a defect: *"The mirror defect: `"disabled": "false"`
skipped a server the user meant to keep running."* The two plausible
misspellings are one hand edit apart and want opposite answers, and the
only thing that distinguishes them is the value -- which `_flag` exists to
refuse to interpret, correctly.

So neither constant is the answer, and the owner ruled the third option:
**an unparseable `disabled` makes the entry UNUSABLE by name**, which is
the mechanism this module already has for an entry it cannot make sense
of. `transport` stays `'unknown'`, `cfg.error` carries the reason,
`describe()` prints `srv [user] UNUSABLE: 'disabled' must be true or
false` on the acknowledgement screen, `_transport_for` raises so the
server lands in `failures` by name, and `main.py` prints that too. Nothing
runs and nothing is silent, which is the only pair of properties both
spellings can have at once; one edit fixes it. This is decision G
verbatim -- *"an entry we cannot make sense of becomes that one server's
named connect failure rather than a startup error for everything else."*

`_flag` therefore no longer CHOOSES. It returns `Optional[bool]` and each
caller states its own safe direction, because the difference between the
two keys is the finding. `autoApprove` keeps falling to `False` and its
own test passes unchanged -- pinned as a control, since a fix that spread
to the key whose direction was already right would be the fix growing
past its finding.

### A30 -- a composed name is validated at the producer, and an invalid one is refused

`registration.tool_name_for` composes `mcp__<server>__<tool>` and nothing
validated the result. Both halves are outside this repository's control:
the server half is a free-form key in `mcp.json`, the tool half comes from
the server. Verified against the installed `mcp==2.0.0`:
`Tool.model_fields["name"]` is a bare required `str` with empty
`metadata` -- no pattern, no max length. Pinned as a test, so a pin move
that adds one upstream is visible rather than silently duplicated.

Driven through the real `register_all` over a real `ToolRegistry`, checked
against `^[a-zA-Z0-9_-]{1,64}$` -- the tool-name rule Anthropic, OpenAI
and Google all publish:

| why it is realistic | composed name | len | registered | advertised |
|---|---|---|---|---|
| a dot in a hand-written `mcp.json` key | `mcp__my.server__search` | 22 | yes | yes |
| a space from the server | `mcp__fs__read file` | 18 | yes | yes |
| a newline from the server | `mcp__fs__read\nfile` | 18 | yes | yes |
| ordinary naming, two long words | `mcp__plugin_engineering_pagerduty__complete_authentication_for_account` | **70** | yes | yes |
| a server-supplied 400-char name | `mcp__s__xxxx...` | 408 | yes | yes |

`registry.schemas()` is recomputed at the top of every model call (K7), so
one such name is in the `tools` array of **every request for the rest of
the session** whether or not the model ever chooses it. This is the
inversion of the invariant `_manager()` states for itself -- *"One
unreachable server must never take down the others"* -- and it is worse
than the case that comment guards: a **reachable, working** server makes
every request malformed. A 400 on a malformed `tools` array is not
transient, so `core/provider_errors.is_transient` will not retry it, and
nothing in the harness can attribute it -- the error names a tool name and
the user configured a server.

Refused, not mangled: a silently renamed tool is one the model cannot name
back, and the prefix is not a parser, so a generated name maps to no
server. One warning per SERVER for a bad character in the server half,
because it poisons every name that half composes and the server is the one
the operator can rename; one warning per TOOL otherwise, because the
server's other tools are unaffected. That is the collision branch's shape
deliberately -- it was written for a name that collides, this is a name
that is invalid, and the outcome should be the same kind of outcome.

**Checked here because this is the last place the fact is knowable.**
Registration is the one function that sees the server name and the tool
name together; `core/client.py` translates schemas and `tools/registry.py`
dispatches, and neither has a reason to know what a provider will accept.

**Decision G is not reversed, and `registration.py`'s own sentence about
it gained a clause.** That comment said recording what was registered
fixes the collision hazards *"without rejecting any mcp.json content,
which decision G rules out"*. What G rules out is up-front SCHEMA
validation of `mcp.json`; nothing in `register_all` reads the file, and a
named per-server failure is G's own remedy. Left as written, that sentence
would have been a claim made false by a change in the same file -- defect
shape 9, in the file under repair, which is the trap batch 130 had to
avoid in `sandbox.py`.

**The rule is one this project's own names already satisfy.** All 31
registered built-in names match it, measured and pinned as a control: this
is a bound on third-party names, not a constraint the project would have
to live down to. The prefix and the two separators spend 7 of the 64
before either name starts.

### A31 -- the third-party text in a tool schema is bounded at the producer

An MCP tool's `description` went into the schema with a `[MCP: <server>]`
prefix and no other treatment -- no cap, no whitespace normalisation.
Measured: 50,082 characters and 4 newlines in, 50,094 and 4 out,
advertised. The sibling producer for the same class of string,
`core/config_loader._catalog_text`, returns **300** characters and
collapses `"a\n## System"` to one line.

The invariant is §32 A5, verbatim: *"Catalog text is normalised at the
producer -- whitespace collapsed, capped at one constant, applied to
`name` and `description`, at parse time, for every tier."* Of the three
sources of third-party text that enter a model request here, two were
capped and normalised and the third -- the one that arrives from code
nobody here reviewed, at connection time, after the consent screen has
been answered -- was neither. A5's reason is #131: a `description`
containing a newline LEAVES ITS BULLET and forges a top-level section. The
reason that exact mechanism does not fire here is only that tool schemas
are a JSON array rather than a rendered list; the newlines still arrive,
and what they can do then depends on a consumer. That dependence is what
A5 exists to remove, and this project has already paid for it at the
consumer end: `core/reasoning/authorization.py:96` takes
`description.strip().splitlines()[0]` for the grant picker, which is a
consumer defending itself against this producer.

**The half neither report names, and the owner ruled it in.**
`input_schema` is third-party text in the same request and `schema_for`
passed it through untouched -- measured, a 50,082-character property
description arrived byte-for-byte. So did `title`, which is concatenated
INTO the description. Two bounds, because they answer different
questions:

- **`description` is normalised and capped** at `MAX_TOOL_DESCRIPTION_CHARS`
  = 2000, applied AFTER the prefix and the title are composed in so one
  call covers all three strings. Deliberately not
  `MAX_CATALOG_TEXT_CHARS`: 300 is right for a one-line catalog bullet and
  would mutilate a legitimate tool description, and sharing the constant
  would couple two bounds that answer different questions. 2000 is 2.5x
  the longest description this harness ships (786, `shell_interactive`).
  The cut is visible, for `_catalog_text`'s reason.
- **An oversized WHOLE schema loses the tool**, at
  `MAX_TOOL_SCHEMA_CHARS` = 16384, measured on the serialised form because
  that is what is paid. Capping the strings INSIDE `input_schema` was
  considered and declined: a truncated enum or pattern is a tool that
  fails at call time instead of load time, which is the harder failure to
  trace. 16384 is ~3.6x the largest schema this harness ships (4544,
  `shell`). A schema that will not serialise at all is refused by the same
  gate -- it could not be sent either way, and finding out here names the
  server instead of raising inside a provider branch.

**Not done:** wrapping tool descriptions in `prompts/untrusted_content`.
That is a real question -- a third party's text is being read as
harness-authored instruction -- but it is a design decision about what a
tool schema IS, it would change what every MCP tool reads like to the
model, and S05 owns the untrusted-content envelope. Recorded as the seam.

### A32 -- the placeholder is appended, and the drop is disclosed

`_normalize`'s non-text placeholder was guarded by `if not text and
blocks`, so it fired only when **no** text block produced anything. A
result carrying text *and* non-text dropped the non-text with no
placeholder and no log line:

| blocks in | result | placeholder | log lines |
|---|---|---|---|
| `text("Chart generated:")`, `image`, `image` | `{'content': 'Chart generated:'}` | **no** | **0** |
| `text("   ")`, `image` | `{'content': '   '}` | **no** | **0** |
| `image` | `{'content': '[non-text MCP content: image]'}` | yes *(control)* | 0 |
| `text("")` | `{'content': '[non-text MCP content: text]'}` | yes, and it says **text** | 0 |
| `text("boom")` + `structured`, `is_error` | `{'error': 'boom'}` | -- | 0 |

The rule is `AGENTS.md`'s, under its own heading: *"Nothing an
output-policy pass alters is silent... Every altered dispatch result logs
ONE WARNING naming the tool and what changed -- before #49 the depth cap
replaced a whole container with a sentence and logged nothing."*
`_normalize` altered a dispatch result and logged nothing on any of its
three dropping paths. "Chart generated:" with no chart and no note is a
result the model will answer from.

Three things changed, and the fourth row above is why the second one is
not cosmetic:

- **The placeholder is APPENDED** to whatever text survived rather than
  substituted for it. The all-non-text case is unchanged, because with no
  text to append to the placeholder is the content.
- **The kinds come from the blocks the join actually skipped**, and an
  empty text block is not one of them. It carries nothing to drop, so
  reporting it as non-text was the placeholder contradicting itself.
- **One WARNING names the tool and what was dropped.** The tool name had
  to become a PARAMETER: `_normalize` took the result alone, so #49's
  rule was unsatisfiable in the function it applies to.
- **A structured error payload is folded into the one error shape.** The
  error branch dropped `structured_content` entirely, so a server
  reporting `{code: 429, retry_after: 30}` lost the one thing that would
  have explained the failure. In the string, not a second key: D23
  promises `call_tool()` returns one error shape, and a second key makes
  every consumer of a dispatch error a place that may or may not read it.
  Bounded at `MAX_ERROR_DETAIL_CHARS` = 500, because the payload is
  third-party and the string reaches a model.

**Not done:** a multimodal tool-result path. `client.py` declines it
explicitly and that is right -- *"the harness has no multimodal
tool-result path, and inventing one here is scope the rest of the design
can't carry."* The finding is that the decline should be disclosed
consistently, not reversed. And the SUCCESS path's `result` key is still
unbounded: an unbounded tool RESULT is `TECHNICAL_DEBT 48`, which S13-01
measured and which is ruled DEFER, and bounding it in passing would close
a filed item halfway and in the wrong module. Pinned as a control.

### A33 -- the except branch releases both tasks

Found by running S13-08's uncovered path, not by reading.
`connect_all`'s except branch cancels the manager task and stops the loop.
`_start()` is a SECOND task, still parked on `_ready.wait()` at that
moment -- and it stays parked, because `_cancel_manager()` cancels the
manager, `CancelledError` is a `BaseException` so the manager's `except
Exception` does not run, and the `_ready.set()` in that handler is never
reached. Left pending, asyncio reports *"Task was destroyed but it is
pending!"* through its own logger whenever the GC reaches it.

That matters more here than it would elsewhere: `tui/app.py` attaches
`TranscriptLogHandler` to the **root** logger, so an asyncio ERROR can
surface in a user's transcript -- a line naming nothing, attributable to
nothing, at an arbitrary later moment. Cancelled first, while the loop is
still running to deliver it. `cancel()` is not the no-op it looks like: a
`run_coroutine_threadsafe` future never reports `running`, so it returns
True and the task really is cancelled, which is measured rather than
assumed.

### S13-08: the test that could not look, and what running it found

§17 AC8 is the claim that stdio servers -- child processes this harness
spawned -- are reaped. One test could see a real process and it exercised
one path: connect, register, dispatch, `disconnect_all()`, poll. The path
with the **recorded history of orphaning** is the other one, and
`connect_all`'s own comment is about it: *"A timeout here used to abandon
the manager permanently... So one hung server orphaned every other
server's subprocess."* That recovery was pinned only over the in-process
transport, which the integration file's own docstring says cannot see a
child. **So the orphan-prevention path was proven in the one environment
that cannot observe an orphan.**

The report could not run the test -- it is excluded from the review's
permissions -- and named the exact command as the deliverable it owed.
Run: **1 passed in 9.66s**, then the two cases added beside it.

**Both pass, and that is the finding.** The per-server handshake timeout
reaps its child and so does the caller-side timeout, which means
`stdio_client.__aenter__` DOES clean up under cancellation -- the SDK
behaviour S13-08 names as unmeasured, since
`AsyncExitStack.enter_async_context` pushes its exit callback only after
`__aenter__` returns and the stack therefore never owned that child. The
code was right; nothing here had ever looked. The two cases are regression
guards for a property this harness depends on and does not own, and the
negative control is the one the report asks for by name: dropping
`_cancel_manager()` from the except branch makes the second test report a
leaked PID.

### Design Decisions Record — §67 (A28-A33)

| id | decision |
|---|---|
| **A28** | **A child's approval overrides are composed from the True entries of BOTH sides, and the values are typed where they are parsed.** Decision S2's stated rule, finally implemented: the parent's dict was filtered and the agent's merged raw, so a `False` in a child's frontmatter overwrote the parent's `True` and the docstring one line up was false of the line below it. It did not heal one hop out either -- the next `if v` dropped the False rather than restoring the True. The cost is larger than the gate, and that half is measured rather than reported: `candidate_approvals` reads the same dict and is both the sign-off notice and the grant the child receives, so the loosened tool left the candidate list entirely and the user was neither asked at call time nor told at sign-off time. `active_context` is deliberately NOT changed and is pinned as a control -- at depth 0 there is no parent to overwrite, a False is inert under D14's OR, and the next hop's filter drops it, all three measured. The VALUES are validated in `core/config_loader.py` beside the two scalar booleans whose comment already states the rule; measured first, and no value widens a gate, so that half is the type boundary and not an escalation. NOT the three-state override, which BREAKING_CHANGES forbids by name |
| **A29** | **A `disabled` value that is not a JSON boolean makes the entry UNUSABLE by name, and `_flag` reports rather than chooses.** `False` meant NOT disabled, so `"disabled": "true"` connected the server and the consent line showed it as live. **Chosen over the report's per-key constant (`disabled` falls to True)**, which would have re-opened the mirror defect a pinned test names: `"disabled": "false"` is one hand edit away and wants the opposite answer, and the value is the only thing that distinguishes them -- which `_flag` exists to refuse to interpret. So neither constant is the answer and the entry is refused the way a bad `args` is: transport stays `unknown`, `describe()` prints `UNUSABLE: 'disabled' must be true or false`, `_transport_for` raises so the server lands in `failures` by name. Nothing runs and nothing is silent, which is the only pair both spellings can have at once, and it is decision G verbatim. `autoApprove` keeps falling to False, pinned as a control: it is the key whose direction was already right |
| **A30** | **A composed `mcp__<server>__<tool>` name is validated against the rule every provider publishes, and an invalid one is refused by name.** `^[a-zA-Z0-9_-]{1,64}$`, checked in `register_all` because that is the last place the server name and the tool name are seen together. A reachable, working server could put a name in the catalogue that made EVERY request malformed -- `schemas()` is recomputed per call (K7), a 400 on a malformed `tools` array is not transient, and nothing can attribute it. Refused, not mangled: a renamed tool is one the model cannot name back and the prefix is not a parser. One warning per SERVER for a bad character in the server half, because it poisons every name that half composes and the operator can rename it; one per TOOL otherwise. Within decision G, not against it: G rules out up-front SCHEMA validation of `mcp.json`, and a named per-server failure is G's own remedy -- `registration.py`'s own sentence about G gained the clause that says so, because left as written it would have been a claim made false by a change in the same file |
| **A31** | **Third-party text in an advertised tool schema is bounded at the producer, and an oversized schema loses the tool.** `description` is whitespace-collapsed and capped at `MAX_TOOL_DESCRIPTION_CHARS` = 2000, applied after the `[MCP: ]` prefix and the server-supplied `title` are composed in so one call covers all three strings. A5's rule, which two of the three sources of model-facing third-party text already followed. Deliberately not `MAX_CATALOG_TEXT_CHARS`: 300 is right for a one-line bullet and would mutilate a real tool description, and sharing it would couple two bounds that answer different questions. **`input_schema` is the half neither report names** and it is bounded too, by a different mechanism: the serialised whole schema is capped at `MAX_TOOL_SCHEMA_CHARS` = 16384 and the tool is refused over it, because capping the strings INSIDE a schema turns a load-time failure into a call-time one. Both constants are sized from this project's own practice -- 2.5x its longest description, ~3.6x its largest schema. A schema that will not serialise is refused by the same gate |
| **A32** | **The non-text placeholder is appended to whatever text survived, the dropped kinds are the blocks the join actually skipped, one WARNING names the tool, and a structured error payload rides in the one error string.** The guard was `if not text and blocks`, so the disclosure was absent on exactly the half where the result still read as complete. The tool name became a PARAMETER because `_normalize` took the result alone and #49's rule -- one warning naming the tool -- was therefore unsatisfiable in the function it applies to. An empty text block is not reported as non-text: it carries nothing to drop, which was the placeholder contradicting itself. The error payload goes in the string and not a second key, because D23 promises one error shape; bounded at `MAX_ERROR_DETAIL_CHARS` = 500. NOT a multimodal result path, which `client.py` declines for stated reasons, and NOT a bound on the success path's `result` -- that is TECHNICAL_DEBT 48, ruled DEFER, and is pinned as a control |
| **A33** | **`connect_all`'s except branch releases the `_start` task as well as the manager, and cancels it before the loop stops.** Found by running S13-08's uncovered path rather than by reading it. `_start()` is parked on `_ready.wait()` when the caller's budget expires and stays parked, because `CancelledError` is a `BaseException` so the manager's `except Exception` never reaches its `_ready.set()`. Left pending, asyncio reports "Task was destroyed but it is pending!" through its own logger at whatever moment the GC gets to it -- and `tui/app.py` attaches `TranscriptLogHandler` to the ROOT logger, so that line can surface in a user's transcript naming nothing. The ORDER is load-bearing and no return value reports it: a cancellation scheduled after `_shutdown_loop()` is never delivered |

### What §67 deliberately does not change

- **`active_context`'s unfiltered copy** (A28), pinned as a control
  rather than tidied.
- **`autoApprove` falling to `False`** (A29), for the same reason: the
  key whose direction was already right.
- **The strings inside `input_schema`** (A31). The tool is refused whole
  instead; a truncated schema is a tool that fails at call time.
- **The success path's unbounded `result`** (A32), which is
  `TECHNICAL_DEBT 48` and ruled DEFER.
- **`tools/registry.register` itself.** The gate lives in `register_all`
  because MCP is the only dynamic registrant and the composed name is its
  fact. A bound in the registry would be the fix-at-the-consumer shape
  `AGENTS.md` names by name.
---

## §79. A demo nobody has to install

**Requested and designed in batch 163, built across batches 163-166.** The
harness ran where someone installed it -- a clone or the npm package -- and
connecting a model meant copying `providers.json.example` and editing JSON,
which README called "the one setup step they cannot do for you". The owner asked
for a public demo on Hugging Face Spaces and ModelScope Studios, kept current
from `main`, where a visitor can try the harness without installing anything
and connect a model without touching a file. Every decision below was put to
the owner with options and a recommendation before any code was written.

### What was measured or read before anything was designed (2026-10-19)

- **The owner's Hugging Face account is free**, and on a free account the only
  hostable compute Space is a Gradio-SDK app on ZeroGPU, two per account.
  Docker Spaces and the plain CPU tier need a paid plan. ZeroGPU refuses to
  start without at least one `@spaces.GPU` function; a no-op one burns no quota.
  `gr.Server` is a FastAPI subclass, so a Gradio Space can carry custom routes
  and a websocket beside Gradio's own.
- **ModelScope is the international site**, `modelscope.ai`, under the
  organisation `Venastine-Research`. A Studio is fed by a git push to `master`
  (force push prohibited) and redeployed by `POST /openapi/v1/studios/{o}/{r}/deploy`.
  Its free tier is a CPU instance. API-Inference there is
  `https://api-inference.modelscope.ai/v1`, OpenAI-compatible: `/v1/models`
  answered 200 with a live list. A ModelScope access token is account-wide --
  the same token pushes code to the account's repositories.
- **Textual already renders in a browser.** Its web driver
  (`textual.drivers.web_driver:WebDriver`) is what `textual-serve` runs: one
  app subprocess per websocket, framed packets after a `__GANGLION__` line, an
  xterm.js page on the other end. `textual-serve`'s own server is aiohttp-only
  and builds the child's environment with `os.environ.copy()`, so every Space
  secret would reach the child and, through it, the model's tools.
- **Only two providers explicitly invite third-party OAuth in their own
  documentation.** Hugging Face (`https://huggingface.co/docs/hub/oauth`): a
  "Sign in with HF" flow "in any website or App", the `inference-api` scope,
  public apps with no secret, and a device code flow. OpenRouter
  (`https://openrouter.ai/docs/guides/overview/auth/oauth`): "Use OAuth to let
  your users bring their own OpenRouter API key to your app", PKCE, and a mode
  with no callback URL in which the code is shown to copy. Nothing comparable
  was found for ModelScope, Anthropic, OpenAI or Google.
- **A credential used by the harness lives on the server.** The process that
  calls the model runs in the container; the browser only draws the terminal.
  Every visitor shares that container and one Unix user, and a Space has no
  root with which to give each session its own. **Wrong about root, measured
  in batch 165** -- a ZeroGPU Space runs as root; see the next section.
- **The code**: no in-app way to add a provider existed, and
  `credentials.save_credentials()` had no production caller. Any
  OpenAI-compatible endpoint already worked as a hand-written entry. The client
  cache is keyed by provider name for the life of the process, so a key added
  mid-session was ignored until a restart. Shell, `read`, `write` and `edit`
  ship disabled and the host fallback ships off. `/config` writes the
  install tree's `config.yaml`, which every session in a container shares, and
  `/copy --file` writes any path. Every piece of state is redirectable per
  process, which is §60 UH3's supervisor shape exactly.

### What the first deploys measured (2026-10-20)

Batch 165 put the bridge on the owner's private Space before the rest of the
slice was decided, and the Space's own log answered what nothing outside it
could. The bridge writes those answers at every start since
(`deploy/hosted/isolation_report.py`), so a platform that changes them says so.

- **A Space installs `gradio[oauth,mcp]` at the README's `sdk_version`, in the
  same pip command as the bundle's requirements.** Gradio 4.44.0 cannot share
  an environment with the harness at all -- its client wants `websockets<13`,
  `google-genai` wants `>=13` -- and has no `gr.Server`. 6.30.0 resolves, but
  its `mcp` extra wants `mcp<2` and `pydantic<=2.12.5` against the harness's
  deliberate `mcp==2.0.0` and `pydantic==2.13.4`. One environment would run
  the hosted harness on versions CI never tests (HD2, amended).
- **A ZeroGPU Space runs as root**: uid 0, `HOME=/root` at mode 700, an Amazon
  Linux 6.18 kernel on x86_64, and Docker's default capabilities less
  `NET_RAW` -- `DAC_OVERRIDE`, `KILL`, `SETUID` and `SETGID` among them,
  neither `SYS_PTRACE` nor `SYS_ADMIN`. HD6 holds anyway, because reading a
  non-dumpable process takes `SYS_PTRACE` whatever the uid. `DAC_OVERRIDE` is
  the one that matters: root reads any file, a 0700 directory included. And
  root can become another user, which the bullet above said a Space could not.
- **Landlock is at ABI 7 and a seccomp filter installs.** Both were applied
  from Python through `ctypes`, in a child on the session interpreter, which
  then computed an integral and was refused every read outside its import
  roots, every write, a socket, `fork`, `exec`, a signal, `ptrace` and
  `setuid(0)`.
- **The proxy APPENDS to `X-Forwarded-For`.** A request carrying a
  hand-written TEST-NET-2 entry arrived with it first and the real address
  after it, so the rate limit, which keyed on the first entry, keyed on a
  value the client chose (HD8, amended).
- **A session outlived its bridge** (under WSL): a bridge stopped with SIGTERM
  left `main.py --tui` running, re-parented to init, holding its session
  directory (HD8, amended).

### Slices

1. **`/connect`, batch 163.** A preset catalogue, the two sign-in flows, a
   pasted key for everything else, a key stored in `providers.json` or -- for
   the shared demo -- in the process alone, the client cache forgotten on
   every connect, a link that opens in the reader's own browser under the web
   driver, and the three new credential shapes redacted. Useful to every
   install, which is why it came first.
2. **Hosted modes inside the harness, batch 164.** `security/hosted.py` reads
   `VENASTINE_HOSTED` once; a shared deployment refuses the container-reaching
   tools in `is_tool_allowed` whatever `config.yaml` says, refuses `/config`
   and a pasted key, keeps a connected key in memory, turns `/copy --file` into
   a browser download under the file's name alone, and replaces the launch's
   file-shaped provider warnings with a banner that says what the server holds
   and where to revoke it. Every hosted session calls `prctl(PR_SET_DUMPABLE,
   0)` first thing in `main()`. Measured under WSL before that was relied on: a
   sibling process of the same user read an ordinary process's
   `/proc/<pid>/environ`, secret included, and was refused the same read with
   EACCES once the target had made the call.
3. **The bridge and the bundle, batch 165.** `deploy/hosted/`: a Starlette
   bridge on Gradio's `gr.Server` that speaks Textual's web-driver protocol to
   one `main.py --tui` per tab over an allowlisted environment; the session
   environment built at container start from a hash-locked `session.lock`; the
   single-user gate that asks the app's own public URL whether a stranger can
   reach it; and the startup report of what the container lets a process do.
   `scripts/build_demo_bundle.py` builds what a Space runs from the npm `files`
   allowlist and refuses any `neverDistribute` match. A shared deployment's
   math tools run confined, or not at all (HD11). Deployed by hand to the
   owner's private Space and measured there; nothing deploys from `main` yet.
4. **The deploy workflow and the rest of the record, batch 166.**

### What §79 deliberately does not change

**The credential split.** Provider keys still live in `providers.json` and
nowhere else on disk; `/connect` is a writer for that file, not a second store.
The session-only overlay holds a key in memory and is never written.

**The shipped tool defaults.** A shared demo offers what a fresh install offers.
The six math tools run confined there (HD11), and are refused only on a host
where that confinement could not be verified -- which is a deployment that
cannot run them safely, not a different default.

**Who may call the model.** `/connect` is a slash command, typed by a person;
nothing in a context window can reach it.

### Design Decisions Record — §79 (HD1-HD11)

| id | decision |
|---|---|
| **HD1** | **The browser draws the TUI through Textual's web driver, and the bridge is ours.** A Starlette bridge speaks `textual-serve`'s framed protocol to one `main.py --tui` child per websocket. The `textual-serve` package, pinned exactly and installed only by the deploy, supplies its xterm.js client assets and nothing else; its aiohttp server is never run, because it copies the whole parent environment into the child. A pseudo-terminal emulator was the alternative and loses Textual's browser-side link opening and file delivery |
| **HD2** | **One app serves both platforms**: Gradio SDK with `gr.Server` mounting the bridge's routes. On Hugging Face the Space runs on ZeroGPU, the only compute a free account can host, with one no-op `@spaces.GPU(duration=1)` function registered through `@app.api` and never stacked on it; no provider call is ever made inside a GPU function. On ModelScope the Studio runs on the free CPU tier. **Amended in batch 165**, by measurement: the Space installs `gradio[oauth,mcp]` at the README's `sdk_version` (6.30.0, on Python 3.12) in the same command as the bundle's requirements, and that extra cannot share an environment with the harness's `mcp` and `pydantic` pins -- so there are TWO environments. The bridge's is the Space's own and holds only `textual-serve`, `uv` and the harness's `textual` pin; the sessions' is built at container start by `deploy/hosted/session_env.py`, with `uv`, from `session.lock` -- the harness's `requirements.txt` compiled by the bundle builder with a hash for every file, installed `--require-hashes --no-build`, and reused while the lock is unchanged (3 s on the Space). As root it lives in `/opt`, where a math child that has become `nobody` can still import from it (HD11) |
| **HD3** | **One process per browser tab, isolated by its environment**: a fresh session directory per connection, and `HOME`, `APP_DB_PATH`, `AGENT_LOG_FILE`, `AGENT_WORKSPACE`, `AGENT_OUTPUT_DIR`, `AGENT_PROVIDERS_FILE` and `AGENT_SECRETS_FILE` all inside it. The environment is built from an allowlist, never copied, and never carries `PYTHONPATH`. The directory is deleted when the tab closes, so nothing a visitor did outlives the session. Setting `AGENT_WORKSPACE` here does not contradict the npm launcher's rule against it: that rule exists because a shared directory would become an auto-approved write boundary for every project, and a per-session temporary directory is the opposite |
| **HD4** | **Two hosted modes, read from the real environment only**: `VENASTINE_HOSTED=shared` and `VENASTINE_HOSTED=single-user`. A `.env` cannot set either, by A70. Shared mode only REMOVES capability: `/config` is refused, because it writes a file every session in the container shares; `/copy --file` delivers to the reader's browser instead of writing a server path; and a banner says the session is temporary |
| **HD5** | **In a shared deployment a credential comes only from a scoped sign-in and lives only in the session's process.** `/connect` offers Hugging Face's device code flow -- through a public OAuth app the project owns, its client id in `config.yaml` as `hf_oauth_client_id`, its scopes `openid profile inference-api` on the app's registration -- and OpenRouter's PKCE flow with the code shown to copy. A pasted key is refused there. The credential is held in an in-memory overlay in `credentials.py`, never written, and dropped when the session ends. In a local install, and in single-user mode, both sign-ins are offered and a pasted key is accepted and saved to `providers.json`. OAuth is offered for exactly these two providers because they are the two whose official documentation explicitly invites a third-party app to use it; that list grows only by the same test. **Amended in batch 165**: `hf_oauth_client_id` names the project's own public app, registered by the owner on 2026-10-20, so the sign-in works in every install without anyone registering another. A shared deployment offers -- in its banner, `/connect`'s list and the command panel -- only the sign-ins that work there, because offering Hugging Face with no client id led straight to the pasted-token refusal, measured on the Space |
| **HD6** | **A session cannot read another session's memory.** Every hosted session process marks itself non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`) at startup, after exec, which resets it, so another process of the same user is refused its `/proc/<pid>/mem`, `/proc/<pid>/environ` and a debugger attach. The Hugging Face token arrives from Hugging Face directly into the session and is never typed, so it never crosses the websocket. **Amended in batch 165**, by measurement: a ZeroGPU Space runs every session as root, and this still holds, because reading a non-dumpable process takes `CAP_SYS_PTRACE`, which the Space does not grant. What root does have is `DAC_OVERRIDE`, so a session that could run code could read another's FILES; that is why no tool in a shared deployment reaches the container, and why the one tool family that evaluates what the model sends is walled in (HD11) |
| **HD7** | **Single-user mode is honoured only when the app is not reachable anonymously**, and fails closed. The bridge requests its own public URL with no credentials -- `SPACE_HOST` on Hugging Face, `VENASTINE_PUBLIC_URL` on ModelScope, where its absence refuses -- at startup and before each session, and an anonymous answer refuses every session with a page saying why. When honoured, a pasted key and a ModelScope token are accepted, an optional `VENASTINE_PROVIDERS_JSON` secret seeds the session's `providers.json`, `/config` and `/copy --file` work, and at bridge start `config.yaml` -- the one location, written with `config_edit`'s comment-preserving writer -- enables `read`, `write`, `edit` and `shell` and the host fallback, because the private container is the sandbox and a `/config` edit would be lost at the next restart. The owner asked for the tools explicitly. Interactive shell sessions still need Docker or Podman and stay unavailable. **Amended in batch 165**: every session of a single-user deployment shares ONE state directory that nothing deletes (`VENASTINE_STATE_DIR`, by default `$TMPDIR/venastine-state`), so the owner's threads survive a reloaded tab until the container restarts; a shared deployment still gives every tab a fresh directory and deletes it |
| **HD8** | **The bridge bounds what it runs**: a maximum number of concurrent sessions (4 shared, 2 single-user), an idle timeout, a hard lifetime, address-space and CPU limits on the child, and the whole process group killed on stop. A websocket must carry a same-origin `Origin` header and a one-time ticket, and session creation is rate-limited per address. **Amended in batch 165**, three measurements: a session now dies with its bridge -- `prctl(PR_SET_PDEATHSIG, SIGTERM)` at startup, after a SIGTERMed bridge left its sessions running under WSL; the bridge's cleanup of a session is shielded from cancellation and forgets the session only once it has stopped, because a cancelled handler (Starlette's TestClient cancels one when its socket closes; any server does at shutdown) left the child running and its directory in place; and the rate limit keys on the LAST `X-Forwarded-For` entry, the one the platform's proxy appends, where it had keyed on the first, which the client writes. A session that never reaches the handshake has its stderr's tail logged, from a file in the session rather than a pipe nobody reads |
| **HD9** | **What deploys is a built bundle, only from `main`, only after the tests pass.** The bundle is the npm `files` allowlist -- what an end user runs, already checked against `neverDistribute` -- plus the deploy overlay, a generated per-platform README and requirements, and `BUILD_INFO.json` naming the commit, which the page shows. Never a git mirror: the Space README needs frontmatter the repository's must not carry, and ModelScope forbids the force push a mirror needs. The workflow runs on `workflow_run` of `tests.yml` completing with success, for a `push` event, from this repository, on `main`; a manual run is a dry run unless it is on `main`; a missing secret turns that platform's step into a dry run; gitleaks scans the bundle before anything is uploaded; every action is pinned by commit. Whoever can merge to `main` decides the code that runs beside every live visitor's credential, and this section says so in as many words. **Amended in batch 165**: `scripts/build_demo_bundle.py` builds the bundle. It refuses a `neverDistribute` match more strictly than the npm and pip channels apply the list -- an `exact` pattern at any depth, a `dir` pattern as any directory segment, an unknown match kind loudly -- and generates the root `app.py` shim, the README, the requirements, `.gitattributes` (an upload that deletes everything else keeps a Space's old one) and `session.lock`, recording in `BUILD_INFO.json` whether the build had local changes. The client id is a public value that gitleaks' `generic-api-key` reads as a key, so `.github/gitleaks.toml` allowlists that one line by its shape, and a bundle is scanned from its own root, where its path is `config.yaml` |
| **HD10** | **Deferred: a model hosted on ZeroGPU, and keys held in the browser.** A ZeroGPU model would spend the visitor's few daily GPU minutes one agent step at a time, each a separate allocation with queueing and a possible cold start, and charging the visitor rather than the Space would mean forwarding their identity header from a subprocess; the no-op GPU function keeps the Space ready for it. Keys held in the browser would mean relaying every model call through the page, which only some providers permit from a browser at all |
| **HD11** | **In a shared deployment the math tools run walled in, or not at all.** SymPy's parser ends in `eval`, and a visitor chooses every expression their own model sends, so `_validate_ast` is the lock and `security/confine.py` is what stands behind it. The child each math call already ran in (H2, H3) puts up four walls after importing the tool and before reading its params: no file may grow and the process is non-dumpable; a child started as root becomes `nobody`; Landlock allows reading beneath the interpreter's import roots and nothing else, from ABI 4 no TCP and from ABI 6 no signal or abstract socket outside the child; and a seccomp denylist answers process creation, networking, signals, tracing, privilege changes, mounts and the kernel's control surfaces with `EPERM`. A denylist, because which calls CPython, glibc and SymPy's lazy imports need is a property of their versions, and Landlock carries the filesystem. A wall that will not go up means the call does not run. The bridge proves the walls at every start with a self-test on the session interpreter -- an integral computed, then every escape attempted -- and only a clean pass sets `VENASTINE_CONFINEMENT=verified` for its sessions; without it `security/hosted.py` refuses the six tools as it refuses the container tools. Linux on x86_64 only. A local install and a single-user deployment are unchanged: one has its owner at the keyboard, the other a shell already. Measured on the Space before it was relied on (2026-10-20: uid 65534, Landlock ABI 7, eleven escapes and the way back to root all refused), and the verdict is in the Space's log at every start |

---

## §78. A release tag that named one version while the run packed another

**Built in batch 162**, from register item 115, which batch 161 filed while
reading the `v0.3.1` tag's runs. `package.json` and `pyproject.toml` both say
`0.2.0`. They said it under `v0.3.0` at `f836602` and under `v0.3.1` at
`e50fe6e`. The `v0.3.1` `publish (npm)` run packed `@yahyahammad/venastine@0.2.0`,
and `scripts/prepublish-check.mjs` reported "prepublish check passed: v0.2.0".
It uploaded nothing, and only because no `NPM_TOKEN` is configured.

**THE TAG WAS A THIRD COPY OF THE VERSION, AND NOTHING READ IT.** The script
compares the two version files with each other. `tests/test_docs_consistency.py`
pins the same pair. `publish.yml` reads no tag name at all. So once the channel
is armed, a tag push publishes whatever the files say, whatever the tag says, and
`publish.yml`'s own header says why that cannot be undone: a published npm
version "can never be replaced -- only deprecated".

**What npm holds, which the register entry did not measure.** `npm view
@yahyahammad/venastine` lists `0.1.0` (2026-09-03) and `0.2.0` (2026-09-09,
`gitHead` `41b4850`, the `v0.2.0` commit), and neither carries a provenance
attestation, so neither came from this workflow's real publish step. So an armed
`v0.3.1` run would not have published silently: npm refuses to publish over an
existing version, and the step would have gone red. The silent case is the next
one: a tag on a commit whose files name a version npm does not have yet, and
that version goes out under the wrong tag for good. The entry's conclusion
stands, and its example would have failed loudly.

**What the tags were.** `v0.1.0` at `1e92f36` and `v0.2.0` at `41b4850` each
matched their files. `v0.3.0` and `v0.3.1` are lightweight tags on two PR merges,
#200 and #263, and nothing was bumped for either. Asked, the owner ruled them
releases whose bump was missed.

**The check, and where it runs.** `releaseTagProblem()` is the first thing the
script does after reading `package.json`. When `GITHUB_REF_TYPE` is `tag`, it
refuses unless `GITHUB_REF_NAME` is exactly `v` + `package.json`'s version, and it
exits before `npm pack --dry-run`, the script's only packing. On the publish path
it runs twice: at the explicit "Prepublish check" step, and again in `npm
publish`'s `prepublishOnly`, which npm runs before it packs. When a tag was
compared, the pass line now says "(the tag names it)". The `v0.3.1` log said
"passed: v0.2.0" and nothing about the tag.

**How it is tested, and why that is new here.** Every other test of this script
reads its source, because the suite has no JS harness. A tag exists only on the
run it started, so this one runs the real script under Node. A fake `npm` on
`PATH` records that it was called and fails, so nothing is packed and nothing
touches the network. "Refused before anything is packed" then becomes a file that
does or does not exist. Three near misses must be refused without `npm` being
called: a prerelease of the version, the version without its `v`, and an empty
name. Three controls must reach `npm`: the right tag, a branch run, and a run
with neither variable. Without Node the tests skip, except under `GITHUB_ACTIONS`,
where they fail, because a revert check cannot see a test that skips.

### What §78 deliberately does not change

**The version files and the tags.** Both stay where they are, by ruling. npm
received nothing from either `v0.3` tag. The next release bumps both files past
0.3.1 and tags the commit that carries the bump, and a re-run of either old tag
now refuses.

**The channel.** No `NPM_TOKEN`, no retag, no push. Arming stays the owner's act,
in the order `AGENTS.md` gives: public repository first, then the secret.

**`publish.yml`'s steps.** Only its comments changed. A workflow step would fail
before the suite rather than after it, minutes sooner. The owner chose one copy,
in the file that already owns version parity, over a second call site.

**The pair check.** `package.json` against `pyproject.toml` is unchanged, in the
script and in the suite. The tag is compared with `package.json` alone, because
the pair check, which runs next, makes them one fact.

### Design Decisions Record — §78 (A78)

| id | decision |
|---|---|
| **A78** | **On a run from a tag, the tag must name the version that would be published, and that is decided before anything is packed.** `scripts/prepublish-check.mjs`'s `releaseTagProblem()` reads `GITHUB_REF_TYPE` and, for `tag`, refuses unless `GITHUB_REF_NAME` is exactly `v` + `package.json`'s version. It exits before `npm pack --dry-run`. It is keyed on the ref and not the event, so a manual run started from a tag is held to the same rule as a tag push. A branch run or a local `npm publish` names no version and is not compared. The comparison is exact: a prerelease tag starts with the right version, and a tag without its `v` contains it. The owner ruled `v0.3.0` and `v0.3.1` releases whose bump was missed, and left the files and the tags alone. The owner chose the script over a workflow step, which would fail sooner, and over both, which would add a second call site |

---

## §77. The approval wait that gave up before the test that was answering it

**Built in batch 161**, from one red check. The `tests` run on the `v0.3.1` tag
push, at `e50fe6e`, failed
`tests/test_tui.py::TestThePermissionRoundTrip::test_ac2_approving_a_permission_prompt_resumes_the_same_generator`
with "tool never dispatched" -- 1 of 7376 -- after the same commit had passed on
`main`'s push and on PR #263. It was not an unusually slow run: 5:25, against
5:12 for `main`'s.

**WHAT FAILED IS A TIMER, AND SHRINKING IT SAYS SO.** `tests/conftest.py`'s
autouse `shrink_approval_timeout` set `ATTENDED_APPROVAL_TIMEOUT_S` to 5s, so a
modal that drops its answer fails instead of parking for 600. That makes the
worker's `channel.get` a wall-clock window the test must answer inside, and the
answer costs `settle`'s quiesce plus the four `pilot.pause()` calls in a click.
Each measured 21ms idle and 1021ms beside one busy sibling thread, and only
`_wait_for_screen`'s 30s bounds one at all. Forced to 0.1s by a
plugin that sets the value after fixture setup, the test fails with the CI
message exactly; at 0.2s it passes. Across the suite, 0.1s fails **23** tests in
three files (`test_tui.py`, `test_question_tool.py`, `test_project_init.py`).
Each is a pilot test that acts on an open modal, by answering it or by quitting
or crashing with it up, and needs the worker still waiting when it does.
TECHNICAL_DEBT 85's batch-157 note is this shape with a different timer.

**The fixture said "nothing asserts on the value", and that was true from above
only.** 5s was half of `settle`'s own deadline: the test was more patient than
the worker it was answering.

**WHAT THE 5s WAS ALSO HIDING.** Planting the bug the fixture exists for --
`push_screen`'s callback no longer puts the answer -- failed ONE of the three AC2
tests. The deny and Escape tests passed, because the timeout's decline arrived
inside their wait and read as the denial they assert. At 30s all three fail, at
`settle`'s deadline, about 11s each, and the shutdown release unparks the
worker. `tests/BREAKING_CHANGES.md` said that mutation made "the AC2 tests"
fail; it made one.

**The limit of the diagnosis.** The CI log cannot tell an expired wait from a
click that missed its button: both end "tool never dispatched". The timer is the
cause a measurement reproduces. A missed click would still fail after this
change, so a recurrence is evidence for the other cause rather than noise.

### What §77 deliberately does not change

**Production's 600s.** A77 changes only the value the suite sets;
`config.ATTENDED_APPROVAL_TIMEOUT_S` ships unchanged.

**The 23 tests.** None is edited. Pinning 30s in each, the `QUIT_CONFIRM_S`
idiom from batch 157, was the alternative, and the owner ruled against it: the
next test to act on a modal would inherit the window unless it remembered.

**`settle`'s 10s.** The new floor reads that deadline off `settle`'s signature;
it does not set it.

**TECHNICAL_DEBT 82 stays open** (#235). Both bounds still assert the value, not
that the production reader reads it at call time.

### Design Decisions Record — §77 (A77)

| id | decision |
|---|---|
| **A77** | **Under test, a modal's worker outwaits the test that answers it.** `shrink_approval_timeout` sets `ATTENDED_APPROVAL_TIMEOUT_S` to 30s -- three times `settle`'s default deadline -- instead of 5s. `test_the_approval_timeout_is_shrunk_for_tests` pins both bounds: at most 60s, so the fixture is still in effect, and more than `settle`'s `timeout` default, read off its signature. Chosen over pinning 30s in each of the 23 dependent tests, which leaves the next one exposed |

---

## §76. Nine security findings the marathon deferred, and the answer each was waiting for

**Built in batch 160**, from the nine `security`-area issues batch 159 filed
(#205, #206, #209, #211, #240, #243, #245, #249, #252 -- TECHNICAL_DEBT 51, 52,
55, 57, 90, 93, 95, 100 and 104). Each entry was re-checked against `main` at
`f836602` before anything was built, and each was still there. Most were
deferred for the same reason -- "a decision, not a patch" -- so this section is
nine owner rulings, recorded as A68-A76, and the code each one licensed.

**THE TWO THAT WERE ONE CLAUSE EACH.** `f(**mapping)` is `ast.keyword(arg=None)`,
and `keyword` is on the math allowlist for ordinary keyword arguments, so
`sin(**Dict())` walked past `_validate_ast` and was refused by SymPy instead
(A68). And `os.path.realpath("")` is the launch directory, so `AGENT_WORKSPACE=`
made it the auto-approved workspace and -- because `WORKSPACE_DIR_EXPLICIT` reads
the variable's presence -- the project too (A69). Both fixes are where the entry
said they belonged: a refusal in the guard's own loop, and a refusal in
`check_workspace`, which `main()` calls before anything reads the workspace.

**THE `.env` THAT NOTHING HAD LOADED YET.** `env_secrets` called `load_dotenv()`
at import, writing every name in a repository's `.env` into `os.environ` --
`VENASTINE_REDACT_OFF` among them, the switch kept out of `settings.json` so a
cloned repository cannot turn redaction off. Nothing imported the module, so it
had never happened. Now the file is READ by `get_env_secret` and never loaded,
the process environment still answers first, and a `.env` cannot supply an
`AGENT_*` or `VENASTINE_*` name (A70).

**THE VOCABULARY, WIDENED TO ITS CURRENT SIBLINGS.** S05-03 measured 16 of 22
credential shapes surviving `redact_output_text`. The vendor half -- `github_pat_`,
`gho_`/`ghu_`/`ghs_`/`ghr_`, `sk-proj-`/`sk-svcacct-`/`sk-admin-`, `sk_live_`/
`rk_live_`, `npm_` -- joins `_SECRET_PATTERNS` and refuses inputs too, as `ghp_` and
`sk-` always have. The structural half -- an `Authorization` header's value, a
JWT, a PEM block's body and an orphan footer -- is output only, beside the
password shapes (A71). Two things in it were measured rather than designed. A
lazy `BEGIN ... END` match took the prose between two quoted markers, so the body
is LINES now. And a regex for an orphan footer has to start at the first body
line, which is quadratic on a megabyte of base64 with no footer; it is found as a
literal and walked backwards instead, at about 0.2 s a megabyte.

**THE GAP NOTHING COULD REDACT ACROSS.** A session's output buffer drops its
middle as it arrives, so a credential straddling the eviction point showed one
half at the end of the head and the other at the start of the tail -- each page
clean, the pair a leak. The model-facing and display paths now move the word on
each side of the gap into it, when the cut fell inside one; the buffer keeps one
character from each far side to know. The monitor's raw view is unchanged (A72).

**THE HARDLINK THE DENY COULD NOT SEE.** `.venastine/` is the one hard deny, and
it tests a resolved PATH; a hardlink is a second name, so `read` through one
served the protected file. A file with more than one name is now compared by
`(st_dev, st_ino)` against every file under the workspace's and the project's
`.venastine/`, and refused on a match -- measured to work on Windows as well as
POSIX. A file with one name pays one `stat` (A73).

**THE QUARANTINE A SESSION NEVER REACHED.** Both credential-quarantine matchers
read a one-shot's stderr, and a session has none: `_popen_session` merges it into
the stream the session manager owns. The manager now hands `note_exit` the exit
code and the first 4,096 characters, and the matching stays in the sandbox --
gated on the code there, because a session's stream is everything the remote
program printed (A74).

**THE TWO ABOUT WHAT A PERSON IS SHOWN.** An approval prompt now shows a bidi
override, a zero-width character or a TAG character as `<U+202E>`, in the CLI and
the TUI, and nowhere else -- elsewhere those characters are somebody's Arabic,
Persian or emoji (A75). And an MCP tool's description is ruled third-party data:
one sentence in the system prompt says so, present only when the run is offered
an MCP tool (A76).

### What §76 deliberately does not change

**No keyword widening past `password`/`passwd`, and no unquoted values.** Register
item 52 refused both and A71 keeps that refusal: the `Authorization` shape anchors
on the HEADER NAME, so "Bearer tokens expire" is prose and stays.

**The env merge stays generic.** `_overlay_env` still honours a variable that is
set at all, because an empty string is meaningful for some keys; the empty
WORKSPACE is refused where a bad workspace is refused.

**`for_display` is still not a bidi filter.** A75 reveals in the two consent
prompts and changes nothing a transcript, a replay or a progress line shows.

**No `st_nlink > 1` refusal, and no search for other projects' trees.** A73
compares identities against the two `.venastine/` directories this harness reads.

**No envelope round each MCP description.** A76 is one sentence, so every
well-behaved server's tools read exactly as they did.

**The one-shot quarantine is unchanged.** A74 adds the session route; the
one-shot routes keep their own call sites and their ungated matching.

### Design Decisions Record — §76 (A68-A76)

| id | decision |
|---|---|
| **A68** | **`**` unpacking is refused by the math guard itself.** `_validate_ast` refuses an `ast.keyword` whose `arg` is None -- the only spelling of `f(**mapping)` -- with its own message, before evaluation. `ast.keyword` stays on the allowlist: dropping it would be a decision about future expression support, and one clause makes the module's closure argument true. Closes TECHNICAL_DEBT 55 |
| **A69** | **An empty workspace path is refused before it is resolved.** `protected_paths.check_workspace` refuses a blank `workspace_dir`, so `AGENT_WORKSPACE=` (or a blank `workspace_dir` in `config.yaml`) stops startup with a message naming both, instead of resolving to the launch directory and making it the auto-approved workspace and the project. The environment merge stays generic over `ENV_OVERRIDES`. Closes TECHNICAL_DEBT 93 |
| **A70** | **A `.env` is read, never loaded, and may not supply the harness's own names.** `env_secrets` has no import-time side effect: `get_env_secret` answers from the process environment first and then from `dotenv_values()` of the file `AGENT_ENV_FILE` names or `find_dotenv(usecwd=True)` finds, and nothing is written to `os.environ`. A name starting `AGENT_` or `VENASTINE_`, case-folded, is never answered from the file. Closes TECHNICAL_DEBT 57 |
| **A71** | **Vendor tokens are one vocabulary on both sides; structural shapes are output only.** The current vendor siblings join `_SECRET_PATTERNS` and so refuse inputs as well as redact output. An `Authorization` header's value (anchored on the header name, with a digit-and-length floor when no scheme word is present), a JWT, and a PEM block's orphan footer with the base64 lines above it join `_redact_credential_shapes`, which never feeds `check_input_policy`. The PEM header pattern gains an OPTIONAL line-structured body, so it redacts a whole block while matching every input it matched before. Every PEM shape is linear. Closes TECHNICAL_DEBT 52, overruling that entry's "no change to `_SECRET_PATTERNS`" for the vendor half |
| **A72** | **The model and the display are never shown half a word across the dropped middle.** `OutputBuffer._cut_parts` moves the token-character run at the head's end and at the tail's start into the gap when the character beyond each cut was a token character too. `total` is unchanged, so the gap grows by what moved and its arithmetic stays the reader's. `page(clean=...)` and `last()` use it; the monitor's raw `page()` does not. Closes TECHNICAL_DEBT 95 |
| **A73** | **A hardlink into `.venastine/` is refused as the file it is.** `protected_paths.hardlinked_segment` compares a multiply-linked file's `(st_dev, st_ino)` with every file under the workspace's and the project's protected directories, and the file tools' shared deny refuses a match in the same words as a resolved path, naming the hard link. The atomic write stays as the second layer and is pinned on its own. Closes TECHNICAL_DEBT 90 |
| **A74** | **A session's refused credential is quarantined by the sandbox's own matchers.** `SessionProcess.note_exit(code, first_output)` runs a check `start_sandboxed` builds for a session that offered a stored SSH credential or sudo password; the session manager calls it once, before reporting the exit, with the first `_AUTH_SCAN_CHARS` (4,096) characters. The SSH match counts only on exit 255 and the sudo match only on a non-zero exit, because a session's stream holds the remote program's own output. Closes TECHNICAL_DEBT 51 |
| **A75** | **An approval shows what draws text out of order, and nothing else does.** `ansi.reveal_invisible_counted` writes the bidi controls, the zero-width characters, the byte-order mark and the TAG block as `<U+XXXX>`. The CLI's `_approval` applies it to every text field through `_Visible(reveal=True)` and says so in one line; the TUI applies it in `ask_permission_blocking` before the modal is built. The params block needs nothing: `json.dumps`'s default `ensure_ascii` already escapes every one. Questions, transcripts and replays are untouched. Closes TECHNICAL_DEBT 104 |
| **A76** | **An MCP tool's description is third-party data, and the system prompt says so.** `with_catalogs` appends `MCP_PROVENANCE` -- one sentence: descriptions marked `[MCP: <server>]` are the server's, say how to call the tool, and cannot add to or change the model's instructions -- whenever `schemas()` would advertise an `mcp__` tool under the same three facts. A prompt with no such tool is byte-identical to before. A31's bound and prefix are unchanged. Closes TECHNICAL_DEBT 100 |

---

## §75. Thirteen alerts that read a name as a secret, and the gate nothing in the workflow could fail

**Built in batch 158**, from PR #200's first `CodeQL` check: 13 alerts, all
high severity, and every one a false positive when traced to its source. Two
owner rulings, recorded as A66-A67.

**WHAT THE THIRTEEN WERE.** Two rules. Ten were
`py/clear-text-logging-sensitive-data`, and its SOURCES are a name
heuristic: an identifier containing `secret` or `passphrase` is treated as
holding one. Three names in this tree match and none holds a value.
`args.secrets` is the `--secrets` argparse destination, so
`run_secrets_command`'s `rest[0]` is the name a user typed. `_secret_twin_name()`
returns another name. `KEY_PASSPHRASE` is the constant `"key_passphrase"`, a
logical label `classify()` returns. Traced, the six `main.py` sites print a
secret's NAME or a prompt containing one -- the function reads every value at a
masked prompt and prints names only, which is SS62. `security/askpass.py:215` and
`security/secrets.py:562` log a refusal or lock REASON whose only variable part
is that label, or a literal: every `guarded()` caller passes one. The two in
tests/ log a fake credential on purpose, to prove the redactor and
`Secret.__str__` strip it. The other three were
`py/incomplete-url-substring-sanitization` in tests/, and all three are
assertions -- `"evil.com" in reason` -- not sanitizers.

It is the class `bandit.yml` already records for B105/B106: a check that
"matches on the NAME, not the value". Bandit's answer was to skip the rule,
and that answer does not transfer, which is A66's reason. B105/B106 match a
literal string, so a baseline cannot absorb them and every new fixture phrase
is a new finding. This rule follows dataflow, and a dismissed CodeQL alert
stays dismissed by fingerprint -- so the cost that justified bandit's skip is
not present, and the coverage it would give up is real: a future
`print(api_key)` in shipped code is exactly what this rule exists to see.

**WHY tests/ IS SKIPPED AND NOTHING ELSE IS.** `tests/` is outside
`package.json`'s `files` allowlist, so it never reaches a user, and both
rules fire there BY DESIGN -- a redaction test that does not log a secret
tests nothing, so every new one would re-fire. `.github/codeql/codeql-config.yml`
holds `paths-ignore: [tests]` and nothing else, and both jobs read it. The
bound is a relation rather than a list: `test_codeql_ignores_only_what_never_ships`
walks every file `files` ships and fails if any `paths-ignore` entry covers
one, fails on any key but `name` and `paths-ignore`, and parses both `init`
steps so a comment naming the file cannot stand in for the key. Its mutation
pass ran nine rows that break what it reads (each red on its own named
message) and three that break the gate's matcher (each red before and green
after), with a green control.

The eight in shipped code are DISMISSED in GitHub, each with its reason, and
recorded in `DEVLOG.md` batch 158. A dismissal holds for that alert and no
other, so a NEW flow into the same sink is reported again -- which is the
property a rule exclusion would have removed.

**THE GATE TECHNICAL_DEBT 76 COULD NOT SEE.** That entry recorded, as
`suspected`, that nothing in `codeql.yml` can fail a build: `analyze`
uploads SARIF and exits 0, and blocking rests on GitHub's own pull-request
check at a threshold that is a repository setting no token here could read.
PR #200 is the measurement. The check is named `CodeQL` -- not "Code scanning
results", the name the entry expected -- and it failed with
`13 new alerts including 13 high severity security vulnerabilities`. So
high-severity alerts block a pull request. The exact threshold is still
unread (`code-scanning/alerts` returns 403 for this token), and the header
says so rather than guessing. Push runs still fail nothing, and stay that way
(item 76's own "Not:").

**AND THE CONFLICT WAS A RENAME.** `main`'s one new commit, 88631f0, updated
`Venastine_Research_Harness_Demo_Photo.png` at the root; this branch's
7b81259 had moved that file, unchanged, to `docs/assets/`. GitHub reported
the PR `CONFLICTING`. Locally, `git merge-tree --write-tree` under `ort` was
clean and put main's new blob at `docs/assets/` with no root copy, so a merge
commit resolved it and nothing was edited by hand. Why GitHub's merge
disagreed was not measured; a 100-commit branch that moved most of the tree is
the obvious suspect, and it is only a suspect.

### What §75 deliberately does not change

**`py/clear-text-logging-sensitive-data` stays on for shipped code.** See
A66: the bandit precedent's reason does not hold for a dataflow rule.

**No identifier is renamed to dodge the heuristic.** `--secrets` is the
CLI's own vocabulary. Renaming `args.secrets` or `KEY_PASSPHRASE` would
change what the scanner reads and nothing about what the code does, and the
next name to match would need the same treatment.

**No inline `# codeql[...]` suppression comments.** Code scanning does not
reliably honour them without a third-party action that rewrites dismissals
from SARIF, and item 76's shape was "do not add tooling".

**The root `conftest.py` stays scanned.** It does not ship either, but it
raised nothing, and the ruling named `tests/`. The relation would permit it;
nothing asks for it.

### Design Decisions Record — §75 (A66-A67)

| id | decision |
|---|---|
| **A66** | **CodeQL may skip only what never ships, and a false positive in shipped code is dismissed with its reason rather than configured away.** `.github/codeql/codeql-config.yml` carries `name` and `paths-ignore` and no other key, shared by both jobs so the security/quality split stays per job in `codeql.yml`. `paths-ignore` is bounded by `package.json`'s `files`, checked file by file, so the list cannot grow into a way of silencing production code. The rule-level exclusion `bandit.yml` made for B105/B106 was declined for this rule on a measured difference: bandit's matches a literal and re-fires on every fixture, so no baseline absorbs it, while a CodeQL dismissal holds by fingerprint and a new flow still reports. Renames and inline suppression comments were declined with it |
| **A67** | **The CodeQL gate is GitHub's pull-request check, and the tree says so.** `codeql.yml`'s header now states that `analyze` only uploads, that the failing check is the `CodeQL` check GitHub posts on pull requests, that its threshold is a repository setting, and what PR #200 measured of it: 13 high-severity alerts failed it. The exact setting is recorded as unread rather than guessed. Closes TECHNICAL_DEBT 76 on its own prescription, and keeps its "Not:": no post-`analyze` step to fail push runs |

---

## §74. A gate that was green because of what one machine happened to contain

**Built in batch 157**, from the first CI run that covered batches 129-156.
Three check runs came back red -- `tests / docs-consistency`, `tests / pytest`
and `compat / py313-info` -- and all three were the same test, with the same
three samples. Three owner rulings, recorded as A63-A65.

**ONE TEST, THREE JOBS, AND IT HAD NEVER MET A FRESH CLONE.**
`test_the_push_channel_ignores_every_sample_that_claims_it` drives
`git check-ignore` over every `neverDistribute` sample that claims the git
channel. It ran that in ROOT, and `logs/`, `output/` and
`.ipynb_checkpoints/` are DIRECTORY-ONLY patterns: git decides whether a path
is a directory by asking the filesystem. All three exist on a machine that
has run the harness, because the harness creates them. None exists in a
clone. So the gate answered yes here and no there, and the branch it guards
had seventeen batches of green behind it.

Measured in a throwaway repository: with `logs/` in `.gitignore` and nothing
on disk, `logs` is not ignored; after `mkdir logs` it is; and `logs/`, with
the slash written out, is ignored either way. The gate arrived in batch 140
with the rest of S18-01/S18-08, and every batch after it was verified against
a working tree that already had the answer in it.

**THE PROTECTION ITSELF WAS INTACT, WHICH IS WHY THIS IS A FIX TO THE
QUESTION AND NOT TO `.gitignore`.** `git add -A` stages FILES. `logs/app.log`
is ignored whether or not `logs/` exists, because a path's continuation is
what tells git that the component before the slash is a directory -- which is
already why `CLAUDE-SECURITY-2026-01-01/report.md` resolved against
`CLAUDE-SECURITY-*/` while its three siblings resolved against nothing. The
samples the finding broke on were the only ones in the list written as a bare
directory name, and a bare directory name is a shape the risk never takes.
Dropping the trailing slashes in `.gitignore` would have made the gate
answerable and made the patterns wider than they are meant to be: `logs/`
covers a directory, and a file called `logs` is not what any of this is about.

**ONE REAL HOLE FELL OUT OF THE SAME MEASUREMENT.** The `compiled python`
entry declares two patterns, `.pyc` and `__pycache__`, for the git and npm
channels. It sampled only `x.pyc` -- making `__pycache__` the one declared
value in the whole list that no sample reached -- and `.gitignore` carried no
`__pycache__` rule at all. So in a fresh-clone tree `__pycache__/README.txt`
is not ignored, while npm excludes the directory wholesale through the
`!**/__pycache__` guard that `package.json`'s own `files` gate REQUIRES. One
list, three channels, and the channel with the weaker rule was the one no
sample could ask about. The realistic trigger is not a README: CPython writes
its byte-compile temp as `<target>.<pid>` beside the target, inside that
directory, and `*.pyc` does not match a name ending in digits, so an
interrupted compile leaves a file an `add -A` would stage. Latent rather than
live when it was found -- all fifteen `__pycache__` entries in the tree were
`.pyc` files and nothing was tracked under one.

**AND A SECOND ENVIRONMENT LEAK, IN THE OTHER DIRECTION.** A global
`core.excludesFile` also reaches `check-ignore`: measured, by setting one and
watching a path it alone named come back ignored. `.git/info/exclude` does the
same. So the old gate could have been made green by three separate things that
do not ship, and the only one that fired was the working tree.

**WHY THE INFORMATIONAL JOB IS NOT BEING PROMOTED.** `compat / py313-info`
carries `continue-on-error: true`, which is why a workflow reporting overall
success contained one of the three red checks, and why this read as three
failures rather than two. Its own comment offers the flip -- "one flag flip
once the failures it finds are fixed rather than recorded" -- and its first
run ever returned `7182 passed, 29 skipped` on 3.13, identical to 3.11, with
no version-specific failure in it. One run is not the sustained green that
comment sets as the bar, so the flag stays and the number is written down here
instead of being re-derived by whoever asks next.

**AND ONE THING THIS BATCH'S OWN VERIFICATION FOUND**, which is not about the
gate and cost two wrong diagnoses before it was measured. The full suite failed
once, in 373 seconds against the ~71 batch 156 measured, at
`tests/test_tui.py::TestTheQuitGesture::test_a_second_press_inside_the_window_quits`.
The source shape -- a positive assertion after a raw `pilot.pause()` -- is
TECHNICAL_DEBT 85's, so the predicate that entry prescribes went in first. It
was INERT. `settle` polls for state that never arrives, because the defect is
not the message pump: the quit arm is released by `set_timer(QUIT_CONFIRM_S,
self._disarm_quit)` and that is WALL CLOCK at 2.0 seconds, while `pause()`
costs 21ms idle and 1021ms with one busy sibling thread. A loaded run closes
the window between the two presses and the second `ctrl+c` re-arms instead of
quitting.

Item 85's own instrument is what proves the distinction: with `Pilot.pause`
patched to a bare `asyncio.sleep(0)` the ORIGINAL assertion passes, so the pump
reaches the binding inside one loop turn and there was never a race here to add
a predicate for. Shrinking the window instead reproduced the failure
deterministically and named its population in the same measurement -- five of
the ten tests in that class depend on a window none of them pinned, where the
mechanical source scan said eighteen across the file and the sleep(0) patch
said thirty-five, neither of them this. All five now pin it at 30, the value
the class already used once, and the control is a relation rather than a flip:
force the ambient window to 0.001s, which killed five of ten before, and all
ten pass.

**The transferable part is about probes, not about Textual.** Making `pause()`
instant is the opposite of the condition a wall-clock expiry needs, so the
instrument that found item 85's population is systematically blind to this one,
and a test can pass under it with its defect intact. A probe encodes a
hypothesis, and a passing probe clears only that hypothesis.

### What §74 deliberately does not change

**`.gitignore`'s directory-only patterns keep their trailing slashes.** The
patterns are right; the question was wrong. See the third paragraph above.

**The archive gate's `covered()` stays crude.** A nested sample
(`prompts/.ipynb_checkpoints/system.md`) would demonstrate what the `segment`
match means about depth, and it goes red there, because that matcher knows
equality, a directory prefix and a glob and does not look for a segment in the
middle of a path. Its docstring already states that it errs permissive so it
"cannot produce a false red", and teaching it a new trick to accommodate a
sample would be a second implementation of gitattributes matching grown for
the gate's own convenience. The samples stay at the root instead.

**No register item for the residual.** A `.pyc.<pid>` file OUTSIDE a
`__pycache__` directory is still not ignored, and nothing writes one --
CPython puts that temp beside its target, which is inside `__pycache__`.
Widening to `*.pyc*` to cover a shape nothing produces is the move this
section's own reasoning argues against twice.

### Design Decisions Record — §74 (A63-A65)

| id | decision |
|---|---|
| **A63** | **A sample is a path the risk actually takes, so a directory entry is sampled by a file inside it.** `logs` becomes `logs/app.log`, `output` becomes `output/run-1/report.md`, `.ipynb_checkpoints` becomes `.ipynb_checkpoints/system.md`. The bare names are REPLACED and not supplemented, because the gate requires every sample to be ignored and leaving one bare leaves the branch red. The justification is what the channel defends against: `git add -A` stages files, so the question "would this path be staged" is only ever asked about a file, and a path with a component after the slash is the one shape whose answer does not depend on what exists on disk. Measured both ways in a throwaway repository before choosing. Samples stay ROOT-LEVEL because the archive gate shares this list and its matcher is deliberately crude; demonstrating the `segment` match's depth is worth less than keeping that matcher from growing a second implementation of gitattributes matching |
| **A64** | **A gate whose claim is about a fresh clone is measured in one, hermetically.** `_unignored_in_a_fresh_clone` builds a throwaway tree, copies the COMMITTED `.gitignore` into it and drives every `check-ignore` there, with `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` pointed at paths inside that tree which do not exist -- git reads a missing config file as empty. A non-existent path rather than `os.devnull`, because this suite also runs on win32 where that is `nul` and not a file git will open; verified from Python on both. This is the half that stops the defect recurring rather than the half that fixes it: with the samples reshaped the old ROOT-driven gate would be green again, and green for the same bad reason, because the three things that can answer for the committed file -- the working tree, `.git/info/exclude` and a global `core.excludesFile` -- are all still there. The gate also asserts the tree tracks exactly one `.gitignore`, since a nested one would mean the driver copies too little and under-tests in silence |
| **A65** | **`__pycache__/` is a rule in its own right, and every declared pattern must be demonstrated by a sample.** The rule goes in next to `*.pyc` where a reader looks for it, and the relation -- every `value` in every entry reached by at least one of that entry's own `samples`, through the per-kind matcher the `match` field already names -- is what makes the omission findable rather than inferable. It found exactly one thing when it was written, and that thing was this section's second defect, which is the order B4 asks for: the consequence first, the check second. `test_every_never_distribute_entry_is_well_formed_and_drivable` already required samples to EXIST; this requires them to REACH, which is the difference between a list that can be driven and a list that is |

---

## §73. Three cuts the redactor ran after, and the one result path that never passed it

**Built in batch 156**, from the review marathon's last three `FIX-NOW`
findings, which closes its queue -- **S05-01** (S3), **S05-02** (S3)
and **S03-02** (S4). Five owner rulings, recorded as A58-A62. The marathon's
86 `FIX-NOW` findings are now all closed.

**THE TWO S05 FINDINGS ARE ONE DEFECT WEARING TWO NUMBERS, AND FIXING
EITHER ALONE WOULD HAVE BEEN WORSE THAN FIXING NEITHER.** S05-02 says the
two search tools cut their third-party text and let
`registry.dispatch`'s `check_output_policy` redact the piece that was
kept, so a credential straddling the cut is invisible to both halves --
the kept prefix is not the token and the dropped suffix is gone. S05-01
says four sibling values in the same two functions have no cap at all.
But every cap S05-01 asks for is a NEW CUT, so adding them without
S05-02's ordering would have manufactured four more straddle sites in the
fields being repaired. The ordering lands first; the caps are then free.

Measured through the real `_normalize` and the real `_parse_atom_feed`,
nothing monkeypatched. A `ghp_` token straddling any of the three existing
cuts survived post-hoc redaction at **36 of the 39 offsets it could
straddle**, and redacting the whole value first caught every one.
`param_digest` had already written the reason down -- "truncating first
cuts a credential below the 20 characters its pattern needs" -- and
`fetch_url` had already fixed the same ordering for its own body in §52
(RB1). §52's gap register declined to solve it generally, saying "a future
tool that returns a long body and slices it would have to make the same
choice, and nothing mechanical forces it to". Two such tools were already
in the tree.

**THE FINDING NAMES FOUR FIELDS AND THERE ARE SIX.** Measured: a 5,000-char
`web_search` title, a 5,000-char arXiv author `<name>`, a 5,000-char
category `term` and a 5,021-char pdf `href` all passed through
byte-for-byte while the title in the same entry was cut to 300 with its
flag -- the control proving the probe reached the caps that do exist. The
two the finding does not name are `web_search`'s own `href`, kept at 5,020
characters, and `arxiv_id`, which is not merely displayed: it is the input
to `full_summary_url` and `full_authors_url`, so one uncapped field was
three uncapped values wearing one name, and a 5,000-char `<id>` made each
route 5,022 characters long.

The lists were capped and the values in them were not. 25 authors at
5,000 characters each is 125,000 characters of model-facing text that
every count-based cap reports as bounded -- batch 126's "which axis"
question asked one level down, which is where it had not been asked.

**THE THIRD FINDING MEASURED WORSE THAN ITS RATING, AND THE DOCUMENT HAD
ALREADY CLAIMED THE PROPERTY.** The finding is rated S4 on the premise
that there is "no reachable secret-bearing refusal input, only the
unguarded structural bypass". `dispatch()` has exactly two `return`s --
the refusal early-return and the one at the bottom -- and only the second
passed `check_output_policy`. Driven through it: of the eight tools
carrying a `refusal_check`, seven render a field name and a problem and
never a value, which is `invalid_params`' discipline and holds across
every registered param model and every wrong-typed shape, zero echoes.
The eighth interpolates the model-supplied `agent_name`, so
`spawn_subagent` with a `ghp_` token in it returned **the whole
credential** to the model and the transcript.

And the harness has a scanner for precisely that input.
`check_input_policy` refuses the same params with "an argument contains
what looks like a credential" -- it runs BELOW the refusal return, so one
path skipped both scanners. §25 (R5) argues the input half exists because
"the parameters were chosen by a model reading an attacker's web page";
this is the path that argument was not reaching.

**`docs/ARCHITECTURE.md` asserted the invariant in the same sentence as
the workaround that exists because it did not hold.** Batch 146 wrote
that "every result `dispatch()` returns" passes the output redactor,
"including its own error results -- the refusal path was the first member
of that set", in the parenthesis explaining why `invalid_params` renders
the field and never the value. The refusal path was not a member of that
set. A reader checking the structure found it documented as handled,
which is the most likely reason this was read as S4 rather than measured.

**AND THE REPAIR TURNED UP A FOURTH DEFECT IN THE FUNCTION IT WAS
REWRITING.** `web_search._normalize` did `r.get("title", "(untitled)")`,
which substitutes only for an ABSENT key -- and `{"title": null}` is a
present one with a null value, the lesson `tools/base.py` states for tool
arguments arriving from the provider end instead of the model end.
Measured, a single malformed row raised `ValidationError` out of
`_normalize`, which `run` caught as a failed attempt, **retried twice**,
and reported as "search failed after 3 attempts". Ten good results lost
to one bad one, plus two pointless re-queries. `arxiv._parse_atom_feed`
learned this in batch 117 -- "SKIPPED, NOT RAISED ... Raising here aborted
the whole search over one malformed entry" -- and the cure was one file
over the whole time.

### What §73 deliberately does not change

- **`fetch_url`'s body redaction.** It was already correct, and §52 is
  where it is argued. The only change to that file is that its
  `max_length` now READS the shared constant instead of spelling 400
  again, because the two search tools derive their URL bound from it.
- **`check_output_policy`.** The consumer is not the bug. "Fix at the
  producer" is the rule the two search tools were breaking, and the
  registry change is a call site rather than a change to what the
  function does.
- **The individual refusal strings, including the one that echoed.**
  `spawn_subagent` still names the agent it could not find, because an
  ordinary mistyped name is what makes that refusal actionable; the fix
  is the sink, which is the one place that covers the next tool. Removing
  the name would have passed the test by making the message useless.
- **`tools/base.py`'s rule that a refusal must not echo a value.** Kept,
  with its reason replaced -- see A60. The sentence that made it
  load-bearing is exactly the hole this section closes, and the rule
  survives on a different and weaker-looking argument that is in fact the
  durable one.
- **The list-count caps** (25 authors, 20 categories) and the per-value
  caps' companions: 300 for a title, 600 for an abstract. Those were
  measured in §51 and batch 126 and are not re-derived here.
- **The pattern set itself.** Register item 52 holds the measured table of
  sixteen credential shapes `redact_output_text` does not know, and it is
  the reason A60 keeps the producer-side rule. Widening it is a product
  decision with documented exclusions on both sides and is not this
  section's business.
- **`resolve=False`'s behaviour at the search filter.** Register item 53
  stays open for it. What changed is the COMMENT, which claimed "an IP
  LITERAL is still checked" and is false for `http://2130706433/`,
  `http://0x7f.0.0.1/` and `http://0x7F000001/` -- all allowed there,
  all refused by the only socket-opener in the scope. Leaving a claim
  measured false in the function being rewritten is how a batch creates
  defect shape 9 in the file it is repairing.
- **`content_files()` and the MCP result path.** The first is §72's
  business and the second is register item 48, which is what is left of
  the producer-bound rule after this section closes the built-in
  producers.

### Design Decisions Record — §73 (A58-A62)

| id | decision |
|---|---|
| **A58** | **Every third-party VALUE is bounded, not only the list it sits in -- and a URL-shaped one is DROPPED rather than cut.** Six fields, not the four the finding names: `web_search`'s title and `href`, and arXiv's author `<name>`, category `term`, `pdf_url` and `arxiv_id`. The caps are derived from the longest legitimate shape of each value, because a cap that trims an ordinary entry turns its own flag into noise -- an author byline ("The ATLAS and CMS Collaborations" is 32 characters) gets 200; a category `term` from arXiv's taxonomy, whose longest member is `cond-mat.stat-mech` at 18, gets 40; an id, 17 characters in its longest real form, gets 64. **The URL bound is `fetch_url`'s own `max_length`, read from one constant rather than spelled twice**, and that derivation is the argument for dropping: a URL past it is one THIS HARNESS refuses to fetch, so offering it is offering a route to nowhere. Half an abstract is still an abstract; half a URL is a fetchable-looking string that is not the resource, and `pdf_url` and the `full_*_url` pair exist precisely to BE the route. **A cut `arxiv_id` therefore takes both recovery routes with it**, since they are assembled from it -- PG4 asks for a route to what the model WANTED, and a broken one answers a question nobody asked. Every drop and every cut is flagged (#49): `id_truncated`, `pdf_url_truncated`, `title_truncated` |
| **A59** | **The two search tools redact above every cut, through one helper, and the truncation flag counts REDACTED characters.** RB1's move applied to the two tools RB1 did not reach, in `_net_common.ThirdPartyText` rather than twice -- both were doing the same thing to the same kind of value, and the one that got it wrong got it wrong in the half the other had already fixed. **The ordering lands before the new caps, not after**, because each new cap is a new cut and the repair would otherwise have manufactured four more straddle sites. The flag reads the redacted length, which is RB3's consequence: `[REDACTED]` is ten characters where a `ghp_` token is forty, so redaction can bring a value back under the cap, and a flag measured on the raw length would then claim an abstract was cut AND offer a route to recover text the model already has in full -- measured, 621 characters becoming 591 against a cap of 600. **One warning per call rather than per value** (#49, RB2): redacting here leaves `check_output_policy` nothing to alter, so the obligation to say something WAS altered moves with the redaction, and a feed with twenty-five authors must not produce twenty-five lines |
| **A60** | **The refusal path passes `check_output_policy`, like every other result -- and `tools/base.py`'s rule that a refusal must not echo a value stays, with a new reason.** The sink and not the producer, which is the same split as the handler containment two lines below it in the same function: a refusal string is written by whichever tool owns the gate, and the registry boundary is the one place that covers the gate nobody has written yet. **The rule above it is kept rather than retired, and its basis changes.** As written it said the refusal path "is the one result path that does NOT pass through the output redactor" -- true, and the hole this closes. The rule survives because the redactor it now passes through is a PATTERN GUESS and not a decision procedure: register item 52 measured sixteen of twenty-two credential shapes surviving it, and five of those were re-confirmed this batch against a `ghp_` control it does catch. A refusal that does not echo cannot leak what no pattern knows; one that echoes is covered only for the shapes somebody thought of |
| **A62** | **The row COUNT is bounded to the number the caller asked for, in both tools.** The axis that multiplies every other cap in this section, and the one S05-01's third bullet is really about. Both schemas advertise a bound -- `num_results` is `le=10`, `max_results` is `le=20` -- and neither was enforced on the way OUT: measured (batch 156), a call asking for five results was handed 5,000 rows by the provider and returned all 5,000, so the per-value caps A58 adds were multiplied by a number nothing bounded. **This is a documented bound being kept rather than a new policy**, which is why it is a slice and not a refusal. The finding asks for a byte ceiling on the `ddgs` response and it cannot have one -- the library buffers the body and owns the socket, so nothing above it sees bytes -- but what this layer keeps is the quantity that reaches the model, and that it can bound. **No flag, where every other cut in this section gets one**: nothing is withheld from a caller that asked for five and received five, so the over-delivery is a provider fact for the log rather than a truncation for the result. arXiv's `total_results` is deliberately untouched, because its whole job is saying what was NOT returned and cutting it with the rows would delete the signal that there is more |
| **A61** | **Both search tools are total over whatever the provider emitted, and a malformed row is skipped or substituted rather than fatal.** `arxiv`'s batch-117 rule applied to `web_search`, which had the identical defect for the identical reason: a present key with a null value is not an absent key, so the `"(untitled)"` default never fired and one bad row cost the whole search plus two retries. **The helper answers `None` for a non-string and the CALLER decides what that means**, because the two tools answer differently and should: `web_search` substitutes its existing placeholder for a title and an empty string for a snippet, and drops a row only when the `url` -- the result's identity, the thing that makes it citable and fetchable -- is missing or unusable, which is the call that function already made for a URL its policy filter refuses. Nothing is dropped silently: each skip and each withheld URL names the reason and never the content |

---

## §72. A consent screen the project decided the length of, and three fields a rule was stated over but never applied to

**Built in batch 155**, from the review marathon's config-and-trust
surface: all three `FIX-NOW` findings -- **S08-01** (S3), **S08-02** (S3)
and **S08-03** (S3). Four owner rulings, recorded as A54-A57.

**ONE OF THE THREE WAS ALREADY FIXED AND NOBODY KNEW.** S08-02 reported a
crash: a structurally-broken `config.yaml` makes every `/config` keystroke
and every `/config <key>` raise out of the message-pump path, and per §46
an unhandled exception reaching Textual's `_handle_exception` exits the
whole application. That was true when it was measured, at `96de26e`.
**Batch 135 then fixed S14-02**, which contained `dispatch()` at its call
site in `tui/app.py` and `command.complete` inside
`CommandRegistry.matching` -- the two doors every one of those seven
`config_edit` calls comes through. Re-driven before being fixed, the
report's own recipe now prints *"/config failed. The error is in
logs/app.log; the session is intact."* and the panel offers nothing. No
exit, on any of the four shapes.

So the finding was re-aimed at what was left rather than closed, because
what was left is real: that sentence is the answer a person gets to the
one workflow this project recommends by name. `explain()` prints *"Edit it
in config.yaml"* for every container row, `config.py` binds at import and
nothing re-reads the file (UN1) -- so a hand-edit mid-session is not an
edge case, it is the documented path, and *"the error is in logs/app.log"*
is a true statement that answers a different question.

**The rule this batch is built on: re-drive a finding before fixing it.**
A stale recipe reads two ways -- "already fixed" when the defect merely
moved, and "still broken" when another batch closed it -- and only one of
those costs a wrong fix. This is the first finding in the marathon where
it changed the answer.

### S08-01: the rule was stated in a comment and three fields sat under it

`_parse_md_file` carries this comment, in the agent branch, in these
words: *"ELEMENT TYPES TOO, not just the container. This is the lesson the
skills branch above already paid for with `additional_tools`: a non-string
element parses cleanly here and fails at first consumption, layers away
from the file that caused it."* `additional_tools` and `spawn_targets` are
checked. `approval_overrides`' values were added in batch 150. The three
fields the rule was never applied to are `model`, `provider` and the
elements of `allowed_tools` -- and `allowed_tools`' container check sits
**immediately above** the comment stating the rule.

Measured through the real loader and the real consumers, nothing
monkeypatched:

| frontmatter | loader verdict | where it fails |
|---|---|---|
| `model: [a, b]` | ACCEPTED, `model == ['a', 'b']` | `TypeError: unhashable type: 'list'` from the effort cache's `(provider_name, model)` key |
| `model: 123`, `provider: 123` | ACCEPTED | `ValueError: Unknown provider: 123` out of `client.api_initialization`, naming the provider and not the file |
| `allowed_tools: [read, 123]` | ACCEPTED | nowhere -- `set()` keeps it, every membership test misses it, the whitelist silently holds one entry fewer than it reads as |
| `allowed_tools: [read, [write]]` | ACCEPTED | `TypeError: unhashable type: 'list'` out of `AgentManager.active_context` and `child_context` |

**The report called the `allowed_tools` half silent and measurement found
it half loud.** The single place `AgentDef.allowed_tools` (an
`Optional[list]`) becomes `ToolContext.allowed_tools` (declared
`Optional[set[str]]`) is `set(agent.allowed_tools)`, in both of
`AgentManager`'s composers -- so a *hashable* non-string sits inert and an
*unhashable* one raises there. Both are now pinned, and the loud half is
the one no report named.

`allowed_tools: [read, on]` is the shape worth having a test for on its
own: YAML reads `on`, `yes` and `true` as booleans, so this is a typo
nobody has to type anything non-string-looking to make.

### S08-02: the sentence, not the crash

Four exception shapes reach `config_edit`'s readers and exactly one was a
`ValueError` -- the type the keystroke path already catches:

| edit | shape | caught by the panel's clause |
|---|---|---|
| a key deleted or renamed | `KeyError` | no |
| a table flattened to a scalar | `TypeError` from the SECOND subscript | no |
| the file no longer parses | ruamel `YAMLError` (derives from `Exception`) | no |
| the file is gone or locked | `OSError` | yes |

The `TypeError` is a third shape neither the report nor its probe listed;
it came out of re-driving. All three now become **one `ValueError` naming
the file and the remedy**, which is the shape
`config_schema.read_document` already chose for the same question one
module over, and the diagnosis in it is a conclusion rather than a guess:
`config.py` reads and validates this file at import, so the process could
not have started if it did not parse.

`get_leaf` went with it. It answered exactly the question the new
translating accessor answers, had no caller anywhere in the repository,
and was named by no document -- so leaving it would have made three
copies of one question where there had been two.

### S08-03: a bound on one axis is not a bound

The trust prompt is the informed-consent gate for content that becomes
instructions (D17). It has **four** sections; the finding named one of
them and cited a second as the bounded contrast. Measured:

| section | bound before | what it floods to |
|---|---|---|
| the file listing | none | 20,000 padding files = 20,001 lines, 600 KB |
| the hub (`AGENTS.md`) | §44's **20 lines**, and nothing else | 2 MB in 4 lines (one long line); 2 MB in 23 lines (twenty 100 KB lines) |
| `settings.json` + `mcp.json` | none | 2 x 2.2 MB = **4.5 MB in 30 lines** |
| the agent/skill catalog | A5's 300 chars **per entry** | 3,000 agents = 1 MB in 6,002 lines, every entry individually legal |

In the 4.5 MB case the `mcp.json` command the section exists to show sat
at **99.9994% of the way in**. And the section the finding held up as the
example to copy is bounded on one axis of the two that matter, which is
why copying it was not the fix.

**What the bound buys is not that a buried command becomes visible -- it
is that the burial does.** A project can still push its real command past
the cut. What it can no longer do is decide how long its own consent
screen is, and the marker it gets instead says *"0 more lines and
2,440,000 more characters of mcp.json are not shown here. Trust covers
the whole file, not the part printed."* -- which is itself an answer to
the consent question, and a better one than 4.5 MB of padding.

### What §72 deliberately does not change

- **`content_files()`**, which is unbounded and must stay so. The hash
  and `workspace_trust`'s invariant -- the loader may read no file the
  trust listing omits -- are about that function. What is bounded is what
  the prompt PRINTS of its answer, and the listing test asserts both
  halves so the two cannot be confused later.
- **`agents/manager.py`'s two `set()` calls**, left as they are. The
  loader gate closes every file-borne route; a belt there would be a
  second answer, and the only caller left is a programmatic one with no
  file to lose -- the asymmetry `max_steps` already settled, one field
  over, with its belt in `core/loop._run`.
- **`max_steps`' repair contract** (#45). It still repairs where the
  three new fields skip, and A55 says why the two are different rather
  than inconsistent.
- **`_config_rows`' narrow `except (ValueError, OSError)`**, which is now
  a narrower duplicate of a clause one layer out and stays anyway. Its
  docstring says what it is actually for: the four shapes it names are an
  unreadable FILE rather than a broken completer, and the panel should
  offer nothing without writing a traceback to `app.log` on every
  keystroke while someone edits in another window.
- **The two adjacent consent-text overstatements** S08-03 names and
  declines: that every project agent's description "enters EVERY system
  prompt" when `prompts/system_prompts.py` filters the catalog to
  `spawnable` agents, and that a harness- or user-shadowed entry is
  displayed as the project's. Both are S4 precision about the wording,
  separable from the paste, and both fail in the over-disclosure
  direction.

### Design Decisions Record — §72 (A54-A57)

| id | decision |
|---|---|
| **A54** | **Every section of the trust prompt is bounded on BOTH axes, by one helper, and the marker names what was withheld in both currencies.** `_bounded(source, prefix, what)` takes 60 lines and 6,000 characters, cuts WITHIN a line rather than dropping it whole -- a file flooded onto one line has exactly one line and it is the line the section is about -- and appends a sentence saying how much is unshown and that trust covers the whole file either way. **All four sections, not the two the finding names**, because capping some of them leaves the prompt exactly as floodable and the cap stops being worth having; `_HUB_PREVIEW_LINES` is replaced rather than joined, so §44's line-only cap cannot be the shape a later section copies. The numbers are MEASURED against the largest legitimate shapes rather than picked -- a five-server pretty-printed `mcp.json` is 54 lines and 923 characters, a realistic `settings.json` is 14 and 264, this project's own `AGENTS.md` is 4,033 characters in its first 60 lines -- because a cap that trims ordinary files turns its own marker into noise. The marker is not a courtesy: a cut nobody is told about turns a bounded prompt into one that misrepresents what it covers |
| **A55** | **`model`, `provider` and the elements of `allowed_tools` are typed at the loader, and a bad shape SKIPS the file.** The rule the function already states in a comment, applied to the three fields that sat under it unchecked. **Skip rather than repair to `None`, which is where this parts company with `max_steps` (#45) deliberately rather than by oversight**: `None` is a REAL value for `model` and `provider` and means "inherit the session's", so a repair would leave the agent available and running on a model nobody chose for it -- and an agent file names a model when being a DIFFERENT model is the point, of which §11's critic rationale ("a model checking its own output for errors shares that model's blind spots") is the sharpest case. A bad `max_steps` has no honest reading, so repairing it invents nothing; an unreadable `model` has one, and it is the wrong one. `allowed_tools` skips for `approval_overrides`' stated reason: half a whitelist is worse than no file at all for a field whose job is deciding what an agent may call. The consumers are NOT changed -- every one of them already assumes a string |
| **A56** | **A `config.yaml` read that cannot answer raises ONE `ValueError` naming the file and the remedy, and the shell prints that sentence.** Four shapes reached the readers and one was catchable; the three that were not are translated at the producer, in `document()` and in the one accessor `catalogue()` and `file_value()` now share. **The producer AND the shell, which is not the double containment the finding warns against**: the producer decides what there is to SAY about the file, which is §16's rule for this module, and the shell decides where to put it -- the split that keeps `config_edit` testable without a terminal. The diagnosis in the sentence is a conclusion and not a guess, since `config.py` validated this file at import. The generic *"/config failed. The error is in logs/app.log"* stays underneath as the backstop for everything that is not this. **A `pending_changes(rows)` parameter was written for this path and reverted in the same batch**, by B4 rather than by a test: the story justifying it was an unreadable file answering twice in one command, and that cannot happen, because the read that fails is the first one and it returns |
| **A57** | **The documented settable-scalar figure is pinned by a RELATION to the catalogue, sharing the sweep's own predicate.** A53's shape, applied to the figure that had already earned it: `docs/ARCHITECTURE.md`'s sentence says the sweep covers "all N settable scalars" **and in the same parenthesis records that the figure was written as 113 in batch 84 and left there through five batches that added thirty keys** -- and by batch 155 it had drifted again, to 143 against 145. A sentence that documents its own drift and then drifts is the argument for reading it rather than trusting it. The predicate is factored into one function the sweep and the gate both call, so the gate cannot pass against a figure that is right about a different set of rows |

---

## §71. A guard that compared two spellings of one id, and a cap whose two halves disagreed

**Built in batch 154**, from the review marathon's persistence surface: all
four `FIX-NOW` findings -- **S09-01** (S3), **S09-02** (S3), **S09-03** (S4)
and **S09-04** (S4). Four owner rulings, recorded as A50-A53. Register items
**110** and **111** are new.

Three of the four are the same mistake at three distances. A comparison
whose two sides were written by different people and never once met; a
setting whose two consumers were written by different people and never
agreed; and an enumeration that two later batches each grew past without
either one noticing. **IN ALL THREE THE CODE AND THE CLAIM WERE WRITTEN
SEPARATELY AND NOTHING EVER COMPARED THEM**, which is what the one new gate
in this batch is for.

### S09-01: the authoritative tier matched nothing, ever

Since T2 every research run records the exact thread ids of its passes in
`pass_threads_json`, and #82 made that record authoritative over the
time-window heuristic -- tier 1, "membership was recorded at run time;
there is nothing to infer". The two sides of that comparison are spelt
differently:

| side | written by | spelling |
|---|---|---|
| the record | `orchestrator._record_pass_thread`: `str(thread_id)` | `acb451c5-9c50-4236-a946-7f687d8dab0c` |
| the row | sqlmodel 0.0.39 / SQLAlchemy 2.0.51 over `SELECT id` | `acb451c59c504236a9467f687d8dab0c` |

`'a-b' in {'ab'}` is False for every id there has ever been, so **the tier
everybody trusts has never relabelled a single row**. Driven end to end
against the real modules on real SQLite -- `create_thread` for the stored
spelling, `_record_pass_thread` for the recorded one, the real
`classify_legacy_pass_threads` -- it relabelled **0**; with the id written
in the stored spelling instead, **1**.

**THE CONTROLS CUT THE FIX IN HALF.** It was written normalising both
sides, which reads as the symmetric thing to do; C1 removed the row-side
call and every test stayed green, because the recorded ids are now hex and
the column already was. So that half had no demonstrated consequence over
a spelling nothing in this project has ever written, and what ships is one
normalisation with the remaining assumption pinned in
`tests/test_storage_e2e.py` rather than branched on. The test that keeps
the symmetric version from coming back is
`test_a_thread_whose_own_id_is_not_a_uuid_is_left_alone`, which the tidier
code raises `ValueError` through.

Nothing observable breaks today, which is why this is S3 and not S2: every
pass thread is born `research_pass` at `core/loop.py:2024`, so no producer
creates the recorded-but-`chat` row tier 1 exists to catch. The cost is
that the first future source that records ids without setting kind at
birth -- and the compactor was exactly such a source under §27's own spec
-- returns the §27 picker flood through the tier nobody will think to
check.

**THE ELEVEN CLASSIFICATION TESTS COULD NOT SEE IT, AND THAT IS THE
FINDING UNDER THE FINDING.** All four tier-1 tests hand-wrote one string --
`"recorded"`, `"pass-zero"` -- on both sides of the comparison, so producer
format and storage format agreed by construction. Defect shape 7: a test
that supplies the model's move. The fixture now issues real UUIDs and
spells them the two ways the two sides spell them (`_stored(name)` is hex,
`_recorded(name)` is dashed), so those four tests are the guard they were
named for.

### S09-02: a cap of 0 turned the tool off and the announcement on

`config.yaml` says of `max_injected_memories`: "`0` disables the `remember`
tool rather than injecting nothing". The tool half honours that --
`available_check` is `MAX_INJECTED_MEMORIES > 0`. The injection half did
not: `rows[:0]` is empty, `total (2) > cap (0)` fires the truncation
branch, and the loop over `shown` appends nothing, so every turn carried

```
## Remembered

Showing the 0 most recent of 2. Older ones exist and are not listed here.
```

with a per-turn `WARNING` behind it telling the user to raise a value they
had set on purpose. **The model was told that remembered content exists and
is being withheld, and the tool that could have earned it was not on the
menu.** Measured through the real `prompt_fragment` with two visible rows,
alongside the three controls the fix must not disturb (cap 0 with no rows,
cap 1 of 2 which still truncates and still warns, cap 2 of 2 which does
neither).

### S09-03: a comment naming a failure that cannot happen

`list_threads`' `getattr` carried "a row read back from a database whose
ALTER has not run yet has no attribute at all". It cannot: the mapped class
always has the attribute, and a database missing the column raises
`OperationalError` at the `SELECT`. What the fallback absorbs is a NULL on
a row written before the column existed -- which is what `get_thread`'s
docstring has said **since batch 75**, where commit `47257f3` ("comments
that named the wrong mechanism") deleted this exact sentence from
`get_thread` and `child_threads` and missed the third copy.

### S09-04: the report named three sentences and the claim was in thirteen

`load_pipeline_run` returns **11** keys and the record carries **6** data
columns. T2 added `pass_threads_json` and E13 added `candidates_json`, and
neither batch found anything written about the thing it had grown. Thirteen
live copies across five files: three in `pipeline_storage.py`'s own
docstrings, five claims in four `ARCHITECTURE.md` lines, three in
`test_pipeline_storage.py`'s prose and two `BREAKING_CHANGES.md` rows. The
past-tense accounts in `DEVLOG.md` (three) and `ROADMAP.md` (one) are the
record of what the function was when they were written and are untouched.

**ONE OF THE TWO `BREAKING_CHANGES` ROWS PROMISED A SYMPTOM THE SUITE DOES
NOT PRODUCE.** It said that adding or removing a return field makes
`test_load_pipeline_run_round_trips_all_fields` "fail on a missing/extra
key", and that the contract "is documented in `pipeline_storage.py`'s
docstring". The test asserted nine keys one at a time and never the set, so
a twelfth field was green and a dropped `pass_threads` was green; no
docstring enumerated anything. A row describing a guard that does not exist
is worse than no row: it is the sentence a later reader checks instead of
the code.

### What §71 deliberately does not change

- **The producer keeps writing `str(thread_id)`** (A50). The dashed form is
  what `output_writer.py` ships as `pass_threads.json` and what an operator
  reads; normalising at the comparison is one place, normalising at the
  producer is every artifact ever written.
- **No backfill, no second index.** `ensure_columns` stays additive-only
  (M7) and the classification stays a separate function; §47's deliberate
  absence of a thread-level pass index stands.
- **`0` keeps meaning "off" rather than "unlimited"** (A51). The config
  comment already said so and both halves now agree with it; what a
  negative would mean is unreachable, because `config_schema` types the key
  `NonNegativeInt`.
- **No gate over the field enumeration in the past-tense accounts** (A53).
  DEVLOG and ROADMAP say "returns 9 fields" about a function that did, and
  a gate that read those would have to be taught to tell a record from a
  contract -- the allowlist shape batch 153 refused for the same reason.
- **`list_threads` keeps the `getattr`.** Only the stated reason was wrong;
  NULL -> `"chat"` is the right answer and `get_thread` and `child_threads`
  do the same thing (item 110).

### Design Decisions Record — §71 (A50-A53)

| id | decision |
|---|---|
| **A50** | **Tier 1 compares UUIDs, not strings; it normalises ONE side and PINS the other; and a populated record keeps its run in tier 1 whatever its ids parse as.** `_as_uuid_key` renders every RECORDED id as `UUID(...).hex`, which is the spelling the column already holds, and answers `None` rather than itself for a value that does not parse -- a thread id IS a UUID, and deciding that two unreadable strings denote one thread is the direction that hides a real conversation. **The symmetric version was written first and control C1 refused it**: parsing the row ids too left every test green, so the branch had no demonstrated consequence (B4) over a spelling no build of this project has written -- `sqlmodel==0.0.39` since the pin was introduced. What is left is an assumption about a library rather than about this code, so it is ASSERTED where a real database exists, by `test_a_thread_id_is_stored_in_a_spelling_str_does_not_produce`, which fails naming this function. A pinned dependency beats a branch for a case nobody can reach. The two halves of that rule pull against each other and both are pinned: an unreadable recorded id costs its own entry's membership and **does not** drop its run to the window tier, because a populated `pass_threads_json` is what "post-T2" means -- consulting a modern run's clock is how a concurrent chat thread gets hidden, which is #82's headline and the unrecoverable direction. The fix is at the comparison and not at the producer: `str(thread_id)` is also the artifact and the operator's copy |
| **A51** | **A cap of 0 is OFF in both halves, and off is silent.** `remember`'s `available_check` already hid the tool; the injection half now returns `""` beside the existing `wants_memories` and empty-rows early returns, so there is no header announcing nothing and no WARNING about a state the user configured. The two halves are asserted TOGETHER in one test rather than separately, because the defect was precisely that they disagreed while each was individually defensible |
| **A52** | **A `BREAKING_CHANGES` row names a symptom the suite actually produces; where it does not, the TEST moves.** The row claiming a key-set failure has been there since §5 and the assertion never existed, so the fix is the assertion -- the same sentence the candidates block three lines above it already makes, that asserting the exact shape is what turns a silent change into a red test -- not a softened row. A row that describes a guard which does not exist is read instead of the code, and it is read by exactly the person who was about to change the thing it guards |
| **A53** | **The documented record shape is pinned by a RELATION to the function, over the sentences that have one.** `test_the_documented_record_shape_matches_what_load_returns` reads the count and the field names out of ARCHITECTURE's own prose and compares them to the keys `load_pipeline_run` returns, so a twelfth column cannot land without the sentence moving with it, and the gate cannot be satisfied by editing it to agree with itself. Over §4.14's enumeration and §9's bare count, and no further: the past-tense accounts are the record of what the function was, and a gate that had to tell those apart would need the historical allowlist that would admit the defect |

## §70. A trust grant that could not persist, and a file three documents kept promising about

**Built in batch 153**, from the review marathon's scaffolding-and-prompts
surface: both `FIX-NOW` findings -- **S17-02** (S3) and **S17-05** (S4). Six
owner rulings, recorded as A46-A49 plus two scope decisions below. Register
items **107**, **108** and **109** are new.

The two findings are the same sentence from opposite sides of one command.
**`/init` IS THE ONE PLACE THE HARNESS WRITES INTO SOMEBODY'S REPOSITORY,
AND BOTH HALVES OF WHAT IT TOLD THEM AFTERWARDS COULD BE WRONG** -- S17-02
because the report could be replaced by a traceback, S17-05 because the
document describing what the command authors named a file that had been
deleted thirty-odd batches earlier.

### S17-02: a settle on both paths, outside every handler on both of them

Audit #95 found that a partial `/init` left a project the user had trusted
a moment ago untrusted -- the write moves the D17 content hash, so
`is_trusted()` answers False for content they had just authored -- and put
the trust settle on the success path AND the failure path, with the
`was_trusted` capture taken before the write. That fix is intact. What it
did not notice is that **the settle itself can raise, and the call sits
outside the `try` on both paths.**

`grant_trust` raises `OSError` from either half: `_save_trust_store` writes
the store through `json_store`, and `_content_hash` re-reads every file the
grant covers. So a full disk, a read-only HOME, or one file an editor holds
open took a bare `OSError` out of `generate()`.

Driven through the real function, with the failure injected at the `open`
boundary -- the shape `tests/test_project_init.py`'s own `_fail_write_of`
already uses, so the whole chain runs:

| arrangement | before | after |
|---|---|---|
| success path, store unwritable | raw `OSError`; **both config files on disk, the notice gone, `is_trusted` True -> False** | the notice, warning first, exit 0 |
| write fails AND the store | raw `OSError`; **the whole report discarded, "wrote 1 file before that" with it** | `InitError` carrying the failure, the file list and the trust warning |
| control: write fails alone | `InitError`, full report | unchanged |
| a content file unreadable | raw `PermissionError` **before anything is written** | proceeds as untrusted, says so |

On the CLI the traceback was certain: `run_init_command` catches `InitError`
only, measured by driving it. The TUI's worker catches `Exception` as well,
so there the same defect degraded to `/init failed: [Errno 28] ...` -- an
error line, with the written list and the trust note lost just the same.

**THE REPORT'S EVIDENCE RECIPE NO LONGER REPRODUCES, AND THE DEFECT DOES.**
S17-02 poisoned the save by planting a directory at `<store>.tmp`. Batch 144
gave `_replace_through_temp` a unique `<path>.<hex>.tmp` name -- because
"two writers raced on the one name, and a failed `os.replace` left the
leftover in the install tree forever" -- so that poison is now ignored and
the arrangement passes. The mechanism the finding describes (full disk,
read-only mount) is untouched. An expired recipe is not a fixed defect, and
a batch that had only re-run the report's steps would have closed this one
as stale.

**THE PRE-WRITE HALF HAD ITS OWN ANSWER THREE FUNCTIONS UP.**
`_read_existing_context` catches `OSError` on the project hub, degrades,
logs a warning, and its comment says "Degrade like config_loader does with
an unreadable hub document". `is_trusted()`'s read of that same file, nine
lines later in the same function's flow, killed the command. One function
answering one error two ways is what A47 closes.

### S17-05: one relocation, fifteen live copies of the claim it falsified

AGENTS.md's I10 reads *"Stubs are templates; only `CONTEXT.md` is
generated"*. Section 44 moved the hub to the root `AGENTS.md` and removed
`CONTEXT.md` outright, so that sentence is false twice over: the file does
not exist, and the generated one is the hub.

**The report names one sentence. Measured, the same claim was standing in
fifteen places across eight files, and eleven of them are in the suite** --
which is where a reader goes to CHECK a claim, so the stale copy is the one
they find first. Two of the three AGENTS.md instances are unnamed by the
report and one of them has teeth:

- **I6 promises that `CONTEXT.md` "is never special-cased out of the
  hash"** -- a guarantee about a file that is not in the hash because it
  does not exist, which leaves the guarantee that matters (WS9 put the root
  `AGENTS.md` in the listing precisely so a cloned hub cannot reach a
  system prompt unasked) unstated. A reader checking the promise finds
  nothing to check.
- The durable-memory section still places the third context tier "beside
  `CONTEXT.md`".
- `tests/BREAKING_CHANGES.md` carried four live claims, including the "why"
  column of the grant row for `write_project_doc` -- *"including
  `.venastine/CONTEXT.md`, which is injected into every opted-in agent"*,
  wrong in the path and the name -- and a fifth naming
  `project_docs.CONTEXT_FILENAME`, a symbol that resolves nowhere. That
  last one is batch 152's `_ASK_HANDLERS` shape, one document over and
  PUBLIC rather than private, so that batch's gate cannot see it (item
  109).
- `tests/test_prompt_tier_boundary.py`, whose whole subject is which tier
  leaks into a research pass, named the leaking tier by its removed
  filename six times.
- Three test NAMES: `test_context_md_still_counts_towards_the_hash` tests
  `AGENTS.md` and says so in its docstring, and two in
  `tests/test_config_loader.py` do the same.

Fixed by **naming the tier or the role, not the filename** (A49), so the
next relocation cannot make these sentences wrong again -- which is the
whole defect shape. The past-tense accounts are left exactly as they are:
`docs/DEVLOG.md` (14 mentions) and `docs/ROADMAP_v2.md` (25) are the
append-only record of what happened when the file existed, and AGENTS.md's
own "`CONTEXT.md` is gone outright" is the sentence that says so.

### What §70 deliberately does not change

- **`is_trusted()` keeps raising** (A48, item 107). Its other three
  production callers are contained -- `main.load_project_config` catches
  `(ValueError, OSError)` and exits 1 with one clean line -- and making a
  security control answer "not trusted" where it currently refuses to start
  is a change to D17's posture, not to this command's.
- **No retry, no rollback, no trust-model change.** #95's U7 stands:
  nothing is rolled back, because the hub's previous content is gone by the
  first step, and deleting documents out of someone's project on an error
  path is a worse failure than the one being reported.
- **The `except` set inside the write `try` is not widened.** S17-02 says
  it is audited and correct, and re-reading it agrees: `OSError` from
  `makedirs`, `InitError` from `_write`/`_write_config`, `ToolCallDenied`
  as a dead backstop.
- **`generate()`'s other pre-write calls stay outside the `try`** (item
  108). `manifest.is_blank`, `propose_kind` and `_config_plan` all touch
  the filesystem before it, and an `OSError` from any of them is still a
  traceback on the CLI. That is one handler's worth of work and a different
  claim from this finding's.
- **No gate over the word `CONTEXT.md`** (A49). The token is legitimate in
  the past-tense accounts, so any gate over it needs a historical
  allowlist -- and that allowlist would admit the three defects it was
  built to catch.

### Design Decisions Record — §70 (A46-A49)

| id | decision |
|---|---|
| **A46** | **A trust re-grant that could not persist is REPORTED, not raised, and the warning leads the receipt.** The files are on disk, so the command did what it was asked; what the user cannot see is that the next launch will ask about this project again, so that line goes first and the exit code stays 0. `json_store` argues that "a trust grant that silently did not persist is worse than a crash" -- and that argument is about SILENCE, which this ends rather than softens. A settled run keeps the note LAST, as the footnote it has always been, and both orderings are asserted, so a fix that moved every trust line to the top could not pass. On the failure path the warning stays where the trust note already was, because the first line there is the write failure and that is the primary thing |
| **A47** | **The pre-write trust capture degrades to UNTRUSTED and says so, and `_settle_trust` is total.** `is_trusted()` hashes every file the grant covers, so the capture is a read of somebody else's files and can fail; it sat before the `try`, and one unreadable file killed `/init` before it wrote anything. `_read_existing_context` three functions up catches `OSError` on the same file for the same reason, so this is one function stopping giving two answers to one error. **`False` rather than `True` for an unanswerable question**, which is `core/workspace_trust.py`'s own posture (`_load_trust_store` returns `{}` for anything unreadable and says it fails CLOSED in both senses): reading it as trusted would launder a cloned `.venastine/` in through a door opened by one locked file. No re-grant is attempted, and the reason is reported rather than left to be inferred from a trust prompt on the next launch |
| **A48** | **The fix is at `/init`'s call sites, not inside the trust control.** `is_trusted()` raising is contained everywhere else it is called, and the loudest containment is the right one at startup: refusing to start names a configuration problem, where a fail-closed read would start degraded and say only "not trusted", which is also what an ordinary untrusted project says. Changing that is a D17 posture decision with its own blast radius, and it is filed with the measurement rather than taken on the way past |
| **A49** | **A document names the TIER OR ROLE, not the filename, and the gate goes on the one sentence with a code relation.** "The project hub" is true before and after section 44, so the sentence cannot be falsified by the next relocation -- which is what made fifteen copies of one claim possible. Where the filename is the point, the current one is named. The gate is `test_the_file_agents_md_promises_is_inside_the_trust_hash`: it reads the filename out of I6's promise with a pattern that requires the name to be ADJACENT to the phrase, and checks it against `workspace_trust.content_files()`, which IS the hash's input set by construction. Adjacency is not fussiness -- batch 152 lost a gate twice to a scan that could not tell code from the prose written about it, and the paragraph this one reads from now names the old filename while explaining that it is gone. **No gate over the word itself**: it is legitimate in the past-tense accounts, and the allowlist that would permit those would permit the defect |

## §69. A consent prompt the model could repaint, and a refusal that read as a yes

**Built in batch 152**, from the review marathon's headless-shell surface:
all three `FIX-NOW` findings -- S16-01, S16-02 and S16-03 -- across
`main.py`, `core/ansi.py`, `AGENTS.md`, `tests/test_cli.py`,
`tests/test_interactive_sessions.py` and
`tests/test_docs_consistency.py`.

Two S3s and one S4, and both S3s are the same sentence from opposite ends:
**the CLI is where a human authorises a tool call, and it trusted the text
it was authorising.** S16-02 is the display -- model-supplied escapes
printed raw onto the line the user decides from. S16-01 is the answer --
a line the parser could not read returned the value that means "yes,
grant nothing", which `core/loop.py` spawns on. Neither needed a
privileged position to exploit: a tool call and a mistyped word.

### S16-02: the subject line the model could erase

`_approval` printed the headline, the notice and the agent's stated reason
as plain f-strings. The params block below them was always safe --
`_render_params` runs `json.dumps`, which emits `\u001b` -- which is what
made the raw half easy to miss: the escape IS visible on the screen, in
the block nobody decides from.

Measured through the real channel before the fix, with a headline of
`cat README.md` + CSI `2K` + CR + a second `$ cat
.venastine/settings.json`:

| field | controls reaching stdout |
|---|---|
| approval headline | ESC |
| approval notice | ESC, CR |
| approval rationale | ESC, CR |
| approval `tool_name` | ESC |
| the asker line (§47) | ESC |
| question text, options | ESC, CR |
| sign-off agent, candidates | ESC, CR |
| confirm title, body | ESC, CR |
| choice proposal, reason, options | ESC x3, CR x3 |
| secret title, body | ESC x2, CR x2 |
| review kind, severity, reason, proposed | ESC x4, CR x4 |
| params block | none -- `\u001b`, as text |

**The report's mechanism is right about the outcome and wrong in one
detail, and the detail is a partial defence worth keeping.**
`str.splitlines()` splits on a carriage return as well as a newline, so a
CR inside a HEADLINE never reached the terminal: it became a second line,
and `main.py` prefixes every line with `  $ `, so the forged command
arrived wearing a doubled prefix. What did the damage was the escape
alone -- CSI `2K` erases the row the real command was drawn on. The
fields printed as single f-strings are the worse case, because there the
CR survives and overwrites the row the user is reading, label and all.

**A second privilege-critical site, which the report does not name.**
`_pick_tools_to_grant` prints an MCP server's own description of its tool
and then asks one y/N per tool. Its own docstring says why that is the
whole of the decision -- "MCP exposes no read-only/write metadata, so
nothing can tell the user that one granted tool searches a library and
another posts to a channel" -- and `candidates()` says the population it
exists for IS MCP tools. Measured: `_bounded` at registration collapses
whitespace, so a newline and a CR never arrive, and **the escape does**.
A grant is wider than an approval: it covers every call for the run.

The fix is at the display boundary, which is where the owner's ruling put
it: every untrusted string this shell prints, not only the ones above a
question. `core/ansi.py` already owns the transform for the pty and the
TUI transcript, so the one-shot form lives there and is built out of
`AnsiStripper` -- one scanner, one answer about what a sequence is.
Twenty-odd call sites, one `_Visible` per prompt, and one line above the
question when something was removed.

**The relation found a site reading the function did not.** The sweep over
`_PAYLOADS` failed on `choice` the first time it ran: `_choice` renders
the option list TWICE, and the second rendering -- the `[... is not one
of: ...]` line on the unmatched path -- is not the kind of line a reader
thinks of as display. That is the argument for a relation over the kind
table rather than a list of print sites, because the thing being guarded
against is a print call nobody remembered.

### S16-01: an empty set already meant something

`_signoff`'s per-piece loop ignores anything that is not a candidate
number, prints what it ignored, and falls out with whatever it collected.
For a line in which NOTHING was a number, that is the empty set -- and the
empty set is already taken. It is the explicit `none`, which returns two
branches earlier, and `core/loop.py:440` spawns on `answer is not None`.

Measured through the real channel and the real `decode`:

| typed | `_signoff` returned | the loop | after |
|---|---|---|---|
| (blank) | `None` | refused | unchanged |
| (no answer) | `None` | refused | unchanged |
| `none` | `set()` | spawns, grants nothing | unchanged |
| `all` | both candidates | spawns | unchanged |
| `1` | one candidate | spawns | unchanged |
| `xyz` | **`set()`** | **SPAWNS** | `None`, refused |
| `0 99` | **`set()`** | **SPAWNS** | `None`, refused |
| `1 xyz` | one candidate | spawns | unchanged |

The two shipped rows are a mistyped refusal becoming an authorisation to
run. `0 99` is the one a user reaches by miscounting rather than
mistyping: both pieces are digits, both out of range, so they are dropped
by the bounds check rather than the digit check and arrive at the same
empty set.

The function's own docstring already said what it should do -- "None for
anything unparsed is belt-and-braces" -- and the code did that for a
blank line and not for a line it could not read. The three neighbouring
cells were pinned by tests; the hole between two green cells stayed green
because no test typed a non-blank line that named nothing.

### S16-03: a name that was never there

AGENTS.md section 29 said every kind in `interaction.SAFE_DEFAULTS` must
have a handler in `_ASK_HANDLERS`. There is no such symbol: the CLI's
table is a closure-local `handlers` published for its test as
`channel.rendered_kinds`, and the TUI's is an if-chain in
`VenastineApp._ask_blocking`. `git log -S` returns only the docs commit
that coined the name, so it was **born stale** rather than renamed
without update -- the shape no convention catches, because there was never
a moment when somebody edited code and forgot the document.

The enforceable invariant exists and is green. What did not resolve was
the name it was filed under, and the cost is a fixer who "adds the missing
handler to `_ASK_HANDLERS`" building a third registry beside the two real
ones.

**The gate's resolution rule was chosen by measuring three candidates**
against AGENTS.md's 131 backquoted private identifiers:

| rule | fails on | why |
|---|---|---|
| substring in any `.py` | **1** | the defect, and nothing else |
| this file's existing AST index | 17 | collects `ast.Name` Store targets, not `ast.Attribute`, so every instance attribute the document names reads as undefined, plus four dunders |
| `tokenize` NAME tokens | 8 | correct about prose, but three are not this repository's symbols at all -- `_check_bindings` and `_watch_theme` are Textual's, `__builtins__` is CPython's -- and one is prose about a design the code deliberately did not take |

**And the first form of the gate was defeated by its own prose.** The
control that writes `_ASK_HANDLERS` back into AGENTS.md left it GREEN,
because the gate's comment block explains the finding by name and a
substring scan cannot tell code from the paragraph written to forbid it.
That is batch 151's conftest-header defect one batch later, in the same
session, found the same way -- by the control rather than by reading. The
gate excludes its own file now. It then caught its author a second time:
the replacement sentence in AGENTS.md closed by naming the symbol while
explaining that it does not exist, and the rule is that the document does
not name it at all.

### What §69 deliberately does not change

- **No classifier change.** ESC is not in `_SHELL_METACHARACTERS` and is
  not being added. Rejecting a command for carrying an escape would move
  the problem, not fix the display of every other untrusted field -- and
  `headline_for` returning the command verbatim stays correct, because
  what must be safe is the display and not the value.
- **No registry change**, for the same reason one level down.
- **No TUI change.** Rich `Text` does not interpret ANSI, and
  `tui/widgets.py` has run its three transcript ingresses through
  `AnsiStripper` since batch 145. S15 owns that claim; this batch is the
  shell that never called it.
- **No re-prompt loop** for the sign-off. One question, one answer, fail
  closed. A re-prompt would be a second interaction model in the one
  place §23 says there is exactly one.
- **No bidi or zero-width filtering** (TECHNICAL_DEBT 104). U+202E can
  make a terminal draw text in an order the bytes do not have, which is
  the same class of problem -- but a bidi mark is legitimate content in
  Arabic and Hebrew text, which a control character is never, so the
  answer is not the same answer.
- **The `choice` kind still ANSWERS with the option as it was offered.**
  Only the labels are scrubbed: `decode` validates a choice against the
  list it was given, and a scrubbed answer would fail that check for the
  one reason that is not the user's fault.

### Design Decisions Record — §69 (A40-A45)

| id | decision |
|---|---|
| **A40** | **Untrusted text is made safe at the DISPLAY boundary, and the one-shot filter lives beside the streaming one.** `core/ansi.py` already owned this transform for the pty (SS27) and, since batch 145, for the TUI transcript; a second stripper in `main.py` would be the two-copies-of-one-decision shape that module's own docstring refuses, so `for_display` / `for_display_counted` are built out of `AnsiStripper` rather than beside it -- one scanner, one answer about what a sequence is, both encodings (`ESC [` and U+009B) for free. **Not in the classifier**: ESC is not a shell metacharacter and adding it would move the problem, since the other untrusted fields are not commands. **Not in the registry**: `headline_for` returning the model's command verbatim is correct, and what has to be safe is the display, not the value. `_render_params` stays as the worked example of a path that was always right, and it got there by RENDERING the value instead of trusting it |
| **A41** | **A removed control is said out loud where something is being decided, and silently where nothing is.** One line above the question, naming the count, because a tool call whose headline tries to move the cursor is evidence about the decision being asked -- nothing a tool legitimately needs to do involves redrawing a consent prompt. On a replayed thread, a live answer or a research pass's progress lines the strip is silent: there is no decision for the warning to inform, and a line per stripped delta is the noise that gets a filter turned off. The count is per PROMPT and not per field, because the user is told once that this call tried to repaint, which is the same reason `tui/widgets.py` keeps one count and lets `tui/app.py` read it. Folding CRLF to LF is NOT counted -- it is a normalisation, and counting it would raise the warning on ordinary text that arrived with Windows line endings |
| **A42** | **Every untrusted string this shell prints goes through the filter, not only the ones above a question.** The report's own framing is that the approval headline is "the privilege-critical instance of one shell-wide habit", and fixing one site of a habit is how the next copy gets written. So: the seven kinds, the review prompt, the grant picker, the MCP acknowledgement, replayed threads, live answers and notices, research-pass events, `--memories` rows, server unavailability lines, and the message of any exception that can carry model or tool text. Untrusted means ORIGINATING OUTSIDE this repository's source -- model text, a stored thread's rows, a tool's or server's own strings, a project config's text. The harness's own labels, counts, paths and prompts are not filtered, because filtering them would say the filter is about tidiness |
| **A43** | **A line that names nothing is not the empty set, and the empty set already means something.** `_signoff` returns `None` when no piece of a non-blank line named a candidate, because `set()` is the explicit `none` -- a legitimate answer a user can give -- and the loop spawns on "not None". Measured: `xyz` and `0 99` both spawned. **A mixed line still grants what it named**: `1 xyz` grants candidate 1 and reports the piece it ignored, because refusing over the second half would discard an answer the user did give. **Not a re-prompt loop**: one question, one answer, fail closed, which is §23's shape and `interaction.decode`'s own None-versus-empty contract one layer up. The refusal says WHICH refusal it was, because a user who typed a tool name instead of its number has to be able to tell an answer from a bug |
| **A44** | **A document names what exists, and that is gated over every backquoted private identifier rather than over the sentence that was wrong.** A leading underscore is an unambiguous signal that a backquoted token is code and not English, so nothing has to guess; 131 of them were measured, 130 resolved. The resolution rule is a substring match over every `.py` including tests, chosen by measuring the two stronger candidates and finding that both fail on correct references -- the AST index on 17 (instance attributes), a `tokenize` NAME index on 8, three of which are Textual's or CPython's symbols. **The gate excludes its own file**, because its comment block explains the defect by name and a gate's evidence may not come from its own explanation of what it forbids -- measured: the control that reintroduced the name left the gate green. The domain is asserted in both directions, which is section 11's instruction |
| **A45** | **At a display boundary a lone carriage return is removed; on the pty path it is kept, and the two contracts disagree on purpose.** `core/ansi.py` leaves one in a pty's text because making `working CR done` read as `done` is emulation, and guessing is worse than showing what arrived. That argument inverts where the text is handed to a terminal: a carriage return is not ambiguous there, it returns the cursor to column 0 and the next text overwrites the line being read. The other controls below U+0020 go with it, and DEL, because backspace, vertical tab and form feed move a cursor too; a newline and a tab stay, as the only two a caller means. The unterminated carry takes the transcript's answer (`release_unterminated=False`) for the transcript's reason. Both halves are asserted in ONE test, so a later reader cannot tidy them into agreement |

## §68. Six places the suite described itself, and two guards that could pass while scanning nothing

**Built in batch 151**, from the review marathon's test-infrastructure
surface: all six `FIX-NOW` findings -- S19-01, S19-02, S19-03, S19-04, S19-05
and S19-13 -- across `pytest.ini`, `conftest.py`,
`tests/test_docs_consistency.py`, `tests/test_pilot_wait.py`,
`tests/test_wsl_backend.py`, `tests/test_tui.py`,
`tests/test_storage_e2e.py`, `.github/workflows/compat.yml` and
`docs/TECHNICAL_DEBT.md`. Register items **85** and **97** ride with it,
because both name "whichever batch next touches those files" as their home.

Two S3s and four S4s, and what they share is that **the subject is this
suite itself**. Every other surface in this marathon had a production
defect at the bottom of it; here the production code is fine and what is
wrong is what the suite says about its own machinery -- a worker policy, a
grouping contract, an offline promise, a stub inventory -- plus two guards
that can pass while scanning nothing.

That makes one shape dominant, and it is the one the marathon's own notes
call **shape 9, a claim made false by a change elsewhere**. Six of the eight
things fixed here are that, which is why the batch is mostly subtraction: a
count nobody could keep true is deleted and the rule it decorated is gated
instead.

**The reports were wrong about three of their own rows**, and that is worth
stating first because it changed the work. S19-02 claims five copies of
`tests/test_tui.py`'s class count. Measured: `AGENTS.md` says **71**, gated
and correct; `.github/workflows/tests.yml` and `tests/test_tui.py` say the
183 CLASSLESS tests were gathered into **41** classes, which is a statement
about what batch 114 did and is still true; `docs/TECHNICAL_DEBT.md`'s **62**
is a measurement record of a run ("all 62 classes were run standing
completely alone -- 62 processes, 251s"), true when it was made. One live
stale digit, not five. The finding's sub-claim that "where it was 21"
contradicts `AGENTS.md`'s 41 is also wrong: 21 pre-existing classes plus the
41 the classless tests became is the 62 the same sentence states, so
"correcting" 21 to 41 would have introduced an error into a sentence that
was internally consistent.

### S19-01 -- a count four sentences carried and nobody could keep (A34)

Four places said `real_storage` was the suite's only non-function-scoped
fixture. It stopped being true in **batch 110**, when
`_schema_cache_is_put_back` arrived in the same file. The register's copy is
the sharpest of the four because **it cites the grep that refutes it in the
same sentence** -- `grep -rn 'scope="module"' tests/` returns two.

Batch 128's whole subject was this claim. It fixed two copies and missed
four, and `96de26e`'s commit message records why in a line that predicted
this one: *"The duplicates were found by grepping the sentence rather than
the claim, so two files that phrase it differently survived."* The four
phrasings are "module-scoped" (twice), "such" and "non-function-scoped".

The rule the count decorated is correct, general, and is what a reader
actually needs: **a module- or session-scoped fixture's file must hold no
test class**, because `--dist loadscope` makes a class its own group and a
second group on one worker asks for a second setup --
`Table 'conversationthread' is already defined`. `AGENTS.md` states it, and
says this about itself in the next breath: *"a count written when it was
true, beside a rule that did not need it."*

So the count goes at all four sites and the rule gets a gate.
`test_this_file_declares_no_test_class` pins it for one file and, in the
finding's words, "cannot pin it for a file nobody has written yet"; the new
`test_every_file_with_a_module_scoped_fixture_declares_no_test_class` reads
the DECORATOR rather than grepping `scope="module"`, so
`@pytest.fixture(autouse=True, scope="session")` is found and a string in a
docstring is not.

**A second defect in the same register entry, found while confirming the
first.** Item 30 is closed, its header says batch 114 closed it, and its
body ended with *"What remains open in this item: the 183 classless tests in
`test_tui.py`, and nothing else"* -- which is the thing batch 114 did, as
the RESOLVED section eight lines below states. A closed item named as its
one piece of open work the thing it was closed for.

### S19-02 -- the one live stale digit, and a premise dead for 37 batches (A35)

`pytest.ini`'s addopts comment is the specification for the suite's grouping
contract, and `AGENTS.md` calls `--dist loadscope` *"the one thing here not
to 'optimise'"*. Its worked example said `tests/test_tui.py` "is 62 classes
and so 62 groups"; it is 71. A specification whose worked example is
numerically wrong invites the reader to re-derive the example.

**Option 2 of the finding's two shapes**, which is what the finding's own
doctrine asks for: stop stating the number anywhere but the one place a gate
reads it. The argument that sentence makes needs "the classless group was
the floor and is not any more"; it never needed the integer. The gated copy
is `AGENTS.md`'s, which
`test_agents_md_states_the_real_test_tui_class_count` reads back out of the
sentence and compares against `ast`.

**The second-order instance is the one that could have changed a decision.**
`.github/workflows/compat.yml` chose the parallel default for a stated
reason -- *"the suite is floor-bound on test_tui.py, so asking for the other
30% of the cores is never faster"* -- and `tests.yml` says of that same
sentence, in capitals, `THE FLOOR THAT USED TO BIND IS GONE, AND THIS RUNNER
NEVER FELT IT (batch 114...)`. So compat.yml reached the right conclusion
from a premise retired 37 batches earlier, which is worse than being wrong:
someone who re-measured the floor, found it gone and changed that line would
have been reasoning correctly. The conclusion is now cited to the
measurement that still supports it -- `-n logical` was not faster under
either distribution mode, alternated three runs a side.

### S19-03 -- the file written to kill shape 1, carrying a copy (A36)

`_was_narrowed` decides whether this invocation collected the whole suite,
and it answered by comparing `config.option.markexpr` against the string
literal `"not integration"` -- a copy of what `pytest.ini`'s addopts supply.
`tests/test_docs_consistency.py`'s own header states the rule this breaks:
*"Every check here compares a document against a live value ... never against
another hand-written number."*

Add a marker to addopts -- a two-line change, because `--strict-markers` --
and every full run starts reading as narrowed.
`test_the_documented_count_is_the_real_one`,
`test_the_tree_states_a_correct_count_for_every_collected_test_file` and
`test_agents_md_states_the_real_modal_parametrisation` stop asserting, and
`test_the_subprocess_collection_agrees_with_the_session`, which has no
file-alone fallback, stops running anywhere at all. **Silently**, which is
the direction the guard's own docstring says it most needs to avoid.

**And the guard on the guard could not see it by construction**, which is
the half worth fixing properly: `_FakeOption.__init__` carried the same
literal as its default, so the test asserted that two copies of one string
agreed with each other. Three copies of one expression, and the only one
that decided anything at runtime was `pytest.ini`'s.

`config.getini("addopts")` was measured before being relied on: it returns
the shlex-split list `['-ra', '--strict-markers', '-m', 'not integration',
'-n', 'auto', '--dist', 'loadscope']`, so the parse is "find `-m`, take the
next element", plus the squashed `-mEXPR` spelling.

### S19-04 -- a guard that passed from one directory up (A37)

The guard forbidding a sixth copy of `settle` globbed
`pathlib.Path("tests")`. pytest does not change the working directory and
`rootdir` is resolved from the arguments, so an invocation launched from
anywhere else globbed nothing and the test passed having read zero files.
Measured both ways: 124 files from the repository root, 0 from one directory
up, and the finding drove it through pytest itself.

With no assertion on the domain, **"found nothing" and "looked nowhere" are
the same observation** -- which is `docs/ARCHITECTURE.md` section 11's FIRST
entry, named there as a measured survivor rather than a hypothetical, with
its instruction attached: *"pin the input in both directions."*

What is being guarded is not cosmetic. Five copies of that helper existed
with *"budgets 40/60/120/120/200 and two incompatible orderings"*, two
independent bugs lived in them, and both were *"invisible to 1246 passing
tests"*. The guard exists because *"a convention is what produced five
copies"* -- and the guard was a convention about the working directory.

### S19-05 -- the file that owns the decision, describing the other branch (A38)

Two claims in the root conftest's own prose.

`pytest_xdist_auto_num_workers`' four-dial table is the reference for this
project's worker policy, and it annotated `-n logical` as *"(what CI runs)"*.
CI runs the default. `tests.yml` says so under a comment carrying three runs
of paired measurements and concluding *"So CI runs exactly what a
contributor runs"*; `compat.yml`, `pytest.ini` and `AGENTS.md` say so too.
So the one file that OWNS the `-n auto` decision was the one file describing
CI as taking the other branch, in the line a reader opens to learn what the
dials mean -- and `AGENTS.md` names that conclusion as something *"not to
re-derive from the above"*, which is exactly what a reader would do from
there.

The second is the stub inventory: the header lists seven fakes and
`_install_fake_sdks` installs `markitdown` too. It matters because of what
follows it -- a `NOT faked, by explicit decision` list -- which makes the
first read as the complete set rather than as an example. A reader asking
"is `markitdown` stubbed, or do I need it installed?" got "no" from the
header and "yes" from line 713, and `docs/ARCHITECTURE.md` records the cost:
*"A green local suite is not a green fresh install."*

**The finding says neither is gateable and one of them is.** The inventory is
a list of module names compared against a function that assigns them, which
is a document against a live value -- this file's own instrument. The CI
sentence stays prose and points at `pytest.ini`, because restating a
workflow's invocation in a third file is what produced the finding.

### S19-13 -- an offline promise two things made false (A39)

`pytest.ini` opened with *"-m 'not integration' keeps the DEFAULT run
strictly offline: no process, no socket"*. On Windows a default run spawns
`wsl.exe` eleven times before any test body runs: `needs_wsl` was
`pytest.mark.skipif(not _has_wsl(), ...)`, evaluated at IMPORT, and an xdist
worker performs its own full collection, so the probe ran once per worker,
each call bounded at 20 seconds.

**The finding's preferred outcome is unavailable, and that was measured.**
It says that if the probe moves off collection the sentence can stand as
written, "and that is the better outcome, because it is the stronger claim".
It cannot: the three `needs_wsl` tests drive a real distribution **in their
bodies**, and they are not marked `integration`, so a default Windows run
spawns processes for reasons that have nothing to do with the probe. The
sentence had to be amended whatever happened to the probe -- so it names the
exception instead of claiming a property this suite takes away.

The probe is still the larger half, and the substance is the one the finding
names: *whether three tests run is decided by whether a background service
answers within 20 seconds*. That is the class `TECHNICAL_DEBT 27` closed and
batch 107 measured -- *"the suite's result on the primary platform was a
function of whether a background service happened to be started"* -- and
`isolate_sandbox_probes` cannot reach this one, because a `skipif` condition
evaluated at import is upstream of every fixture.

### Register items 85 and 97, applied where they were measured rather than where they matched

**Item 85 prescribed the same edit at six sites and one of them was right.**
Its rule has two halves: *the last wait before a positive assertion is a raw
`pause()`/`pump()`*, a source pattern that holds at all six; and *the thing
asserted is state only another thread can produce*, a claim about the code
that is **false at five**. Each producing call was read: `exit()` calls
`_release_permission_channel()` inside itself; `on_worker_state_changed` is
called by hand and `logging` fills `caplog` on this thread;
`switch_to_thread()` calls `refresh_todo_panel()` directly;
`action_pick_thread()` runs every read on the UI thread by its own comment;
`restart_for_config()` calls `self.exit(...)`, which sets `return_value`
before returning. Two of the six use `pump` ahead of NEGATIVE assertions,
which is the one thing `AGENTS.md` says `pump` is for.

Driven as well as read: with `Pilot.pause()` replaced by a bare
`asyncio.sleep(0)` -- the idle heuristic gone, the loop still able to turn --
**all six passed**, and the probe's reach was proved by sabotaging the same
monkeypatch and watching them fail inside `Pilot.pause`. `settle` keeps
working under that patch because it polls, which is what lets the probe tell
the two populations apart instead of merely breaking everything.

So the gesture test -- the one that actually failed, in a 635-second run --
takes the predicate, with the correct diagnosis beside it: the state is
produced by the MESSAGE PUMP, not by another thread. Its pair lost its
`pause()` instead of gaining a predicate, because asserting with no wait at
all is what pins the property ARCHITECTURE §4.14 depends on.

**Item 97's resize half** asserted that three `resize_terminal` calls armed
three timers, and a loaded box delivered two `Resize` messages for three
calls -- so the handler had behaved correctly for every message it was
given. The invariant is what is asserted now, with a floor of two rather
than nothing, because one delivery cannot show coalescing and
`assert armed` alone would go vacuous in the direction that hides the bug.
Its secrets half is untouched, as that entry asks in as many words.

### Design Decisions Record — §68 (A34-A39)

| id | decision |
|---|---|
| **A34** | **A suite-wide count that nothing can keep true is deleted, and the rule it decorated is gated for every file.** Four sentences said `real_storage` was the suite's only non-function-scoped fixture; there have been two in that file since batch 110, and the register's copy cited the refuting grep in the same sentence. The count goes at all four sites -- `AGENTS.md` already argues why about itself, "a count written when it was true, beside a rule that did not need it" -- and `test_every_file_with_a_module_scoped_fixture_declares_no_test_class` enforces the rule for the file a reader is writing rather than for the one that happens to hold the fixtures. It reads the fixture DECORATOR rather than grepping `scope="module"`, so a keyword in any order is found and a docstring mention is not. **Not a gated count**: the gate shape exists (`test_agents_md_states_the_real_test_tui_class_count`), and a number read back out of four sentences is four sentences to keep, where the rule is what a reader needs |
| **A35** | **A figure survives in exactly one place, the place a gate reads it; an argument that needs a magnitude states the magnitude and not the integer.** `pytest.ini`'s worked example said `test_tui.py` "is 62 classes and so 62 groups" and it is 71; the sentence needs "the classless group was the floor and is not any more". **Three of the finding's five copies were not copies** and are left alone, which is the other half of this decision: `tests.yml` and `test_tui.py` say the 183 CLASSLESS tests became 41 classes, a statement about what batch 114 did, and the register's 62 is a measurement record of a run. A figure inside a record of a measurement is not drift. Separately, `compat.yml`'s premise is re-cited: it reached the right conclusion from a floor retired 37 batches earlier, and a reader who re-measured that floor and changed the line would have been reasoning correctly |
| **A36** | **A guard that decides whether the whole suite was collected reads the marker expression off the live config, and its fake derives the expected value from its own input.** `_was_narrowed` compared `markexpr` against the literal `"not integration"`, a copy of `pytest.ini`'s addopts -- so adding a marker there made every full run read as NARROWED, four gates stop asserting and one stop running anywhere, with no failure to say so. `config.getini("addopts")` returns the shlex-split list, measured before being relied on, so the parse is "find `-m`, take the next element" plus the squashed `-mEXPR` form. It carried a third clause excluding long options, on the stated grounds that `--strict-markers` also begins with `-m`; it does not -- every long option begins with `--` -- so the clause could never change an answer and was removed in the same batch, by the control that could not kill it. `_FakeConfig` now computes its `markexpr` from its OWN addopts, so a default run is default by construction: the old fake carried the same literal as the function it tested, and asserted that two copies of one string agreed. **Not a test asserting the literal appears in `pytest.ini`**, which closes the drift by adding a fourth copy |
| **A37** | **A suite-wide scan is anchored on `__file__` and asserts its domain before asserting anything about it.** `Path("tests")` is 124 files from the repository root and 0 from one directory up, and pytest does not chdir -- so the no-sixth-copy guard passed from any other working directory having read nothing, and "found nothing" and "looked nowhere" were the same observation. `docs/ARCHITECTURE.md` section 11's first entry is this shape by name, with "pin the input in both directions" attached. The floor is `> 20`, which is `test_the_cited_test_name_guard_can_actually_fail`'s existing instrument, so there is no new idea to review; the new gate in A34 carries the same two assertions, in both directions, because a walk that finds no fixtures passes an `assert not offenders` just as quietly as one that finds no files |
| **A38** | **A mechanism's own file does not restate another file's invocation, and an inventory that can be read back is read back.** The root conftest's four-dial table annotated `-n logical` as "what CI runs" and CI runs the default -- so the file that owns the `-n auto` decision was the one describing CI as taking the other branch, in the line a reader opens to learn what the dials mean. The table points at `pytest.ini` now rather than becoming a fourth copy. The stub inventory omitted `markitdown`, which matters because the paragraph two below it is a `NOT faked, by explicit decision` list that makes the first read as complete; `test_the_root_conftest_header_names_every_module_it_stubs` compares the sentence against what `_install_fake_sdks` assigns, both directions, with a dotted child counted as named by its parent. **The finding says neither is gateable and one of them is** -- a list of module names against a function that assigns them is a document against a live value, which is that file's own instrument |
| **A39** | **The offline contract names its exception rather than claiming a property the suite takes away, and a collection-time probe becomes a setup-time one.** "No process, no socket" was false on Windows for two independent reasons, and the finding names one: eleven `wsl.exe` calls, one per xdist worker, during collection. The other is that the three `needs_wsl` tests drive a real distribution in their BODIES and are not marked `integration` -- so the sentence had to be amended whatever happened to the probe, and the finding's preferred outcome ("the sentence can stay as written") was never available. `needs_wsl` keeps its name and its three call sites and takes a STRING condition, which pytest evaluates in `pytest_runtest_setup`; with `functools.cache` that is one call per process and zero when the three are deselected, both measured, as was the module-globals lookup for a method. **Not marked `integration`**: that would make the claim true as written at the cost of moving three live tests into a set no workflow runs. **Not an environment-variable opt-in** either, which is `test_ssh_backend.py`'s answer and the wrong one here -- WSL is discoverable and an SSH host is not |

### What §68 deliberately does not change

- **What `_has_wsl` measures, or how `needs_wsl` gates** (A39). The probe
  is the honest test for "is there a distribution here", and the
  asymmetry with `test_ssh_backend.py`'s environment variable is
  reasonable: WSL is discoverable and an SSH host is not.
- **The `integration` marker's population** (A39). The three WSL tests
  stay in the default run, where the one box that can execute them does.
- **`_was_narrowed`'s other two conditions** (A36). Both `-k` and the
  args scan discriminate correctly, including for the
  `-n0 -p no:cacheprovider` form this project's own instructions use.
- **`test_the_subprocess_collection_agrees_with_the_session`'s lack of a
  file-alone fallback** (A36). It is correct to want the real session;
  giving it the subprocess on both sides would compare a value to itself.
- **`tests.yml` and `test_tui.py`'s "41 classes"** (A35), and the
  register's "62 classes were run standing alone". Statements about what
  batch 114 did, and a record of a measurement. Neither is drift.
- **The five of item 85's six whose state is produced synchronously.** A
  predicate there would be ceremony implying a race the code does not
  have, which is B4 from the other side.
- **Item 97's secrets half**, which that entry asks to be left until it
  recurs -- "changing a test to chase a failure it has not shown again is
  how a real defect gets hidden behind a louder fixture".
