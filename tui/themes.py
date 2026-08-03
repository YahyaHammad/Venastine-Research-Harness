"""
tui/themes.py

ROADMAP_v2 §16. Eight themes: dark/light x plain/red/green/blue.

Pure presentation -- no import of core/, no harness state. The only thing
outside this module that knows a theme name is the persisted `tui.theme`
setting, validated by core/config_loader.py's _KNOWN_TUI.

Built on textual.theme.Theme (verified against the pinned textual 1.0.0,
per D22's rule about not assuming a dependency's API shape). Textual
derives the full variable set from these anchors, so each theme only
declares the colours that actually differ.
"""

from textual.theme import Theme

# Shared neutrals. The accent variants below change only the three hue
# slots (primary/secondary/accent), so a colour tweak to the base surfaces
# lands in all eight themes at once rather than eight near-copies drifting.
_DARK_BASE = {
    "background": "#0f1115",
    "surface": "#171a21",
    "panel": "#1e222b",
    "foreground": "#e4e6eb",
    "warning": "#d9a441",
    "error": "#cf5c4a",
    "success": "#5aa86f",
    "dark": True,
}

_LIGHT_BASE = {
    "background": "#f7f7f5",
    "surface": "#ffffff",
    "panel": "#eceae6",
    "foreground": "#1b1c1e",
    "warning": "#9a6d10",
    "error": "#a63a2a",
    "success": "#2f7343",
    "dark": False,
}

# (primary, secondary, accent) per variant. Dark and light get separate
# values because a hue readable on #0f1115 is usually too pale on #f7f7f5.
_ACCENTS_DARK = {
    "plain": ("#8a93a3", "#5f6879", "#a9b2c3"),
    "red": ("#d4674f", "#96422f", "#e8917b"),
    "green": ("#5fa876", "#3d7350", "#8ecba1"),
    "blue": ("#5b90cc", "#3a6294", "#8bb6e6"),
}

_ACCENTS_LIGHT = {
    "plain": ("#5a6273", "#3c4252", "#7d8595"),
    "red": ("#b04a33", "#7d3423", "#c9705a"),
    "green": ("#3f7f57", "#2b5a3c", "#5d9c74"),
    "blue": ("#3f6fa8", "#2b4d76", "#6091c7"),
}


def _build(variant: str, dark: bool) -> Theme:
    base = _DARK_BASE if dark else _LIGHT_BASE
    primary, secondary, accent = (
        _ACCENTS_DARK if dark else _ACCENTS_LIGHT
    )[variant]
    return Theme(
        name=f"{'dark' if dark else 'light'}-{variant}",
        primary=primary,
        secondary=secondary,
        accent=accent,
        **base,
    )


ALL_THEMES = [
    _build(variant, dark)
    for dark in (True, False)
    for variant in ("plain", "red", "green", "blue")
]

THEME_NAMES = [t.name for t in ALL_THEMES]
DEFAULT_THEME = "dark-plain"


def register_all(app) -> None:
    """Register every theme on the app. Call before setting app.theme."""
    for theme in ALL_THEMES:
        app.register_theme(theme)


def resolve(name: str | None) -> str:
    """Theme name to apply, falling back to the default.

    Returns the default rather than raising: config_loader already
    type-checks `tui.theme` as a string, but it cannot know the valid
    names without importing this module, and a stale name in a settings
    file should not stop the app from starting.
    """
    if name in THEME_NAMES:
        return name
    return DEFAULT_THEME
