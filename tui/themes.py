"""
tui/themes.py

Fourteen themes: the eight-theme neutral grid (dark/light x
plain/red/green/blue) plus six standalone tinted themes (matrix,
nightmare, ember, midnight, glassy-lapis, paper) whose PANELS carry the
identity -- background/surface/panel are theirs, not the grid's shared
neutrals.

Pure presentation -- no import of core/, no harness state. Two things
outside this module carry a theme name: the persisted `tui.theme` setting,
validated by core/config_loader.py's _KNOWN_TUI, and tui/preferences.py's
remembered choice. The second deliberately does NOT validate against
THEME_NAMES -- it stores an opaque string and lets the App decide whether
it still resolves, because ctrl+p's command palette can select one of
Textual's own built-in themes and a whitelist here would silently forget
it.

Built on textual.theme.Theme (verified against the pinned textual 1.0.0,
per D22's rule about not assuming a dependency's API shape). Textual
derives the full variable set from these anchors, so each theme only
declares the colours that actually differ.
"""

from textual.theme import Theme

# Shared neutrals. The accent variants below change only the three hue
# slots (primary/secondary/accent), so a colour tweak to the base surfaces
# lands in all eight grid themes at once rather than eight near-copies
# drifting.
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
#
# The four DARK secondaries were lifted in batch 29 (#14): the pass role
# renders in `secondary`, and every original value sat below 3.5:1 against
# the shared dark background -- plain at 3.37 was the filed defect, red
# ~2.7 and blue ~3.0 were the same defect nobody had measured. The floor
# is pinned in tests/test_themes.py; these are the minimal luminance moves
# that clear it.
_ACCENTS_DARK = {
    "plain": ("#8a93a3", "#8792a2", "#a9b2c3"),
    "red": ("#d4674f", "#b0513c", "#e8917b"),
    "green": ("#5fa876", "#4d8f63", "#8ecba1"),
    "blue": ("#5b90cc", "#4a76b3", "#8bb6e6"),
}

_ACCENTS_LIGHT = {
    "plain": ("#5a6273", "#3c4252", "#7d8595"),
    "red": ("#b04a33", "#7d3423", "#c9705a"),
    "green": ("#3f7f57", "#2b5a3c", "#5d9c74"),
    "blue": ("#3f6fa8", "#2b4d76", "#6091c7"),
}

# Standalone tinted themes (batch 29). Each OVERRIDES the shared base
# outright -- that is their identity: the panels themselves carry the
# palette, not just borders and text. Severity slots default to the grid
# trio and are overridden ONLY where contrast against the theme's own
# tint demands it, keeping the semantic hue family (danger stays
# red-ish); every override carries its reason, and the floors live in
# tests/test_themes.py (foreground >= 7:1, severity >= 4:1, identity
# >= 3.5:1, all against the theme's own background).
_STANDALONE = [
    dict(
        name="matrix", dark=True,
        background="#0a120c", surface="#0f1a12", panel="#14231a",
        foreground="#c8e6d0",
        primary="#3dd968", secondary="#2f9e52", accent="#7dffb0",
        # success overridden: the shared #5aa86f dissolves into matrix's
        # own green-dark background.
        success="#4ee08a",
    ),
    dict(
        name="nightmare", dark=True,
        background="#140b0e", surface="#1c0f14", panel="#251318",
        foreground="#e8d5d8",
        primary="#c94f5f", secondary="#a8556a", accent="#e88a96",
        # error overridden: red-on-red -- the shared #cf5c4a loses the
        # alarm against nightmare's blood background.
        error="#ff7b6b",
    ),
    dict(
        name="ember", dark=True,
        background="#16100a", surface="#1f1610", panel="#291d13",
        foreground="#ecdfd0",
        primary="#e08a3c", secondary="#a86228", accent="#f2b06a",
        # warning overridden yellower: the shared amber sat on top of
        # ember's primary orange and read as emphasis, not as caution.
        warning="#d9b83f",
    ),
    dict(
        name="midnight", dark=True,
        background="#0a0f1e", surface="#101830", panel="#16203c",
        foreground="#d5dcea",
        primary="#6f9fe8", secondary="#4a6fb0", accent="#9dbdf2",
    ),
    dict(
        name="glassy-lapis", dark=True,
        background="#14243f", surface="#1c2f52", panel="#243a63",
        foreground="#e2ecf8",
        primary="#7fb4ff", secondary="#5a8ac9", accent="#a8ccff",
        # error overridden: lapis is the lightest dark background, and
        # the shared #cf5c4a fell just under the severity floor on it.
        error="#e06a58",
    ),
    dict(
        name="paper", dark=False,
        background="#f4efe6", surface="#fbf8f2", panel="#eae3d5",
        foreground="#2b2620",
        primary="#7a5c2e", secondary="#5c4a32", accent="#9c7b45",
    ),
]


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


def _build_standalone(spec: dict) -> Theme:
    base = _DARK_BASE if spec["dark"] else _LIGHT_BASE
    merged = {**base, **{k: v for k, v in spec.items() if k != "name"}}
    return Theme(name=spec["name"], **merged)


ALL_THEMES = (
    [
        _build(variant, dark)
        for dark in (True, False)
        for variant in ("plain", "red", "green", "blue")
    ]
    + [_build_standalone(spec) for spec in _STANDALONE]
)

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
# theme variables ($panel, $primary) and so restyles across all fourteen
# themes without edits. A RichLog cannot do that -- it renders Rich Text
# objects, and a Rich style needs a concrete colour, not a variable to
# resolve later. So the resolution happens here, against the Theme
# object, and the same property holds: no literal appears below, and a
# new theme needs no change in this section.
#
# Severity colours. Across the eight GRID themes, warning/error/success
# are shared by _DARK_BASE and _LIGHT_BASE and mean the same thing
# everywhere; primary/secondary/accent are the per-variant hues. A
# STANDALONE theme may override a severity slot when its own panel tint
# would swallow the shared value (matrix's success, nightmare's error,
# ember's warning, glassy-lapis's error -- each with its reason above),
# keeping the semantic hue family. Either way severity uses the
# theme-resolved trio and identity uses the hues, rather than the
# reverse. Floors are pinned in tests/test_themes.py.

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
        # §38: the model's reasoning, when tui.show_thinking renders it
        # inline. `secondary` because thinking is the answer's quieter
        # sibling rather than a severity or an identity -- it already
        # clears the identity floor on every theme, so no theme constant
        # moves for this. Italic separates it from `pass`/`pass_done`,
        # which share the hue in the research view.
        "thinking": f"italic {theme.secondary}",
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


def syntax_theme_for(app) -> str:
    """Which Rich token theme code blocks highlight against (#116).

    Rich ships exactly two: ansi_dark for dark backgrounds, ansi_light for
    light ones. Half the shipped themes are light, and every one of them
    was rendering every code block in a palette designed against the
    opposite background -- on the one transcript element a reader is most
    likely to read character by character.

    Resolved here, against the Theme object's own `dark` flag, for the same
    reason role_styles is: a RichLog cannot reach app.tcss variables, so
    the lookup happens where the Theme object already is. No running app
    (bare-built test widgets) defaults to dark, which preserves what the
    unconditional version did.
    """
    try:
        theme = app.get_theme(app.theme)
    except Exception:  # noqa: BLE001 -- no app, or a theme name we lost
        theme = None
    if theme is not None and not theme.dark:
        return "ansi_light"
    return "ansi_dark"


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
