# Third-Party Notices

This project is licensed under the **Apache License 2.0** — see [LICENSE](./LICENSE) (Copyright 2026 Yahya Hammad).

The dependencies listed below are **not** part of the project's own source but are required at install/runtime. All are permissive and **Apache-2.0 compatible** per the table you provided (2026-08-27). This file records that assessment so a scanner/reviewer does not have to re-derive it.

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

Grouped as `pytest / plugins, MIT / Apache-2.0, Yes` per your table.

## Optional dependencies (`pyproject.toml` extras)

| Package | Version (as pinned) | Primary License | Apache 2.0 Compatible | Notes |
|---|---|---|---|---|
| markitdown | `==0.1.6` (extra `documents`) | MIT | Yes | Permissive; only reachable behind `read`/`write`/`edit` (globally denied). |
| protobuf | `==7.35.1` (extra `documents`) | BSD-3-Clause / Apache-2.0 | Yes | Permissive; transitive via `markitdown`→`onnxruntime`. |
| markdown | unpinned (extra `pdf`) | BSD-3-Clause | Yes | Permissive |
| weasyprint | unpinned (extra `pdf`) | BSD-3-Clause (package) | Yes (with caveats) | Package itself BSD-3-Clause; transitive deps include LGPL (e.g. `pango`/`cairo` via system libs). See weasyprint docs. |

As you noted: `weasyprint = BSD-3-Clause, Yes (with caveats), Permissive (Package) / LGPL (Dependencies)`. If you later pin `markdown`/`weasyprint`, update the version column here.

## What this means

* All listed licenses are **permissive** (MIT / BSD-3-Clause / PSF / Apache-2.0). None are copyleft (GPL/AGPL) and none impose source-disclosure on this Apache-2.0 work.
* No `NOTICE` file is required by any of these dependencies for a source distribution. If you later distribute a binary/wheel that vendors them, include their `LICENSE` files as shipped on PyPI.
* Full license texts: `pip show <package>` or `pip-licenses --format plain --with-urls`, or the PyPI links above.

## How to regenerate

```bash
pip-licenses --format markdown --with-urls --with-license-file
# or
pip install pip-licenses && pip-licenses --format plain
```

If you add/pin a new dependency, add a row here with its SPDX identifier and re-check the `Apache 2.0 Compatible` column. CI does not enforce this file today — it's documentation for reviewers.

---
*Last updated: 2026-08-27 — assessment provided by maintainer; no assumptions made beyond the table above. If any row's upstream re-licenses, this file must be updated.*
