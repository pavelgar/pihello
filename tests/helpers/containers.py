"""Start, drive and stop real Pi-hole containers with podman or docker.

Used by ``scripts/record_fixtures.py`` and the integration tests. Only the
standard library is imported (plus the sibling :mod:`helpers.dnsgen`).

Quick start::

    from helpers.containers import PiholeContainer

    with PiholeContainer("2026.10.0") as pihole:  # starts, waits until ready
        sid = pihole.login()
        print(pihole.api("GET", "info/version", sid=sid).body)
        pihole.logout(sid)
    # container removed here (also on exceptions and, via atexit, on crashes)

Runtime selection: the ``runtime`` argument, else ``$PIHELLO_IT_RUNTIME``
(``podman`` / ``docker`` or a path to either), else the first of ``podman``,
``docker`` found on ``PATH``.

Ports: unless given explicitly, free host ports are picked from the
container's ``port_range`` (default :data:`PORT_RANGE`, 18100-18199) and bound
to ``127.0.0.1``. A port collision at start-up (another worker grabbed it) is
retried with new ports. :func:`parse_port_range` reads ranges such as
``"18400-18499"`` (e.g. from an environment variable).

Images: ``image`` + ``tag``; :func:`split_image_ref` splits a full reference
such as ``docker.io/pihole/pihole:2026.10.0`` into the two.

Passwords: ``password="pihello-test"`` (default) sets
``FTLCONF_webserver_api_password``; ``password=""`` (:data:`NO_PASSWORD`)
starts a Pi-hole without authentication; ``password=None`` leaves the
variable unset so the image generates a random password (you cannot log in).

Session hygiene: Pi-hole has a small session limit
(``webserver.api.max_sessions``, default 16). Always pair :meth:`login` with
:meth:`logout`, or use :meth:`session`. Note that changing
``webserver.api.app_pwhash`` or ``webserver.api.totp_secret`` invalidates every
existing session (see ``docs/compatibility.md``).
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import hashlib
import hmac
import json
import os
import random
import re
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import dnsgen

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "DEFAULT_IMAGE",
    "DEFAULT_PASSWORD",
    "NO_PASSWORD",
    "PORT_RANGE",
    "RUNTIME_ENV",
    "ApiResponse",
    "ContainerError",
    "PiholeContainer",
    "Runtime",
    "api_request",
    "detect_runtime",
    "find_free_ports",
    "is_port_free",
    "parse_port_range",
    "runtime_available",
    "split_image_ref",
    "totp_code",
]

#: Environment variable that forces the container runtime (``podman``/``docker``/path).
RUNTIME_ENV = "PIHELLO_IT_RUNTIME"
#: Image repository; the tag is supplied per container.
DEFAULT_IMAGE = "docker.io/pihole/pihole"
#: Web/API password used by default for test containers.
DEFAULT_PASSWORD = "pihello-test"
#: Pass as ``password`` to start a Pi-hole without authentication.
NO_PASSWORD = ""
#: Host port range (start inclusive, stop exclusive) used for automatic port selection.
PORT_RANGE = (18100, 18200)
#: User agent sent by :func:`api_request` unless overridden.
USER_AGENT = "pihello-recorder"

_RUNTIMES = ("podman", "docker")
_PORT_CONFLICT = re.compile(
    r"address already in use|port is already allocated|bind.*(?:in use|failed)|"
    r"rootlessport.*listen",
    re.IGNORECASE,
)


class ContainerError(RuntimeError):
    """A container operation failed. ``logs`` holds the tail of the container log when known."""

    def __init__(self, message: str, *, logs: str | None = None) -> None:
        if logs:
            message = f"{message}\n--- container log (tail) ---\n{logs}"
        super().__init__(message)
        self.logs = logs


# --------------------------------------------------------------------------- #
# Runtime                                                                      #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Runtime:
    """A container runtime CLI (``podman`` or ``docker``)."""

    name: str
    """``"podman"`` or ``"docker"``."""
    executable: str
    """Absolute path of the CLI."""

    def run(
        self,
        *args: str,
        check: bool = True,
        input: str | None = None,
        timeout: float = 300.0,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``<runtime> *args`` capturing text output.

        Raises :class:`ContainerError` (with stderr) when ``check`` and the exit
        status is non-zero, or when the command times out.
        """
        try:
            proc = subprocess.run(
                [self.executable, *args],
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContainerError(f"{self.name} {' '.join(args[:3])}: timed out") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise ContainerError(
                f"{self.name} {' '.join(args[:3])} failed ({proc.returncode}): {detail}"
            )
        return proc

    def image_present(self, image: str) -> bool:
        """Whether ``image`` exists locally."""
        return self.run("image", "inspect", image, check=False).returncode == 0

    def pull(self, image: str, *, timeout: float = 900.0) -> None:
        """Pull ``image`` (raises :class:`ContainerError` on failure)."""
        self.run("pull", "-q", image, timeout=timeout)

    def ensure_image(self, image: str) -> None:
        """Pull ``image`` unless it is already present."""
        if not self.image_present(image):
            self.pull(image)

    def container_running(self, name: str) -> bool:
        """Whether a container called ``name`` exists and is running."""
        proc = self.run("inspect", "-f", "{{.State.Running}}", name, check=False)
        return proc.returncode == 0 and proc.stdout.strip().lower() == "true"

    def remove_container(self, name: str) -> None:
        """Force-remove a container (with its anonymous volumes); never raises."""
        with contextlib.suppress(ContainerError):
            self.run("rm", "-f", "-v", name, check=False, timeout=60)

    def container_nameservers(self, image: str) -> list[str]:
        """The DNS servers a container started from ``image`` gets (from its resolv.conf).

        Useful as Pi-hole's upstream: the runtime's resolver is reachable from
        the container even where public resolvers such as 8.8.8.8 are not.
        Loopback and scoped link-local addresses are left out.
        """
        proc = self.run(
            "run", "--rm", "--entrypoint", "cat", image, "/etc/resolv.conf", timeout=120
        )
        servers = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                address = parts[1]
                if "%" in address or address.startswith(("127.", "::1")):
                    continue
                servers.append(address)
        return servers

    def network_exists(self, name: str) -> bool:
        """Whether a network called ``name`` exists."""
        return self.run("network", "inspect", name, check=False).returncode == 0

    def create_network(
        self,
        name: str,
        *,
        subnet: str | None = None,
        gateway: str | None = None,
        ipv6_subnet: str | None = None,
        ipv6_gateway: str | None = None,
    ) -> None:
        """Create a bridge network (optionally dual-stack with fixed subnets)."""
        args = ["network", "create"]
        if subnet:
            args += ["--subnet", subnet]
            if gateway:
                args += ["--gateway", gateway]
        if ipv6_subnet:
            args += ["--ipv6", "--subnet", ipv6_subnet]
            if ipv6_gateway:
                args += ["--gateway", ipv6_gateway]
        self.run(*args, name)

    def remove_network(self, name: str) -> None:
        """Remove a network; never raises."""
        with contextlib.suppress(ContainerError):
            self.run("network", "rm", "-f", name, check=False, timeout=60)


def _resolve_runtime(candidate: str) -> Runtime | None:
    path = shutil.which(candidate)
    if path is None:
        return None
    base = Path(path).name.lower()
    name = "docker" if "docker" in base else "podman"
    return Runtime(name=name, executable=path)


def detect_runtime(
    preferred: str | None = None, *, env: Mapping[str, str] | None = None
) -> Runtime:
    """Find a container runtime.

    Order: ``preferred`` → ``$PIHELLO_IT_RUNTIME`` → ``podman`` → ``docker``.
    Raises :class:`ContainerError` when none is usable.
    """
    environ = os.environ if env is None else env
    forced = preferred or environ.get(RUNTIME_ENV) or None
    candidates = [forced] if forced else list(_RUNTIMES)
    for candidate in candidates:
        runtime = _resolve_runtime(candidate)
        if runtime is not None:
            return runtime
    wanted = forced or " or ".join(_RUNTIMES)
    raise ContainerError(f"no container runtime found ({wanted} not on PATH)")


def runtime_available(preferred: str | None = None) -> bool:
    """Whether :func:`detect_runtime` would succeed."""
    try:
        detect_runtime(preferred)
    except ContainerError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Ports                                                                        #
# --------------------------------------------------------------------------- #
def is_port_free(port: int, *, host: str = "127.0.0.1", udp: bool = False) -> bool:
    """Whether ``port`` can currently be bound on ``host`` (TCP, or UDP when ``udp``)."""
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, kind) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


_reserved_ports: set[int] = set()
_reserved_lock = threading.Lock()


def parse_port_range(text: str) -> tuple[int, int]:
    """Parse ``"START-END"`` (both inclusive) into a :data:`PORT_RANGE`-style tuple.

    Raises :class:`ValueError` for anything else or ports outside 1-65535.
    """
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", text)
    if not match:
        raise ValueError(f"expected a port range like 18400-18499, got {text!r}")
    start, end = int(match.group(1)), int(match.group(2))
    if not 0 < start <= end < 65536:
        raise ValueError(f"invalid port range {text!r}")
    return start, end + 1


def find_free_ports(
    count: int,
    *,
    host: str = "127.0.0.1",
    port_range: tuple[int, int] = PORT_RANGE,
    exclude: Sequence[int] = (),
) -> list[int]:
    """Pick ``count`` distinct ports in ``port_range`` free for both TCP and UDP.

    Ports handed out by this process are remembered and not handed out again,
    so concurrent callers in one process do not collide. The scan starts at a
    random offset to make collisions between processes unlikely.
    """
    start, stop = port_range
    candidates = list(range(start, stop))
    offset = random.randrange(len(candidates))
    candidates = candidates[offset:] + candidates[:offset]
    picked: list[int] = []
    with _reserved_lock:
        for port in candidates:
            if port in _reserved_ports or port in exclude:
                continue
            if is_port_free(port, host=host) and is_port_free(port, host=host, udp=True):
                picked.append(port)
                if len(picked) == count:
                    _reserved_ports.update(picked)
                    return picked
    raise ContainerError(f"not enough free ports in {start}-{stop - 1} (need {count})")


# --------------------------------------------------------------------------- #
# HTTP API                                                                     #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ApiResponse:
    """A Pi-hole API response."""

    status: int
    body: Any
    """Parsed JSON, or ``None`` for an empty body / non-JSON body."""
    headers: Mapping[str, str]
    text: str
    """The raw response text."""

    @property
    def ok(self) -> bool:
        """``True`` for 2xx statuses."""
        return 200 <= self.status < 300


def _api_path(path: str) -> str:
    path = path.lstrip("/")
    if not path.startswith("api/") and path != "api":
        path = "api/" + path
    return "/" + path


def api_request(
    base_url: str,
    method: str,
    path: str,
    *,
    body: Any = None,
    sid: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
    context: ssl.SSLContext | None = None,
    user_agent: str = USER_AGENT,
) -> ApiResponse:
    """Send one request to ``base_url`` + ``/api/<path>`` and return the response.

    ``path`` may be given with or without the ``/api/`` prefix and may contain
    a query string. ``sid`` is sent in the ``X-FTL-SID`` header. HTTP error
    statuses are returned, not raised; network failures raise ``OSError``.
    Redirects are not followed (they are returned like any other status):
    Pi-hole's HTTP→HTTPS redirect points at ``https://pi.hole/…``, which may
    well be a different Pi-hole.
    """
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    if sid:
        all_headers["X-FTL-SID"] = sid
    all_headers.update(headers or {})
    request = urllib.request.Request(
        base_url.rstrip("/") + _api_path(path), data=data, method=method, headers=all_headers
    )
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as resp:
            status, raw, resp_headers = resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        status, raw, resp_headers = exc.code, exc.read(), dict(exc.headers)
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        parsed = None
    return ApiResponse(status=status, body=parsed, headers=resp_headers, text=text)


def split_image_ref(ref: str, *, default_tag: str = "latest") -> tuple[str, str]:
    """Split ``registry/repo:tag`` into ``(image, tag)``.

    A ``:`` before the last ``/`` belongs to a registry port
    (``localhost:5000/pihole``); a digest (``@sha256:…``) is kept with the tag
    (``"2026.10.0@sha256:…"``), so ``f"{image}:{tag}"`` is the original reference.
    Without a tag, ``default_tag`` is used.
    """
    ref = ref.strip()
    name, at, digest = ref.partition("@")
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        image, tag = name[:colon], name[colon + 1 :]
    else:
        image, tag = name, default_tag
    if not image or not tag:
        raise ValueError(f"invalid image reference {ref!r}")
    return image, tag + (at + digest if at else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def totp_code(secret: str, *, at: float | None = None, digits: int = 6, period: int = 30) -> int:
    """RFC 6238 TOTP (HMAC-SHA1) for a base32 ``secret`` at time ``at`` (default: now)."""
    padded = secret.strip().replace(" ", "").upper()
    padded += "=" * (-len(padded) % 8)
    key = base64.b32decode(padded)
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return int(value % (10**digits))


# --------------------------------------------------------------------------- #
# Cleanup of containers left behind by crashes                                 #
# --------------------------------------------------------------------------- #
_live: dict[str, Runtime] = {}
_live_lock = threading.Lock()


def _cleanup_live() -> None:
    with _live_lock:
        items = list(_live.items())
        _live.clear()
    for name, runtime in items:
        runtime.remove_container(name)


atexit.register(_cleanup_live)


# --------------------------------------------------------------------------- #
# Pi-hole container                                                            #
# --------------------------------------------------------------------------- #
def _default_name(tag: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", tag)
    return f"pihello-it-{safe}-{os.getpid()}-{random.randrange(16**4):04x}"


@dataclass
class PiholeContainer:
    """A Pi-hole container with published HTTP, HTTPS and DNS ports.

    All ports default to automatically chosen free ports on ``host``. Set
    ``publish_https`` / ``publish_dns`` to ``False`` to skip those.
    ``network``/``ip``/``ip6``/``mac_address`` attach the container to a
    specific network with fixed addresses (see :meth:`Runtime.create_network`).
    ``env`` is merged over the defaults ``TZ=UTC`` and
    ``FTLCONF_dns_listeningMode=ALL`` (required so queries arriving through
    the port forward are answered).
    """

    tag: str = "latest"
    password: str | None = DEFAULT_PASSWORD
    name: str = ""
    runtime: Runtime | str | None = None
    image: str = DEFAULT_IMAGE
    env: Mapping[str, str] = field(default_factory=dict)
    host: str = "127.0.0.1"
    http_port: int | None = None
    https_port: int | None = None
    dns_port: int | None = None
    publish_https: bool = True
    publish_dns: bool = True
    hostname: str | None = "pihole"
    network: str | None = None
    ip: str | None = None
    ip6: str | None = None
    mac_address: str | None = None
    extra_args: Sequence[str] = ()
    prefer_https: bool = False
    """Send helper API requests (readiness checks, :meth:`login`, …) over HTTPS,
    e.g. when ``webserver.port`` makes the HTTP port redirect to HTTPS."""
    port_range: tuple[int, int] = PORT_RANGE
    """Range (start inclusive, stop exclusive) for automatically picked host ports."""
    keep: bool = False
    """Do not remove the container on :meth:`stop` / exit (for debugging)."""
    _started: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = _default_name(self.tag)
        if not isinstance(self.runtime, Runtime):
            self.runtime = detect_runtime(self.runtime)

    # -- properties --------------------------------------------------------- #
    @property
    def rt(self) -> Runtime:
        """The resolved :class:`Runtime`."""
        assert isinstance(self.runtime, Runtime)
        return self.runtime

    @property
    def image_ref(self) -> str:
        """``image:tag``."""
        return f"{self.image}:{self.tag}"

    @property
    def base_url(self) -> str:
        """``http://<host>:<http_port>`` (no ``/api``)."""
        return f"http://{self._url_host}:{self._need(self.http_port, 'http')}"

    @property
    def https_url(self) -> str:
        """``https://<host>:<https_port>``."""
        return f"https://{self._url_host}:{self._need(self.https_port, 'https')}"

    @property
    def api_url(self) -> str:
        """``base_url + "/api"``."""
        return self.base_url + "/api"

    @property
    def dns_address(self) -> tuple[str, int]:
        """``(host, port)`` of the published DNS port (UDP and TCP)."""
        return self.host, self._need(self.dns_port, "dns")

    @property
    def _url_host(self) -> str:
        return f"[{self.host}]" if ":" in self.host else self.host

    @staticmethod
    def _need(port: int | None, what: str) -> int:
        if port is None:
            raise ContainerError(f"{what} port is not published (container not started?)")
        return port

    # -- lifecycle ---------------------------------------------------------- #
    def _environment(self) -> dict[str, str]:
        env = {"TZ": "UTC", "FTLCONF_dns_listeningMode": "ALL"}
        if self.password is not None:
            env["FTLCONF_webserver_api_password"] = self.password
        env.update(self.env)
        return env

    def _run_args(self) -> list[str]:
        args = ["run", "-d", "--name", self.name]
        if self.hostname:
            args += ["--hostname", self.hostname]
        if self.network:
            args += ["--network", self.network]
        if self.ip:
            args += ["--ip", self.ip]
        if self.ip6:
            args += ["--ip6", self.ip6]
        if self.mac_address:
            args += ["--mac-address", self.mac_address]
        args += ["-p", f"{self.host}:{self.http_port}:80/tcp"]
        if self.publish_https:
            args += ["-p", f"{self.host}:{self.https_port}:443/tcp"]
        if self.publish_dns:
            args += ["-p", f"{self.host}:{self.dns_port}:53/udp"]
            args += ["-p", f"{self.host}:{self.dns_port}:53/tcp"]
        for key, value in self._environment().items():
            args += ["-e", f"{key}={value}"]
        args += list(self.extra_args)
        args.append(self.image_ref)
        return args

    def _assign_ports(self, fixed: Mapping[str, int | None]) -> None:
        enabled = {"http": True, "https": self.publish_https, "dns": self.publish_dns}
        wanted = [k for k, on in enabled.items() if on and fixed[k] is None]
        taken = [p for p in fixed.values() if p is not None]
        fresh = iter(
            find_free_ports(len(wanted), host=self.host, port_range=self.port_range, exclude=taken)
            if wanted
            else []
        )
        self.http_port = next(fresh) if "http" in wanted else fixed["http"]
        self.https_port = next(fresh) if "https" in wanted else fixed["https"]
        self.dns_port = next(fresh) if "dns" in wanted else fixed["dns"]

    def start(
        self, *, wait: bool = True, timeout: float = 180.0, attempts: int = 5
    ) -> PiholeContainer:
        """Create and start the container; by default block until :meth:`wait_ready`.

        A stale container with the same name is removed first. Port collisions
        are retried with freshly picked ports (only for auto-picked ports).
        """
        fixed = {"http": self.http_port, "https": self.https_port, "dns": self.dns_port}
        self.rt.ensure_image(self.image_ref)
        self.rt.remove_container(self.name)
        for attempt in range(attempts):
            self._assign_ports(fixed)
            proc = self.rt.run(*self._run_args(), check=False)
            if proc.returncode == 0:
                break
            self.rt.remove_container(self.name)
            message = (proc.stderr or proc.stdout).strip()
            auto = any(v is None for v in fixed.values())
            if not (auto and _PORT_CONFLICT.search(message)) or attempt == attempts - 1:
                raise ContainerError(f"cannot start {self.name}: {message}")
        if not self.keep:  # kept containers must survive the atexit safety net
            with _live_lock:
                _live[self.name] = self.rt
        self._started = True
        if wait:
            try:
                self.wait_ready(timeout=timeout)
            except BaseException:
                self.stop()
                raise
        return self

    def stop(self) -> None:
        """Remove the container (unless ``keep``); never raises."""
        with _live_lock:
            _live.pop(self.name, None)
        if not self.keep:
            self.rt.remove_container(self.name)
        self._started = False

    def __enter__(self) -> PiholeContainer:
        if not self._started:
            self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def running(self) -> bool:
        """Whether the container is running."""
        return self.rt.container_running(self.name)

    def pause(self) -> None:
        """Freeze every process in the container (the published ports stay open)."""
        self.rt.run("pause", self.name, timeout=60)

    def unpause(self) -> None:
        """Undo :meth:`pause`."""
        self.rt.run("unpause", self.name, timeout=60)

    def logs(self, tail: int | None = 60) -> str:
        """Container log (stdout and stderr interleaved), last ``tail`` lines."""
        args = ["logs"] + (["--tail", str(tail)] if tail else []) + [self.name]
        proc = self.rt.run(*args, check=False)
        return (proc.stdout + proc.stderr).strip()

    def wait_ready(self, *, timeout: float = 180.0, gravity: bool = True, dns: bool = True) -> None:
        """Block until the API answers, gravity is built and DNS answers.

        ``gravity`` waits for ``info/ftl`` ``database.gravity > 0`` (needs a known
        password or no password; skipped when ``password`` is ``None``). ``dns``
        waits for an answer on the published DNS port. Raises
        :class:`ContainerError` (with logs) if the container exits or times out.
        """
        deadline = time.monotonic() + timeout
        stage = "API"
        last_error = ""

        def check_alive() -> None:
            if not self.running():
                raise ContainerError(f"{self.name} exited during start-up", logs=self.logs())

        while True:
            if time.monotonic() > deadline:
                detail = f" (last error: {last_error})" if last_error else ""
                raise ContainerError(
                    f"{self.name}: timed out waiting for {stage}{detail}", logs=self.logs()
                )
            try:
                if stage == "API":
                    resp = self.api("GET", "info/login", timeout=3)
                    if resp.status == 200 and isinstance(resp.body, dict):
                        stage = "gravity" if gravity and self.password is not None else "DNS"
                        continue
                elif stage == "gravity":
                    if self._gravity_size() > 0:
                        stage = "DNS"
                        continue
                elif stage == "DNS":
                    if not (dns and self.publish_dns):
                        return
                    host, port = self.dns_address
                    dnsgen.query("pi.hole", "A", server=host, port=port, timeout=1.0, retries=0)
                    return
            except (OSError, dnsgen.DnsQueryError, ValueError, ContainerError) as exc:
                # ContainerError: a login during the gravity stage failed
                # (e.g. transient rate limiting); retry until the deadline.
                last_error = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            check_alive()
            time.sleep(0.5)

    def wait_versions(self, *, timeout: float = 60.0) -> dict[str, Any]:
        """Block until ``/etc/pihole/versions`` is complete; return ``info/version``'s ``version``.

        Right after start-up FTL answers ``500`` (no file yet) or a partial
        file, which the image's update checker fills in line by line. The file
        is complete once its last entry, ``GITHUB_DOCKER_VERSION``, is there
        (its value may be empty or ``null`` when GitHub is unreachable; FTL
        reports an empty value as ``""`` or ``null`` depending on the version).
        Needs a known password (or none). Raises :class:`ContainerError` on timeout.
        """
        deadline = time.monotonic() + timeout
        last = "no reply"
        while True:
            try:
                with self.session() as sid:
                    resp = self.api("GET", "info/version", sid=sid, timeout=5)
                version = resp.body.get("version") if isinstance(resp.body, dict) else None
                if resp.status == 200 and isinstance(version, dict):
                    if _versions_complete(version) and self._versions_file_complete():
                        return version
                    last = "partial versions file"
                else:
                    last = f"HTTP {resp.status}"
            except (OSError, ContainerError) as exc:
                last = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            if time.monotonic() > deadline:
                raise ContainerError(
                    f"{self.name}: info/version still incomplete ({last})", logs=self.logs()
                )
            time.sleep(0.5)

    def _versions_file_complete(self) -> bool:
        proc = self.exec("cat", "/etc/pihole/versions", check=False)
        return proc.returncode == 0 and bool(
            re.search(r"^GITHUB_DOCKER_VERSION=", proc.stdout, re.M)
        )

    def _gravity_size(self) -> int:
        with self.session() as sid:
            resp = self.api("GET", "info/ftl", sid=sid, timeout=5)
        try:
            return int(resp.body["ftl"]["database"]["gravity"])
        except (KeyError, TypeError, ValueError):
            return 0

    # -- container access --------------------------------------------------- #
    def exec(
        self,
        *cmd: str,
        check: bool = True,
        input: str | None = None,
        user: str | None = None,
        timeout: float = 120.0,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command inside the container (``<runtime> exec [-i] [-u user] name cmd``)."""
        args = ["exec"]
        if input is not None:
            args.append("-i")
        if user:
            args += ["-u", user]
        return self.rt.run(*args, self.name, *cmd, check=check, input=input, timeout=timeout)

    def copy_out(self, src: str, dest: str | os.PathLike[str]) -> Path:
        """Copy ``src`` from the container to ``dest`` on the host; returns the host path."""
        dest_path = Path(dest)
        self.rt.run("cp", f"{self.name}:{src}", str(dest_path))
        return dest_path / Path(src).name if dest_path.is_dir() else dest_path

    def read_file(self, path: str) -> str:
        """Return the text content of a file inside the container."""
        return self.exec("cat", path).stdout

    def tls_ca_cert(self, dest_dir: str | os.PathLike[str]) -> Path:
        """Copy the CA certificate the web server's certificate chains to.

        Uses ``/etc/pihole/tls_ca.crt`` when present, else the self-signed
        ``/etc/pihole/tls.crt``. Returns the host path of the copied file.
        """
        for candidate in ("/etc/pihole/tls_ca.crt", "/etc/pihole/tls.crt"):
            if self.exec("test", "-s", candidate, check=False).returncode == 0:
                return self.copy_out(candidate, dest_dir)
        raise ContainerError(f"{self.name}: no TLS certificate found in /etc/pihole")

    def ftl_config(self, key: str, value: str | None = None) -> str:
        """Read (or set, when ``value`` is given) a setting via ``pihole-FTL --config``.

        Secrets are masked by FTL when read this way.
        """
        cmd = ["pihole-FTL", "--config", key] + ([value] if value is not None else [])
        return self.exec(*cmd).stdout.strip()

    # -- API ---------------------------------------------------------------- #
    def api(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        sid: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 15.0,
        https: bool | None = None,
        verify: bool | str = False,
        user_agent: str = USER_AGENT,
    ) -> ApiResponse:
        """Call the API through the published port (see :func:`api_request`).

        With ``https=True`` (default: :attr:`prefer_https`) the HTTPS port is
        used; ``verify`` may be ``True`` (system trust), ``False`` (no
        verification) or a CA bundle path such as
        the one from :meth:`tls_ca_cert`. For a CA bundle the host name is not
        checked (Pi-hole's certificate is issued for ``pi.hole`` only) and
        ``VERIFY_X509_STRICT`` is cleared: Python 3.13+ sets it by default and
        Pi-hole's CA lacks the key-usage extension it demands.
        """
        context: ssl.SSLContext | None = None
        if https is None:
            https = self.prefer_https
        if https:
            if verify is False:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            elif isinstance(verify, str):
                context = ssl.create_default_context(cafile=verify)
                context.check_hostname = False
                context.verify_flags &= ~ssl.VERIFY_X509_STRICT
            else:
                context = ssl.create_default_context()
        base = self.https_url if https else self.base_url
        return api_request(
            base,
            method,
            path,
            body=body,
            sid=sid,
            headers=headers,
            timeout=timeout,
            context=context,
            user_agent=user_agent,
        )

    def login(self, password: str | None = None, *, totp: int | None = None) -> str | None:
        """``POST /api/auth`` and return the session id.

        Uses the container's password unless ``password`` is given. Returns
        ``None`` when the Pi-hole has no password. Raises :class:`ContainerError`
        on any other failure.
        """
        secret = self.password if password is None else password
        if secret is None:
            raise ContainerError(f"{self.name}: password unknown (random password)")
        payload: dict[str, Any] = {"password": secret}
        if totp is not None:
            payload["totp"] = totp
        resp = self.api("POST", "auth", body=payload)
        if resp.status != 200 or not isinstance(resp.body, dict):
            raise ContainerError(f"{self.name}: login failed: {resp.status} {resp.text[:200]}")
        sid = resp.body.get("session", {}).get("sid")
        return sid if isinstance(sid, str) and sid else None

    def logout(self, sid: str | None) -> int | None:
        """``DELETE /api/auth`` for ``sid``; returns the status (``None`` if nothing to do)."""
        if not sid:
            return None
        try:
            return self.api("DELETE", "auth", sid=sid, timeout=5).status
        except OSError:
            return None

    @contextlib.contextmanager
    def session(self, password: str | None = None) -> Iterator[str | None]:
        """Context manager yielding a session id that is always logged out."""
        sid = self.login(password)
        try:
            yield sid
        finally:
            self.logout(sid)

    def versions(self) -> dict[str, str | None]:
        """``{"core", "web", "ftl", "docker"}`` local versions from ``info/version``.

        Values are reported as-is: ``None`` when Pi-hole reports ``null`` (all of
        them can be ``null`` for a few seconds after start-up), and the very
        first v6 image 2025.02.0 reports FTL as ``"vDev-eaa7dbb"``. ``docker``
        falls back to the image tag. Raises :class:`ContainerError` if the
        endpoint fails (it answers ``500`` until ``/etc/pihole/versions`` exists).
        """
        with self.session() as sid:
            resp = self.api("GET", "info/version", sid=sid)
        if resp.status != 200 or not isinstance(resp.body, dict):
            raise ContainerError(f"{self.name}: info/version failed: {resp.status}")
        version = resp.body["version"]
        out: dict[str, str | None] = {}
        for component in ("core", "web", "ftl"):
            value = (version.get(component) or {}).get("local", {}).get("version")
            out[component] = str(value) if value is not None else None
        docker = (version.get("docker") or {}).get("local")
        out["docker"] = str(docker) if docker else self.tag
        return out

    def set_config(self, sid: str | None, config: Mapping[str, Any]) -> ApiResponse:
        """``PATCH /api/config`` with ``{"config": config}``; raises on non-2xx."""
        resp = self.api("PATCH", "config", body={"config": config}, sid=sid)
        if not resp.ok:
            raise ContainerError(
                f"{self.name}: PATCH config failed: {resp.status} {resp.text[:200]}"
            )
        return resp

    def create_app_password(self) -> str:
        """Generate and activate an app password; return it.

        ``GET /api/auth/app`` suggests a password + hash, ``PATCH /api/config``
        stores the hash in ``webserver.api.app_pwhash``. This invalidates all
        existing sessions (FTL behaviour), so callers must log in again.
        """
        with self.session() as sid:
            resp = self.api("GET", "auth/app", sid=sid)
            if resp.status != 200:
                raise ContainerError(f"{self.name}: GET auth/app failed: {resp.status}")
            app = resp.body["app"]
            self.set_config(sid, {"webserver": {"api": {"app_pwhash": app["hash"]}}})
        return str(app["password"])


def _versions_complete(version: Mapping[str, Any]) -> bool:
    """Whether an ``info/version`` ``version`` object comes from a complete file."""
    for component in ("core", "web", "ftl"):
        local = (version.get(component) or {}).get("local") or {}
        if not local.get("version"):
            return False
    return bool((version.get("docker") or {}).get("local"))
