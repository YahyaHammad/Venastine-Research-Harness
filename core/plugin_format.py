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

#: The top level of a plugin, and nothing else may sit there (PL3).
TOP_LEVEL = frozenset({MANIFEST, README, LICENSE, REQUIREMENTS, "skills",
                       "agents", "tools", "tests"})

#: PL5. What later slices of §94 run, by the top-level name that carries
#: it. A plugin holding any of them is refused by this version, before the
#: trust modal, rather than installed with half of what it is. Slice 2
#: took tools and dependencies off this list.
NOT_YET = {
    "mcp.json": "MCP servers",
}

#: PL20. Every tool a plugin adds is `plugin__<plugin>__<tool>`. A plugin
#: name never holds `_` and a tool name never holds `__`, so unlike
#: `mcp__<server>__<tool>` this prefix CAN be taken apart again.
TOOL_PREFIX = "plugin__"
_TOOL_NAME = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*")
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


class NeedsLaterVersion(PluginFormatError):
    """A well-formed plugin holding something this version does not run
    (PL5). `features` names them, for the sentence that refuses it."""

    def __init__(self, features):
        self.features = list(features)
        super().__init__([f"ships {', '.join(self.features)}"])


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

def _invisible(text: str) -> bool:
    """Control, format (bidi overrides, zero-width marks) and line or
    paragraph separators. Each one makes what a reviewer reads and what a
    reader prints differ, so no field of the manifest may hold one."""
    return any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")
               for ch in text)


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
    if not sep or not valid_name(plugin) or not _TOOL_NAME.fullmatch(tool):
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
    return not any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")
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
        why = None
        if not isinstance(schema, dict):
            why = "must be a mapping"
        elif schema.get("type") != "object":
            why = "must have type: object, as every tool's parameters do"
        elif "properties" in schema and not isinstance(
                schema["properties"], dict):
            why = "its properties must be a mapping"
        else:
            why = _json_like(schema)
            if why is None:
                size = len(json.dumps(schema))
                if size > MAX_TOOL_SCHEMA_CHARS:
                    why = (f"is {size} characters as JSON, and a tool's "
                           f"schema is at most {MAX_TOOL_SCHEMA_CHARS}")
        if why:
            problems.append(f"{where}.input_schema: {why}")
            continue
        if description is not None:
            out.append(ToolDef(name=name, description=description,
                               input_schema=schema))
    return tuple(out)


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


# ---- the manifest --------------------------------------------------------

@dataclass(frozen=True)
class Manifest:
    """A plugin's `plugin.yaml`, validated (PL2). `requirements` is
    `requirements.txt`'s, when `validate` or `read_installed` read one."""

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

    def setting(self, key: str) -> Optional[Setting]:
        return next((s for s in self.settings if s.key == key), None)

    def tool(self, name: str) -> Optional[ToolDef]:
        return next((t for t in self.tools if t.name == name), None)


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


def load_yaml(text: str):
    """`text` parsed by the strict loader. yaml.YAMLError otherwise."""
    return yaml.load(text, Loader=_StrictLoader)  # nosec B506 - _StrictLoader is a SafeLoader subclass


def parse_manifest(text: str, name: str) -> Manifest:
    """`plugin.yaml`'s text, for the plugin whose folder is `name` +
    SUFFIX. PluginFormatError listing every problem otherwise."""
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
        elif data["name"] != name:
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
        access = _parse_access(data["access"], problems)
        if "tools" not in data and access.asks:
            problems.append("access: is what a plugin's tools may touch, and "
                            "this plugin declares no tools")

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


def check_files(sizes: dict) -> tuple:
    """(problems, later) for a plugin's files, given each one's size by
    its path inside the plugin folder (posix separators).

    BY SIZE ALONE, so the marketplace refuses a plugin from GitHub's tree
    listing before downloading any of it (PL12). `later` names what PL5
    holds back: the features of later slices this plugin carries.
    """
    problems, later = [], []
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
        if top in NOT_YET:
            later.append(top)
            continue
        if top not in TOP_LEVEL:
            problems.append(f"{path!r}: {top!r} is not part of the plugin "
                            f"format")
        elif top in (MANIFEST, README, LICENSE, REQUIREMENTS):
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
    for required in (MANIFEST, README):
        if required not in sizes:
            problems.append(f"no {required}")
    # In NOT_YET's order, the order the slices that run them come in.
    return problems, [what for top, what in NOT_YET.items() if top in later]


def validate(name: str, files: dict) -> Manifest:
    """The manifest of the plugin called `name`, whose files are `files`
    ({path: bytes}), after every check this module owns. Raises
    PluginFormatError with every problem, or NeedsLaterVersion when the
    plugin is valid and carries something PL5 holds back."""
    problems, later = check_files({path: len(data) for path, data in files.items()})
    if not valid_name(name):
        problems.insert(0, f"{name + SUFFIX!r}: the folder name must be "
                           f"lowercase letters, digits and single hyphens")
    manifest, requirements = None, ()
    text_files = [path for path in files
                  if path in (MANIFEST, README, REQUIREMENTS)
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
    if manifest is not None:
        problems.extend(_tool_files(manifest, files, requirements))
    if problems:
        raise PluginFormatError(problems)
    if later:
        raise NeedsLaterVersion(later)
    return replace(manifest, requirements=requirements)


BOM = chr(0xFEFF)


def _tool_files(manifest: Manifest, files, requirements) -> list:
    """What the manifest's tools and the files must agree on (PL20, PL22):
    each declared tool's code is there, no code sits there for a tool
    nobody declared, and dependencies come with tools to use them."""
    problems = []
    code = [path for path in files if path.startswith("tools/")]
    for tool in manifest.tools:
        if tool.path not in files:
            problems.append(f"tools.{tool.name}: its code, {tool.path}, is "
                            f"not in the plugin")
    if code and not manifest.tools:
        problems.append(f"tools/ holds code, but {MANIFEST} declares no "
                        f"tools; declare them under tools:")
    if REQUIREMENTS in files and not manifest.tools:
        problems.append(f"{REQUIREMENTS}: dependencies are for a plugin's "
                        f"tools, and this plugin declares none")
    return problems


def read_installed(root: str, name: str) -> Manifest:
    """The manifest of the plugin installed at `root`, with its
    requirements, read from disk. PluginFormatError or OSError otherwise.
    Not `validate`: the files were validated at install, and PL8's hashes
    say whether they still are those files."""
    with open(f"{root}/{MANIFEST}", encoding="utf-8-sig") as handle:
        manifest = parse_manifest(handle.read(), name)
    requirements = ()
    try:
        with open(f"{root}/{REQUIREMENTS}", encoding="utf-8-sig") as handle:
            requirements = parse_requirements(handle.read())
    except FileNotFoundError:
        pass
    return replace(manifest, requirements=requirements)
