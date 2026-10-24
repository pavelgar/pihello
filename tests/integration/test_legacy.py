"""pihello 1.x invocations: ``pihello HOST:PORT PASSWORD [-p] [-k] [-f FILE] ...``."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import pytest

from pihello.template import visible_width

if TYPE_CHECKING:
    from pathlib import Path

    from .support import Pihello, Run, Target

pytestmark = pytest.mark.integration

NO_ENV = {"PIHELLO_URL": None, "PIHELLO_PASSWORD": None}
RULE = "─" * 53

#: The example template shipped with pihello 1.x (docs/example.txt).
TEMPLATE_1X = """\
[cyan2]─────────────────────────────────────────────────────[]
[white]PiHole[] ([green4]{blocking}[]) [lightgreen]{version.core.local.version}[white], Web \
[lightgreen]{version.web.local.version}[white], FTL [lightgreen]{version.ftl.local.version}
[cyan2]─────────────────────────────────────────────────────[]
Blocking [darkcyan]{gravity.domains_being_blocked}[] domains for [steelblue]{clients.active}[] \
clients
Blocked [fuchsia]{queries.blocked}[] out of [lightgreen]{queries.total}[] queries \
[underline]today[] ([steelblue]{queries.percent_blocked}%[])
[grey37]Gravity last updated [bold grey50]{gravity.relative.days}[grey37] days \
[bold grey50]{gravity.relative.hours}[grey37] hours and [bold grey50]{gravity.relative.minutes}\
[grey37] minutes ago
"""


def address(url: str) -> str:
    return url.split("://", 1)[1]


def assert_classic(run: Run, pihole: Target) -> None:
    assert run.returncode == 0, run
    lines = run.lines
    assert len(lines) == 6, run
    version = pihole.versions()
    core, web, ftl = (version[c]["local"]["version"] for c in ("core", "web", "ftl"))
    assert lines[0] == lines[2] == RULE, run
    assert lines[1] == f"PiHole (enabled) {core}, Web {web}, FTL {ftl}", run
    assert re.fullmatch(r"Blocking \d+ domains for \d+ clients", lines[3]), run
    assert re.fullmatch(r"Blocked \d+ out of \d+ queries today \(\d+\.\d%\)", lines[4]), run
    gravity = r"Gravity last updated \d+ days \d+ hours and \d+ minutes ago"
    assert re.fullmatch(gravity, lines[5]), run


def test_positional_password(pihello: Pihello, pihole: Target) -> None:
    run = pihello(address(pihole.url), pihole.password or "", env=NO_ENV)
    assert_classic(run, pihole)
    assert "warning: a password on the command line leaks into shell history" in run.stderr
    assert (pihole.password or "") not in run.stderr
    quiet = pihello(address(pihole.url), pihole.password or "", "-q", env=NO_ENV)
    assert_classic(quiet, pihole)
    assert quiet.stderr == ""


def test_https_insecure(pihello: Pihello, pihole: Target) -> None:
    https = address(pihole.https_url)
    run = pihello(https, pihole.password or "", "-p", "-k", "-q", env=NO_ENV)
    assert_classic(run, pihole)
    # without -k, Pi-hole's self-signed certificate is rejected
    run = pihello(https, pihole.password or "", "-p", "-q", env=NO_ENV)
    assert run.returncode == 3, run


def test_template_file_and_output_options(pihello: Pihello, pihole: Target, tmp_path: Path) -> None:
    template = tmp_path / "template.txt"
    template.write_text(TEMPLATE_1X, encoding="utf-8")
    args = (address(pihole.url), pihole.password or "", "-q", "-f", str(template))
    assert_classic(pihello(*args, env=NO_ENV), pihole)

    run = pihello(*args, "-ts", "%Y", "-W", "30", "-c", "-H", "4", "-i", "2", env=NO_ENV)
    assert run.returncode == 0, run
    lines = run.lines
    assert len(lines) == 4, run
    assert lines[0] == time.strftime("%Y"), run
    assert lines[1] == RULE[:30], run
    assert all(visible_width(line) <= 30 for line in lines), run
