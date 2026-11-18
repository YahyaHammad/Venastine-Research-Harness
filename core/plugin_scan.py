"""
core/plugin_scan.py

ROADMAP_v3 §94, slice 4 (PL33, PL36). What a reviewer should look at in a
plugin, found by reading its files and never by running them. The
marketplace's review workflow runs it on every pull request that touches
`plugins/`, and the harness runs it on every install, so a plugin from
`--from` -- which no CI has seen -- is scanned on the machine it is about
to run on.

    python -m core.plugin_scan path/to/name.plugin [--online] [--json]

It READS, it never runs: Python is parsed with `ast`, text is matched, and
the lock is looked up. That is why the review workflow can run it with a
token that writes labels and comments, on files fetched as data: nothing
here imports, executes or unpickles anything a plugin ships.

WHAT FAILS AND WHAT IS LISTED (owner, batch 252). A HIGH finding fails the
marketplace check until someone with write access adds the
`plugin:scan-reviewed` label, and is shown in capitals in a `--from`
install's modal, where you still decide. A NOTE is listed for the reviewer
and fails nothing.

  high  text that hides what it says: bidi, zero-width, tag and other
        format characters, control characters, and files that are not text
  high  code that runs what it builds: exec, eval and compile, a decode
        feeding one of them, and pickle or marshal loads; a builtin or the
        interpreter's internals reached by a name the code spells at run
        time (operator.attrgetter, vars(builtins), types.FunctionType,
        __globals__ ...), and a string a module is imported by
  high  a Python file that declares an encoding other than UTF-8, which
        Python reads differently from this scanner (the format refuses it)
  high  code or text that reaches for credentials: credential paths
        (providers.json, ~/.ssh, ~/.aws ...), joined or concatenated ones
        too, and key-like environment names
  high  a network module imported by a plugin whose manifest says
        `network: false`
  high  text telling the model to ignore its rules, turn off approval or
        exfiltrate, and `curl | sh`
  high  a locked package with a known vulnerability (OSV), one edit from a
        popular package's name, a pinned file uploaded under 14 days ago,
        no pinned wheel, a pinned hash PyPI does not list, or not on PyPI
  note  subprocess, ctypes, dynamic imports, the whole environment read,
        HTML comments in Markdown, and long runs of encoded-looking data

The text checks read what the model reads: a skill's sentence wrapped over
lines is one sentence, and plugin.yaml and mcp.json are read as their
parsed strings, after YAML's and JSON's escapes, as well as line by line.

WHAT IS LISTED. A modal and a pull request comment list at most `LISTED`
findings. `listed` chooses them so a reader sees every HIGH rule in every
file -- code files first -- before any rule repeats, and lets the list grow
to `LISTED_CEILING` when that alone needs more room (owner, batch 272). A
cap sorted by path let forty findings in skills push a `tools/` exec off
the end, where `plugin:scan-reviewed` then let it through unread.

What the format already refuses -- links, hidden and oversized files,
nested plugins, unpinned or URL dependencies, npx/uvx and remote servers --
is `plugin_format.validate`'s, run first by everything that runs this.

THE NETWORK HALF (`online`) asks PyPI's JSON API about each locked release
and OSV's batch API about known vulnerabilities: one request a package and
one in all. A lookup that fails is not a pass: it is listed as unchecked,
and the review workflow fails on it.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import datetime as _dt
import json
import math
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

import yaml

from core import ansi, plugin_format

HIGH = "high"
NOTE = "note"

#: A release younger than this is a finding: the window in which a
#: hijacked or malicious upload is usually caught. This repository holds
#: its own dependencies to the same rule.
FRESH_DAYS = 14
#: The longest run of encoded-looking characters that is not a finding.
BLOB_CHARS = 200
#: Bits per character above which such a run looks encoded rather than
#: written. Hex is at most 4, base64 at most 6; English text is near 4.1
#: only because of its spaces, which a run excludes.
BLOB_ENTROPY = 3.5
#: Findings of one rule in one file that are listed; the rest are counted.
PER_FILE = 5
#: Findings a modal or a review comment lists, and the most it grows to so
#: every HIGH rule in every file has a line (`listed`).
LISTED = 40
LISTED_CEILING = 200
#: What a count row ("and N more like ...") names of what it folds.
FOLDED_LINES = 8
FOLDED_DETAILS = 3
#: What the online checks may take.
TIMEOUT_S = 15.0
MAX_PYPI_BYTES = 8_000_000
MAX_OSV_BYTES = 4_000_000
PYPI = "https://pypi.org/pypi"
OSV = "https://api.osv.dev/v1/querybatch"
USER_AGENT = "venastine-plugin-scan"


@dataclass(frozen=True)
class Finding:
    severity: str        # HIGH or NOTE
    rule: str            # a short slug, e.g. "exec"
    path: str            # inside the plugin; "" for the plugin as a whole
    line: int            # 0 when not tied to a line
    message: str
    #: How many findings this row stands for: 0 for a finding, N for the
    #: "and N more like ..." row that folds N of one rule in one file.
    count: int = 0

    @property
    def where(self) -> str:
        if not self.path:
            return "the plugin"
        return f"{self.path}:{self.line}" if self.line else self.path

    def shown(self) -> str:
        return f"{self.where}: {self.message}"


@dataclass
class Report:
    findings: list = field(default_factory=list)
    #: Why an online check did not run, one line each. Empty when every
    #: lookup answered, or when `online` was not asked for.
    unchecked: list = field(default_factory=list)
    online: bool = False

    @property
    def high(self) -> list:
        return [f for f in self.findings if f.severity == HIGH]

    @property
    def notes(self) -> list:
        return [f for f in self.findings if f.severity == NOTE]

    def summary(self) -> str:
        high, notes = len(self.high), len(self.notes)
        if not self.findings:
            return "nothing to point out"
        parts = []
        if high:
            parts.append(f"{high} high")
        if notes:
            parts.append(f"{notes} note{'' if notes == 1 else 's'}")
        return ", ".join(parts)


# ---- what counts as text --------------------------------------------------

#: Characters allowed although their category would hide them.
_LAYOUT = frozenset({9, 10, 12, 13})  # tab, newline, form feed, return
#: Unicode's variation selectors supplement: invisible, and used to smuggle
#: ASCII past a reader one selector per byte.
_SMUGGLING = range(0xE0100, 0xE01F0)
_HIDDEN_CATEGORIES = ("Cc", "Cf", "Co", "Cs", "Zl", "Zp")


def _hidden(char: str) -> bool:
    code = ord(char)
    if code in _LAYOUT:
        return False
    # ansi.BLANK since batch 273: a Hangul filler, a braille blank or a
    # combining grapheme joiner draws nothing whatever its category says.
    return (unicodedata.category(char) in _HIDDEN_CATEGORIES
            or code in _SMUGGLING or code in ansi.BLANK)


def _codepoint(char: str) -> str:
    name = unicodedata.name(char, "")
    return f"U+{ord(char):04X}" + (f" {name}" if name else "")


def _decoded(data: bytes) -> Optional[str]:
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ---- what the text says ---------------------------------------------------

_WORDS = re.IGNORECASE
_INSTRUCTIONS = (
    ("ignore-rules", re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}?"
        r"\b(?:previous|prior|above|earlier|preceding|system|safety|all|"
        r"any|your|the|these|those)\b[^.\n]{0,30}?"
        r"\b(?:instructions?|rules?|prompts?|guidelines|directions|"
        r"guardrails|policies|policy)\b", _WORDS),
     "tells the model to ignore its rules"),
    ("disable-approval", re.compile(
        r"\b(?:turn\s+off|disable|bypass|skip|avoid|suppress|switch\s+off)"
        r"\b[^.\n]{0,30}?\b(?:approvals?|confirmations?|confirm\s+prompts?|"
        r"permission\s+(?:prompts?|checks?)|the\s+user'?s?\s+"
        r"(?:approval|confirmation|consent))\b"
        r"|/plugin\s+approval\s+\S+\s+auto\b|\bauto_?approve\b", _WORDS),
     "tells the model or the user to turn approval off"),
    ("exfiltrate", re.compile(
        r"\bexfiltrat\w*"
        r"|\b(?:send|post|upload|leak|transmit|forward|e-?mail|paste)\b"
        r"[^.\n]{0,60}?\b(?:api[\s_-]?keys?|credentials?|secrets?|"
        r"passwords?|tokens?|providers\.json|ssh\s+keys?|private\s+keys?|"
        r"cookies|\.env)(?![\w-])", _WORDS),
     "tells the model to send credentials or secrets somewhere"),
    ("curl-sh", re.compile(
        r"\b(?:curl|wget)\b[^\n|]{0,200}\|\s*(?:sudo\s+)?(?:ba|z|da|k|fi)?sh\b"
        r"|\b(?:iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]{0,200}"
        r"\|\s*(?:iex|invoke-expression)\b", _WORDS),
     "pipes a download into a shell"),
)

#: Credential locations, matched in code's strings and in the text the
#: model reads. Lowercase; `\` is compared as `/`.
_CREDENTIAL_MARKERS = (
    "providers.json", ".ssh/", "/.ssh", "~/.ssh", "id_rsa", "id_ed25519",
    "id_ecdsa", "id_dsa", ".aws/credentials", ".aws/config", ".netrc",
    "_netrc", ".git-credentials", ".pypirc", ".npmrc", ".docker/config.json",
    ".kube/config", ".gnupg", ".config/venastine", ".config/gh/hosts.yml",
    "login data", "cookies.sqlite", "key4.db", "logins.json",
    ".azure/", "application_default_credentials.json", ".claude/",
    ".codex/auth.json", "keychain",
)
_DOTENV = re.compile(r"(?:^|[\\/])\.env(?:\.[\w-]+)?$")

#: Environment names that hold credentials, split on `_`.
_KEY_SEGMENTS = frozenset({
    "KEY", "KEYS", "APIKEY", "TOKEN", "TOKENS", "SECRET", "SECRETS",
    "PASSWORD", "PASSWD", "PASSPHRASE", "CREDENTIAL", "CREDENTIALS", "AUTH",
    "PAT", "PRIVATE", "COOKIE", "SESSION",
})


def key_like(name: str) -> bool:
    """Whether an environment variable's name says it holds a credential."""
    return any(part in _KEY_SEGMENTS
               for part in re.split(r"[^A-Z0-9]+", name.upper()) if part)


def _credential_marker(text: str) -> Optional[str]:
    folded = text.replace("\\", "/").lower()
    for marker in _CREDENTIAL_MARKERS:
        if marker in folded:
            return marker.strip("/")
    if _DOTENV.search(folded.strip()):
        return ".env"
    return None


_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{%d,}" % (BLOB_CHARS + 1))


def _entropy(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _shown(text: str, most: int = 80) -> str:
    """A matched phrase, safe to print: one line, nothing hidden, short."""
    flat = " ".join(text.split())
    flat = "".join(_codepoint(c) if _hidden(c) else c for c in flat)
    return flat if len(flat) <= most else flat[:most - 3] + "..."


# ---- code -----------------------------------------------------------------

_BUILTINS = {"exec": "builtins.exec", "eval": "builtins.eval",
             "compile": "builtins.compile", "__import__": "builtins.__import__",
             "getattr": "builtins.getattr", "dict": "builtins.dict",
             "vars": "builtins.vars", "globals": "builtins.globals",
             "locals": "builtins.locals"}
_EXEC = frozenset({"builtins.exec", "builtins.eval", "builtins.compile"})
_LOADS = frozenset({
    "pickle.load", "pickle.loads", "pickle.Unpickler", "_pickle.load",
    "_pickle.loads", "_pickle.Unpickler", "marshal.load", "marshal.loads",
    "dill.load", "dill.loads", "cloudpickle.load", "cloudpickle.loads",
    "shelve.open", "joblib.load", "jsonpickle.decode", "pandas.read_pickle",
})
_DECODERS = frozenset({
    "codecs.decode", "zlib.decompress", "gzip.decompress", "bz2.decompress",
    "lzma.decompress", "binascii.unhexlify", "binascii.a2b_base64",
    "binascii.a2b_hex", "bytes.fromhex", "base64.decodebytes",
})
_DECODER_METHODS = frozenset({"decode", "fromhex", "decompress",
                              "b64decode", "b32decode", "b16decode",
                              "a85decode", "b85decode", "urlsafe_b64decode",
                              "unhexlify"})
_SUBPROCESS_PREFIXES = ("subprocess.", "os.exec", "os.spawn", "os.posix_spawn")
_SUBPROCESS = frozenset({"os.system", "os.popen", "pty.spawn",
                         "asyncio.create_subprocess_exec",
                         "asyncio.create_subprocess_shell",
                         "commands.getoutput", "commands.getstatusoutput"})
_DYNAMIC_IMPORT = frozenset({
    "builtins.__import__", "importlib.import_module", "importlib.__import__",
    "importlib.util.spec_from_file_location",
    "importlib.util.module_from_spec", "importlib.machinery.SourceFileLoader",
    "runpy.run_path", "runpy.run_module", "imp.load_source",
    "imp.load_module", "zipimport.zipimporter",
})
_NATIVE = ("ctypes", "_ctypes", "cffi")
#: Modules whose import means talking to the network. Protocol libraries and
#: the common HTTP clients; an SDK that wraps one of these imports it too,
#: inside the lock rather than here, and the walls (PL25) refuse its
#: sockets either way.
_NETWORK = (
    "socket", "_socket", "ssl", "http.client", "http.server", "urllib.request",
    "urllib3", "requests", "httpx", "aiohttp", "websockets", "websocket",
    "ftplib", "smtplib", "poplib", "imaplib", "nntplib", "telnetlib",
    "xmlrpc.client", "xmlrpc.server", "socketserver", "paramiko", "grpc",
    "pycurl", "httplib2", "tornado.httpclient", "asyncssh", "scapy",
)
_NETWORK_CALLS = frozenset({"asyncio.open_connection", "asyncio.start_server",
                            "asyncio.open_unix_connection"})
_ENV_READS = frozenset({"os.getenv", "os.environ.get", "os.environ.pop",
                        "os.getenvb", "os.environb.get"})
_WHOLE_ENV = frozenset({"os.environ.copy", "os.environ.items",
                        "os.environ.values", "os.environ.keys",
                        "os.environb.copy", "os.environb.items"})


#: Names that, reached through a string, are a way to run code: the exec
#: builtins, the importer, and the interpreter's own internals.
_REACHED = frozenset({"exec", "eval", "compile", "__import__", "__builtins__",
                      "system", "popen", "__globals__", "__code__",
                      "__subclasses__", "__loader__"})
#: Constructors of functions and code objects: code built from parts.
_CODE_OBJECTS = frozenset({"types.FunctionType", "types.LambdaType",
                           "types.CodeType", "types.MethodType"})
#: Attributes that reach past a function to the interpreter.
_INTERNALS = frozenset({"__globals__", "__code__", "__subclasses__",
                        "__builtins__"})
#: Calls a reached name comes back from.
_GETTERS = frozenset({"operator.attrgetter", "operator.methodcaller",
                      "operator.itemgetter"})
#: Calls that join a path's parts, and the one that expands a home.
_JOINS = frozenset({"os.path.join", "posixpath.join", "ntpath.join",
                    "pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath",
                    "pathlib.WindowsPath", "pathlib.PurePosixPath",
                    "pathlib.PureWindowsPath", "os.path.expanduser",
                    "os.path.expandvars", "os.path.normpath",
                    "os.path.abspath"})
_IMPORTERS = frozenset({"builtins.__import__", "importlib.import_module",
                        "importlib.__import__"})


def _const(node) -> Optional[str]:
    """The string `node` always evaluates to, when constants alone build
    it: a literal, `+` of two, an f-string of literals, or a literal
    separator's `.join` of literals. Else None."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        if isinstance(node.value, bytes):
            return node.value.decode("latin-1")
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _const(node.left), _const(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = [_const(v) for v in node.values]
        return "".join(parts) if all(p is not None for p in parts) else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join" and len(node.args) == 1
            and isinstance(node.args[0], (ast.List, ast.Tuple))):
        sep = _const(node.func.value)
        parts = [_const(e) for e in node.args[0].elts]
        if sep is not None and all(p is not None for p in parts):
            return sep.join(parts)
    return None


def _network_module(name: str) -> Optional[str]:
    for module in _NETWORK:
        if name == module or name.startswith(module + "."):
            return module
    return None


class _Code(ast.NodeVisitor):
    """One Python file's findings. Names are resolved through the file's
    own imports, so `from pickle import loads as l; l(x)` is pickle.loads,
    and `re.compile` is not the builtin."""

    def __init__(self, path: str, network: bool, add) -> None:
        self.path = path
        self.network = network
        self.add = add
        self.aliases: dict = {}
        self._called: set = set()

    # -- names --------------------------------------------------------------

    def _qualified(self, node) -> Optional[str]:
        """The dotted name `node` reaches, through the file's imports and
        through the strings that name things at run time: `__import__('os')`
        is os, `getattr(builtins, 'exec')` is builtins.exec, and
        `vars(builtins)['exec']` is too."""
        if isinstance(node, ast.Name):
            if node.id in self.aliases:
                return self.aliases[node.id]
            if node.id == "__builtins__":
                return "builtins"
            return _BUILTINS.get(node.id)
        if isinstance(node, ast.Attribute):
            if node.attr == "__dict__":
                return self._qualified(node.value)
            base = self._qualified(node.value)
            return f"{base}.{node.attr}" if base else None
        if isinstance(node, ast.Call) and node.args:
            func = self._qualified(node.func)
            named = _const(node.args[0])
            if func in _IMPORTERS and named:
                # __import__('a.b') gives back a; import_module gives a.b.
                return (named.split(".")[0] if func == "builtins.__import__"
                        else named)
            if func == "builtins.getattr" and len(node.args) >= 2:
                attr = _const(node.args[1])
                base = self._qualified(node.args[0])
                return f"{base}.{attr}" if base and attr else None
            if (func in ("builtins.vars", "builtins.dict")
                    and len(node.args) == 1):
                return self._qualified(node.args[0])
            return None
        if isinstance(node, ast.Call) and not node.args:
            if self._qualified(node.func) in ("builtins.globals",
                                              "builtins.locals",
                                              "builtins.vars"):
                return "<namespace>"
            return None
        if isinstance(node, ast.Subscript):
            key = _const(node.slice)
            base = self._qualified(node.value)
            if key is None or base is None:
                return None
            if base == "<namespace>":
                # globals()['exec'] falls through to the builtins.
                return ("builtins" if key == "__builtins__"
                        else _BUILTINS.get(key, f"builtins.{key}"))
            return f"builtins.{key}" if base == "builtins" else None
        return None

    def _path_text(self, node) -> str:
        """A path's text as far as constants spell it, a part they do not
        spell left empty: `Path.home() / '.ssh'` reads `/.ssh`."""
        folded = _const(node)
        if folded is not None:
            return folded
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return f"{self._path_text(node.left)}/{self._path_text(node.right)}"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._path_text(node.left) + self._path_text(node.right)
        if isinstance(node, ast.Call):
            if self._qualified(node.func) in _JOINS:
                return "/".join(self._path_text(a) for a in node.args)
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("joinpath", "expanduser",
                                           "resolve", "absolute")):
                return "/".join([self._path_text(node.func.value),
                                 *(self._path_text(a) for a in node.args)])
        return ""

    def _credential(self, node) -> None:
        marker = _credential_marker(self._path_text(node))
        if marker:
            self.add(HIGH, "credential-path", self.path, node.lineno,
                     f"names a credential location ({marker})")

    def _imported(self, name: str, node) -> None:
        top = name.split(".")[0]
        if top in _NATIVE:
            self.add(NOTE, "ctypes", self.path, node.lineno,
                     f"loads native code ({name})")
        module = _network_module(name)
        if module and not self.network:
            self.add(HIGH, "network", self.path, node.lineno,
                     f"imports {module}, but plugin.yaml says network: false")

    def visit_Import(self, node) -> None:
        for alias in node.names:
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            else:
                top = alias.name.split(".")[0]
                self.aliases[top] = top
            self._imported(alias.name, node)

    def visit_ImportFrom(self, node) -> None:
        module = "" if node.level else (node.module or "")
        for alias in node.names:
            if alias.name == "*":
                continue
            qualified = f"{module}.{alias.name}" if module else alias.name
            if not node.level:
                self.aliases[alias.asname or alias.name] = qualified
            self._imported(qualified, node)
        if module:
            self._imported(module, node)

    # -- calls --------------------------------------------------------------

    def _decodes(self, node) -> Optional[str]:
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            name = self._qualified(inner.func)
            if name and (name in _DECODERS or name.startswith("base64.")):
                return name
            if (isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in _DECODER_METHODS):
                return f".{inner.func.attr}()"
        return None

    def visit_Call(self, node) -> None:
        name = self._qualified(node.func)
        if name is not None:
            self._call(name, node)
        elif (isinstance(node.func, ast.Attribute)
              and node.func.attr in ("join", "joinpath")):
            # ''.join(['provid', 'ers.json']), Path.home().joinpath('.ssh')
            self._credential(node)
        self.generic_visit(node)

    def _call(self, name: str, node) -> None:
        line = node.lineno
        if name in _EXEC or name in _LOADS:
            self._called.add(id(node.func))
            decoder = None
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                decoder = decoder or self._decodes(arg)
            if decoder:
                self.add(HIGH, "decode-exec", self.path, line,
                         f"decodes data and runs or loads it ({decoder} "
                         f"inside {name.split('.', 1)[1]})")
            elif name in _EXEC:
                self.add(HIGH, "exec", self.path, line,
                         f"runs code it builds at run time "
                         f"({name.split('.', 1)[1]})")
            else:
                self.add(HIGH, "unpickle", self.path, line,
                         f"loads serialized objects ({name}), which runs "
                         f"code carried in the data")
        elif name == "builtins.getattr" and len(node.args) >= 2:
            target = self._qualified(node.args[0])
            attr = _const(node.args[1])
            if target in ("builtins", "<namespace>") and (
                    attr is None or attr in _REACHED):
                self.add(HIGH, "exec", self.path, line,
                         "reaches a builtin by name at run time (getattr "
                         "on builtins)")
            elif attr in _REACHED:
                self.add(HIGH, "exec", self.path, line,
                         f"reaches {attr} by name at run time (getattr)")
        elif name in _GETTERS:
            for arg in node.args:
                spelled = _const(arg) or ""
                reached = sorted(set(spelled.split(".")) & _REACHED)
                if reached:
                    self.add(HIGH, "exec", self.path, line,
                             f"reaches {reached[0]} by name at run time "
                             f"({name})")
        elif name in _CODE_OBJECTS:
            self._called.add(id(node.func))
            self.add(HIGH, "exec", self.path, line,
                     f"builds a function from a code object ({name}), which "
                     f"runs code no reader of this file sees")
        elif name in _IMPORTERS and node.args and _const(node.args[0]):
            # A module named by a string the code spells: read it as the
            # import it is, so a network module is the finding it would be.
            self._imported(_const(node.args[0]), node)
        elif name in _DYNAMIC_IMPORT:
            self.add(NOTE, "dynamic-import", self.path, line,
                     f"imports a module named at run time ({name})")
        elif name in _JOINS:
            self._credential(node)
        elif name in _SUBPROCESS or name.startswith(_SUBPROCESS_PREFIXES):
            self.add(NOTE, "subprocess", self.path, line,
                     f"starts another program ({name})")
        elif name in _NETWORK_CALLS and not self.network:
            self.add(HIGH, "network", self.path, line,
                     f"opens a connection ({name}), but plugin.yaml says "
                     f"network: false")
        elif name in _ENV_READS:
            key = _const(node.args[0]) if node.args else None
            if key is not None:
                if key_like(key):
                    self.add(HIGH, "secret-env", self.path, line,
                             f"reads the environment variable "
                             f"{_shown(key, 60)}, whose name says it "
                             f"holds a credential")
            else:
                self.add(NOTE, "whole-env", self.path, line,
                         f"reads an environment variable named at run time "
                         f"({name})")
        elif name in _WHOLE_ENV or (
                name == "builtins.dict" and node.args
                and self._qualified(node.args[0]) in ("os.environ",
                                                      "os.environb")):
            self.add(NOTE, "whole-env", self.path, line,
                     f"reads the whole environment ({name})")

    def visit_Name(self, node) -> None:
        # `f = eval` and the like: the builtin taken, not called here.
        if (isinstance(node.ctx, ast.Load) and node.id not in self.aliases
                and _BUILTINS.get(node.id) in _EXEC
                and id(node) not in self._called):
            self.add(HIGH, "exec", self.path, node.lineno,
                     f"takes the builtin {node.id}, which runs code it is "
                     f"given")
        elif (isinstance(node.ctx, ast.Load)
              and self.aliases.get(node.id) in _CODE_OBJECTS
              and id(node) not in self._called):
            self.add(HIGH, "exec", self.path, node.lineno,
                     f"takes {self.aliases[node.id]}, which builds a "
                     f"function from a code object")

    def visit_Subscript(self, node) -> None:
        if (isinstance(node.ctx, ast.Load)
                and self._qualified(node.value) in ("os.environ",
                                                    "os.environb")):
            key = _const(node.slice)
            if key is not None and key_like(key):
                self.add(HIGH, "secret-env", self.path, node.lineno,
                         f"reads the environment variable "
                         f"{_shown(key, 60)}, whose name says it holds "
                         f"a credential")
            elif key is None:
                self.add(NOTE, "whole-env", self.path, node.lineno,
                         "reads an environment variable named at run time "
                         "(os.environ[...])")
        self.generic_visit(node)

    def visit_Attribute(self, node) -> None:
        name = self._qualified(node)
        if (isinstance(node.ctx, ast.Load) and name in _CODE_OBJECTS
                and id(node) not in self._called):
            self.add(HIGH, "exec", self.path, node.lineno,
                     f"takes {name}, which builds a function from a code "
                     f"object")
        elif node.attr in _INTERNALS:
            self.add(HIGH, "exec", self.path, node.lineno,
                     f"reaches {node.attr}, a way past a function to the "
                     f"interpreter that no reader of this file follows")
        self.generic_visit(node)

    def visit_BinOp(self, node) -> None:
        if isinstance(node.op, (ast.Add, ast.Div)):
            self._credential(node)
        self.generic_visit(node)

    def _assigned(self, value) -> Optional[str]:
        if isinstance(value, ast.IfExp):
            return self._assigned(value.body) or self._assigned(value.orelse)
        return self._qualified(value)

    def visit_Assign(self, node) -> None:
        # `g = getattr` and `b = __builtins__` name what they hold for the
        # rest of the file, as an import does. Read once, in order, so a
        # later use of the name reads as the thing (batch 272).
        reached = self._assigned(node.value)
        if reached:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases[target.id] = reached
        self.generic_visit(node)

    def visit_For(self, node) -> None:
        if self._qualified(node.iter) in ("os.environ", "os.environb"):
            self.add(NOTE, "whole-env", self.path, node.lineno,
                     "reads the whole environment (a loop over os.environ)")
        self.generic_visit(node)

    def visit_Constant(self, node) -> None:
        value = node.value
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if not isinstance(value, str):
            return
        marker = _credential_marker(value)
        if marker:
            self.add(HIGH, "credential-path", self.path, node.lineno,
                     f"names a credential location ({marker})")


def _strings(tree) -> list:
    """(line, text) for every string in a module, docstrings included."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
    return out


# ---- the scan -------------------------------------------------------------

def _kind(path: str) -> int:
    """Where a file's findings come in a list: the code that runs first,
    then the files that say what runs, then the text, then the plugin's
    own tests, which only CI runs."""
    if path.startswith("tests/"):
        return 3
    if path.endswith(".py"):
        return 0
    if path in (plugin_format.MANIFEST, plugin_format.MCP_JSON,
                plugin_format.REQUIREMENTS, ""):
        return 1
    return 2


def _place(f: Finding) -> tuple:
    # A count follows the findings it counts, in its file; a finding of the
    # whole file (line 0) still leads it.
    return ({HIGH: 0, NOTE: 1}[f.severity], _kind(f.path), f.path,
            sys.maxsize if f.count else f.line, f.rule)


class _Collector:
    def __init__(self) -> None:
        self.findings: list = []
        self._seen: set = set()
        self._per: Counter = Counter()
        self._shown: dict = {}
        self._folded: dict = {}

    def __call__(self, severity, rule, path, line, message) -> None:
        key = (rule, path, line, message)
        if key in self._seen:
            return
        self._seen.add(key)
        self._per[(rule, path)] += 1
        if self._per[(rule, path)] > PER_FILE:
            self._folded.setdefault((severity, rule, path), []).append(
                (line, message))
            return
        self._shown.setdefault((rule, path), set()).add(message)
        self.findings.append(Finding(severity, rule, path, line, message))

    def close(self) -> list:
        for (severity, rule, path), folded in sorted(self._folded.items()):
            n = len(folded)
            lines = sorted({line for line, _ in folded if line})
            where = ""
            if lines:
                shown = ", ".join(map(str, lines[:FOLDED_LINES]))
                where = (f" (line{'s' if len(lines) > 1 else ''} {shown}"
                         f"{' ...' if len(lines) > FOLDED_LINES else ''})")
            # What the rows above do not already say: the code point, the
            # name or the phrase a folded finding was about (batch 272).
            new = [m for m in dict.fromkeys(m for _, m in folded)
                   if m not in self._shown.get((rule, path), ())]
            said = ""
            if new:
                said = ": " + "; ".join(_shown(m, 90) for m in
                                        new[:FOLDED_DETAILS])
                if len(new) > FOLDED_DETAILS:
                    said += f"; and {len(new) - FOLDED_DETAILS} more different"
            self.findings.append(Finding(
                severity, rule, path, 0,
                f"and {n} more like the {rule} finding{'s' if n > 1 else ''} "
                f"above{where}{said}", count=n))
        return sorted(self.findings, key=_place)


def listed(findings: list, most: int = LISTED,
           ceiling: int = LISTED_CEILING) -> tuple:
    """(the findings a modal or a comment lists, in order; how many it
    leaves out; how many of those are HIGH). Every HIGH rule in every file
    gets one line before any rule gets a second -- each rule's first file
    leading, code files first -- and the list grows past `most` up to
    `ceiling` when those lines alone need the room (owner, batch 272)."""
    firsts: dict = {}
    for f in findings:
        if f.severity == HIGH and not f.count:
            firsts.setdefault((f.rule, f.path), f)
    rules_first, again, rules = [], [], set()
    for f in firsts.values():
        (again if f.rule in rules else rules_first).append(f)
        rules.add(f.rule)
    lead = rules_first + again
    room = max(most, min(len(lead), ceiling))
    chosen = lead[:room]
    taken = {id(f) for f in chosen}
    for f in findings:
        if len(chosen) >= room:
            break
        if id(f) not in taken:
            chosen.append(f)
            taken.add(id(f))
    chosen.sort(key=_place)
    left = [f for f in findings if id(f) not in taken]
    return chosen, len(left), sum(1 for f in left if f.severity == HIGH)


def left_out(n: int, high: int) -> str:
    """The line a cut list ends with."""
    return (f"... and {n} more finding{'' if n == 1 else 's'}"
            + (f", {high} of them HIGH" if high else ""))


def _model_reads(path: str) -> bool:
    """Whether the harness ever puts this file's text in front of the model
    or the user: everything but the plugin's tests."""
    return not path.startswith("tests/")


def _text(path: str, text: str, add) -> None:
    lines = text.split("\n")
    for number, line in enumerate(lines, 1):
        hidden = [c for c in line if _hidden(c)]
        if number == 1 and line.startswith(plugin_format.BOM):
            hidden = hidden[1:]
        for char in dict.fromkeys(hidden):
            add(HIGH, "hidden-char", path, number,
                f"holds {_codepoint(char)}, which shows differently from "
                f"what a program or the model reads")
        if ansi.selector_run(line):
            # One selector after an emoji is ordinary; a run is bytes.
            add(HIGH, "hidden-char", path, number,
                "holds a run of variation selectors, which draw nothing and "
                "can carry text no reader sees")
        for run in _BLOB.findall(line):
            if _entropy(run) >= BLOB_ENTROPY:
                add(NOTE, "blob", path, number,
                    f"holds a {len(run):,}-character run of encoded-looking "
                    f"data")
    if not _model_reads(path):
        return
    if path.endswith((".md", ".markdown")) and "<!--" in text:
        add(NOTE, "html-comment", path, text[:text.index("<!--")].count("\n") + 1,
            "holds an HTML comment, which a rendered page hides and the "
            "model reads")
    if path in (plugin_format.MANIFEST, plugin_format.MCP_JSON):
        # Line by line, since two lines of YAML or JSON are two fields; and
        # as the parsed strings, which is what the model reads: an escaped
        # dot (backslash, u002e) is a dot to it, and a folded line is one
        # sentence (batch 272). No backslash-u in this file's own text.
        _said(path, [(n, line) for n, line in enumerate(lines, 1)], add)
        _said(path, _parsed_strings(path, text), add)
    elif not path.endswith(".py"):
        _said(path, [(1, text)], add)


def _parsed_strings(path: str, text: str) -> list:
    """(line, string) for every string the file holds, as its parser reads
    it; line 0 where the parser keeps none. Nothing when it does not parse,
    which the format has already said."""
    text = text.lstrip(plugin_format.BOM)
    try:
        if path == plugin_format.MANIFEST:
            return plugin_format.yaml_strings(text)
        out, stack = [], [json.loads(text)]
    except (ValueError, RecursionError, yaml.YAMLError):
        return []
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            out.append((0, value))
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                stack.extend((key, item))
    return out


#: A single line break: what a writer wraps a sentence with. A blank line
#: still ends one. Replacing it with a space keeps every offset.
_WRAP = re.compile(r"(?<!\n)\n(?!\n)")


def _said(path: str, pieces: list, add) -> None:
    """The instruction and credential checks, on (line, text) pieces. A
    piece is read with its wrapped lines joined, so a sentence split over
    two lines is still the sentence (batch 272)."""
    for line, piece in pieces:
        flat = _WRAP.sub(" ", piece)
        for rule, pattern, what in _INSTRUCTIONS:
            for match in pattern.finditer(flat):
                at = line + piece.count("\n", 0, match.start()) if line else 0
                add(HIGH, rule, path, at,
                    f"{what}: \"{_shown(match.group(0))}\"")
        if not path.endswith(".py"):
            for offset, part in enumerate(piece.split("\n")):
                marker = _credential_marker(part)
                if marker:
                    add(HIGH, "credential-path", path,
                        line + offset if line else 0,
                        f"names a credential location ({marker})")


def _python(path: str, text: str, network: bool, add) -> None:
    try:
        tree = ast.parse(text.lstrip(plugin_format.BOM), filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        detail = getattr(exc, "msg", None) or type(exc).__name__
        add(HIGH, "unparsable", path, getattr(exc, "lineno", 0) or 0,
            f"Python {sys.version_info.major}.{sys.version_info.minor} "
            f"cannot parse it ({_shown(str(detail), 60)}), so nothing here "
            f"could read what it does")
        return
    try:
        _Code(path, network, add).visit(tree)
    except RecursionError:
        add(HIGH, "unparsable", path, 0,
            "nests too deeply to read, so nothing here could read what it "
            "does")
        return
    if _model_reads(path):
        # A tool's strings can reach the model as its result.
        _said(path, _strings(tree), add)


# ---- dependencies ---------------------------------------------------------

def _distance_at_most_one(a: str, b: str) -> bool:
    """Whether one insertion, deletion, substitution or swap of neighbours
    turns `a` into `b` (and they differ)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diffs) == 1:
            return True
        return (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short:
            return True
    return False


def typosquat_of(name: str) -> Optional[str]:
    """The popular package `name` is one edit from, or None. A popular name
    is never a typosquat of another; names under five letters are left
    alone, where one edit is most of the name."""
    canonical = plugin_format.canonical_package(name)
    if canonical in POPULAR:
        return None
    squashed = canonical.replace("-", "")
    for popular in sorted(POPULAR):
        if len(popular) < 5:
            continue
        if (_distance_at_most_one(canonical, popular)
                or squashed == popular.replace("-", "")):
            return popular
    return None


def _requirements(manifest, add) -> None:
    for req in manifest.requirements if manifest else ():
        popular = typosquat_of(req.name)
        if popular:
            add(HIGH, "typosquat", plugin_format.REQUIREMENTS, 0,
                f"{req.name} is one edit from the popular package {popular}; "
                f"check that it is the one you meant")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, body: Optional[bytes] = None, *,
          limit: int = MAX_PYPI_BYTES) -> tuple:
    """(status, bytes) for `url`, POSTing `body` as JSON when given. No
    redirect is followed. OSError when nothing answered."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None
                                     else "GET")
    try:
        with _OPENER.open(request, timeout=TIMEOUT_S) as response:  # nosec B310 - two fixed https hosts
            data = response.read(limit + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OSError(str(getattr(exc, "reason", exc))) from None
    if len(data) > limit:
        raise OSError(f"the answer passed {limit:,} bytes")
    return status, data


def _when(text) -> Optional[_dt.datetime]:
    if not isinstance(text, str):
        return None
    try:
        when = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=_dt.UTC)


class _Shape(ValueError):
    """An answer that is JSON, but not in the shape PyPI or OSV answers in."""


def _release_files(release) -> tuple:
    """(files, yanked) from PyPI's per-version answer, each file a dict
    with its type, sha256 and upload time. _Shape when it is not that."""
    if not isinstance(release, dict):
        raise _Shape
    info = release.get("info") or {}
    files = release.get("urls") or []
    if not isinstance(info, dict) or not isinstance(files, list):
        raise _Shape
    out = []
    for item in files:
        if not isinstance(item, dict):
            raise _Shape
        digests = item.get("digests") or {}
        if not isinstance(digests, dict):
            raise _Shape
        sha = digests.get("sha256")
        out.append({"type": item.get("packagetype"),
                    "sha256": sha.lower() if isinstance(sha, str) else None,
                    "when": _when(item.get("upload_time_iso_8601")
                                  or item.get("upload_time"))})
    return out, bool(info.get("yanked"))


def _pypi(req, fetcher, now) -> tuple:
    """(findings as add() arguments, unchecked reason or None). The files
    judged are the ones the lock pins by hash, since those are what pip
    installs: an old source archive does not make a wheel uploaded
    yesterday old (batch 272)."""
    name = plugin_format.canonical_package(req.name)
    url = (f"{PYPI}/{urllib.parse.quote(name)}/"
           f"{urllib.parse.quote(req.version)}/json")
    try:
        status, data = fetcher(url, None)
    except OSError as exc:
        return [], f"PyPI did not answer about {req.pin}: {exc}"
    where = plugin_format.REQUIREMENTS
    if status == 404:
        return [(HIGH, "not-on-pypi", where, 0,
                 f"PyPI has no release {req.version} of {req.name}")], None
    if status != 200:
        return [], f"PyPI answered {status} about {req.pin}"
    try:
        files, yanked = _release_files(json.loads(data))
    except _Shape:
        return [], (f"PyPI's answer about {req.pin} was not in the shape "
                    f"PyPI answers in")
    except (ValueError, RecursionError):
        return [], f"PyPI's answer about {req.pin} was not JSON"
    out = []
    if not files:
        out.append((HIGH, "not-on-pypi", where, 0,
                    f"PyPI lists no files for {req.pin}"))
        return out, None
    if not any(f["sha256"] for f in files):
        return [], (f"PyPI's answer about {req.pin} gave no file's sha256, "
                    f"so the lock's hashes could not be matched")
    pinned_hashes = {h.lower() for h in req.hashes}
    pinned = [f for f in files if f["sha256"] in pinned_hashes]
    if not pinned:
        out.append((HIGH, "unknown-hash", where, 0,
                    f"none of the hashes the lock pins for {req.pin} is a "
                    f"file PyPI lists for that release"))
        return out, None
    if not any(f["type"] == "bdist_wheel" for f in pinned):
        out.append((HIGH, "sdist-only", where, 0,
                    f"the lock pins no wheel of {req.pin}, only a source "
                    f"archive, which runs its own code to build; the "
                    f"install's --only-binary refuses it"))
    times = [f["when"] for f in pinned]
    if not all(times):
        # An age it cannot read is not a pass (the module's rule).
        return out, (f"PyPI's answer about {req.pin} gave no upload time "
                     f"for a file the lock pins")
    if times:
        age = now - max(times)
        if age < _dt.timedelta(days=FRESH_DAYS):
            days = max(0, age.days)
            out.append((HIGH, "fresh-release", where, 0,
                        f"a file the lock pins for {req.pin} was uploaded "
                        f"{days} day{'' if days == 1 else 's'} ago; wait "
                        f"until it is {FRESH_DAYS} days old, when a bad "
                        f"release has usually been caught"))
    if yanked:
        out.append((NOTE, "yanked", where, 0,
                    f"{req.pin} has been yanked from PyPI"))
    return out, None


def _osv(requirements, fetcher) -> tuple:
    queries = [{"package": {"name": plugin_format.canonical_package(r.name),
                            "ecosystem": "PyPI"}, "version": r.version}
               for r in requirements]
    try:
        status, data = fetcher(OSV, json.dumps({"queries": queries}).encode())
    except OSError as exc:
        return [], f"OSV did not answer: {exc}"
    if status != 200:
        return [], f"OSV answered {status}"
    try:
        answer = json.loads(data)
    except (ValueError, RecursionError):
        return [], "OSV's answer was not JSON"
    results = answer.get("results") if isinstance(answer, dict) else None
    if not isinstance(results, list) or not all(
            isinstance(r, dict) and isinstance(r.get("vulns") or [], list)
            for r in results if r is not None):
        return [], "OSV's answer was not in the shape OSV answers in"
    if len(results) != len(queries):
        return [], (f"OSV answered for {len(results)} of {len(queries)} "
                    f"packages")
    out = []
    for req, result in zip(requirements, results):
        ids = sorted({str(v.get("id")) for v in (result or {}).get("vulns")
                      or () if isinstance(v, dict) and v.get("id")})
        if ids:
            shown = ", ".join(ids[:6]) + (f" and {len(ids) - 6} more"
                                          if len(ids) > 6 else "")
            out.append((HIGH, "cve", plugin_format.REQUIREMENTS, 0,
                        f"{req.pin} has known vulnerabilities: {shown}"))
    return out, None


def _online(manifest, add, fetcher, now) -> list:
    requirements = list(manifest.requirements if manifest else ())
    if not requirements:
        return []
    unchecked = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda r: _pypi(r, fetcher, now), requirements))
    for found, reason in answers:
        for args in found:
            add(*args)
        if reason:
            unchecked.append(reason)
    found, reason = _osv(requirements, fetcher)
    for args in found:
        add(*args)
    if reason:
        unchecked.append(reason)
    return unchecked


def scan(files: dict, manifest, *, online: bool = False,
         fetcher: Optional[Callable] = None,
         now: Optional[_dt.datetime] = None) -> Report:
    """Every finding in a plugin whose files are `files` ({path: bytes})
    and whose validated manifest is `manifest`. With `online`, the lock is
    looked up on PyPI and OSV through `fetcher(url, body) -> (status,
    bytes)`."""
    add = _Collector()
    network = bool(manifest.access.network) if manifest else False
    for path in sorted(files):
        text = _decoded(files[path])
        if text is None:
            add(HIGH, "binary", path, 0,
                "is not text (it holds a NUL byte or is not UTF-8), so "
                "nobody can read what it holds")
            continue
        _text(path, text, add)
        if path.endswith(".py"):
            declared = plugin_format.python_encoding_problem(files[path])
            if declared:
                # The format refuses it; a scan run on its own still says so,
                # since the parse below reads text Python would not run.
                add(HIGH, "encoding", path, 0, declared)
            _python(path, text, network, add)
    _requirements(manifest, add)
    report = Report(online=online)
    if online:
        report.unchecked = _online(
            manifest, add, fetcher or (lambda url, body: fetch(
                url, body, limit=MAX_OSV_BYTES if body else MAX_PYPI_BYTES)),
            now or _dt.datetime.now(_dt.UTC))
    report.findings = add.close()
    return report


# ---- the names a typosquat aims at ----------------------------------------

#: Widely used PyPI packages, written by hand in PEP 503's form. A typosquat
#: targets a name people type often, so these are the ones worth guarding;
#: the list is a tripwire, not a census, and a name it lacks is simply not
#: checked.
POPULAR = frozenset("""
absl-py adlfs aiobotocore aiofiles aiohttp aiosignal alembic amqp anthropic
anyio argcomplete asttokens astroid astunparse async-timeout attrs
azure-core azure-identity azure-storage-blob bcrypt beautifulsoup4 billiard
black bleach blinker boto boto3 botocore brotli build cachetools celery
certifi cffi cfgv chardet charset-normalizer click cloudpickle colorama
coloredlogs contourpy coverage cryptography cycler cython dataclasses-json
datasets debugpy decorator defusedxml deprecated dill distlib dnspython
docker docutils duckdb email-validator et-xmlfile exceptiongroup executing
fastapi filelock fiona flake8 flask flatbuffers fonttools frozenlist fsspec
gast gcsfs geopandas gitdb gitpython google-api-core google-api-python-client
google-auth google-cloud-core google-cloud-storage google-crc32c
google-resumable-media googleapis-common-protos greenlet grpcio
grpcio-status grpcio-tools gunicorn h11 h5py html5lib httpcore httplib2
httpx huggingface-hub humanfriendly identify idna imageio importlib-metadata
iniconfig ipykernel ipython isort itsdangerous jaraco-classes jedi jeepney
jinja2 jmespath joblib jsonschema jsonschema-specifications jupyter-client
jupyter-core keras keyring kiwisolver kombu kubernetes langchain
langchain-core langsmith linkify-it-py llvmlite loguru lxml lz4 mako
markdown markdown-it-py markupsafe marshmallow matplotlib matplotlib-inline
mccabe mcp mdurl ml-dtypes mock more-itertools mpmath msal msal-extensions
msgpack multidict multiprocess mypy mypy-extensions mysqlclient nbconvert
nbformat nest-asyncio networkx nodeenv notebook numba numpy oauth2client
oauthlib onnx onnxruntime openai opencv-python openpyxl opt-einsum orjson
outcome packaging pandas paramiko parso pathspec pexpect pillow pip
platformdirs playwright pluggy polars portalocker pre-commit prettytable
prompt-toolkit propcache protobuf psutil psycopg2 psycopg2-binary ptyprocess
pure-eval py4j pyarrow pyasn1 pyasn1-modules pybind11 pycodestyle pycparser
pycryptodome pycryptodomex pydantic pydantic-core pydantic-settings pyee
pyflakes pygments pyjwt pylint pymongo pymysql pynacl pyopenssl pyparsing
pyproj pyproject-hooks pyrsistent pysocks pyspark pytest pytest-cov
python-dateutil python-dotenv python-multipart pytz pyyaml pyzmq pyzstd
readme-renderer redis referencing regex requests requests-file
requests-oauthlib requests-toolbelt rich rpds-py rsa ruff s3fs s3transfer
safetensors scikit-image scikit-learn scipy scrapy secretstorage selenium
sentry-sdk setuptools shapely shellingham simplejson six smmap sniffio
sortedcontainers soupsieve sqlalchemy sse-starlette stack-data starlette
structlog sympy tabulate tenacity tensorboard tensorflow termcolor textual
threadpoolctl tifffile tiktoken tinycss2 tokenizers tomli tomlkit toolz
torch torchvision tornado tqdm traitlets transformers trio twine twisted
typer typing-extensions tzdata uc-micro-py ujson uritemplate urllib3
uvicorn vine virtualenv wcwidth webencodings websocket-client websockets
werkzeug wheel wrapt xlrd xlsxwriter xxhash yarl zipp zope-interface
zstandard
""".split())


# ---- the command ----------------------------------------------------------

def scan_folder(path: str, *, online: bool = False, fetcher=None,
                now=None) -> tuple:
    """(format problems, Report or None) for the plugin folder at `path`,
    read as `/plugin install --from` reads one."""
    from core import plugin_source
    from core.plugin_format import PluginFormatError
    from core.plugin_source import FetchError

    try:
        candidate = plugin_source.read_folder(path)
        manifest = plugin_format.validate(candidate.name, candidate.files)
    except PluginFormatError as exc:
        return list(exc.problems), None
    except FetchError as exc:
        return [str(exc)], None
    return [], scan(candidate.files, manifest, online=online,
                    fetcher=fetcher, now=now)


def as_dict(report: Report) -> dict:
    return {"findings": [vars(f) | {"where": f.where} for f in report.findings],
            "unchecked": list(report.unchecked), "online": report.online}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.plugin_scan",
        description="Read a plugin folder and say what a reviewer should "
                    "look at. Exits 1 on a format problem, a high finding or "
                    "an online check that did not answer.")
    parser.add_argument("folder")
    parser.add_argument("--online", action="store_true",
                        help="also look the lock up on PyPI and OSV")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    problems, report = scan_folder(args.folder, online=args.online)
    if args.json:
        print(json.dumps({"problems": problems,
                          "report": as_dict(report) if report else None},
                         indent=2))
    else:
        for problem in problems:
            print(f"FORMAT  {problem}")
        for finding in report.findings if report else ():
            print(f"{finding.severity.upper():5}  {finding.shown()}")
        for reason in report.unchecked if report else ():
            print(f"UNCHECKED  {reason}")
        if report is not None:
            print(f"{args.folder}: {report.summary()}")
    failed = bool(problems) or (report is not None and (
        report.high or report.unchecked))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
