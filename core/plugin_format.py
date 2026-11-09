"""
core/plugin_format.py

ROADMAP_v3 §94 (PL1-PL6). What a plugin IS: the folder convention, the
manifest, the files a plugin may hold, and the settings it declares. One
copy of the format, so the installer, the loader and the scanner the
plugins workflow will run (slice 4) cannot come to disagree about what is
valid.

PURE. The standard library and PyYAML; no network, no harness state and no
first-party import. A CI job checking a contribution imports this without
the app, and `core/config_loader.py` reaches it through the store, so a
first-party import here would be a cycle.

THE PARSER IS PART OF THE BOUNDARY (PL2). The owner's reason for keeping a
plugin's settings out of `config.yaml` was that a plugin must not be able to
steer the core harness by abusing a parser. The manifest is the one file
every plugin hands this process to parse, so it gets the strictest reader
here: YAML's safe loader, with aliases refused (an alias is how a small
document becomes a large structure), duplicate keys refused (a reviewer sees
the first, a loader keeps the last), every field typed, and every unknown
key refused rather than ignored.
"""

import json
import math
import re
import shlex
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Optional

import yaml

#: PL1. A folder whose name ends in this is a plugin, and no other folder
#: is. The install id is its path under `plugins/` with this removed, so
#: `plugins/math.plugin` is `math` and can sit beside the category
#: `plugins/math/`, which holds narrower plugins.
SUFFIX = ".plugin"
MANIFEST = "plugin.yaml"
README = "README.md"
LICENSE = "LICENSE"

#: The manifest format this build reads. A higher number is a plugin
#: written for a newer harness, refused by name rather than half-read.
FORMAT_VERSION = 1

#: Plugin and category names. Lowercase words joined by single hyphens:
#: the name is typed in a path (PL1) and becomes the prefix of every skill
#: and agent the plugin adds (PL4), and slice 2's tool names, which a
#: provider holds to `[A-Za-z0-9_-]`.
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_NAME_CHARS = 40

#: Folders from `plugins/` down to the plugin, the plugin's own included.
#: The owner wants nesting without a fixed depth; a bound still has to
#: exist somewhere, and eight is past any taxonomy a reader can follow.
MAX_DEPTH = 8

#: One path component of a file inside a plugin. No leading dot, because a
#: hidden file is what a reviewer's file view folds away; nothing a Windows
#: filesystem refuses or silently rewrites.
_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)})
MAX_COMPONENT_CHARS = 100
MAX_PATH_CHARS = 200

#: PL3. Sized for skill and agent text with room to spare: the largest
#: skill this repository ships is under 10 KB. A plugin past them is a
#: dataset, and a dataset is not what a plugin is for.
MAX_FILES = 300
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 5_000_000
MAX_MANIFEST_BYTES = 64 * 1024

#: Slice 2 (PL20-PL22). A plugin's Python dependencies: a full lock, every
#: package pinned exactly and by the hash of what installs.
REQUIREMENTS = "requirements.txt"

#: Slice 3 (PL27-PL31). A plugin's MCP servers, in the `mcpServers` shape
#: of your own mcp.json, each listing the tools it serves; the code of one
#: the plugin ships sits in `servers/`.
MCP_JSON = "mcp.json"
SERVERS = "servers"

#: The top level of a plugin, and nothing else may sit there (PL3). PL5's
#: list of what later slices run emptied with slice 3, and went with it.
TOP_LEVEL = frozenset({MANIFEST, README, LICENSE, REQUIREMENTS, MCP_JSON,
                       "skills", "agents", "tools", SERVERS, "tests"})

#: PL20. Every tool a plugin adds is `plugin__<plugin>__<tool>`. A plugin
#: name never holds `_` and a tool name never holds `__`, so unlike
#: `mcp__<server>__<tool>` this prefix CAN be taken apart again.
TOOL_PREFIX = "plugin__"
_TOOL_NAME = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*")
#: PL28. A tool an MCP server serves keeps the server's own name, which may
#: be camelCase; still no `__`, so the prefix comes apart one way.
_MCP_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*")
#: The providers' rule for a whole tool name, prefix included:
#: `^[a-zA-Z0-9_-]{1,64}$` (mcp_client/registration.py has the sources).
MAX_TOOL_NAME_CHARS = 64
MAX_TOOLS = 32
#: The two bounds mcp_client/registration.py puts on a third party's tool,
#: for its reasons: both are paid on every model call.
MAX_TOOL_DESCRIPTION_CHARS = 2000
MAX_TOOL_SCHEMA_CHARS = 16384
MAX_SCHEMA_DEPTH = 20
#: PL21. What a plugin's tools may ask to touch.
WORKSPACE_ACCESS = ("none", "read", "write")
MAX_REQUIREMENTS = 200

_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_HANDLE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
_LICENSE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}")
MAX_DESCRIPTION_CHARS = 200
MAX_PEOPLE = 20
MAX_PERSON_CHARS = 100

_FIELDS = ("format", "name", "version", "description", "authors",
           "maintainers", "license", "min_harness", "settings", "tools",
           "access")
_REQUIRED = ("format", "name", "version", "description", "authors",
             "license")

#: PL6. The kinds of value a plugin setting can hold: the scalar kinds
#: `/config` already edits, and nothing that nests.
SETTING_TYPES = ("boolean", "integer", "number", "string", "choice")
_SETTING_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
MAX_SETTINGS = 32
MAX_CHOICES = 32
MAX_CHOICE_CHARS = 64
DEFAULT_STRING_CHARS = 200
MAX_STRING_CHARS = 1000
_SETTING_FIELDS = {
    "boolean": {"type", "default", "description"},
    "integer": {"type", "default", "description", "min", "max"},
    "number": {"type", "default", "description", "min", "max"},
    "string": {"type", "default", "description", "max_length"},
    "choice": {"type", "default", "description", "choices"},
}
_TRUE_WORDS = ("true", "on", "yes")
_FALSE_WORDS = ("false", "off", "no")


class PluginFormatError(ValueError):
    """A plugin that is not valid, with EVERY problem found rather than
    the first, so a contributor fixing one at a time against a CI run is
    not the loop this format costs."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# ---- names and ids -------------------------------------------------------

def valid_name(name) -> bool:
    return (isinstance(name, str) and len(name) <= MAX_NAME_CHARS
            and NAME_PATTERN.fullmatch(name) is not None)


def parse_id(text: str) -> tuple:
    """The folders of a plugin id as typed: `design/web-design/js`.

    A trailing `.plugin` is tolerated, because it is what the folder is
    called and pasting it is the obvious mistake. Anything else that is
    not a sequence of valid names raises ValueError, so a path with `..`
    or a separator in it never reaches a URL or the store.
    """
    raw = text.strip()
    if raw.endswith(SUFFIX):
        raw = raw[:-len(SUFFIX)]
    parts = tuple(raw.split("/"))
    if not raw or not all(valid_name(part) for part in parts):
        raise ValueError(
            f"{text.strip()!r} is not a plugin id: it is folder names under "
            f"plugins/, each lowercase letters, digits and single hyphens, "
            f"joined by '/', like design/web-design/javascript-and-typescript")
    if len(parts) > MAX_DEPTH:
        raise ValueError(
            f"{text.strip()!r} is {len(parts)} folders deep; a plugin id has "
            f"at most {MAX_DEPTH}")
    return parts


def folder_of(parts: tuple) -> str:
    """`math/linear-algebra/x` -> `math/linear-algebra/x.plugin`."""
    return "/".join(parts[:-1] + (parts[-1] + SUFFIX,))


# ---- text fields ---------------------------------------------------------

#: The categories no field may hold. Cs since batch 251 (PL31): a lone
#: surrogate, which only an escape can make -- YAML takes an escaped pair
#: as two halves where JSON joins it into one character, measured -- and
#: which no UTF-8 encoder will write, so it could never reach a provider.
_HIDDEN = ("Cc", "Cf", "Cs", "Zl", "Zp")


def _invisible(text: str) -> bool:
    """Control, format (bidi overrides, zero-width marks), lone surrogates
    and line or paragraph separators. Each one makes what a reviewer reads
    and what a reader prints differ, so no field of the manifest may hold
    one."""
    return any(unicodedata.category(ch) in _HIDDEN for ch in text)


def _half_pair(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cs" for ch in text)


#: What to write instead of an escaped surrogate pair. No backslash-u in
#: this file's own text, so no tool that edits it can turn one into the
#: character it names.
_HALF_PAIR = ("holds half of an escaped surrogate pair, which YAML keeps "
              "as two broken halves where JSON makes one character; write "
              "the character itself, or its eight-digit escape (a capital U)")


def _line(value, max_chars: int):
    """`value` if it is one printable line of at most `max_chars`, else
    None."""
    if (not isinstance(value, str) or not value.strip()
            or len(value) > max_chars or _invisible(value)):
        return None
    return value


def version_tuple(text: str) -> tuple:
    """(major, minor, patch) from `0.3.13` or a branch build such as
    `0.3.13-b.1.plugin-system.r0123456789ab`, whose base is what a
    plugin's `min_harness` is compared with. ValueError otherwise."""
    match = _VERSION.match(text.strip())
    if match is None:
        raise ValueError(f"{text!r} is not a version like 1.2.3")
    return tuple(int(part) for part in match.groups())


# ---- settings ------------------------------------------------------------

def _number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


@dataclass(frozen=True)
class Setting:
    """One setting a plugin declares (PL6). The value you choose is kept
    beside the install, never in `config.yaml`."""

    key: str
    type: str
    default: object
    description: str
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: tuple = ()
    max_length: int = DEFAULT_STRING_CHARS

    def check(self, value) -> Optional[str]:
        """None when `value` is valid for this setting, else why not."""
        if self.type == "boolean":
            return None if isinstance(value, bool) else "must be true or false"
        if self.type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return "must be a whole number"
        elif self.type == "number":
            if not _number(value):
                return "must be a number"
        elif self.type == "string":
            if not isinstance(value, str):
                return "must be text"
            if len(value) > self.max_length:
                return f"must be at most {self.max_length} characters"
            if _invisible(value):
                return "must not hold control or invisible characters"
            return None
        elif self.type == "choice":
            return (None if value in self.choices
                    else f"must be one of {', '.join(self.choices)}")
        if self.minimum is not None and value < self.minimum:
            return f"must be at least {_shown(self.minimum)}"
        if self.maximum is not None and value > self.maximum:
            return f"must be at most {_shown(self.maximum)}"
        return None

    def parse(self, text: str):
        """The value `text`, as typed after `/plugin config`, means.
        ValueError naming why when it means none."""
        text = text.strip()
        if self.type == "boolean":
            if text.lower() in _TRUE_WORDS:
                return True
            if text.lower() in _FALSE_WORDS:
                return False
            raise ValueError(f"{self.key} must be true or false")
        if self.type == "integer":
            if not re.fullmatch(r"-?[0-9]{1,18}", text):
                raise ValueError(f"{self.key} must be a whole number")
            value = int(text)
        elif self.type == "number":
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"{self.key} must be a number") from None
            if value.is_integer() and "." not in text and "e" not in text.lower():
                value = int(value)
        else:
            value = text
        why = self.check(value)
        if why:
            raise ValueError(f"{self.key} {why}")
        return value

    def describe(self) -> str:
        """The kind of value, for the modal and `/plugin config`."""
        if self.type == "boolean":
            return "true or false"
        if self.type == "choice":
            return "one of " + ", ".join(self.choices)
        if self.type == "string":
            return f"text, up to {self.max_length} characters"
        kind = "a whole number" if self.type == "integer" else "a number"
        if self.minimum is not None and self.maximum is not None:
            return f"{kind} from {_shown(self.minimum)} to {_shown(self.maximum)}"
        if self.minimum is not None:
            return f"{kind}, at least {_shown(self.minimum)}"
        if self.maximum is not None:
            return f"{kind}, at most {_shown(self.maximum)}"
        return kind


def shown(value) -> str:
    """A setting's value as `/plugin config` and the modal print it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


_shown = shown


def _parse_setting(key, spec, problems: list) -> Optional[Setting]:
    where = f"settings.{key}"
    if isinstance(key, bool):
        problems.append(
            f"setting {str(key).lower()}: YAML reads the words yes, no, on "
            f"and off as true or false, so this key is not the word you "
            f"wrote; quote it or choose another")
        return None
    if not isinstance(key, str) or not _SETTING_KEY.fullmatch(key):
        problems.append(
            f"setting {key!r}: a key is a lowercase letter, then letters, "
            f"digits or underscores, at most 40 characters")
        return None
    if not isinstance(spec, dict):
        problems.append(f"{where}: must be a mapping with a type, a default "
                        f"and a description")
        return None
    kind = spec.get("type")
    if kind not in SETTING_TYPES:
        problems.append(f"{where}.type: must be one of "
                        f"{', '.join(SETTING_TYPES)}")
        return None
    unknown = sorted(set(spec) - _SETTING_FIELDS[kind], key=str)
    if unknown:
        problems.append(f"{where}: {', '.join(map(str, unknown))} "
                        f"{'is' if len(unknown) == 1 else 'are'} not a field "
                        f"of a {kind} setting")
    for needed in ("default", "description"):
        if needed not in spec:
            problems.append(f"{where}: needs a {needed}")
    description = _line(spec.get("description"), MAX_DESCRIPTION_CHARS)
    if "description" in spec and description is None:
        problems.append(f"{where}.description: one line of at most "
                        f"{MAX_DESCRIPTION_CHARS} characters")
    fields = {}
    if kind in ("integer", "number"):
        for bound in ("min", "max"):
            if bound in spec:
                value = spec[bound]
                ok = (_number(value) if kind == "number"
                      else isinstance(value, int) and not isinstance(value, bool))
                if not ok:
                    problems.append(f"{where}.{bound}: must be "
                                    f"{'a number' if kind == 'number' else 'a whole number'}")
                else:
                    fields["minimum" if bound == "min" else "maximum"] = value
        if ("minimum" in fields and "maximum" in fields
                and fields["minimum"] > fields["maximum"]):
            problems.append(f"{where}: min is above max")
    elif kind == "string" and "max_length" in spec:
        length = spec["max_length"]
        if (not isinstance(length, int) or isinstance(length, bool)
                or not 1 <= length <= MAX_STRING_CHARS):
            problems.append(f"{where}.max_length: a whole number from 1 to "
                            f"{MAX_STRING_CHARS}")
        else:
            fields["max_length"] = length
    elif kind == "choice":
        choices = spec.get("choices")
        if (not isinstance(choices, list) or not choices
                or len(choices) > MAX_CHOICES
                or not all(_line(c, MAX_CHOICE_CHARS) and " " not in c
                           for c in choices)
                or len(set(choices)) != len(choices)):
            problems.append(
                f"{where}.choices: a list of 1 to {MAX_CHOICES} different "
                f"words, each at most {MAX_CHOICE_CHARS} characters")
            return None
        fields["choices"] = tuple(choices)
    if "default" not in spec or description is None or problems_for(where, problems):
        return None
    setting = Setting(key=key, type=kind, default=spec["default"],
                      description=description, **fields)
    why = setting.check(setting.default)
    if why:
        problems.append(f"{where}.default: {why}")
        return None
    return setting


def problems_for(where: str, problems: list) -> bool:
    """Whether a problem about `where` (or a field under it) was recorded."""
    return any(p.startswith((where + ":", where + ".")) for p in problems)


# ---- tools (slice 2) -----------------------------------------------------

def tool_name(plugin: str, tool: str) -> str:
    """The name the model calls a plugin's tool by (PL20)."""
    return f"{TOOL_PREFIX}{plugin}__{tool}"


def split_tool_name(full: str) -> Optional[tuple]:
    """(plugin, tool) from `plugin__<plugin>__<tool>`, or None."""
    if not full.startswith(TOOL_PREFIX):
        return None
    plugin, sep, tool = full[len(TOOL_PREFIX):].partition("__")
    # The MCP rule, which holds every PL20 name too.
    if not sep or not valid_name(plugin) or not _MCP_TOOL_NAME.fullmatch(tool):
        return None
    return plugin, tool


@dataclass(frozen=True)
class ToolDef:
    """One tool a plugin declares in its manifest (PL20). Its code is
    `tools/<module>.py`, whose `run(params, context)` the runner calls; the
    harness learns everything else here without running any of it."""

    name: str
    description: str
    input_schema: dict = field(hash=False)

    @property
    def module(self) -> str:
        return self.name.replace("-", "_")

    @property
    def path(self) -> str:
        return f"tools/{self.module}.py"


@dataclass(frozen=True)
class Access:
    """What a plugin's tools ask to touch (PL21). A ceiling: the grant
    made per project may be less, never more."""

    workspace: str = "none"
    network: bool = False

    @property
    def asks(self) -> bool:
        return self.workspace != "none" or self.network


def _layout_only(text: str) -> bool:
    """`_invisible`, except that a newline or a tab is layout: a tool's
    description is written for the model, and YAML's block scalars end
    their lines with one."""
    return not any(unicodedata.category(ch) in _HIDDEN
                   and ch not in "\n\t" for ch in text)


def _json_like(value, depth: int = 0) -> Optional[str]:
    """None when `value` is JSON a provider will take as a schema, else
    why not. YAML can hold more than JSON can -- a date, a set, a
    non-string key -- and a schema that will not serialise is a tool that
    fails on the first model call rather than here."""
    if depth > MAX_SCHEMA_DEPTH:
        return f"nests deeper than {MAX_SCHEMA_DEPTH} levels"
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if _number(value) else "holds a number JSON cannot"
    if isinstance(value, str):
        if _half_pair(value):
            return _HALF_PAIR
        return None if _layout_only(value) else (
            "holds control or invisible characters")
    if isinstance(value, list):
        for item in value:
            why = _json_like(item, depth + 1)
            if why:
                return why
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or isinstance(key, bool):
                return f"has the key {key!r}, which is not text"
            if _half_pair(key):
                return _HALF_PAIR
            if not _layout_only(key):
                return "has a key with control or invisible characters"
            why = _json_like(item, depth + 1)
            if why:
                return why
        return None
    return f"holds a {type(value).__name__}, which JSON has no form for"


def _parse_tools(plugin: str, raw, problems: list) -> tuple:
    if not isinstance(raw, list) or not raw:
        problems.append("tools: a list of 1 to "
                        f"{MAX_TOOLS} tools, each with a name, a description "
                        "and an input_schema")
        return ()
    if len(raw) > MAX_TOOLS:
        problems.append(f"tools: at most {MAX_TOOLS}")
        return ()
    out, names, modules = [], set(), set()
    for index, spec in enumerate(raw):
        where = f"tools[{index}]"
        if not isinstance(spec, dict):
            problems.append(f"{where}: a mapping with a name, a description "
                            f"and an input_schema")
            continue
        unknown = sorted(str(k) for k in spec
                         if k not in ("name", "description", "input_schema"))
        if unknown:
            problems.append(f"{where}: {', '.join(unknown)} "
                            f"{'is' if len(unknown) == 1 else 'are'} not a "
                            f"field of a tool")
        name = spec.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            problems.append(f"{where}.name: lowercase letters and digits, "
                            f"words joined by single '-' or '_', starting "
                            f"with a letter")
            continue
        where = f"tools.{name}"
        full = tool_name(plugin, name)
        if len(full) > MAX_TOOL_NAME_CHARS:
            problems.append(
                f"{where}: the model calls it {full}, {len(full)} characters, "
                f"and a provider accepts at most {MAX_TOOL_NAME_CHARS}; "
                f"shorten the tool's name or the plugin's")
        if name in names:
            problems.append(f"{where}: declared twice")
        module = name.replace("-", "_")
        if module in modules and name not in names:
            problems.append(f"{where}: its code would be tools/{module}.py, "
                            f"which another tool's name also maps to")
        names.add(name)
        modules.add(module)
        description = spec.get("description")
        if (not isinstance(description, str) or not description.strip()
                or len(description) > MAX_TOOL_DESCRIPTION_CHARS
                or not _layout_only(description)):
            problems.append(f"{where}.description: text of at most "
                            f"{MAX_TOOL_DESCRIPTION_CHARS} characters, no "
                            f"control or invisible characters")
            description = None
        schema = spec.get("input_schema")
        why = _schema_problem(schema)
        if why:
            problems.append(f"{where}.input_schema: {why}")
            continue
        if description is not None:
            out.append(ToolDef(name=name, description=description,
                               input_schema=schema))
    return tuple(out)


def _schema_problem(schema) -> Optional[str]:
    """Why `schema` cannot be a tool's parameters, or None. One rule for
    a tool's `input_schema` and an MCP tool's `inputSchema` (PL20, PL28)."""
    if not isinstance(schema, dict):
        return "must be a mapping"
    if schema.get("type") != "object":
        return "must have type: object, as every tool's parameters do"
    if "properties" in schema and not isinstance(schema["properties"], dict):
        return "its properties must be a mapping"
    why = _json_like(schema)
    if why is None:
        size = len(json.dumps(schema))
        if size > MAX_TOOL_SCHEMA_CHARS:
            why = (f"is {size} characters as JSON, and a tool's schema is "
                   f"at most {MAX_TOOL_SCHEMA_CHARS}")
    return why


def _parse_access(raw, problems: list) -> Access:
    if not isinstance(raw, dict):
        problems.append("access: a mapping with workspace and network")
        return Access()
    unknown = sorted(str(k) for k in raw if k not in ("workspace", "network"))
    if unknown:
        problems.append(f"access: {', '.join(unknown)} "
                        f"{'is' if len(unknown) == 1 else 'are'} not a field "
                        f"of access")
    workspace = raw.get("workspace", "none")
    if workspace not in WORKSPACE_ACCESS:
        problems.append("access.workspace: none, read or write")
        workspace = "none"
    network = raw.get("network", False)
    if not isinstance(network, bool):
        problems.append("access.network: true or false")
        network = False
    return Access(workspace=workspace, network=network)


# ---- requirements.txt (slice 2) ------------------------------------------

@dataclass(frozen=True)
class Requirement:
    """One locked package (PL22): an exact version and the hashes of the
    files that version may install from."""

    name: str
    version: str
    hashes: tuple
    extras: tuple = ()

    @property
    def pin(self) -> str:
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        return f"{self.name}{extras}=={self.version}"


_REQUIREMENT = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?)"
    r"(?:\[(?P<extras>[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*)\])?"
    r"==(?P<version>[0-9][A-Za-z0-9.+!_-]{0,62})")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")
_COMMENT = re.compile(r"(?:^|\s)#.*$")


def canonical_package(name: str) -> str:
    """PEP 503's normal form, so `Foo_Bar` and `foo-bar` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str) -> tuple:
    """`requirements.txt`'s locked packages (PL22). PluginFormatError
    listing every problem otherwise.

    A FULL LOCK AND NOTHING ELSE. Each requirement is `name==version`
    followed by at least one `--hash=sha256:`; comments and blank lines are
    allowed, and every option pip has -- an index, a URL, `-r`, `-e`, an
    environment marker -- is refused, because each is a way for what
    installs to differ from what the modal listed. pip is then run with
    `--require-hashes`, which refuses a dependency the lock left out, so
    an incomplete lock fails the install rather than resolving the rest
    from the index.
    """
    problems, out, seen = [], [], {}
    logical, start = [], 0
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if _invisible(line.replace("\t", " ")):
            problems.append(f"{REQUIREMENTS} line {number}: holds control or "
                            f"invisible characters")
            continue
        stripped = _COMMENT.sub("", line).rstrip()
        if not logical:
            start = number
        if stripped.endswith("\\"):
            logical.append(stripped[:-1])
            continue
        logical.append(stripped)
        joined = " ".join(logical).split()
        logical = []
        if not joined:
            continue
        where = f"{REQUIREMENTS} line {start}"
        match = _REQUIREMENT.fullmatch(joined[0])
        if match is None:
            problems.append(
                f"{where}: {joined[0][:80]!r} is not name==version. A plugin's "
                f"requirements are a full lock: exact versions with hashes, "
                f"and no options, URLs or environment markers")
            continue
        hashes = []
        for token in joined[1:]:
            found = _HASH.fullmatch(token)
            if found is None:
                problems.append(f"{where}: {token[:80]!r} -- only "
                                f"--hash=sha256:<64 hex digits> may follow a "
                                f"pin")
            else:
                hashes.append(found.group(1))
        name = match.group("name")
        if not hashes:
            problems.append(f"{where}: {name} has no --hash=sha256:. Generate "
                            f"the file with `uv pip compile --generate-hashes` "
                            f"or pip-tools' `pip-compile --generate-hashes`")
        key = canonical_package(name)
        if key in seen:
            problems.append(f"{where}: {name} is already pinned on line "
                            f"{seen[key]}")
        seen[key] = start
        extras = tuple(match.group("extras").split(",")) if match.group(
            "extras") else ()
        out.append(Requirement(name=name, version=match.group("version"),
                               hashes=tuple(hashes), extras=extras))
    if logical:
        problems.append(f"{REQUIREMENTS} ends with a line continued by '\\'")
    if len(out) > MAX_REQUIREMENTS:
        problems.append(f"{REQUIREMENTS}: {len(out)} packages; a plugin "
                        f"locks at most {MAX_REQUIREMENTS}")
    if not out and not problems:
        problems.append(f"{REQUIREMENTS} pins no package; leave the file out "
                        f"instead")
    if problems:
        raise PluginFormatError(problems)
    return tuple(out)


# ---- mcp.json (slice 3) --------------------------------------------------

#: PL27. Servers in one plugin; each is a container of its own while it
#: runs.
MAX_SERVERS = 8
MAX_ARGS = 64
MAX_ARG_CHARS = 1000
MAX_ENV = 32
MAX_TITLE_CHARS = 200
_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
#: What the harness sets for a server, or what decides what runs rather
#: than what it is told: a plugin's `env` may hold neither.
_HARNESS_ENV = frozenset({"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "PWD"})
_HARNESS_ENV_PREFIXES = ("PYTHON", "LD_", "DYLD_", "VENASTINE_")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
#: MCP's own Tool fields, as a server's tools/list answer spells them, so a
#: listing pastes in unchanged (PL28). The model reads the first four.
_MCP_TOOL_KEYS = ("name", "title", "description", "inputSchema",
                  "outputSchema", "annotations", "icons", "_meta", "execution")
_SERVER_KEYS = ("type", "command", "args", "env", "tools")
#: Keys your own mcp.json reads that a plugin's may not hold, and why.
#: Remote servers and other commands are deferred, not refused for good
#: (owner, batch 251); the sentence says so.
_REMOTE = ("is for a remote MCP server, and a plugin's servers are local in "
           "this version; remote servers are deferred to a later one")
_NOT_A_PLUGINS = {
    "url": _REMOTE,
    "headers": _REMOTE,
    "cwd": ("is the harness's to choose: the workspace when the project "
            "grants it, else a private folder"),
    "autoApprove": "is yours, not the plugin's: /plugin approval <id> auto",
    "auto_approve": "is yours, not the plugin's: /plugin approval <id> auto",
    "disabled": "is yours, not the plugin's: /plugin disable <id>",
}


@dataclass(frozen=True)
class McpTool:
    """One tool a plugin's MCP server serves, as its mcp.json lists it
    (PL28). The model reads this, never what the server says at run time,
    so what the modal showed is what the model gets."""

    name: str
    description: str
    input_schema: dict = field(hash=False)
    title: Optional[str] = None
    server: str = ""

    @property
    def model_description(self) -> str:
        """The title composed in, as mcp_client/registration.py does."""
        if self.title and self.title not in self.description:
            return f"{self.title}. {self.description}"
        return self.description


@dataclass(frozen=True)
class McpServer:
    """One server in a plugin's mcp.json (PL27, PL29): `python -m module
    args...`, a module the plugin ships in servers/ or one its lock
    installs."""

    name: str
    module: str
    args: tuple = ()
    env: tuple = ()          # ((name, value), ...)
    tools: tuple = ()

    @property
    def command_line(self) -> str:
        return shlex.join(["python", "-m", self.module, *self.args])


def _strict_json(text: str):
    """`json.loads`, refusing what the user tier's reader lets through: a
    repeated key (a reviewer sees the first, a loader keeps the last, as
    PL2 says of YAML) and NaN or Infinity, which JSON proper has not got."""
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"the key {key!r} appears twice")
            out[key] = value
        return out

    def constant(word):
        raise ValueError(f"{word} is not a JSON value")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _shown_key(key) -> str:
    text = str(key)[:60]
    return repr(text) if _invisible(text) else text


def parse_mcp_json(text: str, plugin: str) -> tuple:
    """A plugin's mcp.json (PL27): its servers, each with the tools it
    serves. PluginFormatError listing every problem otherwise.

    YOUR OWN mcp.json'S SHAPE, AND A STRICTER READER. The user tier ignores
    a key it does not know and turns an entry it cannot read into that
    server's failure at connect, because a file you wrote or pasted should
    not stop a session (ROADMAP_v2 §17's decision G). A plugin's file is
    read before its trust modal, by the rule the manifest has: every key
    known, every field typed, every problem reported.
    """
    try:
        data = _strict_json(text)
    except RecursionError:
        raise PluginFormatError(
            [f"{MCP_JSON} nests too deeply to read"]) from None
    except ValueError as exc:
        if isinstance(exc, json.JSONDecodeError):
            why = f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
        else:
            why = str(exc)
        raise PluginFormatError(
            [f"{MCP_JSON} is not valid JSON: {why}"]) from None
    if not isinstance(data, dict):
        raise PluginFormatError(
            [f'{MCP_JSON} must be an object: {{"mcpServers": {{...}}}}'])
    problems = []
    unknown = [_shown_key(k) for k in data if k != "mcpServers"]
    if unknown:
        problems.append(f"{MCP_JSON}: {', '.join(unknown)} "
                        f"{'is' if len(unknown) == 1 else 'are'} not a field; "
                        f"a plugin's {MCP_JSON} holds mcpServers and nothing "
                        f"else")
    raw = data.get("mcpServers")
    if not isinstance(raw, dict) or not raw or len(raw) > MAX_SERVERS:
        problems.append(f"{MCP_JSON}: mcpServers must be an object of 1 to "
                        f"{MAX_SERVERS} servers")
        raise PluginFormatError(problems)
    servers = []
    for name, spec in raw.items():
        server = _parse_server(plugin, name, spec, problems)
        if server is not None:
            servers.append(server)
    if problems:
        raise PluginFormatError(problems)
    return tuple(servers)


def _parse_server(plugin: str, name: str, spec, problems: list):
    if not valid_name(name):
        problems.append(f"mcpServers: {_shown_key(name)!r} is not a server "
                        f"name: lowercase letters, digits and single "
                        f"hyphens, at most {MAX_NAME_CHARS} characters")
        return None
    where = f"mcpServers.{name}"
    if not isinstance(spec, dict):
        problems.append(f"{where}: an object with command, args and tools")
        return None
    count = len(problems)
    for key in spec:
        if key not in _SERVER_KEYS:
            problems.append(f"{where}.{_shown_key(key)}: " + _NOT_A_PLUGINS.get(
                key, "is not a field of a plugin's MCP server"))
    kind = spec.get("type", "stdio")
    if kind != "stdio":
        problems.append(f"{where}.type: is {str(kind)[:40]!r}, and a "
                        f"plugin's server speaks stdio; remote servers are "
                        f"deferred to a later version")
    if spec.get("command") != "python":
        problems.append(
            f'{where}.command: must be "python", with "args": ["-m", '
            f'"<module>", ...]: a plugin\'s server is a Python module it '
            f'ships in {SERVERS}/ or installs from its {REQUIREMENTS}. '
            f'Servers started by npx, uvx or another command are deferred '
            f'to a later version')
    args = spec.get("args")
    module, rest = None, ()
    if (not isinstance(args, list) or len(args) < 2 or args[0] != "-m"
            or not isinstance(args[1], str) or not _MODULE.fullmatch(args[1])):
        problems.append(f'{where}.args: must start "-m", "<module>", a '
                        f'dotted Python module name, as in ["-m", '
                        f'"notes_server"]')
    else:
        module, rest = args[1], tuple(args[2:])
        if len(rest) > MAX_ARGS or not all(
                isinstance(a, str) and len(a) <= MAX_ARG_CHARS
                and not _invisible(a) for a in rest):
            problems.append(f"{where}.args: at most {MAX_ARGS} after the "
                            f"module, each text of at most {MAX_ARG_CHARS} "
                            f"characters on one line")
    env = []
    raw_env = spec.get("env", {})
    if not isinstance(raw_env, dict) or len(raw_env) > MAX_ENV:
        problems.append(f"{where}.env: an object of at most {MAX_ENV} "
                        f"variables, each a text value")
    else:
        for key, value in raw_env.items():
            if not _ENV_KEY.fullmatch(key):
                problems.append(f"{where}.env: {_shown_key(key)!r} is not a "
                                f"variable name: capital letters, digits and "
                                f"'_', starting with a letter")
            elif key in _HARNESS_ENV or key.startswith(_HARNESS_ENV_PREFIXES):
                problems.append(f"{where}.env.{key}: is the harness's to set")
            elif (not isinstance(value, str) or len(value) > MAX_ARG_CHARS
                  or _invisible(value)):
                problems.append(f"{where}.env.{key}: text of at most "
                                f"{MAX_ARG_CHARS} characters on one line")
            else:
                env.append((key, value))
    tools = []
    raw_tools = spec.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        problems.append(
            f"{where}.tools: a list of the tools this server serves that the "
            f"model may call, each as the server's own tools/list answer "
            f"gives it: name, description and inputSchema. A tool the server "
            f"has and this list leaves out never loads")
    elif len(raw_tools) > MAX_TOOLS:
        problems.append(f"{where}.tools: at most {MAX_TOOLS}")
    else:
        for index, tool_spec in enumerate(raw_tools):
            tool = _parse_mcp_tool(plugin, name, index, tool_spec, problems)
            if tool is not None:
                tools.append(tool)
    if len(problems) > count or module is None:
        return None
    return McpServer(name=name, module=module, args=rest, env=tuple(env),
                     tools=tuple(tools))


def _parse_mcp_tool(plugin: str, server: str, index: int, spec,
                    problems: list):
    where = f"mcpServers.{server}.tools[{index}]"
    if not isinstance(spec, dict):
        problems.append(f"{where}: an object with name, description and "
                        f"inputSchema")
        return None
    unknown = [_shown_key(k) for k in spec if k not in _MCP_TOOL_KEYS]
    if unknown:
        problems.append(f"{where}: {', '.join(unknown)} "
                        f"{'is' if len(unknown) == 1 else 'are'} not a field "
                        f"of an MCP tool")
    name = spec.get("name")
    if not isinstance(name, str) or not _MCP_TOOL_NAME.fullmatch(name):
        problems.append(
            f"{where}.name: letters and digits, words joined by single '-' or "
            f"'_', starting with a letter. A provider takes no other tool "
            f"name, so a server's tool named otherwise cannot be offered "
            f"through a plugin")
        return None
    where = f"mcpServers.{server}.tools.{name}"
    full = tool_name(plugin, name)
    if len(full) > MAX_TOOL_NAME_CHARS:
        problems.append(
            f"{where}: the model calls it {full}, {len(full)} characters, and "
            f"a provider accepts at most {MAX_TOOL_NAME_CHARS}; the server "
            f"names its tools, so shorten the plugin's name")
    description = spec.get("description")
    if (not isinstance(description, str) or not description.strip()
            or len(description) > MAX_TOOL_DESCRIPTION_CHARS
            or not _layout_only(description)):
        problems.append(
            f"{where}.description: text of at most "
            f"{MAX_TOOL_DESCRIPTION_CHARS} characters, no control or "
            f"invisible characters. Write one here if the server gives none: "
            f"this is what the model reads")
        description = None
    title = spec.get("title")
    if title is not None and _line(title, MAX_TITLE_CHARS) is None:
        problems.append(f"{where}.title: one line of at most "
                        f"{MAX_TITLE_CHARS} characters")
    why = _schema_problem(spec.get("inputSchema"))
    if why:
        problems.append(f"{where}.inputSchema: {why}")
        return None
    for key in _MCP_TOOL_KEYS[4:]:
        if key in spec:
            other = _json_like(spec[key])
            if other:
                problems.append(f"{where}.{key}: {other}")
    if description is None:
        return None
    return McpTool(name=name, description=description,
                   input_schema=spec["inputSchema"], title=title,
                   server=server)


# ---- the manifest --------------------------------------------------------

@dataclass(frozen=True)
class Manifest:
    """A plugin's `plugin.yaml`, validated (PL2). `requirements` is
    `requirements.txt`'s and `servers` is `mcp.json`'s, when `validate` or
    `read_installed` read them."""

    name: str
    version: str
    description: str
    authors: tuple
    license: str
    maintainers: tuple = ()
    min_harness: Optional[str] = None
    settings: tuple = ()
    tools: tuple = ()
    access: Access = Access()
    requirements: tuple = ()
    servers: tuple = ()

    def setting(self, key: str) -> Optional[Setting]:
        return next((s for s in self.settings if s.key == key), None)

    def tool(self, name: str) -> Optional[ToolDef]:
        return next((t for t in self.tools if t.name == name), None)

    def server(self, name: str) -> Optional[McpServer]:
        return next((s for s in self.servers if s.name == name), None)

    @property
    def mcp_tools(self) -> tuple:
        """Every tool the plugin's servers serve, in mcp.json's order."""
        return tuple(t for s in self.servers for t in s.tools)

    @property
    def has_code(self) -> bool:
        """Whether anything of the plugin's runs: tools or servers."""
        return bool(self.tools or self.servers)


class _StrictLoader(yaml.SafeLoader):
    """`yaml.safe_load`, with aliases refused. See the module docstring."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None, None, "aliases (*name) are not allowed in a plugin "
                "manifest", event.start_mark)
        return super().compose_node(parent, index)


def _construct_mapping(loader, node, deep=False):
    """A mapping whose keys are plain scalars and never repeat."""
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool)) or key is None:
            raise yaml.constructor.ConstructorError(
                None, None, "a mapping key must be a plain word",
                key_node.start_mark)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"the key {key!r} appears twice",
                key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)

#: PL31. YAML 1.1, which PyYAML reads, makes `1e6` -- JSON's way to write a
#: million -- the TEXT "1e6": its floats need a dot and a signed exponent.
#: Of `1e6`, `1E+6`, `1.5e3` and `1.5e-3`, only the last stayed a number
#: (PyYAML 6.0.3, measured), so a schema pasted from JSON passed every check
#: and sent a provider `"maximum": "1e6"`. YAML 1.2 reads all four as
#: numbers, and so does this loader (owner, batch 251). Appended after
#: SafeLoader's own resolvers, none of which matches these, on this class's
#: own copy of the table.
_StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[eE][-+]?[0-9]+$"),
    list("-+0123456789."))


def load_yaml(text: str):
    """`text` parsed by the strict loader. yaml.YAMLError otherwise."""
    return yaml.load(text, Loader=_StrictLoader)  # nosec B506 - _StrictLoader is a SafeLoader subclass


def parse_manifest(text: str, name: Optional[str]) -> Manifest:
    """`plugin.yaml`'s text, for the plugin whose folder is `name` +
    SUFFIX. A Git repository root has no folder name to match, so passes
    None. PluginFormatError listing every problem otherwise."""
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise PluginFormatError(
            [f"{MANIFEST} is over {MAX_MANIFEST_BYTES // 1024} KB"])
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        first = str(exc).strip().splitlines()
        raise PluginFormatError(
            [f"{MANIFEST} is not valid YAML: "
             f"{' '.join(line.strip() for line in first[:3])}"]) from None
    if not isinstance(data, dict):
        raise PluginFormatError([f"{MANIFEST} must be a YAML mapping"])
    problems = []
    unknown = sorted(str(k) for k in data if k not in _FIELDS)
    if unknown:
        problems.append(f"{MANIFEST}: {', '.join(unknown)} "
                        f"{'is' if len(unknown) == 1 else 'are'} not a "
                        f"manifest field")
    for field_name in _REQUIRED:
        if field_name not in data:
            problems.append(f"{MANIFEST}: needs {field_name}")

    fmt = data.get("format")
    if "format" in data:
        if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt < 1:
            problems.append("format: must be the number 1")
        elif fmt > FORMAT_VERSION:
            raise PluginFormatError(
                [f"written for plugin format {fmt}; this harness reads "
                 f"format {FORMAT_VERSION}, so it needs a newer harness"])

    if "name" in data:
        if not valid_name(data["name"]):
            problems.append("name: lowercase letters, digits and single "
                            "hyphens, at most 40 characters")
        elif name is not None and data["name"] != name:
            problems.append(f"name: is {data['name']!r}, but the folder is "
                            f"{name + SUFFIX!r}; they must agree")

    version = data.get("version")
    if "version" in data:
        if isinstance(version, (int, float)) and not isinstance(version, bool):
            problems.append("version: quote it, as in version: \"1.0.0\", "
                            "or YAML reads it as a number")
        elif not isinstance(version, str) or not _VERSION.fullmatch(version):
            problems.append("version: three numbers, as in \"1.0.0\"")

    description = _line(data.get("description"), MAX_DESCRIPTION_CHARS)
    if "description" in data and description is None:
        problems.append(f"description: one line of at most "
                        f"{MAX_DESCRIPTION_CHARS} characters, no control or "
                        f"invisible characters")

    authors = _people(data, "authors", _line_person, problems, required=True)
    maintainers = _people(data, "maintainers", _handle, problems,
                          required=False)

    license_id = data.get("license")
    if "license" in data and (not isinstance(license_id, str)
                              or not _LICENSE_ID.fullmatch(license_id)):
        problems.append("license: an SPDX identifier, as in MIT or "
                        "Apache-2.0")

    min_harness = data.get("min_harness")
    if min_harness is not None and (not isinstance(min_harness, str)
                                    or not _VERSION.fullmatch(min_harness)):
        problems.append("min_harness: a quoted version, as in \"0.3.14\"")

    settings = []
    raw_settings = data.get("settings")
    if raw_settings is not None:
        if not isinstance(raw_settings, dict):
            problems.append("settings: must be a mapping of setting keys")
        elif len(raw_settings) > MAX_SETTINGS:
            problems.append(f"settings: at most {MAX_SETTINGS}")
        else:
            for key, spec in raw_settings.items():
                setting = _parse_setting(key, spec, problems)
                if setting is not None:
                    settings.append(setting)

    tools = ()
    if "tools" in data:
        tools = _parse_tools(data.get("name") if valid_name(data.get("name"))
                             else name, data["tools"], problems)
    access = Access()
    if "access" in data:
        # Whether anything uses it is `validate`'s question (_code_files),
        # since an MCP server, which mcp.json declares, uses it too.
        access = _parse_access(data["access"], problems)

    if problems:
        raise PluginFormatError(problems)
    return Manifest(name=data["name"], version=version,
                    description=description, authors=authors,
                    license=license_id, maintainers=maintainers,
                    min_harness=min_harness, settings=tuple(settings),
                    tools=tools, access=access)


def _line_person(value) -> bool:
    return _line(value, MAX_PERSON_CHARS) is not None


def _handle(value) -> bool:
    return isinstance(value, str) and _HANDLE.fullmatch(value) is not None


def _people(data: dict, key: str, ok, problems: list, *, required: bool) -> tuple:
    if key not in data:
        return ()
    value = data[key]
    what = ("names, each one line of at most 100 characters"
            if key == "authors" else "GitHub usernames")
    if (not isinstance(value, list) or (required and not value)
            or len(value) > MAX_PEOPLE or not all(ok(v) for v in value)):
        problems.append(f"{key}: a list of {'1' if required else '0'} to "
                        f"{MAX_PEOPLE} {what}")
        return ()
    return tuple(value)


# ---- the files -----------------------------------------------------------

def _component_ok(part: str) -> bool:
    return (len(part) <= MAX_COMPONENT_CHARS
            and _COMPONENT.fullmatch(part) is not None
            and not part.endswith(".")
            and part.split(".")[0].casefold() not in _WINDOWS_RESERVED)


def check_files(sizes: dict) -> list:
    """The problems with a plugin's files, given each one's size by its
    path inside the plugin folder (posix separators).

    BY SIZE ALONE, so the marketplace refuses a plugin from GitHub's tree
    listing before downloading any of it (PL12).
    """
    problems = []
    if len(sizes) > MAX_FILES:
        problems.append(f"{len(sizes)} files; a plugin holds at most "
                        f"{MAX_FILES}")
    total = sum(sizes.values())
    if total > MAX_TOTAL_BYTES:
        problems.append(f"{total:,} bytes; a plugin holds at most "
                        f"{MAX_TOTAL_BYTES:,}")
    seen = {}
    for path in sorted(sizes):
        parts = path.split("/")
        if (len(path) > MAX_PATH_CHARS
                or not all(_component_ok(part) for part in parts)):
            problems.append(
                f"{path!r}: not an allowed file name -- letters, digits, "
                f"'.', '_' and '-', not starting with '.', not ending with "
                f"'.', and not a name Windows reserves, such as CON or NUL")
            continue
        if any(part.endswith(SUFFIX) for part in parts):
            problems.append(f"{path!r}: a plugin cannot hold another plugin")
            continue
        folded = path.casefold()
        if folded in seen:
            problems.append(f"{path!r} and {seen[folded]!r} differ only in "
                            f"case, which one folder on Windows cannot hold")
            continue
        seen[folded] = path
        if sizes[path] > MAX_FILE_BYTES:
            problems.append(f"{path!r}: {sizes[path]:,} bytes; one file holds "
                            f"at most {MAX_FILE_BYTES:,}")
        top = parts[0]
        if top not in TOP_LEVEL:
            problems.append(f"{path!r}: {top!r} is not part of the plugin "
                            f"format")
        elif top in (MANIFEST, README, LICENSE, REQUIREMENTS, MCP_JSON):
            if len(parts) > 1:
                problems.append(f"{path!r}: {top} is a file, not a folder")
        elif len(parts) == 1:
            problems.append(f"{path!r}: {top}/ is a folder")
        elif top == "skills" and not path.endswith(".md"):
            problems.append(f"{path!r}: skills/ holds .md files")
        elif top == "agents" and (len(parts) != 2 or not path.endswith(".md")):
            problems.append(f"{path!r}: agents/ holds .md files, with no "
                            f"folders inside it")
        elif top == "tools" and not path.endswith(".py"):
            # Code only. Data a tool reads ships inside a .py file or comes
            # from the workspace; a binary here is what a reviewer cannot
            # read, and slice 4's scanner refuses one outright.
            problems.append(f"{path!r}: tools/ holds .py files")
        elif top == SERVERS and not path.endswith(".py"):
            # tools/'s rule, for its reason. Folders are allowed here: a
            # server is a module and may be a package.
            problems.append(f"{path!r}: {SERVERS}/ holds .py files")
    for required in (MANIFEST, README):
        if required not in sizes:
            problems.append(f"no {required}")
    return problems


def validate(name: str, files: dict) -> Manifest:
    """The manifest of the plugin called `name`, whose files are `files`
    ({path: bytes}), after every check this module owns. Raises
    PluginFormatError with every problem."""
    problems = check_files({path: len(data) for path, data in files.items()})
    if not valid_name(name):
        problems.insert(0, f"{name + SUFFIX!r}: the folder name must be "
                           f"lowercase letters, digits and single hyphens")
    manifest, requirements, servers = None, (), ()
    text_files = [path for path in files
                  if path in (MANIFEST, README, REQUIREMENTS, MCP_JSON)
                  or path.endswith((".md", ".py"))]
    for path in sorted(text_files):
        try:
            text = files[path].decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{path!r}: not UTF-8 text")
            continue
        if path == MANIFEST:
            try:
                manifest = parse_manifest(text.lstrip(BOM), name)
            except PluginFormatError as exc:
                problems.extend(exc.problems)
        elif path == REQUIREMENTS:
            try:
                requirements = parse_requirements(text.lstrip(BOM))
            except PluginFormatError as exc:
                problems.extend(exc.problems)
        elif path == MCP_JSON:
            try:
                servers = parse_mcp_json(text.lstrip(BOM), name)
            except PluginFormatError as exc:
                problems.extend(exc.problems)
    if manifest is not None:
        problems.extend(_code_files(manifest, files, servers))
    if problems:
        raise PluginFormatError(problems)
    return replace(manifest, requirements=requirements, servers=servers)


BOM = chr(0xFEFF)


def _code_files(manifest: Manifest, files, servers: tuple) -> list:
    """What the manifest, mcp.json and the files must agree on (PL20,
    PL22, PL27-PL29): each declared tool's code is there, no code sits
    there for nothing, each server's module is the plugin's or its lock's,
    and dependencies and access come with something to use them. Tool
    names are one namespace across tools and servers."""
    problems = []
    code = [path for path in files if path.startswith("tools/")]
    for tool in manifest.tools:
        if tool.path not in files:
            problems.append(f"tools.{tool.name}: its code, {tool.path}, is "
                            f"not in the plugin")
    if code and not manifest.tools:
        problems.append(f"tools/ holds code, but {MANIFEST} declares no "
                        f"tools; declare them under tools:")
    shipped = [path for path in files if path.startswith(SERVERS + "/")]
    for server in servers:
        top = server.module.split(".")[0]
        if (f"{SERVERS}/{top}.py" in files
                or any(p.startswith(f"{SERVERS}/{top}/") for p in shipped)):
            continue
        if REQUIREMENTS not in files:
            problems.append(f"mcpServers.{server.name}: runs the module "
                            f"{server.module}, which is not in {SERVERS}/, "
                            f"and there is no {REQUIREMENTS} to install it "
                            f"from")
    if shipped and not servers:
        problems.append(f"{SERVERS}/ holds code, but there is no {MCP_JSON} "
                        f"naming a server to run it")
    anything = manifest.tools or servers
    if REQUIREMENTS in files and not anything:
        problems.append(f"{REQUIREMENTS}: dependencies are for a plugin's "
                        f"tools and MCP servers, and this plugin declares "
                        f"none")
    if manifest.access.asks and not anything:
        problems.append("access: is what a plugin's tools and MCP servers "
                        "may touch, and this plugin declares no tools and "
                        "no MCP servers")
    # PL28: one namespace. Slice 2's tools are lowercase and already
    # unique among themselves; a server's may be camelCase, and two names
    # one letter's case apart are one name to a reader.
    taken = {t.name.casefold(): f"tools.{t.name}" for t in manifest.tools}
    count = len(manifest.tools)
    for server in servers:
        for tool in server.tools:
            count += 1
            where = f"mcpServers.{server.name}.tools.{tool.name}"
            folded = tool.name.casefold()
            if folded in taken:
                problems.append(f"{where}: the name is already "
                                f"{taken[folded]}; names are compared "
                                f"ignoring case")
            else:
                taken[folded] = where
    if count > MAX_TOOLS:
        problems.append(f"{count} tools in all; a plugin adds at most "
                        f"{MAX_TOOLS}, its own and its servers' together")
    return problems


def read_installed(root: str, name: str) -> Manifest:
    """The manifest of the plugin installed at `root`, with its
    requirements and servers, read from disk. PluginFormatError or OSError
    otherwise. Not `validate`: the files were validated at install, and
    PL8's hashes say whether they still are those files."""
    with open(f"{root}/{MANIFEST}", encoding="utf-8-sig") as handle:
        manifest = parse_manifest(handle.read(), name)
    requirements, servers = (), ()
    try:
        with open(f"{root}/{REQUIREMENTS}", encoding="utf-8-sig") as handle:
            requirements = parse_requirements(handle.read())
    except FileNotFoundError:
        pass
    try:
        with open(f"{root}/{MCP_JSON}", encoding="utf-8-sig") as handle:
            servers = parse_mcp_json(handle.read(), name)
    except FileNotFoundError:
        pass
    return replace(manifest, requirements=requirements, servers=servers)
