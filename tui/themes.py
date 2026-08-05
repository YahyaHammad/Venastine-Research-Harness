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


# ---------------------------------------------------------------------------
# ---- Role palette (ROADMAP_v2 §26) ----------------------------------------
# ---------------------------------------------------------------------------
#
# The transcript was uniformly white: a user message, the model's answer, a
# pipeline trace line and a tool call were the same colour, and the "you"
# label read as the first word of the message it introduced.
#
# WHY THIS IS A FUNCTION OF A THEME rather than a table of colour names.
# Everything else in the TUI styles itself through app.tcss, which uses
# theme variables ($panel, $primary) and so restyles across all eight themes
# without edits. A RichLog cannot do that -- it renders Rich Text objects,
# and a Rich style needs a concrete colour, not a variable to resolve later.
# So the resolution happens here, against the Theme object, and the same
# property holds: no literal appears below, and a new theme needs no change
# in this section.
#
# Only THREE colours are guaranteed to mean the same thing in every theme --
# warning, error and success are shared by _DARK_BASE and _LIGHT_BASE, while
# primary/secondary/accent are the per-variant hues. So severity roles use
# the shared three and identity roles use the hues, rather than the reverse.

def role_styles(theme: Theme) -> dict[str, str]:
    """Rich style strings keyed by transcript role, for one theme.

    Roles are what a line MEANS, not where it came from: `pass` covers a
    research pass boundary whether the CLI or the TUI produced it, and
    `tool_error` is a failed tool call rather than "amber".
    """
    return {
        # Who is speaking. The hues, because these distinguish identity.
        "user": f"bold {theme.primary}",
        "user_label": f"bold {theme.primary}",
        "assistant_label": f"bold {theme.accent}",
        # The answer itself stays plain foreground DELIBERATELY. It is the
        # longest text on screen and the thing most often actually read;
        # tinting it costs contrast to say something the label already said.
        "assistant": "",
        # The harness talking about itself.
        "system": "dim italic",
        "pass": f"bold {theme.secondary}",
        "pass_done": theme.secondary,
        "tool": f"dim {theme.accent}",
        # How bad it is. The three shared colours.
        "tool_error": theme.warning,
        "warning": theme.warning,
        "error": f"bold {theme.error}",
        "success": theme.success,
        # Confidence tiers, ordered worst-to-best in meaning rather than in
        # this dict. UNVERIFIED_COVERAGE is a gap in what was asked, not a
        # claim that failed, so it reads as absent rather than as wrong.
        "HIGH": theme.success,
        "MEDIUM": theme.foreground,
        "LOW": theme.warning,
        "UNVERIFIED": theme.error,
        "UNVERIFIED_COVERAGE": f"dim {theme.error}",
    }


def styles_for(app) -> dict[str, str]:
    """The active theme's role styles, or unstyled when there is no app.

    A widget is constructed before it is mounted and tests build them bare
    (`ResearchProgress()` in test_pipeline_events.py), where `self.app`
    RAISES rather than returning None. Falling back to empty strings keeps
    a widget renderable in both cases -- Rich treats "" as no style -- so
    presentation degrades instead of a NoActiveAppError reaching a test
    that is not about theming at all.
    """
    try:
        theme = app.get_theme(app.theme)
    except Exception:  # noqa: BLE001 -- no app, or a theme name we lost
        theme = None
    if theme is None:
        return {}
    return role_styles(theme)


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
