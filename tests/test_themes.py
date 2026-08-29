"""
test_themes.py

Batch 29: the theme roster grows from the eight-theme neutral grid to
fourteen, six of them STANDALONE -- their panels carry the identity
(background/surface/panel are the theme's own, not the grid's shared
neutrals), and #14's contrast defect is pinned shut for every theme at
once.

Three pins, each one of the ways this file could silently regress:

  - SLOT COMPLETENESS: role_styles() must fill every key for every
    theme. #14 verified as a fact that "every one of the eight themes
    populates every slot" -- verified, not pinned; a fourteenth theme
    missing UNVERIFIED_COVERAGE would otherwise ship green.
  - CONTRAST FLOORS, computed from the Theme objects themselves:
    foreground >= 7:1, severity trio >= 4:1, identity trio >= 3.5:1 on
    dark backgrounds and >= 3:1 on light ones. Two identity tiers
    because WCAG large-text AA is 3.0 and the filed defect (#14) was
    specifically DARK secondaries -- plain's pass role at 3.37, red at
    ~2.7, blue at ~3.0. The four dark secondaries were nudged to clear
    the dark floor; the light grid already cleared the light one.
  - THE GRID IS UNCHANGED: the first eight names and every accent trio
    except the four nudged secondaries are exactly what shipped before
    this batch. A rename or reorder here breaks every persisted
    `tui.theme` value at once.
"""

import pytest

from tests.conftest import pump
from tui import themes
from tui.themes import ALL_THEMES, THEME_NAMES, role_styles

GRID_NAMES = [
    "dark-plain", "dark-red", "dark-green", "dark-blue",
    "light-plain", "light-red", "light-green", "light-blue",
]
STANDALONE_NAMES = [
    "matrix", "nightmare", "ember", "midnight", "glassy-lapis", "paper",
]

EXPECTED_ROLE_KEYS = {
    "user", "user_label", "assistant_label", "assistant", "system",
    "thinking", "pass", "pass_done", "tool", "tool_error", "warning",
    "error", "success", "HIGH", "MEDIUM", "LOW", "UNVERIFIED",
    "UNVERIFIED_COVERAGE",
    # Batch 41 (X7). The only roles that set a BACKGROUND.
    "diff_add", "diff_del", "diff_context", "diff_header",
}


# ---- WCAG contrast, computed rather than trusted ----------------------------

def _lin(channel: float) -> float:
    channel /= 255.0
    return channel / 12.92 if channel <= 0.04045 \
        else ((channel + 0.055) / 1.055) ** 2.4


def _lum(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _ratio(fg: str, bg: str) -> float:
    hi, lo = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _rgb(hex_colour: str):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _apart(a: str, b: str) -> float:
    """Perceptual distance between two colours ("redmean", 0..~765).

    NOT `_ratio`. WCAG contrast answers "can this be read against that
    background", which is the question the floors below ask. The question
    batch 41 had to answer is different -- "can a reader tell these two
    KINDS OF LINE apart" -- and contrast is the wrong instrument for it:
    dark-plain's accent and its warning sit at a luminance ratio of 1.05
    and are obviously different (grey against amber), while its accent and
    its secondary sat at 1.48 and were the pair the defect was filed
    about. Luminance cannot see hue; this can.
    """
    r1, g1, b1 = _rgb(a)
    r2, g2, b2 = _rgb(b)
    mean_r = (r1 + r2) / 2
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return ((2 + mean_r / 256) * dr * dr
            + 4 * dg * dg
            + (2 + (255 - mean_r) / 256) * db * db) ** 0.5


# ---- Roster -----------------------------------------------------------------

def test_the_grid_names_are_unchanged_and_come_first():
    """A persisted `tui.theme: "dark-green"` must keep meaning dark-green.
    New names append; existing names never move."""
    assert THEME_NAMES[:8] == GRID_NAMES
    assert THEME_NAMES[8:] == STANDALONE_NAMES
    assert themes.DEFAULT_THEME == "dark-plain"


def test_every_standalone_name_resolves():
    for name in STANDALONE_NAMES:
        assert themes.resolve(name) == name, \
            f"{name} registered but resolve() fell back to the default"


def test_paper_is_the_only_new_light_theme():
    by_name = {t.name: t for t in ALL_THEMES}
    for name in STANDALONE_NAMES[:5]:
        assert by_name[name].dark is True, name
    assert by_name["paper"].dark is False, \
        "paper is the light counterweight; ansi_light code blocks follow \
from this flag"


# ---- Slot completeness -------------------------------------------------------

@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
def test_every_theme_fills_every_role_slot(theme):
    styles = role_styles(theme)
    assert set(styles) == EXPECTED_ROLE_KEYS
    empty = [k for k, v in styles.items()
             if not v and k != "assistant"]
    assert empty == [], \
        f"{theme.name}: empty role styles {empty} -- the assistant body is \
the one deliberately unstyled slot"


# ---- Batch 41 (X1/X2): the roles a transcript puts next to each other ---------

#: Every role a line in the transcript can be painted with. NOT the tier
#: roles, which live in the claims view and never sit beside these; and not
#: `assistant`, which is deliberately unstyled (the answer is the longest
#: text on screen and tinting it costs contrast to say what the label
#: already said).
#:
#: `user_label` is absent on purpose: it and `user` are the two halves of
#: ONE line (`you ›  the message`) and are meant to match. Colliding there
#: is the design, not the defect.
MESSAGE_ROLES = [
    "system", "thinking", "tool", "tool_error", "warning", "error",
    "user", "assistant_label", "pass", "pass_done", "success",
]


@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
def test_the_message_roles_are_pairwise_distinct(theme):
    """The reported defect, pinned so it cannot come back quietly.

    A tool call, a reasoning line and a routed WARNING rendered as one
    grey blur: `tool` was `dim {accent}`, `thinking` is `italic
    {secondary}`, and across the grid themes accent and secondary are one
    luminance step apart in the same hue -- so dimming accent lands it on
    secondary. `warning` and `tool_error` were the same string outright.

    Distinctness of the STYLE STRING rather than of the colour, because
    the strings are what Rich is handed: `italic #8792a2` and `#8792a2`
    are two different renderings of one hue and that is a legitimate
    separation (themes.py says so for thinking vs pass_done), while two
    identical strings cannot be anything but the same line twice.
    """
    styles = role_styles(theme)
    seen = {}
    for role in MESSAGE_ROLES:
        style = styles[role]
        clash = seen.get(style)
        assert clash is None, (
            f"{theme.name}: {role!r} and {clash!r} both render as "
            f"{style!r} -- two kinds of line the reader cannot tell apart"
        )
        seen[style] = role


def test_every_message_role_is_actually_reachable():
    """MESSAGE_ROLES above is a hand-written list, which is the shape this
    project keeps catching drift in. This is the cheap half: every role it
    names must exist in the palette, so a rename fails here rather than
    silently shrinking what the distinctness check covers."""
    styles = role_styles(ALL_THEMES[0])
    missing = [r for r in MESSAGE_ROLES if r not in styles]
    assert not missing, f"MESSAGE_ROLES names roles the palette lacks: {missing}"


# ---- Contrast floors ----------------------------------------------------------

@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
def test_foreground_is_body_readable(theme):
    assert _ratio(theme.foreground, theme.background) >= 7.0, theme.name


@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
@pytest.mark.parametrize("slot", ["warning", "error", "success"])
def test_severity_roles_meet_the_floor(theme, slot):
    colour = getattr(theme, slot)
    assert _ratio(colour, theme.background) >= 4.0, \
        f"{theme.name}.{slot} = {colour} at {_ratio(colour, theme.background):.2f}"


@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
@pytest.mark.parametrize("slot", ["primary", "secondary", "accent"])
def test_identity_roles_meet_the_floor(theme, slot):
    # Two tiers: the filed defect was DARK secondaries (plain's pass at
    # 3.37); light backgrounds give dim accents more lift, and WCAG
    # large-text AA is 3.0.
    floor = 3.5 if theme.dark else 3.0
    colour = getattr(theme, slot)
    assert _ratio(colour, theme.background) >= floor, \
        f"{theme.name}.{slot} = {colour} at {_ratio(colour, theme.background):.2f} \
(floor {floor})"


def test_the_dark_secondaries_that_were_nudged_are_named():
    """#14's four concrete regressions, pinned by value: reverting any of
    these hexes back to the pre-batch-29 colour fails the identity floor,
    and this test says WHICH theme broke rather than leaving it to the
    parametrised contrast run."""
    by_name = {t.name: t for t in ALL_THEMES}
    expected = {
        "dark-plain": "#8792a2",
        "dark-red": "#b0513c",
        "dark-green": "#4d8f63",
        "dark-blue": "#4a76b3",
    }
    for name, hex_colour in expected.items():
        assert by_name[name].secondary.lower() == hex_colour, name


# ---- Standalone identity: the panels themselves ------------------------------

def test_standalone_panels_are_tinted_not_grid_neutral():
    """The entire point of the standalone themes (#batch 29): their
    background/surface/panel are THEIR colours. A future refactor that
    drops the override mechanism would silently turn matrix into
    dark-green with extra steps."""
    by_name = {t.name: t for t in ALL_THEMES}
    for name in ("matrix", "nightmare", "ember", "midnight",
                 "glassy-lapis"):
        t = by_name[name]
        assert t.background != themes._DARK_BASE["background"], \
            f"{name} fell back to the grid's dark base"
        assert t.panel != themes._DARK_BASE["panel"], name
    paper = by_name["paper"]
    assert paper.background != themes._LIGHT_BASE["background"]
    assert paper.surface != themes._LIGHT_BASE["surface"]


# ---- #183: a theme switch reaches the whole sidebar -------------------------
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_theme_switch_restyles_the_goal_banner(mocker):
    """The banner resolves per draw (#116), so the MECHANISM was always
    there -- what was missing was the poke. Without restyle_sidebar()
    the banner kept the old palette until the next goal change, which
    on the tinted standalone themes is a half-recoloured session."""
    from tui.app import VenastineApp
    from tui.widgets import GoalBanner

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 2)
        banner = app.query_one(GoalBanner)
        captured = []
        real_update = banner.update
        mocker.patch.object(
            banner, "update",
            side_effect=lambda content="": (
                captured.append(content), real_update(content))[1])

        app.memory.set_extra("goal", "ship the themes")
        app.refresh_goal_banner()
        await pilot.pause()
        assert captured[-1].style == "bold #d9a441", \
            "dark-plain's warning hue expected first"

        app.query_one("#prompt").value = "/theme light-red"
        await pilot.press("enter")
        await pilot.pause()

        assert captured[-1].style == "bold #9a6d10", \
            f"banner kept the old palette after /theme: {captured[-1].style!r}"


@pytest.mark.asyncio
async def test_a_theme_switch_pokes_every_rich_sidebar_widget(mocker):
    """Wiring pin: the visual test above proves the banner's path; this
    proves the call fans out to the research panel too (the todo panel
    shares refresh_todo_panel with the banner's path, and the usage line
    is deliberately absent -- see restyle_sidebar's docstring)."""
    from tui.app import VenastineApp
    from tui.widgets import ResearchProgress

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 2)
        # INSTANCE-level patch (the batch-26 lesson: a class-level mock is
        # not a descriptor, so self.restyle() would skip the binding and
        # the lambda would miss its argument).
        panel = app.query_one(ResearchProgress)
        poked = []
        real_restyle = panel.restyle
        mocker.patch.object(
            panel, "restyle",
            side_effect=lambda: (poked.append(1), real_restyle())[1])

        app.query_one("#prompt").value = "/theme matrix"
        await pilot.press("enter")
        await pilot.pause()

    assert poked, "the research panel was never restyled by /theme"


@pytest.mark.asyncio
async def test_bare_theme_command_says_what_a_theme_restyles():
    """#14's discoverability note. With tinted panels, 'theme' is no
    longer a border tweak -- the bare listing is where a user finds
    that out."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 2)
        app.query_one("#prompt").value = "/theme"
        await pilot.press("enter")
        await pilot.pause()
        entries = [txt for _r, txt in app._transcript._entries]

    assert any("restyle panels" in txt for txt in entries), entries[-3:]



# ---- Batch 41 (X1): the two pins that go red if the roles collapse back -----

#: Minimum perceptual separation between the colour behind `tool` and the
#: colour behind `thinking`/`pass_done`. dark-plain sat at 98.5 before this
#: batch -- the tightest secondary/accent pair of all fourteen themes, and
#: the shipped default. Lifting its accent to #b8c1d1 puts it at 142.7; the
#: next-tightest theme (paper) is 143.8. 120 is the floor with headroom on
#: both sides of that gap.
ROLE_SEPARATION_FLOOR = 120.0


@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
def test_a_tool_call_is_visibly_apart_from_a_reasoning_line(theme):
    """The filed defect, measured.

    `tool` renders in accent and `thinking`/`pass_done` in secondary, and
    those two lines sit next to each other constantly -- a tool call, then
    the model reasoning about its result. On a deliberately monochrome
    theme hue cannot separate them, so lightness has to, and dark-plain's
    did not: 98.5 redmean units, against >= 143 everywhere else.

    Asserted on the THEME's slots rather than by parsing the style strings,
    because the mapping (tool -> accent, thinking -> secondary) is what the
    test is about; parsing would let a role move to a different slot and
    still pass.
    """
    assert _apart(theme.accent, theme.secondary) >= ROLE_SEPARATION_FLOOR, (
        f"{theme.name}: accent {theme.accent} and secondary "
        f"{theme.secondary} are {_apart(theme.accent, theme.secondary):.1f} "
        f"apart -- a tool call and a reasoning line read as one colour"
    )


@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
def test_dim_is_the_system_role_alone(theme):
    """`dim` is a Rich ATTRIBUTE, not a colour, so every check in this file
    is blind to it: the contrast floors measure the undimmed hue and the
    separation floor above measures theme slots. A role whose only
    distinguishing mark is `dim` is therefore a role whose real appearance
    nothing here can see -- which is exactly what `tool` was
    (`dim {accent}`), on the one style the transcript draws most often.

    `system` keeps it, and that is the point of the exception rather than
    a hole in the rule: `system` is the harness narrating itself and is
    MEANT to recede, it carries no hue to measure, and it is the only role
    for which "quieter than the body text" is the whole specification.

    Scoped to MESSAGE_ROLES, so it says nothing about the confidence
    tiers. `UNVERIFIED_COVERAGE` is `dim {error}` on purpose and is the
    counter-example that proves the rule is about the transcript: it lives
    in the claims modal directly beside `UNVERIFIED`, which is the same
    hue undimmed, and the dimming is what says "a gap in what was asked,
    not a claim that failed". There the attribute carries meaning against
    a sibling; on a transcript line it carries only a colour nothing can
    measure.
    """
    styles = role_styles(theme)
    dimmed = [role for role in MESSAGE_ROLES
              if "dim" in styles[role].split() and role != "system"]
    assert dimmed == [], (
        f"{theme.name}: {dimmed} render with `dim`. Pick a colour the "
        f"floors in this file can measure, or say here why this role is "
        f"the second one that recedes."
    )


# ---- Batch 41 (X7): the diff tint, derived and measured ---------------------

def _tint_of(theme, slot):
    """The background a diff row is painted with, read out of the style
    string rather than recomputed -- so this measures what the transcript
    actually draws, not a second copy of the formula."""
    return role_styles(theme)[slot].split(" on ")[1]


@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
@pytest.mark.parametrize("slot", ["diff_add", "diff_del"])
def test_a_diff_row_is_readable_on_its_own_tint(theme, slot):
    """A diff row is the one place in the transcript where the background
    is not the theme's own, so the foreground floor has to be re-checked
    against it -- `test_foreground_is_body_readable` measures against
    `theme.background` and knows nothing about this."""
    tint = _tint_of(theme, slot)
    assert _ratio(theme.foreground, tint) >= 4.5, \
        f"{theme.name}.{slot}: text on {tint} at {_ratio(theme.foreground, tint):.2f}"


@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
@pytest.mark.parametrize("slot", ["diff_add", "diff_del"])
def test_the_tint_is_visible_against_the_page(theme, slot):
    """The other half, and the one that actually bounds DIFF_TINT. Blend
    further toward the background and the foreground reads better and
    better while the band the whole feature exists to draw fades out --
    so a floor on readability alone would be satisfied by a tint that is
    not there. 1.15 is the visible-band floor; 0.8 puts the fourteen
    themes between 1.22 and 1.53."""
    tint = _tint_of(theme, slot)
    assert _ratio(tint, theme.background) >= 1.15, \
        f"{theme.name}.{slot}: {tint} against {theme.background} at " \
        f"{_ratio(tint, theme.background):.2f} -- the row is not marked"


@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
def test_added_and_removed_are_told_apart_by_more_than_position(theme):
    """The two tints derive from `success` and `error`, which every theme
    keeps in different hue families -- but two of the standalone themes
    override a severity slot against their own panel tint (nightmare's
    error, matrix's success), and this is what says the overrides did not
    collapse the pair."""
    add, delete = _tint_of(theme, "diff_add"), _tint_of(theme, "diff_del")
    assert _apart(add, delete) >= 25.0, \
        f"{theme.name}: {add} and {delete} are {_apart(add, delete):.1f} apart"

