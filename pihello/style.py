from .color import Color

STYLE_TO_CODE = {
    "bold": "1",
    "faint": "2",
    "italic": "3",
    "underline": "4",
    "blink": "5",
    "fblink": "6",
    "strike": "9",
    "normal": "10",
    "dunderline": "21",
}


class Style:
    """
    Definition of a complete style which may include text styling as well as foreground, background and underline color.
    """

    def __init__(self, s: str):
        self.style = s.strip("[ ]").lower()

    def get_ansi_style(self) -> str:
        codes = self.parse_styles()
        fmt = ";".join(codes)
        return f"\x1b[0m\x1b[{fmt}m" if fmt else "\x1b[0m"

    def parse_styles(self) -> tuple:
        """
        Parses the style string and returns a tuple of style codes in correct order.
        """
        if not self.style:
            return ()

        codes = []  # List of control codes
        for s in self.style.split(" "):
            if s in STYLE_TO_CODE:
                codes.append(STYLE_TO_CODE[s])
            elif s.startswith(":"):  # background color
                color = Color.parse(s[1:])
                codes.extend(color.get_ansi_codes(foreground=False))
            elif s.startswith("_"):  # underline color
                color = Color.parse(s[1:])
                codes.extend(color.get_ansi_codes(foreground=False, underline=True))
            else:  # foreground color
                color = Color.parse(s)
                codes.extend(color.get_ansi_codes())

        return tuple(codes)
