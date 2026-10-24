"""Sanity checks for the recorded Pi-hole API fixtures.

The fixtures under ``tests/fixtures/pihole/<docker-tag>/responses.json`` are
produced by ``scripts/record_fixtures.py`` from real Pi-hole containers and are
replayed by ``helpers.fakepihole``. These tests make sure every recording is
well-formed, complete enough for the rest of the test-suite, consistent with
``index.json`` and properly sanitised (nothing from the recording machine).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import ipaddress
import json
import re
import sys
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "pihole"
INDEX = FIXTURES / "index.json"
COMPAT_DOC = ROOT / "docs" / "compatibility.md"
RECORDER = ROOT / "scripts" / "record_fixtures.py"

TAGS = sorted(
    (p.parent.name for p in FIXTURES.glob("*/responses.json")),
    key=lambda t: tuple(int(x) for x in re.findall(r"\d+", t)),
)

FAKE_SID = "cGloZWxsby1mYWtlLXNpZA="
FAKE_CSRF = "cGloZWxsby1mYWtlY3NyZg="
RPI_MODEL = "Raspberry Pi 4 Model B Rev 1.5"

#: Every endpoint of the architecture doc's sources table, exactly as queried.
SOURCE_ENDPOINTS = (
    "GET /api/stats/summary",
    "GET /api/dns/blocking",
    "GET /api/stats/recent_blocked?count=10",
    "GET /api/info/version",
    "GET /api/info/system",
    "GET /api/info/host",
    "GET /api/info/sensors",
    "GET /api/info/ftl",
    "GET /api/info/database",
    "GET /api/info/messages/count",
    "GET /api/stats/top_domains?count=10",
    "GET /api/stats/top_domains?blocked=true&count=10",
    "GET /api/stats/top_clients?count=10",
    "GET /api/stats/top_clients?blocked=true&count=10",
    "GET /api/stats/upstreams",
    "GET /api/history",
    "GET /api/network/gateway",
)

#: Further keys the fake server and client tests rely on: key -> expected status.
REQUIRED_STATUS = {
    "GET /api/info/login": 200,
    "GET /api/auth": 200,
    "GET /api/auth/sessions": 200,
    "GET /api/padd": 200,
    "GET /api/info/metrics": 200,
    "GET /api/info/client": 200,
    "GET /api/stats/query_types": 200,
    "GET /api/network/interfaces": 200,
    "GET /api/network/devices": 200,
    "GET /api/network/routes": 200,
    "GET /api/config/webserver/api/max_sessions": 200,
    "GET /api/config/dns/upstreams": 200,
    "GET /api/dhcp/leases": 200,
    "GET /api/lists": 200,
    "GET /api/groups": 200,
    "GET /api/endpoints": 200,
    "GET /api/dns/blocking#disabled": 200,
    "POST /api/auth": 200,
    "POST /api/auth#wrong-password": 401,
    "POST /api/auth#app-password": 200,
    "POST /api/auth#seats-exceeded": 429,
    "POST /api/auth#login-rate-limited": 429,
    "POST /api/auth#totp": 200,
    "POST /api/auth#totp-invalid": 401,
    "POST /api/auth#no-password": 200,
    "GET /api/stats/summary#no-password": 200,
    "GET /api/stats/summary#unauthenticated": 401,
    "GET /api/stats/summary#invalid-sid": 401,
    "GET /api/does/not/exist": 404,
    "DELETE /api/auth": 204,
    "DELETE /api/auth#unauthenticated": 401,
}

KEY_RE = re.compile(
    r"^(GET|POST|PUT|PATCH|DELETE) /api/[A-Za-z0-9_/.-]+(\?[^#\s]+)?(#[a-z0-9.-]+)?$"
)
MAC_RE = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.]*\d)")
IPV6_RE = re.compile(
    r"(?<![0-9A-Za-z:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![0-9A-Za-z:])"
)
DOC_NETS4 = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
#: Non-public ranges that would reveal the recording machine's networks.
PRIVATE_NETS4 = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
]


@cache
def load(tag: str) -> dict[str, Any]:
    """Parsed ``responses.json`` for ``tag``."""
    data: dict[str, Any] = json.loads((FIXTURES / tag / "responses.json").read_text("utf-8"))
    return data


@cache
def raw_text(tag: str) -> str:
    """Raw text of ``responses.json`` for ``tag``."""
    return (FIXTURES / tag / "responses.json").read_text("utf-8")


def body(tag: str, key: str) -> Any:
    """Recorded body for ``key``."""
    return load(tag)["responses"][key]["body"]


def walk(value: Any, path: str = "") -> list[tuple[str, Any]]:
    """Flatten JSON into ``(path, leaf)`` pairs."""
    if isinstance(value, dict):
        return [item for k, v in value.items() for item in walk(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [item for i, v in enumerate(value) for item in walk(v, f"{path}[{i}]")]
    return [(path, value)]


pytestmark = pytest.mark.skipif(not TAGS, reason="no recorded fixtures")
by_tag = pytest.mark.parametrize("tag", TAGS)


# --------------------------------------------------------------------------- #
# index.json                                                                   #
# --------------------------------------------------------------------------- #
def test_index_lists_every_recording_and_latest() -> None:
    index = json.loads(INDEX.read_text("utf-8"))
    assert index.pop("latest") == TAGS[-1]
    assert sorted(index) == sorted(TAGS)


@by_tag
def test_index_matches_meta(tag: str) -> None:
    index = json.loads(INDEX.read_text("utf-8"))
    assert index[tag] == load(tag)["meta"]["versions"]
    assert set(index[tag]) == {"core", "web", "ftl", "docker"}


def test_compatibility_doc_mentions_every_recording() -> None:
    text = COMPAT_DOC.read_text("utf-8")
    missing = [tag for tag in TAGS if f"`{tag}`" not in text]
    assert not missing, f"docs/compatibility.md does not list {missing}"


# --------------------------------------------------------------------------- #
# Format                                                                       #
# --------------------------------------------------------------------------- #
@by_tag
def test_meta(tag: str) -> None:
    meta = load(tag)["meta"]
    assert meta["docker_tag"] == tag
    assert meta["recorder"] == "scripts/record_fixtures.py"
    assert meta["password"] == "pihello-test"
    recorded = dt.datetime.fromisoformat(meta["recorded_at"])
    assert recorded.tzinfo is not None
    versions = meta["versions"]
    assert versions["docker"] == tag
    assert str(versions["core"]).startswith("v6.")


@by_tag
def test_meta_versions_match_info_version(tag: str) -> None:
    version = body(tag, "GET /api/info/version")["version"]
    meta = load(tag)["meta"]["versions"]
    for component in ("core", "web", "ftl"):
        assert meta[component] == version[component]["local"]["version"]


@by_tag
def test_response_keys_and_entries(tag: str) -> None:
    responses = load(tag)["responses"]
    assert list(responses) == sorted(responses), "keys are written sorted"
    for key, entry in responses.items():
        assert KEY_RE.match(key), key
        route = key.split("#", 1)[0]
        if "?" in route:
            params = route.split("?", 1)[1].split("&")
            assert params == sorted(params), f"{key}: query parameters must be sorted"
        assert isinstance(entry["status"], int)
        assert 100 <= entry["status"] < 600
        assert "body" in entry
        if entry["status"] == 204:
            assert entry["body"] is None


@by_tag
def test_took_is_kept(tag: str) -> None:
    summary = body(tag, "GET /api/stats/summary")
    assert isinstance(summary["took"], float)


# --------------------------------------------------------------------------- #
# Completeness                                                                 #
# --------------------------------------------------------------------------- #
@by_tag
@pytest.mark.parametrize("key", SOURCE_ENDPOINTS)
def test_every_source_endpoint_recorded(tag: str, key: str) -> None:
    entry = load(tag)["responses"][key]
    assert entry["status"] == 200
    assert isinstance(entry["body"], dict)


@by_tag
@pytest.mark.parametrize(("key", "status"), sorted(REQUIRED_STATUS.items()))
def test_required_variant_recorded(tag: str, key: str, status: int) -> None:
    assert load(tag)["responses"][key]["status"] == status


@by_tag
def test_totp_required_is_rejected(tag: str) -> None:
    entry = load(tag)["responses"]["POST /api/auth#totp-required"]
    assert entry["status"] in (400, 401)


@by_tag
def test_traffic_made_stats_non_trivial(tag: str) -> None:
    queries = body(tag, "GET /api/stats/summary")["queries"]
    assert queries["total"] > 300
    assert 0 < queries["blocked"] < queries["total"]
    assert len(body(tag, "GET /api/stats/top_domains?count=10")["domains"]) == 10
    assert len(body(tag, "GET /api/stats/top_domains?blocked=true&count=10")["domains"]) == 10
    assert len(body(tag, "GET /api/stats/top_clients?count=10")["clients"]) >= 4
    assert body(tag, "GET /api/stats/recent_blocked?count=10")["blocked"]
    upstreams = body(tag, "GET /api/stats/upstreams")["upstreams"]
    assert any(u["port"] == 53 and u["count"] > 0 for u in upstreams)


@by_tag
def test_update_check_data(tag: str) -> None:
    """Remote versions are real when the update checker reached GitHub."""
    meta = load(tag)["meta"]
    version = body(tag, "GET /api/info/version")["version"]
    if not meta["survey"].get("update_check_got_remote_versions"):
        pytest.skip("recorded while GitHub's API was unreachable/rate limited")
    for component in ("core", "web", "ftl"):
        remote = version[component]["remote"]["version"]
        assert isinstance(remote, str)
        assert remote.startswith("v6.")
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", version["docker"]["remote"])


@by_tag
def test_auth_variants_shape(tag: str) -> None:
    assert body(tag, "GET /api/dns/blocking#disabled")["blocking"] == "disabled"
    assert body(tag, "POST /api/auth#no-password")["session"]["sid"] is None
    seats = body(tag, "POST /api/auth#seats-exceeded")["error"]
    assert seats["key"] == "api_seats_exceeded"
    limited = body(tag, "POST /api/auth#login-rate-limited")["error"]
    assert limited["key"] == "rate_limiting"
    wrong = body(tag, "POST /api/auth#wrong-password")["session"]
    assert wrong["valid"] is False
    assert wrong["sid"] is None


# --------------------------------------------------------------------------- #
# Sanitisation                                                                 #
# --------------------------------------------------------------------------- #
@by_tag
def test_session_tokens_are_fake(tag: str) -> None:
    for key, entry in load(tag)["responses"].items():
        for path, value in walk(entry["body"]):
            if path.endswith(".sid") and value is not None:
                assert value == FAKE_SID, f"{key}{path}"
            if path.endswith(".csrf") and value is not None:
                assert value == FAKE_CSRF, f"{key}{path}"


@by_tag
def test_host_identity_is_generic(tag: str) -> None:
    host = body(tag, "GET /api/info/host")["host"]
    assert host["model"] == RPI_MODEL
    assert host["uname"]["nodename"] == "pihole"
    assert host["uname"]["machine"] == "aarch64"
    assert "rpi" in host["uname"]["release"]
    for _path, value in walk(host.get("dmi", {})):
        assert value is None or "Raspberry Pi" in str(value) or re.fullmatch(r"[\d./ Rev]+", value)
    padd = body(tag, "GET /api/padd")
    assert padd["node_name"] == "pihole"
    assert padd["host_model"] == RPI_MODEL


@by_tag
def test_user_agents_and_headers(tag: str) -> None:
    for session in body(tag, "GET /api/auth/sessions")["sessions"]:
        assert session["user_agent"] == "pihello-recorder"
    headers = {h["name"].lower(): h["value"] for h in body(tag, "GET /api/info/client")["headers"]}
    assert headers["user-agent"] == "pihello-recorder"
    assert headers["host"] == "pi.hole"


@by_tag
def test_only_generic_interface_names(tag: str) -> None:
    names = {i["name"] for i in body(tag, "GET /api/network/interfaces")["interfaces"]}
    assert names <= {"lo", "eth0", "eth1", "wlan0"}


@by_tag
def test_no_private_or_real_ipv4(tag: str) -> None:
    for token in set(IPV4_RE.findall(raw_text(tag))):
        try:
            ip = ipaddress.IPv4Address(token)
        except ValueError:
            continue
        assert not any(ip in net for net in PRIVATE_NETS4), token
        assert (
            any(ip in net for net in DOC_NETS4)
            or ip.is_loopback
            or ip.is_global
            or (ip.is_unspecified or ip.is_multicast or token == "255.255.255.255")
        ), token


@by_tag
def test_no_real_ipv6(tag: str) -> None:
    doc = ipaddress.ip_network("2001:db8::/32")
    for token in set(IPV6_RE.findall(raw_text(tag))):
        try:
            ip = ipaddress.IPv6Address(token)
        except ValueError:
            continue
        fake_link_local = ip.is_link_local and str(ip).startswith("fe80::ff:fe00:")
        assert (
            ip in doc or ip.is_loopback or ip.is_unspecified or ip.is_multicast or (fake_link_local)
        ), token


@by_tag
def test_no_real_mac_addresses(tag: str) -> None:
    for token in set(MAC_RE.findall(raw_text(tag))):
        mac = token.lower()
        allowed = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}
        assert mac in allowed or mac.startswith("02:00:00:00:00:"), mac


@by_tag
def test_no_tailscale_or_lan_markers(tag: str) -> None:
    text = raw_text(tag).lower()
    for marker in ("ts.net", "tailscale", "fd7a:", "192.168.", "10.88.", "dns.podman"):
        assert marker not in text, marker
    # predictable host interface names (wlp2s0, enp3s0, eno1, ...)
    assert not re.search(r"\b(?:wl|en)(?:p\d+s\d+|o\d+)\b", text)


# --------------------------------------------------------------------------- #
# The recorder's sanitiser itself                                              #
# --------------------------------------------------------------------------- #
@cache
def recorder() -> ModuleType:
    """Import ``scripts/record_fixtures.py`` as a module."""
    spec = importlib.util.spec_from_file_location("_pihello_record_fixtures", RECORDER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


def test_sanitizer_maps_addresses_deterministically() -> None:
    rec = recorder()
    data = {
        "a": "192.168.7.42",
        "b": ["fd7a:115c:a1e0::1234:5678", "fe80::1c2b:3cff:fe4d:5e6f", "1e:2b:3c:4d:5e:6f"],
        "c": "42.7.168.192.in-addr.arpa",
        "keep": ["192.0.2.2", "127.0.0.1", "8.8.8.8", "2001:db8:53::2", "fe80::ff:fe00:2", "::1"],
        "sid": "realsessionid",
        "time": "2026-10-17 13:08:09 +0200",
    }
    out = rec.sanitize_responses({"GET /api/x": {"body": data}}, exact={}, words={}, recorded_at=0)
    clean = out["GET /api/x"]["body"]
    assert clean["a"] == "198.51.100.10"
    assert clean["b"] == ["2001:db8:ffff::10", "fe80::ff:fe00:a0", "02:00:00:00:00:a0"]
    assert clean["c"] == "10.100.51.198.in-addr.arpa"
    assert clean["keep"] == data["keep"]
    assert clean["sid"] == FAKE_SID
    assert clean["time"] == data["time"]


def test_response_key_sorts_query() -> None:
    rec = recorder()
    assert rec.response_key("GET", "stats/top_domains?count=10&blocked=true") == (
        "GET /api/stats/top_domains?blocked=true&count=10"
    )
    assert rec.response_key("post", "/api/auth", "totp") == "POST /api/auth#totp"


def test_find_leaks_reports_host_data() -> None:
    rec = recorder()
    leaks = rec.find_leaks(
        '{"ip": "10.1.2.3", "board": "ExampleBoard X1"}', ["ExampleBoard X1"], []
    )
    assert any("ExampleBoard X1" in leak for leak in leaks)
    assert any("10.1.2.3" in leak for leak in leaks)
    assert rec.find_leaks('{"ip": "192.0.2.2", "mac": "02:00:00:00:00:02"}', [], []) == []
