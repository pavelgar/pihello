"""Style-tag words → :class:`Style` objects → SGR escape sequences.

A style tag such as ``[bold steelblue on grey15 _red]`` is a list of words.
Each word is an attribute (``bold``, ``italic``, ...), a reset (``normal`` /
``reset``), a foreground color, ``:<color>`` / ``on <color>`` for the
background or ``_<color>`` for the underline color. Words are
case-insensitive; later words override earlier ones for the same slot.

Theme references (``@name``) and expressions are resolved by the template
engine before the words reach this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .color import Color, ColorDepth, ColorParseError, did_you_mean, parse_color
from .color_tables import COLOR_NAMES

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "ATTRIBUTES",
    "EMPTY",
    "RESET",
    "Style",
    "StyleError",
    "build_style",
    "parse_style",
    "split_words",
    "validate_word",
]

RESET = "\x1b[0m"
"""The SGR sequence that resets every attribute and color."""

# word → (bit, SGR code). Order of the table is the order codes are emitted in.
ATTRIBUTES: dict[str, tuple[int, str]] = {
    "bold": (1 << 0, "1"),
    "dim": (1 << 1, "2"),
    "faint": (1 << 1, "2"),
    "italic": (1 << 2, "3"),
    "underline": (1 << 3, "4"),
    "blink": (1 << 4, "5"),
    "fblink": (1 << 5, "6"),
    "reverse": (1 << 6, "7"),
    "hidden": (1 << 7, "8"),
    "strike": (1 << 8, "9"),
    "dunderline": (1 << 9, "21"),
    "overline": (1 << 10, "53"),
}
_ATTRIBUTE_CODES: tuple[tuple[int, str], ...] = tuple(dict.fromkeys(ATTRIBUTES.values()))
_RESET_WORDS = frozenset(("normal", "reset"))
_KEYWORDS = (*ATTRIBUTES, *_RESET_WORDS, "on")


class StyleError(ValueError):
    """A style word is invalid.

    ``index`` is the position of the offending word in the word list given to
    :func:`build_style` (``None`` if unknown); ``hint`` is a suggestion.
    """

    def __init__(self, message: str, *, hint: str | None = None, index: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.index = index


class Style:
    """An immutable text style: attribute bits plus fg/bg/underline colors.

    ``None`` colors mean "terminal default". Instances are hashable and compare
    by value; :meth:`sgr` is memoised per color depth.
    """

    __slots__ = ("_sgr", "attrs", "bg", "fg", "ul")

    attrs: int
    fg: Color | None
    bg: Color | None
    ul: Color | None
    _sgr: dict[int, str]

    def __init__(
        self,
        attrs: int = 0,
        fg: Color | None = None,
        bg: Color | None = None,
        ul: Color | None = None,
    ) -> None:
        set_ = object.__setattr__
        set_(self, "attrs", attrs)
        # Default colors are implied by the leading SGR 0; store them as None.
        set_(self, "fg", None if fg is None or fg.is_default else fg)
        set_(self, "bg", None if bg is None or bg.is_default else bg)
        set_(self, "ul", None if ul is None or ul.is_default else ul)
        set_(self, "_sgr", {})

    def __setattr__(self, key: str, value: object) -> None:
        raise AttributeError("Style is immutable")

    @property
    def is_plain(self) -> bool:
        """Whether this style has no attributes and only default colors."""
        return not self.attrs and self.fg is None and self.bg is None and self.ul is None

    def codes(self, depth: ColorDepth) -> list[str]:
        """SGR parameters for this style at ``depth`` (without the leading ``0``)."""
        if depth <= ColorDepth.NONE:
            return []
        codes = [code for bit, code in _ATTRIBUTE_CODES if self.attrs & bit]
        if self.fg is not None:
            codes.extend(self.fg.codes(depth, "fg"))
        if self.bg is not None:
            codes.extend(self.bg.codes(depth, "bg"))
        if self.ul is not None:
            codes.extend(self.ul.codes(depth, "ul"))
        return codes

    def sgr(self, depth: ColorDepth) -> str:
        """The complete escape sequence selecting this style, e.g. ``ESC[0;1;38;5;45m``.

        Always starts with ``0`` (styles replace each other). Returns ``""``
        for a plain style or ``ColorDepth.NONE``.
        """
        cache = self._sgr
        sequence = cache.get(depth)
        if sequence is None:
            codes = self.codes(depth)
            sequence = cache[depth] = "\x1b[0;" + ";".join(codes) + "m" if codes else ""
        return sequence

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Style):
            return NotImplemented
        return (self.attrs, self.fg, self.bg, self.ul) == (
            other.attrs,
            other.fg,
            other.bg,
            other.ul,
        )

    def __hash__(self) -> int:
        return hash((self.attrs, self.fg, self.bg, self.ul))

    def __repr__(self) -> str:
        return f"Style({self})"

    def __str__(self) -> str:
        words = [
            word for word, (bit, _) in ATTRIBUTES.items() if self.attrs & bit and word != "faint"
        ]
        if self.fg is not None:
            words.append(str(self.fg))
        if self.bg is not None:
            words.append(f":{self.bg}")
        if self.ul is not None:
            words.append(f"_{self.ul}")
        return " ".join(words) or "normal"


EMPTY = Style()
"""The plain style (``[]``)."""

# Parsed word cache: word → (kind, attribute bit, color).
# kind: "a" attribute, "r" reset, "o" the 'on' keyword, "f"/"b"/"u" fg/bg/underline color.
_WORD_CACHE: dict[str, tuple[str, int, Color | None]] = {}
_WORD_CACHE_LIMIT = 1024


def _unknown_word(word: str) -> StyleError:
    lowered = word.lower()
    hint = did_you_mean(lowered, (*_KEYWORDS, *COLOR_NAMES))
    if hint is None:
        hint = (
            "style words are attributes (bold, dim, italic, underline, ...), normal, "
            "a color, :color / on color (background) or _color (underline color); "
            "write \\[ for a literal bracket"
        )
    return StyleError(f"unknown style word {word!r}", hint=hint)


def _parse_word(word: str) -> tuple[str, int, Color | None]:
    cached = _WORD_CACHE.get(word)
    if cached is not None:
        return cached
    lowered = word.lower()
    result: tuple[str, int, Color | None]
    attribute = ATTRIBUTES.get(lowered)
    if attribute is not None:
        result = ("a", attribute[0], None)
    elif lowered in _RESET_WORDS:
        result = ("r", 0, None)
    elif lowered == "on":
        result = ("o", 0, None)
    else:
        kind = "f"
        spec = word
        if word[0] in ":_":
            kind = "b" if word[0] == ":" else "u"
            spec = word[1:]
            if not spec:
                what = "background" if kind == "b" else "underline"
                raise StyleError(f"missing {what} color after {word!r}", hint="e.g. :navy or _red")
        try:
            color = parse_color(spec)
        except ColorParseError as error:
            if kind == "f" and not spec.startswith(("#", "color(", "rgb(")):
                raise _unknown_word(word) from None
            raise StyleError(error.message, hint=error.hint) from None
        result = (kind, 0, None if color.is_default else color)
    if len(_WORD_CACHE) >= _WORD_CACHE_LIMIT:
        _WORD_CACHE.clear()
    _WORD_CACHE[word] = result
    return result


def validate_word(word: str) -> None:
    """Raise :class:`StyleError` if ``word`` is not a valid style word on its own."""
    _parse_word(word)


def build_style(words: Sequence[str], base: Style = EMPTY) -> Style:
    """Apply style ``words`` in order on top of ``base`` and return the result.

    ``normal``/``reset`` clear everything accumulated so far. Raises
    :class:`StyleError` whose ``index`` points at the offending word.
    """
    attrs, fg, bg, ul = base.attrs, base.fg, base.bg, base.ul
    pending_on = -1
    for index, word in enumerate(words):
        try:
            kind, bit, color = _parse_word(word)
        except StyleError as error:
            error.index = index
            raise
        if pending_on >= 0:
            if kind != "f":
                raise StyleError(
                    f"expected a color after 'on', got {word!r}",
                    hint="e.g. 'white on navy'",
                    index=index,
                )
            pending_on = -1
            bg = color
            continue
        if kind == "a":
            attrs |= bit
        elif kind == "f":
            fg = color
        elif kind == "b":
            bg = color
        elif kind == "u":
            ul = color
        elif kind == "o":
            pending_on = index
        else:  # reset
            attrs, fg, bg, ul = 0, None, None, None
    if pending_on >= 0:
        raise StyleError(
            "expected a color after 'on'", hint="e.g. 'white on navy'", index=pending_on
        )
    if attrs == base.attrs and fg is base.fg and bg is base.bg and ul is base.ul:
        return base
    if not attrs and fg is None and bg is None and ul is None:
        return EMPTY
    return Style(attrs, fg, bg, ul)


def split_words(text: str) -> list[str]:
    """Split style text into words at whitespace, keeping ``rgb(1, 2, 3)`` together."""
    if "(" not in text:
        return text.split()
    words: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char.isspace() and depth == 0:
            if current:
                words.append("".join(current))
                current = []
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        current.append(char)
    if current:
        words.append("".join(current))
    return words


def parse_style(text: str) -> Style:
    """Parse a whole style string such as ``"bold #ff8800 on navy"``.

    ``@name`` theme references are not allowed here. Raises :class:`StyleError`.
    """
    words = split_words(text)
    for index, word in enumerate(words):
        if word.startswith("@"):
            raise StyleError(
                f"theme reference {word!r} is not allowed here",
                hint="theme styles cannot reference other theme styles",
                index=index,
            )
    return build_style(words)
