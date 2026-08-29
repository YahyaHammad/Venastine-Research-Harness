"""
tools/builtin/file_ops.py

Three file-operation tools — read, write, edit — confined to a
configurable workspace directory. Registered as three separate tools
to match config.ToolPermissions' three separate boolean fields (read /
write / edit), so review/audit agents can be given read access without
write or edit.

SECURITY MODEL (ROADMAP §6, deviated per user decision):
  Paths within WORKSPACE_DIR are auto-approved (unless the matching
  ToolApprovals boolean is True, which forces approval even inside the
  workspace). Paths outside WORKSPACE_DIR always require user approval
  — they are NOT hard-rejected, so an operator can grant one-off access
  to files outside the workspace. Symlinks and '..' segments are always
  resolved via os.path.realpath before any check, so a path that looks
  inside the workspace but resolves outside it is treated as outside.

READ TOOL PAGINATION (deviated from ROADMAP §6):
  The read tool accepts offset (lines to skip) and count (lines to
  read). count is clamped to config.MAX_READ_LINES with a note in the
  response (not a hard error). Output is also truncated at
  config.MAX_READ_CHARS with a note. Files larger than
  config.MAX_FILE_SIZE_BYTES are rejected before opening to prevent
  memory exhaustion (especially on the markitdown path, which loads
  the whole file).

RICH FORMAT SUPPORT (deviated from ROADMAP §6):
  For binary/rich extensions (.pdf, .docx, .pptx, etc.) the read tool
  uses markitdown to convert to markdown before applying line/char
  truncation. Plain-text extensions use open() directly.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from pydantic import BaseModel, Field

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ---- Path helpers ---------------------------------------------------------
# ---------------------------------------------------------------------------

WORKSPACE_ROOT = os.path.realpath(config.WORKSPACE_DIR)

_RICH_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".pptx", ".xlsx", ".xls",
    ".html", ".htm", ".epub", ".ipynb",
})


def resolve_path(user_path: str) -> str:
    """Resolve *user_path* relative to WORKSPACE_ROOT via realpath.

    Handles ``..`` segments and symlinks. Does NOT reject paths that
    land outside the workspace — that decision belongs to the approval
    layer (``_file_approval_check``), not to path resolution.

    PUBLIC since batch 41 (X4). The TUI has to read the file a `write`
    or an `edit` is about to change in order to show the diff, and it
    must land on the same file the tool will — a second
    ``os.path.join(WORKSPACE_ROOT, ...)`` at the call site is this
    project's canonical producer/consumer bug, and here it would mean a
    diff of one file beside an edit to another.
    """
    return os.path.realpath(os.path.join(WORKSPACE_ROOT, user_path))


def _is_within_workspace(user_path: str) -> bool:
    resolved = resolve_path(user_path)
    return resolved == WORKSPACE_ROOT or resolved.startswith(WORKSPACE_ROOT + os.sep)


def _file_approval_check(tool_name: str, params: dict) -> bool:
    """Path-dependent approval policy for read / write / edit.

    Outside workspace → always True (ask user).
    Within workspace  → defer to the matching ToolApprovals boolean
                        (False = auto-approved, True = still ask).
    """
    path = params.get("path", "")
    if not _is_within_workspace(path):
        return True
    approvals = config.ToolApprovals()
    return getattr(approvals, tool_name, False)


# ===========================================================================
# ---- READ -----------------------------------------------------------------
# ===========================================================================

class ReadFileParams(BaseModel):
    path: str = Field(..., description="Path relative to the agent's workspace directory, or an absolute path.")
    offset: int = Field(0, ge=0, description="Number of lines to skip before reading (0-based).")
    count: int = Field(
        config.MAX_READ_LINES,
        ge=1,
        description=f"Number of lines to read. Clamped to {config.MAX_READ_LINES} if higher.",
    )


READ_TOOL_SCHEMA = {
    "name": "read",
    "description": (
        "Read the contents of a file. For rich formats (PDF, DOCX, PPTX, "
        "XLSX, HTML, etc.) the content is converted to markdown first. "
        "Use offset and count to page through large files. Output is "
        f"limited to {config.MAX_READ_LINES} lines or {config.MAX_READ_CHARS} "
        "characters, whichever comes first."
    ),
    "input_schema": ReadFileParams.model_json_schema(),
}


def _read_rich(resolved_path: str) -> str:
    """Convert a rich-format file to markdown via markitdown.

    markitdown is an OPTIONAL extra since #144: this function is its only
    consumer, it sits behind `read`, and `read` is globally denied and
    cannot be re-enabled at runtime -- so a default install carries neither
    it nor its eleven transitive distributions. Anyone who reaches here has
    enabled `read` in config.py, and the bare ModuleNotFoundError would name
    a package they never asked for and cannot place.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as e:
        raise RuntimeError(
            f"Reading {os.path.basename(resolved_path)} needs the optional "
            f"`markitdown` dependency, which a default install does not "
            f"carry (it serves only this path, behind the globally-denied "
            f"`read` tool). Install it with: pip install -e \".[documents]\""
        ) from e
    md = MarkItDown()
    result = md.convert(resolved_path)
    return result.text_content


def _truncate_lines(
    lines: list[str],
    offset: int,
    count: int,
) -> tuple[str, int, bool, Optional[str]]:
    """Apply offset, clamp count, join, then char-truncate.

    Returns (content, lines_returned, truncated, message).
    """
    clamped = count > config.MAX_READ_LINES
    effective_count = min(count, config.MAX_READ_LINES)

    selected = lines[offset:offset + effective_count]
    lines_returned = len(selected)
    content = "\n".join(selected)

    truncated = False
    message_parts: list[str] = []

    if clamped:
        truncated = True
        message_parts.append(
            f"Requested {count} lines, returned {effective_count} (maximum). "
            f"Use offset={offset + effective_count} to read further."
        )

    if len(content) > config.MAX_READ_CHARS:
        truncated = True
        content = content[:config.MAX_READ_CHARS]
        message_parts.append(
            f"Content truncated at {config.MAX_READ_CHARS} characters."
        )

    message = " ".join(message_parts) if message_parts else None
    return content, lines_returned, truncated, message


def read_run(params: dict) -> dict:
    parsed = ReadFileParams(**params)
    resolved = resolve_path(parsed.path)

    if not os.path.exists(resolved):
        return {"error": f"No such file or directory: {parsed.path}"}
    if not os.path.isfile(resolved):
        return {"error": f"Not a regular file: {parsed.path}"}

    file_size = os.path.getsize(resolved)
    if file_size > config.MAX_FILE_SIZE_BYTES:
        return {
            "error": (
                f"File size ({file_size} bytes) exceeds the maximum "
                f"readable size ({config.MAX_FILE_SIZE_BYTES} bytes)."
            ),
        }

    ext = os.path.splitext(resolved)[1].lower()

    try:
        if ext in _RICH_EXTENSIONS:
            raw_text = _read_rich(resolved)
            all_lines = raw_text.splitlines()
        else:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.read().splitlines()
    except Exception as e:
        logger.exception("read tool failed on %s", parsed.path)
        return {"error": f"Could not read file: {e}"}

    content, lines_returned, truncated, message = _truncate_lines(
        all_lines, parsed.offset, parsed.count,
    )

    result: dict = {
        "content": content,
        "lines_returned": lines_returned,
        "offset": parsed.offset,
        "truncated": truncated,
    }
    if message:
        result["message"] = message
    return result


# ===========================================================================
# ---- WRITE ----------------------------------------------------------------
# ===========================================================================

class WriteFileParams(BaseModel):
    path: str = Field(..., description="Path relative to the agent's workspace directory, or an absolute path.")
    content: str = Field(..., description="Full content to write — overwrites any existing file.")


WRITE_TOOL_SCHEMA = {
    "name": "write",
    "description": (
        "Create or overwrite a file. Parent directories are created "
        "automatically. Content size is limited to "
        f"{config.MAX_FILE_SIZE_BYTES} bytes."
    ),
    "input_schema": WriteFileParams.model_json_schema(),
}


def write_run(params: dict) -> dict:
    parsed = WriteFileParams(**params)

    content_bytes = parsed.content.encode("utf-8")
    if len(content_bytes) > config.MAX_FILE_SIZE_BYTES:
        return {
            "error": (
                f"Content size ({len(content_bytes)} bytes) exceeds the "
                f"maximum writable size ({config.MAX_FILE_SIZE_BYTES} bytes)."
            ),
        }

    resolved = resolve_path(parsed.path)
    os.makedirs(os.path.dirname(resolved), exist_ok=True)

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(parsed.content)
    except Exception as e:
        logger.exception("write tool failed on %s", parsed.path)
        return {"error": f"Could not write file: {e}"}

    return {"result": f"Wrote {len(parsed.content)} characters to {parsed.path}"}


# ===========================================================================
# ---- EDIT -----------------------------------------------------------------
# ===========================================================================

class EditFileParams(BaseModel):
    path: str = Field(..., description="Path relative to the agent's workspace directory, or an absolute path.")
    old_text: str = Field(..., description="Exact text to replace — must appear EXACTLY ONCE in the file.")
    new_text: str = Field(..., description="Replacement text.")


EDIT_TOOL_SCHEMA = {
    "name": "edit",
    "description": (
        "Replace an exact, uniquely-matching span of text within an "
        "existing file. old_text must appear exactly once; if it appears "
        "zero or more than one time the edit is rejected with an error."
    ),
    "input_schema": EditFileParams.model_json_schema(),
}


def edit_run(params: dict) -> dict:
    parsed = EditFileParams(**params)
    resolved = resolve_path(parsed.path)

    if not os.path.isfile(resolved):
        return {"error": f"No such file: {parsed.path}"}

    # ROADMAP_v2 §31 (H7), #55. read_run has had this check since the
    # beginning and this module's own docstring gives the reason --
    # "rejected before opening to prevent memory exhaustion" -- while
    # the sibling twenty lines down read whatever it was pointed at.
    # Measured on a 30 MB file: 60 MB of peak traced memory to answer
    # "old_text not found in file."
    #
    # Copied rather than reinvented, down to the wording, so the two
    # paths refuse identically. A caller that learns what `read` rejects
    # now knows what `edit` rejects.
    file_size = os.path.getsize(resolved)
    if file_size > config.MAX_FILE_SIZE_BYTES:
        return {
            "error": (
                f"File size ({file_size} bytes) exceeds the maximum "
                f"editable size ({config.MAX_FILE_SIZE_BYTES} bytes)."
            ),
        }

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.exception("edit tool read failed on %s", parsed.path)
        return {"error": f"Could not read file: {e}"}

    match_count = content.count(parsed.old_text)
    if match_count == 0:
        return {"error": "old_text not found in file."}
    if match_count > 1:
        return {
            "error": (
                f"old_text appears {match_count} times — must be unique. "
                "Include more surrounding context to narrow the match."
            ),
        }

    content = content.replace(parsed.old_text, parsed.new_text)

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        logger.exception("edit tool write failed on %s", parsed.path)
        return {"error": f"Could not write file: {e}"}

    return {"result": f"Replaced 1 occurrence in {parsed.path}"}
