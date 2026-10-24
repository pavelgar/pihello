"""Shared code of the integration tests (imported by ``conftest.py`` and the tests).

Run with ``uv run pytest -m integration tests/integration``. Environment:

``PIHELLO_IT_IMAGE``
    Full image reference (default: the newest tag in
    ``tests/fixtures/pihole/index.json``, from ``docker.io/pihole/pihole``).
``PIHELLO_IT_RUNTIME``
    ``podman`` / ``docker`` (see :func:`helpers.containers.detect_runtime`).
``PIHELLO_IT_PORTS``
    Host port range for the containers (default ``18400-18499``).
``PIHELLO_IT_URL`` / ``PIHELLO_IT_PASSWORD``
    Test an existing Pi-hole instead. Only read-only tests run; everything that
    needs container control or changes the Pi-hole's state is skipped.

Ground rules every test follows:

- pihello always runs as ``python -m pihello`` in a subprocess, with an
  isolated HOME/XDG environment (:class:`Pihello`).
- FTL rejects more than 3 password checks per wall-clock second, server-wide.
  Every login (pihello's and ours) goes through the target's
  :class:`LoginThrottle`.
- No test leaves a pihello session behind: the ``make_pihello`` fixture checks
  the Pi-hole's session list after each test, using one long-lived *observer*
  session so the check itself needs no extra logins.
"""

from __future__ import annotations

import collections
import contextlib
import functools
import json
import os
import random
import re
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from helpers import dnsgen
from helpers.containers import (
    DEFAULT_IMAGE,
    DEFAULT_PASSWORD,
    ApiResponse,
    ContainerError,
    PiholeContainer,
    api_request,
    detect_runtime,
    parse_port_range,
    runtime_available,
    split_image_ref,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

IMAGE_ENV = "PIHELLO_IT_IMAGE"
URL_ENV = "PIHELLO_IT_URL"
PASSWORD_ENV = "PIHELLO_IT_PASSWORD"
PORTS_ENV = "PIHELLO_IT_PORTS"
DEFAULT_PORTS = "18400-18499"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pihole"
#: User agent of our own helper sessions; must not look like pihello's ("pihello/…").
OBSERVER_AGENT = "integration-observer"
#: Containers never get update information: GitHub allows 60 anonymous API calls
#: per hour and every container start makes 4.
NO_GITHUB = ("--add-host", "api.github.com:127.0.0.1")


# --------------------------------------------------------------------------- #
# What to test                                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ItSettings:
    image: str
    tag: str
    url: str | None
    password: str | None
    ports: tuple[int, int]

    @property
    def external(self) -> bool:
        return self.url is not None

    @property
    def image_ref(self) -> str:
        return f"{self.image}:{self.tag}"


def _default_image() -> str:
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    tags = sorted((t for t in index if re.fullmatch(r"\d{4}\.\d{2}\.\d+", t)), key=_tag_key)
    return f"{DEFAULT_IMAGE}:{tags[-1]}"


def _tag_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.split("."))


def load_settings(env: Mapping[str, str] | None = None) -> ItSettings:
    environ = os.environ if env is None else env
    image, tag = split_image_ref(environ.get(IMAGE_ENV) or _default_image())
    url = environ.get(URL_ENV) or None
    password = environ.get(PASSWORD_ENV) if url else DEFAULT_PASSWORD
    ports = parse_port_range(environ.get(PORTS_ENV) or DEFAULT_PORTS)
    return ItSettings(image, tag, url.rstrip("/") if url else None, password or None, ports)


SETTINGS = load_settings()


def recorded_versions(tag: str) -> dict[str, str] | None:
    """Versions recorded for a Docker tag in ``tests/fixtures/pihole/index.json``."""
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    entry = index.get(tag)
    return entry if isinstance(entry, dict) else None


# --------------------------------------------------------------------------- #
# Versions                                                                     #
# --------------------------------------------------------------------------- #
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")


def parse_ftl(version: str | None) -> tuple[int, int, int]:
    """``"v6.7.1"`` → ``(6, 7, 1)``. The first v6 image reports ``vDev-eaa7dbb``,
    which is the v6.0 FTL (docs/compatibility.md), so unparseable versions are v6.0."""
    match = _VERSION_RE.fullmatch(version or "")
    if not match:
        return (6, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


_SINCE_RE = re.compile(r"\(FTL v(\d+)\.(\d+)\+")
_NULLABLE_RE = re.compile(r"\bnull (?:if|when|for|without)\b|\bmay be empty\b")


def version_gated() -> dict[str, tuple[int, int]]:
    """Catalog paths documented as "(FTL v6.N+)" → that version."""
    from pihello import sources

    gated = {}
    for info in sources.catalog():
        match = _SINCE_RE.search(info.description)
        if match:
            gated[info.path] = (int(match.group(1)), int(match.group(2)))
    return gated


def nullable() -> frozenset[str]:
    """Catalog paths documented as possibly null/empty ("null if unknown", "may be empty")."""
    from pihello import sources

    return frozenset(
        info.path for info in sources.catalog() if _NULLABLE_RE.search(info.description)
    )


def unavailable_paths(ftl: tuple[int, int, int]) -> frozenset[str]:
    """Paths that may be missing on this FTL version (by the catalog's own words)."""
    gated = {path for path, since in version_gated().items() if ftl[:2] < since}
    return frozenset(gated) | nullable()


# --------------------------------------------------------------------------- #
# Login rate limit                                                             #
# --------------------------------------------------------------------------- #
class LoginThrottle:
    """Keeps password checks against one Pi-hole below FTL's limit.

    FTL rejects the 4th password check within one wall-clock second (all
    clients together). ``with throttle(n):`` waits until ``n`` more logins fit
    into a sliding 1.1 s window, runs the block and books ``n`` logins at its
    end (a block's logins happen before it ends, so this errs on the safe side).
    """

    LIMIT = 3
    WINDOW = 1.1

    def __init__(self) -> None:
        self._stamps: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def __call__(self, logins: int = 1) -> Iterator[None]:
        if not 0 <= logins <= self.LIMIT:
            raise ValueError(f"cannot do {logins} logins in one block")
        while logins:
            with self._lock:
                now = time.monotonic()
                while self._stamps and self._stamps[0] <= now - self.WINDOW:
                    self._stamps.popleft()
                if len(self._stamps) + logins <= self.LIMIT:
                    break
                wait = self._stamps[0] + self.WINDOW - now
            time.sleep(max(wait, 0.01))
        try:
            yield
        finally:
            with self._lock:
                self._stamps.extend([time.monotonic()] * logins)


# --------------------------------------------------------------------------- #
# The Pi-hole under test                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class Target:
    """A Pi-hole under test: a container we control, or an external instance."""

    name: str
    url: str
    password: str | None
    container: PiholeContainer | None = None
    seeded: Seed | None = None
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    _observer: str | None = field(default=None, repr=False)
    _versions: dict[str, Any] | None = field(default=None, repr=False)

    @classmethod
    def for_container(cls, container: PiholeContainer) -> Target:
        url = container.https_url if container.prefer_https else container.base_url
        return cls(container.name, url, container.password, container)

    # -- container control ----------------------------------------------------
    def require_container(self) -> PiholeContainer:
        if self.container is None:
            pytest.skip("needs container control (testing an external Pi-hole)")
        return self.container

    @property
    def https_url(self) -> str:
        return self.require_container().https_url

    @property
    def dns_address(self) -> tuple[str, int]:
        return self.require_container().dns_address

    # -- API ------------------------------------------------------------------
    def api(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        sid: str | None = None,
        timeout: float = 15.0,
    ) -> ApiResponse:
        context = None
        if self.url.startswith("https://"):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return api_request(
            self.url,
            method,
            path,
            body=body,
            sid=sid,
            timeout=timeout,
            context=context,
            user_agent=OBSERVER_AGENT,
        )

    def login(self, password: str | None = None, *, totp: int | None = None) -> str | None:
        """Log in (throttled); ``None`` for a password-less Pi-hole. Raises on failure."""
        payload: dict[str, Any] = {"password": self.password if password is None else password}
        if totp is not None:
            payload["totp"] = totp
        with self.throttle():
            resp = self.api("POST", "auth", body=payload)
        if resp.status != 200 or not isinstance(resp.body, dict):
            raise ContainerError(f"{self.name}: login failed: {resp.status} {resp.text[:200]}")
        sid = (resp.body.get("session") or {}).get("sid")
        return sid if isinstance(sid, str) and sid else None

    def logout(self, sid: str | None) -> None:
        if sid:
            with contextlib.suppress(OSError):
                self.api("DELETE", "auth", sid=sid, timeout=5)

    @contextlib.contextmanager
    def session(self, password: str | None = None) -> Iterator[str | None]:
        sid = self.login(password)
        try:
            yield sid
        finally:
            self.logout(sid)

    def observe(self, method: str, path: str, *, body: Any = None) -> ApiResponse:
        """A request with the long-lived observer session (logged in again on 401)."""
        if not self.password:
            return self.api(method, path, body=body)
        if self._observer is None:
            self._observer = self.login()
        resp = self.api(method, path, body=body, sid=self._observer)
        if resp.status == 401:  # sessions were invalidated (app password, 2FA change)
            self._observer = self.login()
            resp = self.api(method, path, body=body, sid=self._observer)
        return resp

    def get(self, path: str) -> Any:
        resp = self.observe("GET", path)
        assert resp.status == 200, f"GET {path}: {resp.status} {resp.text[:300]}"
        return resp.body

    def sessions(self) -> list[dict[str, Any]]:
        return list(self.get("auth/sessions")["sessions"])

    def pihello_sessions(self) -> list[dict[str, Any]]:
        """Sessions opened by pihello (user agent ``pihello/<version>…``)."""
        return [s for s in self.sessions() if str(s.get("user_agent") or "").startswith("pihello/")]

    def delete_session(self, session_id: int) -> None:
        resp = self.observe("DELETE", f"auth/session/{session_id}")
        assert resp.status in (200, 204, 404), resp.text

    def close(self) -> None:
        if self._observer:
            self.logout(self._observer)
            self._observer = None

    def versions(self) -> dict[str, Any]:
        """``info/version``'s ``version`` object (cached)."""
        if self._versions is None:
            self._versions = dict(self.get("info/version")["version"])
        return self._versions

    @property
    def ftl(self) -> tuple[int, int, int]:
        return parse_ftl(self.versions()["ftl"]["local"]["version"])

    def set_blocking(self, enabled: bool, timer: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"blocking": enabled, "timer": timer}
        resp = self.observe("POST", "dns/blocking", body=body)
        assert resp.ok, resp.text
        return dict(resp.body)


# --------------------------------------------------------------------------- #
# Containers                                                                   #
# --------------------------------------------------------------------------- #
def container_runtime_or_skip() -> None:
    if SETTINGS.external:
        pytest.skip("needs container control (testing an external Pi-hole)")
    if not runtime_available():
        pytest.skip("no container runtime (podman or docker) found")


@functools.cache
def upstreams() -> str:
    """``FTLCONF_dns_upstreams`` value: the resolver(s) the runtime gives containers.

    Pi-hole's default upstreams (8.8.8.8, 8.8.4.4) are not reachable from every
    network; forwarded test queries would then time out.
    """
    servers = detect_runtime().container_nameservers(SETTINGS.image_ref)
    return ";".join(servers) or "8.8.8.8;8.8.4.4"


def start_target(
    purpose: str,
    *,
    password: str | None = DEFAULT_PASSWORD,
    env: Mapping[str, str] | None = None,
    https: bool = False,
    wait_versions: bool = True,
) -> Target:
    """Start a fresh container for ``SETTINGS.image``; the caller stops it.

    ``https``: talk to it over HTTPS only (``Target.url`` is the HTTPS URL).
    """
    container_runtime_or_skip()
    tag_label = re.sub(r"[^A-Za-z0-9_.-]", "-", SETTINGS.tag.split("@", 1)[0])
    container = PiholeContainer(
        SETTINGS.tag,
        image=SETTINGS.image,
        password=password,
        name=f"pihello-it-{purpose}-{tag_label}-{os.getpid()}-{random.randrange(16**4):04x}",
        runtime=detect_runtime(),
        env={"FTLCONF_dns_upstreams": upstreams(), **(env or {})},
        port_range=SETTINGS.ports,
        extra_args=NO_GITHUB,
        prefer_https=https,
    )
    container.start(timeout=240)
    try:
        if wait_versions:
            container.wait_versions(timeout=90)
    except BaseException:
        container.stop()
        raise
    return Target.for_container(container)


def stop_target(target: Target) -> None:
    target.close()
    if target.container is not None:
        target.container.stop()


@contextlib.contextmanager
def running_target(purpose: str, **kwargs: Any) -> Iterator[Target]:
    target = start_target(purpose, **kwargs)
    try:
        yield target
    finally:
        stop_target(target)


# --------------------------------------------------------------------------- #
# Seed data                                                                    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Seed:
    """What :func:`seed` put into the Pi-hole."""

    deny_exact: tuple[str, ...] = ("ads.pihello.test", "tracker.pihello.test")
    deny_regex: tuple[str, ...] = (r"(^|\.)telemetry\.pihello\.test$",)
    allow_exact: tuple[str, ...] = ("allowed.pihello.test",)
    regex_hits: tuple[str, ...] = ("telemetry.pihello.test", "eu.telemetry.pihello.test")
    gravity_hits: tuple[str, ...] = ()
    queries: int = 0
    blocked: int = 0
    qtypes: tuple[str, ...] = ()

    @property
    def blocked_domains(self) -> frozenset[str]:
        return frozenset((*self.deny_exact, *self.regex_hits, *self.gravity_hits))


ALLOWED_DOMAINS = ("example.com", "example.org", "iana.org", "pi-hole.net", "github.com")
ALLOWED_TYPES = ("A", "AAAA", "MX", "TXT", "HTTPS", "NS", "SOA")


def seed(target: Target) -> Seed:
    """Deny/allow list entries plus a few hundred DNS queries of several types."""
    plan = Seed()
    container = target.require_container()
    for kind, domains in (
        ("deny/exact", plan.deny_exact),
        ("deny/regex", plan.deny_regex),
        ("allow/exact", plan.allow_exact),
    ):
        for domain in domains:
            resp = target.observe(
                "POST", f"domains/{kind}", body={"domain": domain, "comment": "pihello it"}
            )
            assert resp.status == 201, f"POST domains/{kind}: {resp.status} {resp.text[:300]}"
    gravity = container.exec(
        "pihole-FTL",
        "sqlite3",
        "/etc/pihole/gravity.db",
        "SELECT domain FROM gravity WHERE domain GLOB '[a-z]*.[a-z]*' ORDER BY domain LIMIT 2",
    ).stdout.split()
    assert len(gravity) == 2, f"no gravity domains: {gravity}"
    host, port = target.dns_address
    blocked = (*plan.deny_exact, *plan.regex_hits, *gravity)
    probes, blocked_probes = _await_lists(plan, host, port)
    batch: list[tuple[str, str]] = []
    for _ in range(3):
        batch += [(name, qtype) for name in blocked for qtype in ("A", "AAAA")]
    for _ in range(2):
        batch += [(name, qtype) for name in ALLOWED_DOMAINS for qtype in ALLOWED_TYPES]
    batch += [(name, "A") for name in plan.allow_exact]
    batch += [("pi.hole", "A"), (dnsgen.reverse_name("127.0.0.1"), "PTR")]
    batch += [(f"nx{i}.pihello.invalid", "A") for i in range(3)]
    random.Random(6).shuffle(batch)
    replies = dnsgen.query_many(batch, server=host, port=port, timeout=3.0, retries=1)
    answered = [r for r in replies if isinstance(r, dnsgen.DnsResponse)]
    assert len(answered) >= len(batch) * 0.9, f"only {len(answered)}/{len(batch)} queries answered"
    return Seed(
        gravity_hits=tuple(gravity),
        queries=len(answered) + probes,
        blocked=sum(1 for name, _ in batch if name in blocked) + blocked_probes,
        qtypes=("A", "AAAA", *ALLOWED_TYPES[2:], "PTR"),
    )


def _await_lists(plan: Seed, host: str, port: int) -> tuple[int, int]:
    """Wait until FTL applied the new list entries (it reloads them asynchronously).

    Returns the number of probe queries sent and how many of them were blocked.
    """
    probes = blocked = 0
    deadline = time.monotonic() + 20
    for name in (*plan.deny_exact, plan.regex_hits[0]):
        while True:
            probes += 1
            reply = dnsgen.query(name, "A", server=host, port=port, timeout=2.0, retries=0)
            if reply.is_blocked_null:
                blocked += 1
                break
            assert time.monotonic() < deadline, f"{name} is still not blocked"
            time.sleep(0.2)
    return probes, blocked


# --------------------------------------------------------------------------- #
# Running pihello                                                              #
# --------------------------------------------------------------------------- #
@dataclass(repr=False)
class Run:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float

    def __repr__(self) -> str:  # pytest shows the repr in assertion messages
        return (
            f"pihello {' '.join(self.args)} -> exit {self.returncode} in {self.duration:.2f}s\n"
            f"--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"
        )

    @property
    def lines(self) -> list[str]:
        return self.stdout.rstrip("\n").split("\n")


def base_env(home: Path) -> dict[str, str]:
    """A clean environment: no PIHELLO_*/color variables, HOME and XDG dirs in ``home``."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "TZ": "UTC",
    }
    if sys.platform == "win32":  # pragma: no cover
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    return env


class Pihello:
    """Runs ``python -m pihello`` against a :class:`Target` with an isolated HOME."""

    def __init__(self, home: Path, target: Target | None) -> None:
        self.home = home
        self.target = target
        self.argv0 = [sys.executable, "-m", "pihello"]
        self.env = base_env(home)
        if target is not None:
            self.env["PIHELLO_URL"] = target.url
            if target.password:
                self.env["PIHELLO_PASSWORD"] = target.password

    def command(self, *args: str) -> list[str]:
        return [*self.argv0, *args]

    def environment(self, env: Mapping[str, str | None] | None = None) -> dict[str, str]:
        merged: dict[str, str | None] = {**self.env, **(env or {})}
        return {key: value for key, value in merged.items() if value is not None}

    def default_logins(self) -> int:
        return 1 if self.target is not None and self.target.password else 0

    def __call__(
        self,
        *args: str,
        env: Mapping[str, str | None] | None = None,
        input: str | None = None,
        timeout: float = 60.0,
        logins: int | None = None,
        prefix: tuple[str, ...] = (),
    ) -> Run:
        throttle = self.target.throttle if self.target is not None else LoginThrottle()
        count = self.default_logins() if logins is None else logins
        with throttle(count):
            started = time.perf_counter()
            proc = subprocess.run(
                [*prefix, *self.command(*args)],
                env=self.environment(env),
                cwd=self.home,
                input=input,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                check=False,
            )
            duration = time.perf_counter() - started
        return Run(list(args), proc.returncode, proc.stdout, proc.stderr, duration)

    def popen(
        self, *args: str, env: Mapping[str, str | None] | None = None, **kwargs: Any
    ) -> subprocess.Popen[bytes]:
        """Start pihello without waiting (the caller throttles and cleans up)."""
        return subprocess.Popen(
            self.command(*args), env=self.environment(env), cwd=self.home, **kwargs
        )


def run_together(
    runners: list[Pihello], *args: str, env: Mapping[str, str | None] | None = None
) -> list[Run]:
    """Start one pihello per runner at the same moment (no throttling!); wait for all."""
    import concurrent.futures

    barrier = threading.Barrier(len(runners))

    def one(runner: Pihello) -> Run:
        barrier.wait()
        started = time.perf_counter()
        proc = subprocess.run(
            runner.command(*args),
            env=runner.environment(env),
            cwd=runner.home,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        return Run(
            list(args), proc.returncode, proc.stdout, proc.stderr, time.perf_counter() - started
        )

    with concurrent.futures.ThreadPoolExecutor(len(runners)) as pool:
        return list(pool.map(one, runners))


def assert_no_pihello_sessions(target: Target) -> None:
    leftover = target.pihello_sessions()
    assert not leftover, "pihello left sessions open: " + ", ".join(
        f"#{s.get('id')} {s.get('user_agent')}" for s in leftover
    )


#: Lines for the terminal summary (performance numbers, skipped checks).
NOTES: list[str] = []
