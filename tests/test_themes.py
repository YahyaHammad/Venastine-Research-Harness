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
    "pass", "pass_done", "tool", "tool_error", "warning", "error",
    "success", "HIGH", "MEDIUM", "LOW", "UNVERIFIED", "UNVERIFIED_COVERAGE",
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
