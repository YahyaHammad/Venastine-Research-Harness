"""The hosted demo's web bridge (ROADMAP_v3 §79).

`supervisor.py` runs one harness process per browser tab and relays Textual's
web driver protocol; `single_user.py` decides whether a private deployment may
run with the full tool set; `web.py` is the Starlette wiring; `app.py` is the
Gradio entry point the Space and the Studio run.
"""
