# example

A worked example of the Venastine plugin format. Copy this folder, rename
it to `<your-name>.plugin`, and change `name` in `plugin.yaml` to match.

It adds:

- the skill `example:checklist`, a method for turning a vague request into
  a checklist before starting;
- the agent `example:planner`, which writes such a checklist and nothing
  else.

Try it from a folder on your own computer before you open a pull request:

```
/plugin install --from docs/plugin-template/example.plugin
```

`docs/PLUGINS.md` is the full guide.
