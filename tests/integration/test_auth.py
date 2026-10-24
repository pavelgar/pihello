"""Authentication end to end: password sources, app passwords, 2FA, no password, seats."""

from __future__ import annotations

import shlex
import stat
import time
from typing import TYPE_CHECKING

import pytest

from helpers.containers import ContainerError, totp_code

from .support import Pihello, Run, Target, run_together, running_target

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration

NO_ENV = {"PIHELLO_PASSWORD": None, "PIHELLO_URL": None}


def assert_greeting(run: Run) -> None:
    assert run.returncode == 0, run
    assert run.stdout.startswith("Pi-hole on "), run


# --------------------------------------------------------------------------- #
# Where the password comes from                                                #
# --------------------------------------------------------------------------- #
def test_password_from_environment(pihello: Pihello) -> None:
    run = pihello("--color", "never")
    assert_greeting(run)
    assert run.stderr == ""


def test_password_file(pihello: Pihello, pihole: Target, tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text(f"{pihole.password}\nsecond line is ignored\n")
    run = pihello("--password-file", str(secret), env={"PIHELLO_PASSWORD": None})
    assert_greeting(run)
    run = pihello(env={"PIHELLO_PASSWORD": None, "PIHELLO_PASSWORD_FILE": str(secret)})
    assert_greeting(run)


def test_password_command(pihello: Pihello, pihole: Target, tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text(f"{pihole.password}\n")
    run = pihello(
        "--password-command", f"cat {shlex.quote(str(secret))}", env={"PIHELLO_PASSWORD": None}
    )
    assert_greeting(run)
    shell = f"printf '%s\\n' {shlex.quote(pihole.password or '')} | head -n 1"
    run = pihello("--password-command", shell, env={"PIHELLO_PASSWORD": None})
    assert_greeting(run)


def test_config_written_by_init(pihello: Pihello, pihole: Target) -> None:
    run = pihello(
        "init",
        "--yes",
        "--url",
        pihole.url,
        "--password-stdin",
        "--shell",
        "bash",
        input=f"{pihole.password}\n",
        env=NO_ENV,
    )
    assert run.returncode == 0, run
    assert pihole.password not in run.stdout + run.stderr
    config = pihello.home / ".config" / "pihello" / "config.toml"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    text = config.read_text()
    assert f'url = "{pihole.url}"' in text
    assert f'password = "{pihole.password}"' in text
    assert "pihello -q" in run.stdout  # the shell snippet

    run = pihello("--color", "never", env=NO_ENV)
    assert_greeting(run)
    doctor = pihello("doctor", "--color", "never", env=NO_ENV, logins=2)
    assert doctor.returncode == 0, doctor
    assert f"config file {config} (default location, mode 600)" in doctor.stdout, doctor
    assert pihole.password not in doctor.stdout


def test_wrong_password(pihello: Pihello) -> None:
    run = pihello("--color", "never", env={"PIHELLO_PASSWORD": "not-the-password"})
    assert run.returncode == 4, run
    assert run.stdout == ""
    assert "pihello: Pi-hole rejected the password\nhint: check the password" in run.stderr, run
    assert "not-the-password" not in run.stderr


def test_missing_password(pihello: Pihello) -> None:
    run = pihello(env={"PIHELLO_PASSWORD": None}, logins=0)
    assert run.returncode == 4, run
    assert "pihello: Pi-hole requires a password" in run.stderr, run
    assert "app password" in run.stderr, run


# --------------------------------------------------------------------------- #
# App password and two-factor authentication (own container: these settings   #
# invalidate every session)                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def auth_pihole() -> Iterator[Target]:
    with running_target("auth") as target:
        yield target


@pytest.fixture(scope="module")
def app_password(auth_pihole: Target) -> str:
    app = auth_pihole.get("auth/app")["app"]
    resp = auth_pihole.observe(
        "PATCH", "config", body={"config": {"webserver": {"api": {"app_pwhash": app["hash"]}}}}
    )
    assert resp.ok, resp.text
    return str(app["password"])


def test_app_password(
    make_pihello: Callable[[Target], Pihello], auth_pihole: Target, app_password: str
) -> None:
    pihello = make_pihello(auth_pihole)
    run = pihello("--color", "never", env={"PIHELLO_PASSWORD": app_password})
    assert_greeting(run)
    doctor = pihello("doctor", "--color", "never", env={"PIHELLO_PASSWORD": app_password}, logins=2)
    assert doctor.returncode == 0, doctor
    assert "✓ logged in with the configured password" in doctor.stdout, doctor
    assert app_password not in doctor.stdout + doctor.stderr


def test_concurrent_greetings_with_app_password(
    make_pihello: Callable[[Target], Pihello], auth_pihole: Target, app_password: str
) -> None:
    """FTL checks an app password after the normal one; both count against its limit
    of 3 per second, and an app-password check over the limit gets a plain 401.
    Before pihello retried those, two shells starting together failed half the time."""
    runners = [make_pihello(auth_pihole) for _ in range(2)]
    for _ in range(3):
        with auth_pihole.throttle(3):
            runs = run_together(runners, "--color", "never", env={"PIHELLO_PASSWORD": app_password})
        for run in runs:
            assert_greeting(run)
            assert run.stderr == "", run


def _totp_login(target: Target, secret: str) -> str | None:
    """Log in with password + TOTP; a code works once and (FTL ≥ v6.4) one try per 30 s."""
    for attempt in range(2):
        try:
            return target.login(totp=totp_code(secret))
        except ContainerError:
            if attempt:
                raise
            time.sleep(30.5 - time.time() % 30)  # next TOTP window
    return None


def test_two_factor_authentication(
    make_pihello: Callable[[Target], Pihello], auth_pihole: Target, app_password: str
) -> None:
    pihello = make_pihello(auth_pihole)
    secret = auth_pihole.get("auth/totp")["totp"]["secret"]
    body = {"config": {"webserver": {"api": {"totp_secret": secret}}}}
    assert auth_pihole.observe("PATCH", "config", body=body).ok  # ends every session
    try:
        run = pihello("--color", "never")
        assert run.returncode == 4, run
        assert "two-factor authentication enabled" in run.stderr, run
        assert "app password" in run.stderr.split("hint:", 1)[1], run

        run = pihello("--color", "never", env={"PIHELLO_PASSWORD": "not-the-password"})
        assert run.returncode == 4, run
        assert "Pi-hole rejected the password (two-factor" in run.stderr, run

        run = pihello("--color", "never", env={"PIHELLO_PASSWORD": app_password})
        assert_greeting(run)  # app passwords bypass 2FA
    finally:
        sid = _totp_login(auth_pihole, secret)
        off = {"config": {"webserver": {"api": {"totp_secret": ""}}}}
        resp = auth_pihole.api("PATCH", "config", body=off, sid=sid)
        assert resp.ok, resp.text  # sessions are invalidated again, no logout needed
    assert_greeting(pihello("--color", "never"))


# --------------------------------------------------------------------------- #
# All API seats taken                                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def one_seat_pihole() -> Iterator[Target]:
    with running_target("seats", env={"FTLCONF_webserver_api_max_sessions": "1"}) as target:
        yield target


def test_too_many_sessions(
    make_pihello: Callable[[Target], Pihello], one_seat_pihole: Target
) -> None:
    pihello = make_pihello(one_seat_pihole)
    seat = one_seat_pihole.login()  # the only seat
    try:
        run = pihello("--color", "never")
    finally:
        one_seat_pihole.logout(seat)
    assert run.returncode == 4, run
    assert "no free API sessions" in run.stderr, run
    assert "max_sessions" in run.stderr.split("hint:", 1)[1], run
    assert_greeting(pihello("--color", "never"))  # the seat is free again


# --------------------------------------------------------------------------- #
# Pi-hole without a password                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def open_pihole() -> Iterator[Target]:
    with running_target("nopw", password="") as target:
        yield target


def test_no_password_needed(make_pihello: Callable[[Target], Pihello], open_pihole: Target) -> None:
    pihello = make_pihello(open_pihole)
    run = pihello("--color", "never")
    assert_greeting(run)
    assert run.stderr == ""
    doctor = pihello("doctor", "--color", "never")
    assert doctor.returncode == 0, doctor
    assert "✓ this Pi-hole has no password; no login needed" in doctor.stdout, doctor
    assert "no API session used" in doctor.stdout, doctor


def test_stray_password_is_harmless(
    make_pihello: Callable[[Target], Pihello], open_pihole: Target
) -> None:
    pihello = make_pihello(open_pihole)
    env = {"PIHELLO_PASSWORD": "left-over-password"}
    run = pihello("--color", "never", env=env, logins=1)
    assert_greeting(run)
    assert run.stderr == ""
    doctor = pihello("doctor", "--color", "never", env=env, logins=1)
    assert doctor.returncode == 0, doctor
    assert "! this Pi-hole has no password, so the configured one is not needed" in doctor.stdout
