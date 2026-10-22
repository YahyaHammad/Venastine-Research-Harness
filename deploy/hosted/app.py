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


def build(python=None, confinement: str = "",
          modelscope_secret: str | None = None):
    """The Gradio server with the bridge mounted; not yet launched.
    `python` is the session interpreter (session_env.py), or None to run
    sessions on this one, as a local checkout does. `confinement` is the
    math tools' self-test verdict (isolation_report.py). `modelscope_secret`
    is the ModelScope app's client secret, out of the environment (HD13)."""
    import gradio as gr

    from deploy.hosted.web import build_bridge

    bridge = build_bridge(python=python, confinement=confinement,
                          modelscope_secret=modelscope_secret)
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


def take_secret(environ=None) -> str:
    """The ModelScope app's client secret, REMOVED from the environment
    (HD13), so no process this one starts inherits it -- not uv building the
    session environment, not the isolation probes, not a session, whose
    environment is an allowlist anyway. `/proc/<pid>/environ` still shows
    the block this process started with, which is why main() also makes it
    non-dumpable."""
    from deploy.hosted.sign_in import SECRET_ENV

    environ = os.environ if environ is None else environ
    return environ.pop(SECRET_ENV, "").strip()


def main() -> None:
    # FIRST, before anything can be started with this environment or read
    # it: the bridge is the one process that holds a deployment's secret,
    # and a session runs as the same user, so the bridge's memory and
    # environ are refused to it the way a session's are to another (HD6).
    secret = take_secret()
    from security.hosted import make_undumpable

    undumpable = make_undumpable()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger(__name__).info("bridge non-dumpable: %s", undumpable)
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
    server = build(python, confinement, secret)
    # `ssr_mode` is ignored on Hugging Face, where the deployment sets
    # GRADIO_SSR_MODE=false instead; it is passed for a local run.
    server.launch(ssr_mode=False,
                  server_name=os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0"),  # nosec B104 - the platform's proxy is the only thing in front of it
                  server_port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")))
