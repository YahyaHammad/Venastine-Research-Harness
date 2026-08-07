"""
tools/builtin/project_docs.py

ROADMAP_v2 §24: the two narrowly-scoped file tools `/init` runs on --
`read_project_doc` (I2) and `write_project_doc` (I1).

WHY NOT `read` AND `write`. Both are `False` in config.ToolPermissions, and
that is not a default anyone can change: is_tool_allowed() reads the
dataclass directly, settings.json has no `permissions` section (and
_KNOWN_SETTINGS *raises* on an unknown key), and D14 forbids a ToolContext
widening anything -- the global check runs first and unconditionally. So
registry.dispatch("write", ...) raises ToolCallDenied BEFORE the
approval_callback is ever consulted, for every user, out of the box.

The two ways out were flipping those globals or scoping new tools. Flipping
`write` hands the model unprompted write access anywhere inside
WORKSPACE_DIR permanently, and flipping `read` hands every agent and all ten
research passes file-read access by default -- a standing posture change to
serve one command. These tools instead do exactly what /init needs and
nothing else, so §24 adds no capability that outlives it.

BOTH TOOLS LIVE IN ONE MODULE because they resolve paths against the same
root. A second copy of that resolver is precisely how the two would drift
apart -- the project's "fix at the producer" rule, applied before the bug.

CONSEQUENCE, NAMED RATHER THAN DISCOVERED: a registered tool with permission
True is advertised to EVERY run, not only to /init's. The initializer agent's
`allowed_tools` narrows that agent; it does not stop plain chat or a research
pass from calling `read_project_doc`. That is why the readable set here is
DOCUMENTATION, not "text files": a doc/manifest allowlist plus a hard deny of
`.venastine/`, `.git/`, `.env*` and `providers.json` means the most a
prompt-injected research pass gains is the project's own README. Widening
this to source files would quietly turn it into `read` with no approval gate.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import config
from core import config_loader, workspace_trust

logger = logging.getLogger(__name__)

# Documentation and manifests. Deliberately NOT a general text-file list --
# see the module docstring's consequence note.
_DOC_EXTENSIONS = frozenset({".md", ".markdown", ".rst", ".txt"})
_DOC_FILENAMES = frozenset({
    "pyproject.toml", "package.json", "cargo.toml", "go.mod",
    "makefile", "dockerfile", "license", "licence", "changelog", "notice",
})

# Denied whatever the extension says. `.venastine/` can hold mcp.json and
# settings.json; providers.json is where API keys live. check_output_policy
# would redact a leaked key from the result, but a backstop is not a reason
# to hand it the chance.
_DENIED_SEGMENTS = frozenset({
    ".git", ".venastine", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache",
})
_DENIED_FILENAMES = frozenset({"providers.json"})

CONTEXT_FILENAME = "CONTEXT.md"


def _project_root() -> Optional[str]:
    """The resolved project path, or None when no session has been
    initialized. None is "there is no project", not "guess one" -- the same
    convention get_agents() and memories/manager.py follow."""
    return config_loader.get_project_path()


def doc_path(project_path: str, name: str) -> str:
    """Where a document in the set lives. DERIVED FROM ITS NAME, never taken
    from a path parameter (I1).

    CONTEXT.md is the one file §14's loader reads, and it reads it from
    `.venastine/` only -- so the hub sits inside the trust boundary while the
    documents it links are ordinary committed root files (I9). That split is
    the reason this function exists instead of a single join.
    """
    if name == CONTEXT_FILENAME:
        return os.path.join(workspace_trust.venastine_dir(project_path), name)
    return os.path.join(project_path, name)


def _resolve_in_project(root: str, user_path: str) -> Optional[str]:
    """Resolve *user_path* under *root* and return it only if it really
    lands inside.

    realpath BEFORE the containment test, so '..' segments and a symlink
    pointing outside the project are both caught -- file_ops' rule, and the
    reason it is a rule: a path that merely looks contained is not.
    """
    resolved = os.path.realpath(os.path.join(root, user_path))
    if resolved != root and not resolved.startswith(root + os.sep):
        return None
    return resolved


def _is_readable_doc(root: str, resolved: str) -> bool:
    rel = os.path.relpath(resolved, root)
    parts = rel.split(os.sep)
    if any(segment in _DENIED_SEGMENTS for segment in parts):
        return False
    name = os.path.basename(resolved).lower()
    if name in _DENIED_FILENAMES or name.startswith(".env"):
        return False
    if name in _DOC_FILENAMES:
        return True
    return os.path.splitext(name)[1] in _DOC_EXTENSIONS


# ===========================================================================
# ---- READ -----------------------------------------------------------------
# ===========================================================================

READ_TOOL_SCHEMA = {
    "name": "read_project_doc",
    "description": (
        "Read one of this project's documentation or manifest files, by the "
        "relative path given in the file listing you were shown. Markdown, "
        "text and manifest files only, and only within the project "
        f"directory. Returns at most {config.INIT_READ_CHARS} characters per "
        "call; use offset to continue through a longer file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the project root, exactly "
                               "as it appears in the file listing.",
            },
            "offset": {
                "type": "integer",
                "description": "Characters to skip before reading. Use the "
                               "value suggested by a truncated response to "
                               "read the next part of a long file.",
            },
        },
        "required": ["path"],
    },
}


def read_run(params: dict) -> dict:
    # params.get throughout, not params[]: no provider validates tool inputs
    # against the schema, and a bare KeyError escapes the loop's
    # ToolCallDenied handler and aborts the whole turn (remember.py's note).
    root = _project_root()
    if root is None:
        return {"error": "read_project_doc: no project directory has been "
                         "resolved, so there is nothing to read."}

    user_path = (params.get("path") or "").strip()
    if not user_path:
        return {"error": "read_project_doc requires 'path'."}

    resolved = _resolve_in_project(root, user_path)
    if resolved is None:
        return {"error": f"read_project_doc: {user_path!r} resolves outside "
                         f"the project directory."}
    if not _is_readable_doc(root, resolved):
        return {"error": f"read_project_doc: {user_path!r} is not a readable "
                         f"project document. Documentation and manifest "
                         f"files only."}
    if not os.path.isfile(resolved):
        return {"error": f"read_project_doc: no such file: {user_path}"}

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        logger.warning("read_project_doc failed for %s: %s", user_path, e)
        return {"error": f"Could not read {user_path}: {e}"}

    offset = params.get("offset") or 0
    if not isinstance(offset, int) or offset < 0:
        offset = 0

    chunk = text[offset:offset + config.INIT_READ_CHARS]
    end = offset + len(chunk)
    result = {"path": user_path, "content": chunk, "truncated": end < len(text)}
    if result["truncated"]:
        # The continuation offset is IN the message, not merely implied by
        # the numbers. A model that has to compute where to resume usually
        # re-reads from zero and spends the budget twice.
        result["message"] = (
            f"Read characters {offset}-{end} of {len(text)}. "
            f"Call again with offset={end} to continue.")
    return result


# ===========================================================================
# ---- WRITE ----------------------------------------------------------------
# ===========================================================================

WRITE_TOOL_SCHEMA = {
    "name": "write_project_doc",
    "description": (
        "Write one of this project's standard documentation files. Takes the "
        "document's NAME, not a path -- the location is derived, so this "
        "cannot write anywhere else. Overwrites the named file, so the caller "
        "is expected to have shown the user what changes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "One of the project's standard document names, "
                               "e.g. CONTEXT.md or ARCHITECTURE.md.",
            },
            "content": {
                "type": "string",
                "description": "Full markdown body of the document.",
            },
        },
        "required": ["name", "content"],
    },
}


def _known_doc_names() -> frozenset:
    """The allowlist IS the confinement (I1), so it is read from the one
    place that defines the sets rather than restated here. Imported lazily:
    tools/registry.py imports this module at import time, and project_init
    imports config, which would close a cycle."""
    from project_init.doc_sets import all_document_names
    return all_document_names()


def approval_notice(params: dict, context) -> str:
    """What the approval prompt shows.

    The gate is only worth having if the prompt says what it is gating
    (remember.py's rule). Here that is the destination and the size --
    the content itself has already been shown as a diff by the caller,
    and repeating several kilobytes of markdown inside a modal would bury
    the one line that says which file is about to change.
    """
    root = _project_root()
    name = params.get("name") or CONTEXT_FILENAME
    where = doc_path(root, name) if root else name
    size = len(params.get("content") or "")
    verb = "Overwrite" if root and os.path.exists(where) else "Create"
    return f"{verb} {where} ({size} characters)."


def write_run(params: dict) -> dict:
    root = _project_root()
    if root is None:
        return {"error": "write_project_doc: no project directory has been "
                         "resolved, so there is nowhere to write."}

    name = (params.get("name") or "").strip()
    known = _known_doc_names()
    if name not in known:
        # Not coerced to CONTEXT.md, and not accepted as a path. An
        # unrecognised name is the one case where this tool could become a
        # general writer, so it fails loudly and lists what it accepts.
        return {"error": f"write_project_doc: {name!r} is not one of this "
                         f"project's standard documents. Expected one of: "
                         f"{', '.join(sorted(known))}."}

    content = params.get("content")
    if not isinstance(content, str) or not content.strip():
        return {"error": "write_project_doc requires non-empty 'content'."}

    encoded = content.encode("utf-8")
    if len(encoded) > config.MAX_FILE_SIZE_BYTES:
        return {"error": f"Content size ({len(encoded)} bytes) exceeds the "
                         f"maximum writable size "
                         f"({config.MAX_FILE_SIZE_BYTES} bytes)."}

    destination = doc_path(root, name)
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        logger.warning("write_project_doc failed for %s: %s", destination, e)
        return {"error": f"Could not write {destination}: {e}"}

    return {"result": f"Wrote {len(content)} characters to {destination}"}
