"""Fixtures for the end-to-end tests against real Pi-hole containers (see ``support.py``)."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest

from .support import (
    NOTES,
    SETTINGS,
    ItSettings,
    Pihello,
    Target,
    assert_no_pihello_sessions,
    seed,
    start_target,
    stop_target,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# The suite drives pihello through a pty (fcntl/termios) and needs a Linux
# container runtime, so on Windows it isn't even collected (CI runs it on Linux).
collect_ignore_glob = ["test_*.py"] if sys.platform == "win32" else []


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated_environment() -> None:
    """Replaces the unit tests' fake HOME/XDG dirs for this process.

    Rootless podman keeps its containers under the real ``$HOME`` / ``$XDG_*``,
    so the test process must keep them; pihello itself always runs in a
    subprocess with an isolated environment (:class:`Pihello`).
    """


@pytest.fixture(scope="session")
def it_settings() -> ItSettings:
    return SETTINGS


@pytest.fixture(scope="session")
def pihole() -> Iterator[Target]:
    """The main Pi-hole (password ``pihello-test``), seeded with lists and queries."""
    if SETTINGS.external:
        assert SETTINGS.url is not None
        target = Target("external", SETTINGS.url, SETTINGS.password)
        yield target
        target.close()
        return
    target = start_target("main")
    try:
        target.seeded = seed(target)
        yield target
    finally:
        stop_target(target)


@pytest.fixture
def make_pihello(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Callable[..., Pihello]]:
    """``make_pihello(target)`` → a :class:`Pihello` runner with a fresh HOME.

    At teardown every target used must have no pihello session left.
    """
    used: list[Target] = []

    def factory(target: Target | None) -> Pihello:
        if target is not None and target not in used:
            used.append(target)
        return Pihello(tmp_path_factory.mktemp("home"), target)

    yield factory
    for target in used:
        assert_no_pihello_sessions(target)


@pytest.fixture
def pihello(make_pihello: Callable[..., Pihello], pihole: Target) -> Pihello:
    """A runner for the main Pi-hole (``PIHELLO_URL`` / ``PIHELLO_PASSWORD`` set)."""
    return make_pihello(pihole)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Print notes collected by the tests (performance numbers, skipped checks)."""
    if NOTES:
        terminalreporter.section("pihello integration notes")
        for line in NOTES:
            terminalreporter.write_line(line)
