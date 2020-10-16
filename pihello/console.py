from .style import Style


class TagParseError(Exception):
    """Raised when a tag could not be parsed."""


class Console:
    """Definition of the console being used."""

    def __init__(self, width=0, height=0, tab_size=4, variables={}):
        self.width = width
        self.height = height
        self.tab_size = tab_size
        self.variables = variables

    @property
    def size(self):
        """Get the size of the console."""
        return (self.width, self.height)

    def print(self, *objects: tuple, sep=" ", end="\n", style=None):
        """Prints the given styled strings and other positional arguments to stdout."""
        if not objects:
            print(sep=sep, end=end)

        strs = [self.style(obj) for obj in objects]
        print(*strs, sep=sep, end=end)

    def style(self, obj) -> str:
        """Wrapper function to determine whether an object has styling and process it accordingly."""
        if isinstance(obj, str):
            return self.parse(obj)
        return str(obj)

    def parse(self, s: str) -> str:
        """Parses a string containing possible styles and variable injections and returns the styled string."""
        ptr = 0
        stop = len(s)
        buffer = ""
        while ptr < stop:
            _next = ""
            if s[ptr] == "\\" and (s[ptr + 1] == "[" or s[ptr + 1] == "{"):
                ptr += 1
                _next = s[ptr]

            elif s[ptr] == "[":  # Beginning of a style
                end = s.find("]", ptr + 1)
                if end < 0:
                    raise TagParseError("Matching ']' could not be found.")

                _next = Style(s[ptr : end + 1]).get_ansi_style()
                ptr = end

            elif s[ptr] == "{":  # Beginning of a variable
                end = s.find("}", ptr + 1)
                if end < 0:
                    raise TagParseError("Matching '}' could not be found.")
                var_name = s[ptr : end + 1].strip("{ }")
                val = self.variables.get(var_name)

                if val is None:
                    raise TagParseError(f"No variable '{var_name}'.")

                # Shorten floats to 1 decimal place
                _next = f"{val:.1f}" if isinstance(val, float) else str(val)
                ptr = end

            else:
                _next = s[ptr]

            buffer += _next
            ptr += 1

        buffer += "\x1b[0m"
        return buffer
