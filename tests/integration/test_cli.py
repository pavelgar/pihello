"""pihello's commands end to end against the main (seeded) Pi-hole container."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import re
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from helpers.containers import PiholeContainer, find_free_ports
from pihello import presets, sources, themes

from .support import SETTINGS, Pihello, Target, run_together, unavailable_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.integration

ESC = "\x1b"
ALT_SCREEN_ON = "\x1b[?1049h\x1b[?25l"
ALT_SCREEN_OFF = "\x1b[?25h\x1b[?1049l"


def number(text: str) -> int:
    return int(text.replace(",", ""))


def generic(path: str) -> str:
    return ".".join("N" if part.isdigit() else part for part in path.split("."))


# --------------------------------------------------------------------------- #
# The greeting                                                                 #
# --------------------------------------------------------------------------- #
def test_default_greeting_shows_live_data(pihello: Pihello, pihole: Target) -> None:
    before = pihole.get("stats/summary")
    run = pihello("--color", "never")
    after = pihole.get("stats/summary")
    assert run.returncode == 0, run
    assert run.stderr == "", run
    assert ESC not in run.stdout
    lines = run.lines
    assert len(lines) == 8, run
    host = pihole.url.split("://", 1)[1].rsplit(":", 1)[0]
    assert lines[0].startswith(f"Pi-hole on {host} · ● enabled"), run
    version = pihole.versions()
    core, web, ftl = (version[c]["local"]["version"] for c in ("core", "web", "ftl"))
    assert lines[2].strip() == f"Versions   Core {core} · Web {web} · FTL {ftl}", run

    queries = re.fullmatch(r"Queries +([\d,]+) today · ([\d,]+) blocked", lines[3])
    assert queries, run
    total, blocked = number(queries[1]), number(queries[2])
    assert before["queries"]["total"] <= total <= after["queries"]["total"]
    assert before["queries"]["blocked"] <= blocked <= after["queries"]["blocked"]
    percent = re.fullmatch(r"Blocked +[█▉▊▋▌▍▎▏░]+ (\d+\.\d)%", lines[4])
    assert percent, run
    assert abs(float(percent[1]) - 100 * blocked / max(total, 1)) < 0.11
    gravity = re.fullmatch(r"Gravity +([\d,]+) domains · updated (.+)", lines[6])
    assert gravity, run
    assert number(gravity[1]) == after["gravity"]["domains_being_blocked"] > 0
    assert re.fullmatch(r"just now|\d+[dhms]( \d+[hms])? ago", gravity[2]), run
    if pihole.seeded:
        assert total >= pihole.seeded.queries
        assert blocked >= pihole.seeded.blocked
        last = lines[7].split(None, 2)[2]
        assert last in pihole.seeded.blocked_domains, run


@pytest.mark.parametrize("name", presets.names())
def test_preset_renders_strictly(
    make_pihello: Callable[[Target], Pihello], pihole: Target, name: str
) -> None:
    pihello = make_pihello(pihole)
    # --cache lets the --color never run below reuse this run's data without a login.
    run = pihello("--preset", name, "--strict", "--color", "always", "--cache", "3600")
    if run.returncode != 0:
        # Only values the catalog documents as unavailable on this version may be missing.
        missing = re.search(r"variable '([^']+)' (?:has no value|is not defined)", run.stderr)
        assert missing, run
        assert generic(missing[1]) in unavailable_paths(pihole.ftl), run
        run = pihello("--preset", name, "--color", "always", "--cache", "3600")
        assert run.returncode == 0, run
    assert run.stderr == "", run
    assert ESC + "[" in run.stdout, run

    plain = pihello("--preset", name, "--color", "never", "--cache", "3600", logins=0)
    assert plain.returncode == 0, plain
    assert ESC not in plain.stdout, plain
    assert len(plain.lines) == len(run.lines), plain


def test_update_status_unknown_without_github(pihello: Pihello, pihole: Target) -> None:
    """The containers cannot reach api.github.com, so Pi-hole knows no remote versions."""
    if pihole.container is None:
        pytest.skip("an external Pi-hole may know its update status")
    run = pihello("--preset", "updates", "--color", "never")
    assert run.returncode == 0, run
    assert run.lines[-1] == "Update status unknown.", run
    assert re.search(r"^Docker +\S+ +- *$", run.stdout, re.MULTILINE), run
    vars_run = pihello("vars", "--json", "updates.*")
    assert json.loads(vars_run.stdout)["updates.status"] == "unknown", vars_run


def test_every_theme(pihello: Pihello) -> None:
    signatures = {}
    for theme in themes.available():
        run = pihello("-T", theme.name, "--color", "truecolor", env={"FORCE_COLOR": "1"})
        assert run.returncode == 0, run
        assert run.stderr == "", run
        assert len(run.lines) == 8, run
        signatures[theme.name] = tuple(re.findall(r"\x1b\[[0-9;]*m", run.stdout))
        assert signatures[theme.name], run
    # Every theme really styles the greeting differently.
    assert len(set(signatures.values())) == len(signatures)


def test_template_string_honours_no_color(pihello: Pihello) -> None:
    run = pihello("-e", "[@good]{blocking}[] {queries.total|number}", env={"NO_COLOR": "1"})
    assert run.returncode == 0, run
    assert re.fullmatch(r"enabled [\d,]+\n", run.stdout), run


def test_console_script(pihello: Pihello) -> None:
    script = Path(sys.executable).with_name("pihello")
    if not script.exists():
        pytest.skip("pihello console script not installed next to the interpreter")
    pihello.argv0 = [str(script)]
    run = pihello("--preset", "minimal", "--color", "never")
    assert run.returncode == 0, run
    assert re.fullmatch(r"● Pi-hole · \S+ queries · \S+% blocked · \d+ clients\n", run.stdout), run


# --------------------------------------------------------------------------- #
# Other commands                                                               #
# --------------------------------------------------------------------------- #
def test_vars_json_covers_every_namespace(pihello: Pihello) -> None:
    run = pihello("vars", "--json")
    assert run.returncode == 0, run
    assert run.stderr == "", run
    tree = json.loads(run.stdout)
    assert set(tree) >= sources.NAMESPACES, sources.NAMESPACES - set(tree)
    flat = pihello("vars", "--json", "queries.types.*")
    assert flat.returncode == 0, flat
    assert set(json.loads(flat.stdout)) == {f"queries.types.{t}" for t in tree["queries"]["types"]}


def test_vars_table(pihello: Pihello) -> None:
    run = pihello("vars", "--color", "never")
    assert run.returncode == 0, run
    assert re.fullmatch(r"PATH +VALUE +DESCRIPTION", run.lines[0]), run
    rows = {line.split()[0]: line.split()[1] for line in run.lines[1:]}
    assert set(rows) == {info.path for info in sources.catalog()}
    assert re.fullmatch(r"\d+", rows["queries.total"]), run
    assert rows["blocking"] == '"enabled"', run
    assert rows["pihello.version"].startswith('"2.'), run


def test_doctor(pihello: Pihello, pihole: Target) -> None:
    run = pihello("doctor", "--color", "never", logins=2)
    assert run.returncode == 0, run
    version = pihole.versions()
    expected = ", ".join(
        f"{label} {value}"
        for label, value in (
            ("core", version["core"]["local"]["version"]),
            ("web", version["web"]["local"]["version"]),
            ("FTL", version["ftl"]["local"]["version"]),
            ("docker", version["docker"]["local"]),
        )
        if value
    )
    assert f"✓ Pi-hole {expected}\n" in run.stdout, run
    assert "✓ logged in with the configured password" in run.stdout, run
    count = len(sources.SOURCES)
    assert f"✓ {count} of {count} data sources available" in run.stdout, run
    assert "✓ logged out; no session left behind" in run.stdout, run
    assert "other pihello session" not in run.stdout, run
    assert "✗" not in run.stdout, run


@pytest.mark.parametrize("command", ["presets", "themes"])
def test_live_previews(pihello: Pihello, command: str) -> None:
    run = pihello(command, "--live", "--color", "never", "--width", "100")
    assert run.returncode == 0, run
    assert run.stderr == "", run
    assert "cannot render" not in run.stdout, run
    names = presets.names() if command == "presets" else [t.name for t in themes.available()]
    lines = [line for line in run.lines if line and not line.startswith(" ")]
    if command == "presets":  # the gallery ends with a one-line hint
        assert lines[-1].startswith("Preview one with `pihello presets NAME`"), run
        lines = lines[:-1]
    assert [line.split()[0] for line in lines] == list(names), run


# --------------------------------------------------------------------------- #
# --watch                                                                      #
# --------------------------------------------------------------------------- #
def _read_all(fd: int, sink: bytearray) -> None:
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:  # EIO: the other side of the pty is closed
            return
        if not chunk:
            return
        sink.extend(chunk)


def test_watch_on_a_terminal(pihello: Pihello, pihole: Target) -> None:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    modes = termios.tcgetattr(slave)
    output = bytearray()
    reader = threading.Thread(target=_read_all, args=(master, output), daemon=True)
    reader.start()
    try:
        with pihole.throttle():
            proc = pihello.popen("--watch", "1", stdin=slave, stdout=slave, stderr=subprocess.PIPE)
            try:
                time.sleep(3.2)
                proc.send_signal(signal.SIGINT)
                code = proc.wait(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                assert proc.stderr is not None
                stderr = proc.stderr.read().decode()
                proc.stderr.close()
        assert termios.tcgetattr(slave) == modes  # pihello leaves the terminal modes alone
    finally:
        os.close(slave)
        reader.join(timeout=5)
        os.close(master)
    text = output.decode("utf-8", "replace")
    assert code == 0, stderr
    assert stderr == ""
    assert text.startswith(ALT_SCREEN_ON), text[:200]
    assert text.rstrip("\r\n").endswith(ALT_SCREEN_OFF), text[-200:]
    frames = text.count("· every 1s · Ctrl-C to quit")
    assert 2 <= frames <= 5, text
    assert text.count("Pi-hole") >= frames
    assert "⚠" not in text


def test_watch_without_a_terminal(pihello: Pihello, pihole: Target) -> None:
    with pihole.throttle():
        proc = pihello.popen("--watch", "1", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(2.5)
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0, stderr
    assert stderr == b""
    text = stdout.decode()
    assert ESC not in text
    frames = [frame for frame in text.split("\n\n") if frame.strip()]
    assert len(frames) >= 2, text
    assert all(frame.startswith("Pi-hole on ") for frame in frames), text


# --------------------------------------------------------------------------- #
# --cache                                                                      #
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def api_debug_log(pihole: Target) -> Iterator[Callable[[], int] | None]:
    """Counts the ``POST /api/auth`` requests FTL logged (``debug.api``), or ``None``."""
    container = pihole.container
    if container is None:
        yield None
        return
    resp = pihole.observe("PATCH", "config", body={"config": {"debug": {"api": True}}})
    assert resp.ok, resp.text
    try:
        yield lambda: _count_auth_requests(container)
    finally:
        pihole.observe("PATCH", "config", body={"config": {"debug": {"api": False}}})


def _count_auth_requests(container: PiholeContainer) -> int:
    log = container.exec("cat", "/var/log/pihole/FTL.log").stdout
    return len(re.findall(r"-> POST /api/auth\b", log))


def test_cache_hit_needs_no_login(pihello: Pihello, pihole: Target, tmp_path: Path) -> None:
    marker = tmp_path / "password-used"
    command = (
        f"echo used >> {shlex.quote(str(marker))} && echo {shlex.quote(pihole.password or '')}"
    )
    env = {"PIHELLO_PASSWORD": None}
    with api_debug_log(pihole) as auth_requests:
        start = auth_requests() if auth_requests else 0
        first = pihello("--cache", "60", "--password-command", command, "--color", "never", env=env)
        assert first.returncode == 0, first
        assert marker.read_text().count("used") == 1
        if auth_requests:
            deadline = time.monotonic() + 3
            while auth_requests() == start and time.monotonic() < deadline:
                time.sleep(0.1)
            assert auth_requests() == start + 1
        cached = list((pihello.home / ".cache" / "pihello").iterdir())
        assert len(cached) == 1
        assert cached[0].stat().st_mode & 0o777 == 0o600

        second = pihello(
            "--cache", "60", "--password-command", command, "--color", "never", env=env, logins=0
        )
        assert second.returncode == 0, second
        assert second.lines[2:4] == first.lines[2:4], second  # versions and queries, cached
        assert marker.read_text().count("used") == 1  # the password was not even obtained
        if auth_requests:
            time.sleep(0.3)
            assert auth_requests() == start + 1  # and Pi-hole saw no login


# --------------------------------------------------------------------------- #
# Unreachable Pi-hole                                                          #
# --------------------------------------------------------------------------- #
def test_quiet_when_nothing_listens(make_pihello: Callable[[Target | None], Pihello]) -> None:
    port = find_free_ports(1, port_range=SETTINGS.ports)[0]
    pihello = make_pihello(None)
    run = pihello("-q", "--url", f"http://127.0.0.1:{port}", env={"PIHELLO_PASSWORD": "x" * 8})
    assert run.returncode == 3, run
    assert run.stdout == run.stderr == "", run
    assert run.duration < 3, run


def test_quiet_when_pihole_hangs(pihello: Pihello, pihole: Target) -> None:
    container = pihole.require_container()
    container.pause()
    try:
        run = pihello("-q", "--timeout", "1")
    finally:
        container.unpause()
    assert run.returncode == 3, run
    assert run.stdout == run.stderr == "", run
    assert run.duration < 1 + 1.5, run
    # FTL handles the queued login once it runs again, but pihello gave up and never
    # got the session id: that session can only expire (or be deleted, as here).
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not pihole.pihello_sessions():
        time.sleep(0.1)
    for session in pihole.pihello_sessions():
        pihole.delete_session(int(session["id"]))


# --------------------------------------------------------------------------- #
# Blocking state                                                               #
# --------------------------------------------------------------------------- #
def test_blocking_disabled_with_timer(pihello: Pihello, pihole: Target) -> None:
    pihole.require_container()  # changes the Pi-hole's state
    state = pihole.set_blocking(False, 300)
    try:
        assert state["blocking"] == "disabled"
        run = pihello("--color", "never")
        assert run.returncode == 0, run
        assert re.search(r"· ● disabled for (4m \d+s|5m)$", run.lines[0]), run
        status = pihello("--preset", "status", "--color", "never")
        assert re.fullmatch(r"● \S+% of \S+ · (4m \d+s|5m)\n", status.stdout), status
    finally:
        state = pihole.set_blocking(True)
    assert state["blocking"] == "enabled"
    run = pihello("--color", "never")
    assert run.returncode == 0, run
    assert run.lines[0].endswith("· ● enabled"), run


# --------------------------------------------------------------------------- #
# Several shells starting at once                                              #
# --------------------------------------------------------------------------- #
def test_concurrent_greetings(make_pihello: Callable[[Target], Pihello], pihole: Target) -> None:
    """5 greetings at once exceed FTL's 3 logins per second; the client retries."""
    runners = [make_pihello(pihole) for _ in range(5)]
    with pihole.throttle(3):  # the window is empty before, and blocked for 1 s after
        runs = run_together(runners, "--color", "never")
    for run in runs:
        assert run.returncode == 0, run
        assert run.stderr == "", run
        assert run.stdout.startswith("Pi-hole on "), run
