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
          sign_in_secrets: dict | None = None):
    """The Gradio server with the bridge mounted; not yet launched.
    `python` is the session interpreter (session_env.py), or None to run
    sessions on this one, as a local checkout does. `confinement` is the
    math tools' self-test verdict (isolation_report.py). `sign_in_secrets`
    are the sign-ins' client secrets by variable name, out of the
    environment (HD13)."""
    import gradio as gr

    from deploy.hosted.web import build_bridge

    bridge = build_bridge(python=python, confinement=confinement,
                          sign_in_secrets=sign_in_secrets)
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


def take_secrets(environ=None) -> dict:
    """Every sign-in secret by variable name -- each ModelScope app's
    registered by hand, and a Studio's own app's `OAUTH_CLIENT_SECRET`,
    which ModelScope's Studio OAuth injects -- every one REMOVED from the
    environment (HD13), so no process this one starts inherits it -- not uv
    building the session environment, not the isolation probes, not a
    session, whose environment is an allowlist anyway. One not set maps to
    "". `/proc/<pid>/environ` still shows the block this process started
    with, which is why main() also makes it non-dumpable."""
    from deploy.hosted.sign_in import SECRET_ENVS

    environ = os.environ if environ is None else environ
    return {name: environ.pop(name, "").strip() for name in SECRET_ENVS}


def drop_spaces_flag(environ=None) -> bool:
    """On a ModelScope Studio, remove `SYSTEM`, which its Studio OAuth sets
    to `spaces` for Gradio's own sign-in button (batch 177); True when it
    did. Gradio 6.30 reads `SYSTEM=spaces` as running on a Hugging Face
    Space and then starts that platform's hot-reload watcher from the
    `spaces` package (gradio/http_server.py), among other Space-only
    defaults (gradio/blocks.py: `api_open`, `pwa`). The bridge
    uses no Gradio sign-in, ModelScope's docs say a non-Gradio app should
    not depend on these variables, and the Studio was measured without it.
    `STUDIO_ID` is ModelScope's and never Hugging Face's, which is how a
    Space keeps its own `SYSTEM`."""
    environ = os.environ if environ is None else environ
    if not environ.get("STUDIO_ID"):
        return False
    return environ.pop("SYSTEM", None) is not None


def main() -> None:
    # FIRST, before anything can be started with this environment or read
    # it: the bridge is the one process that holds a deployment's secrets,
    # and a session runs as the same user, so the bridge's memory and
    # environ are refused to it the way a session's are to another (HD6).
    secrets = take_secrets()
    from security.hosted import make_undumpable

    undumpable = make_undumpable()
    dropped = drop_spaces_flag()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger(__name__).info("bridge non-dumpable: %s", undumpable)
    if dropped:
        logging.getLogger(__name__).info(
            "SYSTEM=spaces removed: this is a ModelScope Studio, not a Space")
    from pathlib import Path

    from deploy.hosted.web import build_label

    # Which commit this is, where the operator can read it: a private Studio
    # answers an anonymous request with 403, so its log is where a deploy is
    # confirmed (scripts/deploy_demo.py looks for this line; batch 169).
    root = Path(__file__).resolve().parents[2]
    logging.getLogger(__name__).info("serving %s", build_label(root))
    # Before anything listens: what a killed predecessor left of its
    # visitors' sessions (supervisor.py says why only here).
    from deploy.hosted.supervisor import sweep_stale_sessions

    sweep_stale_sessions()
    from deploy.hosted import session_env

    # BEFORE the server listens, so the platform shows the Space as starting
    # rather than serving a page whose sessions cannot run. A built bundle
    # always carries the lock; a checkout has none and uses this interpreter.
    python = session_env.ensure() if session_env.LOCK.exists() else None
    # What this container lets a session confine itself with, in the log
    # where the operator can read it, then the math tools' self-test on the
    # session interpreter (isolation_report.py says why; HD11).
    from deploy.hosted import isolation_report

    isolation_report.log(python, root)
    confinement = isolation_report.verify_confinement(python, root)
    server = build(python, confinement, secrets)
    # `ssr_mode` is ignored on Hugging Face, where the deployment sets
    # GRADIO_SSR_MODE=false instead; it is passed for a local run.
    server.launch(ssr_mode=False,
                  server_name=os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0"),  # nosec B104 - the platform's proxy is the only thing in front of it
                  server_port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")))
