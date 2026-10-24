"""Pi-hole v6 REST API client.

A small, dependency-free client built on :mod:`urllib.request`:

- :func:`normalize_url` turns whatever the user typed (``pi.hole``,
  ``http://192.168.1.2/admin/``, ``https://proxy.lan/pihole/api``) into a
  canonical base URL.
- :class:`PiholeClient` logs in with ``POST /api/auth``, sends the session id
  in the ``X-FTL-SID`` header, transparently re-authenticates once when a
  session expires, logs out with ``DELETE /api/auth`` and maps every failure to
  the :mod:`pihello.errors` hierarchy with a helpful hint.

Networking notes
----------------
- Standard proxy environment variables (``http_proxy``, ``https_proxy``,
  ``no_proxy``) are honoured exactly like :mod:`urllib` does, except that
  loopback hosts (``localhost``, ``127.0.0.0/8``, ``::1``) always bypass proxies.
- Redirects are never followed: Pi-hole's own HTTP→HTTPS redirect points at
  ``webserver.domain`` (``https://pi.hole/...``, dropping the port), which is
  rarely reachable, and following redirects could leak the password or session
  id to another host. A redirect to HTTPS raises :class:`ConnectionFailed`
  suggesting an ``https://`` URL instead.
- The timeout also bounds host name resolution, which urllib's timeout doesn't
  cover (see :func:`_resolve`).
- The heavy modules (:mod:`urllib.request`, :mod:`ssl`) are imported on first
  use so that importing this module stays cheap.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlencode, urlsplit

from pihello import __version__
from pihello.errors import (
    APIError,
    AuthenticationError,
    ConfigError,
    ConnectionFailed,
    EndpointNotFound,
    PiholeError,
    TLSError,
    TooManySessions,
    TwoFactorRequired,
)

if TYPE_CHECKING:
    import socket
    import ssl
    import urllib.request
    from collections.abc import Callable, Mapping
    from email.message import Message

__all__ = [
    "DEFAULT_TIMEOUT",
    "LOGIN_RATE_LIMIT_RETRIES",
    "LOGOUT_TIMEOUT",
    "PiholeClient",
    "normalize_url",
]

_log = logging.getLogger("pihello")

DEFAULT_TIMEOUT = 5.0
"""Default per-request timeout in seconds."""

LOGOUT_TIMEOUT = 2.0
"""Upper bound for the ``DELETE /api/auth`` request issued by :meth:`PiholeClient.logout`."""

LOGIN_RATE_LIMIT_RETRIES = 2
"""Retries of a login that Pi-hole rejected with ``429 rate_limiting``.

FTL allows at most 3 password checks per wall-clock second, server-wide (and
rejects even correct passwords beyond that), so several shells starting at
once can trip it. An app password costs two checks: FTL tries the normal
password first. When that second check is over the limit, FTL does not answer
``429`` but ``401 "password incorrect"`` (after the same 250 ms pause), so a
``401`` that took FTL at least that long (its ``took``) is retried too. Retries
wait a random 0.35 to 1.1 s and never exceed the client's ``timeout`` in total.
"""

_RATE_LIMIT_DELAY = (0.35, 1.1)
_RATE_LIMIT_PAUSE = 0.25  # FTL sleeps this long before answering a rate-limited login
_MIN_ATTEMPT_TIME = 0.25  # don't retry if less than this would be left for the request
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
_AUTHORITY_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//")
_HOST_RE = re.compile(r"^[\w.~%-]+$")
_ASCII_PRINTABLE = "".join(map(chr, range(0x21, 0x7F)))
_STRIP_SEGMENTS = frozenset({"api", "admin"})
_ADMIN_PAGES = frozenset({"login", "index.php", "index.lp", "login.lp"})

_NOT_PIHOLE_HINT = (
    "check that the URL points at the Pi-hole web server (e.g. http://pi.hole); "
    "Pi-hole v5 and older are not supported"
)
_APP_PASSWORD_HINT = (
    "create an app password in the Pi-hole web interface (Settings → Web interface / API → "  # noqa: S105
    "Configure app password) and use it as pihello's password"
)
_RATE_LIMIT_HINT = (
    "Pi-hole accepts only 3 password checks per second across all clients, so many shells "
    "starting at once (e.g. tmux restoring panes) can hit this limit; set `cache_ttl` in the "
    "config (e.g. cache_ttl = 30) so most greetings skip the login, or try again in a moment"
)
_CERT_HINT = (
    "pass the Pi-hole CA with --ca-cert (it is /etc/pihole/tls_ca.crt on the Pi-hole) "
    "or skip verification with --insecure"
)


# --------------------------------------------------------------------------- #
# URL handling                                                                 #
# --------------------------------------------------------------------------- #
def normalize_url(url: str) -> str:
    """Return the canonical Pi-hole base URL (without ``/api``) for ``url``.

    - A missing scheme defaults to ``http://`` (``pi.hole`` → ``http://pi.hole``);
      a bare IPv6 address is bracketed (``fd00::2`` → ``http://[fd00::2]``).
    - The scheme and host are lower-cased; ports and IPv6 brackets are kept.
    - Trailing ``/``, ``/admin`` (and admin pages such as ``/admin/login``) and
      ``/api`` are stripped; other sub-paths (reverse proxies) are kept, with
      non-ASCII characters percent-encoded.
    - Query strings and fragments are dropped.

    Raises :class:`~pihello.errors.ConfigError` if the URL is empty, uses a
    scheme other than ``http``/``https``, contains credentials or has an invalid
    host or port.
    """
    text = url.strip() if isinstance(url, str) else ""
    if not text:
        raise ConfigError(
            "no Pi-hole URL configured",
            hint="set `url` in the config file or PIHELLO_URL, or pass the address on the "
            "command line",
        )
    shown = _redact_userinfo(text)  # what error messages may quote
    redacted = shown != text
    if any(ch.isspace() for ch in text):
        raise ConfigError(f"invalid Pi-hole URL {shown!r}: contains whitespace")

    match = _SCHEME_RE.match(text)
    if match is None:
        if "://" in text or text.startswith("//"):
            raise ConfigError(f"invalid Pi-hole URL {shown!r}", hint="use e.g. http://pi.hole")
        if text.count(":") >= 2 and not text.startswith("["):
            text = _bracket_bare_ipv6(text, shown)
        text = "http://" + text
    else:
        scheme = match.group(1).lower()
        if scheme not in ("http", "https"):
            raise ConfigError(
                f"unsupported URL scheme {match.group(1)!r} in {shown!r}",
                hint="Pi-hole's API is served over http:// or https://",
            )
        text = scheme + text[match.end(1) :]

    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        detail = "" if redacted else f": {exc}"  # the detail may quote the user info
        raise ConfigError(f"invalid Pi-hole URL {shown!r}{detail}") from None
    if parts.username is not None or parts.password is not None:
        raise ConfigError(
            f"credentials in the Pi-hole URL are not supported: {shown}",
            hint="put the password in the config file, PIHELLO_PASSWORD or a password file",
        )
    host = parts.hostname or ""
    if not host:
        raise ConfigError(
            f"invalid Pi-hole URL {shown!r}: no host name", hint="use e.g. http://pi.hole"
        )
    if ":" in host:
        netloc_host = f"[{host}]"
    elif _is_host_name(host):
        netloc_host = host
    else:
        raise ConfigError(f"invalid host name {host!r} in Pi-hole URL {shown!r}")
    if port == 0:
        raise ConfigError(f"invalid port 0 in Pi-hole URL {shown!r}")
    netloc = netloc_host if port is None else f"{netloc_host}:{port}"

    segments = [segment for segment in parts.path.split("/") if segment]
    while segments:
        last = segments[-1].lower()
        is_admin_page = last in _ADMIN_PAGES and len(segments) >= 2
        if last in _STRIP_SEGMENTS or (is_admin_page and segments[-2].lower() == "admin"):
            segments.pop()
        else:
            break
    # http.client sends the request line as ASCII: percent-encode anything else.
    path = "".join(
        "/" + quote(segment, safe=_ASCII_PRINTABLE, errors="surrogateescape")
        for segment in segments
    )
    return f"{parts.scheme}://{netloc}{path}"


def _is_host_name(host: str) -> bool:
    """Whether ``host`` is a name the resolver accepts.

    :mod:`socket` IDNA-encodes every host name, which fails (``UnicodeError``)
    for empty or over-long labels such as ``pi..hole``.
    """
    if not _HOST_RE.match(host):
        return False
    try:
        host.encode("idna")
    except UnicodeError:
        return False
    return True


def _bracket_bare_ipv6(text: str, original: str) -> str:
    """Wrap a bare IPv6 address (optionally followed by a path) in brackets."""
    import ipaddress  # noqa: PLC0415

    address, slash, rest = text.partition("/")
    try:
        ipaddress.IPv6Address(address.split("%", 1)[0])
    except ValueError:
        raise ConfigError(
            f"invalid Pi-hole URL {original!r}",
            hint="IPv6 addresses with a port must be bracketed, e.g. http://[fd00::2]:8080",
        ) from None
    return f"[{address}]{slash}{rest}"


def _redact_userinfo(url: str) -> str:
    """Replace ``user:password@`` in ``url`` with ``***@`` (for error messages).

    Without ``//`` (``admin:secret@pi.hole``) anything before the last ``@``
    may be a secret, so all of it is hidden.
    """
    text = url.strip()
    authority = _AUTHORITY_RE.match(text)
    if authority is None:
        return re.sub(r"^.*@", "***@", text, flags=re.DOTALL)
    start = authority.end()
    return text[:start] + re.sub(r"^[^/?#]*@", "***@", text[start:])


def _is_loopback(host: str) -> bool:
    """Return whether ``host`` names the local machine."""
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    import ipaddress  # noqa: PLC0415

    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Client                                                                       #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Reply:
    """A raw HTTP reply (any status) with the body decoded as JSON when possible."""

    status: int
    reason: str
    raw: bytes
    data: Any = None
    is_json: bool = False
    location: str | None = None

    def _object(self, key: str) -> dict[str, Any]:
        if isinstance(self.data, dict):
            value = self.data.get(key)
            if isinstance(value, dict):
                return cast("dict[str, Any]", value)
        return {}

    @property
    def error(self) -> dict[str, Any]:
        """Pi-hole's ``{"error": {"key", "message", "hint"}}`` object, or ``{}``."""
        return self._object("error")

    @property
    def session(self) -> dict[str, Any]:
        """Pi-hole's ``{"session": {...}}`` object, or ``{}``."""
        return self._object("session")


def _jitter() -> float:
    """Random delay before retrying a rate-limited login."""
    import random  # noqa: PLC0415 - only needed when rate-limited

    return random.uniform(*_RATE_LIMIT_DELAY)  # noqa: S311 - not security relevant


def _is_login_rate_limited(reply: _Reply) -> bool:
    """``429 rate_limiting`` for password checks (not the 30 s TOTP window)."""
    message = str(reply.error.get("message") or "").lower()
    return (
        reply.status == 429 and reply.error.get("key") == "rate_limiting" and "2fa" not in message
    )


def _maybe_rate_limited(reply: _Reply, elapsed: float) -> bool:
    """A rejected password that may really be FTL's login rate limit.

    FTL checks an app password after the normal one; if only that second check
    exceeds the limit, the reply is the usual ``401`` session object ("password
    incorrect"), sent after FTL's 250 ms pause. A wrong password is answered
    without that pause, so a quick ``401`` is never retried.

    The reply's ``took`` is FTL's own processing time (measured: 0.29 s with the
    pause, 0.07 s for an honest miss), so network latency and TLS handshakes
    don't count; ``elapsed`` (client-side) is the fallback without it.
    """
    session = reply.session
    took = reply.data.get("took") if isinstance(reply.data, dict) else None
    duration = took if isinstance(took, (int, float)) and not isinstance(took, bool) else elapsed
    return (
        reply.status == 401
        and bool(session)
        and session.get("valid") is False
        and duration >= _RATE_LIMIT_PAUSE
    )


@functools.cache
def _no_redirect_class() -> type[Any]:
    """Build (once, lazily) an ``HTTPRedirectHandler`` that never follows redirects."""
    import urllib.request  # noqa: PLC0415

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        # Decline before urllib parses the Location header (it raises ValueError for
        # e.g. "https://[bad"); the 3xx then surfaces as an HTTPError.
        def http_error_302(self, *args: Any, **kwargs: Any) -> None:
            return None

        http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    return _NoRedirect


def _resolve(host: str, port: int, timeout: float | None) -> list[Any]:
    """``socket.getaddrinfo`` for a TCP connection that gives up after ``timeout`` seconds.

    urllib's timeout doesn't cover name resolution, and the system resolver can
    block far longer: when the Pi-hole is the only DNS server and it is down, a
    lookup of ``pi.hole`` waits for every retry (often 10 s or more). The lookup
    runs in a daemon thread that is abandoned if it takes too long.
    """
    import ipaddress  # noqa: PLC0415
    import socket  # noqa: PLC0415

    def lookup() -> list[Any]:
        return socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)

    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        return lookup()  # an IP address: nothing to look up
    if timeout is None:
        return lookup()
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(lookup())
        except Exception as exc:  # re-raised in the calling thread
            outcome.append(exc)

    worker = threading.Thread(target=run, name="pihello-dns", daemon=True)
    worker.start()
    worker.join(timeout)
    if not outcome:
        error = getattr(socket, "EAI_AGAIN", -3)
        raise socket.gaierror(error, f"DNS lookup of {host} timed out after {timeout:g}s")
    if isinstance(outcome[0], Exception):
        raise outcome[0]
    return cast("list[Any]", outcome[0])


def _connect(
    address: tuple[str, int], timeout: Any = None, source_address: Any = None
) -> socket.socket:
    """``socket.create_connection`` with name resolution bounded by ``timeout``."""
    import socket  # noqa: PLC0415

    host, port = address
    limit = float(timeout) if isinstance(timeout, (int, float)) else None
    error: OSError | None = None
    for *_, sockaddr in _resolve(host, port, limit):
        try:
            return socket.create_connection((sockaddr[0], port), timeout, source_address)
        except TimeoutError as exc:
            error = _ConnectTimeout(*exc.args)
        except OSError as exc:
            error = exc
    raise error or OSError(f"no addresses found for {host}")


def _dropped_before_reply(exc: BaseException) -> bool:
    """Whether ``exc`` means the connection closed before any reply (safe to retry a GET).

    Certificate failures, refused connections, timeouts and DNS errors are not:
    retrying them would only fail again, or wait again.
    """
    import ssl  # noqa: PLC0415
    from http.client import RemoteDisconnected  # noqa: PLC0415

    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(
        exc,
        (
            ssl.SSLEOFError,
            ssl.SSLZeroReturnError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            RemoteDisconnected,
        ),
    ):
        return True
    return isinstance(exc, ssl.SSLError) and "eof" in str(exc).lower()


class _ConnectTimeout(TimeoutError):
    """The TCP handshake got no answer: the host is unreachable, not slow."""


#: Serialises TLS handshakes; see ``_HTTPSConnection.connect`` in :func:`_handler_classes`.
_TLS_HANDSHAKE_LOCK = threading.Lock()


@functools.cache
def _handler_classes() -> tuple[type[Any], type[Any]]:
    """Build (once, lazily) urllib HTTP/HTTPS handlers whose connections use :func:`_connect`."""
    import http.client  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    class _HTTPConnection(http.client.HTTPConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = _connect

    class _HTTPSConnection(http.client.HTTPSConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = _connect

        def connect(self) -> None:
            # http.client's own connect(), with the TLS handshake taking turns:
            # Pi-hole FTL up to at least v6.5 fails parallel handshakes and can
            # crash (segfault) on them. The TCP connect and the requests
            # themselves still run in parallel.
            http.client.HTTPConnection.connect(self)
            # _tunnel_host and _context are set by http.client (3.10-3.14 alike)
            server_hostname = self._tunnel_host or self.host  # type: ignore[attr-defined]
            context = self._context  # type: ignore[attr-defined]
            with _TLS_HANDSHAKE_LOCK:
                self.sock = context.wrap_socket(self.sock, server_hostname=server_hostname)

    class _HTTPHandler(urllib.request.HTTPHandler):
        def do_open(self, http_class: Any, req: Any, **kwargs: Any) -> Any:
            return super().do_open(_HTTPConnection, req, **kwargs)

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def do_open(self, http_class: Any, req: Any, **kwargs: Any) -> Any:
            return super().do_open(_HTTPSConnection, req, **kwargs)

    return _HTTPHandler, _HTTPSHandler


class PiholeClient:
    """Client for one Pi-hole v6 instance.

    The client is safe to share between threads: :meth:`get` may be called
    concurrently (sources are fetched in parallel), and an expired session is
    renewed by exactly one thread.

    Use it as a context manager so the session is always released::

        with PiholeClient("http://pi.hole", "app-password") as pihole:
            summary = pihole.get("stats/summary")
    """

    base_url: str
    """Normalised base URL without ``/api`` (e.g. ``https://pi.hole:8443/pihole``)."""
    api_url: str
    """``base_url + "/api"``."""
    sid: str | None
    """Current session id, ``None`` when not logged in or the Pi-hole has no password."""

    def __init__(
        self,
        url: str,
        password: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        verify: bool | str = True,
        user_agent: str | None = None,
    ) -> None:
        """Create a client; no network traffic happens until the first request.

        ``timeout`` is the per-request timeout in seconds (it also bounds the host
        name lookup). ``verify`` controls TLS
        certificate verification: ``True`` uses the system CA store, ``False``
        disables verification, a string is the path of a PEM CA bundle (or a
        directory of hashed certificates). Raises :class:`ConfigError` for an
        invalid URL, timeout or CA path.
        """
        self.base_url = normalize_url(url)
        self.api_url = self.base_url + "/api"
        parts = urlsplit(self.base_url)
        self._host = parts.hostname or ""
        self._https = parts.scheme == "https"
        self._authority = parts.netloc
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout < math.inf  # also rejects NaN
        ):
            raise ConfigError(f"invalid timeout {timeout!r}: must be a positive number of seconds")
        self.timeout = float(timeout)
        self.verify = self._check_verify(verify)
        self.user_agent = user_agent or f"pihello/{__version__}"
        self.sid = None
        self._password = password or None
        self._passwordless = False
        self._auth_lock = threading.RLock()
        self._opener_lock = threading.Lock()
        self._opener: urllib.request.OpenerDirector | None = None
        # Injectable for tests (login rate-limit retries).
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.monotonic
        self._jitter: Callable[[], float] = _jitter

    def __repr__(self) -> str:
        """Return a representation that never includes credentials."""
        return f"PiholeClient({self.base_url!r}, authenticated={self.sid is not None})"

    # -- context manager ------------------------------------------------------
    def __enter__(self) -> PiholeClient:
        """Log in (see :meth:`login`) and return the client."""
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        """Log out (see :meth:`logout`)."""
        self.logout()

    # -- public API -----------------------------------------------------------
    @property
    def has_password(self) -> bool:
        """Whether a password was configured for this client."""
        return self._password is not None

    @property
    def passwordless(self) -> bool:
        """Whether the last login found a Pi-hole without a password (``sid: null``)."""
        return self._passwordless

    def login(self) -> None:
        """Create an API session with ``POST /api/auth``.

        Does nothing without a configured password or when a session already
        exists. A ``200`` reply with a ``null`` sid means the Pi-hole has no
        password: requests are then sent unauthenticated. Raises
        :class:`TwoFactorRequired` (TOTP enabled; Pi-hole answers ``400`` when the
        code is missing), :class:`TooManySessions`, :class:`AuthenticationError`,
        :class:`ConnectionFailed` or :class:`APIError`.

        A ``429 rate_limiting`` reply (more than 3 password checks per second on
        the Pi-hole) is retried up to :data:`LOGIN_RATE_LIMIT_RETRIES` times after
        a random 0.35 to 1.1 s delay, within the ``timeout`` budget.
        """
        if self._password is None:
            return
        with self._auth_lock:
            if self.sid is not None:
                return
            started = self._clock()
            budget = self.timeout
            retries = 0
            while True:
                sent = time.monotonic()  # real time: FTL's pause is real, too
                reply = self._send(
                    "POST", "auth", body={"password": self._password}, timeout=budget
                )
                suspect = _maybe_rate_limited(reply, time.monotonic() - sent)
                limited = _is_login_rate_limited(reply) or suspect
                if retries >= LOGIN_RATE_LIMIT_RETRIES or not limited:
                    break
                delay = self._jitter()
                remaining = self.timeout - (self._clock() - started)
                if remaining - delay < _MIN_ATTEMPT_TIME:
                    break
                _log.debug("login rate-limited by Pi-hole, retrying in %.2f s", delay)
                self._sleep(delay)
                retries += 1
                budget = remaining - delay
            session = reply.session
            if 200 <= reply.status < 300 and session:
                if not session.get("valid", False):
                    raise AuthenticationError(
                        "Pi-hole rejected the login: "
                        + str(session.get("message") or "invalid session"),
                        hint="check the password",
                    )
                sid = session.get("sid")
                if isinstance(sid, str) and not (sid.isascii() and sid.isprintable()):
                    # Pi-hole's sids are base64; http.client refuses to send this one.
                    raise self._not_pihole("auth", "an unusable session id", reply.status)
                self.sid = sid if isinstance(sid, str) and sid else None
                self._passwordless = self.sid is None
                _log.debug("login: %s", "session created" if self.sid else "no password set")
                return
            raise self._login_error(reply, retries=retries, suspect=suspect)

    def logout(self) -> None:
        """Delete the current session with ``DELETE /api/auth``.

        Never raises and uses a short timeout, so it is safe in ``finally``
        blocks and at exit. Does nothing when there is no session.
        """
        with self._auth_lock:
            sid, self.sid = self.sid, None
        if sid is None:
            return
        try:
            reply = self._send("DELETE", "auth", sid=sid, timeout=min(self.timeout, LOGOUT_TIMEOUT))
            if reply.status not in (200, 204, 410):
                _log.debug("logout: unexpected HTTP %s", reply.status)
        except Exception as exc:  # logout must never fail
            _log.debug("logout failed: %s", exc)

    def get(self, endpoint: str, params: Mapping[str, str] | None = None) -> Any:
        """``GET /api/<endpoint>`` and return the decoded JSON body.

        ``endpoint`` is relative to ``/api`` and may contain a query string
        (``"stats/top_domains?count=10"``); ``params`` are appended to it. On a
        ``401`` the client logs in again once (if it has a password) and
        retries. Raises :class:`EndpointNotFound` for ``404``,
        :class:`AuthenticationError`, :class:`ConnectionFailed`,
        :class:`TLSError` or :class:`APIError`.
        """
        path = _endpoint_path(endpoint, params)
        sid = self.sid
        reply = self._send("GET", path, sid=sid)
        if reply.status == 401 and self._password is not None:
            self._renew_session(sid)
            reply = self._send("GET", path, sid=self.sid)
        if reply.status == 401:
            raise self._unauthorized(reply, path)
        return self._result(reply, path)

    def probe(self) -> dict[str, Any]:
        """Unauthenticated ``GET /api/info/login`` (``{"dns": bool, "https_port": int}``).

        Useful to check that the URL points at a Pi-hole v6 without credentials.
        """
        reply = self._send("GET", "info/login")
        if reply.status == 401:
            raise self._unauthorized(reply, "info/login")
        data = self._result(reply, "info/login")
        if not isinstance(data, dict):
            raise self._not_pihole("info/login", "an unexpected JSON document", reply.status)
        return {key: value for key, value in data.items() if key != "took"}

    # -- authentication helpers -------------------------------------------------
    def _renew_session(self, stale_sid: str | None) -> None:
        """Log in again unless another thread already replaced ``stale_sid``."""
        with self._auth_lock:
            if self.sid != stale_sid:
                return
            _log.debug("session rejected (HTTP 401), logging in again")
            self.sid = None
            self.login()

    def _login_error(
        self, reply: _Reply, *, retries: int = 0, suspect: bool = False
    ) -> PiholeError:
        """Map a failed ``POST /api/auth`` reply to an exception.

        ``suspect``: the rejection may have been FTL's login rate limit in
        disguise (see :func:`_maybe_rate_limited`).
        """
        session = reply.session
        error = reply.error
        api_message = str(error.get("message") or session.get("message") or "")
        mentions_2fa = "2fa" in api_message.lower() or "totp" in api_message.lower()
        if reply.status == 401:
            maybe_limited = (
                "; if it is right, Pi-hole may have been rate-limiting logins: " + _RATE_LIMIT_HINT
                if suspect
                else ""
            )
            if mentions_2fa:
                return TwoFactorRequired(
                    "Pi-hole rejected the two-factor (TOTP) code", hint=_APP_PASSWORD_HINT
                )
            if session.get("totp"):
                return TwoFactorRequired(
                    "Pi-hole rejected the password (two-factor authentication is enabled "
                    "on this Pi-hole)",
                    hint="check the password" + maybe_limited + "; " + _APP_PASSWORD_HINT,
                )
            return AuthenticationError(
                "Pi-hole rejected the password",
                hint="check the password (an app password from Settings → Web interface / API "
                "works too)" + maybe_limited,
            )
        if reply.status == 400 and mentions_2fa:
            return TwoFactorRequired(
                "this Pi-hole has two-factor authentication enabled and needs a TOTP code",
                hint=_APP_PASSWORD_HINT + " (app passwords bypass 2FA)",
            )
        if reply.status == 429:
            if error.get("key") == "rate_limiting":
                if mentions_2fa:
                    return AuthenticationError(
                        "Pi-hole is rate-limiting two-factor (TOTP) attempts",
                        hint="wait for the next 30-second TOTP window; " + _APP_PASSWORD_HINT,
                    )
                tried = f" (still limited after {retries} retries)" if retries else ""
                return AuthenticationError(
                    f"Pi-hole is rate-limiting login attempts{tried}", hint=_RATE_LIMIT_HINT
                )
            return TooManySessions(
                "Pi-hole has no free API sessions (webserver.api.max_sessions reached)",
                hint="wait for old sessions to expire, log them out in Settings → Web interface / "
                "API, or raise webserver.api.max_sessions",
            )
        try:
            self._result(reply, "auth")
        except PiholeError as exc:
            return exc
        return self._not_pihole("auth", "a reply without a session", reply.status)

    def _unauthorized(self, reply: _Reply, path: str) -> AuthenticationError:
        """Build the error for a ``401`` that cannot be fixed by logging in."""
        if self._password is None:
            return AuthenticationError(
                "Pi-hole requires a password",
                hint="set `password` (or password_file / password_command / PIHELLO_PASSWORD); "
                + _APP_PASSWORD_HINT,
            )
        detail = reply.error.get("message") or "Unauthorized"
        return AuthenticationError(
            f"Pi-hole refused access to /api/{_bare(path)} ({detail})",
            hint="the session was rejected right after logging in; check the password and "
            "the Pi-hole's API settings",
        )

    # -- response handling ------------------------------------------------------
    def _result(self, reply: _Reply, path: str) -> Any:
        """Return the JSON payload of a successful reply or raise the mapped error."""
        status = reply.status
        if 200 <= status < 300:
            if not reply.raw.strip():
                return None
            if not reply.is_json:
                raise self._not_pihole(path, "a non-JSON response", status)
            return reply.data
        if 300 <= status < 400:
            raise self._redirected(reply, path)
        error = reply.error
        key = str(error["key"]) if error.get("key") is not None else None
        api_message = str(error["message"]) if error.get("message") is not None else None
        api_hint = str(error["hint"]) if error.get("hint") is not None else None
        if status == 404:
            if not error:
                raise self._not_pihole(path, "HTTP 404 without a Pi-hole error", status)
            raise EndpointNotFound(
                f"/api/{_bare(path)} is not available on this Pi-hole (HTTP 404)",
                status=status,
                key=key,
                api_message=api_message,
                path=_bare(path),
                hint="this endpoint may need a newer Pi-hole version",
            )
        if status in (502, 503, 504) and not error:
            raise ConnectionFailed(
                f"{self.base_url} answered HTTP {status} {reply.reason}".rstrip(),
                hint="a reverse proxy could not reach Pi-hole, or Pi-hole is (re)starting; "
                "try again in a moment",
            )
        if not reply.is_json:
            raise self._not_pihole(path, f"HTTP {status} with a non-JSON body", status)
        detail = api_message or reply.reason or "error"
        if api_hint and api_hint != f"/api/{_bare(path)}":
            detail += f" ({api_hint})"
        raise APIError(
            f"Pi-hole returned HTTP {status} for /api/{_bare(path)}: {detail}",
            status=status,
            key=key,
            api_message=api_message,
            path=_bare(path),
        )

    def _redirected(self, reply: _Reply, path: str) -> PiholeError:
        """Map a 3xx reply (redirects are never followed) to an exception."""
        location = reply.location or ""
        try:
            target = urlsplit(location) if location else None
        except ValueError:  # e.g. "https://[bad"
            target = None
        if target is not None and target.scheme == "https" and not self._https:
            # Pi-hole redirects to webserver.domain (often unreachable) and its HTTPS
            # port: keep our host, take the port from the redirect, not our HTTP port.
            try:
                port = target.port or 443
            except ValueError:
                port = 443
            host = f"[{self._host}]" if ":" in self._host else self._host
            netloc = host if port == 443 else f"{host}:{port}"
            suggestion = f"https://{netloc}" + urlsplit(self.base_url).path
            return ConnectionFailed(
                f"{self.base_url} redirects to HTTPS ({location})",
                hint=f"use the https:// URL instead, e.g. {suggestion} (Pi-hole's HTTPS port is "
                f"{port}, unless Docker publishes it on another one; add --ca-cert or --insecure "
                "for Pi-hole's self-signed certificate)",
            )
        return self._not_pihole(
            path, f"a redirect to {location}" if location else "a redirect", reply.status
        )

    def _not_pihole(self, path: str, what: str, status: int | None) -> APIError:
        """Error for a server that answers, but not like a Pi-hole v6 API."""
        return APIError(
            f"{self.base_url} doesn't look like a Pi-hole v6 API "
            f"(got {what} for /api/{_bare(path)})",
            status=status,
            path=_bare(path),
            hint=_NOT_PIHOLE_HINT,
        )

    # -- transport --------------------------------------------------------------
    def _send(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        sid: str | None = None,
        timeout: float | None = None,
    ) -> _Reply:
        """Perform one HTTP request and return the reply for any HTTP status.

        Network-level failures raise :class:`ConnectionFailed`/:class:`TLSError`.
        """
        from http.client import HTTPException  # noqa: PLC0415
        from urllib.error import HTTPError, URLError  # noqa: PLC0415
        from urllib.request import Request  # noqa: PLC0415

        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if sid:
            headers["X-FTL-SID"] = sid
        request = Request(f"{self.api_url}/{path}", data=data, headers=headers, method=method)
        opener = self._get_opener()
        limit = self.timeout if timeout is None else timeout
        # A GET whose connection dropped before any reply is retried once: Pi-hole's
        # web server occasionally cuts off one of several parallel TLS handshakes.
        attempts = 2 if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            cause: BaseException
            try:
                try:
                    response = opener.open(request, timeout=limit)
                except HTTPError as err:
                    with err:
                        reply = _make_reply(
                            err.code, err.reason, err.headers, err.read(_MAX_RESPONSE_BYTES)
                        )
                else:
                    with response:
                        reply = _make_reply(
                            response.status,
                            response.reason,
                            response.headers,
                            response.read(_MAX_RESPONSE_BYTES),
                        )
            except URLError as err:
                reason = err.reason
                cause = reason if isinstance(reason, BaseException) else OSError(str(reason))
            except (OSError, HTTPException) as err:
                cause = err
            else:
                elapsed = (time.perf_counter() - started) * 1000
                _log.debug("%s /api/%s -> %s (%.0f ms)", method, path, reply.status, elapsed)
                return reply
            failure = self._network_error(cause, limit)
            elapsed = (time.perf_counter() - started) * 1000
            _log.debug("%s /api/%s failed after %.0f ms: %s", method, path, elapsed, failure)
            if attempt == attempts or not _dropped_before_reply(cause):
                raise failure
            _log.debug("%s /api/%s: connection dropped, retrying once", method, path)
        raise AssertionError("unreachable")  # pragma: no cover

    def _network_error(self, exc: BaseException, timeout: float) -> ConnectionFailed:
        """Map a low-level exception to :class:`ConnectionFailed` / :class:`TLSError`."""
        import socket  # noqa: PLC0415
        import ssl  # noqa: PLC0415
        from http.client import HTTPException  # noqa: PLC0415

        where = self._authority
        if isinstance(exc, ssl.SSLCertVerificationError):
            detail = exc.verify_message or getattr(exc, "reason", None) or str(exc)
            hint = _CERT_HINT
            if "hostname mismatch" in str(detail).lower() or "not valid for" in str(detail).lower():
                hint = (
                    "connect with a host name the certificate was issued for (Pi-hole's own "
                    "certificate is issued for webserver.domain, `pi.hole` by default), "
                    "or use --insecure"
                )
            return TLSError(f"TLS certificate verification failed for {where}: {detail}", hint=hint)
        if isinstance(exc, ssl.SSLError):
            detail = str(getattr(exc, "reason", None) or exc).replace("_", " ").lower()
            hint = "check that this port serves HTTPS"
            if "wrong version number" in detail or "record layer" in detail:
                hint = (
                    "this port doesn't seem to speak HTTPS; use http:// or Pi-hole's HTTPS "
                    "port (443 by default)"
                )
            return TLSError(f"TLS handshake with {where} failed: {detail}", hint=hint)
        if isinstance(exc, _ConnectTimeout):
            return ConnectionFailed(
                f"no connection to {where} within {timeout:g}s",
                hint="the host isn't answering at all (waiting longer won't help): check the "
                "address, that the Pi-hole is up, and that you're on its network (e.g. not "
                "away from home or off the VPN)",
            )
        if isinstance(exc, TimeoutError):  # connected, but no (complete) reply in time
            return ConnectionFailed(
                f"no response from {self.base_url} within {timeout:g}s",
                hint="Pi-hole may be down or overloaded; raise the limit with --timeout "
                "(or `timeout` in the config)",
            )
        if isinstance(exc, socket.gaierror):
            return ConnectionFailed(
                f"could not resolve host {self._host!r}",
                hint="check the host name, or use the Pi-hole's IP address "
                "(if Pi-hole is your only DNS server and it is down, names won't resolve)",
            )
        if isinstance(exc, ConnectionRefusedError):
            return ConnectionFailed(
                f"connection to {where} refused",
                hint=f"is Pi-hole running at {self.base_url}? is the port right?",
            )
        if isinstance(exc, (ConnectionResetError, HTTPException)):
            hint = "Pi-hole may be restarting; try again"
            if not self._https:
                hint = "if this is Pi-hole's HTTPS port, use https://; otherwise " + hint
            return ConnectionFailed(f"{where} closed the connection unexpectedly", hint=hint)
        detail = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        return ConnectionFailed(
            f"could not connect to {where}: {detail}",
            hint="check the URL and your network connection",
        )

    def _get_opener(self) -> urllib.request.OpenerDirector:
        """Build (once) the urllib opener with TLS, proxy and redirect handling."""
        if self._opener is None:
            with self._opener_lock:
                if self._opener is None:
                    import urllib.request  # noqa: PLC0415

                    http_handler, https_handler = _handler_classes()
                    handlers: list[Any] = [_no_redirect_class()(), http_handler()]
                    if _is_loopback(self._host):
                        handlers.append(urllib.request.ProxyHandler({}))
                    if self._https:
                        handlers.append(https_handler(context=self._ssl_context()))
                    self._opener = urllib.request.build_opener(*handlers)
        return self._opener

    def _ssl_context(self) -> ssl.SSLContext:
        """Create the SSL context for ``verify``.

        ``VERIFY_X509_STRICT`` (default since Python 3.13) is cleared because the
        CA that Pi-hole generates for its self-signed certificate lacks the key
        usage extension and would otherwise be rejected even when trusted
        explicitly. Chain and host name verification stay enabled.
        """
        import ssl  # noqa: PLC0415

        if self.verify is False:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        if self.verify is True:
            context = ssl.create_default_context()
        else:
            path = Path(self.verify).expanduser()
            try:
                if path.is_dir():
                    context = ssl.create_default_context(capath=str(path))
                else:
                    context = ssl.create_default_context(cafile=str(path))
            except (ssl.SSLError, OSError, ValueError) as exc:
                raise ConfigError(
                    f"could not load CA certificate {str(path)!r}: {exc}",
                    hint="--ca-cert / verify_tls must point at a PEM file with the CA certificate",
                ) from None
        context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        return context

    @staticmethod
    def _check_verify(verify: bool | str) -> bool | str:
        """Validate the ``verify`` argument."""
        if isinstance(verify, bool):
            return verify
        if isinstance(verify, str) and verify.strip():
            path = Path(verify.strip()).expanduser()
            if not path.exists():
                raise ConfigError(
                    f"CA certificate file not found: {str(path)!r}",
                    hint="--ca-cert / verify_tls must point at a PEM file with the CA certificate",
                )
            return str(path)
        raise ConfigError(
            f"invalid TLS verification setting {verify!r}",
            hint="use true, false or the path of a CA certificate file",
        )


def _make_reply(status: int, reason: Any, headers: Message | None, raw: bytes) -> _Reply:
    """Build a :class:`_Reply`, decoding ``raw`` as JSON when possible."""
    data: Any = None
    is_json = False
    if raw.strip():
        try:
            data = json.loads(raw, parse_constant=_non_finite)
            is_json = True
        except (ValueError, RecursionError):  # ValueError: JSONDecodeError, UnicodeDecodeError
            data = None
    location = headers.get("Location") if headers is not None else None
    return _Reply(
        status=status,
        reason=str(reason or ""),
        raw=raw,
        data=data,
        is_json=is_json,
        location=location,
    )


def _non_finite(_constant: str) -> None:
    """Read ``NaN``/``Infinity`` (not JSON; Pi-hole's cJSON never sends them) as unknown."""
    return


def _endpoint_path(endpoint: str, params: Mapping[str, str] | None) -> str:
    """Normalise an endpoint (``"/api/stats/summary"`` → ``"stats/summary"``) and add params."""
    path = endpoint.strip().lstrip("/")
    if path == "api" or path.startswith("api/"):
        path = path[4:]
    if params:
        path += ("&" if "?" in path else "?") + urlencode(dict(params))
    return path


def _bare(path: str) -> str:
    """Return ``path`` without its query string (for messages)."""
    return path.split("?", 1)[0]
