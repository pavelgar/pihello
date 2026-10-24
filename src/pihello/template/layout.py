"""Visible width, tab expansion, wrapping/clipping and ANSI serialisation.

The renderer produces *lines of runs*: each logical line is a list of
``(sgr, text)`` pairs where ``sgr`` is the complete escape sequence for the
run's style (``""`` for plain text). This module lays those lines out for a
terminal of a given width and serialises them so that styles never bleed:
every line that emitted a style ends with ``ESC[0m`` and the next line
re-emits whatever style is active.

Widths follow the terminal convention: escape sequences, control characters,
combining marks and zero-width characters take 0 columns, East Asian Wide and
Fullwidth characters take 2, everything else (including box-drawing and
block characters) takes 1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import re
    from collections.abc import Iterator

__all__ = [
    "Run",
    "ansi_pattern",
    "char_width",
    "clip_text",
    "expand_tabs",
    "layout",
    "pad",
    "serialize",
    "strip_ansi",
    "truncate",
    "visible_width",
]

Run = tuple[str, str]
"""A ``(sgr, text)`` pair: ``sgr`` is a full escape sequence or ``""``."""

RESET = "\x1b[0m"

_ANSI_PATTERN = (
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI ... final byte (SGR, cursor movement, ...)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST (titles, hyperlinks)
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS / SOS / PM / APC ... ST
    r"|\x1b[ -/]*[0-~]"  # two-character (and nF) escapes
)
_ansi_regex: re.Pattern[str] | None = None


def ansi_pattern() -> re.Pattern[str]:
    """The compiled regular expression matching one escape sequence (compiled lazily)."""
    global _ansi_regex  # noqa: PLW0603 - lazily compiled to keep import cheap
    if _ansi_regex is None:
        import re  # noqa: PLC0415

        _ansi_regex = re.compile(_ANSI_PATTERN)
    return _ansi_regex


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (and stray ``ESC`` characters) from ``text``."""
    if "\x1b" not in text:
        return text
    return ansi_pattern().sub("", text).replace("\x1b", "")


_WIDTH_CACHE: dict[str, int] = {}


def _compute_width(char: str) -> int:
    import unicodedata  # noqa: PLC0415 - only needed for non-ASCII text

    code = ord(char)
    if 0x1160 <= code <= 0x11FF:  # Hangul Jamo medial vowels / final consonants
        return 0
    category = unicodedata.category(char)
    if category in ("Mn", "Me", "Cf", "Cc", "Zl", "Zp", "Cs") or unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def char_width(char: str) -> int:
    """Number of terminal columns ``char`` occupies (0, 1 or 2)."""
    code = ord(char)
    if code < 0x7F:
        return 1 if code >= 0x20 else 0
    if code < 0xA0:
        return 0
    if code < 0x300 or 0x2500 <= code <= 0x259F:  # Latin-1 (incl. soft hyphen), box drawing
        return 1
    width = _WIDTH_CACHE.get(char)
    if width is None:
        width = _WIDTH_CACHE[char] = _compute_width(char)
    return width


def _line_width(text: str) -> int:
    if text.isascii():
        if text.isprintable():
            return len(text)
        if "\x1b" in text:
            text = strip_ansi(text)
        return sum(1 for char in text if " " <= char < "\x7f")
    if "\x1b" in text:
        text = strip_ansi(text)
    width = 0
    for char in text:
        width += char_width(char)
    return width


def visible_width(text: str) -> int:
    """Visible width of ``text`` in terminal columns.

    Escape sequences are ignored and East Asian wide characters count as 2.
    For multi-line text the width of the widest line is returned. Tabs and
    other control characters count as 0 (expand tabs first).
    """
    if "\n" in text:
        return max(_line_width(line) for line in text.split("\n"))
    return _line_width(text)


def _cells(text: str) -> Iterator[tuple[str, int]]:
    """Split text into ``(piece, width)`` cells; escape sequences are atomic, 0 wide."""
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\x1b":
            match = ansi_pattern().match(text, index)
            end = match.end() if match else index + 1
            yield text[index:end], 0
            index = end
            continue
        yield char, char_width(char)
        index += 1


def clip_text(text: str, width: int) -> str:
    """Cut ``text`` to at most ``width`` visible columns (wide characters never split)."""
    if width <= 0:
        return ""
    kept: list[str] = []
    column = 0
    for piece, cells in _cells(text):
        if column + cells > width:
            break
        kept.append(piece)
        column += cells
    return "".join(kept)


def truncate(text: str, width: int, end: str = "…") -> str:
    """Limit ``text`` to ``width`` visible columns, marking the cut with ``end``."""
    if width <= 0:
        return ""
    if visible_width(text) <= width:
        return text
    end_width = visible_width(end)
    if end_width > width:
        return clip_text(text, width)
    return clip_text(text, width - end_width) + end


def pad(text: str, width: int, fill: str = " ", align: str = "<") -> str:
    """Pad ``text`` with ``fill`` to ``width`` visible columns.

    ``align`` is ``"<"`` (left-justify), ``">"`` (right) or ``"^"`` (center).
    Text that is already wide enough is returned unchanged.
    """
    missing = width - visible_width(text)
    if missing <= 0 or not fill:
        return text
    if align == "<":
        return text + fill * missing
    if align == ">":
        return fill * missing + text
    left = missing // 2
    return fill * left + text + fill * (missing - left)


_MAX_TAB_SIZE = 10_000  # wider than any terminal; bounds memory for absurd --tab-size values


def expand_tabs(runs: list[Run], tab_size: int) -> list[Run]:
    """Replace tabs with spaces up to the next multiple of ``tab_size`` columns.

    Columns are counted across runs (visible width). ``tab_size <= 0``
    removes tabs; tab stops are capped at 10,000 columns.
    """
    tab_size = min(tab_size, _MAX_TAB_SIZE)
    column = 0
    result: list[Run] = []
    for sgr, text in runs:
        if "\t" not in text:
            result.append((sgr, text))
            column += _line_width(text)
            continue
        pieces: list[str] = []
        for index, part in enumerate(text.split("\t")):
            if index and tab_size > 0:
                spaces = tab_size - column % tab_size
                pieces.append(" " * spaces)
                column += spaces
            pieces.append(part)
            column += _line_width(part)
        result.append((sgr, "".join(pieces)))
    return result


_Cell = tuple[str, str, int]  # (sgr, piece, width)


def _flatten(runs: list[Run]) -> list[_Cell]:
    return [(sgr, piece, cells) for sgr, text in runs for piece, cells in _cells(text)]


def _regroup(cells: list[_Cell]) -> list[Run]:
    runs: list[Run] = []
    current_sgr = ""
    pieces: list[str] = []
    for sgr, piece, _ in cells:
        if sgr != current_sgr and pieces:
            runs.append((current_sgr, "".join(pieces)))
            pieces = []
        current_sgr = sgr
        pieces.append(piece)
    if pieces:
        runs.append((current_sgr, "".join(pieces)))
    return runs


def _rstrip(cells: list[_Cell]) -> list[_Cell]:
    """Drop trailing whitespace, looking through (and keeping) zero-width cells."""
    end = len(cells)
    zero_width: list[_Cell] = []
    while end:
        cell = cells[end - 1]
        if not cell[2]:
            zero_width.append(cell)
        elif not cell[1].isspace():
            break
        end -= 1
    if not zero_width:
        return cells[:end]
    return cells[:end] + zero_width[::-1]


_NO_BREAK = frozenset("\xa0\u2007\u202f")  # no-break, figure and narrow no-break space


def wrap_runs(runs: list[Run], width: int) -> list[list[Run]]:
    """Word-wrap one logical line to ``width`` columns.

    Breaks at the last whitespace (other than no-break spaces) when possible (the
    whitespace at the break is dropped), otherwise hard-breaks. A character wider than ``width`` is
    placed on its own line.
    """
    lines: list[list[_Cell]] = []
    line: list[_Cell] = []
    column = 0
    break_at = -1  # index in `line` of the last whitespace preceded by content
    has_content = False
    skip_space = False
    for cell in _flatten(runs):
        cells = cell[2]
        if not cells:
            line.append(cell)
            continue
        is_space = cell[1].isspace() and cell[1] not in _NO_BREAK
        if skip_space:
            if is_space:
                continue
            skip_space = False
        if column + cells <= width:
            if is_space:
                if has_content:
                    break_at = len(line)
            else:
                has_content = True
            line.append(cell)
            column += cells
            continue
        # The cell overflows the line.
        if is_space:
            if has_content:
                lines.append(_rstrip(line))
                line = []
            else:  # a whitespace-only line: drop the whitespace instead of emitting it
                line = [c for c in line if not c[2]]
            column, break_at, has_content, skip_space = 0, -1, False, True
            continue
        if break_at >= 0:
            lines.append(_rstrip(line[:break_at]))
            line = line[break_at + 1 :]
            column = sum(c[2] for c in line)
            # The tail has no whitespace and is at most width - 2 columns wide (the
            # head held content plus the break), so the overflowing cell now fits.
            has_content = any(c[2] for c in line)
            break_at = -1
        elif has_content:
            lines.append(line)
            line, column, has_content = [], 0, False
        else:  # only whitespace before this cell: drop it (keep escape sequences)
            line, column = [c for c in line if not c[2]], 0
        line.append(cell)
        column += cells
        has_content = True
    if has_content or not lines:
        lines.append(line)
    else:  # only zero-width cells (escape sequences) or nothing left: keep them on the last line
        lines[-1].extend(line)
    return [_regroup(cells) for cells in lines]


def clip_runs(runs: list[Run], width: int) -> list[Run]:
    """Cut one logical line to ``width`` columns, discarding the rest."""
    result: list[Run] = []
    column = 0
    for sgr, text in runs:
        text_width = _line_width(text)
        if column + text_width <= width:
            result.append((sgr, text))
            column += text_width
            continue
        result.append((sgr, clip_text(text, width - column)))
        break
    return result


def layout(
    lines: list[list[Run]],
    *,
    width: int = 0,
    height: int = 0,
    overflow: str = "wrap",
    tab_size: int = 4,
) -> list[list[Run]]:
    """Expand tabs, then wrap or clip every line to ``width`` and keep ``height`` lines.

    ``width``/``height`` of 0 mean unlimited; ``overflow`` is ``"wrap"`` or ``"clip"``.
    """
    result: list[list[Run]] = []
    for runs in lines:
        if any("\t" in text for _, text in runs):
            runs = expand_tabs(runs, tab_size)  # noqa: PLW2901
        if width > 0 and sum(_line_width(text) for _, text in runs) > width:
            if overflow == "clip":
                result.append(clip_runs(runs, width))
            else:
                result.extend(wrap_runs(runs, width))
        else:
            result.append(runs)
        if 0 < height <= len(result):
            del result[height:]
            break
    return result


def serialize(lines: list[list[Run]]) -> str:
    """Join lines of runs into terminal text.

    A style's escape sequence is emitted lazily (only before visible text) and
    only when it differs from the active one; a line that emitted any style
    ends with ``ESC[0m``. Plain runs produce no escapes at all.
    """
    output: list[str] = []
    for runs in lines:
        active = ""
        parts: list[str] = []
        for sgr, text in runs:
            if not text:
                continue
            if sgr != active:
                parts.append(sgr or RESET)
                active = sgr
            parts.append(text)
        if active:
            parts.append(RESET)
        output.append("".join(parts))
    return "\n".join(output)
