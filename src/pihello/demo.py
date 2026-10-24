"""Realistic, reproducible offline demo data.

:func:`variables` returns a complete variable tree for a typical home network
(a Raspberry Pi 5 serving a dozen devices) shaped exactly like
:func:`pihello.sources.collect` output for the latest Pi-hole release. It is
built from raw API payloads (:func:`api_payloads`) that go through the same
transforms as live data, so derived values and builtins are always consistent.

Everything is deterministic and relative to :data:`NOW`, so ``ago`` and
``duration`` produce stable, pleasant values in screenshots and tests
(render with ``RenderOptions(now=demo.NOW)``).
"""

from __future__ import annotations

from typing import Any

from pihello import sources

__all__ = ["NOW", "URL", "api_payloads", "variables"]

NOW: float = 1792830600.0
"""Fixed demo timestamp: Saturday 2026-10-24 08:30:00 UTC."""

URL = "http://pi.hole"
"""The Pi-hole URL reported by the demo builtins."""

_DAY = 86400
_BUCKET = 600
_GRAVITY_AGE = 3 * _DAY + 4 * 3600 + 12 * 60 + 31  # "3d 4h ago"
_SYSTEM_UPTIME = 18 * _DAY + 6 * 3600 + 1234  # "18d 6h"
_FTL_UPTIME = 6 * _DAY + 2 * 3600 + 731  # FTL was restarted by its last update

# Relative query activity per hour of day (00:00 … 23:00): quiet nights,
# a morning ramp and an evening peak. Interpolated linearly between hours.
_ACTIVITY = (
    0.55, 0.36, 0.26, 0.21, 0.20, 0.24, 0.42, 0.78, 1.00, 1.04, 1.05, 1.10,
    1.16, 1.10, 1.05, 1.06, 1.12, 1.22, 1.36, 1.46, 1.52, 1.40, 1.15, 0.84,
)  # fmt: skip

_TOP_DOMAINS = (
    ("connectivitycheck.gstatic.com", 2814),
    ("www.google.com", 2203),
    ("time.apple.com", 1687),
    ("i.ytimg.com", 1452),
    ("apresolve.spotify.com", 1205),
    ("www.youtube.com", 1110),
    ("mesu.apple.com", 961),
    ("gateway.icloud.com", 902),
    ("github.com", 788),
    ("ntp.ubuntu.com", 640),
)
_TOP_BLOCKED = (
    ("pagead2.googlesyndication.com", 812),
    ("googleads.g.doubleclick.net", 655),
    ("app-measurement.com", 591),
    ("mobile.events.data.microsoft.com", 504),
    ("stats.g.doubleclick.net", 431),
    ("device-metrics-us.amazon.com", 388),
    ("incoming.telemetry.mozilla.org", 302),
    ("graph.facebook.com", 276),
    ("settings-win.data.microsoft.com", 233),
    ("log-config.samsungacr.com", 198),
)
_RECENT_BLOCKED = (
    "googleads.g.doubleclick.net",
    "app-measurement.com",
    "pagead2.googlesyndication.com",
    "device-metrics-us.amazon.com",
    "mobile.events.data.microsoft.com",
    "stats.g.doubleclick.net",
    "log-config.samsungacr.com",
    "incoming.telemetry.mozilla.org",
    "graph.facebook.com",
    "ads.roku.com",
)
# (name, ip, queries, blocked queries); a name of "" means unknown (label = ip)
_CLIENTS = (
    ("macbook-pro.lan", "192.168.1.23", 9812, 902),
    ("pixel-8.lan", "192.168.1.41", 7420, 1311),
    ("living-room-tv.lan", "192.168.1.60", 6107, 1640),
    ("ipad.lan", "192.168.1.35", 4880, 744),
    ("desktop.lan", "192.168.1.20", 4310, 520),
    ("work-laptop.lan", "192.168.1.27", 3655, 401),
    ("nas.lan", "192.168.1.10", 2970, 140),
    ("echo-dot.lan", "192.168.1.72", 2205, 1050),
    ("", "192.168.1.88", 1603, 233),
    ("printer.lan", "192.168.1.50", 702, 61),
)
_RAM_TOTAL = 8245488  # KiB: Raspberry Pi 5, 8 GB
_RAM_USED = 1187232
_RAM_FREE = 5106432
_RAM_AVAILABLE = 6792480
_SWAP_TOTAL = 524284


def _noise(count: int, seed: int = 20261024) -> list[float]:
    """Deterministic pseudo-random values in [-1, 1) (a tiny LCG, no imports)."""
    values = []
    state = seed
    for _ in range(count):
        state = (state * 1103515245 + 12345) % 2**31
        values.append(state / 2**30 - 1.0)
    return values


def _activity(timestamp: float) -> float:
    hours = (timestamp % _DAY) / 3600
    low = int(hours) % 24
    high = (low + 1) % 24
    fraction = hours - int(hours)
    return _ACTIVITY[low] * (1 - fraction) + _ACTIVITY[high] * fraction


def _history() -> list[dict[str, int]]:
    """144 ten-minute buckets covering the 24 hours before :data:`NOW`."""
    start = int(NOW) - _DAY
    noise = _noise(3 * 144)
    history = []
    for index in range(144):
        timestamp = start + index * _BUCKET + _BUCKET // 2
        activity = _activity(timestamp)
        total = round(373 * activity * (1 + 0.08 * noise[index]))
        # IoT telemetry keeps going at night, so the blocked share rises then.
        blocked_share = 0.142 + 0.07 * max(0.0, 1.0 - activity) + 0.015 * noise[144 + index]
        blocked = round(total * blocked_share)
        cached = round(total * (0.33 + 0.03 * noise[288 + index]))
        history.append(
            {
                "timestamp": timestamp,
                "total": total,
                "cached": cached,
                "blocked": blocked,
                "forwarded": total - blocked - cached,
            }
        )
    return history


def _split(total: int, shares: dict[str, float], remainder_to: str) -> dict[str, int]:
    """Distribute ``total`` by ``shares``; rounding leftovers go to ``remainder_to``."""
    parts = {key: int(total * share) for key, share in shares.items()}
    parts[remainder_to] += total - sum(parts.values())
    return parts


def _summary(history: list[dict[str, int]]) -> dict[str, Any]:
    total = sum(bucket["total"] for bucket in history)
    blocked = sum(bucket["blocked"] for bucket in history)
    cached = sum(bucket["cached"] for bucket in history)
    forwarded = sum(bucket["forwarded"] for bucket in history)
    types = _split(
        total,
        {
            "A": 0.575, "AAAA": 0.243, "ANY": 0.0, "SRV": 0.014, "SOA": 0.003, "PTR": 0.041,
            "TXT": 0.01, "NAPTR": 0.0001, "MX": 0.0005, "DS": 0.001, "RRSIG": 0.0,
            "DNSKEY": 0.001, "NS": 0.002, "SVCB": 0.006, "HTTPS": 0.091, "OTHER": 0.0004,
        },
        "A",
    )  # fmt: skip
    blocked_status = _split(
        blocked,
        {"GRAVITY": 0.876, "REGEX": 0.031, "DENYLIST": 0.009, "GRAVITY_CNAME": 0.064,
         "REGEX_CNAME": 0.001, "DENYLIST_CNAME": 0.0, "SPECIAL_DOMAIN": 0.019},
        "GRAVITY",
    )  # fmt: skip
    cached_status = _split(cached, {"CACHE": 0.972, "CACHE_STALE": 0.028}, "CACHE")
    forwarded_status = _split(
        forwarded, {"FORWARDED": 0.984, "RETRIED": 0.014, "RETRIED_DNSSEC": 0.002}, "FORWARDED"
    )
    status = {
        "UNKNOWN": 0,
        "GRAVITY": blocked_status["GRAVITY"],
        "FORWARDED": forwarded_status["FORWARDED"],
        "CACHE": cached_status["CACHE"],
        "REGEX": blocked_status["REGEX"],
        "DENYLIST": blocked_status["DENYLIST"],
        "EXTERNAL_BLOCKED_IP": 0,
        "EXTERNAL_BLOCKED_NULL": 0,
        "EXTERNAL_BLOCKED_NXRA": 0,
        "GRAVITY_CNAME": blocked_status["GRAVITY_CNAME"],
        "REGEX_CNAME": blocked_status["REGEX_CNAME"],
        "DENYLIST_CNAME": blocked_status["DENYLIST_CNAME"],
        "RETRIED": forwarded_status["RETRIED"],
        "RETRIED_DNSSEC": forwarded_status["RETRIED_DNSSEC"],
        "IN_PROGRESS": 0,
        "DBBUSY": 0,
        "SPECIAL_DOMAIN": blocked_status["SPECIAL_DOMAIN"],
        "CACHE_STALE": cached_status["CACHE_STALE"],
        "EXTERNAL_BLOCKED_EDE15": 0,
    }
    replies = _split(
        total,
        {
            "UNKNOWN": 0.0, "NODATA": 0.104, "NXDOMAIN": 0.047, "CNAME": 0.093, "IP": 0.722,
            "DOMAIN": 0.031, "RRNAME": 0.0, "SERVFAIL": 0.002, "REFUSED": 0.0, "NOTIMP": 0.0,
            "OTHER": 0.0, "DNSSEC": 0.0, "NONE": 0.0006, "BLOB": 0.0002,
        },
        "IP",
    )  # fmt: skip
    return {
        "queries": {
            "total": total,
            "blocked": blocked,
            "percent_blocked": blocked / total * 100,
            "unique_domains": 6843,
            "forwarded": forwarded,
            "cached": cached,
            "frequency": history[-1]["total"] / _BUCKET,
            "types": types,
            "status": status,
            "replies": replies,
        },
        "clients": {"active": 12, "total": 14},
        "gravity": {"domains_being_blocked": 121837, "last_update": int(NOW) - _GRAVITY_AGE},
        "took": 0.000021,
    }


def _clients(blocked: bool) -> dict[str, Any]:
    index = 3 if blocked else 2
    rows = sorted(_CLIENTS, key=lambda row: row[index], reverse=True)
    return {
        "clients": [{"name": row[0], "ip": row[1], "count": row[index]} for row in rows],
        "took": 0.000016,
    }


def _upstreams(summary: dict[str, Any]) -> dict[str, Any]:
    queries = summary["queries"]
    primary = round(queries["forwarded"] * 0.61)
    return {
        "upstreams": [
            {"ip": "blocklist", "name": "blocklist", "port": -1, "count": queries["blocked"],
             "statistics": {"response": 0, "variance": 0}},
            {"ip": "cache", "name": "cache", "port": -1, "count": queries["cached"],
             "statistics": {"response": 0, "variance": 0}},
            {"ip": "9.9.9.9", "name": "dns9.quad9.net", "port": 53, "count": primary,
             "statistics": {"response": 0.018342, "variance": 0.012107}},
            {"ip": "149.112.112.112", "name": "dns.quad9.net", "port": 53,
             "count": queries["forwarded"] - primary,
             "statistics": {"response": 0.021877, "variance": 0.015402}},
        ],
        "total_queries": queries["total"],
        "forwarded_queries": queries["forwarded"],
        "took": 0.000018,
    }  # fmt: skip


def _ftl(summary: dict[str, Any]) -> dict[str, Any]:
    queries = summary["queries"]
    return {
        "ftl": {
            "database": {
                "gravity": summary["gravity"]["domains_being_blocked"],
                "antigravity": 0,
                "groups": 3,
                "lists": 5,
                "clients": 4,
                "domains": {
                    "allowed": {"total": 6, "enabled": 6},
                    "denied": {"total": 3, "enabled": 2},
                },
                "regex": {
                    "allowed": {"total": 1, "enabled": 1},
                    "denied": {"total": 4, "enabled": 4},
                },
            },
            "privacy_level": 0,
            "query_frequency": queries["frequency"],
            "clients": dict(summary["clients"]),
            "pid": 1123,
            "uptime": _FTL_UPTIME * 1000 + 412.733,
            "%mem": 0.6618,
            "%cpu": 0.4,
            "allow_destructive": True,
            "dnsmasq": {
                "dns_cache_inserted": 38211,
                "dns_cache_live_freed": 0,
                "dns_queries_forwarded": queries["forwarded"],
                "dns_auth_answered": 0,
                "dns_local_answered": queries["cached"],
                "dns_stale_answered": queries["status"]["CACHE_STALE"],
                "dns_unanswered": 12,
                "dnssec_max_crypto_use": 0,
                "dnssec_max_sig_fail": 0,
                "dnssec_max_work": 0,
                "bootp": 0,
                "pxe": 0,
                "dhcp_ack": 0,
                "dhcp_decline": 0,
                "dhcp_discover": 0,
                "dhcp_inform": 0,
                "dhcp_nak": 0,
                "dhcp_offer": 0,
                "dhcp_release": 0,
                "dhcp_request": 0,
                "noanswer": 0,
                "leases_allocated_4": 0,
                "leases_pruned_4": 0,
                "leases_allocated_6": 0,
                "leases_pruned_6": 0,
                "tcp_connections": 3,
                "dhcp_leasequery": 0,
                "dhcp_lease_unassigned": 0,
                "dhcp_lease_actve": 0,
                "dhcp_lease_unknown": 0,
            },
        },
        "took": 0.000088,
    }


def api_payloads() -> dict[str, Any]:
    """Return raw API payloads, keyed by :attr:`pihello.sources.Source.endpoint`.

    These mimic what the latest Pi-hole returns (including ``took``); the test
    suite's fake Pi-hole can serve them as a fixture set.
    """
    now = int(NOW)
    history = _history()
    summary = _summary(history)
    load = [0.21, 0.16, 0.11]
    return {
        "stats/summary": summary,
        "dns/blocking": {"blocking": "enabled", "timer": None, "took": 0.000008},
        "stats/recent_blocked?count=10": {"blocked": list(_RECENT_BLOCKED), "took": 0.000013},
        "info/version": {
            "version": {
                "core": {
                    "local": {"version": "v6.5", "branch": "master", "hash": "f503a65d"},
                    "remote": {"version": "v6.5", "hash": "f503a65d"},
                },
                "web": {
                    "local": {"version": "v6.6.1", "branch": "master", "hash": "fb699c4e"},
                    "remote": {"version": "v6.6.1", "hash": "fb699c4e"},
                },
                "ftl": {
                    "local": {
                        "hash": "6d1e4a93",
                        "branch": "master",
                        "version": "v6.7",
                        "date": "2026-09-02 19:41:27 +0200",
                    },
                    "remote": {"version": "v6.8", "hash": "29c8b13f"},
                },
                "docker": {"local": None, "remote": None},
            },
            "took": 0.000043,
        },
        "info/system": {
            "system": {
                "uptime": _SYSTEM_UPTIME,
                "memory": {
                    "ram": {
                        "total": _RAM_TOTAL,
                        "free": _RAM_FREE,
                        "used": _RAM_USED,
                        "available": _RAM_AVAILABLE,
                        "%used": _RAM_USED / _RAM_TOTAL * 100,
                    },
                    "swap": {"total": _SWAP_TOTAL, "free": _SWAP_TOTAL, "used": 0, "%used": 0},
                },
                "procs": 213,
                "cpu": {
                    "nprocs": 4,
                    "%cpu": 2.9,
                    "load": {"raw": load, "percent": [value / 4 * 100 for value in load]},
                },
                "ftl": {"%mem": 0.6618, "%cpu": 0.4},
            },
            "took": 0.000225,
        },
        "info/host": {
            "host": {
                "uname": {
                    "domainname": "(none)",
                    "machine": "aarch64",
                    "nodename": "pihole",
                    "release": "6.12.47+rpt-rpi-2712",
                    "sysname": "Linux",
                    "version": "#1 SMP PREEMPT Debian 1:6.12.47-1+rpt1 (2025-09-16)",
                },
                "model": "Raspberry Pi 5 Model B Rev 1.0",
                "dmi": {
                    "bios": {"vendor": None},
                    "board": {"name": None, "vendor": None, "version": None},
                    "product": {"name": None, "family": None, "version": None},
                    "sys": {"vendor": None},
                },
            },
            "took": 0.000186,
        },
        "info/sensors": {
            "sensors": {
                "list": [
                    {
                        "name": "cpu_thermal",
                        "path": "hwmon0",
                        "source": "devices/virtual/thermal/thermal_zone0",
                        "temps": [
                            {
                                "name": None,
                                "value": 52.1,
                                "max": None,
                                "crit": 110,
                                "sensor": "temp1",
                            },
                        ],
                    },
                    {
                        "name": "rp1_adc",
                        "path": "hwmon1",
                        "source": "devices/platform/axi/1000120000.pcie/1f000c8000.adc",
                        "temps": [
                            {
                                "name": None,
                                "value": 55.9,
                                "max": None,
                                "crit": None,
                                "sensor": "temp1",
                            },
                        ],
                    },
                    {
                        "name": "nvme",
                        "path": "hwmon2",
                        "source": "devices/platform/axi/1000110000.pcie/pci0000:00/"
                        "0000:00:00.0/0000:01:00.0/nvme/nvme0",
                        "temps": [
                            {
                                "name": "Composite",
                                "value": 41.85,
                                "max": 81.85,
                                "crit": 84.85,
                                "sensor": "temp1",
                            },
                        ],
                    },
                ],
                "cpu_temp": 52.1,
                "hot_limit": 60,
                "unit": "C",
            },
            "took": 0.000412,
        },
        "info/ftl": _ftl(summary),
        "info/database": {
            "size": 47185920,
            "type": "Regular file",
            "mode": "rw-r-----",
            "atime": now - 12,
            "mtime": now - 12,
            "ctime": now - 12,
            "owner": {
                "user": {"uid": 999, "name": "pihole", "info": ""},
                "group": {"gid": 999, "name": "pihole"},
            },
            "queries": summary["queries"]["total"],
            "earliest_timestamp": now - _DAY + 4.172937,
            "queries_disk": 3461702,
            "earliest_timestamp_disk": now - 91 * _DAY + 27,
            "sqlite_version": "3.50.4",
            "took": 0.000071,
        },
        "info/messages/count": {"count": 0, "took": 0.00029},
        "stats/top_domains?count=10": {
            "domains": [{"domain": domain, "count": count} for domain, count in _TOP_DOMAINS],
            "total_queries": summary["queries"]["total"],
            "blocked_queries": summary["queries"]["blocked"],
            "took": 0.000019,
        },
        "stats/top_domains?blocked=true&count=10": {
            "domains": [{"domain": domain, "count": count} for domain, count in _TOP_BLOCKED],
            "total_queries": summary["queries"]["total"],
            "blocked_queries": summary["queries"]["blocked"],
            "took": 0.000018,
        },
        "stats/top_clients?count=10": _clients(blocked=False),
        "stats/top_clients?blocked=true&count=10": _clients(blocked=True),
        "stats/upstreams": _upstreams(summary),
        "history": {"history": history, "took": 0.000412},
        "network/gateway": {
            "gateway": [
                {
                    "family": "inet",
                    "interface": "eth0",
                    "address": "192.168.1.1",
                    "local": ["192.168.1.2"],
                },
                {
                    "family": "inet6",
                    "interface": "eth0",
                    "address": "fe80::1",
                    "local": ["fd12:3456:789a:1::2"],
                },
            ],
            "took": 0.000466,
        },
    }


def variables() -> dict[str, Any]:
    """Return the complete demo variable tree (a fresh copy on every call)."""
    return sources.from_responses(api_payloads(), url=URL, now=NOW)
