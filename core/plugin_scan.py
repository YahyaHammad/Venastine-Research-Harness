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
        feeding one of them, and pickle or marshal loads
  high  code or text that reaches for credentials: credential paths
        (providers.json, ~/.ssh, ~/.aws ...) and key-like environment names
  high  a network module imported by a plugin whose manifest says
        `network: false`
  high  text telling the model to ignore its rules, turn off approval or
        exfiltrate, and `curl | sh`
  high  a locked package with a known vulnerability (OSV), one edit from a
        popular package's name, released under 14 days ago, without a
        wheel, or not on PyPI at all
  note  subprocess, ctypes, dynamic imports, the whole environment read,
        HTML comments in Markdown, and long runs of encoded-looking data

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

from core import plugin_format

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
    return (unicodedata.category(char) in _HIDDEN_CATEGORIES
            or code in _SMUGGLING)


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
             "getattr": "builtins.getattr", "dict": "builtins.dict"}
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
        if isinstance(node, ast.Name):
            if node.id in self.aliases:
                return self.aliases[node.id]
            return _BUILTINS.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._qualified(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

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
            attr = node.args[1]
            if (target in ("builtins", "__builtins__")
                    or (isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "__builtins__")):
                if not (isinstance(attr, ast.Constant)
                        and attr.value not in ("exec", "eval", "compile",
                                               "__import__")):
                    self.add(HIGH, "exec", self.path, line,
                             "reaches a builtin by name at run time (getattr "
                             "on builtins)")
        elif name in _DYNAMIC_IMPORT:
            self.add(NOTE, "dynamic-import", self.path, line,
                     f"imports a module named at run time ({name})")
        elif name in _SUBPROCESS or name.startswith(_SUBPROCESS_PREFIXES):
            self.add(NOTE, "subprocess", self.path, line,
                     f"starts another program ({name})")
        elif name in _NETWORK_CALLS and not self.network:
            self.add(HIGH, "network", self.path, line,
                     f"opens a connection ({name}), but plugin.yaml says "
                     f"network: false")
        elif name in _ENV_READS:
            key = node.args[0] if node.args else None
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key_like(key.value):
                    self.add(HIGH, "secret-env", self.path, line,
                             f"reads the environment variable "
                             f"{_shown(key.value, 60)}, whose name says it "
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

    def visit_Subscript(self, node) -> None:
        if (isinstance(node.ctx, ast.Load)
                and self._qualified(node.value) in ("os.environ",
                                                    "os.environb")):
            key = node.slice
            if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and key_like(key.value)):
                self.add(HIGH, "secret-env", self.path, node.lineno,
                         f"reads the environment variable "
                         f"{_shown(key.value, 60)}, whose name says it holds "
                         f"a credential")
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

class _Collector:
    def __init__(self) -> None:
        self.findings: list = []
        self._seen: set = set()
        self._per: Counter = Counter()
        self._more: Counter = Counter()

    def __call__(self, severity, rule, path, line, message) -> None:
        key = (rule, path, line, message)
        if key in self._seen:
            return
        self._seen.add(key)
        self._per[(rule, path)] += 1
        if self._per[(rule, path)] > PER_FILE:
            self._more[(severity, rule, path)] += 1
            return
        self.findings.append(Finding(severity, rule, path, line, message))

    def close(self) -> list:
        counted = set()
        for (severity, rule, path), n in sorted(self._more.items()):
            more = Finding(severity, rule, path, 0,
                           f"and {n} more like the {rule} finding"
                           f"{'s' if n > 1 else ''} above")
            counted.add(id(more))
            self.findings.append(more)
        order = {HIGH: 0, NOTE: 1}
        # A count follows the findings it counts, in its file; a finding of
        # the whole file (line 0) still leads it.
        return sorted(self.findings, key=lambda f: (
            order[f.severity], f.path,
            sys.maxsize if id(f) in counted else f.line, f.rule))


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
    if not path.endswith(".py"):
        _said(path, [(n, line) for n, line in enumerate(lines, 1)], add)


def _said(path: str, pieces: list, add) -> None:
    """The instruction and credential checks, on (line, text) pieces."""
    for line, piece in pieces:
        for rule, pattern, what in _INSTRUCTIONS:
            match = pattern.search(piece)
            if match:
                add(HIGH, rule, path, line,
                    f"{what}: \"{_shown(match.group(0))}\"")
        if not path.endswith(".py"):
            marker = _credential_marker(piece)
            if marker:
                add(HIGH, "credential-path", path, line,
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


def _when(text: str) -> Optional[_dt.datetime]:
    try:
        when = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=_dt.UTC)


def _pypi(req, fetcher, now) -> tuple:
    """(findings as add() arguments, unchecked reason or None)."""
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
        release = json.loads(data)
        files = release.get("urls") or []
        yanked = bool((release.get("info") or {}).get("yanked"))
    except (ValueError, AttributeError):
        return [], f"PyPI's answer about {req.pin} was not JSON"
    out = []
    if not files:
        out.append((HIGH, "not-on-pypi", where, 0,
                    f"PyPI lists no files for {req.pin}"))
        return out, None
    if not any(f.get("packagetype") == "bdist_wheel" for f in files):
        out.append((HIGH, "sdist-only", where, 0,
                    f"{req.pin} has no wheel, only a source archive, which "
                    f"runs its own code to build; the install's "
                    f"--only-binary refuses it"))
    times = [t for t in (_when(f.get("upload_time_iso_8601")
                               or f.get("upload_time")) for f in files) if t]
    if times:
        age = now - min(times)
        if age < _dt.timedelta(days=FRESH_DAYS):
            days = max(0, age.days)
            out.append((HIGH, "fresh-release", where, 0,
                        f"{req.pin} was released {days} day"
                        f"{'' if days == 1 else 's'} ago; wait until it is "
                        f"{FRESH_DAYS} days old, when a bad release has "
                        f"usually been caught"))
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
        results = json.loads(data).get("results") or []
    except (ValueError, AttributeError):
        return [], "OSV's answer was not JSON"
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
