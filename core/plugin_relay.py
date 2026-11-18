"""
core/plugin_relay.py

ROADMAP_v3 §94 (PL45, the owner's answer in batch 274). The harness's own
process between the MCP SDK's stdio reader and a plugin's MCP server,
whose code the plugin's author wrote. The SDK's reader joins output until
it sees a newline, with no bound, so a server that never writes one grows
the harness's memory without end; and it logs every line that is not
JSON-RPC as an error, which the TUI shows, one transcript entry a line.

This passes the server's JSON-RPC lines through unchanged and stops the
server at a line longer than `max_line` bytes. It drops a line that is not
a JSON object or array, naming the first few on stderr (the server's log,
which a refusal is read back from), and stops the server once `max_dropped`
of them have come. Your stdin goes to the server as it arrives.

    python -I -S -B plugin_relay.py <max_line> <max_dropped> -- <command> [args...]

Standard library only, and it imports nothing of the harness or of the
plugin: it is started by path, like the runner. The SDK stops a server by
its process tree (a Windows job, a POSIX process group), so the server goes
with this process.
"""

import json
import os
import subprocess  # nosec B404 - the server's own command line, given by the harness, no shell
import sys
import threading

#: Dropped lines named on stderr; the rest are counted.
SHOWN = 3


def _say(text: str) -> None:
    try:
        sys.stderr.write(f"venastine: {text}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass  # nothing is reading the log any more


def _pump_in(child) -> None:
    """The SDK's requests to the server, as they arrive; the server's stdin
    closes when ours does."""
    source = sys.stdin.buffer
    try:
        while True:
            chunk = source.read1(65536)
            if not chunk:
                break
            child.stdin.write(chunk)
            child.stdin.flush()
    except (OSError, ValueError):
        pass  # the server or the SDK went away; the reader below decides
    finally:
        try:
            child.stdin.close()
        except (OSError, ValueError):
            pass


def _message(line: bytes) -> bool:
    """Whether `line` is one JSON-RPC message: a JSON object, or an array
    of them. Anything else would only be logged by the SDK and dropped."""
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return isinstance(value, (dict, list))


def _stop(child, code: int) -> int:
    try:
        child.kill()
        child.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # the SDK's own stop takes the process tree
    return code


def relay(max_line: int, max_dropped: int, argv: list) -> int:
    child = subprocess.Popen(argv, stdin=subprocess.PIPE,  # nosec B603 - the harness's argv for this server, no shell
                             stdout=subprocess.PIPE)
    threading.Thread(target=_pump_in, args=(child,), daemon=True).start()
    out = sys.stdout.buffer
    dropped = 0
    try:
        while True:
            line = child.stdout.readline(max_line + 1)
            if not line:
                break
            if len(line) > max_line and not line.endswith(b"\n"):
                _say(f"the server wrote a line longer than {max_line:,} bytes, "
                     f"so it was stopped")
                return _stop(child, 1)
            if not line.strip():
                continue
            if _message(line):
                out.write(line if line.endswith(b"\n") else line + b"\n")
                out.flush()
                continue
            dropped += 1
            if dropped <= SHOWN:
                _say(f"dropped a line that is not JSON-RPC: {line[:200]!r}")
            if dropped >= max_dropped:
                _say(f"dropped {dropped} lines that are not JSON-RPC, so the "
                     f"server was stopped")
                return _stop(child, 1)
    except (OSError, ValueError):
        return _stop(child, 0)  # the SDK closed our stdout: it is done
    if dropped > SHOWN:
        _say(f"dropped {dropped} lines in all that are not JSON-RPC")
    return child.wait()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or args[2] != "--":
        _say("usage: plugin_relay.py <max_line> <max_dropped> -- <command> ...")
        return 2
    return relay(int(args[0]), int(args[1]), args[3:])


if __name__ == "__main__":
    code = main()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    # Not sys.exit: the stdin thread may still be blocked in a read, and an
    # interpreter finalizing under it prints "Fatal Python error: could not
    # acquire lock" into the server's log, over the server's own last words.
    os._exit(code)
