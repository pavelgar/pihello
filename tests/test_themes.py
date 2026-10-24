"""Tests for the built-in themes (:mod:`pihello.themes`)."""

from __future__ import annotations

import re

import pytest

from pihello import themes
from pihello.errors import ConfigError
from pihello.template import ColorDepth, RenderOptions, Template

REQUIRED = (
    "default",
    "classic16",
    "nord",
    "dracula",
    "gruvbox",
    "catppuccin",
    "solarized",
    "tokyonight",
    "monokai",
    "pihole",
    "mono",
)
ALL_THEMES = themes.available()
THEME_IDS = [theme.name for theme in ALL_THEMES]
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _sgr_params(text: str) -> list[int]:
    """Every numeric SGR parameter emitted in ``text``."""
    return [int(p) for match in SGR.findall(text) for p in match.split(";") if p]


def _sample(theme: themes.Theme, depth: ColorDepth) -> str:
    """Render one word in every style of ``theme``."""
    source = " ".join(f"[@{name}]{name}" for name in themes.STYLE_NAMES) + "[]"
    return Template.parse(source).render({}, theme=theme.styles, options=RenderOptions(color=depth))


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #
def test_style_names_match_the_contract() -> None:
    assert themes.STYLE_NAMES == (
        "title",
        "subtitle",
        "border",
        "label",
        "value",
        "muted",
        "accent",
        "good",
        "warn",
        "bad",
        "info",
        "highlight",
    )


def test_required_themes_exist() -> None:
    assert set(REQUIRED) <= set(THEME_IDS)


def test_available_lists_default_first_then_alphabetical() -> None:
    assert THEME_IDS[0] == "default"
    assert THEME_IDS[1:] == sorted(THEME_IDS[1:])
    assert len(set(THEME_IDS)) == len(THEME_IDS)


def test_get_returns_the_registered_theme() -> None:
    for theme in ALL_THEMES:
        assert themes.get(theme.name) is theme
    assert themes.get("  Nord ") is themes.get("nord")


def test_get_unknown_theme_suggests_the_closest_name() -> None:
    with pytest.raises(ConfigError, match="unknown theme 'drakula'") as excinfo:
        themes.get("drakula")
    assert excinfo.value.hint is not None
    assert "did you mean 'dracula'" in excinfo.value.hint


def test_get_unknown_theme_without_close_match_lists_all() -> None:
    with pytest.raises(ConfigError) as excinfo:
        themes.get("zzzzzz")
    assert excinfo.value.hint is not None
    assert "did you mean" not in excinfo.value.hint
    assert all(name in excinfo.value.hint for name in REQUIRED)


def test_theme_styles_are_read_only() -> None:
    theme = themes.get("default")
    with pytest.raises(TypeError):
        theme.styles["title"] = "red"  # type: ignore[index]


# --------------------------------------------------------------------------- #
# Theme contents                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ALL_THEMES, ids=THEME_IDS)
def test_theme_defines_every_style_name(theme: themes.Theme) -> None:
    assert tuple(theme.styles) == themes.STYLE_NAMES
    assert all(isinstance(words, str) and words.strip() for words in theme.styles.values())
    assert theme.description
    assert theme.name == theme.name.lower()


@pytest.mark.parametrize("theme", ALL_THEMES, ids=THEME_IDS)
def test_theme_styles_are_valid_style_tags(theme: themes.Theme) -> None:
    for name, words in theme.styles.items():
        assert "@" not in words, f"{theme.name}.{name} must not reference other styles"
        Template.parse(f"[{words}]x[]", name=f"{theme.name}.{name}")


@pytest.mark.parametrize("theme", ALL_THEMES, ids=THEME_IDS)
def test_theme_hex_colors_are_well_formed(theme: themes.Theme) -> None:
    for words in theme.styles.values():
        for word in words.split():
            if word.startswith("#"):
                assert re.fullmatch(r"#[0-9A-Fa-f]{6}", word), word


@pytest.mark.parametrize("theme", ALL_THEMES, ids=THEME_IDS)
@pytest.mark.parametrize("depth", list(ColorDepth), ids=lambda d: d.name)
def test_theme_renders_at_every_color_depth(theme: themes.Theme, depth: ColorDepth) -> None:
    text = _sample(theme, depth)
    if depth is ColorDepth.NONE:
        assert "\x1b" not in text
    else:
        assert "\x1b[" in text
    if depth is ColorDepth.ANSI16:
        params = _sgr_params(text)
        assert 38 not in params
        assert 48 not in params


@pytest.mark.parametrize("theme", ALL_THEMES, ids=THEME_IDS)
def test_key_roles_are_visually_distinct(theme: themes.Theme) -> None:
    styles = theme.styles
    assert styles["label"] != styles["value"]
    assert len({styles["good"], styles["warn"], styles["bad"]}) == 3
    assert styles["muted"] != styles["value"]


def test_classic16_only_uses_the_basic_palette() -> None:
    for words in themes.get("classic16").styles.values():
        for word in words.split():
            match = re.fullmatch(r"color\((\d+)\)", word)
            if match:
                assert int(match.group(1)) < 16
            else:
                assert word in {"bold", "dim", "italic", "underline", "default"}, word
    params = _sgr_params(_sample(themes.get("classic16"), ColorDepth.TRUECOLOR))
    assert 38 not in params, "classic16 must not emit 256-color or truecolor codes"


def test_mono_uses_no_colors_at_all() -> None:
    params = _sgr_params(_sample(themes.get("mono"), ColorDepth.TRUECOLOR))
    colors = [p for p in params if 30 <= p <= 49 or 90 <= p <= 107 or p == 58]
    assert colors == []
    assert {1, 2, 3, 4} <= set(params)


# --------------------------------------------------------------------------- #
# Resolving a theme with overrides                                            #
# --------------------------------------------------------------------------- #
def test_resolve_without_overrides_is_a_copy_of_the_theme() -> None:
    styles = themes.resolve("nord")
    assert styles == dict(themes.get("nord").styles)
    styles["title"] = "red"
    assert themes.get("nord").styles["title"] != "red"


def test_resolve_applies_overrides() -> None:
    styles = themes.resolve("default", {"accent": "bold #ff8800", "muted": "grey42 italic"})
    assert styles["accent"] == "bold #ff8800"
    assert styles["muted"] == "grey42 italic"
    assert styles["title"] == themes.get("default").styles["title"]


def test_resolve_expands_references_to_the_base_theme() -> None:
    base = themes.get("dracula").styles
    styles = themes.resolve("dracula", {"accent": "@good underline", "good": "@bad"})
    assert styles["accent"] == f"{base['good']} underline"
    assert styles["good"] == base["bad"]
    assert "@" not in "".join(styles.values())


def test_resolved_styles_render() -> None:
    styles = themes.resolve("default", {"accent": "bold #ff8800 on grey11"})
    text = Template.parse("[@accent]x[]").render({}, theme=styles)
    assert "38;2;255;136;0" in text


def test_resolve_unknown_theme() -> None:
    with pytest.raises(ConfigError, match="unknown theme"):
        themes.resolve("nope")


def test_resolve_rejects_unknown_style_names_with_a_hint() -> None:
    with pytest.raises(ConfigError, match="unknown style name 'acent'") as excinfo:
        themes.resolve("default", {"acent": "red"})
    assert excinfo.value.hint is not None
    assert "did you mean 'accent'" in excinfo.value.hint


@pytest.mark.parametrize(
    "words",
    ["bold notacolor", "#12345", "color(300)", "[red]", "{x}", "@nope", "@"],
)
def test_resolve_rejects_invalid_style_words(words: str) -> None:
    with pytest.raises(ConfigError, match="invalid style for 'accent'"):
        themes.resolve("default", {"accent": words})


def test_resolve_rejects_non_string_styles() -> None:
    with pytest.raises(ConfigError, match="must be a string"):
        themes.resolve("default", {"accent": 42})  # type: ignore[dict-item]


def test_resolve_accepts_an_empty_override_as_reset() -> None:
    assert themes.resolve("default", {"border": ""})["border"] == ""
