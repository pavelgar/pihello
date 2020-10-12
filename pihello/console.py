from typing import Any
from .style import Style


class Console:
    """Definition of the console being used."""

    def __init__(
        self,
        width: int = 0,
        height: int = 0,
        tab_size: int = 4,
    ):
        self.width = width
        self.height = height
        self.tab_size = tab_size

    @property
    def size(self):
        """Get the size of the console."""
        return (self.width, self.height)

    def print(self, *objects: Any, sep=" ", end="\n", style=None):
        """Prints the given styled strings and other positional arguments to stdout."""
        if not objects:
            print()

        strs = [self.style(obj) for obj in objects]
        s = sep.join(strs) + end
        print(s)

    def style(self, obj) -> str:
        """Wrapper function to determine whether an object has styling and process it accordingly."""
        if not isinstance(obj, str):
            return str(obj)
        return self.parse(obj)

    def parse(self, s: str) -> str:
        """Parses a string containing styles and returns the styled string."""
        ptr = 0
        end = len(s)
        buffer = ""
        while ptr < end:
            start = ptr
            ptr = s.find("[", start)  # Find next style opening bracket
            if ptr < 0:  # No more styles to parse
                buffer += s[start:]
                break

            if ptr > 0:  # Add everything before the style bracket
                buffer += s[start:ptr]
                if s[ptr - 1] == "\\":  # Check for an escaped opening bracket
                    ptr += 1
                    continue

            start = ptr  # Set to start of the style string
            ptr = s.find("]", start)  # Find the closing bracket
            ptr += 1
            style = Style(s[start:ptr])
            buffer += style.get_ansi_style()

        buffer += "\x1b[0m"
        return buffer
