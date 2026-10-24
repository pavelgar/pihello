"""Terminal colors: parsing, SGR codes and color-depth downgrading.

A :class:`Color` is either the terminal *default* color, an xterm-256 palette
index (``number``) or a 24-bit ``rgb`` triplet. :meth:`Color.codes` returns the
SGR parameters for a given :class:`ColorDepth`, downgrading as needed:

* truecolor ``rgb`` → nearest xterm-256 entry (16-255) → one of 0-15 (nearest
  for greys and dark colors; hue-preserving for chromatic colors);
* indices 0-15 always use the classic ``30-37``/``90-97`` (``40-47``/``100-107``)
  codes, so they follow the user's terminal theme;
* underline colors (SGR 58) have no 16-color form and are dropped there;
* :attr:`ColorDepth.NONE` produces no codes at all.

Distances use the "redmean" approximation of perceived color difference.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from .color_tables import COLOR_NAMES, PALETTE

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "DEFAULT",
    "Color",
    "ColorDepth",
    "ColorParseError",
    "color_distance",
    "lookup_name",
    "nearest_16",
    "nearest_256",
    "parse_color",
]


class ColorDepth(enum.IntEnum):
    """How many colors the output terminal supports."""

    NONE = 0
    """No escape sequences at all."""
    ANSI16 = 1
    """The 16 classic colors (SGR 30-37, 90-97, 40-47, 100-107)."""
    ANSI256 = 2
    """The xterm-256 palette (SGR 38;5;N)."""
    TRUECOLOR = 3
    """24-bit color (SGR 38;2;R;G;B)."""


class ColorParseError(ValueError):
    """A color specification could not be parsed.

    ``hint`` carries a suggestion for the user (e.g. a did-you-mean).
    """

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# Role → (classic base for 0-7, classic base for 8-15, extended code, default code)
_FG = (30, 90, "38", "39")
_BG = (40, 100, "48", "49")
_UL = (0, 0, "58", "59")


class Color:
    """A terminal color: default, an xterm-256 index, or an RGB triplet.

    Use :func:`parse_color` to build one from text. Instances are immutable,
    hashable and compare by value.
    """

    __slots__ = ("name", "number", "rgb")

    number: int | None
    rgb: tuple[int, int, int] | None
    name: str

    def __init__(
        self,
        number: int | None = None,
        rgb: tuple[int, int, int] | None = None,
        name: str = "",
    ) -> None:
        if number is not None and rgb is not None:
            raise ValueError("a Color has either a palette number or an rgb triplet")
        if number is not None and not 0 <= number <= 255:
            raise ValueError(f"palette number out of range: {number}")
        if rgb is not None and not all(0 <= c <= 255 for c in rgb):
            raise ValueError(f"rgb component out of range: {rgb}")
        object.__setattr__(self, "number", number)
        object.__setattr__(self, "rgb", rgb)
        object.__setattr__(self, "name", name)

    def __setattr__(self, key: str, value: object) -> None:
        raise AttributeError("Color is immutable")

    @property
    def is_default(self) -> bool:
        """Whether this is the terminal's default color."""
        return self.number is None and self.rgb is None

    @property
    def triplet(self) -> tuple[int, int, int] | None:
        """The RGB value of this color (palette colors use xterm defaults)."""
        if self.rgb is not None:
            return self.rgb
        if self.number is not None:
            return PALETTE[self.number]
        return None

    def downgrade(self, depth: ColorDepth) -> Color:
        """Return the closest color representable at ``depth``.

        Default colors and colors that already fit are returned unchanged.
        """
        if depth >= ColorDepth.TRUECOLOR or self.is_default:
            return self
        if self.rgb is not None:
            if depth == ColorDepth.ANSI256:
                return Color(nearest_256(self.rgb))
            return Color(nearest_16(self.rgb))
        number = self.number
        if number is not None and depth <= ColorDepth.ANSI16 and number >= 16:
            return Color(nearest_16(PALETTE[number]))
        return self

    def codes(self, depth: ColorDepth, role: str = "fg") -> tuple[str, ...]:
        """SGR parameters selecting this color at ``depth``.

        ``role`` is ``"fg"`` (foreground), ``"bg"`` (background) or ``"ul"``
        (underline color, SGR 58 — dropped at 16 colors). Returns ``()`` when
        nothing should be emitted (``ColorDepth.NONE``).
        """
        if role == "fg":
            table = _FG
        elif role == "bg":
            table = _BG
        elif role == "ul":
            table = _UL
        else:
            raise ValueError(f"unknown color role {role!r}")
        if depth <= ColorDepth.NONE:
            return ()
        if role == "ul" and depth == ColorDepth.ANSI16:
            return ()
        if self.is_default:
            return (table[3],)
        color = self.downgrade(depth)
        if color.rgb is not None:
            red, green, blue = color.rgb
            return (table[2], "2", str(red), str(green), str(blue))
        number = color.number or 0
        if number < 16 and role != "ul":
            return (str(table[0] + number) if number < 8 else str(table[1] + number - 8),)
        return (table[2], "5", str(number))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Color):
            return NotImplemented
        return self.number == other.number and self.rgb == other.rgb

    def __hash__(self) -> int:
        return hash((self.number, self.rgb))

    def __repr__(self) -> str:
        if self.rgb is not None:
            return "Color(rgb=({}, {}, {}))".format(*self.rgb)
        if self.number is not None:
            return f"Color({self.number})"
        return "Color(default)"

    def __str__(self) -> str:
        if self.name:
            return self.name
        if self.rgb is not None:
            return "#{:02x}{:02x}{:02x}".format(*self.rgb)
        if self.number is not None:
            return f"color({self.number})"
        return "default"


DEFAULT = Color(name="default")
"""The terminal's default color."""


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    """Approximate perceptual distance between two RGB colors ("redmean")."""
    rmean = (a[0] + b[0]) // 2
    dr = a[0] - b[0]
    dg = a[1] - b[1]
    db = a[2] - b[2]
    return (((512 + rmean) * dr * dr) >> 8) + 4 * dg * dg + (((767 - rmean) * db * db) >> 8)


_NEAREST_256: dict[tuple[int, int, int], int] = {}
_NEAREST_16: dict[tuple[int, int, int], int] = {}


def _nearest(rgb: tuple[int, int, int], start: int, stop: int) -> int:
    best = start
    best_distance = color_distance(rgb, PALETTE[start])
    for index in range(start + 1, stop):
        distance = color_distance(rgb, PALETTE[index])
        if distance < best_distance:
            best, best_distance = index, distance
    return best


def nearest_256(rgb: tuple[int, int, int]) -> int:
    """Index (16-255) of the xterm-256 color closest to ``rgb``.

    The 16 base colors are excluded because terminals theme them freely.
    """
    index = _NEAREST_256.get(rgb)
    if index is None:
        index = _NEAREST_256[rgb] = _nearest(rgb, 16, 256)
    return index


# Hue sector (0 = red, 1 = yellow, ... 5 = magenta) → classic color index.
_HUE_TO_16 = (1, 3, 2, 6, 4, 5)
_CHROMA_THRESHOLD = 48
_DARK_THRESHOLD = 80
_BRIGHT_THRESHOLD = 192


def _hue_sector(rgb: tuple[int, int, int]) -> int:
    red, green, blue = rgb
    high = max(rgb)
    chroma = high - min(rgb)
    if high == red:
        hue = ((green - blue) / chroma) % 6
    elif high == green:
        hue = (blue - red) / chroma + 2
    else:
        hue = (red - green) / chroma + 4
    return int(hue + 0.5) % 6


def nearest_16(rgb: tuple[int, int, int]) -> int:
    """Index (0-15) of the classic terminal color that best represents ``rgb``.

    Greys and very dark colors use the nearest palette entry (redmean). Clearly
    chromatic colors keep their hue: the hue picks red/yellow/green/cyan/blue/
    magenta and the brightness picks the normal or bright variant, so e.g.
    ``steelblue`` stays blue and pastel ``lightgreen`` stays green instead of
    collapsing into grey or yellow as a pure RGB distance would.
    """
    index = _NEAREST_16.get(rgb)
    if index is None:
        high = max(rgb)
        if high - min(rgb) < _CHROMA_THRESHOLD or high < _DARK_THRESHOLD:
            index = _nearest(rgb, 0, 16)
        else:
            index = _HUE_TO_16[_hue_sector(rgb)] + (8 if high >= _BRIGHT_THRESHOLD else 0)
        _NEAREST_16[rgb] = index
    return index


def lookup_name(name: str) -> int | None:
    """Palette index of a color name, or ``None`` if unknown.

    Case-insensitive; ``_``, ``-`` and spaces are ignored and ``gray`` is
    accepted for ``grey`` (and vice versa).
    """
    key = name.lower()
    number = COLOR_NAMES.get(key)
    if number is not None:
        return number
    key = key.replace("_", "").replace("-", "").replace(" ", "")
    number = COLOR_NAMES.get(key)
    if number is None and "gray" in key:
        number = COLOR_NAMES.get(key.replace("gray", "grey"))
    if number is None and "grey" in key:
        number = COLOR_NAMES.get(key.replace("grey", "gray"))
    return number


def suggest(word: str, candidates: Iterable[str]) -> list[str]:
    """Up to three close matches for ``word`` among ``candidates``."""
    import difflib  # noqa: PLC0415 - only needed on the error path

    return difflib.get_close_matches(word, list(candidates), n=3, cutoff=0.6)


def did_you_mean(word: str, candidates: Iterable[str], prefix: str = "") -> str | None:
    """A ``"did you mean 'x'?"`` hint, or ``None`` when nothing is close."""
    matches = suggest(word, candidates)
    if not matches:
        return None
    return "did you mean " + alternatives([prefix + match for match in matches]) + "?"


def alternatives(options: list[str]) -> str:
    quoted = [f"'{option}'" for option in options]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


_HEX_DIGITS = frozenset("0123456789abcdef")
_COLOR_FORMS = "a color name, color(N), #rrggbb, #rgb, rgb(r, g, b) or default"


def _parse_component(text: str, what: str, original: str, maximum: int = 255) -> int:
    text = text.strip()
    if not text.isdigit() or not text.isascii():
        raise ColorParseError(
            f"invalid {what} {text!r} in color {original!r}",
            hint=f"use a whole number between 0 and {maximum}",
        )
    value = int(text) if len(text) <= 4 else maximum + 1
    if value > maximum:
        raise ColorParseError(
            f"{what} {text} is out of range in color {original!r}",
            hint=f"use a whole number between 0 and {maximum}",
        )
    return value


def parse_color(text: str, *, hints: bool = True) -> Color:
    """Parse a color specification.

    Accepts an xterm color name (``steelblue``, ``grey37``), ``color(N)`` with
    0 ≤ N ≤ 255, ``#rrggbb``, ``#rgb``, ``rgb(r, g, b)`` or ``default``.

    Raises :class:`ColorParseError` (with a did-you-mean hint unless ``hints``
    is false, which skips the costly search) on failure.
    """
    original = text
    spec = text.strip().lower()
    if not spec:
        raise ColorParseError("empty color", hint=f"use {_COLOR_FORMS}")
    if spec == "default":
        return DEFAULT
    if spec[0] == "#":
        digits = spec[1:]
        if len(digits) not in (3, 6) or not _HEX_DIGITS.issuperset(digits):
            raise ColorParseError(
                f"invalid hex color {original!r}", hint="use #rgb or #rrggbb (hex digits 0-9, a-f)"
            )
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        rgb = (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
        return Color(rgb=rgb, name=spec)
    if spec.startswith(("color(", "rgb(")):
        if not spec.endswith(")"):
            raise ColorParseError(f"missing ')' in color {original!r}")
        inner = spec[spec.index("(") + 1 : -1]
        if spec[0] == "c":
            return Color(_parse_component(inner, "color number", original), name=spec)
        parts = inner.split(",")
        if len(parts) != 3:
            raise ColorParseError(
                f"rgb() needs three components, got {len(parts)} in {original!r}",
                hint="write rgb(r, g, b) with values 0-255",
            )
        red, green, blue = (_parse_component(p, "rgb component", original) for p in parts)
        return Color(rgb=(red, green, blue), name=spec)
    number = lookup_name(spec)
    if number is not None:
        return Color(number, name=spec)
    if spec.isdigit():
        raise ColorParseError(
            f"unknown color {original!r}", hint=f"use color({spec}) for palette colors"
        )
    hint = did_you_mean(spec.replace("_", "").replace("-", ""), COLOR_NAMES) if hints else None
    raise ColorParseError(
        f"unknown color {original!r}",
        hint=hint or f"use {_COLOR_FORMS}; `pihello colors` lists the names",
    )
