"""pihello 1.x invocations: ``pihello ADDR PASSWORD [-f FILE] [-p] [-k] [-ts [FMT]] ...``."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from pihello.cli import UI, build_parser, cli_overrides, main, resolve_settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from helpers.fakepihole import FakePihole

PASSWORD = "pihello-test"


def overrides(argv: list[str]) -> tuple[dict[str, object], str]:
    stderr = io.StringIO()
    ui = UI(stderr)
    args = build_parser().parse_args(argv)
    return cli_overrides(args, ui), stderr.getvalue()


def test_full_legacy_command_line_maps_to_settings() -> None:
    values, err = overrides(
        [
            "pi.hole:8080",
            "pw",
            "-f",
            "t.txt",
            "-p",
            "-k",
            "-ts",
            "%H:%M",
            "-W",
            "80",
            "-H",
            "20",
            "-c",
            "-i",
            "2",
        ]
    )
    assert values["url"] == "https://pi.hole:8080"
    assert values["password"] == "pw"
    assert values["template"] == "t.txt"
    assert values["verify_tls"] is False
    assert values["timestamp"] == "%H:%M"
    assert values["width"] == 80
    assert values["height"] == 20
    assert values["overflow"] == "clip"
    assert values["tab_size"] == 2
    assert "deprecated" not in err
    assert "leaks into shell history" in err
    assert "pw" not in err.replace("ps", "")  # never echo the password back


def test_timestamp_without_format_and_long_options() -> None:
    values, _ = overrides(["host", "--timestamp", "--proto", "--file", "x", "--indent", "8"])
    assert values["timestamp"] is True
    assert values["url"] == "https://host"
    assert values["template"] == "x"
    assert values["tab_size"] == 8
    assert values["password"] is None


def test_https_flag_upgrades_explicit_http() -> None:
    values, _ = overrides(["http://host", "-p"])
    assert values["url"] == "https://host"
    values, _ = overrides(["https://host", "-p"])
    assert values["url"] == "https://host"
    values, _ = overrides(["-u", "host", "-p"])
    assert values["url"] == "https://host"


def test_https_flag_applies_to_configured_url(home: Path) -> None:
    path = home / ".config" / "pihello" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("url = 'http://pi.hole'\n")
    args = build_parser().parse_args(["-p"])
    assert resolve_settings(args, UI(io.StringIO())).url == "https://pi.hole"


def test_addr_and_url_conflict(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["host.lan", "--url", "other"]) == 2
    assert "either as ADDR or with --url" in capsys.readouterr().err
    assert main(["shwo", "--url", "other"]) == 2  # a lone word: a mistyped command
    assert "unknown command 'shwo'" in capsys.readouterr().err


def test_positional_password_conflicts_with_password_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["host", "pw", "--password-file", "x"]) == 2
    err = capsys.readouterr().err
    assert "cannot be used together" in err
    assert "PASSWORD argument" in err


def test_deprecation_warning_suppressed_by_quiet() -> None:
    _values, err = overrides(["host", "pw", "-q"])
    # cli_overrides runs before the quiet flag is applied by main(); main() sets it first
    stderr = io.StringIO()
    ui = UI(stderr)
    ui.quiet = True
    cli_overrides(build_parser().parse_args(["host", "pw", "-q"]), ui)
    assert stderr.getvalue() == ""
    assert err  # without quiet the warning is printed


def test_legacy_invocation_against_fake_pihole(
    fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    address = fake_pihole.url.removeprefix("http://")
    code = main([address, PASSWORD, "-W", "0"])
    out, err = capsys.readouterr()
    assert code == 0, err
    # the classic preset (the exact 1.x look) is used when no template is configured
    assert "Gravity last updated" in out
    assert "Blocking" in out
    assert "leaks into shell history" in err
    assert PASSWORD not in out
    assert PASSWORD not in err
    assert not fake_pihole.active_sessions


def test_legacy_quiet_has_no_stderr(
    fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    address = fake_pihole.url.removeprefix("http://")
    assert main([address, PASSWORD, "-q"]) == 0
    assert capsys.readouterr().err == ""


def test_legacy_wrong_password(fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]) -> None:
    address = fake_pihole.url.removeprefix("http://")
    assert main([address, "wrong"]) == 4
    err = capsys.readouterr().err
    assert "rejected the password" in err
    assert "wrong" not in err.replace("leaks", "")


def test_legacy_template_file(
    fake_pihole: FakePihole, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = tmp_path / "greeting.txt"
    template.write_text("[green]Blocked {queries.blocked} of {queries.total}[]\n")
    address = fake_pihole.url.removeprefix("http://")
    assert main([address, PASSWORD, "-f", str(template), "-q"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Blocked ")
    assert "\x1b[" not in out  # not a TTY


def test_legacy_timestamp_first_line(
    fake_pihole: FakePihole, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = tmp_path / "t.txt"
    template.write_text("body")
    address = fake_pihole.url.removeprefix("http://")
    assert main([address, PASSWORD, "-f", str(template), "-ts", "TS-%Y", "-q"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("TS-20")
    assert lines[1] == "body"


def test_legacy_https_insecure(
    fake_pihole_factory: Callable[..., FakePihole], capsys: pytest.CaptureFixture[str]
) -> None:
    server = fake_pihole_factory(https=True)
    address = server.url.removeprefix("https://")
    assert main([address, PASSWORD, "-p", "-k", "-q", "-e", "{blocking}"]) == 0
    assert capsys.readouterr().out.strip() in {"enabled", "disabled"}
    assert not server.active_sessions


def test_mistyped_command_is_not_taken_as_a_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pihello 1.x always needed ADDR *and* PASSWORD. A lone word that doesn't
    # look like a host is a mistyped command: never look it up (search domains
    # or wildcard DNS could resolve it) or send the configured password there.
    import socket

    def no_dns(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not resolve")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    assert main(["vras"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'vras'" in err
    assert "hint: did you mean `pihello vars`?" in err
    assert main(["pihole"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'pihole'" in err
    assert "pihello -u pihole" in err
    assert main(["-q", "shwo"]) == 2
    assert capsys.readouterr().err == ""


def test_lone_addr_that_looks_like_a_host_is_still_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pihello import cli

    seen: list[object] = []

    def show(args: object, _ui: object) -> int:
        seen.append(getattr(args, "addr", None))
        return 0

    monkeypatch.setitem(cli._HANDLERS, "show", show)
    hosts = ["pi.hole", "192.168.1.2", "localhost", "pihole:8080", "[fd00::2]", "http://pihole"]
    for addr in hosts:
        assert main([addr]) == 0, addr
    assert main(["pihole", "PASSWORD"]) == 0  # the 1.x form: any host name
    assert main(["--", "pihole"]) == 0  # explicitly an operand
    assert seen == [*hosts, "pihole", "pihole"]


def test_command_typo_detection() -> None:
    from pihello.cli import _command_typo

    assert _command_typo("vras") == "vars"
    assert _command_typo("docter") == "doctor"
    assert _command_typo("pihole") is None
    assert _command_typo("vars.lan") is None
    assert _command_typo("vras:8080") is None
    assert _command_typo(None) is None


def test_double_dash_keeps_a_host_named_like_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # `pihello -- doctor PASSWORD`: after "--" every word is ADDR / PASSWORD,
    # even one spelled like a command (`pihello show doctor` works as well).
    from pihello import cli

    seen: list[tuple[str, object, object]] = []

    def record(name: str) -> Callable[..., int]:
        def handler(args: object, _ui: object) -> int:
            seen.append((name, getattr(args, "addr", None), getattr(args, "password", None)))
            return 0

        return handler

    for command in ("show", "doctor"):
        monkeypatch.setitem(cli._HANDLERS, command, record(command))
    assert main(["--", "doctor"]) == 0
    assert main(["-q", "--", "doctor", "-secret-"]) == 0
    assert main(["-q", "doctor"]) == 0  # options before a command: still that command
    assert seen == [
        ("show", "doctor", None),
        ("show", "doctor", "-secret-"),
        ("doctor", None, None),
    ]
