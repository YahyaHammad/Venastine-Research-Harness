"""
tests/_bridge_child.py

A stand-in for `main.py --tui` under Textual's web driver, run as a script by
test_hosted_bridge.py so the supervisor's relay is driven over a real pipe
without starting a harness: a preamble, the `__GANGLION__` line, then framed
packets each way (deploy/hosted/supervisor.py's docstring has the protocol).

    python tests/_bridge_child.py echo          -- answers, then quits on "quit"
    python tests/_bridge_child.py no-handshake  -- complains on stderr, exits

The leading underscore keeps pytest from collecting it.
"""

import json
import sys


def _packet(kind: bytes, payload: bytes) -> bytes:
    return kind + len(payload).to_bytes(4, "big") + payload


def _meta(meta: dict) -> bytes:
    return _packet(b"M", json.dumps(meta).encode("utf-8"))


def _read_exact(stream, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def main(mode: str) -> int:
    out = sys.stdout.buffer
    if mode == "no-handshake":
        sys.stderr.write("boom: the harness could not import its config\n")
        return 3
    out.write(b"[mcp] a line the harness prints before Textual starts\n")
    out.write(b"__GANGLION__\n")
    out.write(_packet(b"D", b"hello"))
    out.write(_meta({"type": "open_url", "url": "javascript:alert(1)"}))
    out.write(_meta({"type": "open_url", "url": "https://example.org/x"}))
    out.flush()
    stdin = sys.stdin.buffer
    while True:
        try:
            header = _read_exact(stdin, 5)
            payload = _read_exact(stdin, int.from_bytes(header[1:5], "big"))
        except EOFError:
            return 0
        kind = header[:1]
        if kind == b"D":
            out.write(_packet(b"D", b"echo:" + payload))
        elif kind == b"M":
            meta = json.loads(payload)
            if meta.get("type") == "quit":
                return 0
            if meta.get("type") == "resize":
                out.write(_packet(b"D", f"size:{meta['width']}x{meta['height']}"
                                  .encode()))
        out.flush()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "echo"))
