"""A fake Pi-hole v6 API server for tests.

:class:`FakePihole` is a threaded stdlib :mod:`http.server` bound to
``127.0.0.1`` (or ``host=``, e.g. ``"::1"``) on a random port. It replays a
recorded fixture set (``tests/fixtures/pihole/<docker-tag>/responses.json``)
and emulates the parts of Pi-hole that matter to pihello, verified against real
Pi-hole containers:

- ``POST /api/auth``: correct password → new random ``sid`` (base64 with ``+``,
  ``/`` and ``=``); wrong password → recorded ``401``; ``password=None`` →
  ``200`` with ``sid: null`` and every endpoint open; ``totp=True`` → a correct
  password without a ``totp`` code gets ``400 "No 2FA token found"`` and a
  wrong code ``401 "Invalid 2FA token"`` (like the real thing); more than
  ``max_sessions`` sessions → ``429 api_seats_exceeded``.
- Sessions are accepted from the ``X-FTL-SID`` header (case-insensitive), a
  ``sid`` header, a ``sid`` query parameter or JSON body field. A ``sid``
  cookie is rejected even with ``X-FTL-CSRF`` (as observed on Pi-hole v6.x for
  non-browser clients). Missing/unknown sessions get ``401``.
- ``GET /api/auth`` reports the session; ``DELETE /api/auth`` logs out (``204``)
  and removes it from :attr:`FakePihole.active_sessions`.
- ``GET /api/info/login`` is unauthenticated; unknown endpoints get Pi-hole's
  JSON ``404`` (even without a session); a known endpoint with the wrong method
  gets the recorded ``#method-not-allowed`` reply (``404`` up to FTL v6.7.0,
  ``405`` from v6.7.1); paths outside ``/api`` get an HTML ``404``.
- ``app_password="..."`` is accepted like an app password (``"app-password
  correct"``, no TOTP needed). Like FTL, it is only checked after the normal
  password failed, so an app-password login (and any wrong password) costs two
  password checks.
- Optional rate limits (off by default, so tests stay deterministic):
  ``login_rate_limit=3`` rejects the 4th and later password checks in the same
  wall-clock second (after a 250 ms pause, like FTL): with ``429
  rate_limiting`` when the normal-password check is over the limit, but with
  the plain wrong-password ``401`` when only the app-password check is (FTL
  v6.0-v6.8 behaviour; its ``took`` of ~0.29 s, unlike ~0.07 s for an honest
  miss, includes the pause, whatever :attr:`FakePihole.rate_limit_delay` really
  waits). ``totp_rate_limit=True`` allows one TOTP attempt per
  30 s window. Both use :attr:`FakePihole.clock`, which tests may replace.
- Query strings match exactly first (order-insensitive), then the bare path,
  then the recorded variant of the same path with the most matching parameters.

Faults can be injected per endpoint (:meth:`FakePihole.fail`) and endpoints
removed (:meth:`FakePihole.remove`) to simulate older Pi-hole versions.

The special fixture name ``"demo"`` serves :func:`pihello.demo.api_payloads`
(rich, non-empty data). ``"latest"`` uses ``index.json``'s ``latest`` entry and
falls back to ``"demo"`` while no recorded fixtures exist.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import socket
import socketserver
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from types import TracebackType

__all__ = [
    "FIXTURES_DIR",
    "FakePihole",
    "RecordedRequest",
    "Session",
    "available_fixtures",
    "latest_fixture",
    "load_fixture",
    "normalize_key",
]

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "pihole"

_JSON = "application/json; charset=utf-8"
# FTL's `took` for login replies, measured on 2026.10.0: one password check (hash)
# takes ~0.035 s; a rate-limited check adds FTL's 250 ms pause (disguised 401:
# 0.287-0.290 s, honest app-password miss with two checks: 0.07 s).
_CHECK_TOOK = 0.035
_PAUSE_TOOK = 0.25
_PUBLIC_ENDPOINTS = frozenset({"info/login", "auth"})

_DEFAULT_BODIES: dict[str, tuple[int, Any]] = {
    "POST /api/auth": (
        200,
        {
            "session": {
                "valid": True,
                "totp": False,
                "sid": "PLACEHOLDER",
                "csrf": "PLACEHOLDER",
                "validity": 1800,
                "message": "password correct",
            },
            "took": 0.035,
        },
    ),
    "POST /api/auth#wrong-password": (
        401,
        {
            "session": {
                "valid": False,
                "totp": False,
                "sid": None,
                "validity": -1,
                "message": "password incorrect",
            },
            "took": 0.035,
        },
    ),
    "POST /api/auth#totp-required": (
        400,
        {
            "error": {
                "key": "bad_request",
                "message": "No 2FA token found in JSON payload",
                "hint": None,
            },
            "took": 0.03,
        },
    ),
    "POST /api/auth#totp-invalid": (
        401,
        {
            "error": {"key": "unauthorized", "message": "Invalid 2FA token", "hint": None},
            "took": 0.03,
        },
    ),
    "POST /api/auth#seats-exceeded": (
        429,
        {
            "error": {
                "key": "api_seats_exceeded",
                "message": "API seats exceeded",
                "hint": "increase webserver.api.max_sessions",
            },
            "took": 0.07,
        },
    ),
    "POST /api/auth#no-password": (
        200,
        {
            "session": {
                "valid": True,
                "totp": False,
                "sid": None,
                "validity": -1,
                "message": "no password set",
            },
            "took": 0.00001,
        },
    ),
    "POST /api/auth#login-rate-limited": (
        429,
        {
            "error": {
                "key": "rate_limiting",
                "message": "Rate-limiting login attempts",
                "hint": None,
            },
            "took": 0.25,
        },
    ),
    "POST /api/auth#totp-rate-limited": (
        429,
        {
            "error": {
                "key": "rate_limiting",
                "message": "Rate-limiting 2FA token requests, try again later",
                "hint": None,
            },
            "took": 0.00002,
        },
    ),
    "#unauthenticated": (
        401,
        {
            "error": {"key": "unauthorized", "message": "Unauthorized", "hint": None},
            "took": 0.00002,
        },
    ),
    "#not-found": (
        404,
        {"error": {"key": "not_found", "message": "Not found", "hint": None}, "took": 0.000004},
    ),
}


# --------------------------------------------------------------------------- #
# Fixture loading                                                              #
# --------------------------------------------------------------------------- #
def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else -1 for part in tag.split("."))


def available_fixtures() -> list[str]:
    """Return the recorded fixture tags on disk, oldest first."""
    if not FIXTURES_DIR.is_dir():
        return []
    tags = [p.parent.name for p in FIXTURES_DIR.glob("*/responses.json")]
    return sorted(tags, key=_version_key)


def latest_fixture() -> str | None:
    """Return the newest recorded fixture tag (``index.json``'s ``latest``), if any."""
    index = FIXTURES_DIR / "index.json"
    if index.is_file():
        with contextlib.suppress(OSError, ValueError, AttributeError):
            latest = json.loads(index.read_text(encoding="utf-8")).get("latest")
            if isinstance(latest, str) and (FIXTURES_DIR / latest / "responses.json").is_file():
                return latest
    tags = available_fixtures()
    return tags[-1] if tags else None


def _demo_fixture() -> dict[str, Any]:
    from pihello import demo

    responses = {
        normalize_key(endpoint): {"status": 200, "body": body}
        for endpoint, body in demo.api_payloads().items()
    }
    responses["GET /api/info/login"] = {
        "status": 200,
        "body": {"dns": True, "https_port": 443, "took": 0.000007},
    }
    return {"meta": {"docker_tag": "demo", "password": "pihello-test"}, "responses": responses}


def load_fixture(fixture: str | Path = "latest") -> dict[str, Any]:
    """Load a fixture set: a tag (``"2026.10.0"``), ``"latest"``, ``"demo"`` or a path.

    Returns ``{"meta": {...}, "responses": {normalised key: {"status", "body"}}}``.
    """
    if isinstance(fixture, str) and fixture == "demo":
        return _demo_fixture()
    if isinstance(fixture, str) and fixture == "latest":
        latest = latest_fixture()
        if latest is None:
            return _demo_fixture()
        fixture = latest
    path = Path(fixture)
    if not path.exists() and isinstance(fixture, str) and "/" not in fixture:
        path = FIXTURES_DIR / fixture
    if path.is_dir():
        path = path / "responses.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no Pi-hole fixture {str(fixture)!r}; available: {available_fixtures() or 'none'}"
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    responses = {
        normalize_key(key): value
        for key, value in document.get("responses", {}).items()
        if isinstance(value, dict) and "status" in value
    }
    return {"meta": document.get("meta", {}), "responses": responses}


def _split_key(key: str) -> tuple[str | None, str, str | None, str | None]:
    """Split ``"[METHOD ][/api/]path[?query][#variant]"`` into its parts.

    Returns ``(method or None, path below /api, sorted query or None, variant or None)``.
    """
    text = key.strip()
    method: str | None = None
    head, _, rest = text.partition(" ")
    if rest and head.isalpha() and head.isupper():
        method, text = head, rest.strip()
    text, _, variant = text.partition("#")
    text, _, query = text.partition("?")
    path = text.strip().lstrip("/")
    if path == "api" or path.startswith("api/"):
        path = path[4:]
    sorted_query = urlencode(sorted(parse_qsl(query, keep_blank_values=True))) if query else None
    return method, path.rstrip("/"), sorted_query or None, variant or None


def normalize_key(key: str, *, method: str = "GET") -> str:
    """Normalise a response key to ``"METHOD /api/path[?sorted query][#variant]"``."""
    found_method, path, query, variant = _split_key(key)
    normalized = f"{found_method or method} /api/{path}"
    if query:
        normalized += f"?{query}"
    if variant:
        normalized += f"#{variant}"
    return normalized


# --------------------------------------------------------------------------- #
# Records                                                                      #
# --------------------------------------------------------------------------- #
class _Headers(dict[str, str]):
    """A ``dict`` with case-insensitive string keys (stored lower-case)."""

    def __init__(self, items: Iterator[tuple[str, str]] | None = None) -> None:
        super().__init__()
        for key, value in items or ():
            self[key] = value

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key.lower(), value)

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(key.lower())

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and super().__contains__(key.lower())

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(key.lower(), default)


@dataclass(frozen=True)
class RecordedRequest:
    """One request received by the fake server."""

    method: str
    path: str
    """Full request path without the query string, e.g. ``"/api/stats/summary"``."""
    query: dict[str, str]
    headers: dict[str, str]
    """Request headers; lookups are case-insensitive."""
    json: Any
    """Decoded JSON body (``None`` if empty or not JSON)."""
    body: bytes = b""

    @property
    def endpoint(self) -> str:
        """The path below ``/api`` (``"stats/summary"``)."""
        return _split_key(self.path)[1]

    @property
    def sid(self) -> str | None:
        """The session id sent in the ``X-FTL-SID`` header, if any."""
        return self.headers.get("x-ftl-sid")


@dataclass
class Session:
    """An active API session."""

    sid: str
    csrf: str
    created: float
    last_used: float
    user_agent: str | None = None
    validity: int = 1800


@dataclass
class _Fault:
    method: str | None
    path: str
    query: str | None
    status: int | None
    delay: float
    raw: bytes | None
    drop: bool
    headers: dict[str, str] = field(default_factory=dict)
    times: int | None = None
    drip: float = 0.0


# --------------------------------------------------------------------------- #
# HTTP plumbing                                                                #
# --------------------------------------------------------------------------- #
class _Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, fake: FakePihole, context: ssl.SSLContext | None) -> None:
        self.fake = fake
        self.ssl_context = context
        self.address_family = socket.AF_INET6 if ":" in fake.host else socket.AF_INET
        # getaddrinfo turns "fe80::1%eth0" into the (address, port, flow, scope) bind() needs.
        info = socket.getaddrinfo(fake.host, 0, self.address_family, socket.SOCK_STREAM)
        super().__init__(info[0][4], _Handler)

    def server_bind(self) -> None:
        # Skip HTTPServer's socket.getfqdn() reverse lookup (slow on some hosts).
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.fake.host
        self.server_port = int(self.server_address[1])

    def finish_request(self, request: Any, client_address: Any) -> None:
        if self.ssl_context is None:
            super().finish_request(request, client_address)
            return
        request.settimeout(5)
        try:
            tls = self.ssl_context.wrap_socket(request, server_side=True)
        except (OSError, ssl.SSLError):
            return
        try:
            super().finish_request(tls, client_address)
        finally:
            with contextlib.suppress(OSError):
                tls.close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Clients that time out or hang up are expected in fault-injection tests.
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakePihole"
    sys_version = ""
    timeout = 10
    server: _Server
    drip = 0.0  # seconds between the reply's bytes (see FakePihole.fail)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.server.fake._handle(self, "GET")

    def do_POST(self) -> None:
        self.server.fake._handle(self, "POST")

    def do_DELETE(self) -> None:
        self.server.fake._handle(self, "DELETE")

    def do_PUT(self) -> None:
        self.server.fake._handle(self, "PUT")

    def do_PATCH(self) -> None:
        self.server.fake._handle(self, "PATCH")


def _new_token() -> str:
    """A token shaped like Pi-hole's sids/csrf tokens (24 base64 chars ending in '=')."""
    return base64.b64encode(os.urandom(17)).decode("ascii")


# --------------------------------------------------------------------------- #
# The fake                                                                     #
# --------------------------------------------------------------------------- #
class FakePihole:
    """A threaded fake Pi-hole v6 API replaying recorded responses.

    Use as a context manager or call :meth:`start` / :meth:`stop`::

        with FakePihole("2026.10.0") as pihole:
            client = PiholeClient(pihole.url, "pihello-test")
    """

    def __init__(
        self,
        fixture: str | Path = "latest",
        *,
        password: str | None = "pihello-test",  # noqa: S107 - the fixtures' password
        totp: bool = False,
        max_sessions: int = 16,
        https: bool = False,
        login_rate_limit: int | None = None,
        totp_rate_limit: bool = False,
        app_password: str | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        """Prepare (but don't start) a server for ``fixture`` (see :func:`load_fixture`).

        ``login_rate_limit`` is the number of password checks allowed per
        wall-clock second (real Pi-hole: 3; ``None`` = unlimited);
        ``totp_rate_limit`` allows one TOTP attempt per 30-second window.
        ``host`` is the address to listen on: ``"::1"`` for IPv6, or a link-local
        address with its zone (``"fe80::1%eth0"``).
        """
        loaded = load_fixture(fixture)
        self.fixture = str(fixture)
        self.meta: dict[str, Any] = loaded["meta"]
        self.responses: dict[str, dict[str, Any]] = loaded["responses"]
        self.password = password
        self.totp = totp
        self.totp_code = "123456"
        """The TOTP code accepted when ``totp=True``."""
        self.max_sessions = max_sessions
        self.https = https
        self.login_rate_limit = login_rate_limit
        self.totp_rate_limit = totp_rate_limit
        self.app_password = app_password
        self.host = host
        self.clock: Callable[[], float] = time.time
        """Wall clock used by the rate limits (replace it to make tests deterministic)."""
        self.rate_limit_delay = 0.25
        """Seconds FTL pauses before answering a rate-limited login."""
        self._login_checks: tuple[int, int] = (0, 0)  # (second, password checks in it)
        self._totp_window: int | None = None
        self.ca_cert_path: Path | None = None
        self.requests: list[RecordedRequest] = []
        self.active_sessions: dict[str, Session] = {}
        self._overrides: dict[str, dict[str, Any]] = {}
        self._removed: list[tuple[str, str | None]] = []
        self._faults: list[_Fault] = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._tmpdir: str | None = None

    # -- lifecycle --------------------------------------------------------------
    @property
    def url(self) -> str:
        """Base URL of the running server, e.g. ``"http://127.0.0.1:41234"``."""
        if self._server is None:
            raise RuntimeError("FakePihole is not running; call start() first")
        scheme = "https" if self.https else "http"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}:{self.port}"

    @property
    def port(self) -> int:
        """TCP port of the running server."""
        if self._server is None:
            raise RuntimeError("FakePihole is not running; call start() first")
        return int(self._server.server_address[1])

    def start(self) -> None:
        """Bind to a free port and serve in a background thread."""
        if self._server is not None:
            return
        context = self._make_tls_context() if self.https else None
        self._stopping.clear()
        self._server = _Server(self, context)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"FakePihole:{self.port}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop serving, wake delayed handlers, join all threads and clean up."""
        server, self._server = self._server, None
        if server is not None:
            self._stopping.set()
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
            self.ca_cert_path = None

    def __enter__(self) -> FakePihole:
        """Start the server."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the server."""
        self.stop()

    def _make_tls_context(self) -> ssl.SSLContext:
        import trustme

        ca = trustme.CA()
        cert = ca.issue_cert("127.0.0.1", "localhost", "::1")
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        cert.configure_cert(context)
        self._tmpdir = tempfile.mkdtemp(prefix="pihello-fake-")
        self.ca_cert_path = Path(self._tmpdir) / "ca.pem"
        ca.cert_pem.write_to_path(str(self.ca_cert_path))
        return context

    # -- configuration ------------------------------------------------------------
    def set_response(self, key: str, status: int, body: Any) -> None:
        """Override or add a response, e.g. ``set_response("GET /api/x", 200, {...})``.

        ``key`` may omit the method (``GET``) and the ``/api/`` prefix. ``body`` may
        be ``bytes`` to send a raw (non-JSON) body.
        """
        with self._lock:
            self._overrides[normalize_key(key)] = {"status": status, "body": body}

    def remove(self, endpoint: str) -> None:
        """Make ``endpoint`` answer ``404`` (simulates an older Pi-hole version)."""
        _, path, query, _ = _split_key(endpoint)
        with self._lock:
            self._removed.append((path, query))

    def fail(
        self,
        endpoint: str,
        *,
        status: int | None = None,
        delay: float = 0.0,
        raw: bytes | None = None,
        drop: bool = False,
        headers: Mapping[str, str] | None = None,
        times: int | None = None,
        drip: float = 0.0,
    ) -> None:
        """Inject a fault for requests to ``endpoint`` (``"stats/summary"``, ``"POST auth"``).

        - ``delay``: wait this many seconds first (interrupted by :meth:`stop`);
          alone, the normal response follows the delay.
        - ``status``: answer with this status and a Pi-hole style JSON error
          (or ``raw`` as the body).
        - ``raw``: send these bytes as the body (status 200 unless given).
        - ``drop``: close the connection without any response.
        - ``headers``: extra response headers (e.g. ``Location`` for redirects).
        - ``times``: only fail this many times (``None`` = until :meth:`clear_faults`).
        - ``drip``: send the reply (status line, headers and body) one byte at a
          time, waiting this many seconds before each (a server that stays just
          below any per-read timeout); interrupted by :meth:`stop`.

        Without a method in ``endpoint`` the fault applies to every method; without
        a query it applies to every query string.
        """
        method, path, query, _ = _split_key(endpoint)
        fault = _Fault(
            method, path, query, status, delay, raw, drop, dict(headers or {}), times, drip
        )
        with self._lock:
            self._faults.append(fault)

    def clear_faults(self) -> None:
        """Remove every injected fault."""
        with self._lock:
            self._faults.clear()

    def expire_sessions(self) -> None:
        """Invalidate every session (simulates session timeout or an FTL restart)."""
        with self._lock:
            self.active_sessions.clear()

    def requests_to(self, endpoint: str, method: str | None = None) -> list[RecordedRequest]:
        """Return the recorded requests for ``endpoint`` (path below ``/api``)."""
        _, path, _, _ = _split_key(endpoint)
        with self._lock:
            return [
                request
                for request in self.requests
                if request.endpoint == path and (method is None or request.method == method)
            ]

    # -- request handling -----------------------------------------------------------
    def _handle(self, handler: _Handler, method: str) -> None:
        parts = urlsplit(handler.path)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None
        headers = _Headers(iter(handler.headers.items()))
        request = RecordedRequest(method, parts.path, query, headers, payload, body)
        with self._lock:
            self.requests.append(request)
            fault = self._take_fault(method, request.endpoint, query)

        handler.drip = fault.drip if fault is not None else 0.0
        if fault is not None:
            if fault.delay > 0:
                self._stopping.wait(fault.delay)
            if fault.drop:
                handler.close_connection = True
                return
            if fault.raw is not None or fault.status is not None:
                status = fault.status or 200
                if fault.raw is not None:
                    is_html = fault.raw.lstrip().startswith(b"<")
                    content_type = "text/html" if is_html else "text/plain"
                    self._send(handler, status, fault.raw, content_type, fault.headers)
                else:
                    error = {
                        "error": {
                            "key": "injected",
                            "message": f"Injected HTTP {status}",
                            "hint": None,
                        },
                        "took": 0.00001,
                    }
                    self._send(handler, status, error, _JSON, fault.headers)
                return

        if not (parts.path == "/api" or parts.path.startswith("/api/")):
            page = b"<!DOCTYPE html><html><body><h1>404 Not Found</h1></body></html>"
            self._send(handler, 404, page, "text/html")
            return
        endpoint = request.endpoint
        if self._is_removed(endpoint, query):
            self._send(handler, *self._not_found(parts.path))
            return
        if endpoint == "auth":
            self._send(handler, *self._auth(method, request))
            return
        # Like FTL: unknown endpoints are 404 even without a session.
        response = self._lookup(method, endpoint, query)
        if response is None:
            if self._known_path(endpoint):
                self._send(handler, *self._method_not_allowed(method, endpoint))
            else:
                self._send(handler, *self._not_found(parts.path))
            return
        needs_session = endpoint not in _PUBLIC_ENDPOINTS and response.get("status") != 404
        if needs_session and self._session(request) is None:
            self._send(handler, *self._variant("unauthenticated", f"{method} /api/{endpoint}"))
            return
        self._send(handler, response["status"], response["body"])

    def _take_fault(self, method: str, path: str, query: Mapping[str, str]) -> _Fault | None:
        sorted_query = urlencode(sorted((k, v) for k, v in query.items() if k != "sid")) or None
        for fault in self._faults:
            if fault.method not in (None, method) or fault.path != path:
                continue
            if fault.query is not None and fault.query != sorted_query:
                continue
            if fault.times is not None:
                fault.times -= 1
                if fault.times <= 0:
                    self._faults.remove(fault)
            return fault
        return None

    def _is_removed(self, path: str, query: Mapping[str, str]) -> bool:
        sorted_query = urlencode(sorted((k, v) for k, v in query.items() if k != "sid")) or None
        with self._lock:
            return any(
                removed == path and (removed_query is None or removed_query == sorted_query)
                for removed, removed_query in self._removed
            )

    def _lookup(self, method: str, path: str, query: Mapping[str, str]) -> dict[str, Any] | None:
        pairs = sorted((k, v) for k, v in query.items() if k != "sid")
        base = f"{method} /api/{path}"
        exact = f"{base}?{urlencode(pairs)}" if pairs else base
        with self._lock:
            table = {**self.responses, **self._overrides}
        for key in (exact, base):
            if key in table:
                return table[key]
        best: tuple[int, int, str] | None = None
        wanted = set(pairs)
        for key in table:
            if "#" in key or not key.startswith(base + "?"):
                continue
            recorded = set(parse_qsl(key.split("?", 1)[1], keep_blank_values=True))
            score = (len(recorded & wanted), -len(recorded ^ wanted), key)
            if best is None or score > best:
                best = score
        return table[best[2]] if best is not None else None

    def _variant(self, name: str, base: str | None = None) -> tuple[int, Any]:
        """Return a recorded ``#name`` variant or a default.

        Prefers ``base#name``; otherwise any recorded ``#name`` variant of another
        endpoint with the default's status (never ``/api/auth`` session bodies for
        other endpoints); otherwise the built-in default.
        """
        default = _DEFAULT_BODIES.get(f"{base}#{name}") or _DEFAULT_BODIES[f"#{name}"]
        with self._lock:
            table = {**self.responses, **self._overrides}
        specific = table.get(f"{base}#{name}") if base else None
        if specific is not None:
            return specific["status"], specific["body"]
        auth_base = base is not None and base.endswith(" /api/auth")
        for key in sorted(table):
            entry = table[key]
            if (
                key.endswith(f"#{name}")
                and entry.get("status") == default[0]
                and (auth_base or " /api/auth#" not in key)
            ):
                return entry["status"], entry["body"]
        return default

    def _known_path(self, path: str) -> bool:
        """Whether any method of ``path`` has a (non-variant) recorded response."""
        with self._lock:
            keys = [*self.responses, *self._overrides]
        return any(
            "#" not in key and key.partition(" ")[2].split("?", 1)[0] == f"/api/{path}"
            for key in keys
        )

    def _method_not_allowed(self, method: str, path: str) -> tuple[int, Any]:
        """The recorded wrong-method reply (404 up to FTL v6.7.0, 405 from v6.7.1)."""
        with self._lock:
            table = {**self.responses, **self._overrides}
        recorded = [table[key] for key in sorted(table) if key.endswith("#method-not-allowed")]
        if not recorded or recorded[0].get("status") != 405:
            return self._not_found(f"/api/{path}")
        body = recorded[0]["body"]
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            body = {**body, "error": {**body["error"], "hint": method}}
        return 405, body

    def _not_found(self, path: str) -> tuple[int, Any]:
        """Pi-hole's JSON 404 (recorded if available) with ``hint`` set to ``path``."""
        with self._lock:
            table = {**self.responses, **self._overrides}
        recorded = [
            table[key]
            for key in sorted(table)
            if "#" not in key
            and table[key].get("status") == 404
            and isinstance(table[key].get("body"), dict)
            and isinstance(table[key]["body"].get("error"), dict)
        ]
        if recorded:
            status, body = 404, recorded[0]["body"]
        else:
            status, body = self._variant("not-found")
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            body = {**body, "error": {**body["error"], "hint": path}}
        return status, body

    def _session(self, request: RecordedRequest) -> Session | None:
        """Return the session the request authenticates with (password-less: a dummy)."""
        if self.password is None:
            return Session("", "", 0.0, 0.0)
        sid = (
            request.headers.get("x-ftl-sid")
            or request.headers.get("sid")
            or request.query.get("sid")
            or (request.json.get("sid") if isinstance(request.json, dict) else None)
        )
        if not isinstance(sid, str) or not sid:
            return None
        with self._lock:
            session = self.active_sessions.get(sid)
            if session is None:
                return None
            now = time.time()
            if now - session.last_used > session.validity:
                del self.active_sessions[sid]
                return None
            session.last_used = now
            return session

    def _session_body(
        self, status: int, valid: bool, sid: str | None, message: str
    ) -> tuple[int, Any]:
        return status, {
            "session": {
                "valid": valid,
                "totp": self.totp,
                "sid": sid,
                "validity": 1800 if sid else -1,
                "message": message,
            },
            "took": 0.00001,
        }

    def _auth(self, method: str, request: RecordedRequest) -> tuple[int, Any]:
        if method == "POST":
            return self._login(request)
        if self.password is None:
            with self._lock:
                recorded = self.responses.get(f"{method} /api/auth#no-password")
            if recorded is not None:
                return recorded["status"], recorded["body"]
            status = 200 if method == "GET" else 401
            return self._session_body(status, True, None, "no password set")
        if method not in ("GET", "DELETE"):
            return self._not_found(request.path)
        has_sid = bool(request.headers.get("x-ftl-sid") or request.headers.get("sid"))
        has_sid = has_sid or bool(request.query.get("sid") or request.headers.get("cookie"))
        session = self._session(request)
        if session is None:
            message = "session unknown" if has_sid else "no SID provided"
            return self._session_body(401, False, None, message)
        if method == "DELETE":
            with self._lock:
                self.active_sessions.pop(session.sid, None)
            return 204, None
        status, body = self._session_body(200, True, session.sid, "correct password")
        body["session"]["csrf"] = session.csrf
        return status, body

    def _login(self, request: RecordedRequest) -> tuple[int, Any]:
        if self.password is None:
            return self._variant("no-password", "POST /api/auth")
        if not request.body:
            return 400, {
                "error": {"key": "bad_request", "message": "No request body data", "hint": None},
                "took": 0.00001,
            }
        payload = request.json
        if not isinstance(payload, dict):
            return 400, {
                "error": {
                    "key": "bad_request",
                    "message": "Invalid request body data (no valid JSON), error at hint",
                    "hint": request.body.decode("utf-8", "replace")[:32],
                },
                "took": 0.00001,
            }
        if "password" not in payload:
            return 400, {
                "error": {
                    "key": "bad_request",
                    "message": "No password found in JSON payload",
                    "hint": None,
                },
                "took": 0.00001,
            }
        if self._login_rate_limited():
            self._stopping.wait(self.rate_limit_delay)
            return self._variant("login-rate-limited", "POST /api/auth")
        app_login = False
        took: float | None = None  # FTL's `took` when it differs from the recorded reply's
        if payload["password"] != self.password and self.app_password is not None:
            # FTL checks the app password second; that check counts too, and when it
            # is over the limit FTL answers like a wrong password, not with 429 (but
            # its `took` includes the pause).
            if self._login_rate_limited():
                self._stopping.wait(self.rate_limit_delay)
                took = _CHECK_TOOK + _PAUSE_TOOK + 0.003
            else:
                app_login = payload["password"] == self.app_password
                took = 2 * _CHECK_TOOK
        if payload["password"] != self.password and not app_login:
            with self._lock:
                totp_variant = self.responses.get("POST /api/auth#totp-wrong-password")
            if self.totp and totp_variant is not None:
                status, body = totp_variant["status"], totp_variant["body"]
            else:
                status, body = self._variant("wrong-password", "POST /api/auth")
            if isinstance(body, dict) and isinstance(body.get("session"), dict):
                body = {**body, "session": {**body["session"], "totp": self.totp}}
                if took is not None:
                    body["took"] = took
            return status, body
        if self.totp and not app_login:
            if "totp" not in payload:
                return self._variant("totp-required", "POST /api/auth")
            if self._totp_rate_limited():
                return self._variant("totp-rate-limited", "POST /api/auth")
            if str(payload["totp"]) != str(self.totp_code):
                return self._variant("totp-invalid", "POST /api/auth")
        with self._lock:
            if len(self.active_sessions) >= self.max_sessions:
                seats_exceeded = True
            else:
                seats_exceeded = False
                now = time.time()
                user_agent = request.headers.get("user-agent")
                session = Session(_new_token(), _new_token(), now, now, user_agent)
                self.active_sessions[session.sid] = session
        if seats_exceeded:
            return self._variant("seats-exceeded", "POST /api/auth")
        status, body = self._success_body()
        body["session"].update(sid=session.sid, csrf=session.csrf, totp=self.totp)
        if app_login:
            body["session"]["message"] = "app-password correct"
        return status, body

    def _login_rate_limited(self) -> bool:
        """Count a password check; whether it exceeds ``login_rate_limit`` this second."""
        if self.login_rate_limit is None:
            return False
        second = int(self.clock())
        with self._lock:
            current, count = self._login_checks
            count = count + 1 if current == second else 1
            self._login_checks = (second, count)
        return count > self.login_rate_limit

    def _totp_rate_limited(self) -> bool:
        """Whether a TOTP code was already tried in the current 30-second window."""
        if not self.totp_rate_limit:
            return False
        window = int(self.clock() // 30)
        with self._lock:
            limited = self._totp_window == window
            self._totp_window = window
        return limited

    def _success_body(self) -> tuple[int, Any]:
        with self._lock:
            recorded = self.responses.get("POST /api/auth") or self._overrides.get("POST /api/auth")
        if recorded and isinstance(recorded.get("body"), dict) and recorded["body"].get("session"):
            body = json.loads(json.dumps(recorded["body"]))
            return 200, body
        status, body = _DEFAULT_BODIES["POST /api/auth"]
        return status, json.loads(json.dumps(body))

    def _send(
        self,
        handler: _Handler,
        status: int,
        body: Any,
        content_type: str = _JSON,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(body, (bytes, bytearray)):
            payload = bytes(body)
            if content_type == _JSON:
                content_type = "text/plain"
        elif body is None:
            payload = b""
        else:
            payload = json.dumps(body).encode("utf-8")
        wfile = handler.wfile
        if handler.drip > 0:
            handler.wfile = io.BytesIO()  # collect the reply, then drip it out below
        try:
            handler.send_response(status)
            if payload:
                handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate, private")
            handler.send_header("Connection", "close")
            for name, value in (headers or {}).items():
                handler.send_header(name, value)
            handler.end_headers()
            if payload:
                handler.wfile.write(payload)
            if isinstance(handler.wfile, io.BytesIO):
                reply, handler.wfile = handler.wfile.getvalue(), wfile
                for index in range(len(reply)):
                    if self._stopping.wait(handler.drip):
                        break
                    wfile.write(reply[index : index + 1])
        except OSError:
            pass
        finally:
            handler.wfile = wfile
        handler.close_connection = True
