#!/usr/bin/env python3
r"""scripts/build_demo_bundle.py -- the folder a hosted deployment is made of.

ROADMAP_v3 §79 (HD9). A Hugging Face Space and a ModelScope Studio are each
a repository the platform builds and runs, and what goes into that repository
is a BUILT BUNDLE, never a mirror of the working tree. The first manual
deploy (2026-10-20) is why that is written in capitals: `hf upload` of the
checkout sent 527 files, among them three app logs, two research runs, local
review notes, a security scan's metadata and a notebook checkpoint of
`providers.json` -- none of it secret that time, all of it gitignored, and
all of it now in the Space's history.

    python scripts/build_demo_bundle.py --platform hf --out DIR
    python scripts/build_demo_bundle.py --platform modelscope --out DIR

WHAT GOES IN, from lists that already exist rather than a new one:

  * the files `git ls-files --cached --others --exclude-standard` names --
    tracked, plus untracked-and-not-ignored, so .gitignore is honoured and
    an uncommitted deploy/ still builds locally;
  * of those, only what package.json's `files` ships to npm, plus deploy/;
  * and then every path is checked against package.json's `neverDistribute`,
    ALL channels, and a match is a refusal rather than a skip. A deployment
    is a fourth channel that runs the code beside live tokens, so it takes
    the union of the other three.

WHAT IS GENERATED, on both platforms: `app.py` (the entry point, which hands
over to deploy/hosted/app.py); `deploy/hosted/session.lock` (the harness's
requirements.txt compiled by uv, every file hashed, which session_env.py
installs into a separate environment, so the harness runs on exactly the
pins CI tests); `README.md`; `.gitattributes`; and `BUILD_INFO.json` (the
commit the page footer shows, and the platform the bridge reads). The locks
need `uv` and the network, as any resolve does.

ON HUGGING FACE the bundle is a Gradio-SDK Space: the README's YAML
frontmatter IS its configuration, and `requirements.txt` (the BRIDGE's
needs, from deploy/hosted/requirements.txt plus the harness's textual pin)
is what the Space installs beside its own Gradio.

ON MODELSCOPE it is a Docker-SDK Studio (§79 HD2, amended in batch 166),
because a Gradio-SDK Studio's gateway answered every websocket upgrade with
an empty 200 and served its own Gradio page at `/` -- measured 2026-10-22,
when no session could open. So it carries a `Dockerfile`, a `.dockerignore`
that lets in exactly the bundle's own files, and `deploy/hosted/bridge.lock`
(Gradio plus the bridge's requirements, hashed) in place of a
requirements.txt nothing there would install. The README has no frontmatter;
the deploy script sets the SDK and hardware through ModelScope's API.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess  # nosec B404 - runs git with a fixed argument list, below
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The Gradio a Hugging Face Space installs for this bundle. Not "latest": a
# version, so a deploy is reproducible and moving it is a commit. What to
# re-measure when it moves: it still has `gr.Server`, and the bundle's
# requirements.txt still resolves beside it on Python 3.12 with the Space's
# own install line, which on 6.30.0 was
#   pip install -r requirements.txt "torch<=2.13.0" gradio[oauth,mcp]==6.30.0
#       "uvicorn>=0.14.0" "websockets>=10.4" spaces==0.51.3
# (`uv pip compile` reproduces it; deploy/hosted/requirements.txt says why
# the harness's own pins are not in that command).
GRADIO_VERSION = "6.30.0"
# ModelScope's image is ours, and app.py imports `spaces`; off ZeroGPU it is
# a no-op. The version a Hugging Face ZeroGPU build installed on 2026-10-20.
SPACES_VERSION = "0.51.3"
PYTHON_VERSION = "3.12"

# Both platforms run x86-64 Linux; the lock is resolved for that, from any
# machine, so a Windows build locks what the Space installs.
LOCK_PLATFORM = "x86_64-manylinux_2_28"

# The image a ModelScope Studio is built FROM, by digest, so a deploy is
# reproducible and moving it is a commit: Python 3.12.15, pulled from Docker
# Hub by ModelScope's own builder on 2026-10-22. What to re-measure when it
# moves: bridge.lock still installs with --only-binary (a wheel for every
# pin on that Python), the session environment still builds, and a session
# still opens through the Studio's gateway.
DOCKER_BASE = ("python:3.12-slim@sha256:"
               "cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2")

PLATFORMS = ("hf", "modelscope")
OVERLAY = ("deploy/",)
SESSION_LOCK = "deploy/hosted/session.lock"
BRIDGE_LOCK = "deploy/hosted/bridge.lock"
# Every name the builder writes on either platform. A repository file with
# one of these names is never copied, so it cannot stand in for the
# generated one.
GENERATED = ("app.py", "requirements.txt", "README.md", "BUILD_INFO.json",
             ".gitattributes", SESSION_LOCK, BRIDGE_LOCK, "Dockerfile",
             ".dockerignore")
# In a ModelScope bundle for the platform or for people, never needed inside
# the image, so the .dockerignore does not let them in.
NOT_IN_IMAGE = frozenset({"Dockerfile", ".dockerignore", ".gitattributes",
                          "README.md"})
# Installed by the platform itself; a pin of any of them in the bundle fails
# the Space's resolve or replaces its ZeroGPU runtime.
PLATFORM_MANAGED = frozenset({"gradio", "spaces", "huggingface-hub"})

TITLE = "Venastine Research Harness"
SHORT_DESCRIPTION = "Agentic research harness with a self-checking pipeline"
# The image a shared link to the Space shows (batch 198, the owner's
# request): Hugging Face's `thumbnail`, which takes a URL. docs/assets is
# not in the bundle, so it is the file on GitHub's `main` -- the owner's
# choice over the build's own commit, which an unpushed or rebased commit
# would break. Until the commit that adds it merges there, the address
# answers 404. The page it is rendered from sits beside it.
COVER = "docs/assets/Venastine_Research_Harness_Cover.png"
THUMBNAIL = ("https://raw.githubusercontent.com/YahyaHammad/"
             "Venastine-Research-Harness/main/" + COVER)

APP_SHIM = '''\
"""The entry point a Hugging Face Space or ModelScope Studio runs.

Generated by scripts/build_demo_bundle.py; the work is deploy/hosted/app.py.
`spaces` comes first because ZeroGPU's rule is that it is imported before
anything that could touch CUDA -- nothing here does, and the rule is cheap.

Gradio's analytics are off unless the operator sets the variable: with them
on, every start called huggingface.co and api.gradio.app (measured
2026-10-22), and nothing a visitor does needs either.
"""

import os

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import spaces  # noqa: E402,F401

from deploy.hosted.app import main  # noqa: E402

if __name__ == "__main__":
    main()
'''

# A ModelScope Studio's image (§79 HD2, HD9). The bridge's own packages come
# from bridge.lock, hashed, wheels only; the session environment is built
# INTO the image, so a container start installs nothing and needs no package
# index (session_env.ensure() finds its marker and returns). It runs as
# root, as both platforms measured: confine.py's drop to `nobody` and
# session_env's /opt rule are written for that. Port 7860 is ModelScope's.
DOCKERFILE = f"""\
# Generated by scripts/build_demo_bundle.py (ROADMAP_v3 §79 HD2, HD9).
FROM {DOCKER_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    GRADIO_SERVER_NAME=0.0.0.0 \\
    GRADIO_SERVER_PORT=7860
WORKDIR /app
COPY {BRIDGE_LOCK} /tmp/bridge.lock
RUN pip install --require-hashes --no-deps --only-binary=:all: \\
        -r /tmp/bridge.lock \\
    && rm /tmp/bridge.lock
COPY . .
RUN python -c "from deploy.hosted import session_env; session_env.ensure()"
EXPOSE 7860
CMD ["python", "app.py"]
"""

# Written rather than inherited: a Space keeps `.gitattributes` across every
# deploy, because huggingface_hub's `delete_patterns` never removes it, so
# without this the file is whatever the first upload left. The first manual
# deploy left the repository's own copy plus LFS lines naming its stray
# `tests/__pycache__` files. Every bundled file is text today; these route a
# future binary through LFS, as the Hub expects.
GITATTRIBUTES = """\
# Generated by scripts/build_demo_bundle.py.
*.png filter=lfs diff=lfs merge=lfs -text
*.jpg filter=lfs diff=lfs merge=lfs -text
*.gif filter=lfs diff=lfs merge=lfs -text
*.ico filter=lfs diff=lfs merge=lfs -text
*.pdf filter=lfs diff=lfs merge=lfs -text
*.zip filter=lfs diff=lfs merge=lfs -text
*.gz filter=lfs diff=lfs merge=lfs -text
"""


class BundleError(Exception):
    """The bundle cannot be built as asked, and nothing was written."""


# ---------------------------------------------------------------------------
# ---- What may go in -------------------------------------------------------
# ---------------------------------------------------------------------------

def load_package(root: Path = ROOT) -> dict:
    return json.loads((root / "package.json").read_text(encoding="utf-8"))


def _segments(path: str) -> list:
    return path.split("/")


# The kinds scripts/prepublish-check.mjs knows, each read as the stricter of
# its plausible meanings, because for a denylist that is the one to keep:
#   * `prefix` matches any path SEGMENT (the docs-consistency gate's
#     reading), not only the path's start;
#   * `exact` matches the value ending a path at a segment boundary, so
#     `providers.json` and `.venastine/mcp.json` are refused at any depth --
#     the other channels mean the repository root, where the harness writes
#     them, but a bundle overlays deploy/, and a providers.json beside the
#     bridge or a project's .venastine/mcp.json under it is the same
#     credential store;
#   * `dir` matches a directory of that name at any depth, for the same
#     reason: deploy/hosted/logs/ is still logs.
# tests/test_demo_bundle.py drives each widening; the real-repository build
# there is what shows none of them refuses a file this repository ships.
_MATCHERS = {
    "exact": lambda v, p: p == v or p.endswith("/" + v),
    "basename": lambda v, p: p == v or p.endswith("/" + v),
    "suffix": lambda v, p: p.endswith(v),
    "prefix": lambda v, p: any(s.startswith(v) for s in _segments(p)),
    "dir": lambda v, p: v in _segments(p)[:-1],
    "segment": lambda v, p: v in _segments(p),
    "suffix-or-segment": lambda v, p: p.endswith(v) or v in _segments(p),
}


def forbidden(pkg: dict) -> list:
    """(label, test) for every `neverDistribute` entry, whatever its channels.

    LOUD on an unknown match kind, as prepublish-check.mjs is: skipping it
    would turn the refusal off for that pattern in silence.
    """
    rules = []
    for entry in pkg.get("neverDistribute") or []:
        kind = entry.get("match")
        test = _MATCHERS.get(kind)
        if test is None:
            raise BundleError(
                f"package.json neverDistribute: unknown match kind {kind!r} "
                f"for {entry.get('label')!r}; teach build_demo_bundle.py "
                f"about it rather than skipping it")
        values = tuple(entry.get("value") or ())
        rules.append((entry.get("label", kind),
                      lambda p, vs=values, t=test: any(t(v, p) for v in vs)))
    if not rules:
        raise BundleError("package.json has no neverDistribute entries, so "
                          "nothing could be refused")
    return rules


def _allowed_by_files(path: str, entries: list) -> bool:
    """package.json `files`, as npm reads the shapes this repository uses:
    `dir/` (everything under it), an exact path, and `!**/name` negations
    that remove a path segment anywhere."""
    included = False
    for entry in entries:
        if entry.startswith("!"):
            name = entry[1:].removeprefix("**/")
            if name in _segments(path):
                return False
            continue
        if entry.endswith("/"):
            included = included or path.startswith(entry)
        else:
            included = included or path == entry
    return included


def candidate_files(root: Path = ROOT, run=subprocess.run) -> list:
    """Tracked files plus untracked ones .gitignore does not exclude."""
    result = run(["git", "ls-files", "-z", "--cached", "--others",  # nosec B603 B607 - fixed arguments, no shell
                  "--exclude-standard"],
                 cwd=root, capture_output=True, check=True)
    names = result.stdout.decode("utf-8").split("\0")
    return sorted({n for n in names if n and (root / n).is_file()})


def select(paths: list, pkg: dict) -> list:
    """The files the bundle copies, or BundleError naming the first path a
    `neverDistribute` rule refuses."""
    entries = list(pkg.get("files") or [])
    if not entries:
        raise BundleError("package.json has no `files` allowlist")
    rules = forbidden(pkg)
    chosen = []
    for path in paths:
        if path in GENERATED:
            continue
        if not (_allowed_by_files(path, entries)
                or any(path.startswith(o) for o in OVERLAY)):
            continue
        for label, test in rules:
            if test(path):
                raise BundleError(
                    f"{path} matches neverDistribute {label!r}; a deployment "
                    f"refuses it rather than leaving it out")
        chosen.append(path)
    if not any(p.startswith("deploy/hosted/") for p in chosen):
        raise BundleError("deploy/hosted/ is missing, so there is nothing to run")
    return chosen


# ---------------------------------------------------------------------------
# ---- What is generated ----------------------------------------------------
# ---------------------------------------------------------------------------

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def requirement_names(text: str) -> list:
    """The distribution names a requirements file declares, normalised."""
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _NAME.match(line)
        if match:
            names.append(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def _pin_line(text: str, name: str) -> str:
    """The one requirement line for `name` in a requirements file."""
    lines = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
    found = [ln for ln in lines
             if ln and requirement_names(ln) == [name]]
    if len(found) != 1:
        raise BundleError(f"requirements.txt has {len(found)} lines for "
                          f"{name}; the bridge copies exactly one")
    return found[0]


def requirements(root: Path, platform: str) -> str:
    """What the BRIDGE needs: on Hugging Face the bundle's requirements.txt,
    installed by the Space beside its own Gradio; on ModelScope the input of
    bridge.lock. The harness's pins are not here -- they are the session
    lock -- except textual's, which the bridge must share with the sessions
    to decode their packets."""
    harness = (root / "requirements.txt").read_text(encoding="utf-8")
    deploy = (root / "deploy" / "hosted" / "requirements.txt").read_text(
        encoding="utf-8")
    managed = sorted(set(requirement_names(deploy)) & PLATFORM_MANAGED)
    if managed:
        raise BundleError(
            f"deploy/hosted/requirements.txt pins {', '.join(managed)}, which "
            f"the platform installs itself; the Space's build would not resolve")
    parts = [
        "# Generated by scripts/build_demo_bundle.py: what the bridge needs.\n"
        "# The harness runs in its own environment, from\n"
        "# deploy/hosted/session.lock -- deploy/hosted/requirements.txt says\n"
        "# why. gradio, spaces and huggingface_hub are absent on purpose: the\n"
        "# platform installs them.\n\n",
        deploy.rstrip("\n") + "\n\n",
        "# The harness's own textual pin: the bridge decodes the sessions'\n"
        "# packets with it, so the two ends are one version.\n",
        _pin_line(harness, "textual") + "\n",
    ]
    if platform == "modelscope":
        parts.append("\n# The image is ours, and app.py imports it.\n"
                     f"spaces=={SPACES_VERSION}\n")
    return "".join(parts)


def _uv() -> list:
    found = shutil.which("uv")
    if found:
        return [found]
    try:
        import uv  # noqa: F401 -- only whether `python -m uv` will work
    except ImportError:
        raise BundleError("the session lock needs uv (pip install uv)") from None
    return [sys.executable, "-m", "uv"]


def compile_lock(requirements_file: Path, run=subprocess.run,
                 source: str = "requirements.txt") -> str:
    """A requirements file resolved for the platforms' Linux and Python,
    with a hash for every file (both locks are installed with
    --require-hashes). `source` says in the header what was locked.

    uv runs FROM the file's own folder, because it writes the file's path,
    relative to where it runs, into its `# via -r` notes: from anywhere else
    the bridge lock, compiled from a temporary file, would carry a path of
    the machine that built it."""
    args = [*_uv(), "pip", "compile", requirements_file.name,
            "--python-version", PYTHON_VERSION,
            "--python-platform", LOCK_PLATFORM,
            "--generate-hashes", "--no-header", "--quiet"]
    result = run(args, capture_output=True, check=False,  # nosec B603 - fixed arguments, no shell
                 cwd=str(requirements_file.parent))
    if result.returncode != 0:
        raise BundleError(f"uv could not lock {source}:\n"
                          + result.stderr.decode("utf-8", "replace")[-3000:])
    text = result.stdout.decode("utf-8")
    if "--hash=sha256:" not in text:
        raise BundleError("uv produced a lock with no hashes")
    return (f"# Generated by scripts/build_demo_bundle.py from {source}:\n"
            f"# Python {PYTHON_VERSION}, {LOCK_PLATFORM}, every file hashed.\n"
            + text)


def bridge_requirements(root: Path) -> str:
    """What bridge.lock is compiled from: Gradio at the version both
    platforms run, and the ModelScope bridge's requirements -- the image
    installs both, since no platform installs anything for it."""
    return (requirements(root, "modelscope")
            + f"\n# The Gradio both platforms run.\ngradio=={GRADIO_VERSION}\n")


# Characters with a meaning in a .dockerignore line (Go's filepath.Match
# plus the `!` and `#` that start one, and the whitespace Docker trims).
_PATTERN_CHARS = re.compile(r"[*?\[\]\\]|^[!#]|^\s|\s$")


def dockerignore(paths: list) -> str:
    """The ModelScope bundle's .dockerignore: everything left out, then
    exactly the bundle's own files let back in.

    AN ALLOWLIST, NOT A DENYLIST, because ModelScope builds the image from a
    git clone of the Studio, and that context carries the clone's `.git` and
    whatever else the platform put there; a denylist would have to know
    every such name in advance. Measured 2026-10-22 with `.git`,
    `providers.json`, a stray file and `__pycache__` planted in the context:
    the image held exactly the bundle's files.

    A path this file cannot name LITERALLY is a refusal: an escaped pattern
    would be one more syntax to get right, and the repository has no such
    name today."""
    lines = [
        "# Generated by scripts/build_demo_bundle.py (ROADMAP_v3 §79 HD9).",
        "# Everything is left out, then exactly the bundle's own files are let",
        "# back in, so nothing else in the build context -- the Studio's .git,",
        "# a stray cache -- reaches the image. The last matching line decides.",
        "*",
    ]
    for path in sorted(paths):
        if path in NOT_IN_IMAGE:
            continue
        if _PATTERN_CHARS.search(path):
            raise BundleError(f"{path!r} cannot be named literally in a "
                              f".dockerignore; rename it")
        lines.append(f"!{path}")
    lines += ["# Never, even inside a directory let back in above.",
              "**/__pycache__",
              "**/*.py[cod]"]
    return "\n".join(lines) + "\n"


def readme(platform: str, pkg: dict, info: dict) -> str:
    if len(SHORT_DESCRIPTION) > 60:
        raise BundleError("SHORT_DESCRIPTION is over the 60 characters a "
                          "Space's README may carry")
    homepage = pkg.get("homepage") or ""
    body = (
        f"# {TITLE}\n\n"
        "An agentic harness with a chat mode and a ten-pass research pipeline "
        "that fact-checks and critiques its own claims, running its terminal "
        "interface in the browser.\n\n"
        "Connect a model with `/connect` once a session starts.\n\n"
        f"Source, documentation and the installable version: {homepage}\n\n"
        f"Built from commit `{info['commit']}`"
        + (" with local changes" if info.get("dirty") else "") + ".\n")
    if platform != "hf":
        return body
    front = (
        "---\n"
        f"title: {TITLE}\n"
        "emoji: \U0001F52C\n"
        "colorFrom: indigo\n"
        "colorTo: purple\n"
        "sdk: gradio\n"
        f"sdk_version: {GRADIO_VERSION}\n"
        f'python_version: "{PYTHON_VERSION}"\n'
        "app_file: app.py\n"
        f"short_description: {SHORT_DESCRIPTION}\n"
        f"thumbnail: {THUMBNAIL}\n"
        f"license: {str(pkg.get('license', 'apache-2.0')).lower()}\n"
        "pinned: false\n"
        "---\n\n")
    return front + body


def build_info(root: Path, platform: str, files: list,
               run=subprocess.run) -> dict:
    """`dirty` is true when the bundle is not exactly the commit: a tracked
    file changed, or a file it copies is untracked (an uncommitted deploy/)."""
    def git(*args) -> str:
        result = run(["git", *args], cwd=root, capture_output=True,  # nosec B603 B607 - fixed arguments, no shell
                     check=True)
        return result.stdout.decode("utf-8").strip()

    untracked = set(git("ls-files", "-z", "--others",
                        "--exclude-standard").split("\0"))
    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))
                 or any(f in untracked for f in files),
        "platform": platform,
        "gradio": GRADIO_VERSION,
        "built_at": datetime.datetime.now(datetime.UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# ---- The build ------------------------------------------------------------
# ---------------------------------------------------------------------------

def build(platform: str, out: Path, *, root: Path = ROOT, force: bool = False,
          run=subprocess.run, lock=compile_lock) -> list:
    """Write the bundle to `out` and return the relative paths written.
    `lock` turns a requirements file into a hashed lock: the harness's into
    the session lock, and on ModelScope the bridge's into bridge.lock."""
    if platform not in PLATFORMS:
        raise BundleError(f"unknown platform {platform!r}; one of {PLATFORMS}")
    out = out.resolve()
    if out == root or root in out.parents:
        raise BundleError(f"{out} is inside the repository, where the next "
                          f"build would pick the last one up")
    if out.exists() and any(out.iterdir()):
        if not force:
            raise BundleError(f"{out} is not empty; pass --force to replace it")
        shutil.rmtree(out)

    pkg = load_package(root)
    files = select(candidate_files(root, run), pkg)
    info = build_info(root, platform, files, run)
    generated = {
        "app.py": APP_SHIM,
        "README.md": readme(platform, pkg, info),
        "BUILD_INFO.json": json.dumps(info, indent=2) + "\n",
        ".gitattributes": GITATTRIBUTES,
        SESSION_LOCK: lock(root / "requirements.txt"),
    }
    if platform == "hf":
        generated["requirements.txt"] = requirements(root, platform)
    else:
        with tempfile.TemporaryDirectory() as scratch:
            source = Path(scratch) / "bridge.in"
            source.write_text(bridge_requirements(root), encoding="utf-8")
            generated[BRIDGE_LOCK] = lock(
                source, source=f"gradio=={GRADIO_VERSION} and the bridge's "
                               f"requirements")
        generated["Dockerfile"] = DOCKERFILE
        generated[".dockerignore"] = dockerignore([*files, *generated])

    out.mkdir(parents=True, exist_ok=True)
    for rel in files:
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, target)
    for rel, text in generated.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text, encoding="utf-8", newline="\n")
    return sorted([*files, *generated])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--force", action="store_true",
                        help="replace a non-empty output folder")
    args = parser.parse_args(argv)
    try:
        written = build(args.platform, args.out, force=args.force)
    except BundleError as exc:
        print(f"build_demo_bundle: {exc}", file=sys.stderr)
        return 1
    print(f"build_demo_bundle: {len(written)} files for {args.platform} "
          f"in {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
