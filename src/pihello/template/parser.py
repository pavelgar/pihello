"""Hand-written tokenizer/parser: template source → a flat list of AST nodes.

The template language has three constructs on top of literal text (see
``docs/templates.md``): style tags ``[...]``, expressions ``{...}`` and
comments ``{# ... #}``, plus backslash escapes. Parsing validates everything
that can be validated without data — style words, filter names, filter
arguments and format specs — and raises :class:`~pihello.errors.TemplateError`
with a 1-based line/column, the reason and a hint.

The result is a flat node list: :class:`Text`, :data:`NEWLINE`, :class:`Tag`
and :class:`Expr`. Comment-only lines and exactly one trailing newline of the
source are already removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import TemplateError
from . import filters as _filters
from .layout import strip_ansi
from .style import EMPTY, Style, StyleError, build_style, split_words, validate_word

if TYPE_CHECKING:
    import re

    from .filters import FilterSpec

__all__ = ["NEWLINE", "Expr", "FilterCall", "Node", "Tag", "Text", "parse"]


class Text:
    """Literal text (never contains a newline).

    ``plain`` is the text with any raw escape sequences removed, used for
    ``ColorDepth.NONE``.
    """

    __slots__ = ("plain", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.plain = strip_ansi(text) if "\x1b" in text else text

    def __repr__(self) -> str:
        return f"Text({self.text!r})"


class _Newline:
    __slots__ = ()

    def __repr__(self) -> str:
        return "NEWLINE"


NEWLINE = _Newline()
"""A line break in the template."""


class FilterCall:
    """One ``| name(args)`` step of an expression with its bound arguments."""

    __slots__ = ("args", "column", "line", "spec")

    def __init__(self, spec: FilterSpec, args: tuple[Any, ...], line: int, column: int) -> None:
        self.spec = spec
        self.args = args
        self.line = line
        self.column = column

    def __repr__(self) -> str:
        return f"FilterCall({self.spec.name}{self.args!r})"


class Expr:
    """An expression ``{head | filter ... :format_spec}``.

    ``path`` is the variable path (``None`` for a literal head, whose value is
    in ``literal``); ``segments`` is the path split at dots.
    """

    __slots__ = (
        "column",
        "filters",
        "format_spec",
        "has_default",
        "line",
        "literal",
        "path",
        "segments",
        "source",
    )

    def __init__(
        self,
        *,
        path: str | None,
        literal: Any,
        filters: tuple[FilterCall, ...],
        format_spec: str | None,
        line: int,
        column: int,
        source: str,
    ) -> None:
        self.path = path
        self.segments = tuple(path.split(".")) if path is not None else ()
        self.literal = literal
        self.filters = filters
        self.format_spec = format_spec
        self.line = line
        self.column = column
        self.source = source
        self.has_default = any(call.spec.name == "default" for call in filters)

    def __repr__(self) -> str:
        return f"Expr({self.source!r})"


# Tag item kinds
WORD = "w"  # static style word
THEME = "t"  # @name reference
DYNAMIC = "d"  # word containing {expressions}

TagItem = tuple[str, Any, int, int]  # (kind, payload, line, column)


class Tag:
    """A style tag. ``style`` is set when the tag is fully static.

    Otherwise ``items`` lists ``(kind, payload, line, column)`` entries: a static
    word (``payload`` is the word), a theme reference (the name without ``@``)
    or a dynamic word (a list of literal strings and :class:`Expr` pieces).

    ``theme_refs`` lists the ``(name, line, column)`` of every ``@name`` written
    as a literal inside the tag's expressions (e.g. ``map(enabled=@good)``):
    they are template-authored, so they are validated on every render even
    when the data selects another branch.
    """

    __slots__ = ("column", "items", "line", "source", "style", "theme_refs")

    def __init__(
        self, style: Style | None, items: tuple[TagItem, ...], line: int, column: int, source: str
    ) -> None:
        self.style = style
        self.items = items
        self.line = line
        self.column = column
        self.source = source
        refs: list[tuple[str, int, int]] = []
        for kind, payload, item_line, item_column in items:
            if kind == DYNAMIC:
                for piece in payload:
                    if isinstance(piece, Expr):
                        refs.extend(
                            (name, item_line, item_column) for name in _authored_refs(piece)
                        )
        self.theme_refs = tuple(dict.fromkeys(refs))

    def __repr__(self) -> str:
        return f"Tag({self.source!r})"


Node = Text | _Newline | Tag | Expr


def _authored_refs(expr: Expr) -> list[str]:
    """Theme names (without ``@``) written literally in ``expr``'s head or filter arguments."""
    values: list[Any] = [expr.literal]
    for call in expr.filters:
        for arg in call.args:
            values.extend(arg.values() if isinstance(arg, dict) else (arg,))
    return [
        word[1:]
        for value in values
        if isinstance(value, str) and "@" in value
        for word in split_words(value)
        if word.startswith("@") and len(word) > 1
    ]


_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_%-")
_THEME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_FILTER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_HEAD_STOP = frozenset(" \t|:}\n")
_BAREWORD_STOP = frozenset(" \t\n,()=}\"'")
_ESCAPABLE = frozenset("[]{}\\")
_LITERAL_BRACKET = "write \\[ for a literal '['"
_LITERAL_BRACE = "write \\{ for a literal '{'"
_FORMAT_DOCS = (
    "see https://docs.python.org/3/library/string.html#format-specification-mini-language"
)


_raw_escape_regex: re.Pattern[str] | None = None


def _raw_escape() -> re.Pattern[str]:
    """CSI (``ESC [ ... final``) and OSC (``ESC ] ... BEL/ST``) sequences, compiled lazily."""
    global _raw_escape_regex  # noqa: PLW0603
    if _raw_escape_regex is None:
        import re  # noqa: PLC0415

        _raw_escape_regex = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
    return _raw_escape_regex


def validate_on_target(word: str) -> bool:
    """Whether ``word`` cannot follow ``on`` (it is not a plain color)."""
    try:
        return build_style(["on", word]) is None
    except StyleError:
        return True


def parse_number(token: str) -> int | float | None:
    """``token`` as an int/float literal (``-12``, ``3.5``), else ``None``."""
    if not token or len(token) > 300:
        return None
    start = 1 if token[0] in "+-" else 0
    integer, dot, fraction = token[start:].partition(".")
    if not integer.isdigit() or not integer.isascii():
        return None
    if dot:
        if not fraction.isdigit() or not fraction.isascii():
            return None
        return float(token)
    return int(token)


def _bareword_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("none", "null"):
        return None
    number = parse_number(raw)
    return raw if number is None else number


def _check_format_spec(spec: str) -> str | None:
    """A reason why ``spec`` is not a usable format spec, or ``None``."""
    run = 0
    for char in spec:
        run = run + 1 if char.isdigit() else 0
        if run > 3:
            return f"format spec {spec!r} is too large (widths and precisions are limited to 999)"
    for probe in (0, 0.5, ""):
        try:
            format(probe, spec)
        except (ValueError, TypeError):
            continue
        return None
    return f"invalid format spec {spec!r}"


class _Parser:
    def __init__(self, source: str, name: str) -> None:
        self.src = source
        self.name = name
        self.line = 1
        self.line_start = 0
        self.paths: set[str] = set()

    # -- errors -------------------------------------------------------------
    def error(
        self, message: str, index: int, hint: str | None = None, *, line: int | None = None
    ) -> TemplateError:
        if line is None:
            line, column = self.line, index - self.line_start + 1
        else:
            column = index
        return TemplateError(
            message,
            source_name=self.name,
            line=line,
            column=column,
            hint=hint,
            excerpt=self._excerpt(line, column),
        )

    def _excerpt(self, line: int, column: int) -> str:
        return excerpt(self.src, line, column)

    def at(self, index: int) -> tuple[int, int]:
        return self.line, index - self.line_start + 1

    # -- top level ------------------------------------------------------------
    def parse(self) -> list[Node]:
        src = self.src
        length = len(src)
        kept_lines: list[list[Node]] = []
        line_nodes: list[Node] = []
        text: list[str] = []
        has_comment = False
        has_content = False
        index = 0

        def flush() -> None:
            if text:
                line_nodes.append(Text("".join(text)))
                text.clear()

        def end_line() -> None:
            nonlocal line_nodes, has_comment, has_content
            flush()
            if not (has_comment and not has_content):
                kept_lines.append(line_nodes)
            line_nodes = []
            has_comment = has_content = False

        while index < length:
            stop = index
            while stop < length and src[stop] not in "[{\\\n\x1b":
                stop += 1
            if stop > index:
                chunk = src[index:stop]
                text.append(chunk)
                if not has_content and not chunk.isspace():
                    has_content = True
                index = stop
                if index >= length:
                    break
            char = src[index]
            if char == "\n":
                end_line()
                index += 1
                self.line += 1
                self.line_start = index
            elif char == "\x1b":
                # A raw CSI/OSC escape sequence is literal text; its '[' never starts a tag.
                match = _raw_escape().match(src, index)
                end = match.end() if match else index + 1
                text.append(src[index:end])
                has_content = True
                index = end
            elif char == "\\":
                following = src[index + 1 : index + 2]
                has_content = True
                if following in _ESCAPABLE and following:
                    text.append(following)
                    index += 2
                else:
                    text.append("\\")
                    index += 1
            elif char == "[":
                flush()
                tag, index = self.tag(index)
                line_nodes.append(tag)
                has_content = True
            elif src.startswith("{#", index):
                index = self.comment(index)
                has_comment = True
            else:
                flush()
                expr, index = self.expr(index)
                line_nodes.append(expr)
                has_content = True
        end_line()

        # Literal text within a line is already merged (comments do not flush the
        # text buffer) and lines are separated by NEWLINE, so no Text nodes touch.
        nodes: list[Node] = []
        for number, line in enumerate(kept_lines):
            if number:
                nodes.append(NEWLINE)
            nodes.extend(line)
        return nodes

    def comment(self, start: int) -> int:
        end = self.src.find("#}", start + 2)
        if end < 0:
            raise self.error("unclosed comment '{#'", start, "close it with '#}'")
        newlines = self.src.count("\n", start, end)
        if newlines:
            self.line += newlines
            self.line_start = self.src.rfind("\n", start, end) + 1
        return end + 2

    # -- style tags -----------------------------------------------------------
    def static_item(self, word: str, line: int, column: int) -> TagItem:
        if word.startswith("@"):
            return (THEME, self.theme_name(word, line, column), line, column)
        try:
            validate_word(word)
        except StyleError as error:
            raise self.error(error.message, column, error.hint, line=line) from None
        return (WORD, word, line, column)

    def tag(self, start: int) -> tuple[Tag, int]:
        src = self.src
        tag_line, tag_column = self.at(start)
        close = src.find("]", start + 1)
        newline = src.find("\n", start + 1, close if close >= 0 else len(src))
        if close >= 0 and newline < 0 and src.find("{", start + 1, close) < 0:
            # Fast path: no expressions or comments inside, so ']' ends the tag.
            body = src[start + 1 : close]
            items: list[TagItem] = []
            offset = 0
            for word in split_words(body):
                offset = body.find(word, offset)
                items.append(self.static_item(word, tag_line, tag_column + 1 + offset))
                offset += len(word)
            return self.finish_tag(items, start, close + 1, tag_line, tag_column)
        return self.slow_tag(start, tag_line, tag_column)

    def slow_tag(self, start: int, tag_line: int, tag_column: int) -> tuple[Tag, int]:
        src = self.src
        length = len(src)
        items: list[TagItem] = []
        pieces: list[str | Expr] = []
        chars: list[str] = []
        word_at = (0, 0)
        depth = 0
        index = start + 1

        def finish_word() -> None:
            nonlocal depth
            if chars:
                pieces.append("".join(chars))
                chars.clear()
            if not pieces:
                return
            line, column = word_at
            if len(pieces) == 1 and isinstance(pieces[0], str):
                items.append(self.static_item(pieces[0], line, column))
            else:
                items.append((DYNAMIC, tuple(pieces), line, column))
            pieces.clear()
            depth = 0

        while True:
            if index >= length or src[index] == "\n":
                raise self.error(
                    "unclosed style tag '['",
                    tag_column,
                    f"close it with ']' or {_LITERAL_BRACKET}",
                    line=tag_line,
                )
            char = src[index]
            if char == "]":
                finish_word()
                index += 1
                break
            if char.isspace() and depth == 0:
                finish_word()
                index += 1
                continue
            if char == "{":
                if src.startswith("{#", index):
                    finish_word()
                    index = self.comment(index)
                    continue
                if not pieces and not chars:
                    word_at = self.at(index)
                if chars:
                    pieces.append("".join(chars))
                    chars.clear()
                expr, index = self.expr(index)
                pieces.append(expr)
                continue
            if not pieces and not chars:
                word_at = self.at(index)
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            chars.append(char)
            index += 1

        return self.finish_tag(items, start, index, tag_line, tag_column)

    def finish_tag(
        self, items: list[TagItem], start: int, end: int, tag_line: int, tag_column: int
    ) -> tuple[Tag, int]:
        source = self.src[start:end]
        style: Style | None = None
        if all(kind == WORD for kind, _, _, _ in items):
            try:
                style = build_style([word for _, word, _, _ in items]) if items else EMPTY
            except StyleError as error:
                position = items[error.index or 0]
                raise self.error(error.message, position[3], error.hint, line=position[2]) from None
        else:
            for number, (kind, word, line, column) in enumerate(items):
                if kind != WORD or word.lower() != "on":
                    continue
                following = items[number + 1] if number + 1 < len(items) else None
                if (
                    following is None
                    or following[0] == THEME
                    or (following[0] == WORD and validate_on_target(following[1]))
                ):
                    raise self.error(
                        "expected a color after 'on'", column, "e.g. 'white on navy'", line=line
                    )
        return Tag(style, tuple(items), tag_line, tag_column, source), end

    def theme_name(self, word: str, line: int, column: int) -> str:
        name = word[1:]
        if not name:
            raise self.error(
                "missing theme style name after '@'", column, "e.g. [@title] or [@good]", line=line
            )
        for offset, char in enumerate(name):
            if char not in _THEME_CHARS:
                raise self.error(
                    f"invalid character {char!r} in theme style {word!r}",
                    column + 1 + offset,
                    "theme style names contain letters, digits, _ and -",
                    line=line,
                )
        return name

    # -- expressions ----------------------------------------------------------
    def skip_space(self, index: int) -> int:
        src = self.src
        while index < len(src) and src[index] in " \t":
            index += 1
        return index

    def unclosed_expr(self, start: int) -> TemplateError:
        return self.error(
            "unclosed expression '{'", start, f"close it with '}}' or {_LITERAL_BRACE}"
        )

    def expr(self, start: int) -> tuple[Expr, int]:
        """Parse ``{...}`` starting at the ``{`` at ``start``."""
        src = self.src
        length = len(src)
        line, column = self.at(start)
        index = self.skip_space(start + 1)
        if index >= length or src[index] == "\n":
            raise self.unclosed_expr(start)

        path: str | None = None
        literal: Any = None
        char = src[index]
        if char in "\"'":
            literal, index = self.string(index)
        else:
            head_start = index
            while index < length and src[index] not in _HEAD_STOP:
                index += 1
            token = src[head_start:index]
            if not token:
                if char == "}":
                    raise self.error(
                        "empty expression '{}'", start, "write \\{\\} for literal braces"
                    )
                raise self.error(
                    f'expected a variable path, a "string" or a number before {char!r}',
                    index,
                    "e.g. {queries.total|number}",
                )
            number = parse_number(token)
            if number is not None:
                literal = number
            else:
                path = self.path(token, head_start)
                self.paths.add(path)

        calls: list[FilterCall] = []
        index = self.skip_space(index)
        while index < length and src[index] == "|":
            call, index = self.filter_call(self.skip_space(index + 1))
            calls.append(call)
            index = self.skip_space(index)

        format_spec: str | None = None
        if index < length and src[index] == ":":
            spec_start = index + 1
            index = spec_start
            while index < length and src[index] not in "}\n":
                index += 1
            format_spec = src[spec_start:index].rstrip()
            problem = _check_format_spec(format_spec)
            if problem is not None:
                raise self.error(problem, spec_start, _FORMAT_DOCS)

        if index >= length or src[index] == "\n":
            raise self.unclosed_expr(start)
        if src[index] != "}":
            found = src[index]
            hint = "expected '|' (a filter), ':' (a format spec) or '}'"
            if path is not None and src[index - 1] in " \t":
                hint += "; variable paths cannot contain spaces"
            raise self.error(f"unexpected {found!r} in expression", index, hint)
        index += 1
        expr = Expr(
            path=path,
            literal=literal,
            filters=tuple(calls),
            format_spec=format_spec or None,
            line=line,
            column=column,
            source=src[start:index],
        )
        return expr, index

    def path(self, token: str, start: int) -> str:
        offset = 0
        for segment in token.split("."):
            if not segment:
                raise self.error(
                    f"empty key in variable path {token!r}",
                    start + offset,
                    "paths are keys separated by single dots, e.g. queries.total",
                )
            for position, char in enumerate(segment):
                if char not in _KEY_CHARS:
                    raise self.error(
                        f"invalid character {char!r} in variable path {token!r}",
                        start + offset + position,
                        "keys may contain letters, digits, _, % and -; "
                        'quote literal text: {"text"}',
                    )
            offset += len(segment) + 1
        return token

    def string(self, start: int) -> tuple[str, int]:
        src = self.src
        quote = src[start]
        pieces: list[str] = []
        index = start + 1
        while True:
            if index >= len(src) or src[index] == "\n":
                raise self.error(
                    "unclosed string", start, f"close it with {quote}; write \\n for a newline"
                )
            char = src[index]
            if char == quote:
                return "".join(pieces), index + 1
            if char == "\\" and index + 1 < len(src):
                following = src[index + 1]
                if following in (quote, "\\"):
                    pieces.append(following)
                    index += 2
                    continue
                if following in "nt":
                    pieces.append("\n" if following == "n" else "\t")
                    index += 2
                    continue
            pieces.append(char)
            index += 1

    def filter_call(self, index: int) -> tuple[FilterCall, int]:
        src = self.src
        length = len(src)
        name_start = index
        while index < length and src[index] in _FILTER_CHARS:
            index += 1
        name = src[name_start:index]
        if not name:
            found = src[index] if index < length and src[index] != "\n" else "end of line"
            raise self.error(
                f"expected a filter name after '|', found {found!r}",
                index,
                "e.g. {queries.total|number}",
            )
        spec = _filters.get(name)
        if spec is None:
            from .color import did_you_mean  # noqa: PLC0415

            hint = did_you_mean(name.lower(), _filters.FILTERS)
            if hint is None:
                hint = "available filters: " + ", ".join(_filters.FILTERS)
            raise self.error(f"unknown filter {name!r}", name_start, hint)
        line, column = self.at(name_start)
        args: list[_filters.Arg] = []
        kwargs: dict[str, _filters.Arg] = {}
        index = self.skip_space(index)
        if index < length and src[index] == "(":
            index = self.arguments(index, args, kwargs)
        try:
            bound = _filters.bind(spec, args, kwargs)
        except _filters.FilterArgError as error:
            position = error.arg.position if error.arg is not None else (line, column)
            raise self.error(error.message, position[1], error.hint, line=position[0]) from None
        return FilterCall(spec, bound, line, column), index

    def arguments(
        self, open_index: int, args: list[_filters.Arg], kwargs: dict[str, _filters.Arg]
    ) -> int:
        src = self.src
        length = len(src)
        index = open_index + 1

        def unclosed() -> TemplateError:
            return self.error("unclosed '(' in filter arguments", open_index, "close it with ')'")

        while True:
            index = self.skip_space(index)
            if index >= length or src[index] in "\n}":
                raise unclosed()
            if src[index] == ")":
                return index + 1
            arg, index = self.argument(index)
            index = self.skip_space(index)
            if index < length and src[index] == "=":
                key = arg.value if arg.quoted else arg.raw
                if key in kwargs:
                    raise self.error(
                        f"argument {key!r} given twice",
                        arg.position[1],
                        "give each keyword argument once",
                        line=arg.position[0],
                    )
                value, index = self.argument(self.skip_space(index + 1))
                kwargs[key] = value
                index = self.skip_space(index)
            elif kwargs:
                raise self.error(
                    "positional argument after keyword arguments",
                    arg.position[1],
                    "put positional arguments first",
                    line=arg.position[0],
                )
            else:
                args.append(arg)
            if index >= length or src[index] in "\n}":
                raise unclosed()
            if src[index] == ",":
                index += 1
            elif src[index] != ")":
                raise self.error(
                    f"unexpected {src[index]!r} in filter arguments",
                    index,
                    "separate arguments with ',' and quote text that contains special characters",
                )

    def argument(self, index: int) -> tuple[_filters.Arg, int]:
        src = self.src
        position = self.at(index)
        if index < len(src) and src[index] in "\"'":
            value, end = self.string(index)
            return _filters.Arg(value, src[index:end], quoted=True, position=position), end
        end = index
        while end < len(src) and src[end] not in _BAREWORD_STOP:
            end += 1
        raw = src[index:end]
        if not raw:
            found = src[index] if index < len(src) and src[index] != "\n" else "end of line"
            raise self.error(
                f"expected an argument, found {found!r}",
                index,
                'quote arguments that contain special characters, e.g. join(", ")',
            )
        return _filters.Arg(_bareword_value(raw), raw, position=position), end


def _normalize(source: str) -> str:
    """``source`` without a byte order mark and with ``\\n`` line endings."""
    if source.startswith("\ufeff"):  # a byte order mark is not content
        source = source[1:]
    if "\r" in source:
        source = source.replace("\r\n", "\n")
    return source


def excerpt(source: str, line: int, column: int) -> str:
    """Line ``line`` of template ``source`` with a caret under ``column`` (for errors)."""
    lines = _normalize(source).split("\n")
    text = lines[line - 1] if 0 < line <= len(lines) else ""
    gutter = f"{line} | "
    return f"{gutter}{text}\n{' ' * (len(gutter) - 2)}| {' ' * (column - 1)}^"


def parse(source: str, name: str = "<template>") -> tuple[list[Node], frozenset[str]]:
    """Parse template ``source`` into ``(nodes, paths)``.

    Raises :class:`~pihello.errors.TemplateError` on invalid input.
    """
    source = _normalize(source)
    if source.endswith("\n"):
        source = source[:-1]
    parser = _Parser(source, name)
    nodes = parser.parse()
    return nodes, frozenset(parser.paths)
