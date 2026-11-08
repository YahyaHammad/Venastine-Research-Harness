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

import math
import re
import unicodedata
from dataclasses import dataclass
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

#: The top level of a plugin, and nothing else may sit there (PL3).
TOP_LEVEL = frozenset({MANIFEST, README, LICENSE, "skills", "agents", "tests"})

#: PL5. What later slices of §94 run, by the top-level name that carries
#: it. A plugin holding any of them is refused by this version, before the
#: trust modal, rather than installed with half of what it is.
NOT_YET = {
    "tools": "tools",
    "mcp.json": "MCP servers",
    "requirements.txt": "Python dependencies",
}

_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_HANDLE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
_LICENSE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}")
MAX_DESCRIPTION_CHARS = 200
MAX_PEOPLE = 20
MAX_PERSON_CHARS = 100

_FIELDS = ("format", "name", "version", "description", "authors",
           "maintainers", "license", "min_harness", "settings")
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


# ---- the manifest --------------------------------------------------------

@dataclass(frozen=True)
class Manifest:
    """A plugin's `plugin.yaml`, validated (PL2)."""

    name: str
    version: str
    description: str
    authors: tuple
    license: str
    maintainers: tuple = ()
    min_harness: Optional[str] = None
    settings: tuple = ()

    def setting(self, key: str) -> Optional[Setting]:
        return next((s for s in self.settings if s.key == key), None)


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

    if problems:
        raise PluginFormatError(problems)
    return Manifest(name=data["name"], version=version,
                    description=description, authors=authors,
                    license=license_id, maintainers=maintainers,
                    min_harness=min_harness, settings=tuple(settings))


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
        elif top in (MANIFEST, README, LICENSE):
            if len(parts) > 1:
                problems.append(f"{path!r}: {top} is a file, not a folder")
        elif len(parts) == 1:
            problems.append(f"{path!r}: {top}/ is a folder")
        elif top == "skills" and not path.endswith(".md"):
            problems.append(f"{path!r}: skills/ holds .md files")
        elif top == "agents" and (len(parts) != 2 or not path.endswith(".md")):
            problems.append(f"{path!r}: agents/ holds .md files, with no "
                            f"folders inside it")
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
    manifest = None
    text_files = [path for path in files
                  if path in (MANIFEST, README) or path.endswith(".md")]
    for path in sorted(text_files):
        try:
            text = files[path].decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{path!r}: not UTF-8 text")
            continue
        if path == MANIFEST:
            try:
                manifest = parse_manifest(text.lstrip("\ufeff"), name)
            except PluginFormatError as exc:
                problems.extend(exc.problems)
    if problems:
        raise PluginFormatError(problems)
    if later:
        raise NeedsLaterVersion(later)
    return manifest
