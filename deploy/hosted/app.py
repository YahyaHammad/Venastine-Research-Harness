"""
deploy/hosted/app.py

ROADMAP_v3 §79 (HD2). The entry point a Hugging Face Space and a ModelScope
Studio run: Gradio's `gr.Server` -- a FastAPI app -- with the bridge's routes
on it. The bundle's root `app.py` imports `spaces` first and calls `main()`.

WHY GRADIO AT ALL, when nothing here is a Gradio interface: on a free Hugging
Face account the only compute a Space can have is ZeroGPU, and ZeroGPU runs
only Gradio-SDK Spaces. It also refuses to start without at least one
`@spaces.GPU` function, so there is one below, and it is never called -- no
model runs here, and no provider call is ever made inside a GPU function,
which would spend the visitor's GPU quota on an HTTP request. Off ZeroGPU,
`spaces.GPU` returns the function unchanged.
"""

from __future__ import annotations

import logging
import os

import spaces


@spaces.GPU(duration=1)
def _zerogpu_placeholder() -> str:
    """Never called. ZeroGPU will not start a Space without one GPU function
    (HD2); a model hosted here is deferred (HD10)."""
    return "unused"


def build(python=None, confinement: str = ""):
    """The Gradio server with the bridge mounted; not yet launched.
    `python` is the session interpreter (session_env.py), or None to run
    sessions on this one, as a local checkout does. `confinement` is the
    math tools' self-test verdict (isolation_report.py)."""
    import gradio as gr

    from deploy.hosted.web import build_bridge

    bridge = build_bridge(python=python, confinement=confinement)
    server = gr.Server(title="Venastine Research Harness")

    @server.api(name="status")
    def status() -> str:
        """Whether the bridge is up, and how many sessions it has free."""
        return f"ok, {bridge.supervisor.capacity_left()} sessions free"

    for route in bridge.routes():
        server.router.routes.append(route)

    @server.on_event("shutdown")
    async def _stop_sessions() -> None:
        await bridge.supervisor.stop_all()

    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from deploy.hosted import session_env

    # BEFORE the server listens, so the platform shows the Space as starting
    # rather than serving a page whose sessions cannot run. A built bundle
    # always carries the lock; a checkout has none and uses this interpreter.
    python = session_env.ensure() if session_env.LOCK.exists() else None
    # What this container lets a session confine itself with, in the log
    # where the operator can read it, then the math tools' self-test on the
    # session interpreter (isolation_report.py says why; HD11).
    from pathlib import Path

    from deploy.hosted import isolation_report

    root = Path(__file__).resolve().parents[2]
    isolation_report.log(python, root)
    confinement = isolation_report.verify_confinement(python, root)
    server = build(python, confinement)
    # `ssr_mode` is ignored on Hugging Face, where the deployment sets
    # GRADIO_SSR_MODE=false instead; it is passed for a local run.
    server.launch(ssr_mode=False,
                  server_name=os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0"),  # nosec B104 - the platform's proxy is the only thing in front of it
                  server_port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")))
