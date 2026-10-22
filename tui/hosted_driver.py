"""
tui/hosted_driver.py

ROADMAP_v3 §79 (HD13). Textual's web driver, plus the three packets a hosted
bridge sends back about a ModelScope sign-in it is completing for this
session.

The bridge -- `deploy/hosted/supervisor.py` -- runs every hosted session under
this driver. It is the only writer of the session's stdin, and everything a
browser sends reaches it there as screen input (a `D` packet), never as a
meta packet: so a `venastine_*` packet arriving here was written by the
bridge, and a page cannot forge one. The app still accepts a credential only
for the sign-in it is waiting for (tui/app.py), because "only the bridge can
send this" is a property of the deployment, and the app should not depend on
it alone.

Outside a hosted session nothing selects this driver, and nothing sends it
these packets.
"""

from __future__ import annotations

from textual.drivers.web_driver import WebDriver
from textual.message import Message


class BridgeSignInUrl(Message):
    """The bridge started a sign-in: the consent page to open."""

    def __init__(self, request: str, url: str) -> None:
        super().__init__()
        self.request = request
        self.url = url


class BridgeCredential(Message):
    """The bridge traded the pasted code for a token. Never logged: the
    debug breadcrumbs name message types, not their fields (batch 78)."""

    def __init__(self, request: str, token: str) -> None:
        super().__init__()
        self.request = request
        self.token = token


class BridgeSignInFailed(Message):
    """The sign-in did not finish; `retry` when the paste may be tried
    again in the same flow."""

    def __init__(self, request: str, reason: str, retry: bool) -> None:
        super().__init__()
        self.request = request
        self.reason = reason
        self.retry = retry


def bridge_message(packet_type: str, payload: dict) -> Message | None:
    """The app message for one of the bridge's sign-in packets, or None for
    any other packet -- including one of these with a field missing."""
    request = payload.get("id")
    if not isinstance(request, str):
        return None
    if packet_type == "venastine_sign_in_url":
        url = payload.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            return BridgeSignInUrl(request, url)
    elif packet_type == "venastine_credential":
        token = payload.get("token")
        if isinstance(token, str) and token:
            return BridgeCredential(request, token)
    elif packet_type == "venastine_sign_in_failed":
        return BridgeSignInFailed(request, str(payload.get("reason") or
                                               "The sign-in failed."),
                                  payload.get("retry") is True)
    return None


class HostedWebDriver(WebDriver):
    """The web driver a hosted bridge starts its sessions with."""

    def on_meta(self, packet_type: str, payload: dict[str, object]) -> None:
        message = bridge_message(packet_type, payload)
        if message is not None:
            self._app.post_message(message)
            return
        super().on_meta(packet_type, payload)

    def send_to_bridge(self, meta: dict) -> None:
        """One meta packet to the bridge. Call it on the UI thread, which
        is the thread that writes the screen: two threads writing one pipe
        could interleave a packet."""
        self.write_meta(meta)


def send_to_bridge(app, meta: dict) -> bool:
    """Send `meta` through the app's driver when it is this one; False when
    the session is not running under a bridge that can hear it."""
    driver = getattr(app, "_driver", None)
    if not isinstance(driver, HostedWebDriver):
        return False
    driver.send_to_bridge(meta)
    return True
