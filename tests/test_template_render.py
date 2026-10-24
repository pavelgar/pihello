"""Rendering: golden ANSI output, styles, themes, values, strictness and layout."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from pihello.errors import TemplateError
from pihello.template import (
    ColorDepth,
    RenderOptions,
    Template,
    escape,
    render,
    sanitize,
    strip_ansi,
    visible_width,
)

T, C256, C16, NONE = (
    ColorDepth.TRUECOLOR,
    ColorDepth.ANSI256,
    ColorDepth.ANSI16,
    ColorDepth.NONE,
)
R = "\x1b[0m"
THEME = {"good": "green bold", "bad": "red", "warn": "#ffaf00", "muted": "grey50", "title": "bold"}


def r(source: str, variables: dict[str, Any] | None = None, **options: Any) -> str:
    theme = options.pop("theme", THEME)
    return Template.parse(source).render(
        variables or {}, theme=theme, options=RenderOptions(**options)
    )


# --------------------------------------------------------------------------- #
# Golden ANSI output                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("plain text", "plain text"),
        ("[bold turquoise2]Hi[] there", f"\x1b[0;1;38;5;45mHi{R} there"),
        ("[red]a[blue]b[]c", f"\x1b[0;91ma\x1b[0;94mb{R}c"),
        ("[red]a[red]b", f"\x1b[0;91mab{R}"),  # same style: no new sequence
        ("[red]a[@bad]b", f"\x1b[0;91mab{R}"),  # equal style via theme: no new sequence
        ("[red][blue]x", f"\x1b[0;94mx{R}"),  # unused styles are never emitted
        ("[red]", ""),
        ("[]x[]y", "xy"),
        ("[bold]a[normal]b[reset]c", f"\x1b[0;1ma{R}bc"),
        ("[bold]a[]", f"\x1b[0;1ma{R}"),
        ("[bold]a\nb[]\nc", f"\x1b[0;1ma{R}\n\x1b[0;1mb{R}\nc"),  # style carries across lines
        ("[bold]a\n\nb", f"\x1b[0;1ma{R}\n\n\x1b[0;1mb{R}"),
        ("[#ff8800 on navy]x", f"\x1b[0;38;2;255;136;0;44mx{R}"),
        ("[underline _#00ff00]x", f"\x1b[0;4;58;2;0;255;0mx{R}"),
        ("[bold italic dim]x", f"\x1b[0;1;2;3mx{R}"),
        ("a [default]b", "a b"),
    ],
)
def test_golden_truecolor(source: str, expected: str) -> None:
    assert r(source) == expected


@pytest.mark.parametrize(
    ("depth", "expected"),
    [
        (T, f"\x1b[0;1;38;2;255;136;0;48;5;67;58;5;9mX{R} \x1b[0;97mW{R}"),
        (C256, f"\x1b[0;1;38;5;208;48;5;67;58;5;9mX{R} \x1b[0;97mW{R}"),
        (C16, f"\x1b[0;1;93;44mX{R} \x1b[0;97mW{R}"),
        (NONE, "X W"),
    ],
)
def test_golden_depths(depth: ColorDepth, expected: str) -> None:
    assert r("[bold #ff8800 on steelblue _red]X[] [white]W", color=depth) == expected


def test_plain_text_contains_no_escapes() -> None:
    output = r("Blocked {n|number} of {t} queries\n\tdone", {"n": 1234, "t": 5})
    assert "\x1b" not in output
    assert output == "Blocked 1,234 of 5 queries\n    done"


# --------------------------------------------------------------------------- #
# Themes and dynamic tags                                                      #
# --------------------------------------------------------------------------- #
def test_theme_styles() -> None:
    assert r("[@good]ok") == f"\x1b[0;1;32mok{R}"
    assert r("[@good red]ok") == f"\x1b[0;1;91mok{R}"  # later words override
    assert r("[@title @bad]x") == f"\x1b[0;1;91mx{R}"
    assert r("[@Good]ok") == f"\x1b[0;1;32mok{R}"  # names are case-insensitive
    assert r("[@good]ok", theme={"good": "normal"}) == "ok"


@pytest.mark.parametrize(
    ("source", "theme", "reason", "hint"),
    [
        ("[@godo]x", THEME, "unknown theme style '@godo'", "did you mean '@good'?"),
        ("[@zzz]x", THEME, "unknown theme style", "available theme styles: @good"),
        ("[@good]x", None, "unknown theme style '@good'", "no theme is active"),
        ("[@good]x", {"good": "gren"}, "theme style '@good' ('gren') is invalid", "'green'"),
        ("[@good]x", {"good": "bold @bad"}, "refers to '@bad'", "cannot reference"),
        ("[{x|map(a=@goood)}]y", THEME, "unknown theme style '@goood'", "'@good'"),
        ("[{x|if(@good, @bda)}]y", THEME, "unknown theme style '@bda'", "'@bad'"),
    ],
)
def test_theme_errors(source: str, theme: Any, reason: str, hint: str | None) -> None:
    with pytest.raises(TemplateError) as info:
        Template.parse(source).render({"x": "@nope"}, theme=theme)
    assert reason in info.value.reason
    assert (info.value.line, info.value.column) == (1, 2)
    if hint is not None:
        assert hint in (info.value.hint or "")


@pytest.mark.parametrize(
    ("blocking", "expected"),
    [
        ("enabled", f"\x1b[0;1;32mBlocking{R}"),
        ("disabled", f"\x1b[0;91mBlocking{R}"),
        ("failed", f"\x1b[0;38;2;255;175;0mBlocking{R}"),
        (None, "Blocking"),  # a missing value contributes nothing
    ],
)
def test_conditional_styles(blocking: str | None, expected: str) -> None:
    source = "[{blocking|map(enabled=@good, disabled=@bad, _=@warn)}]Blocking"
    assert r(source, {"blocking": blocking}) == expected


def test_dynamic_tags() -> None:
    assert r("[{u|if(@warn, @muted)} bold]x", {"u": True}) == f"\x1b[0;1;38;2;255;175;0mx{R}"
    assert r("[bold {c}]x", {"c": "red underline"}) == f"\x1b[0;1;4;91mx{R}"
    assert r("[:{c}]x", {"c": "navy"}) == f"\x1b[0;44mx{R}"
    assert r("[@{name}]x", {"name": "bad"}) == f"\x1b[0;91mx{R}"
    assert r("[white on {c}]x", {"c": "navy"}) == f"\x1b[0;97;44mx{R}"
    # data that is not a style is ignored (unless strict)…
    assert r("[bold {c}]x", {"c": "bogus"}) == f"\x1b[0;1mx{R}"
    assert r("[white on {c}]x", {}) == f"\x1b[0;97mx{R}"
    # …and the result is cached safely per word list
    assert r("[bold {c}]x", {"c": "bogus"}) == f"\x1b[0;1mx{R}"
    with pytest.raises(TemplateError, match="invalid style from") as info:
        r("[bold {c}]x", {"c": "bogus"}, strict=True)
    assert info.value.hint


def test_theme_names_from_data() -> None:
    # '@name' coming from data is resolved when it exists and ignored otherwise…
    assert r("[{x}]y", {"x": "@bad"}) == f"\x1b[0;91my{R}"
    assert r("[{x}]y", {"x": "@nope bold"}) == f"\x1b[0;1my{R}"
    assert r("[{x}]y", {"x": "@"}) == "y"
    # …unless strict
    with pytest.raises(TemplateError, match="unknown theme style '@nope'"):
        r("[{x}]y", {"x": "@nope"}, strict=True)


def test_dynamic_tag_paths_are_reported() -> None:
    assert Template.parse("[{a.b|if(red)}]x").paths() == frozenset({"a.b"})


# --------------------------------------------------------------------------- #
# Values                                                                       #
# --------------------------------------------------------------------------- #
VARS = {
    "queries": {"total": 48213, "blocked": 7321, "percent_blocked": 15.18, "types": {"A": 1}},
    "blocking": "enabled",
    "timer": None,
    "updates": {"available": False, "components": ["core", "web"]},
    "top_domains": [{"domain": "a.com", "count": 5}, {"domain": "b.org", "count": 3}],
    "system": {"memory": {"ram": {"%used": 42.5}}},
    "flat.key": "flat",
    "nums": [1, 2, 3],
}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{queries.total}", "48213"),
        ("{ queries.total | number }", "48,213"),
        ("{queries.percent_blocked}", "15.2"),  # float → one decimal (1.x behaviour)
        ("{queries.percent_blocked:.2f}", "15.18"),
        ("{queries.total:>8}|", "   48213|"),
        ("{queries.total:,}", "48,213"),
        ("{blocking:^9}|", " enabled |"),
        ("{updates.available}", "no"),
        ("{updates.available:>4}", "  no"),
        ("{updates.components}", "core, web"),
        ("{updates.components:>10}", " core, web"),
        ("{top_domains.0.domain}", "a.com"),
        ("{top_domains.1.count}", "3"),
        ("{top_domains.-1.domain}", "b.org"),
        ("{top_domains.5.domain}", "?"),
        ("{top_domains.x}", "?"),
        ("{nums.0.deeper}", "?"),
        ("{system.memory.ram.%used}%", "42.5%"),
        ("{flat.key}", "flat"),
        ("{missing}", "?"),
        ("{queries.missing}", "?"),
        ("{timer}", "?"),
        ("{queries}", "?"),  # a group renders as missing
        ("{queries.types}", "?"),
        ('{"literal"}', "literal"),
        ("{42}", "42"),
        ("{1.25}", "1.2"),
        ('{"─"|repeat(3)}', "───"),
        ('{timer|duration|prefix(" for ")}', "?"),
        ('{timer|duration|prefix(" for ")|default("")}', ""),
        ("{top_domains|pluck(count)|sum}", "8"),
        ("{x:.2f}", "?"),
        ("{queries:>5}", "?"),  # a group stays missing even with a format spec
    ],
)
def test_values(source: str, expected: str) -> None:
    assert r(source, VARS, color=NONE) == expected


def test_missing_marker_option() -> None:
    assert r("{nope}", missing="n/a", color=NONE) == "n/a"
    assert r("{nope}", missing="", color=NONE) == ""
    assert r("[red]{nope}", missing="-") == f"\x1b[0;91m-{R}"


def test_format_spec_fallbacks() -> None:
    assert r("{x:.1f}", {"x": "2.25"}, color=NONE) == "2.2"
    assert r("{x:.2f}", {"x": "abc"}, color=NONE) == "abc"
    assert r("{x:d}", {"x": 2.5}, color=NONE) == "2.5"
    assert r("{x:s}", {"x": 5}, color=NONE) == "5"
    with pytest.raises(TemplateError, match="cannot apply format spec") as info:
        r("{x:.2f}", {"x": "abc"}, strict=True)
    assert (info.value.line, info.value.column) == (1, 1)


def test_values_from_data_are_sanitized() -> None:
    evil = "evil\x1b]0;pwned\x07.com\r\x00\x9b"
    output = r("{d}", {"d": evil})
    assert "\x1b" not in output
    assert output == "evil.com"
    assert r("{d}", {"d": "tab\there"}, color=NONE) == "tab here"


def test_escape_sequences_in_data_keep_padded_widths() -> None:
    # Regression: only the ESC was removed, so the rest of the sequence ("[31m")
    # showed up although ljust/truncate had counted the sequence as zero columns.
    data = {"d": "\x1b[31mab\x1b[0m"}
    assert r("{d|ljust(6)}|", data, color=NONE) == "ab    |"
    assert r("{d|truncate(1)}|", data, color=NONE) == "…|"
    assert visible_width(r("{d|rjust(8)}|", {"d": "x\x1b]0;title\x07y"})) == 9


def test_values_from_data_are_always_encodable() -> None:
    # Regression: json.loads(bytes) decodes with "surrogatepass", so a domain name
    # with CESU-8 bytes reaches the renderer as a lone surrogate, and writing the
    # greeting to stdout failed with UnicodeEncodeError.
    output = r("{d}", {"d": "a\ud800b\udfffc"}, color=NONE)
    assert output == "abc"
    output.encode("utf-8")


def test_malformed_list_index_is_missing() -> None:
    # Regression: "--1" passed the digit check and int("--1") raised ValueError.
    assert r("{x.--1}", {"x": [1, 2]}, color=NONE) == "?"
    assert r("{x.-1}", {"x": [1, 2]}, color=NONE) == "2"
    with pytest.raises(TemplateError, match=r"variable 'x\.--1' is not defined"):
        r("{x.--1}", {"x": [1, 2]}, strict=True)


def test_format_spec_overflow_falls_back() -> None:
    # Regression: OverflowError (huge int → float, out-of-range %c) escaped the fallback.
    huge = 10**400
    assert r("{x:.2f}", {"x": huge}, color=NONE) == str(huge)
    assert r("{x:.2f}", {"x": "9" * 350}, color=NONE) == "9" * 350
    assert r("{x:c}|", {"x": -1}, color=NONE) == "-1|"
    with pytest.raises(TemplateError, match="cannot apply format spec"):
        r("{x:.2f}", {"x": huge}, strict=True)


def test_multiline_values_split_lines_and_keep_style() -> None:
    output = r("[red]{v}[] end", {"v": "one\ntwo"})
    assert output == f"\x1b[0;91mone{R}\n\x1b[0;91mtwo{R} end"


def test_mapping_like_variables() -> None:
    class Vars:
        def __init__(self, data: dict[str, Any]) -> None:
            self.data = data

        def keys(self) -> Any:
            return self.data.keys()

        def get(self, key: str, default: Any = None) -> Any:
            return self.data.get(key, default)

        def __iter__(self) -> Any:
            return iter(self.data)

        def __contains__(self, key: object) -> bool:
            return key in self.data

        def __getitem__(self, key: str) -> Any:
            return self.data[key]

    variables = Vars({"a": Vars({"b": 7})})
    template = Template.parse("{a.b} {a.c}")
    assert template.render(variables, options=RenderOptions(color=NONE)) == "7 ?"  # type: ignore[arg-type]


def test_filter_failure_is_missing_or_strict_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from pihello.template import filters

    def boom(value: Any) -> Any:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(filters.FILTERS["upper"], "func", boom)
    template = Template.parse("{x|upper}")
    assert template.render({"x": "a"}, options=RenderOptions(color=NONE)) == "?"
    with pytest.raises(TemplateError, match="filter 'upper' failed on 'a': kaboom") as info:
        template.render({"x": "a"}, options=RenderOptions(strict=True))
    assert (info.value.line, info.value.column) == (1, 4)


# --------------------------------------------------------------------------- #
# Strict mode                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source", "reason", "hint"),
    [
        (
            "{queries.totl}",
            "variable 'queries.totl' is not defined",
            "did you mean 'queries.total'?",
        ),
        ("{querys.total}", "variable 'querys.total' is not defined", "'queries.total'"),
        ("{zzzz}", "variable 'zzzz' is not defined", "pihello vars"),
        ("{top_domains.5.domain}", "is not defined", "'top_domains' has only 2 items"),
        ("{top_domains.0.domian}", "is not defined", "'top_domains.0.domain'"),
        ("{blocking.x}", "is not defined", "pihello vars"),
        ("{flat.kye}", "is not defined", "'flat.key'"),
        ("{timer}", "variable 'timer' has no value", 'default("n/a")'),
        ("{timer.x}", "variable 'timer.x' has no value", None),
        ("{queries}", "'queries' is a group; use e.g. {queries.total}", "keys: total, blocked"),
        ("{nums|take(0)|first}", "{nums|take(0)|first} has no value", "|default"),
        ('{"x"|int}', '{"x"|int} has no value', None),
    ],
)
def test_strict(source: str, reason: str, hint: str | None) -> None:
    with pytest.raises(TemplateError) as info:
        r("line one\n  " + source, VARS, strict=True)
    assert reason in info.value.reason
    assert (info.value.line, info.value.column) == (2, 3)
    if hint is not None:
        assert hint in (info.value.hint or "")


def test_strict_empty_group() -> None:
    with pytest.raises(TemplateError, match=r"is a group; use e.g. \{group\.key\}"):
        r("{g}", {"g": {}}, strict=True)


def test_render_errors_carry_an_excerpt() -> None:
    """Regression: render-time errors (an unknown ``@name``, strict missing values)
    were the only template errors without the source line and caret."""
    with pytest.raises(TemplateError) as info:
        r("line one\nab [@titel]x", theme={"title": "bold"})
    assert info.value.excerpt == "2 | ab [@titel]x\n  |     ^"  # under the @name
    with pytest.raises(TemplateError) as info:
        r("\ufeffx {queries.totl}\r\nnext", VARS, strict=True)  # same lines as the parser sees
    assert info.value.excerpt == "1 | x {queries.totl}\n  |   ^"
    with pytest.raises(TemplateError) as info:
        r("[{x|if(@good, @bda)}]y", {"x": 1}, theme=THEME)
    assert info.value.excerpt == "1 | [{x|if(@good, @bda)}]y\n  |  ^"


@pytest.mark.parametrize(
    ("source", "renamed"),
    [
        ("{status}", "{blocking}"),
        ("{core_current}", "{version.core.local.version}"),
        ("{web_current}", "{version.web.local.version}"),
        ("{FTL_current}", "{version.ftl.local.version}"),
        ("{dns_queries_today|number}", "{queries.total}"),
        ("{ads_blocked_today}", "{queries.blocked}"),
        ("{ads_percentage_today:.1f}", "{queries.percent_blocked}"),
        ("{domains_being_blocked}", "{gravity.domains_being_blocked}"),
        ("{unique_clients}", "{clients.active}"),
        ("{gravity_last_updated.relative.days}", "{gravity.relative.days}"),
        ("{gravity_last_updated.relative.minutes}", "{gravity.relative.minutes}"),
    ],
)
def test_strict_hints_pihello_03_renames(source: str, renamed: str) -> None:
    """Regression: names from pihello 0.3 only got "`pihello vars` lists the variables"."""
    with pytest.raises(TemplateError, match="is not defined") as info:
        r(source, VARS, strict=True)
    assert info.value.hint == f"renamed to {renamed} in pihello 1.0"


def test_strict_rename_hint_only_for_exact_old_names() -> None:
    for source in ("{statusx}", "{status_x}", "{gravity_last_updated.absolute}"):
        with pytest.raises(TemplateError) as info:
            r(source, VARS, strict=True)
        assert "renamed" not in (info.value.hint or ""), source
    assert r("{status}", {"status": "ok"}, strict=True) == "ok"  # a real variable wins


def test_strict_default_suppresses() -> None:
    assert r('{timer|default("forever")}', VARS, strict=True) == "forever"
    assert r("{nope|default(none)}", VARS, strict=True, color=NONE) == "?"
    assert r("[{nope}]ok", VARS, strict=True) == "ok"  # tags never raise for missing values


# --------------------------------------------------------------------------- #
# Hostile data                                                                 #
# --------------------------------------------------------------------------- #
def _nested(depth: int, leaf: Any = "a") -> Any:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("depth", [400, 100_000])
def test_deeply_nested_lists_are_missing(depth: int) -> None:
    # Regression: to_text() recursed once per level, so a hostile Pi-hole sending
    # a list nested ~400 deep made render() raise RecursionError (exit 1).
    deep = {"x": _nested(depth)}
    assert r("{x}|{x:>3}|{x|join}|{x|first|first}", deep, color=NONE) == "?|?||?"
    assert r("[{x}]y {x|map(a=b)}", deep, color=NONE) == "y ?"
    for source in ("{x}", "{x:>3}", "{x|map(a=b)}"):
        with pytest.raises(TemplateError):
            r(source, deep, strict=True)


def test_nesting_up_to_the_limit_still_renders() -> None:
    assert r("{x}", {"x": _nested(30)}, color=NONE) == "a"
    assert r("{x}", {"x": [1, _nested(30, 2)]}, color=NONE) == "1, 2"


def test_recursion_errors_are_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from pihello.template import engine

    def deep(value: Any) -> str | None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(engine, "to_text", deep)
    assert r("{x}|{x:>3}|[{x}]y", {"x": [1]}, color=NONE) == "?|?|y"
    with pytest.raises(TemplateError, match="has no value"):
        r("{x}", {"x": [1]}, strict=True)


def test_huge_values_are_cut() -> None:
    # Regression: wrapping a 20 MB value took 1.6 GB and 7 s, even with a height.
    assert len(r("{x}", {"x": "a" * 100_000}, color=NONE)) == 65_536
    assert len(r('{"ab"|repeat(40000)}', color=NONE)) == 65_536


@pytest.mark.parametrize(
    "value",
    ["a" * 5_000_000, "word " * 1_000_000, "日本" * 2_500_000],
    ids=["ascii", "words", "wide"],
)
def test_huge_values_render_quickly(value: str) -> None:
    import time
    import tracemalloc

    def render() -> str:
        return r("[@good]{x}[] {x|upper}", {"x": value}, width=80, height=24, color=NONE)

    # Memory and time are measured separately: tracemalloc itself slows rendering
    # down several times over (most on Windows CI runners).
    tracemalloc.start()
    try:
        output = render()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(output.split("\n")) == 24
    assert peak < 64 * 1024 * 1024 + 4 * len(value)  # the value and its upper() copy
    start = time.perf_counter()
    render()
    assert time.perf_counter() - start < 1.0  # 32 MiB took 0.8 s and +173 MiB before


def test_format_specs_pad_sanitized_data() -> None:
    # Regression: the format spec ran first and counted the escape sequence.
    assert r("|{x:>16}|", {"x": "evil\x1b[31m.com"}, color=NONE) == "|        evil.com|"
    assert r("|{x:<6}|", {"x": ["a\x1b]0;t\x07b"]}, color=NONE) == "|ab    |"
    assert r("|{x:^5}|", {"x": "a\tb"}, color=NONE) == "| a b |"


def test_tabs_in_data_are_single_spaces() -> None:
    # Regression: data tabs were expanded to tab stops after ljust/truncate had
    # counted them as zero columns, so padded columns grew.
    assert r("{x}|", {"x": "a\tb\x0bc\x0cd"}, color=NONE) == "a b c d|"
    assert r("{x|ljust(6)}|", {"x": "a\tb"}, color=NONE) == "a b   |"
    assert r("{x|truncate(3)}|", {"x": "a\t\tbcd"}, color=NONE) == "a …|"
    assert r("{x|join}|", {"x": ["a\tb", "c"]}, color=NONE) == "a b, c|"
    # tabs written in the template keep their tab stops
    assert r("a\t{x}\tb", {"x": "c"}, color=NONE) == "a   c   b"
    assert r('{"a\\tb"}', color=NONE) == "a   b"


def test_tabs_in_data_keep_the_top_preset_aligned() -> None:
    from pihello import demo, presets, themes

    template = Template.parse(presets.get("top").source)
    data = demo.variables()
    clean = template.render(data, theme=themes.resolve("default"), options=RenderOptions())
    for group in ("top_domains", "top_blocked"):
        for entry in data[group]:
            entry["domain"] = "\t".join(entry["domain"].split("."))
    for group in ("top_clients", "top_blocked_clients"):
        for entry in data[group]:
            entry["label"] = "\t" + entry["label"]
    hostile = template.render(data, theme=themes.resolve("default"), options=RenderOptions())
    widths = [visible_width(line) for line in clean.split("\n")]
    assert [visible_width(line) for line in hostile.split("\n")] == widths
    assert max(widths) == 64


@pytest.mark.parametrize(
    "invisible",
    [
        *"\u061c\u180e\u200b\u200e\u200f\u2028\u2029\u202a\u202b\u202c\u202d\u202e",
        *"\u2060\u2061\u2064\u2066\u2067\u2068\u2069\u206a\u206f\ufeff\ufff9\ufffb",
        "\U0001bca0",
        "\U0001d173",
        "\U000e0001",
        "\U000e0041",
    ],
)
def test_reordering_and_invisible_characters_are_removed_from_data(invisible: str) -> None:
    value = f"evil{invisible}moc.example"
    assert r("{x}", {"x": value}, color=NONE) == "evilmoc.example"
    assert r("|{x:>16}|{x|rjust(16)}|", {"x": value}, color=NONE) == (
        "| evilmoc.example| evilmoc.example|"
    )
    assert sanitize(value) == "evilmoc.example"


def test_visible_format_characters_are_kept() -> None:
    # Joiners shape emoji and many scripts; the soft hyphen is visible.
    for text in ("👩\u200d💻", "क\u200dष", "Pi\u200cHole", "Pi\xadhole", "a\u00a0b", "e\u0301"):
        assert r("{x}", {"x": text}, color=NONE) == text
    # the template's own characters are not data
    assert r("[@good]\u202eab\u202c {x}", {"x": "\u202ecd"}, color=NONE) == "\u202eab\u202c cd"


def test_sanitize_contract() -> None:
    assert sanitize("plain text") == "plain text"
    assert sanitize("a\x1b[31mb\x1b]0;title\x07c\x1bPdcs\x1b\\d") == "abcd"
    assert sanitize("a\tb\x0bc\x0cd") == "a b c d"
    assert sanitize("one\r\ntwo\x00\x7f\x9b") == "one\ntwo"
    assert sanitize("x\ud800y\u202ez\u200b") == "xyz"
    assert sanitize("日本 👩\u200d💻") == "日本 👩\u200d💻"
    assert sanitize("") == ""


@pytest.mark.parametrize(
    ("source", "variables"),
    [
        ("{q}", {"q": {"cpu_temp": 1, "k\x1b]0;owned\x07": 2}}),
        ("{q}", {"q": {"k\x1b]0;owned\x07\u202e": 1}}),
        ("{q.tota}", {"q": {"tota\x07l": 1}}),
        ("{q.total}", {"q.tot\x1b[2Jal": 1}),
        ("[{x}]y", {"x": "@ba\x1b]0;owned\x07d"}),
        ("[bold {x}]y", {"x": "re\x1b]0;owned\x07d"}),
        ("{x|map(a=b)}", {"x": _nested(100_000)}),
        ("{x:.2f}", {"x": "\x1b]0;owned\x07" * 1000}),
    ],
)
def test_strict_messages_never_echo_raw_data(source: str, variables: dict[str, Any]) -> None:
    # Regression: strict hints quoted data-controlled dict keys verbatim, e.g.
    # "keys: cpu_temp, k\x1b]0;owned\x07", which retitled the terminal window.
    with pytest.raises(TemplateError) as info:
        r(source, variables, strict=True)
    for text in (info.value.reason, info.value.hint or ""):
        assert text.isprintable(), text
        assert len(text) < 400


def test_invalid_style_words_from_data_are_dropped_in_linear_time() -> None:
    # Regression: each invalid word restarted the style and computed a
    # did-you-mean hint nobody saw: 48 KB of words took 3 s.
    import time

    words = " ".join(f"zq{index}x" for index in range(8000))
    start = time.perf_counter()
    assert r("[bold {x}]y", {"x": words}) == f"\x1b[0;1my{R}"
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("bogus red", f"\x1b[0;91my{R}"),
        ("on on navy", f"\x1b[0;44my{R}"),
        ("on bogus navy", f"\x1b[0;44my{R}"),
        ("on bold navy", f"\x1b[0;44my{R}"),
        ("red on", f"\x1b[0;91my{R}"),
        ("on bold", "y"),
        (":bogus _ :navy", f"\x1b[0;44my{R}"),
        ("#12 rgb(1,2) color(300) bold", f"\x1b[0;1my{R}"),
        ("@nope @bad", f"\x1b[0;91my{R}"),
    ],
)
def test_invalid_style_words_from_data(words: str, expected: str) -> None:
    assert r("[{x}]y", {"x": words}) == expected


def test_quoted_at_in_tag_arguments_is_plain_text() -> None:
    # Regression: "@home" in quotes was taken for a theme style and every render failed.
    source = '[{x|eq("@home")|if(@good, @bad)}]y'
    assert r(source, {"x": "@home"}) == f"\x1b[0;1;32my{R}"
    assert r(source, {"x": "work"}) == f"\x1b[0;91my{R}"
    assert r('[{x|map("@a"="b", _=@bad)}]y', {"x": 1}) == f"\x1b[0;91my{R}"
    assert r("[{x|replace('@typo', '')}]y", {"x": "red"}) == f"\x1b[0;91my{R}"
    # quoted text that ends up in the style is read like data: unknown names are skipped…
    assert r("[{x|if('bold @nope', @bad)}]y", {"x": 1}) == f"\x1b[0;1my{R}"
    assert r("[{x|if('bold @bad', @good)}]y", {"x": 1}) == f"\x1b[0;1;91my{R}"
    # …and bare @names are checked on every render, whichever branch is taken
    with pytest.raises(TemplateError, match="unknown theme style '@bda'"):
        r('[{x|eq("@home")|if(@good, @bda)}]y', {"x": "@home"})


# --------------------------------------------------------------------------- #
# Layout through render                                                        #
# --------------------------------------------------------------------------- #
def test_wrap_styled_text() -> None:
    output = r("[red]hello [blue]big[] world", width=9)
    assert output == f"\x1b[0;91mhello \x1b[0;94mbig{R}\nworld"
    assert all(visible_width(line) <= 9 for line in output.split("\n"))


def test_wrapped_continuations_keep_style() -> None:
    output = r("[bold]aaa bbb ccc", width=4)
    assert output == f"\x1b[0;1maaa{R}\n\x1b[0;1mbbb{R}\n\x1b[0;1mccc{R}"


def test_clip_and_height() -> None:
    source = "[red]first line here[]\nsecond line\nthird"
    assert r(source, width=5, overflow="clip") == f"\x1b[0;91mfirst{R}\nsecon\nthird"
    assert r(source, width=5, height=2, color=NONE) == "first\nline"
    assert r(source, height=2, color=NONE) == "first line here\nsecond line"
    assert r(source, width=6, height=0, color=NONE).split("\n") == [
        "first",
        "line",
        "here",
        "second",
        "line",
        "third",
    ]


def test_wide_characters_wrap_by_columns() -> None:
    assert r("日本語テキスト", width=6, color=NONE) == "日本語\nテキス\nト"
    assert r("日本語テキスト", width=5, overflow="clip", color=NONE) == "日本"


def test_tabs_expand() -> None:
    assert r("a\tb", color=NONE) == "a   b"
    assert r("a\tb", tab_size=8, color=NONE) == "a       b"
    assert r("[red]ab\t[blue]c", tab_size=4) == f"\x1b[0;91mab  \x1b[0;94mc{R}"
    assert r("{v}", {"v": "x\ty"}, color=NONE) == "x y"  # a tab in data is a single space


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #
def test_render_function() -> None:
    assert render("Hi {n}", {"n": "pi"}) == "Hi pi"
    assert render("[red]x", {}, color=NONE) == "x"
    assert (
        render("[@good]x", {}, theme=THEME, options=RenderOptions(color=C16)) == f"\x1b[0;1;32mx{R}"
    )
    assert render("[red]{w}", {"w": "aa bb"}, width=2, color=NONE) == "aa\nbb"
    with pytest.raises(TemplateError, match=r"^t\.txt:1:1"):
        render("{", {}, name="t.txt")
    with pytest.raises(TypeError):
        render("x", {}, colour=NONE)


def test_render_function_cache_is_bounded() -> None:
    from pihello.template import engine

    engine._TEMPLATE_CACHE.clear()
    for index in range(70):
        assert render(f"t{index}", {}) == f"t{index}"
    assert len(engine._TEMPLATE_CACHE) <= 64
    cached = engine._TEMPLATE_CACHE[("t69", "<template>")]
    assert render("t69", {}) == "t69"
    assert engine._TEMPLATE_CACHE[("t69", "<template>")] is cached


def test_style_caches_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from pihello.template import engine

    monkeypatch.setattr(engine, "_CACHE_LIMIT", 2)
    engine._STYLE_CACHE.clear()
    engine._THEME_WORDS.clear()
    themes = [{"a": "red"}, {"a": "blue"}, {"a": "green"}, {"a": "bold"}]
    for theme in themes:
        r("[@a {x}]x", {"x": "italic"}, theme=theme)
    assert len(engine._STYLE_CACHE) <= 2
    assert len(engine._THEME_WORDS) <= 2


def test_template_reuse_and_repr() -> None:
    template = Template.parse("{n}", name="watch.txt")
    assert template.source_name == "watch.txt"
    assert template.source == "{n}"
    assert repr(template) == "<Template 'watch.txt'>"
    assert [template.render({"n": i}) for i in range(3)] == ["0", "1", "2"]
    assert Template("x").render({}) == "x"


def test_render_options() -> None:
    options = RenderOptions()
    assert options.color is T
    assert (options.width, options.height, options.overflow, options.tab_size) == (0, 0, "wrap", 4)
    assert (options.strict, options.missing, options.now) == (False, "?", None)
    assert dataclasses.replace(options, width=10).width == 10
    assert RenderOptions(color=1).color is C16  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        options.width = 3  # type: ignore[misc]
    for bad in ({"width": -1}, {"height": -2}, {"tab_size": -1}, {"width": 1.5}, {"width": True}):
        with pytest.raises(ValueError, match="must be an integer"):
            RenderOptions(**bad)
    with pytest.raises(ValueError, match="overflow"):
        RenderOptions(overflow="scroll")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ColorDepth"):
        RenderOptions(color=7)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    ["plain", "[bold]", "{x}", "a\\b", "C:\\path", "]}{[", "\\[", "{# not #}", "trail\\"],
)
def test_escape_round_trips(text: str) -> None:
    assert render(escape(text), {}) == text


def test_strip_ansi_matches_plain_render() -> None:
    source = "[bold red]Hello[] [@good]{name|upper}[]\n[on navy]{n|number}"
    variables = {"name": "pi", "n": 12345}
    colored = Template.parse(source).render(variables, theme=THEME)
    plain = Template.parse(source).render(variables, theme=THEME, options=RenderOptions(color=NONE))
    assert strip_ansi(colored) == plain == "Hello PI\n12,345"


# --------------------------------------------------------------------------- #
# pihello 1.x templates                                                        #
# --------------------------------------------------------------------------- #
DEFAULT_CONTENT_1X = """\
[cyan2]─────────────────────────────────────────────────────[]
[white]PiHole[] ([green4]{blocking}[]) [lightgreen]{version.core.local.version}[white], Web [lightgreen]{version.web.local.version}[white], FTL [lightgreen]{version.ftl.local.version}
[cyan2]─────────────────────────────────────────────────────[]
Blocking [darkcyan]{gravity.domains_being_blocked}[] domains for [steelblue]{clients.active}[] clients
Blocked [fuchsia]{queries.blocked}[] out of [lightgreen]{queries.total}[] queries [underline]today[] ([steelblue]{queries.percent_blocked}%[])
[grey37]Gravity last updated [bold grey50]{gravity.relative.days}[grey37] days [bold grey50]{gravity.relative.hours}[grey37] hours and [bold grey50]{gravity.relative.minutes}[grey37] minutes ago\
"""  # noqa: E501 - verbatim pihello 1.x template

# docs/example.txt and docs/SHOWCASE.md from pihello 1.x (the latter with 0.x variable names)
EXAMPLE_TXT_1X = DEFAULT_CONTENT_1X + "\n"
SHOWCASE_1X = """\
[cyan2]─────────────────────────────────────────────────────[]
[white]PiHole[] ([green4]{status}[]) [lightgreen]{core_current}[white], Web [lightgreen]{web_current}[white], FTL [lightgreen]{FTL_current}
[cyan2]─────────────────────────────────────────────────────[]
Blocking [darkcyan]{domains_being_blocked}[] domains for [steelblue]{unique_clients}[] clients
Blocked [fuchsia]{ads_blocked_today}[] out of [lightgreen]{dns_queries_today}[] queries [underline]today[] ([steelblue]{ads_percentage_today}%[])
[grey37]Gravity last updated [bold grey50]{gravity_last_updated.relative.days}[grey37] days [bold grey50]{gravity_last_updated.relative.hours}[grey37] hours and [bold grey50]{gravity_last_updated.relative.minutes}[grey37] minutes ago
"""  # noqa: E501

V2_VARIABLES = {
    "blocking": "enabled",
    "version": {
        "core": {"local": {"version": "v6.0.5"}},
        "web": {"local": {"version": "v6.0.2"}},
        "ftl": {"local": {"version": "v6.0.4"}},
    },
    "gravity": {"domains_being_blocked": 123456, "relative": {"days": 1, "hours": 2, "minutes": 3}},
    "clients": {"active": 12},
    "queries": {"blocked": 4821, "total": 48213, "percent_blocked": 9.99912},
}

EXPECTED_PLAIN = """\
─────────────────────────────────────────────────────
PiHole (enabled) v6.0.5, Web v6.0.2, FTL v6.0.4
─────────────────────────────────────────────────────
Blocking 123456 domains for 12 clients
Blocked 4821 out of 48213 queries today (10.0%)
Gravity last updated 1 days 2 hours and 3 minutes ago"""


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[prefix + key] = value
    return flat


@pytest.mark.parametrize("source", [DEFAULT_CONTENT_1X, EXAMPLE_TXT_1X])
@pytest.mark.parametrize(
    "variables", [V2_VARIABLES, _flatten(V2_VARIABLES)], ids=["nested", "flat"]
)
def test_pihello_1x_default_template(source: str, variables: dict[str, Any]) -> None:
    template = Template.parse(source)
    assert template.paths() >= {"blocking", "queries.total", "gravity.relative.days"}
    output = template.render(variables, options=RenderOptions(color=C256, strict=True))
    assert strip_ansi(output) == EXPECTED_PLAIN
    lines = output.split("\n")
    assert lines[0] == f"\x1b[0;38;5;50m{'─' * 53}{R}"
    assert lines[1].startswith(f"\x1b[0;97mPiHole{R} (\x1b[0;38;5;28menabled{R}) ")
    assert lines[1].endswith(f"\x1b[0;97m, FTL \x1b[0;38;5;120mv6.0.4{R}")
    assert lines[4].endswith(f"queries \x1b[0;4mtoday{R} (\x1b[0;38;5;67m10.0%{R})")
    assert lines[5] == (
        "\x1b[0;38;5;59mGravity last updated \x1b[0;1;38;5;244m1\x1b[0;38;5;59m days "
        "\x1b[0;1;38;5;244m2\x1b[0;38;5;59m hours and \x1b[0;1;38;5;244m3\x1b[0;38;5;59m"
        f" minutes ago{R}"
    )
    assert (
        strip_ansi(template.render(variables, options=RenderOptions(color=C16))) == EXPECTED_PLAIN
    )


def test_pihello_showcase_template_parses_and_renders() -> None:
    template = Template.parse(SHOWCASE_1X)
    assert "gravity_last_updated.relative.days" in template.paths()
    output = template.render(
        {"status": "enabled", "unique_clients": 3}, options=RenderOptions(color=NONE)
    )
    assert output.split("\n")[1] == "PiHole (enabled) ?, Web ?, FTL ?"
    assert output.count("\n") == 5


class TestPihello1xBugsFixed:
    """Regression tests for the bugs of the 1.x renderer (pihello/console.py)."""

    def test_hex_and_rgb_colors_render(self) -> None:
        # 1.x: `self.number < 16` with number=None → TypeError
        assert r("[#ff8800]x[rgb(1, 2, 3)]y") == (f"\x1b[0;38;2;255;136;0mx\x1b[0;38;2;1;2;3my{R}")

    def test_unknown_color_is_a_template_error(self) -> None:
        # 1.x: KeyError from COLOR_NAMES[...]
        with pytest.raises(TemplateError, match="unknown style word 'steelblu'"):
            Template.parse("[steelblu]x")

    def test_rgb_underline_color_uses_58(self) -> None:
        # 1.x: rgb underline colors were emitted with 48 (background)
        assert r("[underline _#00ff00]x") == f"\x1b[0;4;58;2;0;255;0mx{R}"

    def test_trailing_backslash(self) -> None:
        # 1.x: IndexError on s[ptr + 1]
        assert r("ends with \\") == "ends with \\"

    def test_missing_variables_render_as_marker(self) -> None:
        # 1.x: TagParseError("No variable ...") for any missing variable
        assert r("{not.there}", color=NONE) == "?"

    def test_width_height_clip_and_indent(self) -> None:
        # 1.x: accepted -W/-H/-c/-i but never implemented them
        source = "\tone two three\nfour\nfive"
        assert r(source, width=9, color=NONE) == "    one\ntwo three\nfour\nfive"
        assert r(source, width=9, overflow="clip", height=2, color=NONE) == "    one t\nfour"
        assert r(source, tab_size=2, height=1, color=NONE) == "  one two three"
