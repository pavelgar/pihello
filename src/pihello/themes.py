"""Built-in color themes.

A theme maps each *semantic* style name in :data:`STYLE_NAMES` to style-tag
words (the same words you would write inside ``[...]`` in a template). Presets
only ever say ``[@label]`` or ``[@good]``, so every preset works with every
theme, and users can restyle any of them from the ``[styles]`` config table.

Semantic roles, as used by the built-in presets:

=============  ================================================================
``title``      the main heading ("Pi-hole")
``subtitle``   secondary headings: section names, the host name
``border``     rules, box frames and separators
``label``      field labels ("Queries", "Gravity")
``value``      ordinary values
``muted``      de-emphasised text: units, "ago", hints, empty bar cells
``accent``     key numbers: total queries, active clients
``good``       healthy state: blocking enabled, up to date
``warn``       attention: updates available, high load, timers
``bad``        failure: blocking disabled, errors, over the hot limit
``info``       informational numbers: gravity size, domains, upstreams
``highlight``  blocked things: blocked count, percentage, blocked domains
=============  ================================================================

Palette sources: Nord (nordtheme.com), Dracula (draculatheme.com spec),
gruvbox dark (morhetz), Catppuccin Mocha, Solarized (Ethan Schoonover), Tokyo
Night "night" (folke), Monokai (Wimer Hazenberg), and the Pi-hole logo
(``pi-hole/graphics``) plus the AdminLTE colors of the Pi-hole web dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from pihello.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["STYLE_NAMES", "Theme", "available", "get", "resolve"]

STYLE_NAMES: tuple[str, ...] = (
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

_REFERENCE = re.compile(r"@([A-Za-z0-9_-]*)")


@dataclass(frozen=True)
class Theme:
    """A named mapping of every semantic style name to style-tag words."""

    name: str
    description: str
    styles: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        """Freeze ``styles`` so a theme can never be mutated by accident."""
        object.__setattr__(self, "styles", MappingProxyType(dict(self.styles)))


def _theme(name: str, description: str, **styles: str) -> Theme:
    """Build a theme, insisting on exactly the :data:`STYLE_NAMES` keys, in order."""
    missing = set(STYLE_NAMES) - styles.keys()
    extra = styles.keys() - set(STYLE_NAMES)
    if missing or extra:  # pragma: no cover - guarded by tests
        raise AssertionError(f"theme {name!r}: missing {missing}, unknown {extra}")
    return Theme(name, description, {key: styles[key] for key in STYLE_NAMES})


_THEMES: tuple[Theme, ...] = (
    _theme(
        "default",
        "the pihello palette, in the spirit of 1.x: cyan rules, light-green values",
        title="bold grey93",
        subtitle="bold steelblue1",
        border="cyan2",
        label="grey74",
        value="lightgreen",
        muted="grey50",
        accent="steelblue1",
        good="bold green3",
        warn="bold orange1",
        bad="bold red1",
        info="darkcyan",
        highlight="hotpink",
    ),
    _theme(
        "classic16",
        "only the 16 basic ANSI colors, so it follows your terminal palette everywhere",
        title="bold color(15)",
        subtitle="bold color(14)",
        border="color(6)",
        label="default",
        value="color(10)",
        muted="color(8)",
        accent="color(12)",
        good="bold color(10)",
        warn="bold color(11)",
        bad="bold color(9)",
        info="color(14)",
        highlight="color(13)",
    ),
    _theme(
        "nord",
        "Nord: arctic, north-bluish frost and aurora colors",
        title="bold #88C0D0",
        subtitle="bold #81A1C1",
        border="#5E81AC",
        label="#D8DEE9",
        value="#8FBCBB",
        muted="#616E88",
        accent="#88C0D0",
        good="bold #A3BE8C",
        warn="bold #EBCB8B",
        bad="bold #BF616A",
        info="#81A1C1",
        highlight="#B48EAD",
    ),
    _theme(
        "dracula",
        "Dracula: purple, pink and cyan on a dark background",
        title="bold #FF79C6",
        subtitle="bold #BD93F9",
        border="#6272A4",
        label="#8BE9FD",
        value="#F8F8F2",
        muted="#6272A4",
        accent="#BD93F9",
        good="bold #50FA7B",
        warn="bold #FFB86C",
        bad="bold #FF5555",
        info="#F1FA8C",
        highlight="#FF79C6",
    ),
    _theme(
        "gruvbox",
        "gruvbox dark: retro groove, warm yellows, oranges and aquas",
        title="bold #FABD2F",
        subtitle="bold #FE8019",
        border="#7C6F64",
        label="#83A598",
        value="#EBDBB2",
        muted="#928374",
        accent="#8EC07C",
        good="bold #B8BB26",
        warn="bold #FABD2F",
        bad="bold #FB4934",
        info="#83A598",
        highlight="#D3869B",
    ),
    _theme(
        "catppuccin",
        "Catppuccin Mocha: soothing pastels on a deep base",
        title="bold #CBA6F7",
        subtitle="bold #B4BEFE",
        border="#6C7086",
        label="#89B4FA",
        value="#CDD6F4",
        muted="#7F849C",
        accent="#94E2D5",
        good="bold #A6E3A1",
        warn="bold #FAB387",
        bad="bold #F38BA8",
        info="#89DCEB",
        highlight="#F5C2E7",
    ),
    _theme(
        "solarized",
        "Solarized dark: Ethan Schoonover's precise low-contrast palette",
        title="bold #268BD2",
        subtitle="bold #6C71C4",
        border="#586E75",
        label="#2AA198",
        value="#93A1A1",
        muted="#586E75",
        accent="#268BD2",
        good="bold #859900",
        warn="bold #B58900",
        bad="bold #DC322F",
        info="#6C71C4",
        highlight="#D33682",
    ),
    _theme(
        "tokyonight",
        "Tokyo Night: neon blues and purples of a city at night",
        title="bold #7AA2F7",
        subtitle="bold #BB9AF7",
        border="#545C7E",
        label="#7DCFFF",
        value="#C0CAF5",
        muted="#565F89",
        accent="#BB9AF7",
        good="bold #9ECE6A",
        warn="bold #E0AF68",
        bad="bold #F7768E",
        info="#73DACA",
        highlight="#FF9E64",
    ),
    _theme(
        "monokai",
        "Monokai: the classic vivid editor palette",
        title="bold #A6E22E",
        subtitle="bold #E6DB74",
        border="#75715E",
        label="#66D9EF",
        value="#F8F8F2",
        muted="#75715E",
        accent="#AE81FF",
        good="bold #A6E22E",
        warn="bold #FD971F",
        bad="bold #F92672",
        info="#E6DB74",
        highlight="#F92672",
    ),
    _theme(
        "pihole",
        "Pi-hole brand: logo reds and leaf green with the web dashboard's box colors",
        title="bold #FF0000",
        subtitle="bold #12B212",
        border="#980200",
        label="#B8C7CE",
        value="#FFFFFF",
        muted="#8AA4AF",
        accent="#00C0EF",
        good="bold #00A65A",
        warn="bold #F39C12",
        bad="bold #DD4B39",
        info="#3C8DBC",
        highlight="#DD4B39",
    ),
    _theme(
        "mono",
        "no colors at all, only bold, dim, italic and underline",
        title="bold underline",
        subtitle="bold",
        border="dim",
        label="normal",
        value="bold",
        muted="dim",
        accent="bold",
        good="bold",
        warn="bold underline",
        bad="bold reverse",
        info="italic",
        highlight="bold italic",
    ),
)

_BY_NAME: dict[str, Theme] = {theme.name: theme for theme in _THEMES}


def available() -> tuple[Theme, ...]:
    """Return every built-in theme, ``default`` first, then alphabetically."""
    rest = sorted((t for t in _THEMES if t.name != "default"), key=lambda t: t.name)
    return (_BY_NAME["default"], *rest)


def _did_you_mean(word: str, choices: list[str]) -> str:
    """A hint suggesting the closest of ``choices`` and listing all of them."""
    from difflib import get_close_matches  # noqa: PLC0415 - only needed on errors

    close = get_close_matches(word, choices, n=1, cutoff=0.6)
    listing = ", ".join(choices)
    if close:
        return f"did you mean {close[0]!r}? Choose one of: {listing}"
    return f"choose one of: {listing}"


def get(name: str) -> Theme:
    """Return the built-in theme called ``name``.

    Raises:
        ConfigError: no such theme (the hint suggests the closest name).
    """
    key = name.strip().lower()
    try:
        return _BY_NAME[key]
    except KeyError:
        names = [t.name for t in available()]
        raise ConfigError(f"unknown theme {name!r}", hint=_did_you_mean(key, names)) from None


def _expand(key: str, words: str, base: Mapping[str, str]) -> str:
    """Validate override ``words`` and expand ``@name`` references from ``base``.

    Raises:
        ConfigError: brackets/braces, an unknown ``@name`` or invalid style words.
    """
    if any(char in words for char in "[]{}"):
        raise ConfigError(
            f"invalid style for {key!r}: {words!r}",
            hint="write only style words, e.g. 'bold #ff8800', without [ ] or { }",
        )
    for ref in _REFERENCE.findall(words):
        if ref not in STYLE_NAMES:
            raise ConfigError(
                f"invalid style for {key!r}: unknown theme style '@{ref}'",
                hint=_did_you_mean(ref, list(STYLE_NAMES)),
            )
    expanded = _REFERENCE.sub(lambda match: base[match.group(1)], words)
    # The template's style parser is the single authority on style syntax.
    from pihello.template.style import StyleError, parse_style, split_words  # noqa: PLC0415

    try:
        parse_style(expanded)
    except StyleError as error:
        raise ConfigError(
            f"invalid style for {key!r}: {error}",
            hint=error.hint or "see 'pihello colors' for color names, e.g. 'bold #ff8800'",
        ) from None
    return " ".join(split_words(expanded))


def resolve(name: str, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the styles of theme ``name`` with ``overrides`` applied.

    ``overrides`` (the ``[styles]`` config table) may only use names from
    :data:`STYLE_NAMES`. An override may refer to the *base* theme's styles
    with ``@name`` (``accent = "@good underline"``); those references are
    expanded here, so the result only contains concrete style words.

    Raises:
        ConfigError: unknown theme, unknown style name, or invalid style words.
    """
    base = get(name).styles
    styles = dict(base)
    for key, words in (overrides or {}).items():
        if key not in STYLE_NAMES:
            raise ConfigError(
                f"unknown style name {key!r}",
                hint=_did_you_mean(key, list(STYLE_NAMES)),
            )
        if not isinstance(words, str):
            raise ConfigError(
                f"style {key!r} must be a string of style words, not {type(words).__name__}"
            )
        styles[key] = _expand(key, words, base)
    return styles
