"""
mcp_client/

MCP (Model Context Protocol) client -- ROADMAP_v2 §17.

NAMED `mcp_client`, NOT `mcp`, AND THAT IS LOAD-BEARING. The project root
is on sys.path, so a top-level `mcp/` package here would shadow the
installed MCP SDK and `import mcp` inside this very package would resolve
to itself. That is the `logging.py` incident (see CLAUDE.md conventions)
with a worse blast radius. §17's spec sketches `mcp/client.py`; do not
follow it there.

Every symbol imported from the SDK lives inside this package, so a future
major-version migration touches one directory.
"""
