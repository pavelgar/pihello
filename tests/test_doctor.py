"""``pihello doctor``."""

from __future__ import annotations

import io
import socket
import sys
from typing import TYPE_CHECKING, Any

import pytest

from pihello import config, doctor
from pihello.cli import UI, build_parser, main

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from helpers.fakepihole import FakePihole

PASSWORD = "pihello-test"


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(["doctor", *argv])
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def cfg() -> Path:
    path = config.default_config_path()  # in the isolated HOME
    path.parent.mkdir(parents=True)
    return path


def configure(cfg: Path, server: FakePihole, extra: str = "", password: str = PASSWORD) -> None:
    cfg.write_text(f'url = "{server.url}"\npassword = "{password}"\n{extra}')
    cfg.chmod(0o600)


def sessions_reply(server: FakePihole, *, stale: int = 0) -> None:
    sessions = [
        {"id": 0, "current_session": True, "login_at": 1, "user_agent": "pihello/2 (doctor)"}
    ]
    sessions += [
        {"id": i + 1, "current_session": False, "login_at": 1, "user_agent": "pihello/2"}
        for i in range(stale)
    ]
    server.set_response("GET /api/auth/sessions", 200, {"sessions": sessions})


def test_all_good(cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]) -> None:
    configure(cfg, fake_pihole)
    sessions_reply(fake_pihole)
    code, out, err = run([], capsys)
    assert code == 0, out
    assert err == ""
    for expected in (
        "✓ Python",
        f"✓ config file {cfg}",
        "effective settings",
        "set (hidden)",
        f"✓ address {fake_pihole.url}",
        "is an IP address",
        "✓ TCP connection",
        "✓ Pi-hole v6 API answers",
        "✓ logged in with the configured password",
        "✓ Pi-hole core",
        "data sources available",
        "logged out; no session left behind",
    ):
        assert expected in out, expected
    assert "✗" not in out
    assert PASSWORD not in out
    assert not fake_pihole.active_sessions


def test_reports_http_when_https_available(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    import socket

    with socket.socket() as listener:  # something answers on the HTTPS port
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        fake_pihole.set_response("GET /api/info/login", 200, {"dns": True, "https_port": port})
        configure(cfg, fake_pihole)
        code, out, _ = run([], capsys)
    assert code == 0
    assert f"serves HTTPS on port {port}" in out
    assert f'use url = "https://127.0.0.1:{port}"' in out
    assert "cannot list API sessions" in out or "logged out" in out


def test_https_port_that_does_not_answer_is_not_recommended(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    # Docker: info/login reports the container's port 443, which isn't published
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    fake_pihole.set_response("GET /api/info/login", 200, {"dns": True, "https_port": port})
    configure(cfg, fake_pihole)
    code, out, _ = run([], capsys)
    assert code == 0
    assert "using plain HTTP although" not in out
    assert f"127.0.0.1:{port} doesn't answer" in out
    assert 'url = "https://127.0.0.1:PORT"' in out
    assert "Everything looks good" in out


def test_settings_table_fits_80_columns(
    cfg: Path,
    fake_pihole: FakePihole,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # the config path is printed once (on the "config file" line), not per row
    configure(cfg, fake_pihole, extra="theme = 'nord'\ntimeout = 5\n[profiles.p]\nwidth = 100\n")
    monkeypatch.setenv("PIHELLO_PASSWORD_FILE", str(cfg.parent / "a-long-password-file-name"))
    cfg.write_text(cfg.read_text().replace(f'password = "{PASSWORD}"\n', ""))
    _, out, _ = run(["-P", "p", "--no-color", "--timeout", "1"], capsys)
    table = out.split("effective settings", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
    assert len(table) >= 9
    assert all(len(line) <= 80 for line in table), "\n".join(table)
    assert out.count(str(cfg)) == 1
    assert "profile 'p'" in out
    assert "environment (PIHELLO_PASSWORD_FILE)" in out


def test_settings_origins(
    cfg: Path,
    fake_pihole: FakePihole,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg.write_text(
        f'url = "{fake_pihole.url}"\n[profiles.p]\ntheme = "nord"\n[styles]\naccent="red"\n'
    )
    monkeypatch.setenv("PIHELLO_PASSWORD", PASSWORD)
    code, out, _ = run(["-P", "p", "--timeout", "2"], capsys)
    assert code == 0, out
    assert "environment (PIHELLO_PASSWORD)" in out
    assert "profile 'p'" in out
    assert "command line (--timeout)" in out
    assert "profiles: p" in out
    assert "styles" in out
    assert PASSWORD not in out


def test_no_config_and_no_url(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run([], capsys)
    assert code == 2
    assert "! no config file" in out
    assert "✗ no Pi-hole address configured" in out
    assert "pihello init" in out
    assert "1 problem found, 1 warning" in out


def test_broken_config(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg.write_text("url = \n")
    code, out, _ = run([], capsys)
    assert code == 2
    assert "invalid TOML" in out


def test_explicit_missing_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["--config", str(tmp_path / "x.toml")], capsys)
    assert code == 2
    assert "from --config" in out
    assert "not found" in out


def test_invalid_settings(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg.write_text("url = 'http://x'\n")
    code, out, _ = run(["-P", "missing"], capsys)
    assert code == 2
    assert "unknown profile" in out


def test_invalid_url(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg.write_text("url = 'http://'\n")
    code, out, _ = run([], capsys)
    assert code == 2
    assert "✗ address" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_permission_warning(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    cfg.chmod(0o644)
    code, out, err = run([], capsys)
    assert code == 0
    assert "other users can read it" in out
    assert f"chmod 600 {cfg}" in out
    assert err == ""  # reported in the checklist, not as a separate warning


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_permission_hint_quotes_the_path(
    tmp_path: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    # the hint is a command to copy and paste
    path = tmp_path / "My Configs" / "pi hole.toml"
    path.parent.mkdir()
    configure(path, fake_pihole)
    path.chmod(0o644)
    code, out, _ = run(["--config", str(path)], capsys)
    assert code == 0
    assert f"chmod 600 '{path}'" in out


def test_wrong_password(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole, password="wrong-one")
    code, out, _ = run([], capsys)
    assert code == 4
    assert "✗ login: Pi-hole rejected the password" in out
    assert "wrong-one" not in out
    assert not fake_pihole.active_sessions


def test_no_password_but_required(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg.write_text(f'url = "{fake_pihole.url}"\n')
    code, out, _ = run([], capsys)
    assert code == 4
    assert "this Pi-hole has a password" in out


def test_passwordless_pihole(
    cfg: Path, fake_pihole_factory: Callable[..., FakePihole], capsys: pytest.CaptureFixture[str]
) -> None:
    server = fake_pihole_factory(password=None)
    cfg.write_text(f'url = "{server.url}"\n')
    code, out, _ = run([], capsys)
    assert code == 0, out
    assert "no password; no login needed" in out
    assert "no API session used" in out
    configure(cfg, server)
    code, out, _ = run([], capsys)
    assert code == 0
    assert "the configured one is not needed" in out


def test_totp(
    cfg: Path, fake_pihole_factory: Callable[..., FakePihole], capsys: pytest.CaptureFixture[str]
) -> None:
    server = fake_pihole_factory(totp=True)
    configure(cfg, server)
    code, out, _ = run([], capsys)
    assert code == 4
    assert "two-factor authentication is enabled" in out
    assert "app password" in out


def test_seats_exceeded(
    cfg: Path, fake_pihole_factory: Callable[..., FakePihole], capsys: pytest.CaptureFixture[str]
) -> None:
    server = fake_pihole_factory(max_sessions=0)
    configure(cfg, server)
    code, out, _ = run([], capsys)
    assert code == 4
    assert "webserver.api.max_sessions" in out
    assert "Currently active sessions" in out


def test_password_command_failure(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    command = f"{sys.executable} -c 1"  # a Windows path needs TOML escapes
    cfg.write_text(config.dumps({"url": fake_pihole.url, "password_command": command}))
    code, out, _ = run([], capsys)
    assert code == 2
    assert "printed nothing" in out


def test_password_command_ok(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    command = [sys.executable, "-c", f"print({PASSWORD!r})"]
    cfg.write_text(config.dumps({"url": fake_pihole.url, "password_command": command}))
    code, out, _ = run([], capsys)
    assert code == 0, out
    assert "password read from password_command" in out


def test_connection_refused(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg.write_text('url = "http://127.0.0.1:9"\n')
    code, out, _ = run(["--timeout", "1"], capsys)
    assert code == 3
    assert "✗ cannot connect to port 9" in out
    assert "firewall" in out


def test_dns_failure(cfg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg.write_text('url = "http://pihello-test.invalid"\n')
    code, out, _ = run(["--timeout", "5"], capsys)
    assert code == 3
    assert "cannot resolve pihello-test.invalid" in out or "timed out" in out
    assert "pi.hole" in out


def test_dns_timeout(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    cfg.write_text('url = "http://slow.example"\n')

    def slow(*_args: Any, **_kwargs: Any) -> Any:
        time.sleep(1)
        raise OSError("late")

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    code, out, _ = run(["--timeout", "0.1"], capsys)
    assert code == 3
    assert "timed out after 0.1 s" in out


def test_dns_resolves_localhost(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    code, out, _ = run(["--url", fake_pihole.url.replace("127.0.0.1", "localhost")], capsys)
    assert "DNS localhost →" in out
    assert code == 0, out


def test_placeholder_dns_answer_is_skipped_and_explained(
    cfg: Path,
    fake_pihole: FakePihole,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Seen on a real LAN: the Pi-hole's name had A 192.168.1.2 and AAAA `::`.
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host != "pihole.lan":
            return real_getaddrinfo(host, port, *args, **kwargs)
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    configure(cfg, fake_pihole)
    code, out, _ = run(["--url", fake_pihole.url.replace("127.0.0.1", "pihole.lan")], capsys)
    assert "DNS pihole.lan → ::, 127.0.0.1" in out
    assert ":: is not a real address; pihello skips it" in out
    assert "TCP connection to 127.0.0.1" in out
    assert code == 0, out


def test_not_a_pihole(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_pihole.set_response("GET /api/info/login", 200, b"<html>hello</html>")
    configure(cfg, fake_pihole)
    code, out, _ = run([], capsys)
    assert code == 5
    assert "doesn't look like a Pi-hole v6" in out


def test_tls(
    cfg: Path, fake_pihole_factory: Callable[..., FakePihole], capsys: pytest.CaptureFixture[str]
) -> None:
    server = fake_pihole_factory(https=True)
    configure(cfg, server)
    code, out, _ = run([], capsys)
    assert code == 3
    assert "HTTPS certificate of 127.0.0.1" in out
    code, out, _ = run(["--ca-cert", str(server.ca_cert_path)], capsys)
    assert code == 0, out
    assert f"✓ HTTPS certificate verified against {server.ca_cert_path}" in out
    code, out, _ = run(["-k"], capsys)
    assert code == 0
    assert "NOT verified" in out


def test_missing_and_broken_sources(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    fake_pihole.remove("info/sensors")
    fake_pihole.fail("info/system", status=500)
    code, out, _ = run([], capsys)
    assert code == 0, out
    assert "info/sensors: not on this Pi-hole version (sensors show as missing)" in out
    assert "! info/system" in out


def test_all_sources_broken(
    cfg: Path,
    fake_pihole: FakePihole,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pihello import sources
    from pihello.errors import APIError

    configure(cfg, fake_pihole)

    def broken(_client: Any, planned: Any) -> Any:
        return {}, [sources.FetchProblem(s, APIError("bad")) for s in planned]

    monkeypatch.setattr(sources, "fetch", broken)
    code, out, _ = run([], capsys)
    assert code == 5
    assert "✗ 0 of" in out


def test_versions_and_updates(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    fake_pihole.set_response(
        "GET /api/info/version",
        200,
        {
            "version": {
                "core": {"local": {"version": "v5.18"}, "remote": {"version": "v6.1"}},
                "web": {"local": {"version": "v6.0"}, "remote": {"version": "v6.0"}},
                "ftl": {"local": {"version": "v6.0"}, "remote": {"version": None}},
            }
        },
    )
    code, out, _ = run([], capsys)
    assert "core v5.18, web v6.0, FTL v6.0" in out
    assert "update available: core v5.18 → v6.1" in out
    assert "pihole -up" in out
    assert "pihello 0.3.0" in out
    assert code == 0


def test_docker_updates_and_null_remote(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    component = {"local": {"version": "v6.5"}, "remote": {"version": None}}
    reply = {
        "version": {
            "core": component,
            "web": component,
            "ftl": component,
            "docker": {"local": "2026.10.0", "remote": "null"},
        }
    }
    fake_pihole.set_response("GET /api/info/version", 200, reply)
    _, out, _ = run([], capsys)
    assert "docker 2026.10.0" in out
    assert "update status unknown" in out  # no remote version was ever fetched
    assert "up to date" not in out
    reply["version"]["docker"]["remote"] = "2026.11.0"  # type: ignore[index]
    fake_pihole.set_response("GET /api/info/version", 200, reply)
    _, out, _ = run([], capsys)
    assert "update available: docker 2026.10.0 → 2026.11.0" in out
    assert "recreate the container" in out


def test_bare_metal_update_hint(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    # version.docker exists on every install; bare metal has null image tags
    configure(cfg, fake_pihole)
    current = {"local": {"version": "v6.5"}, "remote": {"version": "v6.5"}}
    reply = {
        "version": {
            "core": current,
            "web": current,
            "ftl": {"local": {"version": "v6.7"}, "remote": {"version": "v6.8"}},
            "docker": {"local": None, "remote": None},
        }
    }
    fake_pihole.set_response("GET /api/info/version", 200, reply)
    _, out, _ = run([], capsys)
    assert "update available: FTL v6.7 → v6.8" in out
    assert "pihole -up" in out
    assert "recreate the container" not in out


def test_versions_unknown_and_newer(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    fake_pihole.set_response("GET /api/info/version", 200, {"version": {}})
    _, out, _ = run([], capsys)
    assert "version unknown" in out
    fake_pihole.set_response(
        "GET /api/info/version", 200, {"version": {"core": {"local": {"version": "v7.0"}}}}
    )
    _, out, _ = run([], capsys)
    assert "untested" in out
    fake_pihole.set_response("GET /api/info/version", 500, {"error": {"message": "x"}})
    code, out, _ = run([], capsys)
    assert "✗ versions" in out
    assert code == 5


def test_stale_pihello_sessions(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    sessions_reply(fake_pihole, stale=2)
    code, out, _ = run([], capsys)
    assert code == 0
    assert "3 active API sessions" in out
    assert "2 other pihello sessions are still open" in out


def test_session_leak_detected(
    cfg: Path,
    fake_pihole: FakePihole,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pihello import client as client_module

    configure(cfg, fake_pihole)
    real_get = client_module.PiholeClient.get
    listings = iter(
        [
            {"sessions": [{"id": 0, "current_session": True, "login_at": 1}]},
            {
                "sessions": [
                    {"id": 0, "current_session": False, "login_at": 1},
                    {"id": 1, "current_session": True, "login_at": 2},
                ]
            },
        ]
    )

    def get(self: Any, endpoint: str, params: Any = None) -> Any:
        if endpoint == "auth/sessions":
            return next(listings)
        return real_get(self, endpoint, params)

    monkeypatch.setattr(client_module.PiholeClient, "get", get)
    code, out, _ = run([], capsys)
    assert code == 5
    assert "still open after logging out" in out


def test_session_verification_failure(
    cfg: Path,
    fake_pihole: FakePihole,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pihello import client as client_module
    from pihello.errors import APIError

    configure(cfg, fake_pihole)
    real_get = client_module.PiholeClient.get
    calls: list[int] = []

    def get(self: Any, endpoint: str, params: Any = None) -> Any:
        if endpoint == "auth/sessions":
            calls.append(1)
            if len(calls) == 2:
                raise APIError("nope")
            return {"sessions": [{"id": 0, "current_session": True, "login_at": 1}]}
        return real_get(self, endpoint, params)

    monkeypatch.setattr(client_module.PiholeClient, "get", get)
    code, out, _ = run([], capsys)
    assert code == 0
    assert "could not verify it" in out
    assert not fake_pihole.active_sessions


@pytest.mark.usefixtures("posix_terminal")
def test_ascii_fallback_and_colors(
    cfg: Path, fake_pihole: FakePihole, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(cfg, fake_pihole)

    class Latin1(io.StringIO):
        encoding = "ascii"

    stream = Latin1()
    args = build_parser("doctor").parse_args([])
    assert doctor.run(args, UI(io.StringIO()), stdout=stream) == 0
    text = stream.getvalue()
    assert "ok Python" in text
    assert "✓" not in text

    class ColorTTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    colored = ColorTTY()
    assert doctor.run(args, UI(io.StringIO()), stdout=colored) == 0
    assert "\x1b[32m✓\x1b[0m" in colored.getvalue()


def test_helpers() -> None:
    assert doctor._major("v6.1.2") == 6
    assert doctor._major("6") == 6
    assert doctor._major("dev") is None
    assert doctor._show(True) == "true"
    assert doctor._show(False) == "false"
    assert doctor._show("/x") == "/x"
    assert doctor._sessions(None) == []
    assert doctor._sessions({"sessions": [1, {"id": 2}]}) == [{"id": 2}]
    assert doctor._duration(0.0123) == "12 ms"
    assert doctor._duration(2.25) == "2.2 s"


def test_sessions_listing_unavailable_or_broken(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    fake_pihole.remove("auth/sessions")
    code, out, _ = run([], capsys)
    assert code == 0
    assert "· this Pi-hole cannot list API sessions; logged out" in out
    fake_pihole.fail("auth/sessions", status=500)
    code, out, _ = run([], capsys)
    assert code == 0
    assert "! cannot list API sessions" in out
    assert not fake_pihole.active_sessions


def test_sessions_without_current_flag(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    fake_pihole.set_response("GET /api/auth/sessions", 200, {"sessions": []})
    code, out, _ = run([], capsys)
    assert code == 0
    assert "✓ logged out" in out


def test_login_api_error(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    fake_pihole.fail("POST auth", status=500)
    code, out, _ = run([], capsys)
    assert code == 5
    assert "✗ login:" in out


def test_fetch_errors_during_versions_and_sources(
    cfg: Path,
    fake_pihole: FakePihole,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pihello import sources
    from pihello.errors import AuthenticationError

    configure(cfg, fake_pihole)

    def expired(*_args: Any, **_kwargs: Any) -> Any:
        raise AuthenticationError("session expired")

    monkeypatch.setattr(sources, "fetch", expired)
    code, out, _ = run([], capsys)
    assert code == 4
    assert "✗ versions (info/version): session expired" in out
    assert "✗ data sources: session expired" in out
    assert not fake_pihole.active_sessions


def test_version_entries_without_local_version(
    cfg: Path, fake_pihole: FakePihole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(cfg, fake_pihole)
    reply = {
        "version": {
            "core": {"local": {"version": "v6.2"}, "remote": {"version": "v6.2"}},
            "web": {"local": {}, "remote": {}},
            "ftl": "garbage",
        }
    }
    fake_pihole.set_response("GET /api/info/version", 200, reply)
    code, out, _ = run([], capsys)
    assert code == 0
    assert "✓ Pi-hole core v6.2\n" in out


def test_hints_use_the_same_menu_arrows_as_the_docs() -> None:
    from pihello import doctor

    assert "Settings → Web interface / API → Currently active sessions" in doctor._SESSIONS_HINT
    assert "Settings >" not in doctor._SESSIONS_HINT
