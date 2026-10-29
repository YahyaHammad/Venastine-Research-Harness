"""
tools/builtin/file_ops.py

Three file-operation tools — read, write, edit — confined to a
configurable workspace directory. Registered as three separate tools
to match config.ToolPermissions' three separate boolean fields (read /
write / edit), so review/audit agents can be given read access without
write or edit.

A fourth, view_image (ROADMAP_v3 §81), reads an image and attaches it
to its own result rather than returning text. It follows read's path
rules exactly, with its own permission row so it can be enabled apart
from read. See view_image_run for where the bytes go.

SECURITY MODEL (ROADMAP §6, deviated per user decision):
  Paths within WORKSPACE_DIR are auto-approved (unless the matching
  ToolApprovals boolean is True, which forces approval even inside the
  workspace). Paths outside WORKSPACE_DIR always require user approval
  — they are NOT hard-rejected, so an operator can grant one-off access
  to files outside the workspace. Symlinks and '..' segments are always
  resolved via os.path.realpath before any check, so a path that looks
  inside the workspace but resolves outside it is treated as outside.

  PROTECTED SEGMENTS (security/protected_paths.py) are the one hard
  deny, and the exception to both rules above: any path -- inside or
  outside the workspace -- that resolves into `.venastine/` is refused
  by the refusal_check before approval and re-tested in all three
  handlers, whatever the ToolApprovals booleans say. Case-folded since
  batch 146, because `realpath` only canonicalises a spelling for a
  path that already EXISTS -- which is not the state a `write` is in.

  WHAT THE DENY RESTS ON, AND THE ONE SHAPE IT CANNOT SEE. It rests on
  `realpath`, so a symlink and a junction into `.venastine/` both arrive
  as the real thing. A HARDLINK does not: it is not a link to anything,
  it is the file, and `realpath` returns the path it was given. Batch 146
  measured a `write` through a hardlinked name putting an mcpServers
  object into a protected `settings.json` whose direct spelling is
  refused. The write half is closed -- `json_store.write_text_atomic`
  replaces the name rather than writing through it, so the link breaks
  and the other name keeps its bytes -- and the READ half is not. A
  `read` through a hardlink into `.venastine/` still serves the file,
  and nothing available here can tell: enumerating a file's other names
  is per-platform, and refusing every multiply-linked file would deny
  ordinary ones. Filed in docs/TECHNICAL_DEBT.md rather than left to be
  found, with the same answer the sandbox reached for a link it cannot
  resolve.

BYTES, NOT TEXT, ON THE WRITE PATHS (batch 146):
  All three used to open with mode "w" and no `newline=`, so Python
  translated every "\n" to os.linesep. On Windows `write` could not
  reproduce a file it had just read, and `edit` replacing one span of an
  LF file rewrote every line ending in it -- 2,910 of them in this
  repository's own AGENTS.md. `edit` now reads with `newline=""` as well,
  so the match runs against the file's real bytes, and retries an
  LF-spelled `old_text` translated when the file is CRLF: the model
  cannot see a file's endings through `read` (which joins with "\n"
  whatever it found), so a span it spells from what it was shown has to
  be able to match.

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

import base64
import logging
import os
from typing import Optional

from pydantic import BaseModel, Field

import config
from json_store import write_text_atomic
from security.protected_paths import hardlinked_segment, protected_segment
from tools.base import invalid_params

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

    TOTAL over any JSON object, which it was not until batch 146: a
    `{"path": null}` is well-formed, so `params.get("path", "")` returns
    None rather than the default -- the key IS present -- and
    `os.path.join(WORKSPACE_ROOT, None)` raised a TypeError out of a gate
    that runs ABOVE dispatch()'s containment. tools/base.py carries the one
    copy of what that costs; shell.py's gate has said "total, like
    everything else the gate calls" for a long time, and these two were the
    exception.

    True for a path it cannot read, because ASKING is the fail-closed
    direction for a predicate that can only tighten. In practice the
    refusal_check answers first and the call never arrives here.
    """
    if not isinstance(params, dict):
        return True
    path = params.get("path", "")
    if not isinstance(path, str):
        return True
    if not _is_within_workspace(path):
        return True
    approvals = config.ToolApprovals()
    return getattr(approvals, tool_name, False)


# ===========================================================================
# ---- Protected segments (.venastine/) -------------------------------------
# ===========================================================================

def _protected_path_error(user_path: str) -> Optional[str]:
    """Why *user_path* must be refused outright, or None.

    Shared by the refusal_check and all three handlers so the decision
    and its wording cannot drift between the pre-approval answer and the
    backstop. resolve_path BEFORE the segment test, so `..` travel and a
    symlink are caught the same way _is_within_workspace catches them.

    Unconditional on workspace membership, like project_docs'
    `_is_readable_doc`: the segment is denied wherever the path lands,
    because `.venastine/` holds mcp.json -- which names commands the
    harness spawns at startup -- and skills/ and agents/ content that is
    injected into system prompts. Workspace membership is the wrong
    question here: with AGENT_WORKSPACE pointed at the project the
    directory is INSIDE the workspace and _file_approval_check would
    auto-approve every call against it.

    A HARDLINK IS A SECOND NAME, NOT A PATH, so resolving cannot reach it
    (TECHNICAL_DEBT 90, batch 160): `hardlinked_segment` asks whether a
    multiply-linked file is the same file as one under the project's or the
    workspace's protected directory, and the answer is refused in the same
    words. Until then a `read` through one served the protected file.
    """
    resolved = resolve_path(user_path)
    segment = protected_segment(resolved)
    via = "the path resolves into"
    if segment is None:
        segment = hardlinked_segment(resolved, _protected_roots())
        via = "the path is a hard link to a file in"
    if segment is None:
        return None
    return (
        f"Refused: {via} '{segment}/', which holds this "
        "harness's own configuration and trust-gated content (settings.json, "
        "mcp.json, skills, agents). It is denied however the file tools are "
        "called."
    )


def _protected_roots() -> list[str]:
    """The directories whose `.venastine/` the file tools could otherwise
    reach under another name: the workspace, and the project when one has
    been initialised (they are the same directory when the workspace was
    named as the project)."""
    from core import config_loader

    roots = [WORKSPACE_ROOT]
    project = config_loader.get_project_path()
    if project and os.path.realpath(project) != WORKSPACE_ROOT:
        roots.append(project)
    return roots


def _refusal_for(tool_name: str):
    """ToolSpec.refusal_check for read / write / edit (§32 A7's shape).

    dispatch() consults this BEFORE the approval gate and returns the
    reason as the result, and core/loop.py suppresses the prompt for a
    refused call -- nobody is asked a question whose answer cannot
    change the outcome. NOT the enforcement (base.py's own words): the
    three handlers re-test below, so a caller that reaches them
    directly is refused too.

    ONE PER TOOL since batch 146, which is what makes it able to answer the
    arguments as well as the path. The module already declared the three
    pydantic models that say what each tool takes; they just ran inside the
    handlers, after this check had been handed the raw dict and had already
    raised. Validating here turns a mistyped parameter into a refusal the
    model can read, which is what the sibling four session tools have always
    done -- `shell_sessions._start_refusal` is this shape, one question over.

    THE ORDER IS THE DENY FIRST, and two existing tests are what settled
    it. Validating the whole model first made `write` with a protected path
    and no `content` answer "content: Field required" -- which is the same
    mistake §32 A7 exists to prevent, one layer over: supplying the content
    would only earn the refusal on the next turn, so a correction that
    cannot change the outcome is not the answer to give. The path's own type
    has to be settled before that, because `_protected_path_error` resolves
    it; everything else the model got wrong is answered after.
    """
    def refusal(params: dict, _context=None) -> Optional[str]:
        model = _PARAM_MODELS[tool_name]
        if isinstance(params, dict) and isinstance(params.get("path"), str):
            refused = _protected_path_error(params["path"])
            if refused is not None:
                return refused
        return invalid_params(tool_name, model, params)
    return refusal


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
    """Apply offset, clamp count, then cut at a LINE BOUNDARY.

    Returns (content, lines_returned, truncated, message), where
    `lines_returned` counts the lines that are IN `content`.

    THE CUT NOW HAPPENS BEFORE THE COUNT (batch 146). It used to be the
    other way round: `lines_returned` was computed from the selection and
    the character cut threw most of it away afterwards, so on any file
    averaging over 100 characters a line the number the model resumes from
    named a line far past the last one delivered. Measured on 800 lines of
    160 characters: 500 reported, 313 delivered, and `offset=500` -- which
    the clamp branch's own message tells the model to use -- skipped 187
    lines and 29,920 characters that no call it could construct would ever
    return.

    Cutting at a line boundary rather than mid-line is what makes every
    byte reachable, since `offset` and `count` are in LINES and the ceiling
    is in characters. The sibling in this scope had already reached the
    other half of this: "the continuation offset is IN the message, not
    merely implied by the numbers. A model that has to compute where to
    resume usually re-reads from zero and spends the budget twice"
    (project_docs.read_run). One resume point, named once, for both
    branches -- the clamp message used to carry its own and they disagreed.
    """
    clamped = count > config.MAX_READ_LINES
    effective_count = min(count, config.MAX_READ_LINES)

    selected = lines[offset:offset + effective_count]

    kept: list[str] = []
    used = 0
    cut_inside_a_line = False
    for line in selected:
        extra = len(line) + (1 if kept else 0)   # the joining newline
        if used + extra > config.MAX_READ_CHARS:
            if not kept:
                # A SINGLE LINE LONGER THAN THE WHOLE CEILING -- minified
                # JavaScript, a one-line JSON document, a packed asset.
                # Keeping whole lines only would deliver NOTHING here and
                # the model would have nowhere to go, so this one line is
                # cut and the message says so: `offset + 1` resumes at the
                # next line, and the rest of this one is not reachable
                # through a tool whose paging unit is a line.
                kept.append(line[:config.MAX_READ_CHARS])
                cut_inside_a_line = True
            break
        kept.append(line)
        used += extra

    content = "\n".join(kept)
    lines_returned = len(kept)
    char_cut = cut_inside_a_line or len(kept) < len(selected)
    truncated = clamped or char_cut

    message_parts: list[str] = []
    if clamped:
        message_parts.append(
            f"Requested {count} lines, returned at most {effective_count} "
            f"(maximum)."
        )
    if char_cut:
        message_parts.append(
            f"Content truncated at {config.MAX_READ_CHARS} characters, on a "
            f"line boundary."
        )
    if cut_inside_a_line:
        message_parts.append(
            f"Line {offset} is itself longer than "
            f"{config.MAX_READ_CHARS} characters and was cut, so the rest of "
            f"it is not reachable by offset."
        )
    if truncated:
        message_parts.append(
            f"Use offset={offset + lines_returned} to read further."
        )

    message = " ".join(message_parts) if message_parts else None
    return content, lines_returned, truncated, message


def read_run(params: dict) -> dict:
    parsed = ReadFileParams(**params)
    refused = _protected_path_error(parsed.path)
    if refused is not None:
        return {"error": refused}
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
            with open(resolved, encoding="utf-8", errors="replace") as f:
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

    # BEFORE anything creates a directory: a refused write must not
    # conjure the protected directory into existence on its way to being
    # refused. The makedirs moved into `write_text_atomic` in batch 146 and
    # the ordering it needs did not move with it, which is why this says
    # what it protects rather than naming a line below.
    refused = _protected_path_error(parsed.path)
    if refused is not None:
        return {"error": refused}

    content_bytes = parsed.content.encode("utf-8")
    if len(content_bytes) > config.MAX_FILE_SIZE_BYTES:
        return {
            "error": (
                f"Content size ({len(content_bytes)} bytes) exceeds the "
                f"maximum writable size ({config.MAX_FILE_SIZE_BYTES} bytes)."
            ),
        }

    resolved = resolve_path(parsed.path)

    try:
        # newline="" and a replace rather than a truncating write -- the
        # module docstring's BYTES section says what each buys, and
        # `write_text_atomic` says why it owns both.
        created = not os.path.exists(resolved)
        write_text_atomic(resolved, parsed.content)
    except Exception as e:
        logger.exception("write tool failed on %s", parsed.path)
        return {"error": f"Could not write file: {e}"}

    if created:
        from core.file_mentions import note_file_created

        note_file_created(resolved)
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
    refused = _protected_path_error(parsed.path)
    if refused is not None:
        return {"error": refused}
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
        # newline="" so `content` holds the file's REAL line endings and the
        # uniqueness contract is checked against the bytes on disk. With
        # universal newlines a CRLF file arrived as LF, and writing that
        # back converted the whole file -- which is the defect this fixes,
        # pointing the other way.
        with open(resolved, encoding="utf-8", newline="") as f:
            content = f.read()
    except Exception as e:
        logger.exception("edit tool read failed on %s", parsed.path)
        return {"error": f"Could not read file: {e}"}

    old_text, new_text = parsed.old_text, parsed.new_text
    match_count = content.count(old_text)
    if match_count == 0 and "\n" in old_text:
        # TRANSLATED AND RETRIED, because the model cannot see what it
        # would have to spell. `read` joins the lines it delivers with
        # "\n" whatever the file held, so a span copied from what the
        # model was shown is LF-spelled even for a CRLF file -- and a
        # strict matcher would refuse every multi-line edit of one, with a
        # message naming text the model has no way to produce. Both sides
        # move together so the replacement carries the file's endings too.
        crlf_old = old_text.replace("\n", "\r\n")
        crlf_count = content.count(crlf_old)
        if crlf_count:
            old_text = crlf_old
            new_text = new_text.replace("\n", "\r\n")
            match_count = crlf_count

    if match_count == 0:
        return {"error": "old_text not found in file."}
    if match_count > 1:
        return {
            "error": (
                f"old_text appears {match_count} times — must be unique. "
                "Include more surrounding context to narrow the match."
            ),
        }

    content = content.replace(old_text, new_text)

    try:
        write_text_atomic(resolved, content)
    except Exception as e:
        logger.exception("edit tool write failed on %s", parsed.path)
        return {"error": f"Could not write file: {e}"}

    return {"result": f"Replaced 1 occurrence in {parsed.path}"}


# ===========================================================================
# ---- VIEW_IMAGE (ROADMAP_v3 §81) ------------------------------------------
# ===========================================================================

#: What both wires §81 delivers to accept as a base64 image: Anthropic's
#: image block and the OpenAI-compatible `image_url` part (IM7). Google, a wire
#: since batch 173, is sent three of them and gets a note for GIF (IM10). Judged from
#: the file's own leading bytes, never its extension -- a PNG renamed `.jpg`
#: is a PNG, and declaring the wrong media type is a request the provider
#: rejects.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _image_media_type(head: bytes) -> Optional[str]:
    """The media type *head* (a file's first bytes) declares, or None."""
    for signature, media_type in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return media_type
    # WEBP is a RIFF container: "RIFF", a four-byte length, then "WEBP".
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class ViewImageParams(BaseModel):
    path: str = Field(..., description="Path to a PNG, JPEG, GIF or WEBP file, relative to the agent's workspace directory, or an absolute path.")


VIEW_IMAGE_TOOL_SCHEMA = {
    "name": "view_image",
    "description": (
        "Look at an image file. The image itself is attached to the result, "
        "so you see the picture rather than a description of it. PNG, JPEG, "
        f"GIF and WEBP, up to {config.MAX_IMAGE_BYTES} bytes. For text and "
        "documents use read."
    ),
    "input_schema": ViewImageParams.model_json_schema(),
}


def view_image_run(params: dict, memory=None, call_id=None,
                   parent_run=None) -> dict:
    """Read an image off disk and attach it to this call's result (IM1).

    The RESULT is text -- it names what was attached -- and the bytes go to
    `memory.attach_image`, which writes them into the same row as that
    result (IM3). They never ride in the returned dict, so the output
    redactor, the tool_result event and both shells' transcripts see only
    the sentence.

    The path rules are `read`'s exactly: the same `_refusal_for` and
    `_file_approval_check`, and the protected segment re-tested here as all
    three file tools re-test it.
    """
    parsed = ViewImageParams(**params)
    refused = _protected_path_error(parsed.path)
    if refused is not None:
        return {"error": refused}
    # THE RUN'S OWN ANSWER (IM6), computed once by the loop and read by the
    # schema list too, so the two cannot disagree. The schema list already
    # left this tool out when it is False; this is the backstop for a call
    # made anyway, by a model naming a tool it was not offered.
    if parent_run is None or not getattr(parent_run, "images", False):
        return {"error": (
            "view_image is not available in this run: either the provider "
            "is not marked `accepts_images` in providers.json, or this is a "
            "research pass, which does not take images."
        )}
    if memory is None or call_id is None:
        return {"error": "view_image has no conversation to attach the image to."}

    resolved = resolve_path(parsed.path)
    if not os.path.exists(resolved):
        return {"error": f"No such file or directory: {parsed.path}"}
    if not os.path.isfile(resolved):
        return {"error": f"Not a regular file: {parsed.path}"}
    file_size = os.path.getsize(resolved)
    if file_size > config.MAX_IMAGE_BYTES:
        return {"error": (
            f"Image size ({file_size} bytes) exceeds the maximum attachable "
            f"size ({config.MAX_IMAGE_BYTES} bytes)."
        )}
    try:
        # One byte past the cap, so a file that grew after the size check
        # is refused rather than read without a bound.
        with open(resolved, "rb") as f:
            raw = f.read(config.MAX_IMAGE_BYTES + 1)
    except OSError as e:
        logger.exception("view_image failed on %s", parsed.path)
        return {"error": f"Could not read file: {e}"}
    if len(raw) > config.MAX_IMAGE_BYTES:
        return {"error": (
            f"Image exceeds the maximum attachable size "
            f"({config.MAX_IMAGE_BYTES} bytes)."
        )}

    media_type = _image_media_type(raw[:12])
    if media_type is None:
        return {"error": (
            f"{parsed.path} is not a PNG, JPEG, GIF or WEBP image (judged by "
            f"its contents, not its name)."
        )}
    memory.attach_image(call_id, media_type,
                        base64.b64encode(raw).decode("ascii"))
    return {"content": f"Attached {parsed.path} ({media_type}, {len(raw)} bytes)."}


# ===========================================================================
# ---- What each tool takes -------------------------------------------------
# ===========================================================================

#: Read by `_refusal_for`, so the pre-approval check validates against the
#: same model the handler does. At the BOTTOM because all three classes have
#: to exist first, and looked up inside the closure rather than captured, so
#: there is no import-order question to get wrong.
_PARAM_MODELS = {
    "read": ReadFileParams,
    "write": WriteFileParams,
    "edit": EditFileParams,
    "view_image": ViewImageParams,
}
