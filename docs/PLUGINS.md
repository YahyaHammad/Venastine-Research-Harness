# Plugins

A plugin adds skills, agents, tools and settings of its own to the harness, without
touching any of the harness's files. Plugins come from the marketplace in this repository's
[`plugins/`](../plugins/) folder, where every one is reviewed before it is merged, or
from a folder on your own computer. `docs/ROADMAP_v3.md` §94 records why each rule
below is what it is.

**What this version runs:** skills, agents, settings, and tools with their Python
dependencies, each plugin's tools in a sandbox of their own. MCP servers are coming in a
later version; a plugin that ships one is refused for now, rather than half installed.

## Using plugins

| Command | What it does |
|---|---|
| `/plugin` | What is installed, and whether each one loads here |
| `/plugin list [category]` | Browse the marketplace, one level at a time |
| `/plugin info <id>` | What a plugin holds, before or after you install it |
| `/plugin install <id>` | Install one from the marketplace, after the trust modal |
| `/plugin install --from <folder>` | Install one from a folder on this computer |
| `/plugin uninstall <id> [--purge]` | Remove it; `--purge` removes its settings too |
| `/plugin disable <id>` / `enable <id>` | Turn it off, or back on, in this project only |
| `/plugin config <id> [<key> [<value> \| --reset]]` | See or change its settings |
| `/plugin access <id> [none\|read\|write \| network on\|off]` | What its tools may touch in this project |
| `/plugin approval <id> [ask\|auto]` | Whether each of its tool calls asks you first |

A plugin's **id** is its path in the marketplace: `plugins/design/web-design/javascript-and-typescript.plugin`
is installed with `/plugin install design/web-design/javascript-and-typescript`. In the TUI,
typing `/plugin ` offers the subcommands, then your installed plugins, then their settings
and values, the way `/config` does.

**The trust modal** shows where the plugin comes from (a marketplace commit, or a folder
nobody has reviewed), every skill and agent it adds under the name it will have, its
settings, its files and the start of its README. Nothing is written until you say yes.
Its skills and agents then load at once, in every project, until `/plugin disable` turns
the plugin off in one.

**Names.** Everything a plugin adds carries its name: the `example` plugin's skill is
`example:checklist`, its agent `example:planner`. A plugin can never replace one of the
harness's own skills or agents, or one you wrote, or another plugin's.

**Where they live.** `~/.config/venastine/plugins/`, outside the harness's install folder,
so updating or reinstalling the harness never touches them. The harness records a hash of
every file at install; if one changes afterwards, the plugin stops loading, and
`/plugin` says which file, until you install it again and see the modal.

**Settings** are each plugin's own, declared in its manifest and stored beside it. They are
never keys in `config.yaml`, and no plugin can add one there. A plugin's tools read them on
every call, so a change applies to the next one; skills and agents do not read them.

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
  requirements.txt   optional: the tools' Python dependencies, as a hashed lock
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

### Dependencies

`requirements.txt` lists every package your tools need, **including the ones those depend
on**, each pinned exactly and with the hash of every file it may install from:

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
attributes will do for a test in your `tests/` folder.

## Contributing to the marketplace

1. Put your plugin in `plugins/`, in the category that fits its subject. Make a new category
   folder if none does.
2. Open a pull request that changes **only** `plugins/`. A pull request that touches
   `plugins/` and anything else will be flagged and must be split: a plugin and a change to
   the harness are reviewed differently.
3. Raise `version` in the manifest whenever you change a plugin.
4. A plugin-only pull request never releases a new version of the harness.

Every plugin is reviewed before it is merged. Automated checks for the format and for
suspicious content are coming. Until they land, review is by hand.

Plugins that cannot live in a public repository, such as some biology and security research,
are installed from a folder with `--from`. Installing from a git URL is coming in a later
version. The trust modal says plainly that nobody reviewed such a plugin.
