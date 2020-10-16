import re
from colorsys import rgb_to_hls
from enum import IntEnum
from typing import Optional, Tuple
from .color_tables import COLOR_NAMES, EIGHT_BIT_PALETTE

RE_COLOR = re.compile(r"^\#([0-9a-f]{6})$|color\(([0-9]{1,3})\)$|rgb\(([\d\s,]+)\)$")


class ColorParseError(Exception):
    """Raised when a color could not be parsed."""


class Color:
    """
    Definition of a terminal color.

    *Do not use this instance directly.*
    Use Color.parse() instead.
    """

    def __init__(
        self,
        name: str,
        number: Optional[int] = None,
        triplet: Optional[Tuple[int, int, int]] = None,
    ):
        """Raw name of the color - the string from which it was derived from."""
        self.name = name
        """The color number, if a standard color."""
        self.number = number
        """A triplet of color components, if an RGB color."""
        self.triplet = triplet

    @property
    def is_default(self) -> bool:
        """Check if the color is a default color."""
        return self.name == "default" or (self.number is None and self.triplet is None)

    def get_truecolor(self) -> Optional[Tuple[int, int, int]]:
        """Get an equivalent color triplet (R, G, B) for this color."""
        if self.number is not None:
            return EIGHT_BIT_PALETTE[self.number]
        if self.triplet:
            return self.triplet
        return None

    def get_ansi_codes(self, foreground: bool = True, underline: bool = False) -> tuple:
        """Get the ANSI escape codes for this color."""
        if self.is_default:
            return ("39" if foreground else "59" if underline else "49",)

        elif self.number < 16 and not underline:
            fore, back = (30, 40) if self.number < 8 else (82, 92)
            return (str(fore + self.number if foreground else back + self.number),)

        elif self.number is not None:
            return (
                "38" if foreground else "58" if underline else "48",
                "5",
                str(self.number),
            )

        red, green, blue = self.triplet
        return ("38" if foreground else "48", "2", str(red), str(green), str(blue))

    @classmethod
    def default(cls) -> "Color":
        """Get a Color instance representing the default color."""
        return cls(name="default")

    @classmethod
    def from_triplet(cls, triplet: Tuple[int, int, int]) -> "Color":
        """
        Create a RGB color from a triplet of values (R, G, B).

        Returns a new Color object.
        """
        return cls(name=str(triplet), triplet=triplet)

    @classmethod
    def parse(cls, color: str) -> "Color":
        """Parse a color from string. Can take a rgb(r,g,b), color(n), hex code or color name."""

        # Check if default color
        color = color.lower().strip()
        if color == "default":
            return cls(name=color)

        # Check if named color
        color_num = COLOR_NAMES[color]
        if color_num is not None:
            return cls(name=color, number=color_num)

        # Check if hex or rgb color
        color_match = RE_COLOR.match(color)
        if color_match is None:
            raise ColorParseError(f"{color!r} is not a valid color")

        color_hex, color_num, color_rgb = color_match.groups()
        if color_hex:
            triplet = (
                int(color_hex[0:2], 16),
                int(color_hex[2:4], 16),
                int(color_hex[4:6], 16),
            )
            return cls(name=color, triplet=triplet)

        if color_num:
            color_num = int(color_num)
            if color_num > 255:
                raise ColorParseError(f"Color number must be <= 255 in {color!r}")
            return cls(name=color, number=color_num)

        components = color_rgb.split(",")
        if len(components) != 3:
            raise ColorParseError(f"Expected three components in {color!r}")
        red, green, blue = components
        triplet = (int(red), int(green), int(blue))
        if not all(component <= 255 for component in triplet):
            raise ColorParseError(f"Color components must be <= 255 in {color!r}")
        return cls(name=color, triplet=triplet)
