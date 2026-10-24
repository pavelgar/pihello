"""``pihello`` / ``pihello show``: settings, templates, data, rendering, watch mode."""

from __future__ import annotations

import io
import re
import signal
import sys
from typing import TYPE_CHECKING, ClassVar

import pytest

from pihello import cli, config
from pihello.cli import main

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from helpers.fakepihole import FakePihole

PASSWORD = "pihello-test"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def cfg() -> Path:
    path = config.default_config_path()  # in the isolated HOME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def configured(cfg: Path, fake_pihole: FakePihole) -> FakePihole:
    """A config file pointing at the fake Pi-hole with the right password."""
    cfg.write_text(f'url = "{fake_pihole.url}"\npassword = "{PASSWORD}"\n')
    cfg.chmod(0o600)
    return fake_pihole


class TTY(io.StringIO):
    """A captured stdout that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# Demo data and templates                                                     #
# --------------------------------------------------------------------------- #
def test_demo_default_preset(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["--demo"], capsys)
    assert code == 0, err
    assert err == ""
    assert out.endswith("\n")
    assert not out.endswith("\n\n")
    assert len(out.splitlines()) >= 3
    assert not ANSI.search(out)  # captured stdout is not a terminal


def test_explicit_show_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["show", "--demo"], capsys)[1] == run(["--demo"], capsys)[1]


def test_demo_color_always(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = run(["--demo", "--color", "always"], capsys)
    assert ANSI.search(out)
    _, out, _ = run(["--demo", "--color", "truecolor", "-e", "[#ff0000]x"], capsys)
    assert out == "x\n"  # a depth only picks the colors; a pipe still gets none
    monkeypatch.setenv("FORCE_COLOR", "1")
    _, out, _ = run(["--demo", "--color", "truecolor", "-e", "[#ff0000]x"], capsys)
    assert "38;2;255;0;0" in out


def test_no_color_flag_and_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = TTY()
    monkeypatch.setattr(cli, "_stdout", lambda: stream)
    assert main(["--demo", "--no-color", "-e", "[red]x"]) == 0
    assert stream.getvalue() == "x\n"
    monkeypatch.setenv("NO_COLOR", "1")
    stream.seek(0)
    stream.truncate()
    assert main(["--demo", "-e", "[red]x"]) == 0
    assert stream.getvalue() == "x\n"


@pytest.mark.parametrize("name", ["default", "classic", "minimal", "status"])
def test_every_listed_preset_renders(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["--demo", "--preset", name, "--strict"], capsys)
    assert code == 0, err
    assert out.strip()


def test_template_string(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["--demo", "-e", "{queries.total|number} queries"], capsys)
    assert code == 0
    assert re.fullmatch(r"[\d,]+ queries\n", out)


def test_template_file_and_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    template = tmp_path / "t.txt"
    template.write_text("\ufeffblocking={blocking}\n", encoding="utf-8")
    assert run(["--demo", "-f", str(template)], capsys)[1] == "blocking=enabled\n"
    assert run(["--demo", "--template", str(template)], capsys)[1] == "blocking=enabled\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin {pihello.version}"))
    _, out, _ = run(["--demo", "-f", "-"], capsys)
    assert out.startswith("from stdin 2.")


def test_template_from_config_relative_path(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (cfg.parent / "greeting.txt").write_text("hi {blocking}")
    cfg.write_text("template = 'greeting.txt'\n")
    assert run(["--demo"], capsys)[1] == "hi enabled\n"


def test_template_file_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["--demo", "-f", str(tmp_path / "missing.txt")], capsys)
    assert code == 2
    assert "template file not found" in err
    code, _, err = run(["--demo", "-f", str(tmp_path)], capsys)
    assert code == 2
    assert "cannot read template file" in err


def test_template_syntax_error(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["--demo", "-e", "{queries.total|nosuchfilter}"], capsys)
    assert code == 2
    assert out == ""
    assert err.startswith("pihello: --template-string:")


def test_template_syntax_error_shows_excerpt(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["--demo", "-e", "Hi {queries.total|nubmer}"], capsys)
    assert code == 2
    lines = err.splitlines()
    assert lines[0] == "pihello: --template-string:1:19: unknown filter 'nubmer'"
    assert lines[1] == "    1 | Hi {queries.total|nubmer}"
    assert lines[2] == "      |                   ^"
    assert lines[3] == "hint: did you mean 'number'?"


def test_unknown_preset_and_theme(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["--demo", "--preset", "defualt"], capsys)
    assert code == 2
    assert "did you mean 'default'" in err
    code, _, err = run(["--demo", "-T", "nrod"], capsys)
    assert code == 2
    assert "unknown theme" in err


def test_preset_and_theme_from_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PIHELLO_PRESET", "classic")
    _, classic, _ = run(["--demo"], capsys)
    assert "Gravity last updated" in classic
    monkeypatch.setenv("PIHELLO_THEME", "nosuch")
    assert run(["--demo"], capsys)[0] == 2


def test_style_overrides(
    monkeypatch: pytest.MonkeyPatch, cfg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")  # a depth alone no longer colors a pipe
    cfg.write_text("[styles]\naccent = '#00ff00'\n")
    _, out, _ = run(["--demo", "--color", "truecolor", "-e", "[@accent]x"], capsys)
    assert "38;2;0;255;0" in out
    _, out, _ = run(
        ["--demo", "--color", "truecolor", "--style", "accent=#0000ff", "-e", "[@accent]x"], capsys
    )
    assert "38;2;0;0;255" in out
    code, _, err = run(["--demo", "--style", "nonsense=red", "-e", "x"], capsys)
    assert code == 2
    assert "unknown style name" in err


# --------------------------------------------------------------------------- #
# Output options                                                              #
# --------------------------------------------------------------------------- #
def test_width_wrap_and_clip(capsys: pytest.CaptureFixture[str]) -> None:
    text = "abcdefghij" * 3
    _, out, _ = run(["--demo", "-W", "10", "-e", text], capsys)
    assert out.splitlines() == ["abcdefghij"] * 3
    _, out, _ = run(["--demo", "-W", "10", "-c", "-e", text], capsys)
    assert out.splitlines() == ["abcdefghij"]
    _, out, _ = run(["--demo", "-W", "0", "-e", text], capsys)
    assert out.splitlines() == [text]


def test_width_from_config_and_wrap_flag(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg.write_text("width = 5\noverflow = 'clip'\n")
    assert run(["--demo", "-e", "abcdefgh"], capsys)[1] == "abcde\n"
    assert run(["--demo", "--wrap", "-e", "abcdefgh"], capsys)[1] == "abcde\nfgh\n"


def test_auto_width_uses_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from pihello import terminal

    stream = TTY()
    monkeypatch.setattr(cli, "_stdout", lambda: stream)
    monkeypatch.setattr(terminal, "terminal_size", lambda _stream=None: (8, 24))
    assert main(["--demo", "--no-color", "-e", "abcdefghijkl"]) == 0
    assert stream.getvalue() == "abcdefgh\nijkl\n"


def test_auto_width_is_unlimited_when_piped(capsys: pytest.CaptureFixture[str]) -> None:
    text = "x" * 500
    assert run(["--demo", "-e", text], capsys)[1] == text + "\n"


def test_height_and_tab_size(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["--demo", "-H", "2", "-e", "a\nb\nc"], capsys)
    assert out == "a\nb\n"
    _, out, _ = run(["--demo", "-i", "2", "-e", "\tx"], capsys)
    assert out == "  x\n"
    _, out, _ = run(["--demo", "--indent", "8", "-e", "\tx"], capsys)
    assert out == "        x\n"


def test_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["--demo", "--timestamp", "-e", "body"], capsys)
    first, second = out.splitlines()
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", first)
    assert second == "body"
    _, out, _ = run(["--demo", "-ts", "%Y!", "-e", "body"], capsys)
    assert re.fullmatch(r"\d{4}!\nbody\n", out)


def test_timestamp_formats_like_pihello_1x(capsys: pytest.CaptureFixture[str]) -> None:
    # 1.x used datetime.strftime, which knows %f (microseconds)
    _, out, _ = run(["--demo", "-ts", "%H:%M:%S.%f %z", "-e", "body"], capsys)
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{6} [+-]\d{4}\nbody\n", out)


def test_invalid_timestamp_format_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Windows' strftime raises ValueError for unknown directives; glibc doesn't.
    import datetime as real_datetime

    class Strict(real_datetime.datetime):
        def strftime(self, fmt: str) -> str:
            raise ValueError("Invalid format string")

    monkeypatch.setattr(real_datetime, "datetime", Strict)
    code, out, err = run(["--demo", "-ts", "%Q", "-e", "body"], capsys)
    assert code == 2
    assert out == ""
    assert "invalid --timestamp format '%Q'" in err


def test_timestamp_counts_towards_height(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg.write_text("timestamp = '[%Y]'\n")
    _, out, _ = run(["--demo", "-H", "2", "-e", "a\nb\nc"], capsys)
    assert out.splitlines()[1:] == ["a"]
    _, out, _ = run(["--demo", "-H", "1", "-e", "a\nb\nc"], capsys)
    assert len(out.splitlines()) == 1


def test_missing_placeholder_and_strict(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["--demo", "-e", "<{nope.x}>"], capsys)
    assert out == "<?>\n"
    _, out, _ = run(["--demo", "--missing", "n/a", "-e", "{nope.x}"], capsys)
    assert out == "n/a\n"
    code, _, err = run(["--demo", "--strict", "-e", "{nope.x}"], capsys)
    assert code == 2
    assert "nope.x" in err


def test_no_strict_overrides_config(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg.write_text("strict = true\n")
    assert run(["--demo", "-e", "{nope}"], capsys)[0] == 2
    assert run(["--demo", "--no-strict", "-e", "{nope}"], capsys)[0] == 0


# --------------------------------------------------------------------------- #
# Live data                                                                   #
# --------------------------------------------------------------------------- #
def test_live_render_from_config(
    configured: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(["-e", "{blocking} {queries.total}"], capsys)
    assert code == 0, err
    assert err == ""
    assert re.fullmatch(r"(enabled|disabled) \d+\n", out)
    assert not configured.active_sessions
    assert configured.requests_to("auth", "DELETE")


def test_only_needed_sources_are_fetched(
    configured: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["-e", "{blocking}"], capsys)[0] == 0
    endpoints = {r.endpoint for r in configured.requests if r.method == "GET"}
    assert endpoints == {"dns/blocking"}


def test_builtins_only_template_needs_no_connection(
    cfg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg.write_text('url = "http://127.0.0.1:9"\n')
    code, out, _ = run(["-e", "{pihole.host} {pihello.version}"], capsys)
    assert code == 0
    assert out.startswith("127.0.0.1 2.")


def test_password_sources(
    fake_pihole: FakePihole,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = fake_pihole.url
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD + "\n")
    assert run(["-u", url, "--password-file", str(pw), "-e", "{blocking}"], capsys)[0] == 0
    command = f"{sys.executable} -c \"print('{PASSWORD}')\""
    assert run(["-u", url, "--password-command", command, "-e", "{blocking}"], capsys)[0] == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    assert run(["-u", url, "--password-stdin", "-e", "{blocking}"], capsys)[0] == 0
    monkeypatch.setenv("PIHELLO_PASSWORD", PASSWORD)
    monkeypatch.setenv("PIHELLO_URL", url)
    assert run(["-e", "{blocking}"], capsys)[0] == 0
    monkeypatch.delenv("PIHELLO_PASSWORD")
    monkeypatch.setenv("PIHELLO_PASSWORD_FILE", str(pw))
    assert run(["-e", "{blocking}"], capsys)[0] == 0
    assert not fake_pihole.active_sessions


def test_profiles(cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]) -> None:
    cfg.write_text(
        'url = "http://127.0.0.1:9"\n'
        f'[profiles.fake]\nurl = "{fake_pihole.url}"\npassword = "{PASSWORD}"\n'
    )
    cfg.chmod(0o600)
    assert run(["-P", "fake", "-e", "{blocking}", "--timeout", "0.5"], capsys)[0] == 0
    assert run(["-e", "{blocking}", "--timeout", "0.5", "-q"], capsys)[0] == 3
    code, _, err = run(["-P", "nope"], capsys)
    assert code == 2
    assert "available profiles: fake" in err


def test_password_never_leaks_with_debug(
    configured: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(["--debug", "-e", "{blocking}"], capsys)
    assert code == 0
    assert "pihello: debug:" in err
    assert "ms" in err  # timings
    assert "dns/blocking" in err
    for session in configured.requests:
        if session.sid:
            assert session.sid not in err
    assert PASSWORD not in out
    assert PASSWORD not in err


def test_wrong_password(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg.write_text(f'url = "{fake_pihole.url}"\npassword = "wrong-pw"\n')
    cfg.chmod(0o600)
    code, out, err = run([], capsys)
    assert code == 4
    assert out == ""
    assert err.startswith("pihello: Pi-hole rejected the password")
    assert "hint:" in err
    assert "wrong-pw" not in err
    assert run(["-q"], capsys) == (4, "", "")
    assert not fake_pihole.active_sessions


def test_totp_needs_app_password(
    cfg: Path, fake_pihole_factory: Callable[..., FakePihole], capsys: pytest.CaptureFixture[str]
) -> None:
    server = fake_pihole_factory(totp=True)
    cfg.write_text(f'url = "{server.url}"\npassword = "{PASSWORD}"\n')
    code, _, err = run([], capsys)
    assert code == 4
    assert "app password" in err.lower()


def test_unreachable_is_quiet_in_shell_greetings(
    cfg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg.write_text('url = "http://127.0.0.1:9"\npassword = "x"\n')
    cfg.chmod(0o600)
    # Windows retries a refused connection for about 2 s, so leave room for that
    code, out, err = run(["--timeout", "5"], capsys)
    assert code == 3
    assert out == ""
    assert "refused" in err
    assert run(["-q", "--timeout", "5"], capsys) == (3, "", "")


def test_missing_endpoint_renders_placeholder(
    configured: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configured.remove("info/sensors")
    code, out, err = run(["-e", "{blocking} {sensors.cpu_temp}"], capsys)
    assert code == 0
    assert out.endswith(" ?\n")
    assert err == ""
    code, _, err = run(["--debug", "-e", "{blocking} {sensors.cpu_temp}"], capsys)
    assert code == 0
    assert "info/sensors unavailable (sensors missing)" in err
    code, _, err = run(["--strict", "-e", "{blocking} {sensors.cpu_temp}"], capsys)
    assert code == 5
    assert "info/sensors" in err
    assert not configured.active_sessions


def test_every_source_failing_is_an_error(
    configured: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configured.fail("dns/blocking", status=500)
    code, out, err = run(["-e", "{blocking}"], capsys)
    assert code == 5
    assert out == ""
    assert "500" in err


def test_no_url_with_password(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIHELLO_PASSWORD", "x")
    code, _, err = run(["-e", "{blocking}"], capsys)
    assert code == 2
    assert "no Pi-hole address" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_permission_warning(
    configured: FakePihole, cfg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg.chmod(0o644)
    code, _, err = run(["-e", "{blocking}"], capsys)
    assert code == 0
    assert "readable by other users" in err
    assert "chmod 600" in err
    assert run(["-q", "-e", "{blocking}"], capsys)[2] == ""


def test_https_with_ca_cert_and_insecure(
    cfg: Path, fake_pihole_factory: Callable[..., FakePihole], capsys: pytest.CaptureFixture[str]
) -> None:
    server = fake_pihole_factory(https=True)
    cfg.write_text(f'url = "{server.url}"\npassword = "{PASSWORD}"\n')
    cfg.chmod(0o600)
    code, _, err = run(["-e", "{blocking}"], capsys)
    assert code == 3
    assert "--ca-cert" in err or "certificate" in err
    assert run(["--ca-cert", str(server.ca_cert_path), "-e", "{blocking}"], capsys)[0] == 0
    assert run(["-k", "-e", "{blocking}"], capsys)[0] == 0
    cfg.write_text(f'url = "{server.url}"\npassword = "{PASSWORD}"\nverify_tls = false\n')
    assert run(["-e", "{blocking}"], capsys)[0] == 0
    assert not server.active_sessions


# --------------------------------------------------------------------------- #
# Cache                                                                       #
# --------------------------------------------------------------------------- #
def test_cache_avoids_requests(configured: FakePihole, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--cache", "300", "-e", "{blocking} {now|int}"], capsys)[0] == 0
    count = len(configured.requests)
    code, _, err = run(["--cache", "300", "--debug", "-e", "{blocking} {now|int}"], capsys)
    assert code == 0
    assert len(configured.requests) == count
    assert "cache hit" in err
    assert run(["--no-cache", "-e", "{blocking}"], capsys)[0] == 0
    assert len(configured.requests) > count


def test_pihole_url_and_host_are_normalised_on_every_path(
    cfg: Path,
    fake_pihole: FakePihole,
    ticker: type[FakeTicker],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # url without a scheme, as written by hand; a fetch normalises it, so must
    # a cache hit and a template that needs no data from the Pi-hole
    host_port = fake_pihole.url.removeprefix("http://")
    cfg.write_text(f'url = "{host_port}"\npassword = "{PASSWORD}"\n')
    cfg.chmod(0o600)
    expected = f"{fake_pihole.url} 127.0.0.1"
    template = "{pihole.url} {pihole.host}"
    assert run(["-e", template], capsys)[1] == expected + "\n"  # no data needed
    assert run(["--watch", "-e", template], capsys)[1] == expected + "\n\n"
    fetching = template + " {blocking}"
    miss = run(["--cache", "300", "-e", fetching], capsys)[1]
    assert miss.startswith(expected + " ")
    count = len(fake_pihole.requests)
    hit = run(["--cache", "300", "-e", fetching], capsys)[1]
    assert len(fake_pihole.requests) == count  # served from the cache
    assert hit == miss


def test_cache_from_config_skips_password_command(
    cfg: Path, fake_pihole: FakePihole, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    code = f"open({str(marker)!r}, 'a').write('x'); print({PASSWORD!r})"
    cfg.write_text(
        config.dumps(
            {
                "url": fake_pihole.url,
                "cache_ttl": 300,
                "password_command": [sys.executable, "-c", code],
            }
        )
    )
    assert run(["-e", "{blocking}"], capsys)[0] == 0
    assert run(["-e", "{blocking}"], capsys)[0] == 0
    assert marker.read_text() == "x"  # the password manager ran only once


def test_cache_not_stored_when_fetch_failed(
    configured: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configured.fail("info/system", status=500)
    assert run(["--cache", "300", "-e", "{blocking} {system.uptime}"], capsys)[0] == 0
    count = len(configured.requests)
    configured.clear_faults()
    assert run(["--cache", "300", "-e", "{blocking} {system.uptime}"], capsys)[0] == 0
    assert len(configured.requests) > count


# --------------------------------------------------------------------------- #
# --watch                                                                     #
# --------------------------------------------------------------------------- #
class FakeTicker:
    """Replaces cli.Ticker: scripted resize/timeout events, then Ctrl-C."""

    events: ClassVar[list[bool]] = []
    waits: ClassVar[list[float]] = []

    def __init__(self) -> None:
        self.closed = False

    def wait(self, timeout: float) -> bool:
        FakeTicker.waits.append(timeout)
        if not FakeTicker.events:
            raise KeyboardInterrupt
        return FakeTicker.events.pop(0)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def ticker(monkeypatch: pytest.MonkeyPatch) -> type[FakeTicker]:
    FakeTicker.events = []
    FakeTicker.waits = []
    monkeypatch.setattr(cli, "Ticker", FakeTicker)
    return FakeTicker


def test_watch_tty_frames_and_clean_exit(
    configured: FakePihole,
    ticker: type[FakeTicker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pihello import terminal

    stream = TTY()
    monkeypatch.setattr(cli, "_stdout", lambda: stream)
    monkeypatch.setattr(terminal, "terminal_size", lambda _stream=None: (60, 10))
    ticker.events = [False, True, False]  # tick, resize, tick, then Ctrl-C
    assert main(["--watch", "2", "--no-color", "-e", "{blocking}"]) == 0
    text = stream.getvalue()
    assert text.startswith("\x1b[?1049h\x1b[?25l")
    assert text.endswith("\x1b[?25h\x1b[?1049l")
    assert text.count("\x1b[H") == 4  # 3 fetches + 1 resize redraw
    assert "Ctrl-C to quit" in text
    assert "every 2s" in text
    logins = configured.requests_to("auth", "POST")
    assert len(logins) == 1  # one session for the whole run
    assert not configured.active_sessions
    assert ticker.waits[0] == pytest.approx(2, abs=0.5)
    assert len(configured.requests_to("dns/blocking")) == 3


def test_watch_shows_errors_inline_and_retries(
    configured: FakePihole,
    ticker: type[FakeTicker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pihello import terminal

    stream = TTY()
    monkeypatch.setattr(cli, "_stdout", lambda: stream)
    monkeypatch.setattr(terminal, "terminal_size", lambda _stream=None: (80, 10))
    configured.fail("dns/blocking", status=503, times=1)
    ticker.events = [False]
    assert main(["--watch", "--no-color", "-e", "{blocking}", "--timeout", "1"]) == 0
    frames = stream.getvalue().split("\x1b[H")[1:]
    assert "retrying" in frames[0]
    assert "retrying" not in frames[1]
    assert re.search(r"enabled|disabled", frames[1])
    assert not configured.active_sessions


def test_watch_unreachable_at_start(
    cfg: Path, ticker: type[FakeTicker], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg.write_text('url = "http://127.0.0.1:9"\npassword = "x"\n')
    cfg.chmod(0o600)
    ticker.events = [False]
    # Windows retries a refused connection for about 2 s, so leave room for that
    code, out, err = run(["--watch", "1", "-e", "{blocking}", "--timeout", "5"], capsys)
    assert code == 0
    # not a TTY: plain frames, errors also reported on stderr
    assert out.count("retrying") == 2
    assert "refused" in err


def test_watch_wrong_password_exits(
    cfg: Path, fake_pihole: FakePihole, ticker: type[FakeTicker], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg.write_text(f'url = "{fake_pihole.url}"\npassword = "wrong"\n')
    cfg.chmod(0o600)
    code, _, err = run(["--watch", "-e", "{blocking}"], capsys)
    assert code == 4
    assert "rejected" in err


def test_watch_seats_exceeded_is_retried(
    cfg: Path,
    fake_pihole_factory: Callable[..., FakePihole],
    ticker: type[FakeTicker],
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = fake_pihole_factory(max_sessions=0)
    cfg.write_text(f'url = "{server.url}"\npassword = "{PASSWORD}"\n')
    cfg.chmod(0o600)
    ticker.events = [False]
    code, out, _ = run(["--watch", "-e", "{blocking}"], capsys)
    assert code == 0
    assert "max_sessions" in out


def test_watch_demo_and_builtins(
    ticker: type[FakeTicker], cfg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ticker.events = [False]
    code, out, _ = run(["--watch", "--demo", "-e", "{blocking}"], capsys)
    assert code == 0
    assert out == "enabled\n\nenabled\n\n"
    cfg.write_text('url = "http://127.0.0.1:9"\n')
    ticker.events = []
    code, out, _ = run(["--watch", "-e", "{pihole.host}"], capsys)
    assert code == 0
    assert out == "127.0.0.1\n\n"


def test_real_ticker_wait_and_resize() -> None:
    import os
    import signal
    import threading

    ticker = cli.Ticker()
    try:
        assert ticker.wait(0.01) is False
        if hasattr(signal, "SIGWINCH"):
            threading.Timer(0.05, os.kill, (os.getpid(), signal.SIGWINCH)).start()
            assert ticker.wait(5) is True
    finally:
        ticker.close()
    ticker.close()


@pytest.mark.skipif(
    not hasattr(signal, "SIGWINCH") or not hasattr(signal, "setitimer"), reason="POSIX signals"
)
def test_real_ticker_survives_a_resize_storm() -> None:
    # A Python signal handler runs between any two bytecodes of the main thread.
    # If it takes a lock that the interrupted code holds (threading.Event does
    # inside wait()/clear()), the watch freezes. Dragging a window edge sends
    # SIGWINCH dozens of times per second.
    import os
    import threading
    import time

    def watchdog(_signum: int, _frame: object) -> None:
        raise TimeoutError("Ticker deadlocked in its SIGWINCH handler")

    previous = signal.signal(signal.SIGALRM, watchdog)
    ticker = cli.Ticker()
    stop = threading.Event()

    def storm() -> None:
        while not stop.is_set():
            os.kill(os.getpid(), signal.SIGWINCH)
            time.sleep(0.0002)

    thread = threading.Thread(target=storm, daemon=True)
    signal.setitimer(signal.ITIMER_REAL, 5)
    thread.start()
    try:
        resized = 0
        end = time.monotonic() + 1
        while time.monotonic() < end:
            resized += ticker.wait(0)
        assert resized
    finally:
        stop.set()
        thread.join()
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        ticker.close()


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal and POSIX signals")
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_watch_ends_cleanly_on_sigterm_and_sighup(
    signame: str, fake_pihole: FakePihole, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `kill PID`, closing the terminal window or tmux kill-pane must restore the
    # terminal (alternate screen off, cursor on) and log out the API session.
    import os
    import pty
    import select
    import subprocess
    import time

    monkeypatch.setenv("PIHELLO_URL", fake_pihole.url)
    monkeypatch.setenv("PIHELLO_PASSWORD", PASSWORD)
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "pihello", "--watch", "30", "--no-color", "-e", "{blocking}"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    output = b""

    def read_available(timeout: float) -> bool:
        nonlocal output
        ready, _, _ = select.select([master], [], [], timeout)
        if not ready:
            return False
        try:
            chunk = os.read(master, 65536)
        except OSError:  # EIO: the other side is closed
            return False
        output += chunk
        return bool(chunk)

    try:
        deadline = time.monotonic() + 30
        while b"Ctrl-C to quit" not in output and time.monotonic() < deadline:
            read_available(0.5)
        assert b"Ctrl-C to quit" in output
        assert fake_pihole.active_sessions
        if signame == "SIGHUP":  # the terminal went away: writes now fail with EIO
            os.close(master)
            master = -1
        proc.send_signal(getattr(signal, signame))
        # Keep reading while it exits: macOS drops pty output that is still
        # buffered when the child's side closes.
        deadline = time.monotonic() + 30
        while proc.poll() is None and time.monotonic() < deadline:
            if master >= 0:
                read_available(0.05)
            else:
                time.sleep(0.05)
        proc.wait(timeout=5)
        while master >= 0 and read_available(0.2):
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if master >= 0:
            os.close(master)
    assert not fake_pihole.active_sessions
    assert proc.returncode == 128 + getattr(signal, signame)
    if signame == "SIGTERM":
        assert output.endswith(b"\x1b[?25h\x1b[?1049l")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_during_a_fetch_logs_out(
    fake_pihole: FakePihole, monkeypatch: pytest.MonkeyPatch
) -> None:
    # e.g. a status bar or `timeout 2 pihello` killing a slow run
    import subprocess
    import time

    monkeypatch.setenv("PIHELLO_URL", fake_pihole.url)
    monkeypatch.setenv("PIHELLO_PASSWORD", PASSWORD)
    fake_pihole.fail("dns/blocking", delay=30)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pihello", "-e", "{blocking}", "--timeout", "30"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not fake_pihole.requests_to("dns/blocking") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert fake_pihole.active_sessions
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 128 + signal.SIGTERM
    assert err == b""
    assert not fake_pihole.active_sessions


def test_draw_non_tty() -> None:
    stream = io.StringIO()
    cli._draw(stream, "a\nb", tty=False)
    assert stream.getvalue() == "a\nb\n\n"
    stream = io.StringIO()
    cli._draw(stream, "a\nb", tty=True)
    assert stream.getvalue() == "\x1b[Ha\x1b[K\nb\x1b[K\x1b[J"


def test_isatty_helper() -> None:
    class Broken:
        def isatty(self) -> bool:
            raise ValueError

    assert cli._isatty(Broken()) is False  # type: ignore[arg-type]
    assert cli._isatty(TTY()) is True


@pytest.mark.parametrize("flag", ["--tab-size", "-i", "--indent"])
def test_tab_size_option_is_bounded(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["--demo", flag, "100000000", "-e", "\tx"], capsys)
    assert (code, out) == (2, "")
    assert "0 (no tabs) to 32" in err
    assert run(["--demo", flag, "32", "-e", "\tx"], capsys)[1] == " " * 32 + "x\n"
