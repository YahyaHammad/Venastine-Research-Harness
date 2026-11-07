# Third-Party Notices

This project is licensed under the **Apache License 2.0** — see [LICENSE](../LICENSE) and [NOTICE](../NOTICE) (Copyright 2026 Yahya Hammad).

The tables record the declared core, test, optional, build and hosted dependencies.
Their project-level licenses are permissive; this is not a license assessment of
all transitive packages or embedded native libraries. Declarations and the
license corrections below were checked on 2026-11-07 against the manifests,
PyPI release metadata and the relevant wheel license files.

> **Distribution scope.** Git source archives, the GitHub installers and npm
> carry first-party source and notices; Python dependencies are fetched separately.
> The hosted terminal **does deliver third-party JavaScript and CSS** to browsers.
> Its notices accompany the JavaScript response and are linked from the terminal.
> A built ModelScope container contains installed dependencies and Debian packages;
> distributing that image requires a separate review of the exact image layers.

> **Transitive dependencies.** These tables are not a full resolved inventory.
> For example, HTTP clients bring certifi (MPL-2.0), hosted installs bring tqdm
> (MPL-2.0 AND MIT) and Gradio brings orjson (MPL-2.0 AND (Apache-2.0 OR MIT)).
> `httpx2` (BSD-3-Clause) and SQLAlchemy (MIT) are imported directly by the
> harness but supplied transitively by the SDKs/MCP and SQLModel, respectively.
> Versions selected by ranges can change; use the exact installed artifacts
> when reviewing a redistributed environment.

## Runtime / core dependencies

| Package | Version (as pinned) | Primary License | Apache 2.0 Compatible | Source |
|---|---|---|---|---|
| anthropic | `==2.1.0` | Apache-2.0 (MIT through 0.x; 2.0 relicensed) | Yes | https://pypi.org/project/anthropic/ |
| ddgs | `==9.16.0` | MIT | Yes | https://pypi.org/project/ddgs/ |
| httpx | `==0.28.1` | BSD-3-Clause | Yes | https://pypi.org/project/httpx/ |
| openai | `==3.36.0` | Apache-2.0 | Yes | https://pypi.org/project/openai/ |
| google-genai | `==2.32.0` | Apache-2.0 | Yes | https://pypi.org/project/google-genai/ |
| pydantic | `==2.14.1` | MIT | Yes | https://pypi.org/project/pydantic/ |
| sympy | `==1.14.0` | BSD-3-Clause | Yes | https://pypi.org/project/sympy/ |
| sqlmodel | `==0.0.48` | MIT | Yes | https://pypi.org/project/sqlmodel/ |
| python-dotenv | `==1.2.4` | BSD-3-Clause | Yes | https://pypi.org/project/python-dotenv/ |
| pyyaml | `==6.0.3` | MIT | Yes | https://pypi.org/project/PyYAML/ |
| ruamel.yaml | `==0.19.1` | MIT | Yes | https://pypi.org/project/ruamel.yaml/ |
| textual | `==8.2.8` | MIT | Yes | https://pypi.org/project/textual/ |
| rich | `>=14.2,<16` | MIT | Yes | https://pypi.org/project/rich/ |
| mcp | `==2.3.0` | MIT | Yes | https://pypi.org/project/mcp/ |
| google-re2 | `==1.1.20251105` | BSD-3-Clause | Yes | https://pypi.org/project/google-re2/ |
| cryptography | `>=50.0.0,<52` | Apache-2.0 OR BSD-3-Clause | Yes | https://pypi.org/project/cryptography/ |
| PyJWT | `>=2.15.0,<3` | MIT | Yes | https://pypi.org/project/PyJWT/ |
| defusedxml | `==0.7.1` | PSF-2.0 | Yes | https://pypi.org/project/defusedxml/ |
| pypdf | `==6.21.4` | BSD-3-Clause | Yes | https://pypi.org/project/pypdf/ |

`google-re2` and `cryptography` ship compiled native code. RE2's wheels
include the RE2 C++ library (BSD-3-Clause) and Abseil (Apache-2.0).
Cryptography's wheels include a Rust extension and OpenSSL (Apache-2.0).
The source installers fetch these wheels onto the user's machine; a distributor
of an installed environment must inspect their bundled notices separately.
Declaring cryptography did not introduce it to the install closure: Google
Auth already required it through Google Gen AI.

`pillow` was declared on 2026-10-28 (batch 201), as a floor under what `matplotlib` pulled in, and removed with `matplotlib` in batch 212: the default harness install no longer reaches it; optional document/PDF dependencies can still bring it. The hosted demo's Gradio bridge still installs it, in an environment of its own (`deploy/hosted/requirements.txt`).

## Test dependencies (not used by end users during runtime, only for CI and developer testing)

| Package | Version (as pinned) | Primary License | Apache 2.0 Compatible |
|---|---|---|---|
| pytest | `>=8.0` | MIT | Yes |
| pytest-mock | `>=3.14` | MIT | Yes |
| pytest-asyncio | `>=1.0` | Apache-2.0 | Yes |
| pytest-xdist | `>=3.6` | MIT | Yes |
| starlette | `>=0.40` | BSD-3-Clause | Yes |
| execnet | transitive (via `pytest-xdist`) | MIT | Yes |

`execnet` is listed although nothing declares it: it is `pytest-xdist`'s only dependency and the process that actually runs each worker, so a reader auditing what a `pip install -r requirements-test.txt` puts on the machine would otherwise have to go and find it. It belongs to the test environment rather than the default runtime.

## Optional dependencies (`pyproject.toml` extras)

| Package | Version (as pinned) | Primary License | Apache 2.0 Compatible | Notes |
|---|---|---|---|---|
| markitdown | `==0.1.9` (extra `documents`) | MIT | Yes | Permissive; only reachable behind `read`/`write`/`edit` (globally denied). |
| protobuf | `==7.36.2` (extra `documents`) | BSD-3-Clause | Yes | Permissive; transitive via `markitdown`→`onnxruntime`. |
| markdown | unpinned (extra `pdf`) | BSD-3-Clause | Yes | Permissive |
| weasyprint | unpinned (extra `pdf`) | BSD-3-Clause (package) | Yes (with caveats) | Package itself BSD-3-Clause; Pyphen has alternative GPL/LGPL/MPL code terms and separate dictionary terms; system libraries have their own licenses. |

`weasyprint` is BSD-3-Clause itself. Its Python closure includes Pyphen with
alternative GPL/LGPL/MPL code licenses and separately licensed dictionaries.
Its native stack includes Pango (LGPL) and HarfBuzz (MIT-style); current
WeasyPrint uses pydyf for PDF output. Review exact Python and OS artifacts
before redistributing a PDF-capable environment. Windows MCP installs also
bring pywin32, whose adodbapi component has LGPL-2.1 terms.

## Build backend

| Package | Declaration | Primary License | Source |
|---|---|---|---|
| setuptools | `>=77` in `pyproject.toml` | MIT, with separate vendor notices | https://pypi.org/project/setuptools/ |

## Hosted demo (`deploy/`, ROADMAP_v3 §79)

The bridge runs separately from the harness's session environment. These
packages are installed by the hosting platform or generated Docker build;
`deploy/` is excluded from npm and the metadata-only Python wheel. Installing
packages on the server and sending their assets to browsers are distinct paths.

| Package | Version (as pinned) | Primary License | Apache 2.0 Compatible | Where |
|---|---|---|---|---|
| textual-serve | `==1.1.3` | MIT | Yes | `deploy/hosted/requirements.txt`; its browser assets only, its server never runs |
| uv | `==0.13.4` | MIT OR Apache-2.0 | Yes | `deploy/hosted/requirements.txt`; builds the session environment |
| websockets | `==17.3` | BSD-3-Clause | Yes | `deploy/hosted/requirements.txt`; the websocket upgrade every session is |
| gradio | `==6.30.0` | Apache-2.0 | Yes | installed by the Space at the README's `sdk_version`; in the Studio's `bridge.lock` |
| spaces | `==0.51.3` | Apache-2.0 | Yes | installed by the Space itself; in the Studio's `bridge.lock` |

### Browser assets

The bridge serves only `js/textual.js`, `css/xterm.css` and `NOTICE.txt` under
`/venastine-static/`. It does not expose the wheel's unused Roboto Mono fonts
or background image. The font name tables in Textual Serve 1.1.3 identify
Apache-2.0; blocking those URLs avoids delivering unneeded font copies.
The terminal's external Google Fonts stylesheet is a separate delivery by Google.

The reviewed wheel SHA-256 is
`207a472bc6604e725b1adab4ab8bf12f4c4dc25b04eea31e4d04731d8bf30f18`.
The bridge checks the original JS and CSS hashes before serving them and adds
[full browser notices](../deploy/hosted/browser-NOTICES.txt) to every JS response.
The same text is available at `/venastine-static/NOTICE.txt` and linked from the
terminal. The CSS retains its full MIT notice. Embedded components identified
in the JS include xterm.js and its addons (MIT), js-base64 3.7.7 (BSD-3-Clause),
and webpack loader/runtime code (MIT), as well as Textual Serve (MIT).
Upstream supplies no frontend lockfile, so exact versions of every embedded
component are not established. A changed asset hash requires a renewed review.

### Installed images and native components

The ModelScope recipe installs the bridge and session packages into a
Debian-based Python image. This review did not obtain the deployed image or
establish its delivery terms. It therefore makes no claim of image compliance
or violation. Before supplying an image copy, inspect its exact wheels and OS
packages, preserve bundled LICENSE/NOTICE files, and satisfy applicable source
availability and license requirements for MPL/LGPL/GPL-covered components.
Include OpenSSL, Abseil, native libraries bundled in wheels and Debian packages
in that review. Platform-managed Hugging Face packages, including GPU wheels,
are not a fixed inventory established by the source manifests alone.

## Development and deployment tooling

These tools run in CI or the operator's environment; they are not npm runtime
dependencies. Entries describe the primary project licenses, not every vendored
component or service agreement.

| Package | Declaration / use | Primary License |
|---|---|---|
| huggingface_hub | `==2.2.0`, deployment CI | Apache-2.0 |
| pip-audit | `==2.10.1`, dependency audit | Apache-2.0 |
| bandit | `==1.9.4`, security scan | Apache-2.0 |
| ruff | `==0.15.21`, lint | MIT |
| uv | `==0.13.4`, CI and hosted setup | MIT OR Apache-2.0 |
| modelscope | optional operator login CLI, unpinned | Apache-2.0 |
| gitleaks | `8.30.1`, secret scan | MIT |

CPython, Node.js/npm, GitHub Actions, CodeQL and optional Docker/Podman and SSH
helpers have their own project, bundled-component and, for CodeQL, service/tool
terms. They are execution tools rather than dependencies embedded in the source
package. Do not infer their complete license inventory from the Python tables.

## Non-code third-party material

The repository does not vendor dependency wheels, third-party fonts or browser
bundles. The hosted bridge reads the browser bundle from its installed wheel
and adds the reproduced license texts in `deploy/hosted/browser-NOTICES.txt`.
The cover image contains rendered glyphs from Cascadia Mono (SIL Open Font
License); it does not carry the font file.

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

* The harness's Apache-2.0 license does not replace the licenses of dependencies,
  browser components, fonts or native libraries. Permissive direct dependencies
  do not imply that the full installed closure contains no copyleft components.
* The source channels fetch Python packages separately. The npm launcher uses
  Node built-ins and declares no npm dependencies or bundled dependencies.
  Do not add wheels or installed dependency directories to the npm tarball.
* Hosted JS and CSS are copies delivered to visitors. Their notices must
  accompany those copies; a LICENSE file only in server-side wheel metadata
  does not accompany the browser response.
* A redistributed installed image needs its own artifact inventory, notices
  and any applicable source availability arrangements. A source-only assessment
  does not establish compliance for that image.
* The root [NOTICE](../NOTICE) describes these delivery paths. The first-party
  copyright notice is voluntary; recipients must still retain applicable
  third-party notices when redistributing their covered components.

## How to regenerate

Read `requirements.txt`, `requirements-test.txt`, the build backend and extras
in `pyproject.toml`, `deploy/hosted/requirements.txt`, and generated platform
inputs in `scripts/build_demo_bundle.py`. Inspect the exact resolved wheels
and their license files, including vendor notices; metadata alone can omit them.
`pip-licenses --format markdown --with-urls --with-license-file` can help list
an installed environment, but does not inventory browser bundles or OS layers.

When a declaration changes, update its table row and review the actual delivery
path. For a Textual Serve repin, inspect the bundle components and fonts, update
browser notices and the reviewed asset hashes, and verify the served responses.

*Last checked: 2026-11-07.*
