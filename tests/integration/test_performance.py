"""How long a greeting takes end to end (a new process, login, fetch, render, logout)."""

from __future__ import annotations

import statistics

import pytest

from .support import NOTES, SETTINGS, Pihello

pytestmark = pytest.mark.integration

RUNS = 10
BUDGET = 0.300  # seconds; pihello runs at every shell start


def _median(pihello: Pihello, *args: str, logins: int | None = None) -> tuple[float, list[float]]:
    times = []
    for _ in range(RUNS):
        run = pihello(*args, logins=logins)
        assert run.returncode == 0, run
        times.append(run.duration)
    return statistics.median(times), times


def test_default_greeting_latency(pihello: Pihello) -> None:
    startup, _ = _median(pihello, "--version", logins=0)
    demo, _ = _median(pihello, "--demo", "--color", "always", logins=0)
    live, times = _median(pihello, "--color", "always")
    flag = "  ** over the 300 ms budget **" if live > BUDGET else ""
    NOTES.append(
        f"perf {SETTINGS.tag}: default greeting median {live * 1000:.0f} ms "
        f"(min {min(times) * 1000:.0f}, max {max(times) * 1000:.0f}, n={RUNS}); "
        f"--demo {demo * 1000:.0f} ms; --version {startup * 1000:.0f} ms{flag}"
    )
    # A sanity bound only (CI machines are slow); the budget is reported above.
    assert live < 1.5, times
