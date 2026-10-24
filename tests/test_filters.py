"""Every filter, its edge cases, argument binding and the catalogue."""

from __future__ import annotations

import time
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pihello.errors import TemplateError
from pihello.template import ColorDepth, RenderOptions, Template, visible_width
from pihello.template import filters as F  # noqa: N812 - short alias for the module under test
from pihello.template.filters import FILTERS, Arg, FilterArgError, bind, catalog, to_number, to_text

PLAIN = RenderOptions(color=ColorDepth.NONE, now=1_000_000.0)


def ev(expression: str, value: Any = None, **variables: Any) -> str:
    """Render ``{x|<expression>}`` with ``x = value``."""
    template = Template.parse("{x" + expression + "}")
    return template.render({"x": value, **variables}, options=PLAIN)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("s", "s"),
        (None, None),
        (True, "yes"),
        (False, "no"),
        (3, "3"),
        (15.27, "15.3"),
        (2.0, "2.0"),
        ([1, "a", None, 2.5], "1, a, 2.5"),
        ((1, 2), "1, 2"),
        ({"a": 1}, None),
        (b"x", "b'x'"),
    ],
)
def test_to_text(value: Any, text: str | None) -> None:
    assert to_text(value) == text


class _FloatLike(float):
    pass


def test_to_text_float_subclass_and_mapping_like() -> None:
    assert to_text(_FloatLike(1.25)) == "1.2"

    class MappingLike:
        def keys(self) -> list[str]:
            return []

    assert to_text(MappingLike()) is None


@pytest.mark.parametrize(
    ("value", "number"),
    [
        (1, 1),
        (1.5, 1.5),
        (True, 1),
        ("42", 42),
        (" 4.5 ", 4.5),
        ("abc", None),
        ("", None),
        (None, None),
        ([1], None),
        ("1" * 500, None),
        (_FloatLike(2.5), 2.5),
    ],
)
def test_to_number(value: Any, number: float | None) -> None:
    assert to_number(value) == number


# --------------------------------------------------------------------------- #
# Numbers                                                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("expression", "value", "result"),
    [
        ("|number", 48213, "48,213"),
        ("|number", 1234567890, "1,234,567,890"),
        ("|number", -48213, "-48,213"),
        ("|number", 48213.5, "48,214"),
        ("|number(2)", 48213, "48,213.00"),
        ("|number(decimals=1)", 1234.56, "1,234.6"),
        ("|number(-2)", 5, "5"),
        ("|number", "48213", "48,213"),
        ("|number", "nope", "?"),
        ("|number", float("inf"), "inf"),
        ("|number", True, "1"),
        ("|round", 15.27, "15"),
        ("|round", 2.5, "3"),
        ("|round", -2.5, "-3"),
        ("|round", 0.49999999999999994, "0"),
        ("|round(1)", 15.27, "15.3"),
        ("|round(2)", 0.125, "0.13"),
        ("|round(-2)", 1234.5, "1200"),
        ("|round(-2)", 1250, "1300"),
        ("|round(-1)", -15, "-20"),
        ("|round(3)", 7, "7"),
        ("|round(40)", 1.5, "1.500000000000000"),
        ("|round", 1e300, str(int(1e300))),
        ("|round", "x", "?"),
        ("|round", float("nan"), "nan"),
        ("|int", 15.9, "15"),
        ("|int", -15.9, "-15"),
        ("|int", "12.7", "12"),
        ("|int", "abc", "?"),
        ("|int", float("inf"), "?"),
        ("|percent", 15.27, "15.3%"),
        ("|percent(0)", 15.5, "16%"),
        ("|percent(2)", 7, "7.00%"),
        ("|percent", "x", "?"),
        ("|percent", float("nan"), "nan%"),
        ("|compact", 950, "950"),
        ("|compact", 950.5, "950.5"),
        ("|compact", 999.99, "1.0k"),
        ("|compact", 1000, "1.0k"),
        ("|compact", 48213, "48.2k"),
        ("|compact", 999950, "1.0M"),
        ("|compact", 1_200_000, "1.2M"),
        ("|compact", 3_400_000_000, "3.4B"),
        ("|compact", 5e12, "5.0T"),
        ("|compact", 5e18, "5000000.0T"),
        ("|compact", -48213, "-48.2k"),
        ("|compact", -5, "-5"),
        ("|compact", 0, "0"),
        ("|compact(0)", 48213, "48k"),
        ("|compact(2)", 1234, "1.23k"),
        ("|compact", "x", "?"),
        ("|compact", float("-inf"), "-inf"),
        ("|bytes", 0, "0 B"),
        ("|bytes", 512, "512 B"),
        ("|bytes", 512.5, "512.5 B"),
        ("|bytes", 1536, "1.5 KiB"),
        ("|bytes", 1048575, "1.0 MiB"),
        ("|bytes(KiB)", 32_400_000, "30.9 GiB"),
        ('|bytes("MiB", 2)', 1536, "1.50 GiB"),
        ("|bytes(unit=gib)", 2048, "2.0 TiB"),
        ("|bytes", -2048, "-2.0 KiB"),
        ("|bytes", 1024**7, "1024.0 EiB"),
        ("|bytes", "x", "?"),
    ],
)
def test_numeric_filters(expression: str, value: Any, result: str) -> None:
    assert ev(expression, value) == result


def test_number_after_round_formats_like_a_float() -> None:
    # Regression: round(n) returns a float subclass, which `number` treated as an int.
    assert ev("|round(1)|number", 1234.56) == ev("|number", 1234.6) == "1,235"
    assert ev("|round(1)|number(2)", 1234.56) == "1,234.60"
    assert ev("|round(1)|number", 1e16) == "10,000,000,000,000,000"


def test_round_returns_numbers() -> None:
    assert F.f_round(15.27, 0) == 15
    assert isinstance(F.f_round(15.27, 0), int)
    assert F.f_round(15.27, 1) == 15.3
    rounded = F.f_round(2, 3)
    assert rounded == 2
    rounded = F.f_round(0.5, 3)
    assert isinstance(rounded, F.RoundedFloat)
    assert to_text(rounded) == "0.500"
    assert ev("|round(2)|gt(0.1)", 0.125) == "yes"  # still a number
    assert ev("|round(2):>6", 0.125) == "  0.13"
    assert F.f_round(None, 0) is None
    assert F.f_round(float("inf"), 0) == float("inf")


# --------------------------------------------------------------------------- #
# Time                                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("expression", "value", "result"),
    [
        ("|duration", 0, "0s"),
        ("|duration", 0.7, "0s"),
        ("|duration", 59, "59s"),
        ("|duration", 61, "1m 1s"),
        ("|duration", 3600, "1h"),
        ("|duration", 3661, "1h 1m"),
        ("|duration", 183600, "2d 3h"),
        ("|duration", 86460, "1d"),  # next unit is zero → dropped
        ("|duration", 90061, "1d 1h"),
        ("|duration", -90, "-1m 30s"),
        ("|duration(long)", 0, "0 seconds"),
        ("|duration(long)", 1, "1 second"),
        ("|duration(long)", 183600, "2 days, 3 hours"),
        ('|duration("long")', 90061, "1 day, 1 hour"),
        ("|duration(style=long)", 120, "2 minutes"),
        ("|duration", "x", "?"),
        ("|duration", "90", "1m 30s"),
        ("|ago", 1_000_000 - 30, "just now"),
        ("|ago", 1_000_000 + 30, "just now"),
        ("|ago", 1_000_000 - 3 * 86400 - 4 * 3600, "3d 4h ago"),
        ("|ago", 1_000_000 + 300, "in 5m"),
        ("|ago", 1_000_000 - 60, "1m ago"),
        ("|ago", "x", "?"),
    ],
)
def test_time_filters(expression: str, value: Any, result: str) -> None:
    assert ev(expression, value) == result


def test_ago_defaults_to_current_time() -> None:
    assert F.f_ago(time.time() - 7200) == "2h ago"
    template = Template.parse("{x|ago}")
    assert (
        template.render({"x": time.time() - 7200}, options=RenderOptions(color=ColorDepth.NONE))
        == "2h ago"
    )


def test_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        assert ev("|datetime", 0) == "1970-01-01 00:00"
        assert ev('|datetime("%H:%M")', 3600 * 13 + 60 * 7) == "13:07"
        assert ev("|datetime(%H:%M)", 60) == "00:01"  # ':' inside parentheses belongs to the args
        assert ev("|datetime", "x") == "?"
        assert ev("|datetime", 1e20) == "?"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


# --------------------------------------------------------------------------- #
# Text                                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("expression", "value", "result"),
    [
        ("|upper", "enabled", "ENABLED"),
        ("|lower", "ENABLED", "enabled"),
        ("|title", "pi hole", "Pi Hole"),
        ("|capitalize", "pi HOLE", "Pi hole"),
        ("|upper", True, "YES"),
        ("|upper", 1.25, "1.2"),
        ("|upper", {"a": 1}, "?"),
        ("|truncate(8)", "doubleclick.net", "doublec…"),
        ('|truncate(8, "...")', "doubleclick.net", "doubl..."),
        ('|truncate(8, end="")', "doubleclick.net", "doublecl"),
        ("|truncate(20)", "short", "short"),
        ("|truncate(0)", "short", ""),
        ("|truncate(-5)", "short", ""),
        ("|ljust(6)", "ab", "ab    "),
        ('|ljust(6, ".")', "ab", "ab...."),
        ("|ljust(6, fill=-)", "ab", "ab----"),
        ("|rjust(6)", 42, "    42"),
        ("|rjust(6, 0)", 42, "000042"),
        ("|center(6, *)", "ab", "**ab**"),
        ("|center(1)", "ab", "ab"),
        ("|ljust(-3)", "ab", "ab"),
        ("|ljust(4)", "日本", "日本"),
        ("|ljust(5)", "日本", "日本 "),
        ("|replace(-, _)", "a-b-c", "a_b_c"),
        ('|replace("a", "")', "banana", "bnn"),
        ("|replace(1, 2)", 101, "202"),
        ("|repeat(3)", "ab", "ababab"),
        ("|repeat(0)", "ab", ""),
        ("|repeat(-1)", "ab", ""),
        ("|repeat(3)", "", ""),
        ("|repeat(3)", {"a": 1}, "?"),
        ('|prefix(" for ")', "5m", " for 5m"),
        ('|prefix(" for ")', "", ""),
        ("|suffix(%)", 15, "15%"),
        ("|suffix(%)", "", ""),
        ("|default(none)|suffix(x)", None, "?"),
        ('|default("n/a")', None, "n/a"),
        ('|default("n/a")', "", "n/a"),
        ("|default(0)", 0, "0"),
        ("|default(fallback)", "value", "value"),
    ],
)
def test_text_filters(expression: str, value: Any, result: str) -> None:
    assert ev(expression, value) == result


def test_repeat_is_capped() -> None:
    assert len(ev("|repeat(100000000)", "ab")) == 100_000
    assert Template.parse('{"─"|repeat(5)}').render({}) == "─────"


def test_prefix_passthrough_and_none() -> None:
    assert F.f_prefix(None, "x") is None
    assert F.f_suffix([], "x") == []


# --------------------------------------------------------------------------- #
# Lists                                                                        #
# --------------------------------------------------------------------------- #
HISTORY = [
    {"timestamp": 1, "total": 10, "nested": {"v": 1}},
    {"timestamp": 2, "total": 30, "nested": {"v": 2}},
    {"timestamp": 3, "total": 20},
    "not a dict",
]


@pytest.mark.parametrize(
    ("expression", "value", "result"),
    [
        ("|join", ["a", "b"], "a, b"),
        ("|join( / )", ["a", "b"], "a/b"),  # bareword arguments are stripped
        ('|join(" / ")', ["a", "b"], "a / b"),
        ('|join("")', [1, None, 2], "12"),
        ("|join", "abc", "abc"),
        ("|first", ["a", "b"], "a"),
        ("|first", [], "?"),
        ("|first", "abc", "a"),
        ("|first", 5, "5"),
        ("|first", {"a": 1}, "?"),
        ("|last", ["a", "b"], "b"),
        ("|last", "", "?"),
        ("|last", 5, "5"),
        ("|length", [1, 2, 3], "3"),
        ("|length", "abcd", "4"),
        ("|length", {"a": 1}, "1"),
        ("|length", 5, "?"),
        ("|take(2)", [1, 2, 3], "1, 2"),
        ("|take(-1)", [1, 2, 3], ""),
        ("|take(2)", "abc", "ab"),
        ("|take(2)", 5, "?"),
        ("|slice(1)", [1, 2, 3], "2, 3"),
        ("|slice(0, 2)", [1, 2, 3], "1, 2"),
        ("|slice(-2)", [1, 2, 3], "2, 3"),
        ("|slice(0, none)", [1, 2, 3], "1, 2, 3"),
        ("|slice(1, 3)", "abcd", "bc"),
        ("|slice(1)", 5, "?"),
        ("|pluck(total)", HISTORY, "10, 30, 20"),
        ("|pluck(nested.v)", HISTORY, "1, 2"),
        ("|pluck(total)|sum", HISTORY, "60"),
        ("|pluck(total)", "x", "?"),
        ("|pluck(0)", [[1, 2], [3], [], "ab", {"0": 9}], "1, 3, 9"),
        ("|pluck(-1)", [[1, 2], [3]], "2, 3"),
        ("|sum", [1, 2, "3", "x", None, float("nan")], "6"),
        ("|sum", [], "0"),
        ("|sum", [1.5, 2], "3.5"),
        ("|sum", 5, "5"),
        ("|sum", {"a": 1}, "?"),
        ("|min", [3, 1, 2], "1"),
        ("|min", [], "?"),
        ("|max", [3, 1, 2], "3"),
        ("|max", ["x"], "?"),
        ("|avg", [1, 2], "1.5"),
        ("|avg", [], "?"),
        ("|avg", [1e308, 1e308], "?"),
        ("|avg", 4, "4.0"),
    ],
)
def test_list_filters(expression: str, value: Any, result: str) -> None:
    assert ev(expression, value) == result


def test_avg_overflow_is_missing() -> None:
    assert F.f_avg([float("inf"), float("-inf")]) is None


def test_huge_integers_never_raise() -> None:
    # Regression: math.isnan() on an int beyond float range raised OverflowError,
    # so comparisons and aggregates failed (strict: "filter 'eq' failed").
    huge = 10**400
    assert F.f_eq("9" * 350, 0) is False
    assert F.f_gt(huge, 1) is True
    assert F.f_lt(1.5, huge) is True
    assert F.f_max([huge, 1]) == huge
    assert F.f_min([huge, 1.5]) == 1.5
    assert ev("|gt(0)", huge) == "yes"


def test_pluck_malformed_index_is_missing() -> None:
    # Regression: "--1" passed the digit check and int("--1") raised ValueError.
    assert F.f_pluck([[1, 2]], "--1") == [None]
    assert F.f_pluck([[1, 2]], "-1") == [2]
    assert F.f_pluck([{"a": [1, 2]}], "a.--1") == [None]


@pytest.mark.parametrize(
    ("expression", "value", "result"),
    [
        ("|spark", [1, 2, 3, 4, 5, 6, 7, 8], "▁▂▃▄▅▆▇█"),
        ("|spark", [0, 10], "▁█"),
        ("|spark", [5, 5, 5], "▄▄▄"),  # all equal and non-zero → middle
        ("|spark", [0, 0], "▁▁"),
        ("|spark", [], ""),
        ("|spark", [None, "x"], "  "),
        ("|spark", [1, None, 3], "▁ █"),
        ("|spark", [1, float("inf"), 3], "▁ █"),
        ("|spark", 7, "▄"),
        ("|spark", "abc", "?"),
        ("|spark(4)", [1, 1, 2, 2, 3, 3, 4, 4], "▁▃▆█"),  # downsample by averaging
        ("|spark(4)", [1, 2], "▁▁██"),  # upsample by repeating
        ("|spark(2)", [1, 2, 3, 4], "▁█"),
        ("|spark(2)", [None, None, 1, 2], " ▄"),
        ("|spark(-3)", [1, 2], "▁█"),
        ("|spark(3)", [], ""),
        # fixed scale bounds (values are clamped to [min, max])
        ("|spark(min=0, max=8)", [0, 4, 8], "▁▅█"),
        ("|spark(min=0, max=100)", [10, 50], "▂▅"),
        ("|spark(min=0, max=10)", [-5, 5, 20], "▁▅█"),
        ("|spark(0, 0, 70)", [7, 7], "▂▂"),  # equal values no longer default to the middle
        ("|spark(min=0)", [5, 10], "▅█"),  # max from the data
        ("|spark(max=10)", [5, 10], "▁█"),  # min from the data
        ("|spark(min=100)", [5, 10], "▁▁"),  # all data below the fixed minimum
        ("|spark(max=1)", [5, 10], "██"),  # all data above the fixed maximum
        ("|spark(min=0)", [0, 0], "▁▁"),
        ("|spark(4, min=0, max=4)", [4, 4, 0, 0], "██▁▁"),
        ("|spark(min=0.5, max=1.5)", [1, None], "▅ "),
        ("|spark(min=none, max=none)", [1, 2], "▁█"),
    ],
)
def test_spark(expression: str, value: Any, result: str) -> None:
    assert ev(expression, value) == result


@pytest.mark.parametrize(
    ("expression", "value", "result"),
    [
        ("|bar(10)", 54, "█████▍░░░░"),
        ("|bar(10)", 0, "░░░░░░░░░░"),
        ("|bar(10)", 100, "██████████"),
        ("|bar(10)", 150, "██████████"),  # clamped
        ("|bar(10)", -5, "░░░░░░░░░░"),
        ("|bar(4, max=8)", 1, "▌░░░"),
        ("|bar(4, 1)", 0.5, "██░░"),
        ("|bar", 50, "██████████░░░░░░░░░░"),
        ("|bar(0)", 50, ""),
        ("|bar(-4)", 50, ""),
        ("|bar(4, max=0)", 50, "░░░░"),
        ("|bar(4)", "x", "?"),
        ("|bar(4)", float("nan"), "?"),
        # two-character bars: whole cells, rounded to the nearest cell
        ('|bar(20, chars="#-")', 25, "#####---------------"),
        ('|bar(10, chars="#-")', 54, "#####-----"),
        ('|bar(10, chars="#-")', 56, "######----"),
        ('|bar(4, 8, "=.")', 3, "==.."),
        ("|bar(5, chars=*_)", 100, "*****"),
        ("|bar(5, chars=*_)", 0, "_____"),
        ('|bar(5, chars="#-")', 500, "#####"),
        ('|bar(5, chars="#-")', -1, "-----"),
        ('|bar(0, chars="#-")', 50, ""),
        ('|bar(3, chars="█░")', 50, "██░"),
        ("|bar(4, chars=none)", 50, "██░░"),
    ],
)
def test_bar(expression: str, value: Any, result: str) -> None:
    assert ev(expression, value) == result


# --------------------------------------------------------------------------- #
# Logic                                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("expression", "value", "result"),
    [
        ("|eq(enabled)", "enabled", "yes"),
        ("|eq(enabled)", "disabled", "no"),
        ("|eq(10)", "10", "yes"),
        ('|eq("10")', 10, "yes"),
        ("|eq(10)", 10.0, "yes"),
        ("|eq(true)", True, "yes"),
        ("|eq(yes)", True, "yes"),
        ("|eq(false)", "false", "yes"),
        ("|eq(true)", "maybe", "no"),
        ("|eq(x)", None, "no"),
        ("|eq(x)", [1], "no"),
        ("|eq(none)", "x", "no"),
        ("|ne(enabled)", "enabled", "no"),
        ("|ne(enabled)", "disabled", "yes"),
        ("|ne(x)", None, "no"),  # missing → false
        ("|ne(true)", "maybe", "yes"),  # not comparable → different
        ("|gt(0)", 3, "yes"),
        ("|gt(0)", 0, "no"),
        ("|gt(0)", None, "no"),
        ("|gt(0)", float("nan"), "no"),
        ("|ge(3)", 3, "yes"),
        ("|lt(3)", 2.5, "yes"),
        ("|le(3)", 3, "yes"),
        ("|le(3)", 4, "no"),
        ("|gt(v6.9)", "v6.10", "yes"),  # natural ordering of versions
        ("|lt(v6.1.2)", "v6.1.10", "no"),
        ("|gt(abc)", "abd", "yes"),
        ("|lt(b)", "a1", "yes"),
        ("|gt(a)", "a1", "yes"),
        ("|gt(1a)", "1a1", "yes"),
        ("|not", True, "no"),
        ("|not", None, "yes"),
        ("|not", 0, "yes"),
        ("|not", "x", "no"),
        ("|not", float("nan"), "yes"),
        ("|not|not", [], "no"),
        ("|if(up, down)", True, "up"),
        ("|if(up, down)", False, "down"),
        ("|if(up, down)", None, "down"),
        ("|if(up)", 0, ""),
        ("|if(1, 0)", "x", "1"),
        ("|map(enabled=on, disabled=off)", "enabled", "on"),
        ("|map(enabled=on, disabled=off)", "failed", "failed"),
        ("|map(enabled=on, _=other)", "failed", "other"),
        ("|map(Enabled=on)", "enabled", "on"),  # case-insensitive fallback
        ("|map(true=up, false=down)", True, "up"),
        ("|map(yes=up)", True, "up"),
        ("|map(false=down)", False, "down"),
        ("|map(1=one)", 1, "one"),
        ("|map(1=one)", 1.0, "one"),
        ('|map("not found"=nf)', "not found", "nf"),
        ("|map(a=1)", None, "?"),  # map passes None through
        ('|map("1, 2"=pair)', [1, 2], "pair"),  # lists match their rendered text
    ],
)
def test_logic_filters(expression: str, value: Any, result: str) -> None:
    assert ev(expression, value) == result


def test_compare_helpers() -> None:
    assert F.f_eq("a", "a") is True
    assert F._compare({"a": 1}, "x") is None
    assert F._natural_key("v6.10") > F._natural_key("v6.9")


# --------------------------------------------------------------------------- #
# None passthrough                                                             #
# --------------------------------------------------------------------------- #
_NONE_AWARE = {"default", "if", "not", "eq", "ne", "gt", "ge", "lt", "le"}


@pytest.mark.parametrize("name", sorted(FILTERS))
def test_none_passthrough(name: str) -> None:
    spec = FILTERS[name]
    assert spec.accepts_none == (name in _NONE_AWARE)


# --------------------------------------------------------------------------- #
# Binding                                                                      #
# --------------------------------------------------------------------------- #
def _arg(value: Any, raw: str | None = None, *, quoted: bool = False) -> Arg:
    return Arg(value, raw if raw is not None else str(value), quoted=quoted, position=(1, 1))


def test_bind_defaults_and_keywords() -> None:
    assert bind(FILTERS["bytes"], [], {}) == ("B", 1)
    assert bind(FILTERS["bytes"], [_arg("KiB")], {"decimals": _arg(2)}) == ("KiB", 2)
    assert bind(FILTERS["slice"], [_arg(1)], {}) == (1, None)
    assert bind(FILTERS["slice"], [_arg(1), _arg(None, "none")], {}) == (1, None)
    assert bind(FILTERS["bar"], [], {"max": _arg(2.5)}) == (20, 2.5, None)
    assert bind(FILTERS["bar"], [], {"chars": _arg("#-", '"#-"', quoted=True)}) == (20, 100, "#-")
    assert bind(FILTERS["bar"], [], {"chars": _arg(None, "none")})[2] is None
    assert bind(FILTERS["spark"], [], {}) == (0, None, None)
    assert bind(FILTERS["spark"], [_arg(8), _arg(0)], {"max": _arg(1.5)}) == (8, 0, 1.5)
    assert bind(FILTERS["prefix"], [_arg(5, "05")], {}) == ("05",)  # raw text for strings
    assert bind(FILTERS["map"], [], {"a": _arg(1)}) == ({"a": 1},)
    assert bind(FILTERS["upper"], [], {}) == ()


@pytest.mark.parametrize(
    ("name", "args", "kwargs", "message"),
    [
        ("round", [_arg(1), _arg(2)], {}, "too many arguments"),
        ("upper", [_arg(1)], {}, "takes no arguments"),
        ("truncate", [], {}, "needs argument 'n'"),
        ("round", [_arg(1)], {"n": _arg(2)}, "got argument 'n' twice"),
        ("round", [], {"digits": _arg(2)}, "has no argument 'digits'"),
        ("round", [_arg("x", '"x"', quoted=True)], {}, "must be a whole number"),
        ("round", [_arg(1.5)], {}, "must be a whole number"),
        ("round", [_arg(True, "true")], {}, "must be a whole number"),
        ("slice", [_arg(1), _arg("x")], {}, "a whole number or none"),
        ("bar", [_arg(1)], {"max": _arg("x")}, "must be a number"),
        ("map", [_arg(1)], {}, "only takes key=value"),
        ("map", [], {}, "at least one"),
        ("bytes", [_arg("KB")], {}, "unknown unit 'KB'"),
        ("bytes", [], {"unit": _arg("XB")}, "unknown unit"),
        ("duration", [_arg("medium")], {}, "unknown style"),
        ("ljust", [_arg(3), _arg("ab")], {}, "single one-column character"),
        ("center", [_arg(3)], {"fill": _arg("日")}, "single one-column character"),
        ("replace", [_arg(""), _arg("x")], {}, "must not be empty"),
        ("bar", [_arg(10)], {"chars": _arg("#")}, "exactly two one-column characters"),
        ("bar", [_arg(10)], {"chars": _arg("#-+")}, "exactly two"),
        ("bar", [_arg(10)], {"chars": _arg("日-")}, "one-column"),
        ("bar", [_arg(10)], {"chars": _arg("\u0301-")}, "one-column"),
        ("bar", [_arg(10)], {"chars": _arg(5)}, "exactly two"),
        ("bar", [_arg(10), _arg(100), _arg("x", '"x"', quoted=True)], {}, "exactly two"),
        ("spark", [], {"min": _arg(5), "max": _arg(5)}, "min must be less than max"),
        ("spark", [_arg(0), _arg(10), _arg(1.5)], {}, "min must be less than max"),
        ("spark", [], {"min": _arg("x")}, "a number or none"),
        ("spark", [], {"max": _arg(True, "true")}, "a number or none"),
    ],
)
def test_bind_errors(name: str, args: list[Arg], kwargs: dict[str, Arg], message: str) -> None:
    with pytest.raises(FilterArgError) as info:
        bind(FILTERS[name], args, kwargs)
    assert message in info.value.message
    assert info.value.hint


def test_catalog_examples_match_the_demo_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every documented example renders (strictly) from ``--demo`` data as claimed.

    Regression: ``bytes`` claimed 30.9 GiB (the demo has 7.9 GiB), ``datetime``
    09:41 (the demo time is 08:30 UTC) and ``prefix`` printed a missing value.
    """
    from pihello import demo

    monkeypatch.setenv("TZ", "UTC")  # like the worked examples in docs/templates.md
    time.tzset()
    options = RenderOptions(color=ColorDepth.NONE, strict=True, now=demo.NOW)
    theme = {"good": "green", "bad": "red", "warn": "yellow", "muted": "grey50"}
    try:
        for info in catalog():
            example, arrow, claimed = info.example.partition(" → ")
            if not example:
                continue
            if not example.startswith(("{", "[")):
                example = f"{{{example}|{info.name}}}"
            for part in example.split(" or "):
                text = Template.parse(part).render(demo.variables(), theme=theme, options=options)
                if arrow:
                    assert text == claimed, info
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_datetime_format_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    def strftime(fmt: str, t: object = None) -> str:
        raise ValueError("Invalid format string")

    monkeypatch.setattr(time, "strftime", strftime)
    with pytest.raises(FilterArgError, match="invalid time format") as info:
        bind(FILTERS["datetime"], [_arg("%Q")], {})
    assert info.value.arg is not None


def test_bind_error_points_at_argument() -> None:
    bad = _arg("KB")
    with pytest.raises(FilterArgError) as info:
        bind(FILTERS["bytes"], [], {"unit": bad})
    assert info.value.arg is bad
    with pytest.raises(FilterArgError) as info:
        bind(FILTERS["round"], [], {"digits": bad})
    assert info.value.arg is bad
    assert info.value.hint == "usage: round(n=0)"


def test_parse_time_filter_errors() -> None:
    with pytest.raises(TemplateError) as info:
        Template.parse("x {a|bytes(KB)}")
    assert info.value.line == 1
    assert info.value.column == 12
    assert "KiB" in (info.value.hint or "")


# --------------------------------------------------------------------------- #
# Catalogue                                                                    #
# --------------------------------------------------------------------------- #
SPEC_FILTERS = [
    "number",
    "round",
    "int",
    "percent",
    "compact",
    "bytes",
    "duration",
    "ago",
    "datetime",
    "upper",
    "lower",
    "title",
    "capitalize",
    "truncate",
    "ljust",
    "rjust",
    "center",
    "replace",
    "repeat",
    "prefix",
    "suffix",
    "default",
    "join",
    "first",
    "last",
    "length",
    "take",
    "slice",
    "pluck",
    "sum",
    "min",
    "max",
    "avg",
    "spark",
    "bar",
    "eq",
    "ne",
    "gt",
    "ge",
    "lt",
    "le",
    "not",
    "if",
    "map",
]


def test_catalog_covers_spec() -> None:
    entries = catalog()
    names = [entry.name for entry in entries]
    assert names == SPEC_FILTERS
    by_name = {entry.name: entry for entry in entries}
    assert by_name["number"].signature == "number(decimals=0)"
    assert by_name["truncate"].signature == 'truncate(n, end="…")'
    assert by_name["bytes"].signature == 'bytes(unit="B", decimals=1)'
    assert by_name["slice"].signature == "slice(start, stop=none)"
    assert by_name["bar"].signature == "bar(width=20, max=100, chars=none)"
    assert by_name["spark"].signature == "spark(width=0, min=none, max=none)"
    assert by_name["if"].signature == 'if(then, else="")'
    assert by_name["map"].signature == "map(key=value, ..., _=fallback)"
    assert by_name["upper"].signature == "upper"
    for entry in entries:
        assert entry.description
        assert entry.category in {"number", "time", "text", "list", "logic"}
        assert entry._asdict()["name"] == entry.name


def test_spec_repr_and_lookup() -> None:
    assert repr(FILTERS["round"]) == "<filter round(n=0)>"
    assert F.get("ROUND") is FILTERS["round"]
    assert F.get("nope") is None
    assert repr(F.REQUIRED) == "REQUIRED"
    assert repr(_arg(1)) == "Arg(1)"
    assert F._literal(True) == "true"
    assert F._literal('a"b') == '"a\\"b"'


def test_new_bar_and_spark_arguments_report_positions() -> None:
    with pytest.raises(TemplateError) as info:
        Template.parse('{x|bar(10, chars="#")}')
    assert (info.value.line, info.value.column) == (1, 18)
    assert "filter 'bar'" in info.value.reason
    with pytest.raises(TemplateError) as info:
        Template.parse("{x|spark(min=5, max=1)}")
    assert (info.value.line, info.value.column) == (1, 21)
    assert info.value.hint == "e.g. spark(24, min=0, max=100)"
    with pytest.raises(TemplateError, match="must be a number or none") as info:
        Template.parse("{x|spark(min=low)}")
    assert info.value.hint == "usage: spark(width=0, min=none, max=none)"


def test_shared_spark_scale() -> None:
    # total and blocked drawn on one scale: blocked never looks taller than total
    template = Template.parse("{t|spark(min=0, max=40)}\n{b|spark(min=0, max=40)}")
    total, blocked = [10, 40, 20], [5, 10, 5]
    output = template.render({"t": total, "b": blocked}, options=PLAIN).split("\n")
    assert output == ["▃█▅", "▂▃▂"]
    for top, bottom in zip(*output, strict=True):
        assert F._SPARKS.index(bottom) <= F._SPARKS.index(top)


@settings(max_examples=200, deadline=None)
@given(
    value=st.one_of(st.integers(-1000, 1000), st.floats(allow_nan=False, allow_infinity=False)),
    width=st.integers(0, 60),
    maximum=st.integers(1, 1000),
)
def test_two_char_bar_is_always_width_cells(value: float, width: int, maximum: int) -> None:
    bar = F.f_bar(value, width, maximum, "#-")
    assert bar is not None
    assert len(bar) == width
    assert bar == "#" * bar.count("#") + "-" * bar.count("-")
    smooth = F.f_bar(value, width, maximum)
    assert smooth is not None
    assert visible_width(smooth) == width


@settings(max_examples=200, deadline=None)
@given(
    values=st.lists(st.one_of(st.integers(-500, 500), st.none()), max_size=30),
    low=st.one_of(st.none(), st.integers(-100, 100)),
    span=st.integers(1, 300),
    upper=st.booleans(),
)
def test_spark_bounds_clamp_and_keep_order(
    values: list[int | None], low: int | None, span: int, upper: bool
) -> None:
    high = (low or 0) + span if upper else None
    line = F.f_spark(values, 0, low, high)
    assert line is not None
    assert len(line) == len(values)
    levels = [F._SPARKS.index(c) if c != " " else None for c in line]
    assert [lvl is None for lvl in levels] == [v is None for v in values]
    pairs = sorted(
        (v, lvl) for v, lvl in zip(values, levels, strict=True) if v is not None and lvl is not None
    )
    assert [lvl for _, lvl in pairs] == sorted(lvl for _, lvl in pairs)  # monotonic
    for v, lvl in pairs:
        if low is not None and v <= low:
            assert lvl == 0
        if high is not None and v >= high:
            assert lvl == 7
