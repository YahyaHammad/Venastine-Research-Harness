#!/usr/bin/env python3
r"""scripts/bump_copyright_year.py -- the copyright year, kept current.

ROADMAP_v3 §80 (batch 169). The owner asked not to have to remember it.
copyright-year.yml runs this at 23:59 UTC on December 31, and again early
on January 1 in case GitHub delayed or dropped the first run -- it does
both at busy hours, and midnight UTC on New Year is the busiest there is.

    python scripts/bump_copyright_year.py           # rewrite, if due
    python scripts/bump_copyright_year.py --check   # only say what it would do

THE NOTICE IS A RANGE, the owner's choice: `Copyright 2026 Yahya Hammad`
becomes `Copyright 2026-2027 Yahya Hammad`, then `2026-2028`. The first
year is the one a copyright notice points at, and it never moves.

BOTH FILES: the appendix of LICENSE and NOTICE carry the same line, and
NOTICE is where Apache-2.0 attribution lives. They are rewritten together
or not at all, and a disagreement between them is refused rather than
guessed at.

THE YEAR IT WRITES is next year on December 31 (UTC) and this year on any
other day, so a run on time and a run delayed past midnight both write the
same thing, and a second run changes nothing. It never moves a year
backwards.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("LICENSE", "NOTICE")
# The match stops short of a CR, so a CRLF file keeps it and its holder
# reads the same as an LF file's (LICENSE is CRLF in a Windows worktree).
LINE = re.compile(r"^(?P<indent>[ \t]*)Copyright (?P<first>\d{4})"
                  r"(?:-(?P<last>\d{4}))? (?P<holder>\S[^\r\n]*?)"
                  r"[ \t]*(?=\r?$)", re.MULTILINE)


class YearError(Exception):
    """Nothing was written; the message says why."""


def target_year(now: datetime.datetime) -> int:
    now = now.astimezone(datetime.UTC)
    return now.year + 1 if (now.month, now.day) == (12, 31) else now.year


def _notice(text: str, name: str) -> re.Match:
    found = list(LINE.finditer(text))
    if len(found) != 1:
        raise YearError(f"{name} has {len(found)} copyright lines, not one")
    return found[0]


def bump(root: Path = ROOT, now: datetime.datetime | None = None,
         write: bool = True) -> tuple:
    """(old notice, new notice); the same twice when nothing was due."""
    now = now or datetime.datetime.now(datetime.UTC)
    texts, matches = {}, {}
    for name in FILES:
        # newline="" keeps each file's own line endings.
        with open(root / name, encoding="utf-8", newline="") as f:
            texts[name] = f.read()
        matches[name] = _notice(texts[name], name)
    parts = {name: (m["first"], m["last"] or m["first"], m["holder"])
             for name, m in matches.items()}
    if len(set(parts.values())) != 1:
        raise YearError(f"LICENSE and NOTICE disagree: {parts}")
    first, last, holder = parts[FILES[0]]
    old = f"Copyright {first}" + (f"-{last}" if last != first else "") \
        + f" {holder}"
    year = target_year(now)
    if year <= int(last):
        return old, old
    new = f"Copyright {first}-{year} {holder}"
    if write:
        for name, m in matches.items():
            text = texts[name]
            line = m["indent"] + new
            with open(root / name, "w", encoding="utf-8", newline="") as f:
                f.write(text[:m.start()] + line + text[m.end():])
    return old, new


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="say what would change; write nothing")
    args = parser.parse_args(argv)
    try:
        old, new = bump(write=not args.check)
    except (YearError, OSError) as exc:
        print(f"bump_copyright_year: {exc}", file=sys.stderr)
        return 1
    changed = old != new
    print(f"{old} -> {new}" if changed else f"{old}: current")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"changed={str(changed).lower()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
