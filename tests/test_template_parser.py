"""Tokenizer/parser: AST shape, paths, comments, escapes and error reporting."""

from __future__ import annotations

import pytest

from pihello.errors import EXIT_USAGE, TemplateError
from pihello.template import Template
from pihello.template.color import ColorDepth
from pihello.template.parser import NEWLINE, Expr, Node, Tag, Text, parse, parse_number


def nodes(source: str) -> list[Node]:
    return parse(source)[0]


# --------------------------------------------------------------------------- #
# AST shape                                                                    #
# --------------------------------------------------------------------------- #
def test_plain_text_is_one_node() -> None:
    (node,) = nodes("hello world")
    assert isinstance(node, Text)
    assert node.text == "hello world"
    assert repr(node) == "Text('hello world')"


def test_lines_and_nodes() -> None:
    result = nodes("a [bold]b[]\n{x}")
    assert [type(n).__name__ for n in result] == ["Text", "Tag", "Text", "Tag", "_Newline", "Expr"]
    assert result[4] is NEWLINE
    assert repr(NEWLINE) == "NEWLINE"
    tag = result[1]
    assert isinstance(tag, Tag)
    assert tag.style is not None
    assert repr(tag) == "Tag('[bold]')"
    expr = result[5]
    assert isinstance(expr, Expr)
    assert (expr.path, expr.line, expr.column) == ("x", 2, 1)
    assert repr(expr) == "Expr('{x}')"


def test_expression_structure() -> None:
    (expr,) = nodes("{ queries.total | number(2) | default('-') :>10 }")
    assert isinstance(expr, Expr)
    assert expr.path == "queries.total"
    assert expr.segments == ("queries", "total")
    assert [call.spec.name for call in expr.filters] == ["number", "default"]
    assert expr.filters[0].args == (2,)
    assert repr(expr.filters[0]) == "FilterCall(number(2,))"
    assert expr.format_spec == ">10"
    assert expr.has_default


@pytest.mark.parametrize(
    ("source", "literal"),
    [
        ('{"text"}', "text"),
        ("{'text'}", "text"),
        ('{"say \\"hi\\""}', 'say "hi"'),
        ("{'it\\'s'}", "it's"),
        ('{"a\\\\b"}', "a\\b"),
        ('{"a\\nb\\tc"}', "a\nb\tc"),
        ('{"C:\\path"}', "C:\\path"),
        ('{"}"}', "}"),
        ('{"|:"}', "|:"),
        ("{42}", 42),
        ("{-7}", -7),
        ("{+3}", 3),
        ("{1.5}", 1.5),
    ],
)
def test_literal_heads(source: str, literal: object) -> None:
    (expr,) = nodes(source)
    assert isinstance(expr, Expr)
    assert expr.path is None
    assert expr.literal == literal


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("1", 1),
        ("-1", -1),
        ("1.25", 1.25),
        ("1.", None),
        (".5", None),
        ("1e5", None),
        ("v6.5", None),
        ("", None),
        ("-", None),
        ("\uff11", None),  # fullwidth digit
        ("9" * 301, None),
        ("1.\u0663", None),
    ],
)
def test_parse_number(token: str, value: object) -> None:
    assert parse_number(token) == value


def test_filter_arguments() -> None:
    (expr,) = nodes(
        "{x|map(enabled=@good, 'not found'=@bad, _=none)|if(true, false)|slice(1, none)"
        '|replace( a , "b c" )|join(sep=", ")}'
    )
    assert isinstance(expr, Expr)
    calls = {call.spec.name: call.args for call in expr.filters}
    assert calls["map"] == ({"enabled": "@good", "not found": "@bad", "_": None},)
    assert calls["if"] == (True, False)
    assert calls["slice"] == (1, None)
    assert calls["replace"] == ("a", "b c")
    assert calls["join"] == (", ",)


def test_bareword_conversions() -> None:
    (expr,) = nodes("{x|if(v6.5, NULL)|default(TRUE)|eq(1.5)|ne(-3)|gt(%H:%M)}")
    assert isinstance(expr, Expr)
    assert [call.args for call in expr.filters] == [
        ("v6.5", None),
        (True,),
        (1.5,),
        (-3,),
        ("%H:%M",),
    ]


def test_trailing_comma_and_spacing() -> None:
    (expr,) = nodes("{x | round ( 2 , ) }")
    assert isinstance(expr, Expr)
    assert expr.filters[0].args == (2,)


def test_filter_names_are_case_insensitive() -> None:
    (expr,) = nodes("{x|UPPER}")
    assert isinstance(expr, Expr)
    assert expr.filters[0].spec.name == "upper"


# --------------------------------------------------------------------------- #
# Template.paths                                                               #
# --------------------------------------------------------------------------- #
def test_paths() -> None:
    template = Template.parse(
        "{queries.total|number} [{blocking|map(enabled=@good)}]x {top_domains.0.domain}\n"
        '[bold {updates.available|if(red, green)}] {"lit"} {42} {system.memory.ram.%used}'
        " {a-b}"
    )
    assert template.paths() == frozenset(
        {
            "queries.total",
            "blocking",
            "top_domains.0.domain",
            "updates.available",
            "system.memory.ram.%used",
            "a-b",
        }
    )


def test_paths_empty() -> None:
    assert Template.parse("[bold]just text[]").paths() == frozenset()


# --------------------------------------------------------------------------- #
# Escapes, comments, newlines                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source", "text"),
    [
        ("\\[bold\\]", "[bold]"),
        ("\\{x\\}", "{x}"),
        ("a\\\\b", "a\\b"),
        ("C:\\path\\to", "C:\\path\\to"),  # other escapes are verbatim
        ("trailing\\", "trailing\\"),
        ("lone ] and } are literal", "lone ] and } are literal"),
        ("\\n", "\\n"),
    ],
)
def test_escapes(source: str, text: str) -> None:
    (node,) = nodes(source)
    assert isinstance(node, Text)
    assert node.text == text


def _texts(source: str) -> str:
    out = []
    for node in nodes(source):
        if node is NEWLINE:
            out.append("\n")
        elif isinstance(node, Text):
            out.append(node.text)
        else:
            out.append("<node>")
    return "".join(out)


@pytest.mark.parametrize(
    ("source", "rendered"),
    [
        ("a\n", "a"),  # exactly one trailing newline dropped
        ("a\n\n", "a\n"),
        ("a\r\nb\r\n", "a\nb"),
        ("a\n{# comment #}\nb", "a\nb"),  # comment-only lines vanish
        ("a\n   {# c #}  \t\nb", "a\nb"),
        ("a\n{# one #}{# two #}\nb", "a\nb"),
        ("{# first #}\na", "a"),
        ("a\n{# last #}", "a"),
        ("a\n{# last #}\n", "a"),
        ("a\n{# multi\nline\ncomment #}\nb", "a\nb"),
        ("a {# inline #} b", "a  b"),
        ("x{# spans\nlines #}y", "xy"),
        ("a\n\nb", "a\n\nb"),  # blank lines without comments stay
        ("a\n  \nb", "a\n  \nb"),
        ("[bold]{# styled #}\nx", "<node>\nx"),
        ("{# only #}", ""),
        ("", ""),
        ("\n", ""),
        ("{#}{#}#}", "#}"),  # the comment ends at the first '#}'
        ("{# a #}\n{#}\n#}\nb", "b"),
        ("\\{# not a comment #}", "{# not a comment #}"),
    ],
)
def test_comments_and_newlines(source: str, rendered: str) -> None:
    assert _texts(source) == rendered


def test_leading_bom_is_ignored() -> None:
    # Regression: a BOM (e.g. a template piped through `-f -` from a Windows editor)
    # counted as content, so the header comment line stayed as a blank first line.
    assert _texts("\ufeff{# header #}\na") == "a"
    assert _texts("\ufeffa\n") == "a"
    assert _texts("a\ufeff") == "a\ufeff"  # only a leading BOM is a byte order mark


def test_comment_inside_tag() -> None:
    (tag, text) = nodes("[bold{# note #}red]x")
    assert isinstance(tag, Tag)
    assert tag.style is not None
    assert tag.style.sgr(ColorDepth.TRUECOLOR) == "\x1b[0;1;91m"
    assert isinstance(text, Text)


def test_tag_items_for_dynamic_tags() -> None:
    (tag, _) = nodes("[@title bold {x|upper} on{y} rgb(1, 2, 3)]x")
    assert isinstance(tag, Tag)
    assert tag.style is None
    kinds = [item[0] for item in tag.items]
    assert kinds == ["t", "w", "d", "d", "w"]
    assert tag.items[0][1] == "title"
    assert tag.items[4][1] == "rgb(1, 2, 3)"
    assert tag.items[3][1][0] == "on"
    assert tag.items[2][2:] == (1, 14)


def test_text_nodes_keep_raw_escapes_and_plain_variant() -> None:
    (node,) = nodes("a\x1b[31mb\x1b]8;;x\x1b\\c\x1b")
    assert isinstance(node, Text)
    assert node.text == "a\x1b[31mb\x1b]8;;x\x1b\\c\x1b"  # '[' after ESC is not a tag
    assert node.plain == "abc"
    result = nodes("\x1b[1m[bold]x")
    assert isinstance(result[1], Tag)


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source", "line", "column", "reason", "hint"),
    [
        ("Hello [bold", 1, 7, "unclosed style tag '['", "\\["),
        ("a\n  [bold\n]", 2, 3, "unclosed style tag", "\\["),
        ("[{x}", 1, 1, "unclosed style tag", None),
        ("x {queries.total", 1, 3, "unclosed expression '{'", "\\{"),
        ("x {queries.total\n}", 1, 3, "unclosed expression", None),
        ("{x|number(2}", 1, 10, "unclosed '(' in filter arguments", "')'"),
        ("{x|number(2\n)}", 1, 10, "unclosed '('", None),
        ("{x|join(a b)}", 1, 11, "unexpected 'b' in filter arguments", None),
        ("{x|join(,)}", 1, 9, "expected an argument, found ','", "quote"),
        ("{x|join(", 1, 8, "unclosed '('", None),
        ("{x|join(a", 1, 8, "unclosed '('", None),
        ("{x|join(a=", 1, 11, "expected an argument, found 'end of line'", None),
        ("{queries.total|nubmer}", 1, 16, "unknown filter 'nubmer'", "did you mean 'number'?"),
        ("{x|zzzzzz}", 1, 4, "unknown filter 'zzzzzz'", "available filters: number,"),
        ("{x|}", 1, 4, "expected a filter name after '|', found '}'", None),
        ("{x|", 1, 4, "expected a filter name after '|', found 'end of line'", None),
        ("{x|9}", 1, 4, "unknown filter '9'", None),
        ("{x|round(1, 2)}", 1, 13, "too many arguments for filter 'round'", "usage: round(n=0)"),
        (
            "{x|truncate}",
            1,
            4,
            "filter 'truncate' needs argument 'n'",
            'usage: truncate(n, end="…")',
        ),
        ("{x|number(dec=2)}", 1, 15, "has no argument 'dec'", "usage: number(decimals=0)"),
        ('{x|number(decimals="a")}', 1, 20, "must be a whole number", None),
        ("{x|number(a=1, a=2)}", 1, 16, "argument 'a' given twice", None),
        ("{x|join(sep=a, b)}", 1, 16, "positional argument after keyword", None),
        ("{x|bytes(KB)}", 1, 10, "filter 'bytes': unknown unit 'KB'", "did you mean 'KiB'"),
        ("{x|duration(medium)}", 1, 13, "unknown style 'medium'", None),
        ("{x|ljust(5, ab)}", 1, 13, "single one-column character", None),
        ("{x|map(1)}", 1, 8, "only takes key=value", None),
        ("{x|map}", 1, 4, "at least one key=value", None),
        ("{}", 1, 1, "empty expression '{}'", "\\{\\}"),
        ("{  }", 1, 1, "empty expression", None),
        ("{", 1, 1, "unclosed expression", None),
        ("{ |upper}", 1, 3, "expected a variable path", None),
        ("{:>4}", 1, 2, "expected a variable path", None),
        ("{queries total}", 1, 10, "unexpected 't' in expression", "cannot contain spaces"),
        ("{x y}", 1, 4, "unexpected 'y'", None),
        ('{"a" b}', 1, 6, "unexpected 'b'", None),
        ("{a..b}", 1, 4, "empty key in variable path 'a..b'", None),
        ("{a.}", 1, 4, "empty key", None),
        ("{.a}", 1, 2, "empty key", None),
        ("{a.b(c)}", 1, 5, "invalid character '(' in variable path", "letters, digits"),
        ("{@good}", 1, 2, "invalid character '@'", None),
        ('{"abc}', 1, 2, "unclosed string", None),
        ("{'abc\n'}", 1, 2, "unclosed string", None),
        ("{x:zz}", 1, 4, "invalid format spec 'zz'", "format-specification"),
        ("{x:99999}", 1, 4, "too large", None),
        ("{x:.2f", 1, 1, "unclosed expression", None),
        ("{# never closed", 1, 1, "unclosed comment '{#'", "'#}'"),
        ("a\n{# a\nb", 2, 1, "unclosed comment", None),
        (
            "{# a\nb #}\n[steelblu]",
            3,
            2,
            "unknown style word 'steelblu'",
            "did you mean 'steelblue'",
        ),
        ("x\n  [#12345]", 2, 4, "invalid hex color '#12345'", "#rgb"),
        ("[bold on]", 1, 7, "expected a color after 'on'", "white on navy"),
        ("[on bold]", 1, 5, "expected a color after 'on', got 'bold'", None),
        ("[@good on]", 1, 8, "expected a color after 'on'", None),
        ("[on @good]", 1, 2, "expected a color after 'on'", None),
        ("[on bold {x}]", 1, 2, "expected a color after 'on'", None),
        ("[{x} on]", 1, 6, "expected a color after 'on'", None),
        ("[rgb(1,2)]", 1, 2, "three components", None),
        ("[color(300)]", 1, 2, "out of range", None),
        ("[@]", 1, 2, "missing theme style name", None),
        ("[@go!od]", 1, 5, "invalid character '!' in theme style", None),
        ("[_]", 1, 2, "missing underline color", None),
        ("[:]", 1, 2, "missing background color", None),
        ("[45]", 1, 2, "unknown style word '45'", "\\["),
        ("[blod]", 1, 2, "unknown style word 'blod'", "did you mean 'bold'?"),
        ("[bold {x} blod]", 1, 11, "unknown style word 'blod'", None),
    ],
)
def test_parse_errors(source: str, line: int, column: int, reason: str, hint: str | None) -> None:
    with pytest.raises(TemplateError) as info:
        Template.parse(source, name="greeting.txt")
    error = info.value
    assert reason in error.reason
    assert (error.line, error.column) == (line, column)
    assert error.source_name == "greeting.txt"
    assert str(error).startswith(f"greeting.txt:{line}:{column}: ")
    assert error.exit_code == EXIT_USAGE
    if hint is not None:
        assert error.hint is not None
        assert hint in error.hint


def test_error_excerpt() -> None:
    with pytest.raises(TemplateError) as info:
        Template.parse("line one\nBlocked {queries.blocked|nubmer}")
    excerpt = info.value.excerpt
    assert excerpt == "2 | Blocked {queries.blocked|nubmer}\n  |                          ^"


def test_default_source_name() -> None:
    with pytest.raises(TemplateError, match=r"^<template>:1:1: "):
        Template.parse("{")
