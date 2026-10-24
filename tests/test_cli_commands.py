"""``pihello presets``, ``themes``, ``vars`` and ``colors``."""

from __future__ import annotations

import io
import json
import re
from typing import TYPE_CHECKING

import pytest

from pihello import cli, config, demo, presets, sources, themes
from pihello.cli import format_value, lookup, main

if TYPE_CHECKING:
    from pathlib import Path

    from helpers.fakepihole import FakePihole

PASSWORD = "pihello-test"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def configured(fake_pihole: FakePihole) -> FakePihole:
    path = config.default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(f'url = "{fake_pihole.url}"\npassword = "{PASSWORD}"\n')
    path.chmod(0o600)
    return fake_pihole


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# presets                                                                     #
# --------------------------------------------------------------------------- #
def test_presets_list(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["presets", "--no-preview"], capsys)
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == len(presets.available())
    assert lines[0].startswith("default")
    for preset, line in zip(presets.available(), lines, strict=True):
        assert line.split()[0] == preset.name
        assert f"{preset.min_width} cols" in line
        assert preset.description in line


def test_presets_previews_with_demo_data(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["presets"], capsys)
    assert code == 0, err
    for preset in presets.available():
        assert f"\n{preset.name}  " in "\n" + out
    assert "cannot render" not in out
    assert not ANSI.search(out)


def test_preset_single_with_theme_and_color(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["presets", "default", "-T", "nord", "--color", "always"], capsys)
    assert code == 0
    assert out.startswith("\x1b[1mdefault\x1b[0m")  # --color always: headings too
    assert ANSI.search(out)
    from pihello import presets

    assert f"min. {presets.get('default').min_width} columns" in out


def test_preset_too_wide_note(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["presets", "classic", "-W", "40"], capsys)
    assert "wider than your 40" in out


def test_preset_source(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["presets", "classic", "--source"], capsys)
    assert code == 0
    assert out == presets.get("classic").source.rstrip("\n") + "\n" or out.startswith(
        presets.get("classic").source
    )
    code, _, err = run(["presets", "--source"], capsys)
    assert code == 2
    assert "needs a preset NAME" in err


def test_preset_source_to_a_new_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "new" / "folder" / "greeting.txt"
    code, out, _ = run(["presets", "default", "--source", "-o", str(target)], capsys)
    assert code == 0
    assert out == f"wrote {target}\n"
    data = target.read_bytes()
    assert data.decode("utf-8") == presets.get("default").source  # UTF-8 everywhere
    assert b"\r\n" not in data
    # It never overwrites, e.g. a template you have already customised.
    target.write_text("mine")
    code, _, err = run(["presets", "default", "--source", "-o", str(target)], capsys)
    assert code == 2
    assert "already exists" in err
    assert target.read_text() == "mine"


def test_output_needs_source(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["presets", "default", "-o", "x.txt"], capsys)
    assert code == 2
    assert "--output only works with --source" in err


def test_redirected_preset_source_is_utf8(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Windows redirects stdout as cp1252, which can't hold "●"; `> file` must
    # still produce the UTF-8 file that templates are read as.
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr(cli, "_stdout", lambda: stream)
    assert main(["presets", "default", "--source"]) == 0
    stream.flush()
    # Windows text streams write \r\n; templates accept either line ending.
    text = raw.getvalue().decode("utf-8").replace("\r\n", "\n")
    assert text == presets.get("default").source


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_template_files_in_utf8_or_utf16(
    encoding: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Windows PowerShell 5.1 writes `>` redirections as UTF-16 with a BOM.
    template = tmp_path / "greeting.txt"
    template.write_text("● {queries.total|number}\n", encoding=encoding)
    code, out, err = run(["--demo", "--color", "never", "-f", str(template)], capsys)
    assert code == 0, err
    assert out == "● 48,220\n"


def test_unknown_preset(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["presets", "nope"], capsys)
    assert code == 2
    assert "unknown preset" in err


def test_presets_live(configured: FakePihole, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["presets", "minimal", "--live"], capsys)
    assert code == 0, err
    assert out.startswith("minimal")
    assert not configured.active_sessions


def test_preset_render_error_is_inline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pihello.errors import TemplateError
    from pihello.template import Template

    def broken(*_args: object, **_kwargs: object) -> str:
        raise TemplateError("boom")

    monkeypatch.setattr(Template, "render", broken)
    code, out, _ = run(["presets", "default"], capsys)
    assert code == 0
    assert "cannot render: <template>: boom" in out


# --------------------------------------------------------------------------- #
# themes                                                                      #
# --------------------------------------------------------------------------- #
def test_themes_list_with_swatches(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["themes", "--no-preview"], capsys)
    assert code == 0, err
    for theme in themes.available():
        assert f"{theme.name}  {theme.description}" in out
    assert out.count("title subtitle border label") == len(themes.available())


def test_theme_preview(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")  # a depth alone no longer colors a pipe
    code, out, _ = run(["themes", "nord", "--preset", "minimal", "--color", "256"], capsys)
    assert code == 0
    assert out.startswith("\x1b[1mnord") or out.startswith("nord")
    assert ANSI.search(out)


def test_themes_use_configured_preset(capsys: pytest.CaptureFixture[str]) -> None:
    path = config.default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("preset = 'classic'\n")
    _, out, _ = run(["themes", "default"], capsys)
    assert "Gravity last updated" in out


def test_theme_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["themes", "nrod"], capsys)
    assert code == 2
    assert "did you mean 'nord'" in err


def test_theme_render_error_is_inline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pihello.errors import TemplateError
    from pihello.template import Template

    real = Template.render

    def flaky(self: Template, *args: object, **kwargs: object) -> str:
        if self.source_name == "swatches":
            return real(self, *args, **kwargs)  # type: ignore[arg-type]
        raise TemplateError("boom")

    monkeypatch.setattr(Template, "render", flaky)
    _, out, _ = run(["themes", "default"], capsys)
    assert "cannot render" in out


# --------------------------------------------------------------------------- #
# vars                                                                        #
# --------------------------------------------------------------------------- #
def test_vars_demo_table(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["vars", "--demo"], capsys)
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].split() == ["PATH", "VALUE", "DESCRIPTION"]
    paths = [line.split()[0] for line in lines[1:]]
    assert paths == [info.path for info in sources.catalog()]
    assert any(line.startswith("blocking ") and '"enabled"' in line for line in lines)


def test_vars_filter_substring_and_glob(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["vars", "--demo", "GRAVITY"], capsys)
    paths = [line.split()[0] for line in out.splitlines()[1:]]
    assert paths
    assert all("gravity" in p.lower() for p in paths)
    _, out, _ = run(["vars", "--demo", "queries.*"], capsys)
    paths = [line.split()[0] for line in out.splitlines()[1:]]
    assert all(p.startswith("queries.") for p in paths)
    code, _, err = run(["vars", "--demo", "zzz*"], capsys)
    assert code == 2
    assert "no variables match 'zzz*'" in err


def test_vars_all(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["vars", "--demo", "--all", "top_domains.*"], capsys)
    paths = [line.split()[0] for line in out.splitlines()[1:]]
    assert "top_domains.0.domain" in paths
    assert "top_domains.1.domain" in paths
    flat = sources.flatten(demo.variables())
    assert len(paths) == sum(1 for p in flat if p.startswith("top_domains."))


def test_vars_json_tree_and_flat(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["vars", "--demo", "--json"], capsys)
    assert json.loads(out) == json.loads(json.dumps(demo.variables(), default=str))
    _, out, _ = run(["vars", "--demo", "--json", "queries.total"], capsys)
    assert json.loads(out) == {"queries.total": demo.variables()["queries"]["total"]}


@pytest.mark.usefixtures("posix_terminal")
def test_vars_tty_colors_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    from pihello import terminal

    stream = TTY()
    monkeypatch.setattr(cli, "_stdout", lambda: stream)
    monkeypatch.setattr(terminal, "terminal_size", lambda _stream=None: (70, 24))
    assert main(["vars", "--demo", "gravity"]) == 0
    text = stream.getvalue()
    assert "\x1b[1mPATH" in text
    assert all(len(ANSI.sub("", line)) <= 70 for line in text.splitlines())


def test_vars_live(configured: FakePihole, capsys: pytest.CaptureFixture[str]) -> None:
    configured.remove("info/sensors")
    code, out, err = run(["vars", "sensors"], capsys)
    assert code == 0
    assert "info/sensors unavailable" in err
    assert "—" in out
    assert not configured.active_sessions


def test_vars_live_unconfigured_suggests_demo(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["vars"], capsys)
    assert code == 2
    assert "--demo" in err


def test_format_value_and_lookup() -> None:
    assert format_value(None) == "—"
    assert format_value(True) == "true"
    assert format_value(False) == "false"
    assert format_value("x") == '"x"'
    assert format_value(1.23456789) == "1.23457"
    assert format_value([1]) == "[1 item]"
    assert format_value([1, 2]) == "[2 items]"
    assert format_value({"a": 1}) == "{1 keys}"
    assert format_value(7) == "7"
    assert format_value("y" * 100).endswith("…")
    tree = {"a": {"b": [{"c": 1}]}}
    assert lookup(tree, "a.b.N.c") == 1
    assert lookup(tree, "a.b.0.c") == 1
    assert lookup(tree, "a.b.5.c") is cli._MISSING
    assert lookup(tree, "a.x") is cli._MISSING
    assert lookup(tree, "a.b.c") is cli._MISSING


# --------------------------------------------------------------------------- #
# colors                                                                      #
# --------------------------------------------------------------------------- #
def test_colors(capsys: pytest.CaptureFixture[str]) -> None:
    from pihello.template.color_tables import COLOR_NAMES

    code, out, _ = run(["colors", "-W", "200"], capsys)
    assert code == 0
    for name in COLOR_NAMES:
        assert name in out
    assert "Basic colors (0-15)" in out
    assert "#4682b4" in out or "#5f87af" in out  # steelblue
    assert not ANSI.search(out)


def test_colors_in_color(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")  # a depth alone no longer colors a pipe
    _, out, _ = run(["colors", "--color", "256", "-W", "40"], capsys)
    assert re.search(r"\x1b\[[0-9;]*48;5;\d+m", out)
    widest = max(len(ANSI.sub("", line)) for line in out.splitlines()[:-1])
    assert widest <= 60


@pytest.mark.parametrize(
    "argv", [["presets", "--no-preview"], ["vars", "--demo", "gravity"], ["doctor"]]
)
def test_color_option_applies_to_pihellos_own_text(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # same rules as for the greeting (terminal.detect_color_depth)
    _, out, _ = run([*argv, "--color", "always"], capsys)  # into a pipe
    assert ANSI.search(out)
    monkeypatch.setenv("FORCE_COLOR", "1")
    _, out, _ = run([*argv, "--color", "never"], capsys)
    assert not ANSI.search(out)


def test_themes_help_names_the_real_preview_default(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["themes", "--help"], capsys)
    assert "(default: default)" not in out
    assert "configured preset" in out


def test_vars_without_matches_is_an_error_not_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["vars", "--demo", "zzz*"], capsys)
    assert (code, out) == (2, "")
    assert "warning" not in err
    assert err.startswith("pihello: no variables match 'zzz*'")
    assert "hint:" in err


def test_presets_listing_ends_with_a_hint(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["presets"], capsys)
    last = out.rstrip("\n").splitlines()[-1]
    assert "pihello presets NAME" in last
    assert "--no-preview" in last
    for argv in (["presets", "default"], ["presets", "--no-preview"]):
        _, out, _ = run(argv, capsys)
        assert "pihello presets NAME" not in out


#: Pi-hole data any LAN client can plant (a DNS query for a crafted name lands
#: in top_domains): OSC title, CSI clear, a C1 CSI, bidi override and isolate.
EVIL = "evil\x1b]0;owned\x07\x1b[2J\u009b31m\u202egnp.exe\u2066x\u2069"
CONTROLS = ("\x1b", "\x07", "\u009b", "\u202e", "\u2066", "\u2069")


def _evil_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    data = demo.variables()
    data["top_domains"] = [{"domain": EVIL, "count": 3}]
    data["queries"]["types"] = {EVIL: 1}
    monkeypatch.setattr(demo, "variables", lambda: data)


def test_vars_table_shows_no_terminal_controls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _evil_demo(monkeypatch)
    for argv in (["top_domains"], ["--all", "top_domains"], ["--all", "queries.types"]):
        code, out, err = run(["vars", "--demo", *argv], capsys)
        assert code == 0, err
        assert "evil" in out
        for control in CONTROLS:
            assert control not in out, (argv, control)


def test_vars_json_escapes_c1_and_bidi_controls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _evil_demo(monkeypatch)
    for argv in ([], ["top_domains"], ["queries.types"]):
        code, out, err = run(["vars", "--demo", "--json", *argv], capsys)
        assert code == 0, err
        for control in CONTROLS:
            assert control not in out, (argv, control)
        assert "\\u009b" in out
        assert "\\u202e" in out
        data = json.loads(out)  # still valid JSON, with the exact data
        if not argv:
            assert data["top_domains"][0]["domain"] == EVIL
            assert EVIL in data["queries"]["types"]
        elif argv == ["top_domains"]:
            assert data["top_domains.0.domain"] == EVIL
        else:
            assert f"queries.types.{EVIL}" in data
    assert "ü" in cli._json_text({"x": "ü"})  # printable text stays readable
