"""Tests for :mod:`pihello.demo`: complete, consistent and shaped like live data."""

from __future__ import annotations

import itertools
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pytest

from helpers.fakepihole import latest_fixture
from pihello import demo, sources
from pihello.client import PiholeClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from helpers.fakepihole import FakePihole


def pattern(path: str) -> str:
    return re.sub(r"(?<=\.)\d+(?=\.|$)", "N", path)


@pytest.fixture
def variables() -> dict[str, Any]:
    return demo.variables()


def test_now_is_fixed() -> None:
    assert datetime.fromtimestamp(demo.NOW, timezone.utc) == datetime(
        2026, 10, 24, 8, 30, tzinfo=timezone.utc
    )


def test_variables_are_deterministic_fresh_copies() -> None:
    first, second = demo.variables(), demo.variables()
    assert first == second
    first["queries"]["total"] = -1
    first["top_domains"].clear()
    assert demo.variables() == second


def test_covers_every_namespace(variables: dict[str, Any]) -> None:
    assert set(variables) == sources.NAMESPACES


def test_every_leaf_is_documented_and_every_variable_present(variables: dict[str, Any]) -> None:
    """The catalog documents exactly the demo tree (with ``N`` for list indices)."""
    flat = sources.flatten(variables)
    documented = {info.path for info in sources.catalog()}
    leaves = {pattern(path) for path in flat}
    containers = {info.path for info in sources.catalog() if info.type == "list"}
    assert leaves - documented == set(), "undocumented demo variables"
    assert documented - leaves - containers == set(), "catalog entries missing from demo"
    for path in containers:
        assert any(p.startswith(path + ".") for p in leaves) or path in leaves, path


def test_no_missing_values_except_unknown_hardware(variables: dict[str, Any]) -> None:
    nulls = {path for path, value in sources.flatten(variables).items() if value is None}
    allowed = re.compile(
        r"^(timer|host\.dmi\..*|version\.docker\.(local|remote|update)|"
        r"sensors\.list\.\d+\.temps\.\d+\.(name|max|crit))$"
    )
    assert {path for path in nulls if not allowed.match(path)} == set()


def test_same_shape_as_collect(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    """Serving the demo payloads over HTTP and collecting gives exactly demo.variables()."""
    fake = fake_pihole_factory("demo")
    with PiholeClient(fake.url, "pihello-test") as client:
        collected, problems = sources.collect(client, now=demo.NOW)
    assert problems == []
    expected = demo.variables()
    expected["pihole"] = collected["pihole"]
    assert collected == expected


@pytest.mark.skipif(latest_fixture() is None, reason="no recorded fixtures yet")
def test_shape_matches_latest_recorded_pihole(
    variables: dict[str, Any], fake_pihole_factory: Callable[..., FakePihole]
) -> None:
    fake = fake_pihole_factory("latest")
    with PiholeClient(fake.url, "pihello-test") as client:
        live, problems = sources.collect(client)
    assert problems == []
    demo_flat = {pattern(p): v for p, v in sources.flatten(variables).items()}
    live_flat = {pattern(p): v for p, v in sources.flatten(live).items()}
    containers = {info.path for info in sources.catalog() if info.type == "list"}
    assert set(live_flat) - set(demo_flat) - containers == set()
    for path, value in live_flat.items():
        other = demo_flat.get(path)
        if value is None or other is None or path in containers:
            continue
        assert type(value) is type(other) or {type(value), type(other)} <= {int, float}, path


# --------------------------------------------------------------------------- #
# Realism and internal consistency                                             #
# --------------------------------------------------------------------------- #
def test_query_totals(variables: dict[str, Any]) -> None:
    queries = variables["queries"]
    assert 45_000 <= queries["total"] <= 52_000
    assert 13.0 <= queries["percent_blocked"] <= 17.0
    assert queries["percent_blocked"] == pytest.approx(queries["blocked"] / queries["total"] * 100)
    assert queries["blocked"] + queries["cached"] + queries["forwarded"] == queries["total"]
    assert sum(queries["types"].values()) == queries["total"]
    assert sum(queries["replies"].values()) == queries["total"]
    assert sum(queries["status"].values()) == queries["total"]
    assert queries["unique_domains"] < queries["total"]
    assert 0.1 < queries["frequency"] < 2


def test_history_curve(variables: dict[str, Any]) -> None:
    history = variables["history"]
    assert len(history) == 144
    stamps = [bucket["timestamp"] for bucket in history]
    assert all(b - a == 600 for a, b in itertools.pairwise(stamps))
    assert demo.NOW - 86400 < stamps[0] < stamps[-1] < demo.NOW
    for key in ("total", "blocked", "cached", "forwarded"):
        assert sum(b[key] for b in history) == variables["queries"][key]
    for bucket in history:
        assert bucket["blocked"] + bucket["cached"] + bucket["forwarded"] == bucket["total"]
        assert min(bucket.values()) >= 0

    def hour(bucket: dict[str, int]) -> int:
        return datetime.fromtimestamp(bucket["timestamp"], timezone.utc).hour

    night = [b["total"] for b in history if 1 <= hour(b) < 6]
    evening = [b["total"] for b in history if 18 <= hour(b) < 22]
    assert max(night) < min(evening) / 2


def test_gravity(variables: dict[str, Any]) -> None:
    gravity = variables["gravity"]
    assert 100_000 <= gravity["domains_being_blocked"] <= 150_000
    assert gravity["relative"] == {"days": 3, "hours": 4, "minutes": 12}
    assert gravity["last_update"] < demo.NOW
    assert variables["ftl"]["database"]["gravity"] == gravity["domains_being_blocked"]


def test_clients(variables: dict[str, Any]) -> None:
    assert variables["clients"] == {"active": 12, "total": 14}
    top = variables["top_clients"]
    assert len(top) == 10
    assert sum(c["count"] for c in top) < variables["queries"]["total"]
    assert (
        sum(c["count"] for c in variables["top_blocked_clients"]) <= variables["queries"]["blocked"]
    )
    assert [c["count"] for c in top] == sorted((c["count"] for c in top), reverse=True)
    assert any(c["name"] == "" and c["label"] == c["ip"] for c in top)
    assert all(c["label"] for c in top)


def test_domains(variables: dict[str, Any]) -> None:
    blocked = [d["domain"] for d in variables["top_blocked"]]
    assert "pagead2.googlesyndication.com" in blocked
    assert any("doubleclick.net" in d for d in blocked)
    for key in ("top_domains", "top_blocked"):
        counts = [d["count"] for d in variables[key]]
        assert len(counts) == 10
        assert counts == sorted(counts, reverse=True)
    assert sum(d["count"] for d in variables["top_blocked"]) <= variables["queries"]["blocked"]
    assert variables["recent_blocked"] == variables["recent_blocked_list"][0]
    assert len(variables["recent_blocked_list"]) == 10


def test_upstreams(variables: dict[str, Any]) -> None:
    upstreams = {u["ip"]: u for u in variables["upstreams"]}
    assert list(upstreams) == ["9.9.9.9", "149.112.112.112"]
    assert sum(u["count"] for u in upstreams.values()) == variables["queries"]["forwarded"]
    assert upstreams["9.9.9.9"]["label"] == "dns9.quad9.net#53"
    assert variables["upstreams"][0]["count"] > variables["upstreams"][1]["count"]
    assert 0 < upstreams["9.9.9.9"]["statistics"]["response"] < 0.1


def test_versions_have_exactly_one_update(variables: dict[str, Any]) -> None:
    assert variables["updates"] == {
        "available": True,
        "count": 1,
        "components": ["ftl"],
        "status": "available",
    }
    version = variables["version"]
    assert [version[c]["update"] for c in ("core", "web", "ftl", "docker")] == [
        False,
        False,
        True,
        None,
    ]
    assert version["core"]["local"]["version"] == "v6.5"
    assert version["ftl"]["remote"]["version"] == "v6.8"


def test_host_is_a_raspberry_pi_5(variables: dict[str, Any]) -> None:
    assert variables["host"]["model"].startswith("Raspberry Pi 5")
    assert variables["host"]["uname"]["machine"] == "aarch64"
    assert variables["system"]["cpu"]["nprocs"] == 4
    ram = variables["system"]["memory"]["ram"]
    assert 7.5 * 1024**2 < ram["total"] < 8.1 * 1024**2  # KiB
    assert ram["%used"] == pytest.approx(ram["used"] / ram["total"] * 100)
    assert ram["used"] + ram["free"] <= ram["total"]
    load = variables["system"]["cpu"]["load"]
    assert load["percent"] == pytest.approx([v / 4 * 100 for v in load["raw"]])


def test_sensors(variables: dict[str, Any]) -> None:
    sensors = variables["sensors"]
    assert round(sensors["cpu_temp"]) == 52
    assert sensors["cpu_temp"] < sensors["hot_limit"]
    assert sensors["unit"] == "C"
    assert isinstance(sensors["hot_limit"], float)


def test_durations_and_timestamps(variables: dict[str, Any]) -> None:
    assert 17 * 86400 < variables["system"]["uptime"] < 19 * 86400
    assert 5 * 86400 < variables["ftl"]["uptime"] < variables["system"]["uptime"]
    database = variables["database"]
    for key in ("atime", "mtime", "ctime", "earliest_timestamp", "earliest_timestamp_disk"):
        assert database[key] < demo.NOW
    assert database["earliest_timestamp_disk"] < database["earliest_timestamp"]
    assert database["queries"] == variables["queries"]["total"]


def test_network_and_builtins(variables: dict[str, Any]) -> None:
    assert variables["network"] == {
        "gateway": "192.168.1.1",
        "interface": "eth0",
        "address": "192.168.1.2",
        "addresses": ["192.168.1.2", "fd12:3456:789a:1::2"],
    }
    assert variables["now"] == demo.NOW
    assert variables["pihole"] == {"url": "http://pi.hole", "host": "pi.hole"}
    assert variables["blocking"] == "enabled"
    assert variables["timer"] is None
    assert variables["messages"] == {"count": 0}


def test_api_payloads_cover_every_source() -> None:
    payloads = demo.api_payloads()
    assert set(payloads) == {s.endpoint for s in sources.SOURCES}
    assert all("took" in body for body in payloads.values())
