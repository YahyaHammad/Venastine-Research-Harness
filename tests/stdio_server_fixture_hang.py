"""A real child process that is spawned as an MCP stdio server and never
answers the handshake. The other half of the §17 AC8 integration test.

Deliberately NOT named test_*, for the reason its sibling
`stdio_server_fixture.py` gives: pytest must not collect it. It is argv,
executed as `python tests/stdio_server_fixture_hang.py` by
test_mcp_integration.py.

WHY A SECOND FIXTURE RATHER THAN A FLAG ON THE FIRST. The two are used in
the same test, at the same time, and the point of each is a child process
that does the opposite thing: one connects and must be reaped, the other
never connects and must be reaped anyway. Keeping them separate files
means `_child_pids()` can see them together (its needle,
"stdio_server_fixture", is a prefix of this file's name) or one at a time,
which is what lets a test say WHICH child leaked.

It reads nothing and writes nothing, so `Client.__aenter__` -- which sends
`initialize` and waits for the reply -- never returns. That is the window
S13-08 reasons about and could not run:
`AsyncExitStack.enter_async_context` pushes its exit callback only AFTER
`__aenter__` returns, so a timeout here leaves the child unowned by the
stack, and whether it dies then depends on `stdio_client`'s own cleanup
under cancellation -- SDK behaviour nothing in this repository measured
until batch 150.
"""

import sys
import time

if __name__ == "__main__":
    # On stderr, so it cannot be mistaken for a protocol reply on stdout.
    # A fixture that failed to start and one that started and went quiet
    # are the same thing to the client, and only this line tells them
    # apart if the test ever needs to know.
    sys.stderr.write("stdio_server_fixture_hang: up, answering nothing\n")
    sys.stderr.flush()
    while True:
        time.sleep(3600)
