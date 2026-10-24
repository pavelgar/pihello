"""Color parsing, SGR codes and color-depth downgrading."""

from __future__ import annotations

import pytest

from pihello.template.color import (
    DEFAULT,
    Color,
    ColorDepth,
    ColorParseError,
    alternatives,
    color_distance,
    did_you_mean,
    lookup_name,
    nearest_16,
    nearest_256,
    parse_color,
)
from pihello.template.color_tables import COLOR_NAMES, EIGHT_BIT_PALETTE, PALETTE

T, C256, C16, NONE = (
    ColorDepth.TRUECOLOR,
    ColorDepth.ANSI256,
    ColorDepth.ANSI16,
    ColorDepth.NONE,
)


# --------------------------------------------------------------------------- #
# Tables                                                                       #
# --------------------------------------------------------------------------- #
def test_palette_is_standard_xterm() -> None:
    assert len(PALETTE) == 256
    assert EIGHT_BIT_PALETTE is PALETTE
    levels = (0, 95, 135, 175, 215, 255)
    for index in range(216):
        assert PALETTE[16 + index] == (
            levels[index // 36],
            levels[index // 6 % 6],
            levels[index % 6],
        )
    for index in range(24):
        assert PALETTE[232 + index] == (8 + 10 * index,) * 3
    assert PALETTE[9] == (255, 0, 0)


def test_names_keep_pihello_1x_mapping() -> None:
    # A few 1.x names and their exact indices must never change.
    expected = {"red": 9, "white": 15, "green": 2, "cyan2": 50, "steelblue": 67, "grey37": 59}
    for name, number in expected.items():
        assert COLOR_NAMES[name] == number
    assert all(0 <= n <= 255 for n in COLOR_NAMES.values())


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "number", "rgb"),
    [
        ("steelblue", 67, None),
        ("SteelBlue", 67, None),
        ("  grey37 ", 59, None),
        ("gray37", 59, None),
        ("lightslategrey", 103, None),
        ("lightslategray", 103, None),
        ("darkslategrey2", 87, None),
        ("dark_slate_gray2", 87, None),
        ("deep-sky-blue1", 39, None),
        ("magenta", 5, None),
        ("brightcyan", 14, None),
        ("bright_red", 9, None),
        ("color(0)", 0, None),
        ("color(255)", 255, None),
        ("color( 45 )", 45, None),
        ("#ff8800", None, (255, 136, 0)),
        ("#FF8800", None, (255, 136, 0)),
        ("#f80", None, (255, 136, 0)),
        ("rgb(1,2,3)", None, (1, 2, 3)),
        ("rgb( 10 , 20 , 255 )", None, (10, 20, 255)),
    ],
)
def test_parse_color_forms(text: str, number: int | None, rgb: tuple[int, int, int] | None) -> None:
    color = parse_color(text)
    assert color.number == number
    assert color.rgb == rgb


def test_parse_default() -> None:
    assert parse_color("default") is DEFAULT
    assert parse_color("DEFAULT").is_default
    assert DEFAULT.triplet is None


@pytest.mark.parametrize(
    ("text", "message", "hint"),
    [
        ("steelblu", "unknown color 'steelblu'", "did you mean 'steelblue'"),
        ("gren", "unknown color", "'green'"),
        ("zzzzzz", "unknown color", "pihello colors"),
        ("45", "unknown color '45'", "color(45)"),
        ("", "empty color", "#rrggbb"),
        ("#12345", "invalid hex color", "#rgb or #rrggbb"),
        ("#ggg", "invalid hex color", None),
        ("#", "invalid hex color", None),
        ("color(256)", "out of range", "0 and 255"),
        ("color(99999999)", "out of range", None),
        ("color(-1)", "invalid color number", None),
        ("color(x)", "invalid color number", None),
        ("color(1", "missing ')'", None),
        ("rgb(1,2)", "three components, got 2", "rgb(r, g, b)"),
        ("rgb(1,2,3,4)", "three components, got 4", None),
        ("rgb(1,2,300)", "out of range", None),
        ("rgb(1,,3)", "invalid rgb component", None),
        ("rgb(1.5,2,3)", "invalid rgb component", None),
        ("rgb(\u0661,2,3)", "invalid rgb component", None),  # non-ASCII digit
    ],
)
def test_parse_color_errors(text: str, message: str, hint: str | None) -> None:
    with pytest.raises(ColorParseError) as info:
        parse_color(text)
    assert message in info.value.message
    assert isinstance(info.value, ValueError)
    if hint is not None:
        assert info.value.hint is not None
        assert hint in info.value.hint


def test_lookup_name() -> None:
    assert lookup_name("Grey50") == 244
    assert lookup_name("gray50") == 244
    assert lookup_name("nope") is None
    assert lookup_name("grayish") is None


def test_suggestions_grammar() -> None:
    assert alternatives(["a"]) == "'a'"
    assert alternatives(["a", "b"]) == "'a' or 'b'"
    assert alternatives(["a", "b", "c"]) == "'a', 'b' or 'c'"
    assert did_you_mean("bould", ["bold", "dim"]) == "did you mean 'bold'?"
    assert did_you_mean("godo", ["good"], prefix="@") == "did you mean '@good'?"
    assert did_you_mean("xyz", ["bold"]) is None


# --------------------------------------------------------------------------- #
# Color objects                                                                #
# --------------------------------------------------------------------------- #
def test_color_value_semantics() -> None:
    assert parse_color("red") == Color(9)
    assert parse_color("#ff0000") == Color(rgb=(255, 0, 0))
    assert parse_color("#ff0000") != Color(9)
    assert hash(parse_color("steelblue")) == hash(Color(67))
    assert Color(9) != "red"
    assert repr(Color(9)) == "Color(9)"
    assert repr(Color(rgb=(1, 2, 3))) == "Color(rgb=(1, 2, 3))"
    assert repr(DEFAULT) == "Color(default)"
    assert str(parse_color("steelblue")) == "steelblue"
    assert str(Color(45)) == "color(45)"
    assert str(Color(rgb=(255, 136, 0))) == "#ff8800"
    assert str(Color()) == "default"
    assert Color(9).triplet == (255, 0, 0)
    assert Color(rgb=(1, 2, 3)).triplet == (1, 2, 3)


def test_color_is_immutable_and_validated() -> None:
    color = Color(9)
    with pytest.raises(AttributeError):
        color.number = 3
    with pytest.raises(ValueError, match="either"):
        Color(1, rgb=(1, 2, 3))
    with pytest.raises(ValueError, match="out of range"):
        Color(256)
    with pytest.raises(ValueError, match="out of range"):
        Color(rgb=(0, 0, 256))


# --------------------------------------------------------------------------- #
# SGR codes                                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("spec", "role", "depth", "codes"),
    [
        # classic colors 0-15 use 30-37 / 90-97 / 40-47 / 100-107 at every depth
        ("black", "fg", T, ("30",)),
        ("silver", "fg", C16, ("37",)),
        ("grey", "fg", C256, ("90",)),
        ("red", "fg", T, ("91",)),
        ("white", "fg", T, ("97",)),
        ("maroon", "bg", T, ("41",)),
        ("aqua", "bg", C16, ("106",)),
        # extended palette
        ("steelblue", "fg", T, ("38", "5", "67")),
        ("steelblue", "fg", C256, ("38", "5", "67")),
        ("steelblue", "bg", C256, ("48", "5", "67")),
        ("steelblue", "fg", C16, ("34",)),
        # truecolor
        ("#ff8800", "fg", T, ("38", "2", "255", "136", "0")),
        ("#ff8800", "bg", T, ("48", "2", "255", "136", "0")),
        ("#ff8800", "fg", C256, ("38", "5", "208")),
        ("#ff8800", "fg", C16, ("93",)),
        # underline colors: always 58 (never the classic codes); dropped at 16 colors
        ("red", "ul", T, ("58", "5", "9")),
        ("#ff8800", "ul", T, ("58", "2", "255", "136", "0")),
        ("#ff8800", "ul", C256, ("58", "5", "208")),
        ("#ff8800", "ul", C16, ()),
        ("red", "ul", C16, ()),
        # defaults
        ("default", "fg", T, ("39",)),
        ("default", "bg", C16, ("49",)),
        ("default", "ul", T, ("59",)),
        # NONE emits nothing
        ("#ff8800", "fg", NONE, ()),
        ("red", "bg", NONE, ()),
        ("default", "fg", NONE, ()),
    ],
)
def test_codes(spec: str, role: str, depth: ColorDepth, codes: tuple[str, ...]) -> None:
    assert parse_color(spec).codes(depth, role) == codes


def test_codes_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="role"):
        Color(1).codes(T, "xx")


# --------------------------------------------------------------------------- #
# Downgrading                                                                  #
# --------------------------------------------------------------------------- #
def test_redmean_distance() -> None:
    assert color_distance((1, 2, 3), (1, 2, 3)) == 0
    assert color_distance((0, 0, 0), (255, 255, 255)) > color_distance((0, 0, 0), (128, 128, 128))
    # green differences weigh the most
    assert color_distance((0, 0, 0), (0, 50, 0)) > color_distance((0, 0, 0), (50, 0, 0))


@pytest.mark.parametrize(
    ("rgb", "index"),
    [
        ((255, 135, 0), 208),  # exact cube entries map to themselves
        ((95, 135, 175), 67),
        ((0, 0, 0), 16),  # 0-15 are never chosen
        ((255, 255, 255), 231),
        ((255, 0, 0), 196),
        ((128, 128, 128), 244),  # exact grey ramp entry
        ((138, 138, 138), 245),
        ((8, 8, 8), 232),
        ((255, 136, 0), 208),
        ((250, 2, 3), 196),
        ((90, 92, 94), 59),  # near grey37
    ],
)
def test_nearest_256(rgb: tuple[int, int, int], index: int) -> None:
    assert nearest_256(rgb) == index
    assert 16 <= nearest_256(rgb) <= 255


def test_nearest_256_is_cached_and_optimal() -> None:
    for rgb in ((12, 200, 77), (240, 10, 130), (33, 33, 40)):
        best = min(range(16, 256), key=lambda i: color_distance(rgb, PALETTE[i]))
        assert color_distance(rgb, PALETTE[nearest_256(rgb)]) == color_distance(rgb, PALETTE[best])
        assert nearest_256(rgb) == nearest_256(rgb)


@pytest.mark.parametrize(
    ("rgb", "index"),
    [
        ((0, 0, 0), 0),
        ((255, 255, 255), 15),
        ((128, 128, 128), 8),
        ((192, 192, 192), 7),
        ((95, 95, 95), 8),  # grey37
        ((20, 20, 20), 0),
        ((255, 0, 0), 9),
        ((128, 0, 0), 1),
        ((0, 135, 0), 2),  # green4
        ((135, 255, 135), 10),  # lightgreen stays green (not yellow)
        ((95, 175, 95), 2),  # muted green stays green (not grey/teal)
        ((95, 135, 175), 4),  # steelblue stays blue (not grey)
        ((0, 175, 135), 6),  # darkcyan
        ((0, 255, 215), 14),  # cyan2
        ((255, 136, 0), 11),  # orange → bright yellow
        ((175, 0, 255), 13),  # purple → bright magenta
        ((0, 0, 96), 4),
        ((60, 0, 0), 0),  # very dark → nearest (black)
        ((255, 215, 215), 15),  # pale → white
    ],
)
def test_nearest_16(rgb: tuple[int, int, int], index: int) -> None:
    assert nearest_16(rgb) == index


def test_downgrade() -> None:
    orange = parse_color("#ff8800")
    assert orange.downgrade(T) is orange
    assert orange.downgrade(C256) == Color(208)
    assert orange.downgrade(C16) == Color(11)
    assert orange.downgrade(NONE) == Color(11)
    steelblue = Color(67)
    assert steelblue.downgrade(C256) is steelblue
    assert steelblue.downgrade(C16) == Color(4)
    red = Color(9)
    assert red.downgrade(C16) is red
    assert DEFAULT.downgrade(C16) is DEFAULT
