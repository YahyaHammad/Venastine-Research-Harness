#!/usr/bin/env python3
r"""scripts/bandit-count-check.py -- the half `-b` does not check (batch 141).

`.github/workflows/bandit.yml` says of its baseline flag:

    `-b .github/bandit-baseline.json`: fails only on NEW issues

That holds for the FIRST occurrence per (file, test id) and for nothing after
it. Bandit compares a finding to the baseline with `Issue.__eq__`, whose own
comment is the whole mechanism (`bandit/core/issue.py`):

    # if the issue text, severity, confidence, and filename match, it's
    # the same issue from our perspective
    match_types = ["text", "severity", "cwe", "confidence", "fname",
                   "test", "test_id"]

Neither `lineno` nor `code` is in that list, and `_compare_baseline_results`
is a plain membership test. So the unit of suppression is (file, test id,
message) rather than the call site, and any number of further occurrences in
that file collapse onto the first. Measured at batch 141: a NEW
`subprocess.run` added to `security/sandbox.py` is reported by nothing, while
the same call in a file with no baseline entries IS reported, and a new `eval`
in that same file IS reported. The silence is the baseline absorbing it.

The files that can absorb one that way, from the baseline's own `results`:
`security/sandbox.py` (B603 x7, B607 x3, B404), `pipeline_storage.py`
(B608 x2), `main.py` (B603, B606, B404), `tools/isolation.py` (B603, B404),
`tui/app.py` (B110 x3). The first two are the sandbox and the module that
builds SQL -- the two places a newly introduced finding of exactly that class
would matter most.

WHAT THIS DOES, and it is the behaviour bandit's own `filter_results`
docstring describes three lines above the code that does not do it:

    If the number of results is different to the number reported for the
    same file in the baseline, then we return all results for the file.

Group the baseline by (file, test id). Run bandit with no `-b`. Group the same
way. Fail on any pair whose count has INCREASED. That closes the hole with no
source annotations, leaves the triaged entries exactly as they are, and
reports a file and a test id rather than a line -- which matters, because
baseline line numbers are already known not to be an index into the file
(TECHNICAL_DEBT 18 measured a B314 row saying arxiv.py:141 against a call at
line 182).

BESIDE `-b`, NOT INSTEAD OF IT. The two catch different things: `-b` matches
on message TEXT, so it still reports a reworded finding at an unchanged count,
and this reports a second occurrence at an unchanged message. Neither subsumes
the other.

A DECREASE IS NOT AN ERROR. Removing a `subprocess.run` is the good direction,
and failing on it would make the baseline a thing you have to regenerate --
which items 18 and 22 both say never to do blind, and which is the habit
`bandit.yml` says in its own words it is trying not to build.

ITS SENSITIVITY FOR A TRIAGED PAIR IS BLUNTED BY THE STALE BASELINE COUNT,
and that is measured rather than estimated. Batch 141 annotated all 18 B603
sites in `security/sandbox.py`, so the live count for that pair is 0 while the
baseline still says 7 -- seven findings of headroom before this check fires.
Measured by removing annotations one at a time: it stays green at 1, 4 and 7
removed, and goes red at 8. The mechanism is sound; the slack is exactly the
number of now-dead baseline entries.

Those seven entries can no longer match anything, which makes them the same
kind of inert row `bandit.yml`'s header already describes for the 65
B105/B106 entries -- and makes the same argument for leaving them: dropping
them is an edit to a triaged file, which is a deliberate reviewed step rather
than tidying. If that is ever done, this check gets sharper for free.

PATH FORMS ARE NORMALISED, because the baseline is stored in `./x.py` form
(Linux) and bandit on Windows emits `.\x.py`. Without this the script reports
every file as new on a developer's machine, which is TECHNICAL_DEBT 22's
measured trap -- it is why that entry says to rewrite a throwaway copy rather
than regenerate.
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, ".github", "bandit-baseline.json")

# The same skips bandit.yml passes. B101 is assert-in-source, B105/B106 are
# the hardcoded-password heuristics the header explains are inert here.
SKIPS = "B101,B105,B106"


def normalise(name: str) -> str:
    """One spelling for a path, whichever platform produced it."""
    return name.replace("\\", "/").lstrip("./")


def grouped(results) -> dict:
    counts = collections.Counter()
    for issue in results:
        counts[(normalise(issue["filename"]), issue["test_id"])] += 1
    return counts


def python_files() -> list:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def main() -> int:
    with open(BASELINE, encoding="utf-8") as handle:
        baseline = grouped(json.load(handle)["results"])
    if not baseline:
        print("bandit-count-check: the baseline has no results. That is "
              "either a corrupt file or a regenerated-away baseline; either "
              "way this check cannot discriminate.", file=sys.stderr)
        return 2

    files = python_files()
    if not files:
        print("bandit-count-check: `git ls-files '*.py'` found nothing.",
              file=sys.stderr)
        return 2

    proc = subprocess.run(
        [sys.executable, "-m", "bandit", *files, "-s", SKIPS, "-f", "json",
         "-q"],
        cwd=ROOT, capture_output=True, text=True)
    # Bandit exits 1 when it has findings, which is the normal case here --
    # the baseline exists precisely because there are accepted ones. Only a
    # missing/!unparseable report is a failure of this script.
    try:
        report = json.loads(proc.stdout)
        live = grouped(report["results"])
    except (ValueError, KeyError):
        print("bandit-count-check: could not parse bandit's JSON report.\n"
              "stdout head: %s\nstderr head: %s"
              % (proc.stdout[:400], proc.stderr[:400]), file=sys.stderr)
        return 2

    # A FILE BANDIT COULD NOT PARSE HAS NO FINDINGS, which reads here as a
    # DECREASE and so passes silently -- a syntax error would switch this
    # check off for exactly the file being edited. Measured: mutation A7 in
    # batch 141 corrupted `security/sandbox.py` and this script reported
    # "none grew". Bandit puts those in `errors`, so they are fatal here.
    errors = report.get("errors") or []
    if errors:
        print("bandit-count-check: bandit could not scan %d file(s), so "
              "their findings are missing rather than absent:"
              % len(errors), file=sys.stderr)
        for err in errors[:10]:
            print("  %s: %s" % (err.get("filename"), err.get("reason")),
                  file=sys.stderr)
        return 2

    grew = []
    for key in sorted(set(baseline) | set(live)):
        was, now = baseline.get(key, 0), live.get(key, 0)
        if now > was:
            grew.append((key[0], key[1], was, now))

    if not grew:
        print("bandit-count-check: %d (file, test) pairs in the baseline, "
              "none grew." % len(baseline))
        return 0

    print("bandit-count-check: NEW findings the baseline would absorb.\n",
          file=sys.stderr)
    for filename, test_id, was, now in grew:
        print("  %-44s %-6s %d -> %d" % (filename, test_id, was, now),
              file=sys.stderr)
    print("\n`-b` reports only the FIRST occurrence per (file, test id, "
          "message), so a second one in a file that already carries a "
          "baselined finding of the same id is silent. Fix the finding, or "
          "-- if it is genuinely accepted -- say so in the rollout summary "
          "and add it to the baseline deliberately, which is a reviewed step "
          "and not a regeneration.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
