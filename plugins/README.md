# The plugin marketplace

Every folder here whose name ends in `.plugin` is a plugin, and every other folder is a
category that organises them. A broad plugin and the category of narrower ones can sit
side by side: `math.plugin/` beside `math/`, which holds `calculus.plugin/` and deeper
categories.

From the harness:

```
/plugin list                    the top level
/plugin list math               one category down
/plugin info math/calculus      what a plugin holds
/plugin install math/calculus   install it, after the trust modal
```

There are no plugins here yet.

## Adding one

[`docs/PLUGINS.md`](../docs/PLUGINS.md) is the guide: the folder convention, the manifest,
the settings, the tools and their locked dependencies, MCP servers, and how to try a plugin
from your own folder first.
[`docs/plugin-template/example.plugin`](../docs/plugin-template/example.plugin/) is a
worked example to copy.

A pull request that adds or changes a plugin changes nothing outside `plugins/`; one that
touches both fails its `plugin-boundary` check until it is split. Every pull request here is
scanned and its changed plugins tested;
[the guide's last section](../docs/PLUGINS.md#contributing-to-the-marketplace) says what
each check looks at.
