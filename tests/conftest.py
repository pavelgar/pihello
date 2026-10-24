"""Shared pytest configuration.

Every test runs with an isolated HOME / XDG directories and a scrubbed
environment so nothing ever reads or writes the developer's real pihello
config, cache or color preferences.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from helpers.fakepihole import FakePihole

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_SCRUBBED_ENV = (
    "PIHELLO_CONFIG",
    "PIHELLO_PROFILE",
    "PIHELLO_URL",
    "PIHELLO_PASSWORD",
    "PIHELLO_PASSWORD_FILE",
    "PIHELLO_PRESET",
    "PIHELLO_THEME",
    "NO_COLOR",
    "FORCE_COLOR",
    "CLICOLOR",
    "CLICOLOR_FORCE",
    "COLORTERM",
    "TERM",
    "WT_SESSION",
    "COLUMNS",
    "LINES",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "APPDATA",
    "LOCALAPPDATA",
)


@pytest.fixture(autouse=True)
def _isolated_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point HOME and XDG dirs at a temp dir and remove env vars that change behaviour."""
    home = tmp_path_factory.mktemp("home")
    for name in _SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    return home


@pytest.fixture
def home(_isolated_environment: Path) -> Path:
    """The isolated HOME directory for this test."""
    return _isolated_environment


@pytest.fixture
def fake_pihole_factory() -> Iterator[Callable[..., FakePihole]]:
    """Factory for started :class:`helpers.fakepihole.FakePihole` servers.

    ``fake_pihole_factory(fixture="2025.02.0", password=None, ...)`` — every
    server created is stopped at teardown.
    """
    from helpers.fakepihole import FakePihole

    servers: list[FakePihole] = []

    def factory(*args: Any, **kwargs: Any) -> FakePihole:
        server = FakePihole(*args, **kwargs)
        server.start()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.stop()


@pytest.fixture
def fake_pihole(fake_pihole_factory: Callable[..., FakePihole]) -> FakePihole:
    """A started fake Pi-hole replaying the newest recorded fixture set."""
    return fake_pihole_factory()
