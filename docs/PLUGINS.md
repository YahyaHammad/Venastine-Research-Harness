# Plugins

A plugin adds skills, agents, tools and settings of its own to the harness, without
touching any of the harness's files. Plugins come from the marketplace in this repository's
[`plugins/`](../plugins/) folder, where every one is reviewed before it is merged, or
from a folder on your own computer or a Git repository. `docs/ROADMAP_v3.md` §94 records why each rule
below is what it is.

**What this version runs:** skills, agents, settings, tools with their Python dependencies,
and MCP servers written in Python, each plugin's code in a sandbox of its own, and every
install scanned first. Servers started by `npx` or `uvx`, and remote servers, are deferred
to a later version.

## Using plugins

| Command | What it does |
|---|---|
| `/plugin` | What is installed, and whether each one loads here |
| `/plugin list [category]` | Browse the marketplace, one level at a time |
| `/plugin info <id>` | What a plugin holds, before or after you install it |
| `/plugin install <id>` | Install one from the marketplace, after the trust modal |
| `/plugin install --from <folder>` | Install one from a folder on this computer |
| `/plugin install --from <git-url> [--path <name.plugin>]` | Install one from the default branch of a Git repository |
| `/plugin uninstall <id> [--purge]` | Remove it; `--purge` removes its settings too |
| `/plugin disable <id>` / `enable <id>` | Turn it off, or back on, in this project only |
| `/plugin config <id> [<key> [<value> \| --reset]]` | See or change its settings |
| `/plugin access <id> [none\|read\|write \| network on\|off]` | What its tools may touch in this project |
| `/plugin approval <id> [ask\|auto]` | Whether each of its tool calls asks you first |

A plugin's **id** is its path in the marketplace: `plugins/design/web-design/javascript-and-typescript.plugin`
is installed with `/plugin install design/web-design/javascript-and-typescript`. In the TUI,
typing `/plugin ` offers the subcommands, then your installed plugins, then their settings
and values, the way `/config` does.

**The trust modal** shows where the plugin comes from, whether it came through marketplace
review, every skill and agent it adds under the name it will have, its settings, its files
and the start of its README. The install takes effect when you say yes.
Its skills and agents then load at once, in every project, until `/plugin disable` turns
the plugin off in one.

**The scanner.** Before the modal, the harness reads every file of the plugin, running none,
and the modal lists what it found. A HIGH finding is where a reviewer would stop: characters
that display differently from what the model reads; code that runs code it builds, or loads
pickled data; a path or environment variable that holds credentials; a network module in a
plugin that said `network: false`; text telling the model to ignore its rules, turn approval
off or send secrets somewhere; a download piped into a shell. An external plugin's lock is also looked up
on PyPI and OSV, for a known vulnerability, a release under 14 days old, a package with no
wheel, or a name one letter from a popular package's. A note (another program started, native
code, a module imported by a name made at run time, the whole environment read, an HTML
comment, a long encoded string) is listed for you to weigh. Nothing is refused for a finding:
the choice is yours. `python -m core.plugin_scan <folder> [--online]` runs the same scan from a
shell.

### Installing from Git

Use an HTTPS or SSH clone URL. Git must be on PATH. For a private repository, set up your
Git credential helper or SSH authentication first. Keep passwords and tokens out of the URL.

```text
/plugin install --from https://git.example.org/team/my-plugin.git
/plugin install --from git@git.example.org:team/research.git --path plugins/analysis.plugin
```

Use the first form when the root contains `plugin.yaml`, `README.md` and the rest of the plugin
files. The manifest's `name` is the plugin ID. Use `--path` for a `<name>.plugin` folder inside
a larger repository, giving its path from the repository root with forward slashes. If the
repository also contains project files such as `.gitignore`, keep them outside the plugin
subfolder. Both forms read the default branch and record its exact commit. Use
`/plugin info --from` with the same arguments to preview the plugin without installing it.

Git makes a temporary shallow clone. The harness reads committed files from it without a
checkout, and rejects symlinks and submodules inside the selected plugin. The scanner runs
before the prompt, which shows the remote, the folder, the commit and the unreviewed warning.
Run the install command again to update.

### How installed plugins behave

**Names.** Everything a plugin adds carries its name: the `example` plugin's skill is
`example:checklist`, its agent `example:planner`. A plugin can never replace one of the
harness's own skills or agents, or one you wrote, or another plugin's.

**Where they live.** `~/.config/venastine/plugins/`, outside the harness's install folder,
so updating or reinstalling the harness never touches them. The harness records a hash of
every file at install; if one changes afterwards, the plugin stops loading, and
`/plugin` says which file, until you install it again and see the modal.

**Settings** are each plugin's own, declared in its manifest and stored beside it. They are
never keys in `config.yaml`, and no plugin can add one there. A plugin's tools read them on
every call, so a change applies to the next one; its MCP servers read them when they start, so a
change starts each one again. Skills and agents do not read them.

### A plugin's tools

A tool is code the model can call, named after its plugin: the `example` plugin's tool is
`plugin__example__outline`. The trust modal lists every tool with the description the model
will read, the packages it installs, and where its code runs.

**Where they run.** `plugin_tool_isolation` in `config.yaml` decides, for every plugin:

- `container`, the default: each plugin in a container of its own, from the same image and
  with the same memory, CPU and process limits as the shell's sandbox
  (`sandbox_docker_image`). It needs Docker or Podman; without one, the tools are not offered
  to the model. A plugin can never choose its own image.
- `process`: each plugin as a process of its own on this computer. On Linux it holds itself
  to what you granted with Landlock and seccomp, and refuses every call if it cannot. **On
  Windows and macOS nothing holds it**: its code can read, write and send anything you can,
  and the trust modal says so in capitals.

**What they may touch.** A plugin declares what its tools need: no workspace, read access or
write access, and the network or not. Nothing is granted at install. The first time one of its
tools runs in a project, you are asked: Allow gives it what it declared, there; anything else
runs it with no workspace and no network. The answer is kept for that project, and
`/plugin access <id> read` (or `none`, `write`, `network on`, `network off`) changes it. A grant
never exceeds what the plugin declared.

**Asking first.** Every call of a plugin's tool asks you first, like an MCP server's.
`/plugin approval <id> auto` stops that for one plugin, in every project, and the harness
warns at each launch that it is auto-approved; `ask` turns it back on. That choice is yours
and lives in the store, never in anything the plugin ships.

**A tool cannot** call the model, call another tool, or ask you anything. It gets its
parameters, the plugin's settings and what you granted, and returns a result.

### A plugin's MCP servers

A plugin can also run MCP servers. The trust modal lists each one with the command it runs, its
environment, and every tool of it the model will see, with the description the model will read;
a tool the server has that the plugin does not list never loads. A server starts the first time
the model calls one of its tools, and runs where the plugin's tools run, with the same grant, the
same asking first and the same `/plugin approval`. `/plugin info <id>` says whether each one is
running. Taking access back with `/plugin access` stops it at once.

Your own MCP servers stay in your `mcp.json`, and work as they always have.

**Turning plugins off.** `plugins_enabled: false` in `config.yaml` (through `/config`) hides
every installed plugin without uninstalling it. A hosted deployment never loads plugins.
`plugin_marketplace` names the GitHub repository the marketplace is read from, as
`owner/name`.

## Writing a plugin

Start from [`plugin-template/example.plugin`](plugin-template/example.plugin/): copy it,
rename the folder, and change `name` in its manifest to match.

### The folder

A plugin is a folder whose name ends in **`.plugin`**, and no other folder is one. Its name
is lowercase letters, digits and single hyphens, at most 40 characters. In the marketplace
it can sit at any depth under category folders, which follow the same naming rule:

```
plugins/
  math.plugin/                                 id: math
  math/
    calculus.plugin/                           id: math/calculus
    linear-algebra/
      higher-dimensional-matrices.plugin/      id: math/linear-algebra/higher-dimensional-matrices
```

So a broad plugin and the category of narrower ones beneath it can share a name. Put a plugin
as deep as its subject is narrow. An id is at most eight folders deep, and a plugin can never
contain another plugin.

### What it holds

```
example.plugin/
  plugin.yaml        the manifest (required)
  README.md          what it is for and how to use it (required; shown in the trust modal)
  LICENSE            optional, if it differs from the manifest's license field
  skills/            .md files, in folders if you like
  agents/            .md files, no folders
  tools/             .py files: each declared tool's code, and any helpers it imports
  mcp.json           optional: its MCP servers, and the tools each serves
  servers/           .py files, in folders if you like: the servers' own code
  requirements.txt   optional: Python dependencies of tools and servers, as a hashed lock
  tests/             anything; never loaded by the harness
```

Nothing else may sit at the top level. Every file name is letters, digits, `.`, `_` and `-`,
starts with a letter, digit or `_`, and avoids the names Windows reserves (`CON`, `NUL` and
so on). There are no hidden files, no symbolic links, and no two names that differ only in
case. A plugin holds at most 300 files, 1 MB each and 5 MB together. Text is UTF-8.

### The manifest

```yaml
format: 1
name: example                 # must match the folder name, without .plugin
version: "1.0.0"              # quoted; three numbers
description: One line saying what it is for.
authors: [Your Name]
maintainers: [your-github-username]   # optional; asked to review changes to it
license: Apache-2.0           # an SPDX identifier
min_harness: "0.3.14"         # optional: the oldest harness it works on
settings:                     # optional
  detail:
    type: choice
    choices: [brief, full]
    default: brief
    description: How much it writes.
```

| Field | Required | What it is |
|---|---|---|
| `format` | yes | `1`, the format this guide describes |
| `name` | yes | The folder's name without `.plugin` |
| `version` | yes | `"X.Y.Z"`, quoted. Raise it whenever the plugin changes |
| `description` | yes | One line, at most 200 characters |
| `authors` | yes | A list of 1 to 20 names |
| `maintainers` | no | GitHub usernames to ask for review |
| `license` | yes | An SPDX identifier such as `MIT` or `Apache-2.0` |
| `min_harness` | no | A quoted version; older harnesses refuse the install |
| `settings` | no | Up to 32 settings, below |
| `tools` | no | Up to 32 tools, below |
| `access` | no | What the tools may touch: `workspace` and `network`, below |

The manifest is read strictly, so a mistake is an error rather than a line that is quietly
ignored:

- A field that is not in the table is refused.
- A key that appears twice is refused, since a reviewer reads the first and a reader would keep the last.
- YAML aliases (`&name` / `*name`) are refused.
- No field may hold control characters or invisible ones, such as a right-to-left override.
- Quote versions: YAML reads `1.0` as a number.
- A number written as JSON writes it, such as `1e6`, reads as that number.
- An escape for half of a character, such as an emoji's first half alone, is refused: write the
  character itself.
- Don't name a setting `on`, `off`, `yes` or `no`: YAML reads those words as true and false.

### Settings

| `type` | Fields besides `default` and `description` | Values |
|---|---|---|
| `boolean` | none | `true` / `false` (`/plugin config` also takes on/off, yes/no) |
| `integer` | `min`, `max` (optional) | whole numbers in range |
| `number` | `min`, `max` (optional) | finite numbers in range |
| `string` | `max_length` (optional, default 200, at most 1000) | one line of text |
| `choice` | `choices` (required: 1 to 32 single words) | one of the choices |

A setting's key is a lowercase letter followed by letters, digits or underscores, at most 40
characters. Every setting has a `default` that fits its own rules, and a one-line
`description`. When a plugin is reinstalled, your values carry over wherever they still fit.

### Skills and agents

They are the same files the harness's own are, with the same frontmatter: a skill has
`name`, `description` and optionally `additional_tools`; an agent has `name`, `description`,
its tool policy and the rest. See the harness's own skills and agents in `skills/builtin/` and
`agents/builtin/`, and the template's two. Names are prefixed with your plugin's name when they
load, so write `name: checklist`, not `name: example:checklist`.

An agent that should be spawnable by other agents must say `spawnable: true`. A plugin's agent
that does not say so is not spawnable. A plugin's agent can never do more than the harness lets
any agent do: its tool list only narrows, and approval stays on for whatever needs it.

A file the harness cannot load refuses the whole install, with the reason, so check yours
before you open a pull request.

### Tools

Declare each tool in the manifest. The harness reads this list, never your code, to learn
what the model can call:

```yaml
tools:
  - name: word-count
    description: Count the words in a file in the workspace.
    input_schema:
      type: object
      properties:
        path: {type: string, description: A path inside the workspace.}
      required: [path]
access:
  workspace: read     # none (the default), read or write
  network: false      # the default
```

- **`name`**: lowercase letters and digits, words joined by single `-` or `_`, starting
  with a letter. The model calls it `plugin__<plugin>__<name>`, which must fit the providers'
  64 characters, so a 40-character plugin name leaves 14 for the tool's.
- **`description`**: what the model reads to decide when to call it, up to 2000 characters.
  Several lines are fine.
- **`input_schema`**: a JSON Schema whose `type` is `object`, at most 16,384 characters as
  JSON. It must be plain JSON: no YAML dates, no keys that are not text.

Each tool's code is **`tools/<name>.py`**, with a hyphen in the name written as an
underscore (`word-count` is `tools/word_count.py`), defining `run(params, context)`:

```python
def run(params, context):
    path = os.path.join(context.workspace, params["path"])
    with open(path, encoding="utf-8") as handle:
        return {"words": len(handle.read().split())}
```

| `context.` | What it is |
|---|---|
| `settings` | The plugin's settings: your values, or their defaults |
| `workspace` | The workspace's path, when this project granted read or write; else `None` |
| `access` | `"none"`, `"read"` or `"write"`: what this project granted |
| `network` | Whether this project granted the network |
| `plugin_dir` | Your plugin's own files, read-only |
| `tmp_dir` | A folder for scratch files |

- **Return** a dict, which is what the model sees. Anything else is wrapped as
  `{"result": value}`. It must be JSON, and at most 1 MB of it.
- **Raise** to fail: the model sees the exception and the last lines of its traceback.
- **`print()`** goes to the harness's log, not to the model, so it cannot break a reply.
- **A call is limited** by `sandbox_timeout_seconds`. A tool that runs past it is stopped,
  and the next call starts afresh.
- **The process is kept** between calls for the session, so a module-level cache works.
  But it may be restarted at any time, so keep nothing there that matters.
- **Your helpers** can sit beside the tools and be imported as `tools.<module>`.
- **Python** is the sandbox image's in a container (3.13 for the shipped image), or the
  harness's own under `process`. The runner starts it with `-I -S -B`, so the environment
  and site-packages decide nothing, and no bytecode is written beside your files.
- **Plan for no access.** A tool must cope with `context.workspace` being `None` and with
  the network being refused, because either is what a project that said no gets.

Every tool you declare needs its file, and a `.py` file with no declared tool is refused, so
there is no dead code for a reviewer to wonder about.

### MCP servers

List them in **`mcp.json`**, in the shape your own `~/.config/venastine/mcp.json` has, with one
addition: each server lists the tools it serves.

```json
{
  "mcpServers": {
    "notes": {
      "command": "python",
      "args": ["-m", "notes_server"],
      "env": {"NOTES_MODE": "brief"},
      "tools": [
        {
          "name": "word_count",
          "description": "Count the words in some text.",
          "inputSchema": {
            "type": "object",
            "properties": {"text": {"title": "Text", "type": "string"}},
            "required": ["text"]
          }
        }
      ]
    }
  }
}
```

- **A server is Python.** `command` is `"python"`, and `args` start with `"-m"` and a module.
  Anything after the module is passed to it. The module is one your plugin ships in `servers/`,
  as a file or a package, or one its `requirements.txt` installs. The harness starts it with its
  own launcher, where your tools run and with the same grant.
- **`tools`** is the server's own `tools/list` answer, pasted unchanged. Every field MCP gives a
  tool is accepted (`name`, `title`, `description`, `inputSchema`, `outputSchema`,
  `annotations`, `icons`, `_meta` and `execution`), and the first four are what the model reads.
  List only what the model should have: a tool the server serves that is not listed never loads.
  The model calls each one `plugin__<plugin>__<name>`, in one namespace with your plugin's own
  tools and at most 32 of them in all. A server's tool may be named in camelCase; no `__`.
- **`env`** holds plain values, and the modal shows them. It cannot set `PATH`, `HOME`, the
  temporary-folder variables, or anything starting `PYTHON`, `LD_`, `DYLD_` or `VENASTINE_`.
- **Not a plugin's:** `cwd`, `autoApprove` and `disabled`. The harness picks the working
  directory, and asking first and turning a plugin off are yours (`/plugin approval`,
  `/plugin disable`).
- **Not yet:** servers started by `npx`, `uvx` or any command other than `python`, and remote
  (`http` or SSE) servers. They are deferred to a later version, and refused until then with a
  sentence that says so.

The file is read as strictly as the manifest: a key that appears twice, `NaN`, an unknown field
and a value of the wrong type are each refused, and every problem is reported at once. It is
read as JSON, so it keeps your numbers and escapes as you wrote them.

**Getting the listing.** Run your server once with the MCP SDK's client and print its answer:

```python
import asyncio, json
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

async def main():
    server = StdioServerParameters(command="python", args=["-m", "notes_server"], cwd="servers")
    async with Client(stdio_client(server)) as client:
        tools = (await client.list_tools()).tools
        print(json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in tools], indent=2))

asyncio.run(main())
```

**A server written with the SDK.** In version 2 of the MCP SDK, what version 1 called `FastMCP`
is `MCPServer`:

```python
# servers/notes_server.py
from mcp.server.mcpserver import MCPServer

server = MCPServer("notes")

@server.tool()
def word_count(text: str) -> int:
    """Count the words in some text."""
    return len(text.split())

if __name__ == "__main__":
    server.run()
```

The SDK and everything it needs go in your lock: `mcp==2.3.0` in `requirements.in` locks 28
packages, which took 53 seconds to build in the sandbox image, measured.

**What a server is given:**

| | |
|---|---|
| `VENASTINE_PLUGIN_SETTINGS` | The plugin's settings, as JSON |
| `VENASTINE_WORKSPACE` | The workspace's path, when this project granted read or write; unset otherwise |
| Its working directory | The workspace when granted, else a private folder |

- **`sys.stdin` and `sys.stdout` are MCP's pipe; descriptors 0 and 1 are not.** A child
  process the server starts gets nothing on its stdin, and a write to descriptor 1 lands in the
  log. A `print()` goes on the pipe and breaks it, as under any MCP host, so log to stderr.
- **A server keeps running** for the session. A settings change, an access change or a
  reinstall starts it again on its next call, and so does a crash or a call past
  `sandbox_timeout_seconds`. An error your tool reports is that call's error and nothing more.
- **Its results are held to `max_read_chars`,** as your own servers' are.

### Dependencies

`requirements.txt` lists every package your tools and servers need, **including the ones
those depend on**, each pinned exactly and with the hash of every file it may install from:

```
six==1.17.0 \
    --hash=sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274 \
    --hash=sha256:ff70335d468e7eb6ec65b95b99d3a2836546063f63acc5171de367e834932a81
```

Write the names you need in a `requirements.in` and generate the lock for the sandbox
image's Python:

```
uv pip compile requirements.in --generate-hashes --python-version 3.13 \
    --python-platform x86_64-manylinux_2_28 -o requirements.txt
```

Nothing else may appear in the file: no ranges, no URLs, no `-r`, `-e` or `--index-url`, and
no environment markers, since each would let what installs differ from what the trust modal
listed. The harness installs the lock after you say yes, with pip's `--require-hashes` and
`--only-binary=:all:`. So a package with no wheel is refused (an sdist runs code at install),
and so is a lock that leaves a dependency out. Under `container` it builds inside the sandbox
image, and under `process` for the harness's own Python. Changing `plugin_tool_isolation` or
`sandbox_docker_image` later means reinstalling the plugin, which rebuilds them.

A lock resolved for Linux may not install under `process` on Windows or macOS if a package
there needs another dependency. If yours does, say so in your README.

### Trying it

```
/plugin info --from path/to/your.plugin
/plugin install --from path/to/your.plugin
```

`info` shows the trust modal's text without installing anything. Install again after each
change: the modal marks it as a replacement. Then ask the model to use a tool, or test its
module directly: `run(params, context)` is a plain function, and anything with the context's
attributes will do for a test in your `tests/` folder. A server is tested the way its SDK
tests one, or by running it and calling it with the client above.

Before you open a pull request, `python -m core.plugin_scan path/to/your.plugin --online`
shows what the review will find, and `python scripts/plugin_ci.py test path/to/your.plugin`
runs the checks `plugins.yml` runs. It needs Docker or Podman; `--isolation process` checks
the code on this computer instead, without your own tests.

## Contributing to the marketplace

1. Put your plugin in `plugins/`, in the category that fits its subject. Make a new category
   folder if none does.
2. Open a pull request that changes **only** `plugins/`. A pull request that touches
   `plugins/` and anything else will be flagged and must be split: a plugin and a change to
   the harness are reviewed differently.
3. Raise `version` in the manifest whenever you change a plugin.
4. A plugin-only pull request never releases a new version of the harness.

**What the pull request runs.**

- `plugin-review` reads your plugin's files from GitHub, without running any of them, and keeps
  one comment on the pull request up to date.
  - Its `plugin-boundary` check fails a pull request that changes `plugins/` and anything else.
  - Its `plugin-scan` check fails a plugin the format refuses, one whose `version` did not go
    up, one whose name another plugin has, a file under `plugins/` that is in no plugin, and
    any HIGH scanner finding.
  - A finding that is a false positive is cleared by someone with write access adding the
    `plugin:scan-reviewed` label. A new commit removes the label, since the review covered
    other code.
  - The plugin's maintainers, from the manifest already merged, are mentioned, and those with
    write access are asked to review. A pull request cannot add names to be pinged.
- `plugins` runs the checks the harness owns on each changed plugin, in the image a user's
  install builds in: its lock installs, each tool's module imports and defines `run`, each MCP
  server starts and serves exactly the tools its `mcp.json` lists, and pip-audit finds nothing
  in the lock. Your own tests in `tests/` run too if you have any, with pytest, in a container
  with no workspace, no token and the network only if your manifest asks for it. They are
  optional, and passing them is not the review: they are your code.
- The harness's own workflows skip a pull request that changes only `plugins/`, and merging one
  releases nothing and leaves the demo alone.

Every plugin is also read by a person before it is merged.

Plugins that cannot live in a public repository can be installed from a private Git remote
or a local folder with `--from`.
