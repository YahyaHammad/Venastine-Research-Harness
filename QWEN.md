# QWEN.md

**Read [AGENTS.md](AGENTS.md) before making any non-trivial change to this repository.**

It is the agent-context file for this project, and it is not optional reading: it records design
decisions that are *locked* rather than defaults to re-derive, and several of them have already
been broken once by someone who reasoned from the code alone. It also names which of
`ARCHITECTURE.md`, `ROADMAP.md`, `ROADMAP_v2.md`, `DEVLOG.md` and `tests/BREAKING_CHANGES.md` to
read for what.

This file used to carry its own copy of that guidance. It drifted — by the time it was audited,
five of its claims were false about the present code, including a test count off by an order of
magnitude and a "Known Bug" about missing test dependencies that had long since been fixed. A
second copy is how that happens, so there is now one copy and several filenames.
