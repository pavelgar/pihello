"""Tests for the built-in presets (:mod:`pihello.presets`)."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from helpers.fakepihole import FakePihole, available_fixtures
from pihello import demo, presets, sources, themes
from pihello.client import PiholeClient
from pihello.errors import ConfigError
from pihello.template import ColorDepth, RenderOptions, Template, strip_ansi, visible_width

REQUIRED = (
    "default",
    "classic",
    "minimal",
    "compact",
    "full",
    "dashboard",
    "box",
    "fetch",
    "system",
    "top",
    "history",
    "updates",
    "status",
    "plain",
    "emoji",
)
#: pihello 1.x ``DEFAULT_CONTENT``, verbatim.
CLASSIC_1X = """\
[cyan2]─────────────────────────────────────────────────────[]
[white]PiHole[] ([green4]{blocking}[]) [lightgreen]{version.core.local.version}[white], Web [lightgreen]{version.web.local.version}[white], FTL [lightgreen]{version.ftl.local.version}
[cyan2]─────────────────────────────────────────────────────[]
Blocking [darkcyan]{gravity.domains_being_blocked}[] domains for [steelblue]{clients.active}[] clients
Blocked [fuchsia]{queries.blocked}[] out of [lightgreen]{queries.total}[] queries [underline]today[] ([steelblue]{queries.percent_blocked}%[])
[grey37]Gravity last updated [bold grey50]{gravity.relative.days}[grey37] days [bold grey50]{gravity.relative.hours}[grey37] hours and [bold grey50]{gravity.relative.minutes}[grey37] minutes ago"""  # noqa: E501

ALL_PRESETS = presets.available()
PRESET_IDS = [preset.name for preset in ALL_PRESETS]
ALL_THEMES = themes.available()
THEME_IDS = [theme.name for theme in ALL_THEMES]
COMMENT_LINE = re.compile(r"^\s*(\{#.*?#\}\s*)+$")
STYLE_TAG = re.compile(r"(?<!\\)\[([^\[\]]*)\]")


def _render(
    preset: presets.Preset,
    variables: Mapping[str, Any] | None = None,
    *,
    theme: str = "default",
    color: ColorDepth = ColorDepth.NONE,
    strict: bool = True,
) -> str:
    template = Template.parse(preset.source, name=f"preset:{preset.name}")
    options = RenderOptions(color=color, strict=strict, now=demo.NOW)
    data = demo.variables() if variables is None else variables
    return template.render(data, theme=themes.resolve(theme), options=options)


def _source_without_comments(source: str) -> str:
    return "\n".join(line for line in source.split("\n") if not COMMENT_LINE.match(line))


def _resolves(variables: Mapping[str, Any], path: str) -> bool:
    """Whether ``path`` exists in ``variables`` (the value itself may be ``None``)."""
    node: Any = variables
    for key in path.split("."):
        if isinstance(node, Mapping) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return False
    return True


def _sparse(variables: Mapping[str, Any]) -> dict[str, Any]:
    """Demo data as a minimal Pi-hole would report it: no sensors, no lists."""
    data = copy.deepcopy(dict(variables))
    for name in ("network", "messages", "database", "ftl"):
        data.pop(name, None)
    for name in (
        "top_domains",
        "top_blocked",
        "top_clients",
        "top_blocked_clients",
        "upstreams",
        "recent_blocked_list",
    ):
        data[name] = []
    data["recent_blocked"] = ""
    data["timer"] = None
    data["sensors"] = {"list": [], "cpu_temp": None, "hot_limit": None, "unit": "C"}
    data["host"]["model"] = None
    data["version"]["docker"] = {"local": None, "remote": None}
    return data


def _disabled(variables: Mapping[str, Any]) -> dict[str, Any]:
    """Demo data with blocking disabled by a timer and every update available."""
    data = copy.deepcopy(dict(variables))
    data["blocking"] = "disabled"
    data["timer"] = 299.5
    for component in ("core", "web", "ftl"):
        data["version"][component]["update"] = True
    data["updates"] = {
        "available": True,
        "count": 3,
        "components": ["core", "web", "ftl"],
        "status": "available",
    }
    return data


def _long_host(variables: Mapping[str, Any]) -> dict[str, Any]:
    """Demo data as a real Pi-hole reported it: a long host name, two-digit durations."""
    data = copy.deepcopy(dict(variables))
    data["pihole"]["host"] = "pihole-1.home.example.net"
    data["gravity"]["relative"] = {"days": 99, "hours": 23, "minutes": 59}
    data["gravity"]["last_update"] = demo.NOW - (99 * 86400 + 23 * 3600 + 59 * 60)
    return data


def _long_values(variables: Mapping[str, Any]) -> dict[str, Any]:
    """Demo data with unusually long strings everywhere."""
    data = _disabled(variables)
    data["pihole"]["host"] = "pihole-primary.home.example.internal"
    data["recent_blocked"] = "a-very-long-tracking-subdomain.telemetry.example-analytics.com"
    data["blocking"] = "failed"
    data["timer"] = 86399
    for component in ("core", "web", "ftl"):
        data["version"][component]["local"]["version"] = "vDev-1234567890abcdef"
    return data


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #
def test_required_presets_exist() -> None:
    assert set(REQUIRED) <= set(PRESET_IDS)


def test_available_lists_default_first_then_alphabetical() -> None:
    assert PRESET_IDS[0] == "default"
    assert PRESET_IDS[1:] == sorted(PRESET_IDS[1:])
    assert presets.names() == tuple(PRESET_IDS)


def test_every_template_file_is_registered() -> None:
    files = {path.stem for path in Path(presets.__file__).parent.glob("*.txt")}
    assert files == set(PRESET_IDS)


def test_get_returns_cached_presets() -> None:
    assert presets.get("box") is presets.get("box")
    assert presets.get(" Default ") is presets.get("default")


def test_get_unknown_preset_suggests_the_closest_name() -> None:
    with pytest.raises(ConfigError, match="unknown preset 'dashbord'") as excinfo:
        presets.get("dashbord")
    assert excinfo.value.hint is not None
    assert "did you mean 'dashboard'" in excinfo.value.hint


def test_get_unknown_preset_without_close_match_lists_all() -> None:
    with pytest.raises(ConfigError) as excinfo:
        presets.get("qqqqqqqq")
    assert excinfo.value.hint is not None
    assert "did you mean" not in excinfo.value.hint
    assert "minimal" in excinfo.value.hint


@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_preset_metadata(preset: presets.Preset) -> None:
    assert preset.description
    assert not preset.description.endswith(".")
    assert 10 <= preset.min_width <= 120
    assert preset.source.startswith("{#"), "start every preset with a {# comment #} header"
    assert preset.source.endswith("\n")
    assert "\t" not in preset.source


# --------------------------------------------------------------------------- #
# Parsing and variables                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_preset_paths_use_known_namespaces(preset: presets.Preset) -> None:
    template = Template.parse(preset.source, name=preset.name)
    paths = template.paths()
    assert paths
    unknown = {path for path in paths if path.split(".")[0] not in sources.NAMESPACES}
    assert not unknown


@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_preset_paths_exist_in_demo_data(preset: presets.Preset) -> None:
    """Catches typos hidden behind ``default(...)``, which never fail strictly."""
    variables = demo.variables()
    paths = Template.parse(preset.source, name=preset.name).paths()
    assert sorted(p for p in paths if not _resolves(variables, p)) == []


@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_preset_only_uses_theme_styles(preset: presets.Preset) -> None:
    source = _source_without_comments(preset.source)
    references = set(re.findall(r"@([A-Za-z0-9_-]+)", source))
    assert references <= set(themes.STYLE_NAMES)
    if preset.name == "classic":
        assert not references, "classic is the fixed-color 1.x look"
        return
    for tag in STYLE_TAG.findall(source):
        words = re.sub(r"\{[^{}]*\}", "", tag).split()
        literal = [w for w in words if not w.startswith("@") and w != "reverse"]
        assert not literal, f"[{tag}] uses fixed styles; use @names so themes apply"


def test_classic_is_exactly_the_1x_default() -> None:
    source = presets.get("classic").source
    assert _source_without_comments(source).rstrip("\n") == CLASSIC_1X


def test_classic_renders_like_1x() -> None:
    text = _render(presets.get("classic"))
    variables = demo.variables()
    lines = text.split("\n")
    assert lines[0] == "─" * 53
    assert lines[1].startswith(f"PiHole ({variables['blocking']}) ")
    assert f"Blocked {variables['queries']['blocked']} out of " in lines[4]
    assert lines[5].startswith("Gravity last updated ")


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", THEME_IDS)
@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_preset_renders_strictly_with_every_theme(preset: presets.Preset, theme: str) -> None:
    for depth in ColorDepth:
        text = _render(preset, theme=theme, color=depth)
        assert strip_ansi(text).strip()
        if depth is ColorDepth.NONE:
            assert "\x1b" not in text


@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_preset_has_no_missing_values(preset: presets.Preset) -> None:
    text = _render(preset, strict=False)
    marker = "⁇MISSING⁇"
    template = Template.parse(preset.source)
    options = RenderOptions(color=ColorDepth.NONE, missing=marker, now=demo.NOW)
    assert marker not in template.render(
        demo.variables(), theme=themes.resolve("default"), options=options
    )
    assert text == _render(preset)


@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_preset_theme_changes_the_output(preset: presets.Preset) -> None:
    default = _render(preset, color=ColorDepth.TRUECOLOR)
    nord = _render(preset, theme="nord", color=ColorDepth.TRUECOLOR)
    assert strip_ansi(default) == strip_ansi(nord)
    if preset.name == "classic":
        assert default == nord
    else:
        assert default != nord


@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_min_width_is_honest(preset: presets.Preset) -> None:
    # Typical data, the widest realistic state (disabled on a timer, updates
    # pending), and the longer values real Pi-holes report (host name, durations).
    lines = [
        *_render(preset).split("\n"),
        *_render(preset, _disabled(demo.variables())).split("\n"),
        *_render(preset, _long_host(demo.variables())).split("\n"),
    ]
    widest = max(visible_width(line) for line in lines)
    assert widest <= preset.min_width, f"widest line is {widest} columns"
    assert preset.min_width - widest <= 6, f"min_width could be {widest}"


@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_no_trailing_whitespace(preset: presets.Preset) -> None:
    for line in _render(preset).split("\n"):
        assert line == line.rstrip() or preset.name == "fetch", repr(line)


@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
@pytest.mark.parametrize("variant", [_sparse, _disabled, _long_values])
def test_preset_degrades_gracefully(preset: presets.Preset, variant: Any) -> None:
    """Missing optional data never breaks strict rendering."""
    text = _render(preset, variant(demo.variables()))
    assert strip_ansi(text).strip()


def test_default_shows_the_disable_timer_and_update_hint() -> None:
    text = _render(presets.get("default"), _disabled(demo.variables()))
    first = text.split("\n")[0]
    assert "● disabled for 4m" in first
    assert "↑ updates" in first


@pytest.mark.parametrize(("name", "ellipsis"), [("default", "…"), ("plain", "...")])
def test_long_host_names_are_shortened(name: str, ellipsis: str) -> None:
    # Found on a real Pi-hole: a 20-character host name pushed the first line past
    # min_width. The greeting presets fit even with the Pi-hole disabled as well.
    preset = presets.get(name)
    text = _render(preset, _long_host(_disabled(demo.variables())))
    first = text.split("\n")[0]
    host = "pihole-1.home.example.net"
    assert host[: 16 - len(ellipsis)] + ellipsis in first  # 16 columns, ellipsis included
    assert "pihole-1.home.example.net" not in first
    assert max(visible_width(line) for line in text.split("\n")) <= preset.min_width


def _no_update_info(variables: Mapping[str, Any]) -> dict[str, Any]:
    """What a Pi-hole that could not reach GitHub reports: no remote versions."""
    data = copy.deepcopy(dict(variables))
    for component in ("core", "web", "ftl", "docker"):
        data["version"][component]["update"] = None
    data["updates"] = {"available": False, "count": 0, "components": [], "status": "unknown"}
    return data


@pytest.mark.parametrize(
    ("name", "unknown"),
    [
        ("updates", "Update status unknown."),
        ("full", "Updates     unknown"),
        ("emoji", "❔ update status unknown"),
    ],
)
def test_unknown_update_status_is_not_up_to_date(name: str, unknown: str) -> None:
    """Regression: these presets said "up to date" when Pi-hole had no update information."""
    text = strip_ansi(_render(presets.get(name), _no_update_info(demo.variables())))
    assert unknown in text
    assert "up to date" not in text
    current = _no_update_info(demo.variables())
    current["updates"]["status"] = "current"
    assert "up to date" in strip_ansi(_render(presets.get(name), current))
    missing = _no_update_info(demo.variables())
    del missing["updates"], missing["version"]  # info/version failed
    assert unknown in strip_ansi(_render(presets.get(name), missing, strict=False))


@pytest.mark.parametrize("name", ["status", "minimal"])
def test_single_line_presets(name: str) -> None:
    for variant in (demo.variables(), _disabled(demo.variables()), _sparse(demo.variables())):
        text = _render(presets.get(name), variant)
        assert "\n" not in text
        assert text == text.strip()


def test_status_is_ultra_short() -> None:
    assert visible_width(_render(presets.get("status"))) <= 24


@pytest.mark.parametrize("theme", THEME_IDS)
def test_plain_is_ascii_only(theme: str) -> None:
    preset = presets.get("plain")
    assert _source_without_comments(preset.source).isascii()
    for variant in (demo.variables(), _disabled(demo.variables()), _sparse(demo.variables())):
        for depth in ColorDepth:
            assert strip_ansi(_render(preset, variant, theme=theme, color=depth)).isascii()


@pytest.mark.parametrize("variant", [demo.variables, _sparse, _disabled, _long_values])
def test_box_borders_line_up(variant: Any) -> None:
    data = variant() if variant is demo.variables else variant(demo.variables())
    lines = _render(presets.get("box"), data).split("\n")
    widths = {visible_width(line) for line in lines}
    assert len(widths) == 1, widths
    assert lines[0].startswith("╭")
    assert lines[0].endswith("╮")
    assert lines[-1].startswith("╰")
    assert lines[-1].endswith("╯")
    assert all(line[0] in "│├" and line[-1] in "│┤" for line in lines[1:-1])


@pytest.mark.parametrize(
    "missing",
    [("blocking",), ("version", "updates"), ("queries", "clients", "gravity"), ("recent_blocked",)],
)
def test_box_borders_line_up_when_data_is_missing(missing: tuple[str, ...]) -> None:
    """Regression: an unavailable endpoint printed a 1-column ``?`` where the box
    expected a padded value, so the right border no longer lined up."""
    data = _disabled(demo.variables())
    for name in missing:
        del data[name]
    lines = _render(presets.get("box"), data, strict=False).split("\n")
    assert {visible_width(line) for line in lines} == {presets.get("box").min_width}


@pytest.mark.parametrize("missing", [(), ("system",), ("queries",), ("gravity",), ("network",)])
def test_dashboard_columns_line_up_when_data_is_missing(missing: tuple[str, ...]) -> None:
    """Regression: a missing left-column value printed as ``?`` without padding,
    which glued the right column to it (``Uptime     ?Load``)."""
    data = demo.variables()
    for name in missing:
        del data[name]
    lines = _render(presets.get("dashboard"), data, strict=False).split("\n")
    right = ("Clients", "Blocked    ", "Address", "Gateway", "Load", "CPU temp")
    rows = [(line, label) for line in lines for label in right if f" {label}" in line[11:]]
    assert len(rows) == 6
    assert all(line.index(label, 11) == 32 for line, label in rows), rows


def test_box_borders_line_up_with_a_long_disable_timer() -> None:
    data = _disabled(demo.variables())
    data["timer"] = 400 * 86400 + 23 * 3600  # "for 400d 23h" is wider than its column
    lines = _render(presets.get("box"), data).split("\n")
    assert {visible_width(line) for line in lines} == {presets.get("box").min_width}


@pytest.mark.parametrize("variant", [demo.variables, _sparse, _long_values])
def test_fetch_logo_column_lines_up(variant: Any) -> None:
    data = variant() if variant is demo.variables else variant(demo.variables())
    lines = _render(presets.get("fetch"), data).split("\n")
    info = [line[30:] for line in lines if len(line) > 30]
    assert len(info) >= 15
    assert all(not part.startswith(" ") for part in info)


@pytest.mark.parametrize("variant", [demo.variables, _sparse, _long_values])
def test_top_columns_line_up(variant: Any) -> None:
    data = variant() if variant is demo.variables else variant(demo.variables())
    lines = [line for line in _render(presets.get("top"), data).split("\n") if line.strip()]
    assert len(lines) == 12
    assert all(visible_width(line) <= presets.get("top").min_width for line in lines)
    for line in lines:
        if not line.startswith("Top"):
            assert line[33:35] in {" 1", " 2", " 3", " 4", " 5"}, line


# --------------------------------------------------------------------------- #
# Real Pi-hole responses                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", params=[*available_fixtures(), "demo"])
def collected(request: pytest.FixtureRequest) -> tuple[str, dict[str, Any]]:
    """Variables collected through the real client from a recorded Pi-hole."""
    tag: str = request.param
    with FakePihole(tag) as server, PiholeClient(server.url, server.password) as client:
        variables, _problems = sources.collect(client, now=demo.NOW)
    return tag, variables


@pytest.mark.parametrize("preset", ALL_PRESETS, ids=PRESET_IDS)
def test_preset_renders_against_recorded_pihole(
    preset: presets.Preset, collected: tuple[str, dict[str, Any]]
) -> None:
    """Every preset renders strictly (no missing values) on every recorded Pi-hole.

    Data that older versions lack (``system.ftl``, sensors, remote versions …)
    must be guarded with ``default(...)`` in the presets. The demo payloads,
    served through the same client and sources code, render exactly like the
    demo variables.
    """
    tag, variables = collected
    for theme in ("default", "mono"):
        text = _render(preset, variables, theme=theme)
        assert strip_ansi(text).strip()
    if tag == "demo":
        served = {**variables, "pihole": demo.variables()["pihole"]}  # 127.0.0.1 → pi.hole
        assert _render(preset, served) == _render(preset)
