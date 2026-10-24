"""Tests for :mod:`pihello.sources` (planning, fetching, transforms, catalog)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

from helpers.fakepihole import FakePihole, available_fixtures, load_fixture
from pihello import __version__
from pihello.client import PiholeClient
from pihello.errors import (
    APIError,
    AuthenticationError,
    ConnectionFailed,
    EndpointNotFound,
    TwoFactorRequired,
)
from pihello.sources import (
    BUILTIN_NAMESPACES,
    NAMESPACES,
    SOURCES,
    FetchProblem,
    Source,
    builtins,
    catalog,
    collect,
    fetch,
    flatten,
    from_responses,
    plan,
    plan_all,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

PASSWORD = "pihello-test"
FIXTURES = [*available_fixtures(), "demo"]
CONTAINER_TYPES = {"list"}
VALID_TYPES = {"int", "float", "str", "bool", "timestamp", "list", "list[str]", "list[float]"}


def pattern(path: str) -> str:
    """``top_domains.3.domain`` → ``top_domains.N.domain``."""
    return re.sub(r"(?<=\.)\d+(?=\.|$)", "N", path)


def leaf_patterns(variables: dict[str, Any]) -> set[str]:
    return {pattern(path) for path in flatten(variables)}


def catalog_paths() -> set[str]:
    return {info.path for info in catalog()}


def source(name: str) -> Source:
    return next(s for s in SOURCES if s.name == name)


@pytest.fixture
def client(fake_pihole: FakePihole) -> Iterator[PiholeClient]:
    with PiholeClient(fake_pihole.url, PASSWORD, timeout=3) as pihole:
        yield pihole


# --------------------------------------------------------------------------- #
# Source table and planning                                                    #
# --------------------------------------------------------------------------- #
def test_source_table_is_consistent() -> None:
    names = [s.name for s in SOURCES]
    assert len(names) == len(set(names))
    endpoints = [s.endpoint for s in SOURCES]
    assert len(endpoints) == len(set(endpoints))
    namespaces = [ns for s in SOURCES for ns in s.namespaces]
    assert len(namespaces) == len(set(namespaces))
    assert set(namespaces) | BUILTIN_NAMESPACES == NAMESPACES
    for s in SOURCES:
        assert s.description
        assert not s.endpoint.startswith("/")
        query = s.endpoint.partition("?")[2]
        assert query == "&".join(sorted(query.split("&"))) if query else True


def test_namespace_table_matches_contract() -> None:
    expected = {
        "summary": ("queries", "clients", "gravity"),
        "blocking": ("blocking", "timer"),
        "recent_blocked": ("recent_blocked", "recent_blocked_list"),
        "version": ("version", "updates"),
        "system": ("system",),
        "host": ("host",),
        "sensors": ("sensors",),
        "ftl": ("ftl",),
        "database": ("database",),
        "messages": ("messages",),
        "top_domains": ("top_domains",),
        "top_blocked": ("top_blocked",),
        "top_clients": ("top_clients",),
        "top_blocked_clients": ("top_blocked_clients",),
        "upstreams": ("upstreams",),
        "history": ("history",),
        "network": ("network",),
    }
    assert {s.name: s.namespaces for s in SOURCES} == expected
    assert source("summary").endpoint == "stats/summary"
    assert source("recent_blocked").endpoint == "stats/recent_blocked?count=10"
    assert source("top_blocked").endpoint == "stats/top_domains?blocked=true&count=10"
    assert source("top_blocked_clients").endpoint == "stats/top_clients?blocked=true&count=10"
    assert source("network").endpoint == "network/gateway"


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        ([], ()),
        (["now", "pihello.version", "pihole.url"], ()),
        (["unknown.thing"], ()),
        (["queries.total"], ("summary",)),
        (["gravity.relative.days", "clients.active", "queries"], ("summary",)),
        (["blocking"], ("blocking",)),
        (["timer"], ("blocking",)),
        (["recent_blocked"], ("recent_blocked",)),
        (["updates.available"], ("version",)),
        (["top_domains.0.domain", "queries.total"], ("summary", "top_domains")),
        (["network.address", " system.uptime "], ("system", "network")),
        (
            ["history", "upstreams.0.label", "version.core.local.version"],
            ("version", "upstreams", "history"),
        ),
    ],
)
def test_plan(paths: list[str], expected: tuple[str, ...]) -> None:
    assert tuple(s.name for s in plan(paths)) == expected


def test_plan_all() -> None:
    assert plan_all() == SOURCES
    assert plan(NAMESPACES) == SOURCES


# --------------------------------------------------------------------------- #
# Collecting from (fake) Pi-holes of every recorded version                    #
# --------------------------------------------------------------------------- #
def _has_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_has_key(v, key) for v in value.values())
    if isinstance(value, list):
        return any(_has_key(v, key) for v in value)
    return False


@pytest.fixture(params=FIXTURES)
def versioned_pihole(
    request: pytest.FixtureRequest, fake_pihole_factory: Callable[..., FakePihole]
) -> FakePihole:
    return fake_pihole_factory(request.param)


def test_collect_every_version(versioned_pihole: FakePihole) -> None:
    with PiholeClient(versioned_pihole.url, PASSWORD) as client:
        variables, problems = collect(client, now=1792830600.0)
    assert problems == []
    assert not _has_key(variables, "took")
    assert set(variables) == NAMESPACES
    undocumented = leaf_patterns(variables) - catalog_paths()
    assert undocumented == set(), f"document these in sources._CATALOG: {sorted(undocumented)}"
    assert versioned_pihole.active_sessions == {}


def test_collect_value_types_every_version(versioned_pihole: FakePihole) -> None:
    with PiholeClient(versioned_pihole.url, PASSWORD) as client:
        variables, _ = collect(client)
    types = {info.path: info.type for info in catalog()}
    checks = {
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "float": lambda v: isinstance(v, float),
        "str": lambda v: isinstance(v, str),
        "bool": lambda v: isinstance(v, bool),
        "timestamp": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "list": lambda v: isinstance(v, list),
        "list[str]": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
        "list[float]": lambda v: isinstance(v, list) and all(isinstance(x, float) for x in v),
    }
    wrong = {
        path: value
        for path, value in flatten(variables).items()
        if value is not None and not checks[types[pattern(path)]](value)
    }
    assert wrong == {}


def test_collect_normalises_across_versions(versioned_pihole: FakePihole) -> None:
    with PiholeClient(versioned_pihole.url, PASSWORD) as client:
        variables, _ = collect(client, now=1792830600.0)
    assert variables["blocking"] in {"enabled", "disabled", "failed", "unknown"}
    assert isinstance(variables["queries"]["percent_blocked"], float)
    assert variables["recent_blocked"] == (variables["recent_blocked_list"] or [""])[0]
    for kind in ("allowed", "denied"):
        assert isinstance(variables["ftl"]["database"]["domains"][kind]["total"], int)
        assert isinstance(variables["ftl"]["database"]["regex"][kind]["total"], int)
    assert variables["ftl"]["uptime"] < 10 * 365 * 86400  # seconds, not milliseconds
    for client_entry in variables["top_clients"] + variables["top_blocked_clients"]:
        assert client_entry["label"] == (client_entry["name"] or client_entry["ip"])
    for upstream in variables["upstreams"]:
        assert upstream["label"].startswith(upstream["name"] or upstream["ip"])
    updates = variables["updates"]
    assert updates["count"] == len(updates["components"])
    assert updates["available"] is bool(updates["components"])
    for component in ("core", "web", "ftl", "docker"):
        update = variables["version"][component]["update"]
        assert update is None or isinstance(update, bool)
    assert variables["version"]["docker"]["remote"] != "null"
    assert all(u["ip"] not in ("blocklist", "cache") for u in variables["upstreams"])
    assert set(variables["network"]) == {"gateway", "interface", "address", "addresses"}
    assert variables["pihole"]["url"] == versioned_pihole.url
    assert variables["pihole"]["host"] == "127.0.0.1"
    assert variables["pihello"] == {"version": __version__}


def test_collect_only_fetches_needed_endpoints(
    fake_pihole: FakePihole, client: PiholeClient
) -> None:
    variables, problems = collect(client, ["queries.total", "gravity.relative.days", "now"])
    assert problems == []
    assert set(variables) == {"queries", "clients", "gravity"} | BUILTIN_NAMESPACES
    fetched = {r.endpoint for r in fake_pihole.requests if r.method == "GET"}
    assert fetched == {"stats/summary"}


def test_collect_builtins_only_makes_no_requests(
    fake_pihole: FakePihole, client: PiholeClient
) -> None:
    before = len(fake_pihole.requests)
    variables, problems = collect(client, ["now", "pihello.version"], now=12.5)
    assert problems == []
    assert variables["now"] == 12.5
    assert len(fake_pihole.requests) == before


def test_collect_raw_fixture_matches_transforms(versioned_pihole: FakePihole) -> None:
    """collect() over HTTP equals from_responses() over the recorded payloads."""
    fixture = load_fixture(versioned_pihole.fixture)["responses"]
    payloads = {
        s.endpoint: fixture[f"GET /api/{s.endpoint}"]["body"]
        for s in SOURCES
        if fixture.get(f"GET /api/{s.endpoint}", {}).get("status") == 200
    }
    expected = from_responses(payloads, url=versioned_pihole.url, now=100.0)
    with PiholeClient(versioned_pihole.url, PASSWORD) as client:
        actual, _ = collect(client, now=100.0)
    assert actual == expected


# --------------------------------------------------------------------------- #
# Failures                                                                     #
# --------------------------------------------------------------------------- #
def test_missing_endpoint_leaves_namespace_missing(
    fake_pihole: FakePihole, client: PiholeClient
) -> None:
    fake_pihole.remove("network/gateway")
    variables, problems = collect(client)
    assert "network" not in variables
    assert "queries" in variables
    assert [p.source.name for p in problems] == ["network"]
    assert isinstance(problems[0].error, EndpointNotFound)


def test_failures_are_recorded_per_source(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.fail("info/sensors", status=500)
    fake_pihole.fail("history", raw=b"<html>")
    fake_pihole.set_response("GET /api/stats/summary", 200, ["not", "an", "object"])
    fake_pihole.set_response("GET /api/info/host", 200, {"host": "nope"})
    variables, problems = collect(client)
    failed = {p.source.name: p.error for p in problems}
    assert set(failed) == {"summary", "host", "sensors", "history"}
    assert all(isinstance(e, APIError) for e in failed.values())
    assert "unexpected response from /api/stats/summary" in str(failed["summary"])
    assert failed["summary"].hint
    assert failed["sensors"].status == 500  # type: ignore[attr-defined]
    for namespace in ("queries", "host", "sensors", "history"):
        assert namespace not in variables
    assert "blocking" in variables
    assert all(isinstance(p, FetchProblem) for p in problems)
    assert [p.source.name for p in problems] == [
        s.name for s in SOURCES if s.name in failed
    ]  # deterministic order


def test_timeout_on_one_source_is_a_problem(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("info/database", delay=2)
    with PiholeClient(fake_pihole.url, PASSWORD, timeout=0.3) as client:
        variables, problems = collect(client, ["database.size", "queries.total"])
    assert "queries" in variables
    assert [p.source.name for p in problems] == ["database"]
    assert isinstance(problems[0].error, ConnectionFailed)


def test_every_source_unreachable_raises(fake_pihole: FakePihole) -> None:
    # Windows retries a refused connection for about 2 s, so leave room for that
    client = PiholeClient(fake_pihole.url, timeout=5)
    fake_pihole.stop()
    with pytest.raises(ConnectionFailed, match="refused"):
        collect(client, ["queries.total", "blocking", "version"])
    with pytest.raises(ConnectionFailed):
        collect(client, ["queries.total"])


def test_non_finite_numbers_do_not_crash_collect(fake_pihole: FakePihole) -> None:
    body = b'{"queries": {"frequency": NaN}, "clients": {}, "gravity": {"last_update": Infinity}}'
    fake_pihole.fail("stats/summary", raw=body)
    with PiholeClient(fake_pihole.url, PASSWORD) as client:
        variables, problems = collect(client, ["gravity.relative.days"], now=1792830600.0)
    assert problems == []
    assert variables["gravity"] == {"last_update": None}
    assert variables["queries"] == {"frequency": None}


def test_all_failed_with_api_errors_does_not_raise(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("stats/summary", status=500)
    with PiholeClient(fake_pihole.url, PASSWORD) as client:
        variables, problems = collect(client, ["queries.total"])
    assert len(problems) == 1
    assert "queries" not in variables
    assert "now" in variables


def test_authentication_error_propagates(fake_pihole: FakePihole) -> None:
    client = PiholeClient(fake_pihole.url, "wrong-password")
    with pytest.raises(AuthenticationError):
        collect(client)
    with pytest.raises(AuthenticationError):
        collect(client, ["queries.total"])
    unauthenticated = PiholeClient(fake_pihole.url)
    with pytest.raises(AuthenticationError, match="requires a password"):
        fetch(unauthenticated, SOURCES[:3], max_workers=1)


def test_two_factor_error_propagates(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(totp=True)
    with pytest.raises(TwoFactorRequired):
        collect(PiholeClient(fake.url, PASSWORD))


def test_fetch_sequential_and_parallel_agree(client: PiholeClient) -> None:
    sequential, p1 = fetch(client, SOURCES, max_workers=1)
    parallel, p2 = fetch(client, SOURCES, max_workers=6)
    assert sequential == parallel
    assert p1 == p2 == []


def test_fetch_deduplicates_and_handles_empty(
    fake_pihole: FakePihole, client: PiholeClient
) -> None:
    assert fetch(client, []) == ({}, [])
    variables, _ = fetch(client, [source("blocking"), source("blocking")])
    assert set(variables) == {"blocking", "timer"}
    assert len(fake_pihole.requests_to("dns/blocking")) == 1


def test_fetch_sequential_records_problems(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.remove("dns/blocking")
    variables, problems = fetch(client, [source("blocking")])
    assert variables == {}
    assert isinstance(problems[0].error, EndpointNotFound)


# --------------------------------------------------------------------------- #
# Transforms                                                                   #
# --------------------------------------------------------------------------- #
def _version_payload(**components: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "core": {"local": {"version": "v6.5", "branch": "master"}, "remote": {"version": "v6.5"}},
        "web": {"local": {"version": "v6.6.1"}, "remote": {"version": "v6.6.1"}},
        "ftl": {"local": {"version": "v6.8"}, "remote": {"version": "v6.8"}},
        "docker": {"local": None, "remote": None},
    }
    base.update(components)
    return {"version": base, "took": 0.1}


@pytest.mark.parametrize(
    ("local", "remote", "update"),
    [
        ("v6.5", "v6.5", False),
        ("v6.4", "v6.5", True),
        ("v6.5", "v6.5.1", True),
        ("v6.9", "v6.10", True),
        ("v6.10", "v6.9", False),
        ("v6.6", "v6.5", False),
        ("vDev-eaa7dbb", "v6.8", True),
        ("v6.5", None, None),
        ("v6.5", "", None),
        ("v6.5", "null", None),
        (None, "v6.5", None),
        ("null", "v6.5", None),
    ],
)
def test_component_update(local: Any, remote: Any, update: bool | None) -> None:
    payload = _version_payload(core={"local": {"version": local}, "remote": {"version": remote}})
    result = source("version").transform(payload)
    assert result["version"]["core"]["update"] is update
    assert result["updates"]["components"] == (["core"] if update else [])


@pytest.mark.parametrize(
    ("local_hash", "remote_hash", "update"),
    [("abc", "abc", False), ("abc", "def", True), ("abc", None, None), (None, "def", None)],
)
def test_non_master_branch_compares_hashes(
    local_hash: Any, remote_hash: Any, update: bool | None
) -> None:
    core = {
        "local": {"version": "v6.5", "branch": "development", "hash": local_hash},
        "remote": {"version": None, "hash": remote_hash},
    }
    result = source("version").transform(_version_payload(core=core))
    assert result["version"]["core"]["update"] is update


def test_null_strings_are_normalised() -> None:
    payload = _version_payload(
        core={"local": {"version": "v6.5", "hash": ""}, "remote": {"version": "null"}},
        docker={"local": "2026.10.0", "remote": "null"},
    )
    version = source("version").transform(payload)["version"]
    assert version["core"]["remote"]["version"] is None
    assert version["core"]["local"]["hash"] is None
    assert version["docker"] == {"local": "2026.10.0", "remote": None, "update": None}


@pytest.mark.parametrize(
    ("local", "remote", "update"),
    [
        ("2026.10.0", "2026.10.0", False),
        ("2025.02.0", "2026.10.0", True),
        ("2026.10.1", "2026.10.0", False),
        ("nightly", "2026.10.0", True),
        ("2026.10.0", None, None),
        ("2026.10.0", "null", None),
        (None, None, None),
    ],
)
def test_docker_update(local: Any, remote: Any, update: bool | None) -> None:
    payload = _version_payload(docker={"local": local, "remote": remote})
    result = source("version").transform(payload)
    assert result["version"]["docker"]["update"] is update


def test_updates_in_docker_only_count_the_image() -> None:
    payload = _version_payload(
        core={"local": {"version": "v6.0"}, "remote": {"version": "v6.5"}},
        ftl={"local": {"version": "v6.0"}, "remote": {"version": "v6.8"}},
        docker={"local": "2025.02.0", "remote": "2026.10.0"},
    )
    result = source("version").transform(payload)
    assert result["version"]["core"]["update"] is True
    assert result["updates"] == {
        "available": True,
        "count": 1,
        "components": ["docker"],
        "status": "available",
    }
    current = _version_payload(
        core={"local": {"version": "v6.0"}, "remote": {"version": "v6.5"}},
        docker={"local": "2026.10.0", "remote": "2026.10.0"},
    )
    assert source("version").transform(current)["updates"]["available"] is False
    assert source("version").transform(current)["updates"]["status"] == "current"


def test_updates_bare_metal() -> None:
    payload = _version_payload(
        web={"local": {"version": "v6.5"}, "remote": {"version": "v6.6.1"}},
        ftl={"local": {"version": "v6.7"}, "remote": {"version": "v6.8"}},
    )
    updates = source("version").transform(payload)["updates"]
    assert updates == {
        "available": True,
        "count": 2,
        "components": ["web", "ftl"],
        "status": "available",
    }


@pytest.mark.parametrize(
    ("components", "status"),
    [
        # Pi-hole could not reach GitHub: no remote versions at all (real containers
        # report docker.remote as the string "null").
        (
            {
                "core": {"local": {"version": "v6.5"}, "remote": {"version": None}},
                "docker": {"local": "2026.10.0", "remote": "null"},
            },
            "unknown",
        ),
        # Bare metal, nothing known.
        (
            {
                name: {"local": {"version": "v6.5"}, "remote": {"version": ""}}
                for name in ("core", "web", "ftl")
            },
            "unknown",
        ),
        # Bare metal, one component known to be current.
        (
            {
                "core": {"local": {"version": "v6.5"}, "remote": {"version": "v6.5"}},
                "web": {"local": {"version": "v6.6"}, "remote": {"version": None}},
            },
            "current",
        ),
        # In Docker only the image counts: a known core version doesn't help.
        (
            {
                "core": {"local": {"version": "v6.5"}, "remote": {"version": "v6.5"}},
                "docker": {"local": "2026.10.0", "remote": None},
            },
            "unknown",
        ),
    ],
)
def test_update_status(components: dict[str, Any], status: str) -> None:
    updates = source("version").transform(_version_payload(**components))["updates"]
    assert updates["status"] == status
    assert updates["available"] is False


def test_version_tolerates_unknown_components() -> None:
    payload = _version_payload(extra="weird", odd={"local": "x", "remote": "y"})
    result = source("version").transform(payload)["version"]
    assert result["extra"] == "weird"
    assert result["odd"]["update"] is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("enabled", "enabled"),
        ("disabled", "disabled"),
        ("failed", "failed"),
        ("unknown", "unknown"),
        (True, "enabled"),
        (False, "disabled"),
        ("bogus", "unknown"),
        (None, "unknown"),
    ],
)
def test_blocking_normalisation(raw: Any, expected: str) -> None:
    result = source("blocking").transform({"blocking": raw, "timer": None, "took": 0})
    assert result == {"blocking": expected, "timer": None}


def test_timer_is_float() -> None:
    variables = from_responses({"dns/blocking": {"blocking": "disabled", "timer": 300}})
    assert variables["timer"] == 300.0
    assert isinstance(variables["timer"], float)


def test_recent_blocked() -> None:
    transform = source("recent_blocked").transform
    assert transform({"blocked": []}) == {"recent_blocked": "", "recent_blocked_list": []}
    assert transform({"blocked": ["a.com", "b.com"]}) == {
        "recent_blocked": "a.com",
        "recent_blocked_list": ["a.com", "b.com"],
    }


def test_ftl_v60_shape_is_normalised() -> None:
    raw = {
        "ftl": {
            "database": {
                "gravity": 10,
                "domains": {"allowed": 3, "denied": 4},
                "regex": {"allowed": 1, "denied": {"total": 2, "enabled": 1}},
            },
            "uptime": 48996.057471,
        }
    }
    ftl = source("ftl").transform(raw)["ftl"]
    assert ftl["database"]["domains"] == {"allowed": {"total": 3}, "denied": {"total": 4}}
    assert ftl["database"]["regex"]["denied"] == {"total": 2, "enabled": 1}
    assert ftl["uptime"] == pytest.approx(48.996057471)


def test_ftl_tolerates_missing_parts() -> None:
    assert source("ftl").transform({"ftl": {"pid": 1}}) == {"ftl": {"pid": 1}}
    weird = source("ftl").transform({"ftl": {"database": {"domains": "x"}, "uptime": "?"}})
    assert weird["ftl"] == {"database": {"domains": "x"}, "uptime": "?"}


def test_client_and_upstream_labels() -> None:
    clients = source("top_clients").transform(
        {
            "clients": [
                {"name": "nas.lan", "ip": "10.0.0.2", "count": 3},
                {"name": "", "ip": "10.0.0.9", "count": 1},
                {"name": None, "ip": "fd00::9", "count": 1},
            ]
        }
    )["top_clients"]
    assert [c["label"] for c in clients] == ["nas.lan", "10.0.0.9", "fd00::9"]
    upstreams = source("upstreams").transform(
        {
            "upstreams": [
                {"ip": "blocklist", "name": "blocklist", "port": -1, "count": 9},
                {"ip": "cache", "name": "cache", "port": -1, "count": 8},
                {"ip": "10.0.0.1", "name": "router", "port": -1, "count": 7},
                {"ip": "8.8.8.8", "name": "", "port": 53, "count": 2},
                {"ip": "9.9.9.9", "name": "dns9.quad9.net", "port": 853, "count": 3},
                {"ip": "127.0.0.1", "name": "localhost", "port": True, "count": 3},
            ]
        }
    )["upstreams"]
    assert [u["label"] for u in upstreams] == [
        "router",
        "8.8.8.8#53",
        "dns9.quad9.net#853",
        "localhost",
    ]


@pytest.mark.parametrize(
    ("routes", "expected"),
    [
        ([], {"gateway": None, "interface": None, "address": None, "addresses": []}),
        (
            [
                {
                    "family": "inet",
                    "interface": "eth0",
                    "address": "192.168.1.1",
                    "local": ["192.168.1.2"],
                }
            ],
            {
                "gateway": "192.168.1.1",
                "interface": "eth0",
                "address": "192.168.1.2",
                "addresses": ["192.168.1.2"],
            },
        ),
        (
            [
                {
                    "family": "inet6",
                    "interface": "eth0",
                    "address": "fe80::1",
                    "local": ["fd00::2"],
                },
                {
                    "family": "inet",
                    "interface": "eth0",
                    "address": "10.0.0.1",
                    "local": ["10.0.0.2", "10.0.0.3"],
                },
            ],
            {
                "gateway": "10.0.0.1",
                "interface": "eth0",
                "address": "10.0.0.2",
                "addresses": ["10.0.0.2", "10.0.0.3", "fd00::2"],
            },
        ),
        (
            [{"family": "inet6", "interface": "wg0", "address": None, "local": ["fd00::2"]}],
            {"gateway": None, "interface": "wg0", "address": "fd00::2", "addresses": ["fd00::2"]},
        ),
        (
            [
                {"family": "inet", "interface": None, "address": "10.0.0.1", "local": []},
                {
                    "family": "inet",
                    "interface": "eth1",
                    "address": "10.0.0.1",
                    "local": ["10.0.0.5", 7],
                },
                "garbage",
            ],
            {
                "gateway": "10.0.0.1",
                "interface": None,
                "address": "10.0.0.5",
                "addresses": ["10.0.0.5"],
            },
        ),
    ],
)
def test_network(routes: list[Any], expected: dict[str, Any]) -> None:
    assert source("network").transform({"gateway": routes, "took": 0}) == {"network": expected}


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("summary", {"queries": [], "clients": {}, "gravity": {}}),
        ("summary", "text"),
        ("recent_blocked", {}),
        ("recent_blocked", {"blocked": "x"}),
        ("version", {"version": []}),
        ("top_domains", {"domains": [1]}),
        ("history", {"history": None}),
        ("network", {"gateway": {}}),
        ("messages", {}),
    ],
)
def test_transforms_reject_malformed_payloads(name: str, payload: Any) -> None:
    with pytest.raises((TypeError, KeyError)):
        source(name).transform(payload)


def test_took_is_removed_and_payloads_not_mutated() -> None:
    raw = {"size": 1, "took": 0.5, "owner": {"user": {"uid": 1}}}
    variables = from_responses({"info/database": raw})
    assert variables["database"] == {"size": 1, "owner": {"user": {"uid": 1}}}
    assert raw["took"] == 0.5
    summary: dict[str, Any] = {
        "queries": {"percent_blocked": 0},
        "clients": {},
        "gravity": {},
        "took": 1,
    }
    out = from_responses({"stats/summary": summary})
    assert out["queries"]["percent_blocked"] == 0.0
    assert isinstance(out["queries"]["percent_blocked"], float)
    assert summary["queries"]["percent_blocked"] == 0
    assert isinstance(summary["queries"]["percent_blocked"], int)


def test_float_coercion_in_lists_and_nested_lists() -> None:
    variables = from_responses(
        {
            "info/system": {"system": {"cpu": {"load": {"raw": [1, 0.5, True]}}, "uptime": 5}},
            "info/sensors": {"sensors": {"list": [{"temps": [{"value": 43, "sensor": "t"}]}]}},
        }
    )
    assert variables["system"]["cpu"]["load"]["raw"] == [1.0, 0.5, True]
    assert isinstance(variables["system"]["cpu"]["load"]["raw"][0], float)
    assert variables["system"]["uptime"] == 5
    assert isinstance(variables["system"]["uptime"], int)
    assert isinstance(variables["sensors"]["list"][0]["temps"][0]["value"], float)


# --------------------------------------------------------------------------- #
# Derived values and builtins                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("age", "relative"),
    [
        (0, {"days": 0, "hours": 0, "minutes": 0}),
        (59, {"days": 0, "hours": 0, "minutes": 0}),
        (3 * 86400 + 4 * 3600 + 12 * 60 + 31, {"days": 3, "hours": 4, "minutes": 12}),
        (-100, {"days": 0, "hours": 0, "minutes": 0}),
    ],
)
def test_gravity_relative(age: int, relative: dict[str, int]) -> None:
    now = 1_800_000_000
    summary = {"queries": {}, "clients": {}, "gravity": {"last_update": now - age}}
    variables = from_responses({"stats/summary": summary}, now=now)
    assert variables["gravity"]["relative"] == relative


@pytest.mark.parametrize("last_update", [0, None, "x", True])
def test_gravity_relative_missing_when_unknown(last_update: Any) -> None:
    summary = {"queries": {}, "clients": {}, "gravity": {"last_update": last_update}}
    variables = from_responses({"stats/summary": summary}, now=100)
    assert "relative" not in variables["gravity"]


def test_builtins() -> None:
    assert builtins(url="https://pi.hole:8443/pihole", now=5) == {
        "now": 5.0,
        "pihello": {"version": __version__},
        "pihole": {"url": "https://pi.hole:8443/pihole", "host": "pi.hole"},
    }
    assert builtins(url=None)["pihole"] == {"url": None, "host": None}
    assert builtins(url="http://[::1]:80")["pihole"]["host"] == "::1"
    assert builtins(url="http://[bad")["pihole"]["host"] is None
    import time

    assert abs(builtins(url=None)["now"] - time.time()) < 5


def test_from_responses_defaults() -> None:
    variables = from_responses({})
    assert set(variables) == BUILTIN_NAMESPACES
    assert variables["pihole"]["url"] is None


# --------------------------------------------------------------------------- #
# flatten                                                                      #
# --------------------------------------------------------------------------- #
def test_flatten() -> None:
    variables = {
        "queries": {"total": 1, "types": {"A": 2}},
        "top_domains": [{"domain": "a", "count": 1}, {"domain": "b", "count": 2}],
        "recent_blocked_list": ["x", "y"],
        "empty_list": [],
        "empty_dict": {},
        "nested": [[1, 2], [3]],
        "blocking": "enabled",
        "timer": None,
    }
    assert flatten(variables) == {
        "queries.total": 1,
        "queries.types.A": 2,
        "top_domains.0.domain": "a",
        "top_domains.0.count": 1,
        "top_domains.1.domain": "b",
        "top_domains.1.count": 2,
        "recent_blocked_list": ["x", "y"],
        "empty_list": [],
        "empty_dict": {},
        "nested.0": [1, 2],
        "nested.1": [3],
        "blocking": "enabled",
        "timer": None,
    }


# --------------------------------------------------------------------------- #
# Catalog                                                                      #
# --------------------------------------------------------------------------- #
def test_catalog_entries_are_well_formed() -> None:
    entries = catalog()
    assert catalog() is entries  # cached
    paths = [info.path for info in entries]
    assert len(paths) == len(set(paths))
    source_names = {s.name for s in SOURCES} | {"derived", "builtin"}
    for info in entries:
        assert info.type in VALID_TYPES, info
        assert info.source in source_names, info
        assert info.description, info
        assert "{" not in info.path, info
        assert "{}" not in info.description, info
        assert not info.description.endswith("."), info
        assert info.path.split(".", 1)[0] in NAMESPACES, info


def test_catalog_covers_every_namespace() -> None:
    covered = {info.path.split(".", 1)[0] for info in catalog()}
    assert covered == NAMESPACES


def test_catalog_sources_match_namespaces() -> None:
    by_namespace = {ns: s.name for s in SOURCES for ns in s.namespaces}
    for info in catalog():
        namespace = info.path.split(".", 1)[0]
        if info.source == "builtin":
            assert namespace in BUILTIN_NAMESPACES
        elif info.source != "derived":
            assert by_namespace[namespace] == info.source, info


def test_catalog_list_items_have_container_entries() -> None:
    paths = catalog_paths()
    types = {info.path: info.type for info in catalog()}
    for path in paths:
        segments = path.split(".")
        for index, segment in enumerate(segments):
            if segment == "N":
                container = ".".join(segments[:index])
                assert types.get(container) == "list", f"{container} (list) missing for {path}"


def test_catalog_documents_units() -> None:
    by_path = {info.path: info for info in catalog()}
    assert "KiB" in by_path["system.memory.ram.total"].description
    assert "seconds" in by_path["system.uptime"].description
    assert "seconds" in by_path["ftl.uptime"].description
    assert "sensors.unit" in by_path["sensors.cpu_temp"].description
    assert "bytes" in by_path["database.size"].description
    assert "percent" in by_path["queries.percent_blocked"].description
    assert "seconds" in by_path["upstreams.N.statistics.response"].description
    assert by_path["gravity.relative.days"].source == "derived"
    assert by_path["version.docker.update"].source == "derived"
    assert by_path["version.core.local.version"].description.startswith("Installed Pi-hole Core")
    assert by_path["now"].source == "builtin"
