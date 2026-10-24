"""Style words → Style objects → SGR sequences."""

from __future__ import annotations

import pytest

from pihello.template.color import Color, ColorDepth
from pihello.template.style import (
    ATTRIBUTES,
    EMPTY,
    RESET,
    Style,
    StyleError,
    build_style,
    parse_style,
    split_words,
    validate_word,
)

T, C256, C16, NONE = (
    ColorDepth.TRUECOLOR,
    ColorDepth.ANSI256,
    ColorDepth.ANSI16,
    ColorDepth.NONE,
)


@pytest.mark.parametrize(
    ("word", "code"),
    [
        ("bold", "1"),
        ("dim", "2"),
        ("faint", "2"),
        ("italic", "3"),
        ("underline", "4"),
        ("blink", "5"),
        ("fblink", "6"),
        ("reverse", "7"),
        ("hidden", "8"),
        ("strike", "9"),
        ("dunderline", "21"),
        ("overline", "53"),
    ],
)
def test_attribute_codes(word: str, code: str) -> None:
    assert parse_style(word).sgr(T) == f"\x1b[0;{code}m"
    assert parse_style(word.upper()).sgr(C16) == f"\x1b[0;{code}m"
    assert word in ATTRIBUTES


@pytest.mark.parametrize(
    ("text", "sgr"),
    [
        ("bold steelblue", "\x1b[0;1;38;5;67m"),
        ("steelblue bold", "\x1b[0;1;38;5;67m"),  # attributes first, any word order
        ("bold turquoise2", "\x1b[0;1;38;5;45m"),
        ("white on navy", "\x1b[0;97;44m"),
        ("white :navy", "\x1b[0;97;44m"),
        ("on navy", "\x1b[0;44m"),
        ("underline _red", "\x1b[0;4;58;5;9m"),
        (
            "italic #ff8800 on rgb(0, 0, 95) _#00ff00",
            "\x1b[0;3;38;2;255;136;0;48;2;0;0;95;58;2;0;255;0m",
        ),
        ("red blue", "\x1b[0;94m"),  # later color wins
        ("bold normal red", "\x1b[0;91m"),  # normal clears what came before
        ("bold reset", ""),
        ("default", ""),
        ("red default", ""),
        ("", ""),
        ("normal", ""),
        ("bold bold", "\x1b[0;1m"),
        ("dim faint", "\x1b[0;2m"),
        ("color(45) :color(200)", "\x1b[0;38;5;45;48;5;200m"),
        ("on default", ""),
    ],
)
def test_style_sgr_truecolor(text: str, sgr: str) -> None:
    assert parse_style(text).sgr(T) == sgr


def test_depths() -> None:
    style = parse_style("bold #ff8800 on steelblue _red")
    assert style.sgr(T) == "\x1b[0;1;38;2;255;136;0;48;5;67;58;5;9m"
    assert style.sgr(C256) == "\x1b[0;1;38;5;208;48;5;67;58;5;9m"
    assert style.sgr(C16) == "\x1b[0;1;93;44m"  # underline color dropped
    assert style.sgr(NONE) == ""
    assert style.codes(NONE) == []
    # memoised
    assert style.sgr(C16) is style.sgr(C16)


def test_only_underline_color_at_16_colors_is_plain() -> None:
    assert parse_style("_red").sgr(C16) == ""
    assert parse_style("_red").sgr(C256) == "\x1b[0;58;5;9m"


def test_style_value_semantics() -> None:
    assert parse_style("bold red") == parse_style("red bold")
    assert parse_style("bold red") == Style(1, Color(9))
    assert parse_style("bold red") != parse_style("bold")
    assert hash(parse_style("bold red")) == hash(Style(1, Color(9)))
    assert parse_style("") is EMPTY
    assert EMPTY.is_plain
    assert not parse_style("bold").is_plain
    assert parse_style("default on default") is EMPTY
    assert Style(fg=Color()).is_plain  # default colors normalise to None
    assert Style() != "bold"
    assert str(parse_style("bold dim red on navy _#ff0000")) == "bold dim red :navy _#ff0000"
    assert str(EMPTY) == "normal"
    assert repr(parse_style("bold")) == "Style(bold)"
    with pytest.raises(AttributeError):
        EMPTY.attrs = 1
    assert RESET == "\x1b[0m"


def test_build_style_on_top_of_base() -> None:
    base = parse_style("bold red")
    assert build_style([], base) is base
    assert build_style(["bold"], base) is base
    assert build_style(["blue"], base) == parse_style("bold blue")
    assert build_style(["normal"], base) is EMPTY


@pytest.mark.parametrize(
    ("words", "index", "message"),
    [
        (["bold", "steelblu"], 1, "unknown style word 'steelblu'"),
        (["on"], 0, "expected a color after 'on'"),
        (["on", "bold"], 1, "expected a color after 'on', got 'bold'"),
        (["on", ":red"], 1, "expected a color after 'on'"),
        ([":"], 0, "missing background color"),
        (["_"], 0, "missing underline color"),
        (["#12"], 0, "invalid hex color"),
        ([":nope"], 0, "unknown color 'nope'"),
        (["rgb(1,2)"], 0, "three components"),
    ],
)
def test_build_style_errors(words: list[str], index: int, message: str) -> None:
    with pytest.raises(StyleError) as info:
        build_style(words)
    assert info.value.index == index
    assert message in info.value.message


def test_unknown_word_hints() -> None:
    with pytest.raises(StyleError) as info:
        validate_word("blod")
    assert info.value.hint == "did you mean 'bold'?"
    with pytest.raises(StyleError) as info:
        validate_word("xyzzy")
    assert info.value.hint is not None
    assert "\\[" in info.value.hint


def test_parse_style_rejects_theme_references() -> None:
    with pytest.raises(StyleError, match="not allowed") as info:
        parse_style("bold @good")
    assert info.value.index == 1


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("bold red", ["bold", "red"]),
        ("  bold\tred  ", ["bold", "red"]),
        ("rgb(1, 2, 3) bold", ["rgb(1, 2, 3)", "bold"]),
        ("on rgb( 1 ,2 , 3 )", ["on", "rgb( 1 ,2 , 3 )"]),
        ("a) b", ["a)", "b"]),
        ("  rgb(1,2,3)  bold ", ["rgb(1,2,3)", "bold"]),
        ("", []),
    ],
)
def test_split_words(text: str, words: list[str]) -> None:
    assert split_words(text) == words


def test_word_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from pihello.template import style as style_module

    monkeypatch.setattr(style_module, "_WORD_CACHE_LIMIT", 2)
    style_module._WORD_CACHE.clear()
    for word in ("red", "blue", "green", "bold"):
        validate_word(word)
    assert len(style_module._WORD_CACHE) <= 2
