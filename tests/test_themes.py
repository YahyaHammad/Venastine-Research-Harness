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
from rich.style import Style
from textual.theme import BUILTIN_THEMES, Theme

from tests.conftest import pump
from tui import themes
from tui.themes import ALL_THEMES, THEME_NAMES, role_styles
from tui.widgets import CONVERSATION_ROLES, META_ROLES

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
    # Batch 53. Marks INSIDE an entry rather than kinds of line, which is
    # why they are here and not in MESSAGE_ROLES below: the distinctness
    # and separation floors ask whether two lines can be told apart, and a
    # bold word never sits on the line under a tool call. `md_strong` is
    # the one role in this table that is an attribute with no colour, and
    # it is deliberate -- `**` means emphasis within body text, so a hue
    # would make it a different kind of line rather than a stressed word.
    "md_heading", "md_strong", "md_code",
    # Batch 58. Same argument, one step further: `md_em` and `md_strike`
    # are attributes with no colour for `md_strong`'s reason, `md_link`
    # takes underline because the two hue slots are spoken for (`accent`
    # is the assistant label and every tool line, `secondary` is
    # reasoning and inline code), and `md_bullet` recedes because a
    # marker places the text rather than being read.
    "md_em", "md_strike", "md_link", "md_bullet",
    "table_border", "table_header",
}

#: Every theme ctrl+p's command palette can actually select: Textual's
#: own twelve, registered by App.__init__, beside this project's
#: fourteen. DERIVED, never written down -- a Textual release that adds
#: a theme is covered the day it lands, and one that adds a BROKEN
#: theme fails here rather than in somebody's session.
#:
#: Batch 64 exists because this list did not. Every check in this file
#: parametrised over ALL_THEMES, so three of the twelve built-ins had
#: never once been through role_styles: textual-dark leaves
#: `background` at None, textual-light leaves `foreground` at None, and
#: textual-ansi fills every slot with `ansi_*` names Rich cannot read.
#: Selecting the first of those killed the harness at mount, and kept
#: killing it, because watch_theme had already remembered the name.
SELECTABLE_THEMES = list(BUILTIN_THEMES.values()) + list(ALL_THEMES)


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


# ---- Integrity: can this theme be rendered at all? (batch 64) ----------------
# ------------------------------------------------------------------------------
#
# INTEGRITY over all twenty-six, QUALITY over our fourteen, and the
# split is measured rather than assumed. Held to this file's own floors,
# nine of Textual's twelve fail a contrast check -- textual-dark's
# `secondary` is 1.89:1 against its own background, where the identity
# floor is 3.5, and solarized-light's foreground is 4.99 against a floor
# of 7 -- and three fail pairwise distinctness (textual-dark and
# textual-light both set accent == warning, monokai sets error ==
# secondary). Those floors are decisions about OUR palettes; widening
# them to somebody else's themes would mean either a red suite or floors
# lowered until they said nothing. So the checks below ask only whether
# a theme can be drawn, and the ones after them keep asking whether ours
# are any good.

@pytest.mark.parametrize("theme", SELECTABLE_THEMES, ids=lambda t: t.name)
def test_every_theme_fills_every_role_slot(theme):
    styles = role_styles(theme)
    assert set(styles) == EXPECTED_ROLE_KEYS
    empty = [k for k, v in styles.items()
             if not v and k != "assistant"]
    assert empty == [], \
        f"{theme.name}: empty role styles {empty} -- the assistant body is \
the one deliberately unstyled slot"


@pytest.mark.parametrize("theme", SELECTABLE_THEMES, ids=lambda t: t.name)
def test_every_role_style_is_one_rich_can_parse(theme):
    """The check that would have caught all three, and the only one that
    could have.

    Rich's Text.render resolves a style string through
    `console.get_style(style, default=Style.null())`. An unparseable one
    therefore renders PLAIN and raises nothing -- which is why
    textual-light lost six roles and textual-ansi lost twenty-five with
    nobody noticing. A test that merely draws a transcript sees a
    perfectly ordinary line and passes; only asking Rich to parse the
    string can tell.

    Batch 41's X1 lesson at one remove. There the unmeasurable thing was
    `dim`, an attribute no contrast floor can see; here it is a colour
    name Rich silently drops on the floor.
    """
    for role, style in role_styles(theme).items():
        assert isinstance(style, str), (
            f"{theme.name}: {role!r} is {style!r}, not a style string")
        try:
            Style.parse(style)
        except Exception as exc:  # noqa: BLE001 -- Rich raises several
            raise AssertionError(
                f"{theme.name}: {role!r} is {style!r}, which Rich cannot "
                f"parse ({exc}). It would render with no style at all "
                f"and say nothing about it.") from None


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_the_minimal_legal_theme_is_covered(dark):
    """Seven of the eight slots blank -- everything Theme lets you omit.

    Pins the fallback against Textual's RULES rather than against its
    current data. The two themes that crash today happen to be broken
    now; a release that fills textual-dark's background in would quietly
    delete the coverage, and this is what stays behind when it does.
    """
    theme = Theme(name="minimal", primary="#ff0000", dark=dark)
    styles = role_styles(theme)
    assert set(styles) == EXPECTED_ROLE_KEYS
    for role, style in styles.items():
        Style.parse(style)
        assert "None" not in style, (
            f"{role!r} is {style!r} -- an unfilled slot reached the style "
            f"string as the literal text 'None', which is exactly what "
            f"textual-light did")


def test_an_ansi_theme_speaks_richs_vocabulary():
    """textual-ansi's slots are `ansi_blue`, `ansi_default` and the rest
    of the terminal's own sixteen. Textual's Color.parse knows the
    prefix and Rich's Style.parse does not; the names underneath it are
    the same list, so the translation is exact rather than approximate.

    Asserted on the ABSENCE of the prefix rather than on particular
    colours, because which slot holds which ANSI colour is Textual's
    decision and may move.
    """
    styles = role_styles(BUILTIN_THEMES["textual-ansi"])
    leaked = {r: s for r, s in styles.items() if "ansi_" in s}
    assert leaked == {}, (
        f"{leaked} carry Textual's prefix into a Rich style string, "
        f"where it parses as nothing and renders as nothing")
    assert styles["user"] == "bold blue"


def test_a_theme_with_no_rgb_background_gets_no_diff_band():
    """A diff row is the one place the transcript sets a BACKGROUND, and
    on an ANSI theme there is no RGB to blend one out of -- the terminal
    owns those sixteen colours and the harness cannot know what they
    look like.

    So the row takes the severity colour as its FOREGROUND, which is
    what git, diff and patch all do in a sixteen-colour terminal. The
    alternative was a solid band, and that is the slab batch 41 turned
    down for the themes that CAN be measured; picking it here for the
    one theme that cannot would be backwards.
    """
    styles = role_styles(BUILTIN_THEMES["textual-ansi"])
    assert styles["diff_add"] == "green"
    assert styles["diff_del"] == "red"
    for role in ("diff_add", "diff_del"):
        assert " on " not in styles[role], (
            f"{role} is {styles[role]!r} -- _tint answered None and the "
            f"f-string interpolated it anyway")


@pytest.mark.parametrize("theme", ALL_THEMES, ids=lambda t: t.name)
def test_no_shipped_theme_needs_the_fallback(theme):
    """The promise that keeps batch 64's blast radius at zero.

    `_palette` fills a blank slot from Textual's own
    to_color_system().generate(), and that derivation is LOSSY: the base
    shade comes back as `color.lighten(0).hex`, an HSL round trip, which
    moves #d9a441 to #D8A441 and differs from the raw slot on 41 values
    across these fourteen. Since none of them leaves a slot blank, none
    of them can ever reach it -- and a fifteenth that did would be told
    here rather than by a hex quietly shifting under the contrast floors
    below.
    """
    blank = [slot for slot in themes._SLOTS if not getattr(theme, slot)]
    assert blank == [], (
        f"{theme.name} leaves {blank} unset, so its colours would come "
        f"from Textual's lossy derivation rather than from this file")


def test_the_fallback_memo_cannot_serve_a_stale_palette():
    """Why `_RESOLVED` is keyed on the slot VALUES and not on the name.

    Textual's Theme is a plain mutable dataclass and App.register_theme
    overwrites by name, so a name key would hand a re-registered theme
    the colours of the one it replaced -- a cache that is right until
    somebody changes a theme and then wrong for the rest of the session,
    which is the worst shape a cache can take.

    Both of these need the fallback (seven slots blank), so both go
    through the memo rather than the fast path.
    """
    red = Theme(name="reused", primary="#ff0000")
    green = Theme(name="reused", primary="#00ff00")
    assert role_styles(red)["user"] != role_styles(green)["user"], (
        "two themes sharing a name got one palette -- the memo is keyed "
        "on theme.name, so re-registering a theme serves the old one")


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


# ---- Batch 48: and which of those roles is the CONVERSATION -----------------

#: The entry roles MESSAGE_ROLES does not name, and why each is absent
#: from it rather than from here. `assistant` is deliberately unstyled, so
#: the colour checks skip it; `diff` paints a background and has its own
#: pair of floors below; `assistant_label` is the other direction -- a
#: label drawn beside an entry rather than an entry that can be copied.
#: `pipeline_tool` renders with `tool`'s style by alias, so naming it in
#: MESSAGE_ROLES would make the distinctness check flag two roles that
#: share one style string on purpose.
ENTRY_ROLES = sorted((set(MESSAGE_ROLES) - {"assistant_label"})
                     | {"assistant", "diff", "pipeline_tool"})


def test_every_transcript_role_is_classified_as_conversation_or_harness():
    """Batch 48. `/copy conversation` is an ALLOWLIST, which fails in the
    safe direction -- an unclassified role goes missing from the copy
    rather than leaking a harness line into it. Safe is not the same as
    correct, and a role nobody classified is a role whose absence nobody
    decided, so it is caught here rather than by someone noticing their
    tool calls are gone.

    Held against MESSAGE_ROLES because that list is already the
    hand-maintained inventory of what a transcript line can be, and one
    inventory that two checks read is the whole point -- a second list
    would drift from the first exactly the way this project keeps
    recording.
    """
    classified = CONVERSATION_ROLES | META_ROLES
    unclassified = [r for r in ENTRY_ROLES if r not in classified]
    assert not unclassified, (
        f"{unclassified} can appear in the transcript and are in neither "
        f"CONVERSATION_ROLES nor META_ROLES, so /copy conversation drops "
        f"them without anyone having decided to. Classify them in "
        f"tui/widgets.py.")

    invented = sorted(classified - set(ENTRY_ROLES))
    assert not invented, (
        f"{invented} are classified but cannot reach a transcript entry. "
        f"Either the role went away and the classification should follow, "
        f"or MESSAGE_ROLES above is missing it.")


def test_the_two_role_sets_do_not_overlap():
    """The guard against 'fix' by widening: adding a role to both sets
    makes the check above pass while /copy conversation quietly carries
    the harness line the target exists to leave out."""
    both = sorted(CONVERSATION_ROLES & META_ROLES)
    assert not both, (
        f"{both} are both conversation and harness. A role means one "
        f"thing; if this one genuinely means two, the producer is what "
        f"needs splitting -- see the `tool_error` line in tui/app.py.")



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


# ---- Batch 64: the palette, the mount, and the screen -----------------------
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_palette_offers_exactly_the_themes_this_file_covers():
    """The guard that keeps SELECTABLE_THEMES honest.

    Everything above is parametrised over a list built from
    BUILTIN_THEMES and ALL_THEMES. If Textual ever grows a third source
    of themes -- or register_all stops reaching one of ours -- the
    coverage would shrink in silence, which is precisely the shape of
    the gap batch 64 was reported through: twenty-six themes selectable,
    fourteen ever tested, and the difference invisible until someone
    picked one.
    """
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pump(pilot, 2)
        offered = set(app.available_themes)

    covered = {theme.name for theme in SELECTABLE_THEMES}
    assert offered == covered, (
        f"the palette offers {sorted(offered - covered)} that no check "
        f"in this file has seen, and this file covers "
        f"{sorted(covered - offered)} that cannot be selected")


@pytest.mark.parametrize(
    "name", ["textual-dark", "textual-light", "textual-ansi"])
@pytest.mark.asyncio
async def test_a_remembered_built_in_theme_still_mounts(name):
    """THE REPORTED BUG, and the half of it that made it urgent.

    watch_theme remembers every theme change whatever route made it, so
    a palette selection is written to the preference store before the
    crash it causes. _startup_theme then restores it, on_mount writes the
    session banner, and Transcript._style asks for colours that cannot be
    resolved -- so the harness died at startup, every startup. The store
    is user-tier, at ~/.config/venastine, so reinstalling does not clear
    it: hand-editing the JSON was the only way out.

    `isolate_ui_preferences` is autouse, so this writes to a file per
    test rather than to the developer's own.
    """
    from tui import preferences
    from tui.app import VenastineApp

    assert preferences.remember_theme(name, None)
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 30)) as pilot:
        await pump(pilot, 2)
        assert app.theme == name, (
            f"mounted on {app.theme!r} rather than the remembered "
            f"{name!r}")
        app.screen._compositor.render_strips()
        await pilot.pause()


@pytest.mark.asyncio
async def test_every_selectable_theme_renders_a_transcript():
    """The whole path, end to end: styles_for -> Rich -> the compositor.

    Deliberately kept BESIDE the parse check rather than instead of it.
    This would have caught textual-dark, which raised, and neither of the
    other two, which rendered plain and said nothing -- Rich resolves an
    unparseable style with `default=Style.null()`. A green run here is
    not evidence that a theme has any colour in it.

    One app, twenty-six switches: mounting twenty-six apps would be the
    same assertion at twenty-six times the cost, and the thing being
    exercised is the switch.
    """
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test(size=(100, 30)) as pilot:
        await pump(pilot, 2)
        transcript = app.query_one("#transcript")
        for theme in SELECTABLE_THEMES:
            app.theme = theme.name
            await pilot.pause()
            for role in sorted(EXPECTED_ROLE_KEYS):
                transcript.write_role(role, f"a {role} line")
            await pilot.pause()
            app.screen._compositor.render_strips()


@pytest.mark.asyncio
async def test_an_unstylable_theme_does_not_take_the_app_down(
        monkeypatch, caplog):
    """Section 27's rule, applied to the one path that had escaped it.

    _palette makes textual-dark's particular fault impossible. This makes
    the CLASS of it non-fatal: styles_for is on the path of every
    transcript line including the session banner, so anything raising
    there is a harness that will not start, behind a preference file the
    installer cannot reach.

    And it WARNS, which is the other half. Silence is what let
    textual-light and textual-ansi render unstyled for as long as they
    did, so a contained failure that reported nothing would be the same
    defect wearing a better exception story.

    Once per theme, though. styles_for runs per drawn line, and the
    latch is the opposite of preferences._remember's deliberate
    non-latching for the reason that module gives: a warning fires there
    on a human's own action, and here on every line.
    """
    import logging

    monkeypatch.setattr(themes, "_UNSTYLABLE", set())
    broken = Theme(name="unstylable", primary="not-a-colour")

    class _App:
        theme = "unstylable"

        def get_theme(self, name):
            return broken

    with caplog.at_level(logging.WARNING, logger="tui.themes"):
        assert themes.styles_for(_App()) == {}, (
            "an unresolvable theme has to degrade to unstyled, the way a widget "
            "with no running app already does -- not reach the message pump"
        )
        assert themes.styles_for(_App()) == {}

    warnings = [r for r in caplog.records
               if "unstylable" in r.getMessage()]
    assert len(warnings) == 1, (
        f"{len(warnings)} warnings for one theme -- styles_for is called "
        f"once per rendered line, so this has to be latched")
