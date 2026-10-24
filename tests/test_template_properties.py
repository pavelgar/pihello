"""Property-based tests (hypothesis) for the template engine."""

from __future__ import annotations

import re
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pihello.errors import TemplateError
from pihello.template import (
    ColorDepth,
    RenderOptions,
    Template,
    escape,
    strip_ansi,
    visible_width,
)
from pihello.template.layout import char_width, wrap_runs

SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
THEME = {"good": "green bold", "bad": "red", "muted": "grey50 italic"}

SPECIAL = [*"[]{}\\|:()#@,=\"'. \n\t_-%", "{#", "#}", "\x1b", "\x1b[", "日", "\u0301"]
any_text = st.lists(st.one_of(st.sampled_from(SPECIAL), st.characters()), max_size=60).map("".join)

TEXT_FRAGMENTS = st.lists(
    st.sampled_from([*"ab xyz.,!-", "日", "本", "─", "█", "e\u0301", "\t", "\n", "\x1b[1m"]),
    max_size=12,
).map("".join)
TAGS = st.sampled_from(
    [
        "[bold]",
        "[]",
        "[red on navy]",
        "[@good]",
        "[@bad underline]",
        "[#ff8800 _red underline]",
        "[italic grey50]",
        "[{s}]",
        "[bold {flag|if(@good, @muted)}]",
        "[{flag|map(true=steelblue, false=:maroon)}]",
        "[normal]",
    ]
)
EXPRESSIONS = st.sampled_from(
    [
        "{n}",
        "{n|number}",
        "{n|compact}",
        "{s}",
        "{s|upper}",
        "{s|truncate(3)}",
        "{s|ljust(6, .)}",
        "{f}",
        "{f:.2f}",
        "{f|percent}",
        "{lst}",
        "{lst|spark(4)}",
        "{lst|pluck(x)|sum}",
        "{n|bar(5, 50)}",
        "{n|duration}",
        "{missing}",
        "{flag}",
        '{"日本"}',
        '{"─"|repeat(3)}',
    ]
)
ESCAPED = st.sampled_from(["\\[", "\\]", "\\{", "\\}", "\\\\", "{# c #}"])
TEMPLATES = st.lists(st.one_of(TEXT_FRAGMENTS, TAGS, EXPRESSIONS, ESCAPED), max_size=12).map(
    "".join
)
VARIABLES = st.fixed_dictionaries(
    {
        "n": st.one_of(st.integers(-(10**12), 10**12), st.none()),
        "s": st.text(max_size=10),
        "f": st.floats(allow_nan=True, allow_infinity=True),
        "lst": st.lists(st.one_of(st.integers(-100, 100), st.none()), max_size=8),
        "flag": st.booleans(),
    }
)
DEPTHS = st.sampled_from([ColorDepth.ANSI16, ColorDepth.ANSI256, ColorDepth.TRUECOLOR])
LAYOUTS = st.fixed_dictionaries(
    {
        "width": st.integers(0, 30),
        "height": st.integers(0, 5),
        "overflow": st.sampled_from(["wrap", "clip"]),
        "tab_size": st.integers(0, 8),
    }
)


@SETTINGS
@given(source=any_text)
def test_parser_only_raises_template_error(source: str) -> None:
    try:
        template = Template.parse(source)
    except TemplateError as error:
        line, column = error.line or 0, error.column or 0
        assert line >= 1
        assert column >= 1
        return
    try:
        template.render({"a": 1, "b": [1, 2], "x": "s"}, theme=THEME)
        template.render({}, options=RenderOptions(strict=True, width=10, color=ColorDepth.ANSI16))
    except TemplateError:
        pass


@SETTINGS
@given(text=st.text(max_size=80).filter(lambda t: "\t" not in t and "\r" not in t))
def test_escaped_text_round_trips(text: str) -> None:
    expected = text[:-1] if text.endswith("\n") else text
    assert Template.parse(escape(text)).render({}) == expected


@SETTINGS
@given(source=TEMPLATES, variables=VARIABLES, layout=LAYOUTS)
def test_no_color_output_has_no_escapes(
    source: str, variables: dict[str, Any], layout: dict[str, Any]
) -> None:
    template = Template.parse(source)
    output = template.render(
        variables, theme=THEME, options=RenderOptions(color=ColorDepth.NONE, **layout)
    )
    assert "\x1b" not in output


@SETTINGS
@given(source=TEMPLATES, variables=VARIABLES, layout=LAYOUTS, depth=DEPTHS)
def test_strip_ansi_equals_no_color_render(
    source: str, variables: dict[str, Any], layout: dict[str, Any], depth: ColorDepth
) -> None:
    template = Template.parse(source)
    colored = template.render(variables, theme=THEME, options=RenderOptions(color=depth, **layout))
    plain = template.render(
        variables, theme=THEME, options=RenderOptions(color=ColorDepth.NONE, **layout)
    )
    assert strip_ansi(colored) == plain


@SETTINGS
@given(source=TEMPLATES, variables=VARIABLES, layout=LAYOUTS, depth=DEPTHS)
def test_layout_limits(
    source: str, variables: dict[str, Any], layout: dict[str, Any], depth: ColorDepth
) -> None:
    output = Template.parse(source).render(
        variables, theme=THEME, options=RenderOptions(color=depth, **layout)
    )
    lines = output.split("\n")
    if layout["height"]:
        assert len(lines) <= layout["height"]
    if layout["width"] >= 2:
        assert all(visible_width(line) <= layout["width"] for line in lines)
    for line in lines:
        # styles never bleed: the last style sequence the engine emits on a line is a reset
        # (engine sequences always start with "0"; raw literal escapes like ESC[1m do not)
        emitted = re.findall(r"\x1b\[0[;\d]*m", line)
        if emitted:
            assert emitted[-1] == "\x1b[0m"


@SETTINGS
@given(text=st.text(max_size=60), width=st.integers(1, 20))
def test_wrap_preserves_non_space_content(text: str, width: int) -> None:
    text = text.replace("\n", " ").replace("\t", " ").replace("\x1b", "")
    lines = wrap_runs([("", text)], width)
    joined = "".join(t for runs in lines for _, t in runs)
    assert [c for c in joined if not c.isspace()] == [c for c in text if not c.isspace()]
    for runs in lines:
        line_width = sum(visible_width(t) for _, t in runs)
        widest = max((char_width(c) for _, t in runs for c in t), default=0)
        assert line_width <= max(width, widest)


@SETTINGS
@given(text=st.text(max_size=40))
def test_visible_width_ignores_escapes(text: str) -> None:
    styled = f"\x1b[0;1;38;5;45m{text}\x1b[0m"
    assert visible_width(styled) == visible_width(text) == visible_width(strip_ansi(styled))
