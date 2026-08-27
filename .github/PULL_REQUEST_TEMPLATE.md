<!--
Describe WHAT changed and WHY. The diff already says how.

Large or design changes should have an issue agreed first — anything that adds a new § or
D-number, changes a file-level contract in ARCHITECTURE.md, or touches core/loop.py,
core/client.py, tools/registry.py or the security/ boundaries. See CONTRIBUTING.md.
-->

## What

<!-- One or two sentences. The behavior that is different after this PR. -->

## Why

<!-- The problem this solves. Link the issue: "Closes #123". -->

## Checklist

- [ ] `python -m pytest -q` is green (`cp providers.json.example providers.json` first on a fresh clone)
- [ ] A test covers the change — a new one for a bugfix, or an existing one that would have caught it
- [ ] If this pins a new behavior, `tests/BREAKING_CHANGES.md` has a row saying what breaks, the symptom, and the fix
- [ ] Docs updated in the **one** file that owns the fact (see the table in `CONTRIBUTING.md`), not in two
- [ ] If this deviates from a locked decision in `ROADMAP.md` / `ROADMAP_v2.md`, `DEVLOG.md` records the deviation and why — those specs are append-only

## Notes for the reviewer

<!--
Anything you are unsure about, deliberately left out, or want pushed back on.
"Not doing" is as useful here as it is in an issue.
-->

---

<!--
Sign-off is requested but not enforced by CI: `git commit -s`.
By opening this PR you agree your contribution is licensed under Apache-2.0 (see CONTRIBUTING.md).
If you do not want that, mark the PR conspicuously as `Not a Contribution` and contact the maintainer.
-->
