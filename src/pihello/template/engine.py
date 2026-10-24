"""Template evaluation: :class:`Template`, :class:`RenderOptions` and rendering.

A :class:`Template` is parsed once and can be rendered many times (watch
mode) with different variables, themes and options. Rendering evaluates the
AST into lines of ``(sgr, text)`` runs, lays them out (tabs, wrap/clip,
height) and serialises them with minimal escape sequences.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..errors import TemplateError
from .color import ColorDepth, alternatives, did_you_mean, suggest
from .filters import to_number, to_text
from .layout import layout, serialize, strip_ansi
from .parser import DYNAMIC, THEME, Expr, Tag, Text, excerpt, parse
from .style import EMPTY, Style, StyleError, build_style, split_words

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .layout import Run
    from .parser import Node

__all__ = ["RenderOptions", "Template", "escape", "render"]


@dataclass(frozen=True)
class RenderOptions:
    """How a template is rendered.

    ``width``/``height`` of 0 mean unlimited. ``now`` is the timestamp used by
    the ``ago`` filter (``None`` = current time).
    """

    color: ColorDepth = ColorDepth.TRUECOLOR
    width: int = 0
    height: int = 0
    overflow: Literal["wrap", "clip"] = "wrap"
    tab_size: int = 4
    strict: bool = False
    missing: str = "?"
    now: float | None = None

    def __post_init__(self) -> None:
        if type(self.color) is not ColorDepth:
            object.__setattr__(self, "color", ColorDepth(self.color))
        for field in ("width", "height", "tab_size"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"RenderOptions.{field} must be an integer >= 0, got {value!r}")
        if self.overflow not in ("wrap", "clip"):
            raise ValueError(
                f"RenderOptions.overflow must be 'wrap' or 'clip', got {self.overflow!r}"
            )


_DEFAULT_OPTIONS = RenderOptions()
_MISSING: Any = object()

# Built styles keyed by their final word list (themes resolve to the same words).
_STYLE_CACHE: dict[tuple[str, ...], Style] = {}
# Theme style strings → validated words.
_THEME_WORDS: dict[str, tuple[str, ...]] = {}
_CACHE_LIMIT = 512


def _sanitize(text: str) -> str:
    """Remove escape sequences, control characters (except newline and tab) and lone
    surrogates (which cannot be encoded for output) from data.

    Whole escape sequences go, not just their ESC: widths count them as zero
    columns, so a padded value keeps its width.
    """
    if "\x1b" in text:
        text = strip_ansi(text)
    return "".join(
        ch
        for ch in text
        if ch in "\n\t" or not (ch < " " or "\x7f" <= ch < "\xa0" or "\ud800" <= ch <= "\udfff")
    )


# pihello 0.3 variable names → their 1.0 names (docs/upgrading.md), only for strict hints.
# A key also renames the paths below it (gravity_last_updated.relative.days).
_RENAMED_IN_1_0 = {
    "status": "blocking",
    "core_current": "version.core.local.version",
    "web_current": "version.web.local.version",
    "FTL_current": "version.ftl.local.version",
    "dns_queries_today": "queries.total",
    "ads_blocked_today": "queries.blocked",
    "ads_percentage_today": "queries.percent_blocked",
    "domains_being_blocked": "gravity.domains_being_blocked",
    "unique_clients": "clients.active",
    "gravity_last_updated.relative": "gravity.relative",
}


def _renamed(path: str) -> str | None:
    """A hint naming the 1.0 name of a pihello 0.3 variable ``path``, else ``None``."""
    for old, new in _RENAMED_IN_1_0.items():
        if path == old or path.startswith(old + "."):
            return f"renamed to {{{new}{path[len(old) :]}}} in pihello 1.0"
    return None


class _Context:
    __slots__ = ("depth", "missing", "now", "options", "strict", "template", "theme", "variables")

    def __init__(
        self,
        template: Template,
        variables: Mapping[str, Any],
        theme: Mapping[str, str],
        options: RenderOptions,
    ) -> None:
        self.template = template
        self.variables = variables
        self.theme = theme
        self.options = options
        self.depth = options.color
        self.strict = options.strict
        self.missing = options.missing
        self.now = options.now if options.now is not None else time.time()

    def error(self, message: str, line: int, column: int, hint: str | None = None) -> TemplateError:
        return TemplateError(
            message,
            source_name=self.template.source_name,
            line=line,
            column=column,
            hint=hint,
            excerpt=excerpt(self.template.source, line, column),
        )

    # -- values ----------------------------------------------------------------
    def resolve(self, expr: Expr) -> Any:
        """The value at ``expr.path``; ``_MISSING`` if a key is absent, ``None`` if null."""
        variables = self.variables
        path = expr.path
        assert path is not None  # noqa: S101 - only called for path heads
        if "." in path:
            value = variables.get(path, _MISSING)
            if value is not _MISSING:
                return value
        current: Any = variables
        for segment in expr.segments:
            if isinstance(current, dict) or hasattr(current, "keys"):
                current = current.get(segment, _MISSING)
            elif isinstance(current, (list, tuple)) and segment.removeprefix("-").isdigit():
                position = int(segment)
                current = (
                    current[position] if -len(current) <= position < len(current) else _MISSING
                )
            else:
                return _MISSING
            if current is _MISSING or current is None:
                return current
        return current

    def evaluate(self, expr: Expr) -> tuple[Any, Any]:
        """``(head value, final value)`` of an expression."""
        head = expr.literal if expr.path is None else self.resolve(expr)
        value = None if head is _MISSING else head
        for call in expr.filters:
            spec = call.spec
            if value is None and not spec.accepts_none:
                continue
            try:
                if spec.wants_now:
                    value = spec.func(value, *call.args, now=self.now)
                else:
                    value = spec.func(value, *call.args)
            except Exception as error:
                if self.strict:
                    raise self.error(
                        f"filter '{spec.name}' failed on {value!r}: {error}", call.line, call.column
                    ) from error
                value = None
        return head, value

    def output(self, expr: Expr) -> str:
        head, value = self.evaluate(expr)
        text: str | None
        if value is not None and expr.format_spec is not None:
            text = self.format(expr, value)
        else:
            text = to_text(value)
        if text is None:
            if self.strict and not expr.has_default:
                raise self.missing_error(expr, head, value)
            return self.missing
        if not text.isprintable():
            text = _sanitize(text)
        return text

    def format(self, expr: Expr, value: Any) -> str | None:
        spec = expr.format_spec
        assert spec is not None  # noqa: S101 - caller checked
        if isinstance(value, (bool, list, tuple)):
            value = to_text(value)
        elif isinstance(value, dict) or hasattr(value, "keys"):
            return None
        candidates: list[Any] = [value]
        if isinstance(value, str):
            number = to_number(value)
            if number is not None:
                candidates.append(number)
        else:
            candidates.append(to_text(value))
        for candidate in candidates:
            try:
                return format(candidate, spec)
            except (ValueError, TypeError, OverflowError):
                continue
        if self.strict:
            raise self.error(
                f"cannot apply format spec {spec!r} to {value!r}",
                expr.line,
                expr.column,
                "numeric specs such as .2f need a number",
            )
        return to_text(value)

    def missing_error(self, expr: Expr, head: Any, value: Any) -> TemplateError:
        if expr.path is not None and head is _MISSING:
            return self.error(
                f"variable '{expr.path}' is not defined",
                expr.line,
                expr.column,
                _renamed(expr.path)
                or self.suggest_path(expr)
                or "`pihello vars` lists the available variables",
            )
        if expr.path is not None and head is None:
            return self.error(
                f"variable '{expr.path}' has no value",
                expr.line,
                expr.column,
                f'add a fallback: {{{expr.path}|default("n/a")}}',
            )
        if isinstance(value, dict) or hasattr(value, "keys"):
            keys = list(value.keys())
            example = f"{{{expr.path}.{keys[0]}}}" if keys and expr.path else "{group.key}"
            return self.error(
                f"'{expr.path or expr.source}' is a group; use e.g. {example}",
                expr.line,
                expr.column,
                f"keys: {', '.join(map(str, keys[:12]))}" if keys else None,
            )
        return self.error(
            f"{expr.source} has no value",
            expr.line,
            expr.column,
            'add a fallback with |default("n/a")',
        )

    def suggest_path(self, expr: Expr) -> str | None:
        current: Any = self.variables
        prefix: list[str] = []
        segments = expr.segments
        for position, segment in enumerate(segments):
            if isinstance(current, dict) or hasattr(current, "keys"):
                if segment in current and current[segment] is not None:
                    prefix.append(segment)
                    current = current[segment]
                    continue
                candidates = [str(key) for key in current]
                if not prefix and any("." in key for key in candidates):
                    flat = suggest(expr.path or "", candidates)
                    if flat:
                        return "did you mean " + alternatives(flat) + "?"
                matches = suggest(segment, candidates)
                if not matches:
                    return None
                rest = segments[position + 1 :]
                options = [".".join((*prefix, match, *rest)) for match in matches]
                return "did you mean " + alternatives(options) + "?"
            if isinstance(current, (list, tuple)) and segment.isdigit():
                if int(segment) >= len(current):
                    return f"'{'.'.join(prefix)}' has only {len(current)} items"
                prefix.append(segment)
                current = current[int(segment)]
                continue
            return None
        return None  # pragma: no cover - only called for paths that do not resolve

    # -- styles ----------------------------------------------------------------
    def has_theme_style(self, name: str) -> bool:
        theme = self.theme
        return name in theme or name.lower() in theme

    def theme_words(self, name: str, line: int, column: int) -> tuple[str, ...]:
        theme = self.theme
        value = theme.get(name)
        if value is None:
            value = theme.get(name.lower())
        if value is None:
            hint = did_you_mean(name, list(theme), prefix="@") if theme else None
            if hint is None:
                hint = (
                    "available theme styles: " + ", ".join(f"@{key}" for key in theme)
                    if theme
                    else "no theme is active; pass theme={...} to render()"
                )
            raise self.error(f"unknown theme style '@{name}'", line, column, hint)
        words = _THEME_WORDS.get(value)
        if words is None:
            words = tuple(split_words(value))
            for word in words:
                if word.startswith("@"):
                    raise self.error(
                        f"theme style '@{name}' refers to {word!r}",
                        line,
                        column,
                        "theme styles cannot reference other theme styles",
                    )
            try:
                build_style(words)
            except StyleError as error:
                raise self.error(
                    f"theme style '@{name}' ({value!r}) is invalid: {error.message}",
                    line,
                    column,
                    error.hint,
                ) from None
            if len(_THEME_WORDS) >= _CACHE_LIMIT:
                _THEME_WORDS.clear()
            _THEME_WORDS[value] = words
        return words

    def tag_style(self, tag: Tag) -> Style:
        words: list[str] = []
        has_dynamic = False
        for name, line, column in tag.theme_refs:
            self.theme_words(name, line, column)  # template typos fail on every render
        for kind, payload, line, column in tag.items:
            if kind == THEME:
                words.extend(self.theme_words(payload, line, column))
            elif kind == DYNAMIC:
                has_dynamic = True
                pieces = []
                for piece in payload:
                    if isinstance(piece, str):
                        pieces.append(piece)
                    else:
                        pieces.append(to_text(self.evaluate(piece)[1]) or "")
                for word in split_words("".join(pieces)):
                    if not word.startswith("@"):
                        words.append(word)
                    elif self.strict or self.has_theme_style(word[1:]):
                        words.extend(self.theme_words(word[1:], line, column))
                    # else: an unknown '@word' produced by data contributes nothing
            else:
                words.append(payload)
        key = tuple(words)
        style = _STYLE_CACHE.get(key)
        if style is not None:
            return style
        dropped = False
        while True:
            try:
                style = build_style(words)
                break
            except StyleError as error:
                if self.strict or not has_dynamic:
                    raise self.error(
                        f"invalid style from {tag.source}: {error.message}",
                        tag.line,
                        tag.column,
                        error.hint,
                    ) from None
                # Data produced something that is not a style (or left a dangling
                # 'on'): drop the offending word, as a missing value would be.
                del words[error.index or 0]
                dropped = True
        if not dropped:
            if len(_STYLE_CACHE) >= _CACHE_LIMIT:
                _STYLE_CACHE.clear()
            _STYLE_CACHE[key] = style
        return style


class Template:
    """A parsed template. Parse once with :meth:`parse`, render many times."""

    __slots__ = ("_nodes", "_paths", "source", "source_name")

    source_name: str
    """Name used in error messages (a file name or ``"<template>"``)."""

    def __init__(self, source: str, *, name: str = "<template>") -> None:
        self.source = source
        self.source_name = name
        self._nodes: list[Node]
        self._nodes, self._paths = parse(source, name)

    @classmethod
    def parse(cls, source: str, *, name: str = "<template>") -> Template:
        """Parse ``source``; raises :class:`~pihello.errors.TemplateError` on errors."""
        return cls(source, name=name)

    def paths(self) -> frozenset[str]:
        """Every variable path the template references (including inside style tags)."""
        return self._paths

    def render(
        self,
        variables: Mapping[str, Any],
        *,
        theme: Mapping[str, str] | None = None,
        options: RenderOptions = _DEFAULT_OPTIONS,
    ) -> str:
        """Render with ``variables`` (a nested mapping) and the ``theme`` styles.

        Returns text without a trailing newline. Raises
        :class:`~pihello.errors.TemplateError` for unknown ``@theme`` styles and,
        with ``options.strict``, for missing values.
        """
        context = _Context(self, variables, theme or {}, options)
        depth = context.depth
        lines: list[list[Run]] = [[]]
        current = lines[0]
        sgr = ""
        style: Style = EMPTY
        for node in self._nodes:
            if isinstance(node, Text):
                current.append((sgr, node.text if depth else node.plain))
            elif isinstance(node, Tag):
                new_style = node.style if node.style is not None else context.tag_style(node)
                if new_style is not style:
                    style = new_style
                    sgr = style.sgr(depth)
            elif isinstance(node, Expr):
                text = context.output(node)
                if "\n" in text:
                    first, *rest = text.split("\n")
                    current.append((sgr, first))
                    for part in rest:
                        current = [(sgr, part)]
                        lines.append(current)
                else:
                    current.append((sgr, text))
            else:  # NEWLINE
                current = []
                lines.append(current)
        if options.width or options.height or any("\t" in t for line in lines for _, t in line):
            lines = layout(
                lines,
                width=options.width,
                height=options.height,
                overflow=options.overflow,
                tab_size=options.tab_size,
            )
        return serialize(lines)

    def __repr__(self) -> str:
        return f"<Template {self.source_name!r}>"


_TEMPLATE_CACHE: dict[tuple[str, str], Template] = {}

_ESCAPES = str.maketrans({"\\": "\\\\", "[": "\\[", "]": "\\]", "{": "\\{", "}": "\\}"})


def escape(text: str) -> str:
    """Escape ``text`` so that it renders literally inside a template."""
    return text.translate(_ESCAPES)


def render(source: str, variables: Mapping[str, Any], **kwargs: Any) -> str:
    """Parse and render ``source`` in one go (parsed templates are cached).

    Keyword arguments: ``name`` (for error messages), ``theme``, ``options``,
    and any :class:`RenderOptions` field (``color=``, ``width=``, ...) which
    overrides ``options``.
    """
    name: str = kwargs.pop("name", "<template>")
    theme: Mapping[str, str] | None = kwargs.pop("theme", None)
    options: RenderOptions = kwargs.pop("options", _DEFAULT_OPTIONS)
    if kwargs:
        import dataclasses  # noqa: PLC0415

        options = dataclasses.replace(options, **kwargs)
    key = (source, name)
    template = _TEMPLATE_CACHE.get(key)
    if template is None:
        template = Template(source, name=name)
        if len(_TEMPLATE_CACHE) >= 64:
            _TEMPLATE_CACHE.clear()
        _TEMPLATE_CACHE[key] = template
    return template.render(variables, theme=theme, options=options)
