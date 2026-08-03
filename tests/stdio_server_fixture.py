"""A real MCP server, spoken over stdio, for the §17 AC8 integration test.

Deliberately NOT named test_*: pytest must not collect it. It is executed
as a subprocess (`python tests/stdio_server_fixture.py`) by
test_mcp_integration.py, which is the only way to prove the thing AC8
actually cares about -- that a child process this harness spawned is
reaped on shutdown. An in-process server cannot demonstrate that, because
there is no child.
"""

from mcp.server import MCPServer

mcp = MCPServer("stdio-fixture")


@mcp.tool()
def ping() -> str:
    return "pong"


if __name__ == "__main__":
    mcp.run()
