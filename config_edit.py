"""config_edit.py -- the catalogue behind `/config`, and the writer under it.

WHY THIS IS NOT IN `tui/`. AGENTS.md's rule for §16 is that the TUI is a
SHELL rather than a feature home: anything implemented there is invisible to
the CLI and to the research pipeline, which is why the question tool, the
todo list, `/init` and session summaries are all specified outside it. The
catalogue, the value grammar and the writer are the feature; `/config` is one
surface over them, and a command-line surface can be added later without
moving a line.

WHY IT IS NOT IN `config_schema.py` EITHER. That module is the READER, and it
carries a stated rule that it imports nothing first-party -- `config.py` and
`core/config_loader.py` both sit on top of it, and an edge from there into
`core/` puts the cycle back by a longer route. This module needs that edge:
answering "what outranks this key" means reading `core/config_loader`'s
settings tables. Nothing imports this module except `tui/` and `main.py`, so
the cycle cannot form from this end either.

FOUR RULES, all of them load-bearing.

1. A PROPOSED CHANGE IS VALIDATED AS THE BYTES IT WILL BECOME. `propose()`
   applies the edit to a round-trip tree, dumps it to text, re-parses THAT
   TEXT with `config_schema`'s own safe reader, and validates the result. Not
   the in-memory tree: the round-trip parser hands back its own scalar types,
   and what the next launch reads is bytes. Validating anything else would be
   certifying a document nobody will ever load.

2. THE WRITE PRESERVES EVERYTHING IT DID NOT CHANGE. `config.yaml` is 845
   lines, of which roughly 600 are the comments that say what each value does
   and which keys are AUTHORITY. A dump through `typ="safe"` would erase all
   of them, so the round-trip parser is configured here to reproduce this
   file exactly -- measured, byte for byte -- and a one-value change comes
   out as a one-line diff.

3. NOTHING HERE APPLIES A CHANGE TO A RUNNING PROCESS. `config.py` binds at
   import and `security/posture.py` depends on that (UN1); `tools/builtin/
   shell.py` validates the approval mode at ITS import. A reload would leave
   the frozen posture disagreeing with the file it came from. So the writer
   writes, and `RestartRequest` is how the change reaches a process that will
   read it.

4. THE AUTHORITY KEYS ARE MARKED, NOT BLOCKED (batch 84). `config_schema.
   HARNESS_AUTHORITY_KEYS` used to mean "no in-session surface may write
   this". It now means "no UNATTENDED surface may": a settings file still
   cannot reach these keys, nor an environment variable, nor a tool call.
   A person typing at the prompt can, behind a confirmation that names what
   the key permits.

   STATE THE FILE-TOOL HALF ACCURATELY -- this said "the file tools refuse
   the harness install tree" for three batches and they do not. Measured:
   `file_ops._protected_path_error` denies a path carrying a `.venastine`
   SEGMENT and nothing else, so `<install>/config.yaml` comes back allowed.
   What actually holds is weaker and still real: `write` and `edit` ship
   DENIED in `tool_permissions` and cannot be enabled at runtime, and
   `security/protected_paths` makes a workspace overlapping the harness tree
   a startup error -- so the install tree is never inside the workspace and
   a write there is approval-gated rather than auto-approved. That module's
   own docstring is why the wording matters: a half-understood control is
   worse than none.
   `AUTHORITY_EFFECT` below is that sentence, per key, and it is the reason
   the gate is not a generic "are you sure".
"""

from __future__ import annotations

import dataclasses
import io
import os
import tempfile
import types
import typing
from typing import Any, Optional

from ruamel.yaml import YAML

import config_schema

# The two nested tables, whose leaves are addressed `<table>.<tool>`. Named
# once: the catalogue skips them as rows of their own and expands them
# instead, and every other reference derives from this.
TABLES = ("tool_permissions", "tool_approvals")

#: What each authority key actually permits, in one sentence, for the
#: confirmation that gates it. NOT a generic "are you sure" -- the whole
#: argument for letting a slash command write these is that the person is
#: told what they are turning on, so a missing entry here is a missing gate.
#: `tests/test_config_edit.py` holds this against HARNESS_AUTHORITY_KEYS.
AUTHORITY_EFFECT = {
    "shell_approval_mode": (
        "how often a shell command stops to ask you. `never` runs every "
        "command the agent writes without asking, including in a session "
        "where untrusted content is already in the context window."),
    "allow_insecure_sandbox_fallback": (
        "whether a shell command may run WITHOUT the container when Docker "
        "is unavailable -- on the host, with your files."),
    "auto_approve_sandbox_fallback": (
        "whether that fallback to the host happens without asking you "
        "first."),
    "redact_tool_outputs": (
        "whether secrets are stripped from tool output before it reaches "
        "the model and the transcript."),
    "ensemble_models": (
        "which providers N research passes are sent to. Every entry is a "
        "provider that receives your research content."),
    "critic_model": (
        "which provider the grounding and critic passes are sent to. It "
        "receives the claims and the sources."),
    "embedder_model": (
        "which provider claim and source text is sent to for similarity "
        "scoring."),
    "tool_permissions": (
        "whether a tool may be called at all. This is the global floor -- "
        "no agent, skill or approval can widen it (D14)."),
    "tool_approvals": (
        "whether a tool stops to ask you before it runs. Turning one off "
        "removes a prompt you would otherwise see."),
}

#: config.yaml key -> the settings.json path that outranks it. Declared here
#: rather than derived, because only one of the three sources IS a table:
#: `_COMPACTION_DEFAULTS` maps settings key to field name and is inverted
#: below, while the confidence and source-scoring defaults are hand-written
#: return dicts and the top-level three are resolved in `main.py`. A test
#: holds every name here against the schema and against the loader's known
#: keys, so an added settings key that silently stops matching shows up as a
#: failure rather than as a `/config` that quietly reports the wrong winner.
_SETTINGS_OVERRIDES = {
    "model_name": "default_model",
    "default_effort": "effort",
    "ensemble_mode": "ensemble_mode",
    "authority_adjustment_cap": "source_scoring.authority_adjustment_cap",
    "scholar_venue_weight": "source_scoring.venue_weight",
    "scholar_citation_weight": "source_scoring.citation_weight",
    "scholar_author_weight": "source_scoring.author_weight",
    "scholar_h_saturation": "source_scoring.h_saturation",
    "scholar_min_cohort_size": "source_scoring.min_cohort_size",
    "scholar_min_citation_age_days": "source_scoring.min_citation_age_days",
    # main.py:resolve_review states the precedence in so many words --
    # `research.subagent_review > config.SUBAGENT_REVIEW` -- and this table
    # was missing it, so `/config subagent_review` reported nothing
    # outranking a key a settings file silently wins.
    "subagent_review": "research.subagent_review",
}


def settings_overrides() -> dict:
    """`{config.yaml key: settings.json path}`, compaction included.

    The seven compaction keys come from `core/config_loader`'s own table
    rather than being retyped, which is the same anti-drift move that table
    was created for: it maps settings key to schema field, so inverting it
    is the whole mapping with no second copy to go stale.
    """
    from core.config_loader import _COMPACTION_DEFAULTS

    merged = dict(_SETTINGS_OVERRIDES)
    for key, field in _COMPACTION_DEFAULTS.items():
        merged[field] = f"compaction.{key}"
    return merged


@dataclasses.dataclass(frozen=True)
class KeyRow:
    """One addressable name in `config.yaml`, as `/config` shows it.

    `name` is what the user types: a top-level key, or `<table>.<tool>` for
    the 46 leaves of the two permission tables. Those 23 tool names appear in
    BOTH tables, so the prefix is not decoration -- it is the only thing that
    tells `tool_permissions.shell` from `tool_approvals.shell`.

    TWO VALUES, NOT ONE, and keeping them apart is batch 85's whole subject.
    `in_file` is what `config.yaml` says: the thing `/config` edits and the
    thing the next launch will read. `in_session` is what THIS process is
    running, bound at import and never re-read. They start equal for every
    key without an environment variable, and a write here moves the first
    without moving the second.

    One field for both was the defect. `/config` reported the SESSION's
    value everywhere -- the panel's `now`, the `(was ...)` on a write, the
    authority modal -- while writing against the FILE, so a second change to
    the same key in one session reported the value it had at launch as the
    one being replaced. The file was always right; only the reporting drifted,
    and only once someone did what `/config` is for.
    """

    name: str
    kind: str          # "scalar" | "pair" | "container"
    values: str        # the possible values, as a person reads them
    in_file: Any       # what config.yaml says right now
    in_session: Any    # what this process is running
    authority: bool
    #: The environment variable that beats this key, `$`-prefixed, if any.
    #: STRUCTURED rather than folded into one display string: `pending` and
    #: `explain` both have to READ the variable's name, and a key can be
    #: outranked by a variable AND a settings.json key at once -- `model_name`
    #: is. One field for both meant whichever was found first was the only
    #: one ever reported, and the one that lost was `settings.json
    #: default_model`: the tier that arrives with a directory you cloned.
    environment_variable: Optional[str] = None
    #: The `settings.json` path that beats this key, if any.
    settings_key: Optional[str] = None

    @property
    def outranked_by(self) -> Optional[str]:
        """Every source that beats this key, for display. None if none do."""
        sources = []
        if self.environment_variable:
            sources.append(f"${self.environment_variable}")
        if self.settings_key:
            sources.append(f"settings.json {self.settings_key}")
        return " and ".join(sources) if sources else None

    @property
    def settable(self) -> bool:
        return self.kind in ("scalar", "pair")

    @property
    def pending(self) -> bool:
        """Written, and waiting for a restart to take effect.

        NOT simply "the two differ". For the five keys with an environment
        variable the two differ from launch with nothing pending at all --
        the variable won when the file was read and will win again next
        time -- so a difference there says nothing about whether anyone
        changed anything. `outranked_by` already names the variable; this
        asks whether it is actually set.

        Every other key had the two equal at launch by construction, so a
        difference IS a change made since. That is why no snapshot of the
        file is taken at startup: there is nothing a snapshot would know
        that this does not.

        A CONTAINER IS NEVER PENDING. The document hands back the list or
        dict YAML spells where the model holds the frozenset or tuple its
        consumers rely on, so the two differ for every one of the sixteen,
        always -- and nothing can write one from here anyway, so there is
        nothing for such a row to be pending about. Without this line every
        container row read `pending restart` from launch.
        """
        if not self.settable:
            return False
        if self.in_file == self.in_session:
            return False
        if self.environment_variable:
            return os.environ.get(self.environment_variable) is None
        return True

    @property
    def summary(self) -> str:
        """The one-line description the suggestion panel draws.

        Values first, current second: the panel gives an entry two rendered
        rows and DROPS the rest, so the order is what survives a narrow
        terminal. What someone browsing needs is what they are allowed to
        type; the value in the file is the useful second half.

        THE FILE'S VALUE, because this panel is an editor and the file is
        what it edits. `pending` is how the running process gets its say in
        the one word that fits; the sentence naming what is still in force
        belongs in `explain()`, which has a whole transcript to write into
        instead of half a row. The last two parts are terse for the same
        reason.
        """
        parts = [self.values, f"now {shown(self.in_file)}"]
        if self.pending:
            parts.append("pending restart")
        if self.outranked_by:
            parts.append(self.outranked_by)
        if self.authority:
            parts.append("authority")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# ---- Reading and writing the document -------------------------------------
# ---------------------------------------------------------------------------

def _round_trip() -> YAML:
    """The comment-preserving parser, configured to reproduce THIS file.

    Every setting here was measured against `config.yaml` rather than
    chosen: with the four below, a load-then-dump reproduces all 28,177
    bytes exactly, and without any one of them it does not.

      * `preserve_quotes` -- a quoted scalar stays quoted.
      * `indent(sequence=4, offset=2)` -- ruamel's default un-indents a
        list under its key, which moved every entry of the six list-valued
        keys two columns left.
      * the `null` representer -- ruamel writes a None as an EMPTY value,
        so `ensemble_models: null` became `ensemble_models:` and four other
        keys with it. Empty parses back to None, so this is cosmetic, but a
        write that silently reformats five lines it was not asked to touch
        is a write nobody can review.
      * `width` -- the default wraps long scalars at 80 columns, which
        would reflow the URL and the docker image tag.

    `typ="safe"` stays the READ path in `config_schema`, deliberately: that
    is the one that runs at every startup, and safe mode refuses
    `!!python/...` tags outright. This parser exists for the write path
    only, which is what `config_schema._reader` says it is for.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.representer.add_representer(
        type(None),
        lambda representer, data: representer.represent_scalar(
            "tag:yaml.org,2002:null", "null"))
    return yaml


def read_text() -> tuple[str, str]:
    """`(text, newline)` for the live document.

    The newline is carried out and back rather than normalised away. This
    repo is developed on Windows with `core.autocrlf=true`, so a worktree
    copy of a tracked file can be CRLF even though the index is LF and this
    particular file is LF today. ruamel emits `\\n` whatever it read, so a
    write that did not put the convention back would rewrite all 845 lines
    of a CRLF checkout to report a one-line change.
    """
    with open(config_schema.CONFIG_PATH, encoding="utf-8",
                 newline="") as handle:
        raw = handle.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


#: `((path, mtime_ns, size), tree)` for the last document parsed, or None.
_document_cache = None


def document(fresh: bool = False):
    """The live `config.yaml` as a round-trip tree, comments attached.

    CACHED ON THE FILE'S OWN STAMP, because `catalogue()` reads this on
    every keystroke the suggestion panel sees and parsing 845 lines costs
    about 70ms against the 0.7ms everything else in that path costs. A
    `stat` is microseconds, and the document changes about as often as a
    person types a `/config` command.

    `write()` clears the cache outright rather than trusting the stamp, and
    that is not belt-and-braces: `max_tokens: 16000` and `max_tokens: 18000`
    are the SAME SIZE, so mtime would be carrying the whole comparison, and
    two writes inside one filesystem timestamp tick would be invisible. The
    stamp stays for the case `write()` cannot see -- someone editing the
    file in another window while the harness is up.

    `fresh=True` skips it entirely, for a caller that is about to write.
    """
    global _document_cache
    path = config_schema.CONFIG_PATH
    stamp = None
    if not fresh:
        try:
            status = os.stat(path)
            stamp = (path, status.st_mtime_ns, status.st_size)
        except OSError:
            # Unreadable is `read_text`'s error to raise, with its message.
            stamp = None
        if stamp is not None and _document_cache is not None:
            cached_stamp, tree = _document_cache
            if cached_stamp == stamp:
                return tree

    text, _ = read_text()
    tree = _round_trip().load(io.StringIO(text))
    if stamp is not None:
        _document_cache = (stamp, tree)
    return tree


def forget_document() -> None:
    """Drop the cached parse. Called by `write()`, and by tests."""
    global _document_cache
    _document_cache = None


def _dump(tree) -> str:
    stream = io.StringIO()
    _round_trip().dump(tree, stream)
    return stream.getvalue()


def get_leaf(tree, name: str):
    """Read a dotted name off a round-trip tree."""
    if "." in name:
        table, leaf = name.split(".", 1)
        return tree[table][leaf]
    return tree[name]


def place(tree, name: str, value: Any) -> None:
    """Set a dotted name, keeping the prose that FOLLOWS it. Rule 2's teeth.

    `tree[name] = value` loses comments, in two different ways, and both
    were found by running it rather than by reading ruamel. Each costs the
    user documentation out of their own file, silently, on a write that
    reported itself as a one-line change.

    ONE: THE TEXT AFTER A BLOCK SEQUENCE IS ANCHORED TO ITS LAST INDEX.
    `node.ca.items[5]` on a six-entry list, not the key in the parent. So
    replacing `models_rejecting_sampling_params` took the
    `# Critic and embedder routing` banner and the AUTHORITY note under it
    with it -- six lines. Mutating the node in place does not help; measured.

    TWO: A VALUE THAT CHANGES SHAPE MOVES ITS PARENT'S COMMENT BETWEEN TWO
    SLOTS. For a scalar the following text sits in `ca.items[name][2]`; when
    the value becomes a block mapping ruamel moves it to slot 3 and emits it
    after the block. Going back -- `/config critic_model off` after setting
    a provider -- it writes a scalar again and slot 3 is never emitted, so
    `embedder_model`'s three-line AUTHORITY comment was deleted from the
    file. ruamel handles scalar -> block by itself; this handles the return
    trip, and the leading newline is what makes the result byte-identical to
    the document before the round trip rather than merely comment-complete.
    """
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.tokens import CommentToken

    parent, key = tree, name
    if "." in name:
        table, key = name.split(".", 1)
        parent = tree[table]
    was = parent.get(key)

    if isinstance(value, (list, dict)):
        new = (CommentedMap(value) if isinstance(value, dict)
               else CommentedSeq(value))
        anchors = getattr(getattr(was, "ca", None), "items", None)
        if anchors and len(new):
            old_keys = (list(was.keys()) if isinstance(was, dict)
                        else list(range(len(was))))
            new_keys = (list(new.keys()) if isinstance(new, dict)
                        else list(range(len(new))))
            if old_keys and old_keys[-1] in anchors:
                new.ca.items[new_keys[-1]] = anchors[old_keys[-1]]
        parent[key] = new
        return

    parent[key] = value
    if not isinstance(was, (list, dict)):
        return
    slot = getattr(getattr(parent, "ca", None), "items", {}).get(key)
    if slot and slot[3]:
        slot[2] = CommentToken("\n" + "".join(tok.value for tok in slot[3]),
                               slot[3][0].start_mark)
        slot[3] = None


def write(text: str, newline: str = "\n") -> None:
    """Replace `config.yaml` with `text`, atomically. Raises OSError.

    Temp file plus `os.replace`, which is `json_store.write_json_atomic`'s
    reasoning applied to the one file in this project that is not JSON: a
    truncate-then-write leaves the document empty for the length of the
    write and permanently damaged if the process dies inside it, and THIS
    document is the one without which nothing starts at all.

    Not that function itself, which serialises JSON and would have to grow
    a "or just write these bytes" mode to be shared.
    """
    path = config_schema.CONFIG_PATH
    # A UNIQUE NAME, not a fixed `config.yaml.tmp` sibling. Two writers --
    # a `/config` write and the launcher's merge in another process -- raced
    # on the one name, and a failed `os.replace` (a read-only file on
    # Windows, where opening the temp still succeeds) left the leftover in
    # the install tree forever. `_validate_candidate` below already does it
    # this way.
    handle_fd, tmp = tempfile.mkstemp(prefix="config.", suffix=".writing.yaml",
                                      dir=os.path.dirname(path))
    try:
        with open(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text.replace("\n", newline))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # See `document()`: a one-value change can leave the size identical, so
    # the stamp alone would not always notice our own write.
    forget_document()


# ---------------------------------------------------------------------------
# ---- Describing a key -----------------------------------------------------
# ---------------------------------------------------------------------------

def shown(value: Any) -> str:
    """A value as `config.yaml` spells it, for a one-line description."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value or '""'
    if isinstance(value, (list, tuple, frozenset)):
        return f"{len(value)} entries"
    if isinstance(value, dict):
        return f"{len(value)} entries"
    return str(value)


def _bounds(metadata) -> dict:
    """`{'gt': 0}` and friends, out of pydantic's constraint objects.

    Read off the metadata rather than off the annotation alias, because the
    alias is erased by the time `model_fields` is built: `PositiveInt` and a
    hand-written `Annotated[int, Field(gt=0)]` are indistinguishable here,
    which is the right outcome -- the description follows the CONSTRAINT, so
    a new range shape needs no entry anywhere in this file.
    """
    out = {}
    for item in metadata:
        for attribute in ("gt", "ge", "lt", "le", "min_length"):
            value = getattr(item, attribute, None)
            if value is not None:
                out[attribute] = value
    return out


def _number_phrase(noun: str, bounds: dict) -> str:
    if bounds.get("ge") == 0.0 and bounds.get("le") == 1.0:
        return f"{noun} from 0 to 1"
    if bounds.get("gt") == 0:
        return f"{noun}, above 0"
    if bounds.get("ge") == 0:
        return f"{noun}, 0 or more"
    return noun


def describe(name: str, annotation, metadata) -> tuple[str, str]:
    """`(kind, possible values)` for one field.

    The phrasing is generated from the schema rather than written down per
    key, and that is the point: `config.yaml` already carries a comment
    saying what every value DOES, and a second hand-written copy of what it
    may BE is the copy that goes stale. 89 keys, one function.
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    bounds = _bounds(metadata)

    if origin is typing.Literal:
        return "scalar", " | ".join(str(a) for a in args)
    if annotation is bool:
        return "scalar", "true | false"
    if annotation is int:
        return "scalar", _number_phrase("whole number", bounds)
    if annotation is float:
        return "scalar", _number_phrase("number", bounds)
    if annotation is str:
        return "scalar", "text"
    if origin is typing.Union or origin is types.UnionType:
        inner = [a for a in args if a is not type(None)]
        if inner == [str]:
            return "scalar", "text, or null"
        if inner and typing.get_origin(inner[0]) is dict:
            return "pair", "<name>, or <PROVIDER> <name>, or off"
        return "container", "a list, or null"
    if origin is dict:
        return "container", "a table"
    return "container", "a list"


# ---------------------------------------------------------------------------
# ---- The catalogue --------------------------------------------------------
# ---------------------------------------------------------------------------

def catalogue() -> list[KeyRow]:
    """Every addressable name, in `config.yaml`'s own order.

    FILE ORDER, not alphabetical, and it is worth saying why: the schema
    declares its fields in the order the file writes them, under the same
    section banners, so browsing the panel walks the document. Alphabetical
    would interleave the sandbox with the scholar lookup and put
    `compaction_*` in front of the model the harness runs on.

    The two tables are expanded rather than listed, because a row that says
    `tool_permissions · 23 entries` is not something anyone can act on, and
    the 46 leaves are exactly the rows a person comes here for.

    BOTH VALUES COME FROM HERE: the file's, parsed through the cached
    document, and the session's, off the model bound at import. See
    `KeyRow` for why one of them was not enough.
    """
    config = config_schema.current()
    tree = document()
    authority = config_schema.HARNESS_AUTHORITY_KEYS
    overrides = settings_overrides()
    environment = {key.lower(): variable
                   for key, variable in config_schema.ENV_OVERRIDES.items()}

    rows = []
    for name, field in config_schema.HarnessConfig.model_fields.items():
        if name in TABLES:
            table = getattr(config, name)
            tools = list(type(table).model_fields)
            # THE TABLE ITSELF GETS A ROW, ahead of its leaves. Without one
            # `/config tool_permissions` answered "tool_permissions is not a
            # key in config.yaml" -- false three times over: it is a key, it
            # is a top-level block in the file, and it is one of the nine
            # AUTHORITY keys. A container row is what the other 17 containers
            # already get: explained and counted, with the leaves set
            # individually.
            rows.append(KeyRow(
                name=name,
                kind="container",
                values=f"{len(tools)} tools, each true | false",
                in_file=tree[name],
                in_session=table,
                authority=True))
            for tool in tools:
                rows.append(KeyRow(
                    name=f"{name}.{tool}",
                    kind="scalar",
                    values="true | false",
                    in_file=tree[name][tool],
                    in_session=getattr(table, tool),
                    authority=True))
            continue
        kind, values = describe(name, field.annotation, field.metadata)
        # EVERY SOURCE, not the first one found. An `if/elif` reported
        # `model_name` as outranked by `$AGENT_MODEL` and never mentioned
        # `settings.json default_model` -- the tier that arrives with a
        # directory you cloned, and the one a person most needs told about.
        variable = environment.get(name)
        settings_key = overrides.get(name)
        rows.append(KeyRow(
            name=name,
            kind=kind,
            values=values,
            # The raw document's own types for a container -- a list where
            # the model holds a frozenset or a tuple. Never compared for a
            # container, because `pending` is about the settable rows and a
            # container cannot be written from here.
            in_file=tree[name],
            in_session=getattr(config, name),
            authority=name in authority,
            environment_variable=variable,
            settings_key=settings_key))
    return rows


def pending_changes() -> list:
    """The rows written this session and waiting on a restart.

    Settable rows only: a container's file value is a list where the model
    holds a frozenset or a tuple, so the two differ by construction and
    would report as pending forever. Nothing can write one from here in any
    case, so there is nothing for such a row to be pending about.
    """
    return [row for row in catalogue() if row.settable and row.pending]


def find(name: str) -> Optional[KeyRow]:
    """The row called `name`, or None. Exact match, never a prefix."""
    for row in catalogue():
        if row.name == name:
            return row
    return None


def matching(prefix: str) -> list[KeyRow]:
    """Every row whose name starts with `prefix`, in catalogue order.

    Substring would be friendlier and is deliberately not done: the panel
    completes what it draws into the prompt, and a match in the MIDDLE of a
    name completes to something that does not contain what was typed, which
    reads as the harness ignoring the keystrokes.
    """
    return [row for row in catalogue() if row.name.startswith(prefix)]


def file_value(name: str, tree=None) -> Any:
    """What `config.yaml` itself says for `name`. `KeyRow.in_file`'s source.

    Kept as its own function for the callers that want one name without
    building 133 rows to get it. `tree` is for a caller asking about many
    at once, and matters less than it did now that `document()` caches.
    """
    if tree is None:
        tree = document()
    if "." in name:
        table, leaf = name.split(".", 1)
        return tree[table][leaf]
    return tree[name]


def explain(name: str) -> list[str]:
    """The lines `/config <key>` writes. Plain facts, no styling.

    HERE RATHER THAN IN THE HANDLER, for AGENTS.md's §16 rule: the terminal
    is a shell, so what there is to SAY about a key is this module's, and
    drawing it is `tui/app.py`'s. It also means the sentences are testable
    without a terminal, which is where every one of them is checked.
    """
    row = find(name)
    if row is None:
        return [f"{name} is not a key in config.yaml."]

    lines = [f"{row.name}: {shown(row.in_file)}"]
    lines.append(f"Takes {row.values}.")
    if row.kind == "container":
        # The session's copy, not the document's: `todo_statuses` is a tuple
        # here and a list there, and the tuple is what the code sees.
        lines.extend(_entries(row.in_session))

    if row.pending:
        lines.append(
            f"Written this session. This one is still running "
            f"{shown(row.in_session)} and will pick the new value up at the "
            f"next launch.")

    if row.environment_variable:
        variable = row.environment_variable
        if os.environ.get(variable) is not None:
            lines.append(
                f"{variable} is set to {shown(row.in_session)}, so that is "
                f"what this session is using and what the next launch will "
                f"use too. config.yaml says {shown(row.in_file)}, and a "
                f"write here changes that rather than what is in force.")
        else:
            lines.append(
                f"{variable} would override this if it were set.")
    elif row.outranked_by:
        lines.append(
            f"A {row.outranked_by} key outranks this one, so a project or "
            f"user settings.json can win over whatever is written here.")

    if row.authority:
        key = authority_key(name)
        lines.append(f"AUTHORITY. This decides {AUTHORITY_EFFECT[key]}")
        lines.append(
            "No settings file, environment variable or tool call can reach "
            "it. Changing it here asks first.")
    if row.kind == "pair":
        role = "critic" if name == "critic_model" else "embedder"
        lines.append(
            f"/{role} sets this for the next run without a restart, and is "
            f"remembered per user; this sets the harness default.")
    if row.kind == "pair":
        lines.append(
            f"Set it with: /config {name} <PROVIDER> <name>, or "
            f"/config {name} off")
    elif row.settable:
        lines.append(f"Set it with: /config {name} <value>")
    else:
        lines.append(
            "Edit it in config.yaml. CONFIG_ARCHITECTURE.md says why it is "
            "what it is.")
    return lines


def _entries(value: Any, cap: int = 12) -> list[str]:
    """A container's contents, capped, one per line.

    CAPPED because `domain_authority_suffixes` has 105 entries and the
    transcript is a conversation, not a pager. The cap says how many are
    not shown rather than trailing off, for the same reason the suggestion
    panel's title counts what it did not draw.
    """
    if isinstance(value, dict):
        items = [f"  {key}: {shown(item)}" for key, item in value.items()]
    elif isinstance(value, (list, tuple, frozenset)):
        items = [f"  {shown(item)}" for item in sorted(value, key=str)]
    else:
        return []
    if len(items) <= cap:
        return items
    return items[:cap] + [f"  … and {len(items) - cap} more, in config.yaml"]


def authority_key(name: str) -> Optional[str]:
    """The authority key `name` belongs to, or None.

    A leaf answers with its TABLE: `tool_permissions.shell` is gated because
    `tool_permissions` is, and the sentence the confirmation shows is the
    table's.
    """
    root = name.split(".", 1)[0]
    return root if root in config_schema.HARNESS_AUTHORITY_KEYS else None


# ---------------------------------------------------------------------------
# ---- Proposing a change ---------------------------------------------------
# ---------------------------------------------------------------------------

#: The words that mean "no value" wherever `/config` takes one.
#:
#: PUBLIC, and `auto` is in it, because `tui/app._config_set` carried its own
#: copy with `auto` and this one without -- so `/config critic_model auto`
#: cleared the key while `/config default_effort auto` wrote the literal
#: string and was refused by the schema. One word, two behaviours, decided by
#: which branch a key happened to take. The value grammar lives here by this
#: module's own rule; the terminal reads it.
NULL_WORDS = ("null", "none", "off", "clear", "auto")
_NULL_WORDS = NULL_WORDS


def _unquoted(text: str) -> str:
    """`"x"` and `'x'` mean x, for the fields that take their text verbatim.

    The only way to type an EMPTY string, which is what one shipped value
    is: `scholar_mailto` is `''` in the file, and without this there is no
    way to put it back. Typing `""` would set the two-character string
    `""` -- silently, so the key meant to carry an email address would
    carry a pair of quotes and OpenAlex would be sent them.

    It also covers quoting out of habit, which the verbatim rule would
    otherwise take at its word.
    """
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def parse_value(row: KeyRow, text: str) -> Any:
    """Typed text, as the value the schema will be handed.

    THE STRING FIELDS TAKE THEIR TEXT VERBATIM, and that exception is the
    whole reason this is not one `yaml.load` call. `sandbox_docker_image` is
    `python:3.13-slim`, `scholar_api_url` is a URL and `db_path` is a path:
    handing those to a YAML parser asks it to guess, and the day a value
    contains `: ` it would guess "mapping" and the error would talk about a
    dict the user never typed.

    Everything else IS parsed as YAML, so `true`, `0.4`, `18000` and `null`
    mean in the prompt exactly what they mean in the file -- and a value of
    the wrong shape is refused by the schema a moment later, naming the key,
    rather than by a second type system invented here.
    """
    text = text.strip()
    if not text:
        raise ValueError(f"{row.name} needs a value. It takes {row.values}.")
    if row.values.startswith("text"):
        if "null" in row.values and text.lower() in _NULL_WORDS:
            return None
        return _unquoted(text)
    if text.lower() in _NULL_WORDS and "null" in row.values:
        return None
    try:
        return YAML(typ="safe").load(io.StringIO(text))
    except Exception:
        # Unparseable as YAML is still a legal thing to have typed; let the
        # schema be the one that says what was wrong with it.
        return text


def _validate_candidate(candidate: str, name: str, value: Any) -> None:
    """Run `candidate` past the loader as a FILE. Raises ValueError.

    THROUGH THE SEAM, not through a reader assembled here.
    `config_schema.load(path=...)` was built in batch 83 for exactly this
    caller: it validates a candidate on its own terms, with no environment
    overlay and without touching the live cache, because the file is what
    the next launch reads and the environment it will read it under is not
    knowable now. A candidate missing `db_path` is refused here even in a
    shell where `APP_DB_PATH` happens to be set, which is the property that
    makes "this file will start" mean something.

    The temp file sits beside `config.yaml` rather than in the system temp
    directory, so it lands on the same filesystem as the write that may
    follow and cannot be redirected somewhere unwritable by an environment
    variable.

    The message is re-led rather than passed through: the loader names the
    file it was handed, and "config.a7f3.proposed.yaml is not valid" tells
    a person nothing. Everything after that first line -- the key, the
    problem, one line per fault -- is the loader's and is kept verbatim.
    """
    directory = os.path.dirname(config_schema.CONFIG_PATH)
    try:
        handle, path = tempfile.mkstemp(prefix="config.",
                                        suffix=".proposed.yaml", dir=directory)
    except OSError:
        # A GLOBAL npm PREFIX OWNED BY ROOT is an ordinary install, and the
        # directory above is not writable there. Without this the first
        # `/config` write of such a session raised PermissionError out of the
        # command handler and took the app down -- before anything could tell
        # the user the far more useful fact that the file cannot be written.
        # The same fallback `config_update._validates` carries.
        handle, path = tempfile.mkstemp(prefix="config.",
                                        suffix=".proposed.yaml")
    try:
        with open(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(candidate)
        try:
            config_schema.load(path=path)
        except ValueError as exc:
            detail = str(exc)
            _, _, rest = detail.partition("):\n")
            raise ValueError(
                f"{name} = {shown(value)} was refused:\n"
                + (rest or detail)) from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            # A leftover temp file is untidy, not a failure to report over
            # the outcome of the validation the caller asked for.
            pass


def _changed_lines(before: str, after: str) -> list:
    """`(line number, old, new)` for what actually moved between two texts.

    ALIGNED, not zipped. `zip(old, new)` assumes the documents stay the same
    length: setting `critic_model` from `null` to a provider pair grows the
    file by two lines, and every line after the edit then pairs against its
    neighbour -- measured at 707 of 847 lines reported as changed for a
    one-line edit. It also stopped at the shorter document, so a change that
    only removes lines under-reported.

    Line numbers are the NEW document's, because what the caller shows is
    the file as it will be.
    """
    import difflib

    old, new = before.split("\n"), after.split("\n")
    changed = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for offset in range(max(i2 - i1, j2 - j1)):
            a = old[i1 + offset] if i1 + offset < i2 else ""
            b = new[j1 + offset] if j1 + offset < j2 else ""
            changed.append((j1 + offset + 1, a, b))
    return changed


@dataclasses.dataclass(frozen=True)
class Proposal:
    """A validated change that has not been written yet."""

    name: str
    before: Any
    after: Any
    text: str          # the whole document, as it would be written
    newline: str
    lines: list        # the changed lines, `(number, old, new)`


def propose(name: str, value: Any) -> Proposal:
    """Validate `name = value` against the whole document. Raises ValueError.

    THE WHOLE DOCUMENT, not the one field, and that is what makes this worth
    routing through the file at all: `config_schema` carries cross-field
    invariants that a per-field check cannot see -- a compaction strategy has
    to appear in the roster beside it, a similarity band's floor has to be
    under its ceiling, every domain suffix has to name a class that exists.
    Setting one key can break one of those, and the failure belongs here,
    before the write, rather than at the next launch.
    """
    row = find(name)
    if row is None:
        raise ValueError(f"{name} is not a key in config.yaml.")
    if not row.settable:
        raise ValueError(
            f"{name} holds {shown(row.in_session)} and is edited in "
            f"config.yaml directly, not from here.")

    text, newline = read_text()
    tree = _round_trip().load(io.StringIO(text))
    place(tree, name, value)
    candidate = _dump(tree)
    _validate_candidate(candidate, name, value)

    changed = _changed_lines(text, candidate)
    # `in_file`, not `in_session`: "was" is about the value this write is
    # REPLACING, which is the document's. The two are the same until the
    # first write of a session, which is why taking the wrong one read as
    # correct for as long as nobody changed two values in one sitting.
    return Proposal(name=name, before=row.in_file, after=value,
                    text=candidate, newline=newline, lines=changed)


# ---------------------------------------------------------------------------
# ---- Asking for a restart -------------------------------------------------
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class RestartRequest:
    """"Relaunch this harness so a written change takes effect."

    Lives here rather than in `tui/` or `main.py` because both ends need
    it: the TUI exits with one, `main.py` acts on one, and a record they
    both import from a leaf module is cheaper than an edge between them.

    It carries FACTS, not arguments. Which flags express "this thread, this
    model" is `main.py`'s question, and it is the module that owns the
    parser those flags belong to.
    """

    key: str
    thread_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
