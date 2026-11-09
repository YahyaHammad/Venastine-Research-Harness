"""The example plugin's one tool: a list of points, as an outline.

A tool is a module in tools/ with a `run(params, context)` function, named
after the tool (`outline` here; a tool called `word-count` would live in
tools/word_count.py). The harness's runner calls it with the model's
parameters and a context, and sends back what it returns. See
docs/PLUGINS.md for the whole contract.

It reads its two settings on every call, so `/plugin config example
max_items 3` applies to the next one. It asks for no access, so it never
sees the workspace or the network; it does not need them.
"""


def run(params, context):
    points = [str(point).strip() for point in params.get("points") or []]
    points = [point for point in points if point]
    kept = points[:context.settings["max_items"]]
    if context.settings["detail"] == "full":
        lines = [f"{number}. {point}" for number, point in enumerate(kept, 1)]
    else:
        lines = [f"- {point}" for point in kept]
    return {"outline": "\n".join(lines), "dropped": len(points) - len(kept)}
