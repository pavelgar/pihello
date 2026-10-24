#!/usr/bin/env python3
"""Record real Pi-hole v6 API responses as test fixtures.

For every requested Docker tag this script

1. starts a Pi-hole container (podman, else docker) on a private network that
   uses the documentation ranges ``192.0.2.0/24`` / ``2001:db8:53::/64``, plus a
   second, password-less container,
2. waits for the API and gravity, seeds deny/allow lists, then generates
   realistic DNS traffic from the host (published port) and from short-lived
   "LAN client" containers with fixed addresses,
3. records every endpoint listed in ``docs/architecture.md`` and many more,
   plus auth/error variants (wrong password, TOTP, app password, seat limit,
   unauthenticated, 404, ...),
4. sanitises the recordings (see ``docs/compatibility.md``), verifies that
   nothing from the recording machine leaked, and writes
   ``tests/fixtures/pihole/<tag>/responses.json`` and ``index.json``,
5. tears everything down and logs out every API session it created.

Usage::

    uv run python scripts/record_fixtures.py --tag 2026.10.0 [--tag ...]
    uv run python scripts/record_fixtures.py --all-v6        # every v6 Docker tag
    uv run python scripts/record_fixtures.py --report        # print compatibility tables

Requires network access (gravity download, upstream DNS through the
container runtime's resolver).
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import datetime as dt
import getpass
import ipaddress
import itertools
import json
import os
import random
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

from helpers import dnsgen  # noqa: E402
from helpers.containers import (  # noqa: E402
    ApiResponse,
    ContainerError,
    PiholeContainer,
    Runtime,
    detect_runtime,
    totp_code,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

FIXTURES = REPO / "tests" / "fixtures" / "pihole"
PASSWORD = "pihello-test"  # noqa: S105 - throw-away test container password
FIRST_V6_TAG = "2025.02.0"
RECORDER = "scripts/record_fixtures.py"

# --------------------------------------------------------------------------- #
# Lab network: everything uses documentation address ranges                  #
# --------------------------------------------------------------------------- #
NETWORK = "pihello-survey-net"
SUBNET4, GATEWAY4 = "192.0.2.0/24", "192.0.2.1"
SUBNET6, GATEWAY6 = "2001:db8:53::/64", "2001:db8:53::1"
PIHOLE_IP4, PIHOLE_IP6, PIHOLE_MAC = "192.0.2.2", "2001:db8:53::2", "02:00:00:00:00:02"
NOPW_IP4, NOPW_IP6, NOPW_MAC = "192.0.2.3", "2001:db8:53::3", "02:00:00:00:00:03"


@dataclass(frozen=True)
class LanClient:
    """A short-lived container that sends DNS queries from a fixed address."""

    name: str
    ip4: str
    ip6: str
    mac: str
    use_ipv6: bool
    queries: int
    ad_ratio: float


LAN_CLIENTS = (
    LanClient("laptop", "192.0.2.21", "2001:db8:53::21", "02:00:00:00:00:21", False, 170, 0.22),
    LanClient("phone", "192.0.2.22", "2001:db8:53::22", "02:00:00:00:00:22", False, 130, 0.38),
    LanClient("tv", "192.0.2.23", "2001:db8:53::23", "02:00:00:00:00:23", False, 70, 0.55),
    LanClient("desktop", "192.0.2.24", "2001:db8:53::24", "02:00:00:00:00:24", True, 90, 0.18),
)

LOCAL_HOSTS = [f"{GATEWAY4} router.lan"] + [
    entry for c in LAN_CLIENTS for entry in (f"{c.ip4} {c.name}.lan", f"{c.ip6} {c.name}.lan")
]

# --------------------------------------------------------------------------- #
# Traffic                                                                      #
# --------------------------------------------------------------------------- #
ALLOWED: dict[str, int] = {
    "google.com": 40,
    "www.google.com": 26,
    "youtube.com": 20,
    "www.youtube.com": 14,
    "i.ytimg.com": 10,
    "github.com": 18,
    "api.github.com": 11,
    "raw.githubusercontent.com": 6,
    "wikipedia.org": 7,
    "en.wikipedia.org": 9,
    "cloudflare.com": 6,
    "amazon.com": 6,
    "www.amazon.com": 5,
    "netflix.com": 6,
    "www.netflix.com": 4,
    "reddit.com": 7,
    "www.reddit.com": 6,
    "apple.com": 5,
    "icloud.com": 4,
    "microsoft.com": 5,
    "login.microsoftonline.com": 4,
    "outlook.office365.com": 4,
    "ubuntu.com": 3,
    "debian.org": 3,
    "python.org": 3,
    "pypi.org": 4,
    "files.pythonhosted.org": 3,
    "stackoverflow.com": 5,
    "news.ycombinator.com": 4,
    "bbc.co.uk": 3,
    "spotify.com": 3,
    "discord.com": 4,
    "zoom.us": 2,
    "raspberrypi.com": 3,
    "pi-hole.net": 6,
    "docs.pi-hole.net": 3,
    "discourse.pi-hole.net": 2,
    "pool.ntp.org": 3,
    "duckduckgo.com": 4,
    "mozilla.org": 3,
    "firefox.settings.services.mozilla.com": 3,
    "whatsapp.net": 3,
}
GRAVITY_CANDIDATES = (
    "doubleclick.net",
    "ad.doubleclick.net",
    "googleads.g.doubleclick.net",
    "stats.g.doubleclick.net",
    "pagead2.googlesyndication.com",
    "adservice.google.com",
    "www.googletagservices.com",
    "googleadservices.com",
    "www.googleadservices.com",
    "ads.yahoo.com",
    "analytics.yahoo.com",
    "app-measurement.com",
    "ads.linkedin.com",
    "ads.tiktok.com",
    "analytics.tiktok.com",
    "adnxs.com",
    "ib.adnxs.com",
    "scorecardresearch.com",
    "sb.scorecardresearch.com",
    "taboola.com",
    "cdn.taboola.com",
    "outbrain.com",
    "widgets.outbrain.com",
    "criteo.com",
    "static.criteo.net",
    "hotjar.com",
    "static.hotjar.com",
    "mixpanel.com",
    "api.mixpanel.com",
    "track.hubspot.com",
    "adcolony.com",
    "data.flurry.com",
    "ads.pubmatic.com",
    "secure.adnxs.com",
    "an.facebook.com",
    "pixel.facebook.com",
    "ads.twitter.com",
    "adserver.yahoo.com",
)
DENY_EXACT = ("ads.example.com", "tracker.example.net", "telemetry.example.org")
DENY_REGEX = r"(^|\.)metrics\.example\.(com|net)$"
REGEX_QUERIES = ("metrics.example.com", "eu.metrics.example.net", "us.metrics.example.com")
ALLOW_EXACT = ("www.googleadservices.com",)
NXDOMAINS = ("pihello-does-not-exist.com", "intranet.pihello-test.invalid", "wpad.lan")
SPECIAL_QUERIES: tuple[tuple[str, str], ...] = (
    ("gmail.com", "MX"),
    ("google.com", "TXT"),
    ("pi-hole.net", "NS"),
    ("example.com", "SOA"),
    ("_xmpp-client._tcp.jabber.org", "SRV"),
    ("_ldap._tcp.example.com", "SRV"),
    ("1.1.1.1.in-addr.arpa", "PTR"),
    ("1.2.0.192.in-addr.arpa", "PTR"),
    ("21.2.0.192.in-addr.arpa", "PTR"),
    ("www.github.com", "CNAME"),
    ("letsencrypt.org", "CAA"),
    ("cloudflare.com", "DNSKEY"),
    ("cloudflare.com", "DS"),
    ("sip2sip.info", "NAPTR"),
    ("cloudflare.com", "SVCB"),
    ("example.com", "ANY"),
    ("router.lan", "A"),
    ("pi.hole", "A"),
    ("laptop.lan", "AAAA"),
)

# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #
#: Authenticated GET endpoints recorded verbatim (path below /api, query sorted).
GET_ENDPOINTS = (
    "auth",
    "auth/sessions",
    "info/version",
    "info/system",
    "info/host",
    "info/sensors",
    "info/ftl",
    "info/database",
    "info/messages/count",
    "info/messages",
    "info/metrics",
    "info/client",
    "stats/summary",
    "stats/upstreams",
    "stats/query_types",
    "stats/recent_blocked?count=10",
    "stats/top_domains?count=10",
    "stats/top_domains?blocked=true&count=10",
    "stats/top_clients?count=10",
    "stats/top_clients?blocked=true&count=10",
    "stats/database/summary?from=1&until=4102444800",
    "history",
    "history/clients",
    "dns/blocking",
    "network/gateway",
    "network/interfaces",
    "network/devices",
    "network/routes",
    "config/webserver/api/max_sessions",
    "config/dns/upstreams",
    "dhcp/leases",
    "lists",
    "groups",
    "domains",
    "clients",
    "padd",
    "endpoints",
)

# --------------------------------------------------------------------------- #
# Sanitisation constants                                                       #
# --------------------------------------------------------------------------- #
FAKE_SID = "cGloZWxsby1mYWtlLXNpZA="  # 23 chars like FTL's sids
FAKE_CSRF = "cGloZWxsby1mYWtlY3NyZg="
FAKE_APP_PASSWORD = base64.b64encode(b"pihello-fake-app-password-000000").decode()
FAKE_APP_HASH = (
    "$BALLOON-SHA256$v=1$s=1024,t=32$"
    + base64.b64encode(b"pihello-fakesalt").decode()
    + "$"
    + base64.b64encode(b"pihello-fake-app-password-hash!!").decode()
)
FAKE_TOTP_SECRET = base64.b32encode(b"pihello-fake-totp!!!").decode()
USER_AGENT = "pihello-recorder"
SECRET_KEYS = {
    "sid": FAKE_SID,
    "csrf": FAKE_CSRF,
    "password": FAKE_APP_PASSWORD,
    "hash": FAKE_APP_HASH,
    "secret": FAKE_TOTP_SECRET,
}
FAKE_HOSTNAME = "pihole"
FAKE_HTTP_HOST = "pi.hole"
RPI_MODEL = "Raspberry Pi 4 Model B Rev 1.5"
RPI_MACHINE = "aarch64"
RPI_RELEASE = "6.12.47+rpt-rpi-v8"
RPI_KERNEL_VERSION = "#1 SMP PREEMPT Debian 1:6.12.47-1+rpt1 (2025-09-16)"
RPI_DMI: dict[str, dict[str, str]] = {
    "bios": {"vendor": "Raspberry Pi Ltd", "version": "2025.05.08", "date": "05/08/2025"},
    "board": {"name": "Raspberry Pi 4 Model B", "vendor": "Raspberry Pi Ltd", "version": "1.5"},
    "product": {"name": "Raspberry Pi 4 Model B", "family": "Raspberry Pi", "version": "Rev 1.5"},
    "sys": {"vendor": "Raspberry Pi Ltd"},
}
KEEP_MACS = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}
DOC_NETS4 = tuple(
    ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
DOC_NET6 = ipaddress.ip_network("2001:db8::/32")
FAKE_NET4 = "198.51.100."
FAKE_NET6 = "2001:db8:ffff::"


def log(message: str) -> None:
    """Print a timestamped progress line to stderr."""
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Host secrets (used to verify that nothing leaked)                            #
# --------------------------------------------------------------------------- #
_GENERIC_DMI = {
    "default string",
    "to be filled by o.e.m.",
    "system product name",
    "system manufacturer",
    "not specified",
    "none",
    "unknown",
    "o.e.m.",
    "",
    "0",
    "1.0",
}


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def host_secrets() -> tuple[set[str], dict[str, str]]:
    """Return ``(strings, words)`` identifying the recording machine.

    ``strings`` must not appear anywhere in a fixture (substring match).
    ``words`` maps whole words (host name, user name, interface names) that must
    not appear to their replacement.
    """
    strings: set[str] = set()
    words: dict[str, str] = {}
    uname = os.uname()
    strings.update({uname.release, uname.version})
    hostname = socket.gethostname()
    for word in (hostname, hostname.split(".")[0], getpass.getuser()):
        words[word] = FAKE_HOSTNAME
    for entry in Path("/sys/class/dmi/id").glob("*"):
        if entry.name in {"modalias", "uevent", "power", "subsystem"} or entry.is_dir():
            continue
        value = _read(entry)
        has_letter = any(ch.isalpha() for ch in value)
        if value.lower() not in _GENERIC_DMI and len(value) >= 4 and has_letter:
            strings.add(value)
    for iface in Path("/sys/class/net").glob("*"):
        if iface.name not in {"lo", "eth0", "eth1"} and len(iface.name) >= 3:
            words[iface.name] = "eth0"
        mac = _read(iface / "address").lower()
        if mac and mac not in KEEP_MACS:
            strings.add(mac)
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        out = subprocess.run(
            ["ip", "-j", "addr"], capture_output=True, text=True, check=True, timeout=10
        ).stdout
        for iface_info in json.loads(out):
            for addr in iface_info.get("addr_info", []):
                ip = ipaddress.ip_address(addr["local"])
                if not ip.is_loopback:
                    strings.add(str(ip))
    for line in _read(Path("/etc/resolv.conf")).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"search", "domain"}:
            strings.update(p for p in parts[1:] if "." in p)
        elif len(parts) >= 2 and parts[0] == "nameserver":
            with contextlib.suppress(ValueError):
                if not ipaddress.ip_address(parts[1]).is_loopback:
                    strings.add(parts[1])
    strings.discard("")
    words.pop("", None)
    return strings, words


# --------------------------------------------------------------------------- #
# Sanitiser                                                                    #
# --------------------------------------------------------------------------- #
_MAC_RE = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
_ARPA4_RE = re.compile(r"\b((?:\d{1,3}\.){4})in-addr\.arpa\b", re.IGNORECASE)
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.]*\d)")
_IPV6_RE = re.compile(
    r"(?<![0-9A-Za-z:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![0-9A-Za-z:])"
)


def _keep_ipv4(ip: ipaddress.IPv4Address) -> bool:
    if any(ip in net for net in DOC_NETS4):
        return True
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return True
    if str(ip) == "255.255.255.255":
        return True
    return ip.is_global


def _keep_ipv6(ip: ipaddress.IPv6Address) -> bool:
    if ip in DOC_NET6 or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return True
    # link-local derived from one of our fake MACs 02:00:00:00:00:xx (EUI-64)
    return ip.is_link_local and (int(ip) >> 8) & 0xFFFFFFFFFFFF == 0x0000FFFE0000


@dataclass
class Sanitizer:
    """Deterministic replacement of machine-specific data in recorded JSON.

    Two passes: :meth:`collect` scans every string for addresses so that the
    replacement numbering only depends on the set of addresses (sorted), then
    :meth:`apply` rewrites the data.
    """

    exact: dict[str, str] = field(default_factory=dict)
    words: dict[str, str] = field(default_factory=dict)
    ipv4: dict[str, str] = field(default_factory=dict)
    ipv6: dict[str, str] = field(default_factory=dict)
    macs: dict[str, str] = field(default_factory=dict)
    _seen4: set[ipaddress.IPv4Address] = field(default_factory=set)
    _seen6: set[ipaddress.IPv6Address] = field(default_factory=set)
    _seen_mac: set[str] = field(default_factory=set)

    # -- pass 1 -------------------------------------------------------------- #
    def collect(self, data: Any) -> None:
        """Register every address found in ``data`` (strings, recursively)."""
        for text in _strings(data):
            for mac in _MAC_RE.findall(text):
                norm = mac.lower().replace("-", ":")
                if norm not in KEEP_MACS and not norm.startswith("02:00:00:00:00:"):
                    self._seen_mac.add(norm)
            for match in _ARPA4_RE.finditer(text):
                with contextlib.suppress(ValueError):
                    rev = ".".join(reversed(match.group(1).rstrip(".").split(".")))
                    self._add4(rev)
            for token in _IPV4_RE.findall(text):
                self._add4(token)
            for token in _IPV6_RE.findall(text):
                with contextlib.suppress(ValueError):
                    ip6 = ipaddress.IPv6Address(token)
                    if not _keep_ipv6(ip6):
                        self._seen6.add(ip6)

    def _add4(self, token: str) -> None:
        with contextlib.suppress(ValueError):
            ip = ipaddress.IPv4Address(token)
            if not _keep_ipv4(ip):
                self._seen4.add(ip)

    def finalize(self) -> None:
        """Assign replacement addresses in sorted order."""
        for i, ip4 in enumerate(sorted(self._seen4)):
            self.ipv4[str(ip4)] = f"{FAKE_NET4}{10 + i}"
        link_local = sorted(ip for ip in self._seen6 if ip.is_link_local)
        others = sorted(ip for ip in self._seen6 if not ip.is_link_local)
        for i, ip6 in enumerate(link_local):
            self.ipv6[str(ip6)] = f"fe80::ff:fe00:{0xA0 + i:x}"
        for i, ip6 in enumerate(others):
            self.ipv6[str(ip6)] = f"{FAKE_NET6}{0x10 + i:x}"
        for i, mac in enumerate(sorted(self._seen_mac)):
            self.macs[mac] = f"02:00:00:00:00:{0xA0 + i:02x}"

    # -- pass 2 -------------------------------------------------------------- #
    def text(self, value: str) -> str:
        """Sanitise one string."""
        for old in sorted(self.exact, key=len, reverse=True):
            if old in value:
                value = value.replace(old, self.exact[old])
        for old, new in self.words.items():
            value = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", new, value)
        value = _MAC_RE.sub(lambda m: self.macs.get(m.group(0).lower(), m.group(0)), value)

        def arpa(match: re.Match[str]) -> str:
            octets = match.group(1).rstrip(".").split(".")
            rev = ".".join(reversed(octets))
            new = self.ipv4.get(rev)
            if new is None:
                return match.group(0)
            return ".".join(reversed(new.split("."))) + ".in-addr.arpa"

        value = _ARPA4_RE.sub(arpa, value)
        value = _IPV4_RE.sub(lambda m: self._map4(m.group(0)), value)
        return _IPV6_RE.sub(lambda m: self._map6(m.group(0)), value)

    def _map4(self, token: str) -> str:
        try:
            return self.ipv4.get(str(ipaddress.IPv4Address(token)), token)
        except ValueError:
            return token

    def _map6(self, token: str) -> str:
        try:
            return self.ipv6.get(str(ipaddress.IPv6Address(token)), token)
        except ValueError:
            return token

    def apply(self, data: Any) -> Any:
        """Return a sanitised deep copy of ``data`` (generic rules only)."""
        if isinstance(data, str):
            return self.text(data)
        if isinstance(data, list):
            return [self.apply(v) for v in data]
        if isinstance(data, dict):
            out: dict[str, Any] = {}
            for key, value in data.items():
                if key in ("sid", "csrf") and isinstance(value, str) and value:
                    out[key] = FAKE_SID if key == "sid" else FAKE_CSRF
                elif key == "user_agent" and isinstance(value, str):
                    out[key] = USER_AGENT
                else:
                    out[key] = self.apply(value)
            return out
        return data


def _strings(data: Any) -> Iterator[str]:
    if isinstance(data, str):
        yield data
    elif isinstance(data, list):
        for v in data:
            yield from _strings(v)
    elif isinstance(data, dict):
        for k, v in data.items():
            yield str(k)
            yield from _strings(v)


def _set_if_present(obj: Any, keys: tuple[str, ...], value: Any) -> None:
    for key in keys[:-1]:
        if not isinstance(obj, dict) or key not in obj:
            return
        obj = obj[key]
    if isinstance(obj, dict) and keys[-1] in obj:
        obj[keys[-1]] = value


def _sanitize_host(body: Any) -> None:
    host = body.get("host") if isinstance(body, dict) else None
    if not isinstance(host, dict):
        return
    uname = host.get("uname")
    if isinstance(uname, dict):
        for key, value in (
            ("nodename", FAKE_HOSTNAME),
            ("release", RPI_RELEASE),
            ("version", RPI_KERNEL_VERSION),
            ("machine", RPI_MACHINE),
        ):
            if key in uname:
                uname[key] = value
    if "model" in host:
        host["model"] = RPI_MODEL
    dmi = host.get("dmi")
    if isinstance(dmi, dict):
        for group, fields in dmi.items():
            if not isinstance(fields, dict):
                continue
            for key, value in fields.items():
                if isinstance(value, str):
                    fields[key] = RPI_DMI.get(group, {}).get(key, "Raspberry Pi Ltd")


def _sanitize_sensors(body: Any) -> None:
    sensors = body.get("sensors") if isinstance(body, dict) else None
    if not isinstance(sensors, dict) or not isinstance(sensors.get("list"), list):
        return
    items = [i for i in sensors["list"] if isinstance(i, dict)]
    if not items:
        return
    template = next((i for i in items if i.get("temps")), items[0])
    temp_template = (template.get("temps") or [{}])[0]
    cpu_temp = sensors.get("cpu_temp")
    value = cpu_temp if isinstance(cpu_temp, (int, float)) else 47.2

    def make(name: str, path: str, source: str, temps: list[dict[str, Any]]) -> dict[str, Any]:
        item: dict[str, Any] = {}
        for key, orig in template.items():
            item[key] = {"name": name, "path": path, "source": source, "temps": temps}.get(
                key, orig
            )
        return item

    def temp(t_value: float, crit: float | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in temp_template or {"name": None, "value": None, "sensor": None}:
            out[key] = {
                "name": None,
                "value": t_value,
                "max": None,
                "crit": crit,
                "sensor": "temp1",
            }.get(key, temp_template.get(key))
        return out

    sensors["list"] = [
        make("cpu_thermal", "hwmon0", "devices/virtual/thermal/thermal_zone0", [temp(value, 110)]),
        make("rpi_volt", "hwmon1", "devices/platform/soc/soc:firmware/raspberrypi-hwmon", []),
    ]


def _sanitize_padd(body: Any) -> None:
    if not isinstance(body, dict):
        return
    if "node_name" in body:
        body["node_name"] = FAKE_HOSTNAME
    if "host_model" in body:
        body["host_model"] = RPI_MODEL


def _sanitize_client(body: Any) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("headers"), list):
        return
    for header in body["headers"]:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name", "")).lower()
        if name == "host":
            header["value"] = FAKE_HTTP_HOST
        elif name == "user-agent":
            header["value"] = USER_AGENT
        elif name in ("x-ftl-sid", "sid"):
            header["value"] = FAKE_SID
        elif name == "cookie":
            header["value"] = f"sid={FAKE_SID}"


def _sanitize_app(body: Any) -> None:
    _set_if_present(body, ("app", "password"), FAKE_APP_PASSWORD)
    _set_if_present(body, ("app", "hash"), FAKE_APP_HASH)


def _sanitize_totp(body: Any, recorded_at: float) -> None:
    totp = body.get("totp") if isinstance(body, dict) else None
    if not isinstance(totp, dict):
        return
    if "secret" in totp:
        totp["secret"] = FAKE_TOTP_SECRET
    codes = totp.get("codes")
    if isinstance(codes, list):
        period = int(totp.get("period") or 30)
        half = len(codes) // 2
        totp["codes"] = [
            totp_code(FAKE_TOTP_SECRET, at=recorded_at + (i - half) * period, period=period)
            for i in range(len(codes))
        ]


SPECIFIC: dict[str, Callable[[Any], None]] = {
    "GET /api/info/host": _sanitize_host,
    "GET /api/info/sensors": _sanitize_sensors,
    "GET /api/padd": _sanitize_padd,
    "GET /api/info/client": _sanitize_client,
    "GET /api/auth/app": _sanitize_app,
}


def sanitize_responses(
    responses: Mapping[str, dict[str, Any]],
    *,
    exact: Mapping[str, str],
    words: Mapping[str, str],
    recorded_at: float,
) -> dict[str, dict[str, Any]]:
    """Apply structure-specific then generic sanitisation to every recorded response."""
    staged: dict[str, dict[str, Any]] = json.loads(json.dumps(responses))
    for key, entry in staged.items():
        base = key.split("#", 1)[0]
        body = entry.get("body")
        if base in SPECIFIC:
            SPECIFIC[base](body)
        if base == "GET /api/auth/totp":
            _sanitize_totp(body, recorded_at)
    sanitizer = Sanitizer(exact=dict(exact), words=dict(words))
    sanitizer.collect(staged)
    sanitizer.finalize()
    result: dict[str, dict[str, Any]] = sanitizer.apply(staged)
    return result


def find_leaks(text: str, strings: Iterable[str], words: Iterable[str]) -> list[str]:
    # ``words`` may be a mapping; only the keys matter here.
    """Return human-readable descriptions of host data found in ``text``."""
    problems = [f"host string {s!r}" for s in strings if s and s in text]
    for word in words:
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text, re.IGNORECASE):
            problems.append(f"host word {word!r}")
    for token in set(_IPV4_RE.findall(text)):
        with contextlib.suppress(ValueError):
            if not _keep_ipv4(ipaddress.IPv4Address(token)):
                problems.append(f"non-documentation IPv4 {token}")
    for token in set(_IPV6_RE.findall(text)):
        with contextlib.suppress(ValueError):
            ip6 = ipaddress.IPv6Address(token)
            fake_link_local = ip6.is_link_local and str(ip6).startswith("fe80::ff:fe00:")
            harmless = ip6.is_loopback or ip6.is_unspecified or ip6.is_multicast
            if not (ip6 in DOC_NET6 or harmless or fake_link_local):
                problems.append(f"non-documentation IPv6 {token}")
    for mac in set(_MAC_RE.findall(text)):
        norm = mac.lower()
        if norm not in KEEP_MACS and not norm.startswith("02:00:00:00:00:"):
            problems.append(f"real-looking MAC {mac}")
    return problems


# --------------------------------------------------------------------------- #
# Recording                                                                    #
# --------------------------------------------------------------------------- #
def response_key(method: str, path: str, variant: str | None = None) -> str:
    """Build the fixture key ``METHOD /api/<path>[?sorted query][#variant]``."""
    route, _, query = path.lstrip("/").removeprefix("api/").partition("?")
    key = f"{method.upper()} /api/{route}"
    if query:
        key += "?" + "&".join(sorted(query.split("&")))
    if variant:
        key += f"#{variant}"
    return key


@dataclass
class Recorder:
    """Collects responses (and the raw secrets they contain) for one container."""

    container: PiholeContainer
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def note_secrets(self, body: Any) -> None:
        """Remember sids/csrf tokens/app passwords/TOTP secrets so they are scrubbed everywhere.

        Only the objects that carry credentials are inspected (``session``,
        ``app`` and ``totp``) - a generic ``hash`` key elsewhere is a git hash.
        """
        if not isinstance(body, dict):
            return
        wanted = {"session": ("sid", "csrf"), "app": ("password", "hash"), "totp": ("secret",)}
        for container_key, fields in wanted.items():
            obj = body.get(container_key)
            if not isinstance(obj, dict):
                continue
            for key in fields:
                value = obj.get(key)
                if isinstance(value, str) and len(value) >= 8:
                    self.secrets[value] = SECRET_KEYS[key]

    def call(
        self,
        method: str,
        path: str,
        *,
        variant: str | None = None,
        body: Any = None,
        sid: str | None = None,
        headers: Mapping[str, str] | None = None,
        record: bool = True,
        expect: Iterable[int] | None = None,
    ) -> ApiResponse:
        """Perform a request and (by default) store it under its fixture key."""
        resp = self.container.api(method, path, body=body, sid=sid, headers=headers)
        self.note_secrets(resp.body)
        if record:
            entry: dict[str, Any] = {"status": resp.status, "body": resp.body}
            if resp.body is None and resp.text.strip():
                entry["text"] = resp.text[:2000]
            with self.lock:
                self.responses[response_key(method, path, variant)] = entry
        if expect is not None and resp.status not in set(expect):
            label = variant or "-"
            log(f"  ! {method} {path} ({label}): unexpected {resp.status} {resp.text[:160]}")
        return resp

    def login(self, password: str = PASSWORD, **kwargs: Any) -> str | None:
        """Log in (not recorded unless ``variant`` given); return the sid."""
        variant = kwargs.pop("variant", None)
        resp = self.call(
            "POST",
            "auth",
            body={"password": password, **kwargs},
            variant=variant,
            record=variant is not None,
        )
        if resp.status != 200:
            raise ContainerError(f"login failed: {resp.status} {resp.text[:200]}")
        sid = resp.body["session"]["sid"]
        return sid if isinstance(sid, str) and sid else None

    def logout(self, sid: str | None) -> None:
        """Log out ``sid`` (not recorded)."""
        if sid:
            self.call("DELETE", "auth", sid=sid, record=False)

    def session_valid(self, sid: str | None) -> bool:
        """Whether ``sid`` is still accepted (``GET /api/auth``)."""
        if not sid:
            return False
        status: int = self.call("GET", "auth", sid=sid, record=False).status
        return status == 200


@dataclass
class Lab:
    """Shared state for a recording run."""

    runtime: Runtime
    seed: int
    settle: float
    keep: bool
    created_network: bool = False

    def ensure_network(self) -> None:
        """Create the documentation-range network unless it exists."""
        if self.runtime.network_exists(NETWORK):
            return
        self.runtime.create_network(
            NETWORK, subnet=SUBNET4, gateway=GATEWAY4, ipv6_subnet=SUBNET6, ipv6_gateway=GATEWAY6
        )
        self.created_network = True

    def teardown(self) -> None:
        """Remove the network if this run created it."""
        if self.created_network and not self.keep:
            self.runtime.remove_network(NETWORK)


def _container(lab: Lab, tag: str, *, nopw: bool) -> PiholeContainer:
    env = {
        "FTLCONF_dns_upstreams": GATEWAY4,
        # flush queries to the on-disk database every 10 s (default 60 s)
        "FTLCONF_database_DBinterval": "10",
    }
    return PiholeContainer(
        tag,
        password="" if nopw else PASSWORD,
        name=f"pihello-survey-{tag}" + ("-nopw" if nopw else ""),
        runtime=lab.runtime,
        env=env,
        network=NETWORK,
        ip=NOPW_IP4 if nopw else PIHOLE_IP4,
        ip6=NOPW_IP6 if nopw else PIHOLE_IP6,
        mac_address=NOPW_MAC if nopw else PIHOLE_MAC,
        hostname=FAKE_HOSTNAME,
        # The image's update checker makes 4 unauthenticated api.github.com calls
        # per start (limit: 60/hour/IP). Only the main container may use them, so
        # a full run (10 tags) records real remote versions in info/version.
        extra_args=["--dns-search", "."]
        + (["--add-host", "api.github.com:127.0.0.1"] if nopw else []),
        keep=lab.keep,
    )


def _gravity_domains(rec: Recorder, sid: str | None, rng: random.Random) -> list[str]:
    """Pick well-known ad domains that are really on this gravity list."""
    names = ",".join(f"'{d}'" for d in GRAVITY_CANDIDATES)
    found: list[str] = []
    with contextlib.suppress(ContainerError):
        out = rec.container.exec(
            "pihole-FTL",
            "sqlite3",
            "/etc/pihole/gravity.db",
            f"SELECT DISTINCT domain FROM gravity WHERE domain IN ({names}) ORDER BY domain;",  # noqa: S608 - constant list
        ).stdout
        found = [line.strip() for line in out.splitlines() if line.strip()]
    if not found:  # fall back to the search API
        for domain in GRAVITY_CANDIDATES:
            resp = rec.call("GET", f"search/{domain}", sid=sid, record=False)
            if resp.status == 200 and resp.body.get("search", {}).get("gravity"):
                found.append(domain)
    if len(found) < 10:
        with contextlib.suppress(ContainerError):
            out = rec.container.exec(
                "pihole-FTL",
                "sqlite3",
                "/etc/pihole/gravity.db",
                "SELECT domain FROM gravity ORDER BY domain LIMIT 12 OFFSET 20000;",
            ).stdout
            found += [line.strip() for line in out.splitlines() if line.strip()]
    rng.shuffle(found)
    return found


def _plan(rng: random.Random, count: int, ad_ratio: float, ads: list[str]) -> list[tuple[str, str]]:
    """A realistic query mix: browsing (A/AAAA/HTTPS), ads, lists, odd types."""
    allowed = list(ALLOWED)
    weights = list(ALLOWED.values())
    ad_weights = [1.0 / (i + 1) for i in range(len(ads))]
    plan: list[tuple[str, str]] = []
    while len(plan) < count:
        roll = rng.random()
        if roll < ad_ratio and ads:
            domain = rng.choices(ads, ad_weights)[0]
            plan.append((domain, "A"))
            if rng.random() < 0.5:
                plan.append((domain, "AAAA"))
        elif roll < ad_ratio + 0.05:
            plan.append((rng.choice(DENY_EXACT + REGEX_QUERIES), rng.choice(("A", "AAAA"))))
        elif roll < ad_ratio + 0.07:
            plan.append((rng.choice(NXDOMAINS), "A"))
        elif roll < ad_ratio + 0.10:
            plan.append(rng.choice(SPECIAL_QUERIES))
        else:
            domain = rng.choices(allowed, weights)[0]
            plan.append((domain, "A"))
            plan.append((domain, "AAAA"))
            if rng.random() < 0.4:
                plan.append((domain, "HTTPS"))
    return plan[:count]


def _run_lan_client(lab: Lab, tag: str, client: LanClient, plan: list[tuple[str, str]]) -> None:
    server = PIHOLE_IP6 if client.use_ipv6 else PIHOLE_IP4
    batch = "".join(f"@{server} +tries=2 +time=3 {name} {qtype}\n" for name, qtype in plan)
    name = f"pihello-survey-{tag}-client-{client.name}"
    lab.runtime.remove_container(name)
    try:
        lab.runtime.run(
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--network",
            NETWORK,
            "--ip",
            client.ip4,
            "--ip6",
            client.ip6,
            "--mac-address",
            client.mac,
            "--hostname",
            client.name,
            "--dns-search",
            ".",
            "--entrypoint",
            "sh",
            f"docker.io/pihole/pihole:{tag}",
            "-c",
            "cat > /tmp/batch && dig -f /tmp/batch > /dev/null 2>&1; echo done",
            input=batch,
            timeout=600,
        )
    finally:
        lab.runtime.remove_container(name)


_RESTART_MARK = "Restarting FTL"
_STARTED_MARK = "FTL started on"


def _log_count(container: PiholeContainer, mark: str) -> int:
    return container.logs(tail=None).count(mark)


def await_possible_restart(
    container: PiholeContainer, started_before: int, *, grace: float = 4.0
) -> bool:
    """After a config change, wait for an FTL self-restart (if any) to finish.

    Items flagged ``FLAG_RESTART_FTL`` (e.g. ``webserver.api.max_sessions``)
    make FTL restart itself about a second after the PATCH; sessions are
    restored afterwards. Returns ``True`` when a restart happened.
    """
    deadline = time.monotonic() + grace
    restarted = False
    while time.monotonic() < deadline:
        if _log_count(container, _STARTED_MARK) > started_before:
            restarted = True
            break
        time.sleep(0.5)
    if restarted or _log_count(container, _RESTART_MARK) > 0:
        container.wait_ready(timeout=120, gravity=False)
    return restarted


def configure_local_hosts(rec: Recorder, sid: str | None, survey: dict[str, Any]) -> str | None:
    """Add local DNS records for the LAN clients (names in top_clients).

    Set through the API rather than ``FTLCONF_dns_hosts`` so the item stays
    writable. It is the last config write before the traffic: config writes
    that restart FTL after the traffic would disturb the statistics.
    Returns a valid sid.
    """
    started = _log_count(rec.container, _STARTED_MARK)
    rec.call(
        "PATCH",
        "config",
        sid=sid,
        record=False,
        expect=(200,),
        body={"config": {"dns": {"hosts": LOCAL_HOSTS}}},
    )
    restarted = await_possible_restart(rec.container, started)
    survey["ftl_restart_on_dns_hosts_change"] = restarted
    survey["sessions_invalidated_by_dns_hosts_change"] = not rec.session_valid(sid)
    if restarted or survey["sessions_invalidated_by_dns_hosts_change"]:
        return rec.login()
    return sid


def generate_traffic(lab: Lab, rec: Recorder, sid: str | None, tag: str) -> dict[str, int]:
    """Seed lists and send DNS traffic; return per-source query counts."""
    rng = random.Random(lab.seed)  # noqa: S311 - reproducible traffic, not crypto
    rec.call(
        "POST",
        "domains/deny/exact",
        sid=sid,
        record=False,
        expect=(201,),
        body={
            "domain": list(DENY_EXACT),
            "comment": "pihello survey",
            "groups": [0],
            "enabled": True,
        },
    )
    rec.call(
        "POST",
        "domains/deny/regex",
        sid=sid,
        record=False,
        expect=(201,),
        body={
            "domain": DENY_REGEX,
            "comment": "pihello survey regex",
            "groups": [0],
            "enabled": True,
        },
    )
    rec.call(
        "POST",
        "domains/allow/exact",
        sid=sid,
        record=False,
        expect=(201,),
        body={
            "domain": list(ALLOW_EXACT),
            "comment": "allow shopping ads",
            "groups": [0],
            "enabled": True,
        },
    )
    rec.call(
        "POST",
        "groups",
        sid=sid,
        record=False,
        expect=(201,),
        body={
            "name": "iot",
            "comment": "TVs and smart plugs",
            "enabled": True,
        },
    )
    ads = _gravity_domains(rec, sid, rng)
    log(f"  gravity sample: {len(ads)} ad domains")
    counts: dict[str, int] = {}
    plans = {c.name: _plan(rng, c.queries, c.ad_ratio, ads) for c in LAN_CLIENTS}
    host_plan = _plan(rng, 220, 0.25, ads)
    local_plan = [
        ("pi.hole", "A"),
        ("router.lan", "A"),
        ("pool.ntp.org", "A"),
        ("github.com", "A"),
        ("pi-hole.net", "A"),
        ("raw.githubusercontent.com", "A"),
    ]

    def from_host() -> None:
        host, port = rec.container.dns_address
        results = dnsgen.query_many(host_plan, server=host, port=port, timeout=3, concurrency=4)
        failed = sum(isinstance(r, dnsgen.DnsQueryError) for r in results)
        counts["host"] = len(results) - failed
        if failed:
            log(f"  ! {failed} host queries failed")

    def from_localhost() -> None:
        batch = "".join(f"@127.0.0.1 +tries=2 +time=3 {n} {t}\n" for n, t in local_plan)
        rec.container.exec(
            "sh", "-c", "cat > /tmp/batch && dig -f /tmp/batch >/dev/null 2>&1", input=batch
        )
        counts["localhost"] = len(local_plan)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(LAN_CLIENTS) + 2) as pool:
        futures = [pool.submit(from_host), pool.submit(from_localhost)]
        for client in LAN_CLIENTS:
            futures.append(pool.submit(_run_lan_client, lab, tag, client, plans[client.name]))
            counts[client.name] = len(plans[client.name])
        for future in futures:
            future.result()
    return counts


def _wait_next_totp_window(period: int = 30) -> None:
    time.sleep(period - (time.time() % period) + 1.5)


def record_auth_variants(rec: Recorder, survey: dict[str, Any]) -> str | None:
    """App password, TOTP and seat-limit variants. Returns a fresh valid sid."""
    sid = rec.login()
    # ---- app password -------------------------------------------------------
    app = rec.call("GET", "auth/app", sid=sid, expect=(200,))
    if app.status == 200:
        app_pw, app_hash = app.body["app"]["password"], app.body["app"]["hash"]
        rec.call(
            "PATCH",
            "config",
            sid=sid,
            record=False,
            expect=(200,),
            body={"config": {"webserver": {"api": {"app_pwhash": app_hash}}}},
        )
        survey["sessions_invalidated_by_app_pwhash_change"] = not rec.session_valid(sid)
        if not survey["sessions_invalidated_by_app_pwhash_change"]:
            rec.logout(sid)
        resp = rec.call("POST", "auth", variant="app-password", body={"password": app_pw})
        app_sid = resp.body.get("session", {}).get("sid") if resp.status == 200 else None
        if app_sid:
            rec.call("GET", "auth", variant="app-password", sid=app_sid)
            rec.call("GET", "stats/summary", variant="app-password", sid=app_sid)
            rec.call(
                "PATCH",
                "config",
                variant="app-password-forbidden",
                sid=app_sid,
                body={"config": {"webserver": {"api": {"max_sessions": 16}}}},
            )
            rec.call("DELETE", "auth", variant="app-password", sid=app_sid)
        sid = rec.login()
        rec.call(
            "PATCH",
            "config",
            sid=sid,
            record=False,
            expect=(200,),
            body={"config": {"webserver": {"api": {"app_pwhash": ""}}}},
        )
        if not rec.session_valid(sid):
            sid = rec.login()
    # ---- TOTP ---------------------------------------------------------------
    totp = rec.call("GET", "auth/totp", sid=sid, expect=(200,))
    if totp.status == 200 and isinstance(totp.body, dict) and "totp" in totp.body:
        secret = totp.body["totp"]["secret"]
        _wait_if_window_ending()
        rec.call(
            "PATCH",
            "config",
            sid=sid,
            record=False,
            expect=(200,),
            body={"config": {"webserver": {"api": {"totp_secret": secret}}}},
        )
        still_valid = rec.call("GET", "auth", sid=sid, variant="totp-enabled-old-session")
        survey["sessions_invalidated_by_totp_change"] = still_valid.status != 200
        if still_valid.status == 200:
            rec.logout(sid)
        rec.call("GET", "auth", variant="totp-enabled-unauthenticated")
        rec.call("POST", "auth", variant="totp-required", body={"password": PASSWORD})
        rec.call("POST", "auth", variant="totp-wrong-password", body={"password": "wrong"})
        bad = (totp_code(secret) + 500_000) % 1_000_000
        rec.call("POST", "auth", variant="totp-invalid", body={"password": PASSWORD, "totp": bad})
        rec.call(
            "POST", "auth", variant="totp-rate-limited", body={"password": PASSWORD, "totp": bad}
        )
        totp_sid: str | None = None
        for _attempt in range(3):
            _wait_next_totp_window()
            resp = rec.call(
                "POST",
                "auth",
                variant="totp",
                body={"password": PASSWORD, "totp": totp_code(secret)},
            )
            if resp.status == 200:
                totp_sid = resp.body["session"]["sid"]
                break
        if totp_sid:
            rec.call(
                "PATCH",
                "config",
                sid=totp_sid,
                record=False,
                expect=(200,),
                body={"config": {"webserver": {"api": {"totp_secret": ""}}}},
            )
            if rec.session_valid(totp_sid):
                rec.logout(totp_sid)
        else:
            log("  ! TOTP login failed; disabling TOTP via pihole-FTL --config")
            rec.container.ftl_config("webserver.api.totp_secret", "")
        sid = rec.login()
    # ---- seat limit ---------------------------------------------------------
    # webserver.api.max_sessions carries FLAG_RESTART_FTL: FTL restarts and
    # restores the sessions from its database.
    started = _log_count(rec.container, _STARTED_MARK)
    rec.call(
        "PATCH",
        "config",
        sid=sid,
        record=False,
        expect=(200,),
        body={"config": {"webserver": {"api": {"max_sessions": 2}}}},
    )
    survey["ftl_restart_on_max_sessions_change"] = await_possible_restart(rec.container, started)
    survey["max_sessions_change_keeps_sessions"] = rec.session_valid(sid)
    if not survey["max_sessions_change_keeps_sessions"]:
        sid = rec.login()
    extra: list[str] = []
    try:
        for attempt in range(24):
            resp = rec.call("POST", "auth", body={"password": PASSWORD}, record=False)
            if resp.status == 200:
                extra.append(resp.body["session"]["sid"])
                continue
            rec.responses[response_key("POST", "auth", "seats-exceeded")] = {
                "status": resp.status,
                "body": resp.body,
            }
            survey["seats_exceeded_after_logins"] = attempt + 1
            survey["max_sessions_runtime_change_effective"] = attempt == 1
            break
    finally:
        for other in extra:
            rec.logout(other)
    started = _log_count(rec.container, _STARTED_MARK)
    rec.call(
        "PATCH",
        "config",
        sid=sid,
        record=False,
        expect=(200,),
        body={"config": {"webserver": {"api": {"max_sessions": 16}}}},
    )
    await_possible_restart(rec.container, started)
    if not rec.session_valid(sid):
        sid = rec.login()
    return sid


def _wait_if_window_ending(period: int = 30, margin: float = 8.0) -> None:
    """Avoid enabling TOTP right before a window boundary (keeps timing predictable)."""
    remaining = period - (time.time() % period)
    if remaining < margin:
        time.sleep(remaining + 0.5)


def record_header_variants(rec: Recorder, sid: str, survey: dict[str, Any]) -> None:
    """How the session id may be transported."""
    query = rec.call("GET", f"stats/summary?sid={quote(sid, safe='')}", record=False)
    rec.responses[response_key("GET", "stats/summary", "sid-query")] = {
        "status": query.status,
        "body": query.body,
    }
    survey["sid_transport_status"] = {
        "X-FTL-SID header": rec.call("GET", "stats/summary", sid=sid, record=False).status,
        "sid header": rec.call(
            "GET", "stats/summary", variant="sid-header", headers={"sid": sid}
        ).status,
        "sid query parameter": query.status,
        "sid cookie (GET)": rec.call(
            "GET", "stats/summary", variant="sid-cookie", headers={"Cookie": f"sid={sid}"}
        ).status,
        "Authorization: Bearer": rec.call(
            "GET", "stats/summary", record=False, headers={"Authorization": f"Bearer {sid}"}
        ).status,
    }


def _tls_probe(host: str, port: int, cafile: str, *, strict: bool, hostname: str) -> dict[str, Any]:
    """Handshake with ``cafile``; ``strict`` keeps Python 3.13's VERIFY_X509_STRICT."""
    ctx = ssl.create_default_context(cafile=cafile)
    if not strict:
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ctx.check_hostname = hostname != ""
    try:
        with (
            socket.create_connection((host, port), timeout=10) as sock,
            ctx.wrap_socket(sock, server_hostname=hostname or None) as tls,
        ):
            cert: Any = tls.getpeercert() or {}
    except (ssl.SSLError, ssl.CertificateError, OSError) as exc:
        reason = getattr(exc, "verify_message", None) or getattr(exc, "reason", None) or exc
        return {"ok": False, "error": str(reason)}
    subject = {k: v for rdn in cert.get("subject", ()) for k, v in rdn}
    issuer = {k: v for rdn in cert.get("issuer", ()) for k, v in rdn}
    return {
        "ok": True,
        "subject_cn": subject.get("commonName"),
        "issuer_cn": issuer.get("commonName"),
        "subject_alt_names": [v for _k, v in cert.get("subjectAltName", ())],
    }


def record_tls(rec: Recorder, survey: dict[str, Any]) -> None:
    """Check HTTPS against the container's CA certificate (strict and relaxed X.509)."""
    info: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="pihello-survey-") as tmp:
        try:
            ca = rec.container.tls_ca_cert(tmp)
        except ContainerError as exc:
            survey["tls"] = {"error": str(exc)}
            return
        info["ca_file"] = "/etc/pihole/" + ca.name
        host, port = rec.container.host, rec.container.https_port or 443
        for label, strict, name in (
            ("python_default_context", True, ""),
            ("without_VERIFY_X509_STRICT", False, ""),
            ("hostname_pi.hole", False, FAKE_HTTP_HOST),
            ("hostname_localhost", False, "localhost"),
        ):
            info[label] = _tls_probe(host, port, str(ca), strict=strict, hostname=name)
        resp = rec.container.api("GET", "info/login", https=True, verify=False)
        info["https_info_login_status"] = resp.status
    survey["tls"] = info


def record_nopw(rec: Recorder) -> None:
    """Variants from the password-less container."""
    rec.call("GET", "info/login", variant="no-password")
    resp = rec.call("POST", "auth", variant="no-password", body={"password": ""})
    rec.call("POST", "auth", variant="no-password-any-password", body={"password": "anything"})
    rec.call("POST", "auth", variant="no-password-no-body")
    rec.call("GET", "auth", variant="no-password")
    rec.call("GET", "stats/summary", variant="no-password")
    rec.call("GET", "dns/blocking", variant="no-password")
    rec.call("GET", "info/version", variant="no-password")
    rec.call("GET", "auth/sessions", variant="no-password")
    sid = resp.body.get("session", {}).get("sid") if isinstance(resp.body, dict) else None
    rec.call("DELETE", "auth", variant="no-password", sid=sid)


def record_tag(lab: Lab, tag: str) -> dict[str, Any]:
    """Record one Docker tag and return the (unsanitised) fixture."""
    main = _container(lab, tag, nopw=False)
    nopw = _container(lab, tag, nopw=True)
    rec, rec_nopw = Recorder(main), Recorder(nopw)
    survey: dict[str, Any] = {}
    sid: str | None = None
    started = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(main.start), pool.submit(nopw.start)]:
                future.result()
        survey["startup_seconds"] = round(time.monotonic() - started, 1)
        log(
            f"  containers ready in {survey['startup_seconds']}s "
            f"(http {main.http_port}, https {main.https_port}, dns {main.dns_port})"
        )
        # -- unauthenticated basics -----------------------------------------
        rec.call("GET", "info/login", expect=(200,))
        rec.call("GET", "auth", variant="unauthenticated")
        record_nopw(rec_nopw)
        nopw.stop()
        # -- auth variants: they write config, so they come before traffic ----
        resp = rec.call("POST", "auth", body={"password": PASSWORD}, expect=(200,))
        rec.logout(resp.body["session"]["sid"])
        sid = record_auth_variants(rec, survey)
        # -- local DNS records: the last config write (see configure_local_hosts)
        sid = configure_local_hosts(rec, sid, survey)
        restarts_before = _log_count(main, _STARTED_MARK)
        counts = generate_traffic(lab, rec, sid, tag)
        traffic_done = time.monotonic()
        survey["queries_sent"] = counts
        log(f"  traffic: {sum(counts.values())} queries {counts}")
        wait = lab.settle - (time.monotonic() - traffic_done)
        if wait > 0:
            log(f"  settling {wait:.0f}s (database flush, ARP table)")
            time.sleep(wait)
        if not rec.session_valid(sid):
            sid = rec.login()
        assert sid is not None
        # -- data endpoints ----------------------------------------------------
        for endpoint in GET_ENDPOINTS:
            rec.call("GET", endpoint, sid=sid)
        version = rec.responses["GET /api/info/version"]["body"] or {}
        remote = ((version.get("version") or {}).get("core") or {}).get("remote") or {}
        survey["update_check_got_remote_versions"] = remote.get("version") is not None
        if not survey["update_check_got_remote_versions"]:
            log("  ! info/version has no remote versions (GitHub API rate limit?)")
        listening = rec.call("GET", "config/dns/listeningMode", sid=sid, record=False)
        with contextlib.suppress(KeyError, TypeError):
            survey["dns_listeningMode"] = listening.body["config"]["dns"]["listeningMode"]
        # -- blocking ------------------------------------------------------------
        rec.call(
            "POST", "dns/blocking", sid=sid, body={"blocking": False, "timer": 300}, expect=(200,)
        )
        rec.call("GET", "dns/blocking", variant="disabled", sid=sid)
        rec.call("GET", "stats/summary", variant="blocking-disabled", sid=sid)
        rec.call("POST", "dns/blocking", variant="enable", sid=sid, body={"blocking": True})
        # -- transports, TLS, errors ---------------------------------------------
        record_header_variants(rec, sid, survey)
        record_tls(rec, survey)
        rec.call("GET", "does/not/exist", sid=sid)
        rec.call("GET", "does/not/exist", variant="unauthenticated")
        rec.call("PUT", "info/version", variant="method-not-allowed", sid=sid, body={})
        rec.call("GET", "stats/summary", variant="unauthenticated")
        rec.call("GET", "stats/summary", variant="invalid-sid", sid="AAAAAAAAAAAAAAAAAAAAAA=")
        rec.call("GET", "info/login", variant="authenticated", sid=sid)
        survey["ftl_restarts_during_recording"] = _log_count(main, _STARTED_MARK) - restarts_before
        if survey["ftl_restarts_during_recording"]:
            log(f"  ! FTL restarted {survey['ftl_restarts_during_recording']}x while recording")
        rec.call("DELETE", "auth", sid=sid, expect=(204, 200))
        logged_out, sid = sid, None
        rec.call("DELETE", "auth", variant="session-unknown", sid=logged_out)
        rec.call("GET", "stats/summary", variant="session-expired", sid=logged_out)
        rec.call("DELETE", "auth", variant="unauthenticated")
        rec.call("POST", "auth", variant="no-body")
        rec.call("POST", "auth", variant="empty-password", body={"password": ""})
        rec.call("POST", "auth", variant="wrong-password", body={"password": "wrong-password"})
        rapid: list[int] = []
        for _ in range(6):
            burst = rec.call("POST", "auth", body={"password": "wrong"}, record=False)
            rapid.append(burst.status)
            if burst.status == 429:
                key = response_key("POST", "auth", "login-rate-limited")
                rec.responses.setdefault(key, {"status": burst.status, "body": burst.body})
        survey["wrong_password_burst_statuses"] = rapid
        check = rec.call("POST", "auth", body={"password": PASSWORD}, record=False)
        survey["login_after_wrong_burst_same_second"] = check.status
        if check.status == 200:
            rec.logout(check.body["session"]["sid"])
        time.sleep(1.2)
        check = rec.call("POST", "auth", body={"password": PASSWORD}, record=False)
        survey["login_after_wrong_burst_next_second"] = check.status
        if check.status == 200:
            rec.logout(check.body["session"]["sid"])
        survey["image_digest"] = _image_digest(lab.runtime, main.image_ref)
    except BaseException:
        with contextlib.suppress(ContainerError):
            interesting = [
                line
                for line in main.logs(tail=400).splitlines()
                if re.search(r"ERR|WARN|CRIT|Restart|started on|terminated|API", line)
            ]
            log("  container log (filtered tail):\n    " + "\n    ".join(interesting[-40:]))
        raise
    finally:
        if sid:
            with contextlib.suppress(OSError):
                rec.logout(sid)
        main.stop()
        nopw.stop()
    responses = {**rec.responses, **rec_nopw.responses}
    return {
        "responses": responses,
        "secrets": {**rec.secrets, **rec_nopw.secrets},
        "survey": survey,
    }


def _image_digest(runtime: Runtime, image: str) -> str | None:
    proc = runtime.run("image", "inspect", "--format", "{{.Digest}}", image, check=False)
    digest = proc.stdout.strip()
    return digest if digest.startswith("sha256:") else None


def versions_of(responses: Mapping[str, dict[str, Any]], tag: str) -> dict[str, str | None]:
    """``{"core","web","ftl","docker"}`` from the recorded ``info/version``."""
    body = responses.get("GET /api/info/version", {}).get("body") or {}
    version = body.get("version", {})
    out: dict[str, str | None] = {}
    for component in ("core", "web", "ftl"):
        out[component] = ((version.get(component) or {}).get("local") or {}).get("version")
    out["docker"] = (version.get("docker") or {}).get("local") or tag
    return out


class LeakError(Exception):
    """Sanitised output still contains data from the recording machine."""


def write_fixture(tag: str, raw: dict[str, Any], out_dir: Path) -> Path:
    """Sanitise, verify and write ``<out_dir>/<tag>/responses.json``."""
    recorded_at = time.time()
    strings, words = host_secrets()
    exact = dict(raw["secrets"])
    exact.update({s: "[redacted]" for s in strings if len(s) >= 6})
    word_map = {w: new for w, new in words.items() if len(w) >= 3}
    cleaned = sanitize_responses(
        {**raw["responses"], "__survey__": {"body": raw["survey"]}},
        exact=exact,
        words=word_map,
        recorded_at=recorded_at,
    )
    survey = cleaned.pop("__survey__")["body"]
    responses = cleaned
    fixture = {
        "meta": {
            "docker_tag": tag,
            "versions": versions_of(responses, tag),
            "password": PASSWORD,
            "recorded_at": dt.datetime.fromtimestamp(recorded_at, dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "recorder": RECORDER,
            "survey": survey,
        },
        "responses": dict(sorted(responses.items())),
    }
    text = json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"
    leaks = find_leaks(text, set(strings) | set(raw["secrets"]), words)
    if leaks:
        raise LeakError(f"refusing to write fixture, leaks found: {sorted(set(leaks))}")
    target = out_dir / tag / "responses.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _tag_key(tag: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", tag))


def update_index(out_dir: Path) -> Path:
    """Rebuild ``index.json`` from every recorded fixture in ``out_dir``."""
    index: dict[str, Any] = {}
    for path in sorted(out_dir.glob("*/responses.json"), key=lambda p: _tag_key(p.parent.name)):
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        index[meta["docker_tag"]] = dict(meta["versions"])
    tags = [t for t in index if t != "latest"]
    if tags:
        index["latest"] = max(tags, key=_tag_key)
    target = out_dir / "index.json"
    target.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return target


def list_v6_tags() -> list[str]:
    """Every docker-pi-hole release tag from 2025.02.0 on (GitHub API)."""
    tags: list[str] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/pi-hole/docker-pi-hole/releases?per_page=100&page={page}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.load(resp)
        if not batch:
            break
        tags += [r["tag_name"] for r in batch if not r.get("draft") and not r.get("prerelease")]
        page += 1
    v6 = [t for t in tags if re.fullmatch(r"\d{4}\.\d{2}\.\d+", t)]
    return sorted((t for t in v6 if _tag_key(t) >= _tag_key(FIRST_V6_TAG)), key=_tag_key)


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #
def _shape(value: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    """Flatten JSON into ``{path: type}``; list items are merged under ``[]``."""
    out: dict[str, str] = {}
    if isinstance(value, dict):
        if prefix and depth > 6:
            return {prefix: "object"}
        for key, item in value.items():
            if key == "took":
                continue
            name = f"{prefix}.{_dynamic_key(key)}" if prefix else key
            out.update(_shape(item, name, depth + 1))
        if not value and prefix:
            out[prefix] = "object"
    elif isinstance(value, list):
        if not value:
            out[prefix] = "array"
        for item in value:
            for key, kind in _shape(item, prefix + "[]", depth + 1).items():
                merged = kind
                if out.get(key) not in (None, kind):
                    merged = "|".join(sorted(set(out[key].split("|")) | {kind}))
                out[key] = merged
    else:
        names = {bool: "bool", int: "int", float: "float", str: "str", type(None): "null"}
        out[prefix or "."] = names.get(type(value), type(value).__name__)
    return out


def _dynamic_key(key: str) -> str:
    """Collapse data-dependent object keys (client IPs, numbers) to a placeholder."""
    if key.isdigit():
        return "<n>"
    try:
        ipaddress.ip_address(key)
    except ValueError:
        return key
    return "<ip>"


def _shape_diff(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Describe added/removed/retyped paths, ignoring data-dependent differences.

    Empty arrays on either side hide their item fields, and ``null`` (as well
    as int-vs-float) is not considered a type change.
    """
    empty = {p for p, k in {**old, **new}.items() if k == "array"}

    def hidden(path: str) -> bool:
        return path in empty or any(path.startswith(e + "[]") for e in empty)

    def kinds(kind: str) -> set[str]:
        out = set(kind.split("|")) - {"null"}
        return {"number"} if out <= {"int", "float"} and out else out

    changes = [f"added `{p}` ({new[p]})" for p in sorted(set(new) - set(old)) if not hidden(p)]
    changes += [f"removed `{p}` ({old[p]})" for p in sorted(set(old) - set(new)) if not hidden(p)]
    for path in sorted(set(old) & set(new)):
        before, after = kinds(old[path]), kinds(new[path])
        if before and after and before != after and not hidden(path):
            changes.append(f"`{path}`: {old[path]} -> {new[path]}")
    return changes


def report(out_dir: Path) -> str:
    """Markdown tables comparing every recorded fixture (endpoints, fields, statuses)."""
    fixtures: dict[str, dict[str, Any]] = {}
    for path in sorted(out_dir.glob("*/responses.json"), key=lambda p: _tag_key(p.parent.name)):
        data = json.loads(path.read_text(encoding="utf-8"))
        fixtures[data["meta"]["docker_tag"]] = data
    tags = list(fixtures)
    lines = ["| Docker tag | Core | Web | FTL |", "| :-- | :-- | :-- | :-- |"]
    for tag in tags:
        v = fixtures[tag]["meta"]["versions"]
        lines.append(f"| `{tag}` | {v['core']} | {v['web']} | {v['ftl']} |")
    lines += ["", "### Status codes per recorded key", ""]
    keys = sorted({k for f in fixtures.values() for k in f["responses"]})
    lines.append("| Key | " + " | ".join(f"`{t}`" for t in tags) + " |")
    lines.append("| :-- |" + " --: |" * len(tags))
    for key in keys:
        row = [str(fixtures[t]["responses"].get(key, {}).get("status", "-")) for t in tags]
        if len(set(row)) == 1 and row[0] in ("200", "204"):
            continue
        lines.append(f"| `{key}` | " + " | ".join(row) + " |")
    lines += ["", "### Field differences (relative to the previous recorded tag)", ""]
    previous: dict[str, dict[str, str]] = {}
    for tag in tags:
        shapes = {
            k: _shape(v["body"])
            for k, v in fixtures[tag]["responses"].items()
            if v.get("status") == 200 and "#" not in k
        }
        if previous:
            changes: list[str] = []
            for key in sorted(set(shapes) | set(previous)):
                old, new = previous.get(key), shapes.get(key)
                if old is None and new is not None:
                    changes.append(f"- `{key}`: now returns 200")
                elif old is not None and new is None:
                    changes.append(f"- `{key}`: no longer returns 200")
                elif old is not None and new is not None:
                    changes += [f"- `{key}`: {c}" for c in _shape_diff(old, new)]
            lines.append(f"#### `{tag}`")
            lines.append("")
            lines += changes or ["- no field changes"]
            lines.append("")
        previous = shapes
    endpoint_sets: dict[str, set[str]] = {}
    for tag in tags:
        body = fixtures[tag]["responses"].get("GET /api/endpoints", {}).get("body") or {}
        eps = set()
        for method, items in (body.get("endpoints") or {}).items():
            for item in items:  # v6.0 lists some routes only with their parameters
                eps.add(f"{method.upper()} {item['uri']}")
        endpoint_sets[tag] = eps
    lines += ["### `/api/endpoints` differences", ""]
    for prev, tag in itertools.pairwise(tags):
        added = sorted(endpoint_sets[tag] - endpoint_sets[prev])
        removed = sorted(endpoint_sets[prev] - endpoint_sets[tag])
        if added or removed:
            lines.append(
                f"- `{tag}`: "
                + ", ".join([f"+`{e}`" for e in added] + [f"-`{e}`" for e in removed])
            )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", action="append", default=[], help="Docker tag (repeatable)")
    parser.add_argument("--all-v6", action="store_true", help="every v6 docker-pi-hole tag")
    parser.add_argument("--output", type=Path, default=FIXTURES, help="fixture directory")
    parser.add_argument("--runtime", help="podman or docker (default: auto)")
    parser.add_argument("--seed", type=int, default=6, help="traffic RNG seed")
    parser.add_argument(
        "--settle",
        type=float,
        default=65.0,
        help="seconds between traffic and recording (DB flush, ARP)",
    )
    parser.add_argument("--keep", action="store_true", help="keep containers for debugging")
    parser.add_argument("--report", action="store_true", help="print comparison tables and exit")
    args = parser.parse_args(argv)

    if args.report:
        sys.stdout.write(report(args.output))
        return 0
    tags = list(dict.fromkeys(args.tag + (list_v6_tags() if args.all_v6 else [])))
    if not tags:
        parser.error("give --tag TAG (repeatable) or --all-v6")

    def on_signal(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, on_signal)
    runtime = detect_runtime(args.runtime)
    lab = Lab(runtime=runtime, seed=args.seed, settle=args.settle, keep=args.keep)
    log(f"runtime: {runtime.name} ({runtime.executable}); tags: {', '.join(tags)}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        images = [f"docker.io/pihole/pihole:{t}" for t in tags]
        list(pool.map(runtime.ensure_image, images))
    failures: list[str] = []
    lab.ensure_network()
    try:
        for tag in tags:
            log(f"{tag}: recording")
            try:
                raw = record_tag(lab, tag)
                path = write_fixture(tag, raw, args.output)
                shown = path.relative_to(REPO) if path.is_relative_to(REPO) else path
                log(f"{tag}: wrote {shown} ({len(raw['responses'])} responses)")
            except (ContainerError, LeakError, OSError, KeyError, TypeError) as exc:
                log(f"{tag}: FAILED: {exc}\n{traceback.format_exc()}")
                failures.append(tag)
    finally:
        lab.teardown()
        update_index(args.output)
    if failures:
        log(f"failed tags: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
