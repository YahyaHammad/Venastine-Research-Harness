"""
tui/themes.py

Fourteen themes: the eight-theme neutral grid (dark/light x
plain/red/green/blue) plus six standalone tinted themes (matrix,
nightmare, ember, midnight, glassy-lapis, paper) whose PANELS carry the
identity -- background/surface/panel are theirs, not the grid's shared
neutrals.

Pure presentation -- no import of core/, no harness state, with one
narrowing batch 64 wrote down rather than hid: `styles_for` keeps the
set of theme names it has already complained about, because it runs
once per rendered line and the complaint must not. Two things
outside this module carry a theme name: the persisted `tui.theme` setting,
validated by core/config_loader.py's _KNOWN_TUI, and tui/preferences.py's
remembered choice. The second deliberately does NOT validate against
THEME_NAMES -- it stores an opaque string and lets the App decide whether
it still resolves, because ctrl+p's command palette can select one of
Textual's own built-in themes and a whitelist here would silently forget
it.

Built on textual.theme.Theme (verified against the installed textual,
per D22's rule about not assuming a dependency's API shape). Textual
derives the full variable set from these anchors, so each theme only
declares the colours that actually differ.

`Theme` gained an `ansi: bool` when the pin moved to 8.2.8, and nothing
here sets it: these fourteen are RGB palettes, which is the whole
premise of the contrast floors they are held to.
"""

import logging

from textual.color import Color
from textual.theme import Theme

logger = logging.getLogger(__name__)

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
    # dark-plain's ACCENT was lifted in batch 41 (X1), for batch 29's
    # reason applied to a different axis. #14 measured contrast against
    # the BACKGROUND; this is separation between two roles that sit on
    # adjacent lines. `tool` renders in accent and `thinking` in
    # secondary, and on a deliberately monochrome theme hue cannot
    # separate them -- only lightness can. #a9b2c3 was 98.5 redmean
    # units from #8792a2, the tightest secondary/accent pair of all
    # fourteen themes (the next is paper at 143.8). #b8c1d1 is 142.7,
    # which clears that floor while staying 111 units off the
    # foreground -- go lighter and the assistant label melts into the
    # body text it introduces, which is the same defect facing the
    # other way.
    "plain": ("#8a93a3", "#8792a2", "#b8c1d1"),
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

#: How far a diff row's background is blended TOWARD the theme's own
#: background (batch 41, X7). 0.8 leaves a band that stands off the
#: page by 1.22-1.53 across the fourteen themes while the foreground
#: still reads on it at 9.3:1 or better -- a highlight rather than a
#: slab. Both floors are pinned in tests/test_themes.py.
DIFF_TINT = 0.8


#: The eight Theme slots `role_styles` reads. Every one of them but
#: `primary` is Optional[str] on textual's own Theme, and four of the
#: twenty-one built-ins the command palette offers do leave one blank
#: or fill it with a vocabulary Rich cannot read: textual-dark has no
#: `background`, textual-light no `foreground`, and ansi-dark and
#: ansi-light nothing but `ansi_*` names. Reading them raw is what made
#: selecting textual-dark kill the harness AT MOUNT -- and keep killing
#: it, since watch_theme had already remembered the name (batch 64).
_SLOTS = ("primary", "secondary", "accent", "warning", "error",
          "success", "foreground", "background")

#: Filled palettes, keyed on the RAW SLOT VALUES rather than on
#: theme.name. Theme is a plain mutable dataclass and
#: App.register_theme overwrites by name, so a name key would hand a
#: re-registered theme the colours of the one it replaced; a value key
#: cannot. Memoisation of a pure function of its input, which is why
#: it costs this module nothing the docstring claims -- and it earns
#: its place because the fallback below is 0.194ms against
#: role_styles' own 0.004ms, on a function called once per drawn line.
_RESOLVED: dict[tuple, dict[str, str]] = {}


def _rich(colour: str) -> str:
    """Textual's `ansi_*` colour names in Rich's vocabulary.

    An ANSI theme fills every slot with `ansi_blue`/`ansi_default` --
    the terminal's own sixteen, which is the entire point of those
    themes -- and Rich's Style.parse does not know the prefix. It knows
    the names underneath it: textual.color.ANSI_COLORS is exactly the
    ANSI subset of rich.color.ANSI_COLOR_NAMES (verified by set
    difference against the installed package -- 16 of 235, re-checked
    when the pin moved to 8.2.8), and `ansi_default` is Rich's
    `default`. A translation, then, rather than a guess.

    There was ONE such theme (`textual-ansi`) until 8.2.5 replaced it
    with `ansi-dark` and `ansi-light`, and `Theme` grew an `ansi` flag
    in the same release. Nothing here reads the flag: a name can be
    renamed again and the `ansi_` prefix is the thing this function is
    actually about.

    Not doing it was never a crash, which is why it lasted: Rich's
    Text.render resolves a style string with `default=Style.null()`,
    so an unparseable one renders PLAIN and says nothing. Twenty-five
    of the thirty-four roles were silently unstyled on the ANSI theme --
    the uniformly white transcript this section exists to fix, reached
    by a different road.
    """
    return colour[5:] if colour.startswith("ansi_") else colour


def _palette(theme: Theme) -> dict[str, str]:
    """`_SLOTS`, every one of them filled, in Rich's vocabulary.

    A blank slot is filled from textual's OWN derivation --
    to_color_system().generate() is what it builds app.tcss's
    $background and $foreground out of -- rather than from a constant
    invented here, which would be a second opinion about a colour the
    rest of the screen already has.

    A FALLBACK and never a replacement, for two measured reasons.
    generate() is lossy: the base shade comes back as
    `color.lighten(0).hex`, an HSL round trip, which moves #d9a441 to
    #D8A441 and differs from the raw slot on 41 values across the
    fourteen shipped themes. And it is 48x slower than role_styles
    itself. Reaching for it only where a slot is actually None leaves
    all fourteen byte-identical and costs the other nineteen built-ins
    eight getattrs.
    """
    raw = tuple(getattr(theme, slot) for slot in _SLOTS)
    if all(raw):
        return {slot: _rich(value) for slot, value in zip(_SLOTS, raw)}
    key = raw + (theme.dark,)
    if key not in _RESOLVED:
        generated = theme.to_color_system().generate()
        _RESOLVED[key] = {slot: _rich(value or generated[slot])
                          for slot, value in zip(_SLOTS, raw)}
    return _RESOLVED[key]


def _tint(colour: str, background: str,
          factor: float = DIFF_TINT) -> str | None:
    """`colour` faded toward `background`, as a hex string -- or None.

    Derived from the theme's own slots rather than written down, for
    the reason the whole of this section exists (#116): a literal pair
    of green and red backgrounds would be designed against one of the
    fourteen palettes and wrong on the other thirteen -- and on the six
    standalone themes, whose panels carry the identity, visibly so.

    None when either end is one of the terminal's own sixteen, which
    is the ANSI themes and nothing else: there is no RGB there to blend,
    so there is no band to draw. Not a hypothetical guard -- textual's
    Color.parse raises on Rich's `default`, and blending two ANSI
    colours in textual hands back one of them unchanged, so both
    answers were wrong before this said so.
    """
    if not (colour.startswith("#") and background.startswith("#")):
        return None
    return Color.parse(colour).blend(Color.parse(background), factor).hex


def role_styles(theme: Theme) -> dict[str, str]:
    """Rich style strings keyed by transcript role, for one theme.

    Roles are what a line MEANS, not where it came from: `pass` covers a
    research pass boundary whether the CLI or the TUI produced it, and
    `tool_error` is a failed tool call rather than "amber".

    Reads the RESOLVED palette rather than the Theme's own slots,
    which is batch 64: four of the thirty-five themes ctrl+p offers
    leave a slot at None or fill it with textual's `ansi_*` names,
    and one of them took the whole app down at mount. `_palette`
    fills and translates; nothing below can see the difference.
    """
    palette = _palette(theme)
    add = _tint(palette["success"], palette["background"])
    delete = _tint(palette["error"], palette["background"])
    return {
        # Who is speaking. The hues, because these distinguish identity.
        "user": f"bold {palette['primary']}",
        "user_label": f"bold {palette['primary']}",
        "assistant_label": f"bold {palette['accent']}",
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
        "thinking": f"italic {palette['secondary']}",
        "pass": f"bold {palette['secondary']}",
        "pass_done": palette["secondary"],
        # PLAIN accent, not `dim` (batch 41, X1). Two reasons, and the
        # second is the one that generalises. `dim {accent}` sat a
        # measured 98.5 redmean units from `thinking`'s
        # `italic {secondary}` on dark-plain -- the shipped default,
        # and the tightest pair of any theme (every other theme is
        # >= 143) -- so a tool call and a reasoning line were one
        # colour on the palette most people actually see. And `dim` is
        # a Rich ATTRIBUTE, not a colour: tests/test_themes.py's
        # contrast floors measure the undimmed hue and cannot see what
        # the terminal paints, so the style on EVERY tool call was the
        # one style whose real contrast nothing measured. Plain accent
        # moves it up, away from secondary, into a number the floors
        # can see.
        "tool": palette["accent"],
        # How bad it is. The three shared colours.
        "tool_error": palette["warning"],
        # BOLD, where tool_error is plain (X2). Both are non-fatal and
        # keep the hue family; the weight separates the harness raising
        # its voice from a tool that failed. They were the same string
        # outright until batch 41, which was survivable only because
        # nothing routed a WARNING here at all -- see
        # TranscriptLogHandler, where that was the actual defect.
        "warning": f"bold {palette['warning']}",
        "error": f"bold {palette['error']}",
        "success": palette["success"],
        # Confidence tiers, ordered worst-to-best in meaning rather than in
        # this dict. UNVERIFIED_COVERAGE is a gap in what was asked, not a
        # claim that failed, so it reads as absent rather than as wrong.
        "HIGH": palette["success"],
        "MEDIUM": palette["foreground"],
        "LOW": palette["warning"],
        "UNVERIFIED": palette["error"],
        "UNVERIFIED_COVERAGE": f"dim {palette['error']}",
        # §41's inline diff for `write` and `edit`. The only roles in
        # this table that set a BACKGROUND, because the whole point is
        # that a changed row is marked across its full width rather
        # than at the one character that carries the sign.
        #
        # Unless there is no background to set. `_tint` answers None on
        # a theme whose colours are the terminal's own sixteen --
        # an ANSI theme and nothing else -- and the row then takes the
        # severity colour as its FOREGROUND, which is what git, diff
        # and patch all do in a sixteen-colour terminal. A solid band
        # was the alternative, and DIFF_TINT's comment above turns that
        # down for every theme that CAN be measured; picking it for the
        # one that cannot would be backwards.
        "diff_add": (f"{palette['foreground']} on {add}" if add
                     else palette["success"]),
        "diff_del": (f"{palette['foreground']} on {delete}" if delete
                     else palette["error"]),
        # Unchanged context recedes: it is there to place the change,
        # not to be read. `secondary` rather than a dimmed foreground
        # for X1's reason -- `dim` is an attribute no floor can
        # measure.
        "diff_context": palette["secondary"],
        # The file being changed. Bold foreground takes no hue at all,
        # which is what keeps it out of the identity roles the same
        # batch just separated -- a path is not a speaker.
        "diff_header": f"bold {palette['foreground']}",
        # Batch 53's markdown marks. These are marks INSIDE an entry rather
        # than kinds of line, which is why they sit outside MESSAGE_ROLES
        # exactly as the diff roles above do: the pairwise-distinctness and
        # separation floors ask whether two LINES can be told apart, and a
        # bold word never sits on the line below a tool call.
        #
        # This does narrow `assistant`'s "stays plain foreground
        # DELIBERATELY" a few lines up, and the narrowing is the decision:
        # that rule is about the BODY, whose length is the reason tinting it
        # costs more contrast than it buys. A heading and a bold run are
        # marks the model asked for, on a few cells at a time.
        #
        # A heading takes weight and no hue, `diff_header`'s reasoning --
        # a section title is not a speaker either. Strong takes the
        # attribute alone, because the one thing `**` means is emphasis
        # within body text and a colour would make it a different KIND of
        # line. Inline code borrows `secondary`, the transcript's quieter
        # slot, so an identifier reads as set apart from the prose without
        # competing with the accent a tool call uses.
        "md_heading": f"bold {palette['foreground']}",
        "md_strong": "bold",
        "md_code": palette["secondary"],
        # Batch 58's three additions, and the shape of the first two is
        # `md_strong`'s: an inline mark says something about the WORDS it
        # covers, so it takes the attribute and no hue -- a colour would
        # make it a different kind of line rather than a stressed one.
        "md_em": "italic",
        "md_strike": "strike",
        # A link takes UNDERLINE and no hue either, and here the reason is
        # the palette rather than the principle: `accent` is already the
        # assistant's own label and every tool call, and `secondary` is
        # reasoning and inline code. Underline is the one link convention
        # every terminal shares, and it costs no slot to say it.
        "md_link": "underline",
        # A list marker recedes for `diff_context`'s reason -- it is there
        # to place the text, not to be read -- so the eye lands on the
        # first word rather than on the hyphen the model happened to type.
        "md_bullet": palette["secondary"],
        # The table's own furniture. The border recedes for
        # `diff_context`'s reason -- it is there to place the cells, not to
        # be read -- and the header row is the heading rule applied inside
        # the grid, so a table and a `##` above it agree about what a title
        # looks like.
        "table_border": palette["secondary"],
        "table_header": f"bold {palette['foreground']}",
    }


#: Theme names already complained about, so the warning below is said
#: once each rather than once per line drawn.
_UNSTYLABLE: set[str] = set()


def styles_for(app) -> dict[str, str]:
    """The active theme's role styles, or unstyled when there is no app.

    A widget is constructed before it is mounted and tests build them bare
    (`ResearchProgress()` in test_pipeline_events.py), where `self.app`
    RAISES rather than returning None. Falling back to empty strings keeps
    a widget renderable in both cases -- Rich treats "" as no style -- so
    presentation degrades instead of a NoActiveAppError reaching a test
    that is not about theming at all.

    The SECOND guard is batch 64's, and it is the one that had to be
    learned. role_styles used to be able to RAISE -- a theme with a
    slot at None reached Color.parse(None) -- and this function is on
    the path of every transcript line, the session banner in on_mount
    included. So a theme the command palette offered took the app down
    at startup and kept taking it down, because watch_theme had
    already written the name to a preference store that lives at the
    USER tier, outside the install tree, where reinstalling cannot
    reach it. _palette makes that particular fault impossible; this
    makes the CLASS of it non-fatal, which is Section 27's rule that a
    display failure is contained rather than fatal. An uncoloured
    transcript is unhelpful; a harness that will not start is not a
    harness.

    It warns, ONCE per theme. That is the opposite of
    preferences._remember's deliberate non-latching, and for the
    reason that module states: a warning fires there on a human's own
    action, and fires here on every line drawn. Silence is precisely
    what let textual-light and the ANSI themes render unstyled for as
    long as they did -- Rich resolves an unparseable style with
    `default=Style.null()` and says nothing -- so a contained failure
    that reported nothing would be the same defect wearing a better
    exception story.
    """
    try:
        theme = app.get_theme(app.theme)
    except Exception:  # noqa: BLE001 -- no app, or a theme name we lost
        theme = None
    if theme is None:
        return {}
    try:
        return role_styles(theme)
    except Exception:  # noqa: BLE001 -- a theme this build cannot resolve
        name = getattr(theme, "name", "?")
        if name not in _UNSTYLABLE:
            _UNSTYLABLE.add(name)
            logger.warning(
                "Theme %r could not be resolved into transcript "
                "colours, so the transcript will render unstyled. Pick "
                "another with /theme or ctrl+p.", name)
        return {}


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
