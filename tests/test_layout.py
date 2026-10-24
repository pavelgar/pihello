"""Visible width, tabs, wrapping, clipping and serialisation."""

from __future__ import annotations

import pytest

from pihello.template.layout import (
    char_width,
    clip_runs,
    clip_text,
    expand_tabs,
    layout,
    pad,
    serialize,
    strip_ansi,
    truncate,
    visible_width,
    wrap_runs,
)

RED = "\x1b[0;91m"
BLUE = "\x1b[0;94m"
R = "\x1b[0m"


# --------------------------------------------------------------------------- #
# strip_ansi / widths                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "plain"),
    [
        ("plain", "plain"),
        ("\x1b[0;1;38;5;45mhi\x1b[0m", "hi"),
        ("\x1b[38;2;1;2;3mx", "x"),
        ("a\x1b]8;;https://pi.hole\x1b\\link\x1b]8;;\x1b\\b", "alinkb"),  # OSC 8 hyperlink
        ("a\x1b]0;title\x07b", "ab"),  # OSC with BEL
        ("a\x1bPdata\x1b\\b", "ab"),  # DCS
        ("a\x1b(Bb", "ab"),  # charset designation (nF)
        ("a\x1bMb", "ab"),  # two-character escape
        ("lone\x1b", "lone"),
        ("a\x1b[31", "a31"),  # unterminated CSI: only the introducer is dropped
    ],
)
def test_strip_ansi(text: str, plain: str) -> None:
    assert strip_ansi(text) == plain
    assert "\x1b" not in strip_ansi(text)


@pytest.mark.parametrize(
    ("char", "width"),
    [
        ("a", 1),
        (" ", 1),
        ("\t", 0),
        ("\x00", 0),
        ("\x7f", 0),
        ("\x9b", 0),
        ("é", 1),
        ("\u00ad", 1),  # soft hyphen
        ("\u0301", 0),  # combining acute accent
        ("\u20dd", 0),  # combining enclosing circle (Me)
        ("\u200b", 0),  # zero width space
        ("\u200d", 0),  # zero width joiner
        ("\ufe0f", 0),  # variation selector
        ("\u1160", 0),  # hangul medial vowel
        ("─", 1),
        ("█", 1),
        ("▁", 1),
        ("░", 1),
        ("…", 1),
        ("→", 1),
        ("日", 2),
        ("한", 2),
        ("\uff21", 2),  # fullwidth A
        ("😀", 2),
        ("\u2028", 0),  # line separator
    ],
)
def test_char_width(char: str, width: int) -> None:
    assert char_width(char) == width
    assert char_width(char) == width  # cached path


@pytest.mark.parametrize(
    ("text", "width"),
    [
        ("", 0),
        ("hello", 5),
        ("\x1b[0;1mhello\x1b[0m", 5),
        ("日本語", 6),
        ("e\u0301", 1),
        ("👍🏽", 4),  # emoji + skin tone modifier (both wide in Unicode data)
        ("─" * 40, 40),
        ("a\tb", 2),
        ("a\x1b[31mb", 2),
        ("ab\ncdef\n", 4),
        ("\x1b[31m日本\x1b[0m", 4),
        ("ctrl\x01", 4),
    ],
)
def test_visible_width(text: str, width: int) -> None:
    assert visible_width(text) == width


# --------------------------------------------------------------------------- #
# Text helpers                                                                 #
# --------------------------------------------------------------------------- #
def test_clip_text() -> None:
    assert clip_text("hello", 3) == "hel"
    assert clip_text("hello", 9) == "hello"
    assert clip_text("hello", 0) == ""
    assert clip_text("hello", -1) == ""
    assert clip_text("日本語", 3) == "日"  # never splits a wide character
    assert clip_text("e\u0301x", 1) == "e\u0301"  # keeps combining marks
    assert clip_text("\x1b[31mabc", 2) == "\x1b[31mab"


@pytest.mark.parametrize(
    ("text", "width", "end", "result"),
    [
        ("hello world", 8, "…", "hello w…"),
        ("hello", 5, "…", "hello"),
        ("hello", 10, "…", "hello"),
        ("hello", 1, "…", "…"),
        ("hello", 0, "…", ""),
        ("hello", -3, "…", ""),
        ("hello world", 8, "...", "hello..."),
        ("hello", 2, "...", "he"),  # end wider than the limit
        ("hello", 3, "", "hel"),
        ("日本語テキスト", 7, "…", "日本語…"),
    ],
)
def test_truncate(text: str, width: int, end: str, result: str) -> None:
    assert truncate(text, width, end) == result
    assert visible_width(truncate(text, width, end)) <= max(width, 0)


def test_pad() -> None:
    assert pad("ab", 5) == "ab   "
    assert pad("ab", 5, ".", ">") == "...ab"
    assert pad("ab", 5, "-", "^") == "-ab--"
    assert pad("ab", 6, "-", "^") == "--ab--"
    assert pad("日本", 6, ".") == "日本.."
    assert pad("toolong", 3) == "toolong"
    assert pad("ab", -1) == "ab"
    assert pad("ab", 5, "") == "ab"


# --------------------------------------------------------------------------- #
# Tabs                                                                         #
# --------------------------------------------------------------------------- #
def test_expand_tabs() -> None:
    assert expand_tabs([("", "a\tb")], 4) == [("", "a   b")]
    assert expand_tabs([("", "\tx")], 4) == [("", "    x")]
    assert expand_tabs([("", "abcd\tx")], 4) == [("", "abcd    x")]
    assert expand_tabs([("", "a\t\tb")], 2) == [("", "a   b")]
    assert expand_tabs([("", "a\tb")], 0) == [("", "ab")]
    assert expand_tabs([("", "a\tb")], 1) == [("", "a b")]
    # columns count across runs and ignore escapes; wide characters count 2
    assert expand_tabs([(RED, "ab"), (BLUE, "\tc")], 4) == [(RED, "ab"), (BLUE, "  c")]
    assert expand_tabs([("", "日\tx")], 4) == [("", "日  x")]
    assert expand_tabs([("", "\x1b[1mab\x1b[0m\tx")], 4) == [("", "\x1b[1mab\x1b[0m  x")]


def test_huge_tab_size_is_bounded() -> None:
    # Regression: `-i 10000000000` passed validation and then tried to allocate
    # gigabytes of spaces per tab (MemoryError, an "internal error").
    ((_, text),) = expand_tabs([("", "a\tb")], 10**12)
    assert text.startswith("a ")
    assert text.endswith(" b")
    assert len(text) <= 10_002


# --------------------------------------------------------------------------- #
# Wrapping                                                                     #
# --------------------------------------------------------------------------- #
def _plain(lines: list[list[tuple[str, str]]]) -> list[str]:
    return ["".join(text for _, text in runs) for runs in lines]


@pytest.mark.parametrize(
    ("text", "width", "expected"),
    [
        ("hello world", 20, ["hello world"]),
        ("hello world", 11, ["hello world"]),
        ("hello world", 10, ["hello", "world"]),
        ("hello world", 5, ["hello", "world"]),
        ("hello world foo", 10, ["hello", "world foo"]),
        ("hello   world", 7, ["hello", "world"]),  # whitespace at the break is dropped
        ("abcdefghij", 4, ["abcd", "efgh", "ij"]),  # hard break
        ("ab cdefghij", 4, ["ab", "cdef", "ghij"]),
        ("    indented text", 10, ["    indent", "ed text"]),  # leading whitespace kept
        ("a b", 1, ["a", "b"]),
        ("日本語", 4, ["日本", "語"]),  # wide characters never split
        ("ab日", 3, ["ab", "日"]),
        ("日", 1, ["日"]),  # wider than the line: placed alone
        ("e\u0301e\u0301e\u0301", 2, ["e\u0301e\u0301", "e\u0301"]),
        ("", 5, [""]),
        ("word ", 4, ["word"]),
        ("x" * 3 + " " + "y" * 9, 4, ["xxx", "yyyy", "yyyy", "y"]),
        ("a bcde日", 4, ["a", "bcde", "日"]),
        (" 0", 1, ["0"]),  # whitespace that cannot fit with the next word is dropped
        ("      word", 5, ["word"]),
        ("  ab", 3, ["  a", "b"]),
    ],
)
def test_wrap(text: str, width: int, expected: list[str]) -> None:
    lines = wrap_runs([("", text)], width)
    assert _plain(lines) == expected


def test_wrap_never_breaks_at_no_break_spaces() -> None:
    # Regression: str.isspace() is true for U+00A0, so lines were wrapped at no-break spaces.
    for nbsp in ("\xa0", "\u2007", "\u202f"):
        lines = wrap_runs([("", f"aaa{nbsp}bbb ccc")], 8)
        assert _plain(lines) == [f"aaa{nbsp}bbb", "ccc"]
        assert _plain(wrap_runs([("", f"aaa{nbsp}bbb")], 4)) == [f"aaa{nbsp}", "bbb"]


def test_wrap_keeps_styles_per_run() -> None:
    runs = [(RED, "hello "), (BLUE, "big"), ("", " world")]
    lines = wrap_runs(runs, 9)
    assert lines == [[(RED, "hello "), (BLUE, "big")], [("", "world")]]
    lines = wrap_runs([(RED, "aaaa"), (BLUE, "bbbb")], 3)
    assert lines == [[(RED, "aaa")], [(RED, "a"), (BLUE, "bb")], [(BLUE, "bb")]]


def test_wrap_treats_escape_sequences_as_zero_width() -> None:
    lines = wrap_runs([("", "ab\x1b[31mcd\x1b[0m ef")], 4)
    assert _plain(lines) == ["ab\x1b[31mcd\x1b[0m", "ef"]
    # trailing zero-width cells never create a line of their own
    assert _plain(wrap_runs([("", "a \x1b[1m")], 1)) == ["a\x1b[1m"]
    assert _plain(wrap_runs([("", "a \x1b[1m b")], 2)) == ["a\x1b[1m", "b"]


def test_clip_runs() -> None:
    assert clip_runs([(RED, "hello"), (BLUE, "world")], 7) == [(RED, "hello"), (BLUE, "wo")]
    assert clip_runs([(RED, "hello")], 5) == [(RED, "hello")]
    assert clip_runs([(RED, "日本"), (BLUE, "x")], 3) == [(RED, "日")]


# --------------------------------------------------------------------------- #
# layout + serialize                                                           #
# --------------------------------------------------------------------------- #
def test_layout() -> None:
    lines = [[("", "one two three")], [("", "\tx")], [("", "four")]]
    assert _plain(layout(lines)) == ["one two three", "    x", "four"]
    assert _plain(layout(lines, width=7)) == ["one two", "three", "    x", "four"]
    assert _plain(layout(lines, width=7, overflow="clip")) == ["one two", "    x", "four"]
    assert _plain(layout(lines, width=7, height=2)) == ["one two", "three"]
    assert _plain(layout(lines, height=2)) == ["one two three", "    x"]
    assert _plain(layout(lines, height=10)) == ["one two three", "    x", "four"]
    assert _plain(layout(lines, tab_size=2)) == ["one two three", "  x", "four"]


def test_serialize_minimal_sequences() -> None:
    lines = [
        [("", "plain "), (RED, "red"), (RED, " more"), ("", " plain")],
        [(RED, "")],
        [(RED, "a"), (BLUE, ""), (BLUE, "b")],
        [("", "end")],
    ]
    assert serialize(lines) == (
        f"plain {RED}red more{R} plain\n"  # reset only when switching to plain
        "\n"  # no text → no escapes at all
        f"{RED}a{BLUE}b{R}\n"
        "end"
    )
    assert serialize([[("", "no escapes")]]) == "no escapes"
    assert serialize([]) == ""
