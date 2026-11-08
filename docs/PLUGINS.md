# Plugins

A plugin adds skills, agents and settings of its own to the harness, without touching
any of the harness's files. Plugins come from the marketplace in this repository's
[`plugins/`](../plugins/) folder, where every one is reviewed before it is merged, or
from a folder on your own computer. `docs/ROADMAP_v3.md` §94 records why each rule
below is what it is.

**What this version runs:** skills, agents and settings. Tools, MCP servers and Python
dependencies are coming in later versions, and run in a sandbox of their own. A plugin
that ships any of them is refused for now, rather than half installed.

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
never keys in `config.yaml`, and no plugin can add one there. Settings are read by a
plugin's tools, which a later version runs; skills and agents do not read them.

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

### Trying it

```
/plugin info --from path/to/your.plugin
/plugin install --from path/to/your.plugin
```

`info` shows the trust modal's text without installing anything. Install again after each
change: the modal marks it as a replacement.

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
