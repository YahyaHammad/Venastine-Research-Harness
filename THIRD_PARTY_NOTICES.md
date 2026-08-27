# Third-Party Notices

This project is licensed under the **Apache License 2.0** — see [LICENSE](./LICENSE) and [NOTICE](./NOTICE) (Copyright 2026 Yahya Hammad).

The dependencies listed below are **not** part of the project's own source but are required at install/runtime. All are permissive and **Apache-2.0 compatible**; the assessment was made on 2026-08-27. This file records it so a scanner or reviewer does not have to re-derive it.

> **Scope.** Two things are covered here: the project's **declared direct** dependencies — the pins in `requirements.txt` and the extras in `pyproject.toml` — and, under *Non-code third-party material* below, the third-party **text** the repository reproduces.
>
> The transitive dependency closure is *not* enumerated: `matplotlib` alone pulls numpy, pillow, kiwisolver and fonttools, and `mcp==2.0.0` brings `httpx2` (see the note at `requirements.txt:47`). For the full closure, run `pip-licenses` against a resolved environment — see *How to regenerate* below.

> Versions shown are the pins from `requirements.txt` / `pyproject.toml` at the time this file was created. Where a range is used (e.g. `textual>=1.0,<2.0`) the exact resolved version will vary.

## Runtime / core dependencies

| Package | Version (as pinned) | Primary License | Apache 2.0 Compatible | Source |
|---|---|---|---|---|
| anthropic | `==0.116.0` | MIT | Yes | https://pypi.org/project/anthropic/ |
| ddgs | `==9.14.4` | MIT | Yes | https://pypi.org/project/ddgs/ |
| httpx | `==0.28.1` | BSD-3-Clause | Yes | https://pypi.org/project/httpx/ |
| openai | `==2.45.0` | Apache-2.0 | Yes | https://pypi.org/project/openai/ |
| google-genai | `==1.0.0` | Apache-2.0 | Yes | https://pypi.org/project/google-genai/ |
| pydantic | `==2.13.4` | MIT | Yes | https://pypi.org/project/pydantic/ |
| sympy | `==1.13.3` | BSD-3-Clause | Yes | https://pypi.org/project/sympy/ |
| sqlmodel | `==0.0.39` | MIT | Yes | https://pypi.org/project/sqlmodel/ |
| python-dotenv | `==1.0.1` | BSD-3-Clause | Yes | https://pypi.org/project/python-dotenv/ |
| matplotlib | `==3.10.3` | PSF / BSD-Compatible | Yes | https://pypi.org/project/matplotlib/ |
| pyyaml | `==6.0.3` | MIT | Yes | https://pypi.org/project/PyYAML/ |
| textual | `>=1.0,<2.0` | MIT | Yes | https://pypi.org/project/textual/ |
| rich | `>=13,<16` | MIT | Yes | https://pypi.org/project/rich/ |
| mcp | `==2.0.0` | MIT | Yes | https://pypi.org/project/mcp/ |

## Test dependencies (not used by end users during runtime, only for CI and developer testing)

| Package | Version (as pinned) | Primary License | Apache 2.0 Compatible |
|---|---|---|---|
| pytest | `>=8.0` | MIT | Yes |
| pytest-mock | `>=3.14` | MIT | Yes |
| pytest-asyncio | `>=1.0` | Apache-2.0 | Yes |

## Optional dependencies (`pyproject.toml` extras)

| Package | Version (as pinned) | Primary License | Apache 2.0 Compatible | Notes |
|---|---|---|---|---|
| markitdown | `==0.1.6` (extra `documents`) | MIT | Yes | Permissive; only reachable behind `read`/`write`/`edit` (globally denied). |
| protobuf | `==7.35.1` (extra `documents`) | BSD-3-Clause / Apache-2.0 | Yes | Permissive; transitive via `markitdown`→`onnxruntime`. |
| markdown | unpinned (extra `pdf`) | BSD-3-Clause | Yes | Permissive |
| weasyprint | unpinned (extra `pdf`) | BSD-3-Clause (package) | Yes (with caveats) | Package itself BSD-3-Clause; transitive deps include LGPL (e.g. `pango`/`cairo` via system libs). See weasyprint docs. |

`weasyprint` is BSD-3-Clause itself; the caveat is its **system** dependencies (`pango`, `cairo`, `harfbuzz`), which are LGPL and are linked at runtime rather than vendored into this project. If `markdown` or `weasyprint` is later pinned, update the version column above.

## Non-code third-party material

**No third-party source code is included in this repository.** Measured, not assumed: 166 tracked
Python files carry no foreign copyright header and no `SPDX-License-Identifier`; a sweep for
copied or adapted code ("adapted from", "copied from", "taken from", "vendored", "borrowed from")
returns only this project's own design prose; and the only tracked files that are neither `.py`
nor `.md` are `LICENSE`, `prompts/universal_system_prompt`, `prompts/untrusted_content`,
`research-mode-birds-eye.mermaid` and `tui/app.tcss` — all first-party. There are no bundled
fonts, images or data files.

What the repository *does* reproduce is third-party **text**:

| Material | Where it lives | Origin | Licensing |
|---|---|---|---|
| **Contributor Covenant, v2.1** | `CODE_OF_CONDUCT.md` (adapted — contact details and enforcement channel are ours) | Authored 2014 by Coraline Ada Ehmke, with later collaborative contributions. https://www.contributor-covenant.org/version/2/1/code_of_conduct.html | **Not stated by upstream** for the document text — see the note below |
| **Mozilla's CoC enforcement ladder** | Inspiration for the *Enforcement Guidelines* section of `CODE_OF_CONDUCT.md`, credited there per the Covenant's own boilerplate | https://github.com/mozilla/diversity | GitHub reports the repository as **MPL-2.0** |
| **Apache License 2.0 text** | `LICENSE` | https://www.apache.org/licenses/LICENSE-2.0 | Reproduced verbatim, which is what the licence itself directs; the appendix boilerplate is filled in with this project's copyright |

> **On the Covenant's licence.** Upstream does not publish an explicit licence for the Code of
> Conduct *text*. Three sources were checked on 2026-08-27: the project's `LICENSE.md`
> (`EthicalSource/contributor_covenant`) states the **Hippocratic License 3.0**, which covers the
> site and its code; the contributor-covenant.org footer carries a copyright line and no licence
> statement; and the raw `content/version/2/1/code_of_conduct.md` carries none either.
>
> **No licence identifier is asserted here on the Covenant's behalf.** The attribution block kept
> in `CODE_OF_CONDUCT.md` — naming the work, its version, its source URL, and the fact that it is
> *adapted* — is verbatim the attribution upstream prescribes in its own boilerplate, and is the
> best available evidence of what they ask adopters to provide. If you need certainty for a
> compliance review, ask upstream rather than inferring from this file.

## What this means

* All licenses **listed above** are **permissive** (MIT / BSD-3-Clause / PSF / Apache-2.0). None are copyleft (GPL/AGPL) and none impose source-disclosure on this Apache-2.0 work. This is a statement about the direct dependencies in the tables, not about the full transitive closure — see *Scope* at the top.
* This project does not redistribute any of them: it is run from a checkout, and `pip` fetches each dependency from PyPI under its own license. **No inbound `NOTICE` obligation therefore arises** — nothing here carries an upstream NOTICE that this project must propagate. The trigger is **redistribution**, not source-vs-binary; if a build that vendors these packages is ever distributed, ship their `LICENSE` and `NOTICE` files as received (Apache-2.0 §4(d) applies to `openai`, `google-genai` and `pytest-asyncio`).
* The [NOTICE](./NOTICE) file this project publishes is therefore **voluntary**, not required. Apache-2.0 §4(d) obliges you to propagate a NOTICE you *received*; it does not oblige a licensor to author one. It exists because scanners and reviewers look for it and because it gives the copyright assertion a canonical home — and it is kept minimal on purpose, since anyone who redistributes this work must carry its contents forward.
* Full license texts: `pip show <package>` or `pip-licenses --format plain --with-urls`, or the PyPI links above.

## How to regenerate

```bash
pip-licenses --format markdown --with-urls --with-license-file
# or
pip install pip-licenses && pip-licenses --format plain
```

When a dependency is added or re-pinned, add a row here with its SPDX identifier and re-check the `Apache 2.0 Compatible` column. CI does not enforce this file today — it is documentation for reviewers.

---
*Last updated: 2026-08-27. If any row's upstream re-licenses, this file must be updated.*
