"""Tests for :mod:`pihello.client` (URL handling, auth, sessions, TLS, error mapping)."""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.client import RemoteDisconnected
from typing import TYPE_CHECKING, Any

import pytest

from pihello import __version__
from pihello.client import PiholeClient, _endpoint_path, _is_loopback, normalize_url
from pihello.errors import (
    APIError,
    AuthenticationError,
    ConfigError,
    ConnectionFailed,
    EndpointNotFound,
    TLSError,
    TooManySessions,
    TwoFactorRequired,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from helpers.fakepihole import FakePihole

PASSWORD = "pihello-test"


@pytest.fixture
def client(fake_pihole: FakePihole) -> PiholeClient:
    return PiholeClient(fake_pihole.url, PASSWORD, timeout=3)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# normalize_url                                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("pi.hole", "http://pi.hole"),
        ("pi.hole/", "http://pi.hole"),
        ("  pi.hole  ", "http://pi.hole"),
        ("\tpi.hole\n", "http://pi.hole"),
        ("PI.HOLE", "http://pi.hole"),
        ("192.168.1.2", "http://192.168.1.2"),
        ("192.168.1.2:8080", "http://192.168.1.2:8080"),
        ("pi.hole:8080/", "http://pi.hole:8080"),
        ("http://pi.hole", "http://pi.hole"),
        ("https://pi.hole", "https://pi.hole"),
        ("HTTP://Pi.Hole", "http://pi.hole"),
        ("HTTPS://PI.HOLE:443/", "https://pi.hole:443"),
        ("http://pi.hole/admin", "http://pi.hole"),
        ("http://pi.hole/admin/", "http://pi.hole"),
        ("http://pi.hole/ADMIN/", "http://pi.hole"),
        ("http://pi.hole/admin/login", "http://pi.hole"),
        ("http://pi.hole/admin/index.php", "http://pi.hole"),
        ("http://pi.hole/api", "http://pi.hole"),
        ("http://pi.hole/api/", "http://pi.hole"),
        ("http://pi.hole/admin/api", "http://pi.hole"),
        ("http://pi.hole//admin//", "http://pi.hole"),
        ("https://proxy.lan/pihole", "https://proxy.lan/pihole"),
        ("https://proxy.lan/pihole/", "https://proxy.lan/pihole"),
        ("https://proxy.lan/pihole/admin/", "https://proxy.lan/pihole"),
        ("https://proxy.lan/pihole/api", "https://proxy.lan/pihole"),
        ("https://proxy.lan/a/b/c/admin", "https://proxy.lan/a/b/c"),
        ("https://proxy.lan/login", "https://proxy.lan/login"),
        ("http://[::1]", "http://[::1]"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("[::1]:8080", "http://[::1]:8080"),
        ("[fd00::2]/admin/", "http://[fd00::2]"),
        ("fd00::2", "http://[fd00::2]"),
        ("fd00::2/admin", "http://[fd00::2]"),
        ("http://[FD00::2]/", "http://[fd00::2]"),
        ("http://pi.hole/?foo=bar", "http://pi.hole"),
        ("http://pi.hole/admin/#/dashboard", "http://pi.hole"),
        ("http://pi-hole_1.home.arpa", "http://pi-hole_1.home.arpa"),
        ("localhost:80", "http://localhost:80"),
        ("pi.hole.", "http://pi.hole."),
        ("http://bücher.lan", "http://bücher.lan"),
        # http.client sends the request line as ASCII: non-ASCII paths are percent-encoded.
        ("https://proxy.lan/piholé/admin/", "https://proxy.lan/pihol%C3%A9"),
        ("https://proxy.lan/pi%20hole", "https://proxy.lan/pi%20hole"),
    ],
)
def test_normalize_url(given: str, expected: str) -> None:
    assert normalize_url(given) == expected


@pytest.mark.parametrize(
    ("given", "match"),
    [
        ("", "no Pi-hole URL"),
        ("   ", "no Pi-hole URL"),
        ("ftp://pi.hole", "unsupported URL scheme 'ftp'"),
        ("file:///etc/passwd", "unsupported URL scheme"),
        ("ws://pi.hole", "unsupported URL scheme"),
        ("http://", "no host name"),
        ("http:///admin", "no host name"),
        ("://pi.hole", "invalid Pi-hole URL"),
        ("//pi.hole", "invalid Pi-hole URL"),
        ("http://pi hole", "whitespace"),
        ("http://pi.hole:99999", "invalid Pi-hole URL"),
        ("http://pi.hole:abc", "invalid Pi-hole URL"),
        ("http://pi.hole:0", "invalid port"),
        ("http://[::1", "invalid Pi-hole URL"),
        ("http://user:secret@pi.hole", "credentials"),
        ("http://user@pi.hole", "credentials"),
        ("http://pi.hole<script>", "invalid host name"),
        ("not:an:ipv6:address", "invalid Pi-hole URL"),
        # Names the resolver's IDNA encoding rejects (they used to crash with UnicodeError).
        ("http://pi..hole", "invalid host name"),
        ("http://.pi.hole", "invalid host name"),
        ("http://" + "a" * 64 + ".lan", "invalid host name"),
        ("http://bücher..lan", "invalid host name"),
    ],
)
def test_normalize_url_rejects(given: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        normalize_url(given)


def test_normalize_url_redacts_credentials() -> None:
    with pytest.raises(ConfigError) as info:
        normalize_url("https://admin:hunter2@pi.hole")
    assert "hunter2" not in str(info.value)
    assert info.value.hint


@pytest.mark.parametrize(
    "given",
    [
        "admin:hunter2@pi.hole",
        "admin:hunter2@pi.hole:8080",
        "admin:hunter2@[::1]:80",
        "http://admin:hunter2@pi.hole:abc",
        "http://admin:hunter2@[::1",
        "ftp://admin:hunter2@pi.hole",
        "//admin:hunter2@pi.hole",
        "http:/admin:hunter2@pi.hole",
        "http://admin:hunter2 x@pi.hole",
        "http://admin:p@hunter2@pi.hole",
        "http://admin:hunter2\uff03@pi.hole",  # urlsplit quotes the netloc
    ],
)
def test_normalize_url_errors_never_show_credentials(given: str) -> None:
    with pytest.raises(ConfigError) as info:
        normalize_url(given)
    assert "hunter2" not in str(info.value)
    assert "hunter2" not in (info.value.hint or "")


def test_normalize_url_non_string() -> None:
    with pytest.raises(ConfigError):
        normalize_url(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("LOCALHOST.", True),
        ("pihole.localhost", True),
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("::1", True),
        ("pi.hole", False),
        ("192.168.1.2", False),
        ("fd00::2", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert _is_loopback(host) is expected


@pytest.mark.parametrize(
    ("endpoint", "params", "expected"),
    [
        ("stats/summary", None, "stats/summary"),
        ("/stats/summary", None, "stats/summary"),
        ("/api/stats/summary", None, "stats/summary"),
        ("api/stats/summary", None, "stats/summary"),
        ("stats/top_domains", {"count": "5"}, "stats/top_domains?count=5"),
        (
            "stats/top_domains?blocked=true",
            {"count": "5"},
            "stats/top_domains?blocked=true&count=5",
        ),
        ("stats/x", {"q": "a b&c"}, "stats/x?q=a+b%26c"),
    ],
)
def test_endpoint_path(endpoint: str, params: dict[str, str] | None, expected: str) -> None:
    assert _endpoint_path(endpoint, params) == expected


# --------------------------------------------------------------------------- #
# Construction                                                                 #
# --------------------------------------------------------------------------- #
def test_client_attributes() -> None:
    client = PiholeClient("pi.hole/admin/", "secret")
    assert client.base_url == "http://pi.hole"
    assert client.api_url == "http://pi.hole/api"
    assert client.sid is None
    assert client.timeout == 5.0
    assert client.verify is True
    assert client.user_agent == f"pihello/{__version__}"
    assert client.has_password
    assert not client.passwordless
    assert "secret" not in repr(client)
    assert repr(client) == "PiholeClient('http://pi.hole', authenticated=False)"


def test_client_custom_user_agent() -> None:
    assert PiholeClient("pi.hole", user_agent="x/1").user_agent == "x/1"


def test_empty_password_means_no_password() -> None:
    assert not PiholeClient("pi.hole", "").has_password


@pytest.mark.parametrize("timeout", [0, -1, "3", True, None, float("nan"), float("inf")])
def test_invalid_timeout(timeout: Any) -> None:
    with pytest.raises(ConfigError, match="timeout"):
        PiholeClient("pi.hole", timeout=timeout)


def test_verify_path_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="CA certificate file not found"):
        PiholeClient("https://pi.hole", verify=str(tmp_path / "missing.pem"))


@pytest.mark.parametrize("verify", ["", "  ", 1, None])
def test_verify_invalid(verify: Any) -> None:
    with pytest.raises(ConfigError, match="TLS verification"):
        PiholeClient("https://pi.hole", verify=verify)


def test_verify_expands_user(home: Path) -> None:
    (home / "ca.pem").write_text("x")
    assert PiholeClient("https://pi.hole", verify="~/ca.pem").verify == str(home / "ca.pem")


def test_invalid_ca_file_is_config_error(tmp_path: Path) -> None:
    bogus = tmp_path / "ca.pem"
    bogus.write_text("not a certificate")
    client = PiholeClient("https://127.0.0.1:1", "pw", verify=str(bogus))
    with pytest.raises(ConfigError, match="could not load CA certificate"):
        client.login()


def test_ca_directory_is_accepted(tmp_path: Path) -> None:
    client = PiholeClient("https://127.0.0.1:1", verify=str(tmp_path))
    context = client._ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_ssl_context_relaxes_strict_x509() -> None:
    context = PiholeClient("https://pi.hole")._ssl_context()
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    assert not context.verify_flags & strict


def test_ssl_context_insecure() -> None:
    context = PiholeClient("https://pi.hole", verify=False)._ssl_context()
    assert not context.check_hostname
    assert context.verify_mode == ssl.CERT_NONE


def test_import_is_lazy() -> None:
    import subprocess
    import sys

    code = (
        "import sys, pihello.client; print('urllib.request' in sys.modules, 'ssl' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.split() == ["False", "False"]


# --------------------------------------------------------------------------- #
# Login / logout / sessions                                                    #
# --------------------------------------------------------------------------- #
def test_login_get_logout(fake_pihole: FakePihole, client: PiholeClient) -> None:
    with client:
        sid = client.sid
        assert sid is not None
        assert list(fake_pihole.active_sessions) == [sid]
        data = client.get("dns/blocking")
        assert data["blocking"] in ("enabled", "disabled")
    assert client.sid is None
    assert fake_pihole.active_sessions == {}

    login, get, logout = fake_pihole.requests
    assert (login.method, login.endpoint, login.json) == ("POST", "auth", {"password": PASSWORD})
    assert login.headers["Content-Type"] == "application/json"
    assert get.headers["X-FTL-SID"] == sid
    assert "sid" not in get.query
    assert get.headers["User-Agent"] == f"pihello/{__version__}"
    assert get.headers["Accept"] == "application/json"
    assert (logout.method, logout.endpoint, logout.sid) == ("DELETE", "auth", sid)
    assert "X-FTL-SID" not in login.headers


def test_login_is_idempotent(fake_pihole: FakePihole, client: PiholeClient) -> None:
    client.login()
    client.login()
    assert len(fake_pihole.requests_to("auth", "POST")) == 1
    client.logout()


def test_logout_without_session_sends_nothing(
    fake_pihole: FakePihole, client: PiholeClient
) -> None:
    client.logout()
    assert fake_pihole.requests == []


def test_logout_never_raises(fake_pihole: FakePihole, client: PiholeClient) -> None:
    client.login()
    fake_pihole.stop()
    client.logout()
    assert client.sid is None


def test_logout_ignores_unexpected_status(fake_pihole: FakePihole, client: PiholeClient) -> None:
    client.login()
    fake_pihole.fail("DELETE auth", status=500)
    client.logout()
    assert client.sid is None


def test_logout_uses_short_timeout(fake_pihole: FakePihole) -> None:
    client = PiholeClient(fake_pihole.url, PASSWORD, timeout=30)
    client.login()
    fake_pihole.fail("DELETE auth", delay=5)
    import time

    started = time.monotonic()
    client.logout()
    assert time.monotonic() - started < 4


def test_no_password_skips_login(fake_pihole: FakePihole) -> None:
    client = PiholeClient(fake_pihole.url)
    with client:
        with pytest.raises(AuthenticationError, match="requires a password") as info:
            client.get("stats/summary")
        assert info.value.hint
        assert "app password" in info.value.hint
    assert fake_pihole.requests_to("auth") == []


def test_wrong_password(fake_pihole: FakePihole) -> None:
    client = PiholeClient(fake_pihole.url, "wrong")
    with pytest.raises(AuthenticationError, match="rejected the password") as info:
        client.login()
    assert type(info.value) is AuthenticationError
    assert client.sid is None


def test_totp_required(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(totp=True)
    with pytest.raises(TwoFactorRequired, match="two-factor") as info:
        PiholeClient(fake.url, PASSWORD).login()
    assert info.value.hint
    assert "app password" in info.value.hint


def test_totp_wrong_password(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(totp=True)
    with pytest.raises(TwoFactorRequired, match="two-factor authentication is enabled"):
        PiholeClient(fake.url, "wrong").login()


def test_totp_invalid_code(fake_pihole: FakePihole) -> None:
    fake_pihole.fail(
        "POST auth",
        status=401,
        raw=b'{"error": {"key": "unauthorized", "message": "Invalid 2FA token", "hint": null}}',
    )
    with pytest.raises(TwoFactorRequired, match="TOTP"):
        PiholeClient(fake_pihole.url, PASSWORD).login()


def test_seats_exceeded(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(max_sessions=1)
    first = PiholeClient(fake.url, PASSWORD)
    first.login()
    with pytest.raises(TooManySessions, match="max_sessions") as info:
        PiholeClient(fake.url, PASSWORD).login()
    assert info.value.hint
    first.logout()
    PiholeClient(fake.url, PASSWORD).login()


RATE_LIMITED = b'{"error": {"key": "rate_limiting", "message": "Rate-limiting login attempts"}}'


def no_sleep_client(url: str, **kwargs: Any) -> tuple[PiholeClient, list[float]]:
    """A client whose rate-limit retries record their delays instead of sleeping."""
    client = PiholeClient(url, PASSWORD, **kwargs)
    sleeps: list[float] = []
    client._sleep = sleeps.append
    client._jitter = lambda: 0.5
    return client, sleeps


def test_rate_limited_login_is_retried(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("POST auth", status=429, raw=RATE_LIMITED, times=1)
    client, sleeps = no_sleep_client(fake_pihole.url)
    client.login()
    assert client.sid is not None
    assert sleeps == [0.5]
    assert len(fake_pihole.requests_to("auth", "POST")) == 2
    client.logout()


def test_rate_limited_login_gives_up(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("POST auth", status=429, raw=RATE_LIMITED)
    client, sleeps = no_sleep_client(fake_pihole.url)
    with pytest.raises(AuthenticationError, match="still limited after 2 retries") as info:
        client.login()
    assert not isinstance(info.value, TooManySessions)
    assert info.value.hint
    assert "cache_ttl" in info.value.hint
    assert sleeps == [0.5, 0.5]
    assert len(fake_pihole.requests_to("auth", "POST")) == 3


def test_rate_limit_retries_respect_timeout_budget(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("POST auth", status=429, raw=RATE_LIMITED)
    client, sleeps = no_sleep_client(fake_pihole.url, timeout=1.0)
    now = [100.0]
    client._clock = lambda: now[0]
    client._jitter = lambda: 0.6

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    client._sleep = sleep
    timeouts: list[float | None] = []
    send = client._send

    def recording_send(*args: Any, **kwargs: Any) -> Any:
        timeouts.append(kwargs.get("timeout"))
        return send(*args, **kwargs)

    client._send = recording_send  # type: ignore[method-assign]
    with pytest.raises(AuthenticationError, match="after 1 retries"):
        client.login()
    assert sleeps == [0.6]  # a second retry would exceed the 1 s budget
    assert timeouts == [1.0, pytest.approx(0.4)]


def test_rate_limit_without_budget_is_not_retried(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("POST auth", status=429, raw=RATE_LIMITED)
    client, sleeps = no_sleep_client(fake_pihole.url, timeout=0.5)
    with pytest.raises(AuthenticationError, match=r"rate-limiting login attempts$"):
        client.login()
    assert sleeps == []


def test_totp_rate_limit_is_not_retried(fake_pihole: FakePihole) -> None:
    body = b'{"error": {"key": "rate_limiting", "message": "Rate-limiting 2FA token requests"}}'
    fake_pihole.fail("POST auth", status=429, raw=body)
    client, sleeps = no_sleep_client(fake_pihole.url)
    with pytest.raises(AuthenticationError, match="two-factor") as info:
        client.login()
    assert sleeps == []
    assert info.value.hint
    assert "app password" in info.value.hint


def test_default_jitter() -> None:
    from pihello.client import _jitter

    delays = {_jitter() for _ in range(50)}
    assert all(0.35 <= delay <= 1.1 for delay in delays)
    assert len(delays) > 1


def test_parallel_logins_survive_pihole_rate_limit(
    fake_pihole_factory: Callable[..., FakePihole],
) -> None:
    """Six shells starting at once against FTL's 3-logins-per-second limit."""
    from pihello import sources

    fake = fake_pihole_factory(login_rate_limit=3)
    barrier = threading.Barrier(6)

    def greet(_: int) -> int:
        client = PiholeClient(fake.url, PASSWORD, timeout=5)
        barrier.wait()
        with client:
            variables, problems = sources.collect(client, ["queries.total", "blocking"])
        assert problems == []
        return int(variables["queries"]["total"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        totals = list(pool.map(greet, range(6)))
    assert len(set(totals)) == 1
    assert fake.active_sessions == {}
    # 3 logins fit in a second; the rest were retried (unless they straddled a second).
    assert len(fake.requests_to("auth", "POST")) >= 6


APP_PASSWORD = "app-secret-123"


def test_app_password_login(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(app_password=APP_PASSWORD, totp=True)
    with PiholeClient(fake.url, APP_PASSWORD) as client:  # app passwords bypass 2FA
        assert client.sid is not None
    assert fake.active_sessions == {}


def test_app_password_over_the_rate_limit_is_retried(
    fake_pihole_factory: Callable[..., FakePihole],
) -> None:
    """FTL checks an app password only after the normal password failed; both checks
    count, and when the second one is over the limit FTL pauses 250 ms and answers
    401 "password incorrect" instead of 429 (seen on every v6 release). The pause shows
    in the reply's ``took``, which the client goes by (the fake skips the real wait)."""
    fake = fake_pihole_factory(login_rate_limit=3, app_password=APP_PASSWORD)
    fake.rate_limit_delay = 0
    now = [1000.0]
    fake.clock = lambda: now[0]
    PiholeClient(fake.url, APP_PASSWORD).login()  # 2 of the 3 checks in this second
    client = PiholeClient(fake.url, APP_PASSWORD)
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += 1.0  # the next attempt lands in a new second

    client._sleep = sleep
    client._jitter = lambda: 0.5
    client.login()
    assert client.sid is not None
    assert sleeps == [0.5]
    assert len(fake.requests_to("auth", "POST")) == 3
    client.logout()


SLOW_401 = (
    b'{"session": {"valid": false, "totp": false, "sid": null, "validity": -1, '
    b'"message": "password incorrect"}}'
)


def wrong_password_reply(took: float) -> bytes:
    """FTL's 401 for a rejected password, with its server-side processing time."""
    return SLOW_401[:-1] + f', "took": {took}}}'.encode()


def test_slow_wrong_password_mentions_the_rate_limit(fake_pihole: FakePihole) -> None:
    """Without FTL's ``took`` the client-side time counts."""
    fake_pihole.fail("POST auth", status=401, raw=SLOW_401, delay=0.26)
    client, sleeps = no_sleep_client(fake_pihole.url)
    with pytest.raises(AuthenticationError, match="rejected the password") as info:
        client.login()
    assert not isinstance(info.value, TooManySessions)
    assert sleeps == [0.5, 0.5]
    assert len(fake_pihole.requests_to("auth", "POST")) == 3
    assert info.value.hint
    assert "rate-limiting" in info.value.hint
    assert "cache_ttl" in info.value.hint


def test_fast_wrong_password_is_not_retried(
    fake_pihole_factory: Callable[..., FakePihole],
) -> None:
    fake = fake_pihole_factory(login_rate_limit=3, app_password=APP_PASSWORD)
    client, sleeps = no_sleep_client(fake.url)
    client._password = "wrong"
    with pytest.raises(AuthenticationError, match="rejected the password") as info:
        client.login()
    assert sleeps == []
    assert len(fake.requests_to("auth", "POST")) == 1
    assert info.value.hint
    assert "rate-limiting" not in info.value.hint


def test_slow_link_wrong_password_is_not_retried(fake_pihole: FakePihole) -> None:
    """A high-latency link (VPN, TLS handshake) makes the reply slow, but FTL's own
    ``took`` (0.07 s for an honest app-password miss, measured) shows no pause."""
    fake_pihole.fail("POST auth", status=401, raw=wrong_password_reply(0.0699), delay=0.3)
    client, sleeps = no_sleep_client(fake_pihole.url)
    with pytest.raises(AuthenticationError, match="rejected the password") as info:
        client.login()
    assert sleeps == []
    assert len(fake_pihole.requests_to("auth", "POST")) == 1
    assert info.value.hint
    assert "rate-limiting" not in info.value.hint


def test_rate_limit_pause_reported_by_pihole_is_retried(fake_pihole: FakePihole) -> None:
    """FTL's ``took`` includes its 250 ms rate-limit pause (0.287-0.290 s measured for the
    disguised 401), whatever the client-side time."""
    fake_pihole.fail("POST auth", status=401, raw=wrong_password_reply(0.288))
    client, sleeps = no_sleep_client(fake_pihole.url)
    with pytest.raises(AuthenticationError, match="rejected the password") as info:
        client.login()
    assert sleeps == [0.5, 0.5]
    assert len(fake_pihole.requests_to("auth", "POST")) == 3
    assert info.value.hint
    assert "rate-limiting" in info.value.hint


def test_parallel_app_password_logins_survive_pihole_rate_limit(
    fake_pihole_factory: Callable[..., FakePihole],
) -> None:
    """Two shells with an app password starting at once (one of them failed before)."""
    fake = fake_pihole_factory(login_rate_limit=3, app_password=APP_PASSWORD)
    barrier = threading.Barrier(2)

    def login(_: int) -> bool:
        client = PiholeClient(fake.url, APP_PASSWORD, timeout=5)
        barrier.wait()
        with client:
            return client.sid is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(login, range(2))) == [True, True]
    assert fake.active_sessions == {}


def test_login_invalid_session_reply(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("POST auth", raw=b'{"session": {"valid": false, "message": "nope"}}')
    with pytest.raises(AuthenticationError, match="nope"):
        PiholeClient(fake_pihole.url, PASSWORD).login()


def test_login_reply_without_session(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("POST auth", raw=b'{"something": "else"}')
    with pytest.raises(APIError, match="doesn't look like a Pi-hole v6 API"):
        PiholeClient(fake_pihole.url, PASSWORD).login()


def test_login_server_error(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("POST auth", status=500)
    with pytest.raises(APIError, match="HTTP 500") as info:
        PiholeClient(fake_pihole.url, PASSWORD).login()
    assert info.value.status == 500


def test_login_bad_request(fake_pihole: FakePihole) -> None:
    fake_pihole.fail(
        "POST auth",
        status=400,
        raw=b'{"error": {"key": "bad_request", "message": "No password found", "hint": null}}',
    )
    with pytest.raises(APIError, match="No password found") as info:
        PiholeClient(fake_pihole.url, PASSWORD).login()
    assert not isinstance(info.value, AuthenticationError)


def test_passwordless_pihole(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(password=None)
    with PiholeClient(fake.url, "anything") as client:
        assert client.sid is None
        assert client.passwordless
        assert client.get("dns/blocking")["blocking"]
    assert fake.requests_to("auth", "DELETE") == []
    with PiholeClient(fake.url) as client:
        assert client.get("stats/summary")["queries"]


def test_relogin_after_session_expiry(fake_pihole: FakePihole, client: PiholeClient) -> None:
    with client:
        old = client.sid
        fake_pihole.expire_sessions()
        assert client.get("dns/blocking")
        assert client.sid not in (None, old)
    assert len(fake_pihole.requests_to("auth", "POST")) == 2


def test_lazy_login_on_first_401(fake_pihole: FakePihole, client: PiholeClient) -> None:
    assert client.get("dns/blocking")
    assert client.sid is not None
    client.logout()


def test_relogin_failure_propagates(fake_pihole: FakePihole, client: PiholeClient) -> None:
    client.login()
    fake_pihole.expire_sessions()
    fake_pihole.password = "changed"
    with pytest.raises(AuthenticationError, match="rejected the password"):
        client.get("dns/blocking")


def test_persistent_401_after_relogin(fake_pihole: FakePihole, client: PiholeClient) -> None:
    client.login()
    fake_pihole.set_response("GET /api/dns/blocking", 401, {"error": {"message": "Unauthorized"}})
    with pytest.raises(AuthenticationError, match="refused access to /api/dns/blocking"):
        client.get("dns/blocking")
    assert len(fake_pihole.requests_to("dns/blocking")) == 2
    client.logout()


def test_parallel_requests_share_one_relogin(fake_pihole: FakePihole, client: PiholeClient) -> None:
    client.login()
    fake_pihole.expire_sessions()
    endpoints = ["dns/blocking", "stats/summary", "info/version", "info/system"] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(client.get, endpoints))
    assert all(results)
    assert len(fake_pihole.requests_to("auth", "POST")) == 2
    assert len(fake_pihole.active_sessions) == 1
    client.logout()
    assert fake_pihole.active_sessions == {}


def test_parallel_lazy_login(fake_pihole: FakePihole, client: PiholeClient) -> None:
    barrier = threading.Barrier(6)

    def call(endpoint: str) -> Any:
        barrier.wait()
        return client.get(endpoint)

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert all(pool.map(call, ["dns/blocking"] * 6))
    assert len(fake_pihole.active_sessions) == 1
    client.logout()


# --------------------------------------------------------------------------- #
# Requests and response mapping                                                #
# --------------------------------------------------------------------------- #
def test_get_with_params(fake_pihole: FakePihole, client: PiholeClient) -> None:
    with client:
        data = client.get("/api/stats/top_domains", {"count": "10", "blocked": "true"})
    assert "domains" in data
    request = fake_pihole.requests_to("stats/top_domains")[0]
    assert request.query == {"count": "10", "blocked": "true"}


def test_probe(fake_pihole: FakePihole) -> None:
    info = PiholeClient(fake_pihole.url).probe()
    assert "took" not in info
    assert info["dns"] is True
    assert fake_pihole.requests[0].sid is None


def test_probe_unexpected_json(fake_pihole: FakePihole) -> None:
    fake_pihole.set_response("info/login", 200, [1, 2])
    with pytest.raises(APIError, match="unexpected JSON"):
        PiholeClient(fake_pihole.url).probe()


def test_probe_unauthorized(fake_pihole: FakePihole) -> None:
    fake_pihole.set_response("info/login", 401, {"error": {"key": "unauthorized"}})
    with pytest.raises(AuthenticationError):
        PiholeClient(fake_pihole.url).probe()


def test_endpoint_not_found(client: PiholeClient) -> None:
    with client, pytest.raises(EndpointNotFound) as info:
        client.get("does/not/exist?x=1")
    error = info.value
    assert error.status == 404
    assert error.path == "does/not/exist"
    assert error.key == "not_found"
    assert "/api/does/not/exist" in str(error)


def test_html_404_is_not_pihole(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.fail("info/version", status=404, raw=b"<html><body>Not Found</body></html>")
    with client, pytest.raises(APIError, match="doesn't look like a Pi-hole v6 API") as info:
        client.get("info/version")
    assert not isinstance(info.value, EndpointNotFound)
    assert info.value.hint
    assert "v5" in info.value.hint


def test_non_json_200(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.fail("info/version", raw=b"<!DOCTYPE html><html>router login</html>")
    with client, pytest.raises(APIError, match="non-JSON response") as info:
        client.get("info/version")
    assert info.value.status == 200


def test_empty_200_and_204(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.fail("info/version", raw=b"")
    fake_pihole.fail("info/host", status=204, raw=b"")
    with client:
        assert client.get("info/version") is None
        assert client.get("info/host") is None


def test_server_error_with_pihole_json(fake_pihole: FakePihole, client: PiholeClient) -> None:
    body = {"error": {"key": "database_error", "message": "DB locked", "hint": "busy"}}
    fake_pihole.set_response("GET /api/info/database", 500, body)
    with client, pytest.raises(APIError) as info:
        client.get("info/database")
    error = info.value
    assert (error.status, error.key, error.api_message, error.path) == (
        500,
        "database_error",
        "DB locked",
        "info/database",
    )
    assert "DB locked (busy)" in str(error)


def test_server_error_without_details(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.set_response("GET /api/info/database", 500, {"unexpected": True})
    with client, pytest.raises(APIError, match="HTTP 500"):
        client.get("info/database")


def test_server_error_html(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.fail("info/database", status=500, raw=b"<html>oops</html>")
    with client, pytest.raises(APIError, match="non-JSON body"):
        client.get("info/database")


@pytest.mark.parametrize("status", [502, 503, 504])
def test_proxy_gateway_errors(fake_pihole: FakePihole, client: PiholeClient, status: int) -> None:
    fake_pihole.fail("info/version", status=status, raw=b"<html>Bad Gateway</html>")
    with client, pytest.raises(ConnectionFailed, match=f"HTTP {status}") as info:
        client.get("info/version")
    assert info.value.hint
    assert "proxy" in info.value.hint


def test_redirect_to_https(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.fail(
        "POST auth", status=308, raw=b"", headers={"Location": "https://pi.hole/api/auth"}
    )
    with pytest.raises(ConnectionFailed, match="redirects to HTTPS") as info:
        client.login()
    assert info.value.hint
    # The HTTP port does not speak HTTPS: suggest the port Pi-hole redirects to (443).
    assert "e.g. https://127.0.0.1 (" in info.value.hint
    assert str(fake_pihole.port) not in info.value.hint
    assert len(fake_pihole.requests) == 1


def test_redirect_to_https_keeps_the_redirect_port(fake_pihole: FakePihole) -> None:
    fake_pihole.fail(
        "POST auth", status=308, raw=b"", headers={"Location": "https://pi.hole:8443/api/auth"}
    )
    client = PiholeClient(fake_pihole.url, PASSWORD)
    with pytest.raises(ConnectionFailed, match="redirects to HTTPS") as info:
        client.login()
    assert info.value.hint
    assert "e.g. https://127.0.0.1:8443 (Pi-hole's HTTPS port is 8443" in info.value.hint


def test_redirect_to_https_with_a_bad_port(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.fail(
        "POST auth", status=308, raw=b"", headers={"Location": "https://pi.hole:99999/api/auth"}
    )
    with pytest.raises(ConnectionFailed, match="redirects to HTTPS") as info:
        client.login()
    assert info.value.hint
    assert "e.g. https://127.0.0.1 (" in info.value.hint


def test_redirect_elsewhere_is_not_followed(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.fail("info/version", status=302, raw=b"", headers={"Location": "/admin/login"})
    with client, pytest.raises(APIError, match="redirect to /admin/login"):
        client.get("info/version")
    assert len(fake_pihole.requests_to("info/version")) == 1


def test_redirect_without_location(fake_pihole: FakePihole, client: PiholeClient) -> None:
    fake_pihole.fail("info/version", status=301, raw=b"")
    with client, pytest.raises(APIError, match="got a redirect"):
        client.get("info/version")


def test_redirect_with_malformed_location(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("info/login", status=302, raw=b"", headers={"Location": "https://[bad/api"})
    with pytest.raises(APIError, match=r"redirect to https://\[bad/api"):
        PiholeClient(fake_pihole.url).probe()


# --------------------------------------------------------------------------- #
# Broken or hostile servers never crash the client                             #
# --------------------------------------------------------------------------- #
def test_deeply_nested_json_is_not_pihole(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("info/login", raw=b"[" * 100_000)
    with pytest.raises(APIError, match="non-JSON response"):
        PiholeClient(fake_pihole.url).probe()


def test_non_finite_json_numbers_are_unknown(fake_pihole: FakePihole) -> None:
    """cJSON never sends NaN/Infinity; Python's json would accept them."""
    fake_pihole.fail("info/login", raw=b'{"dns": true, "https_port": Infinity, "x": NaN}')
    assert PiholeClient(fake_pihole.url).probe() == {"dns": True, "https_port": None, "x": None}


@pytest.mark.parametrize("sid", ["abc\r\nX-Injected: 1", "sid☃", "a\x00b"])
def test_unusable_session_id_is_rejected(fake_pihole: FakePihole, sid: str) -> None:
    import json

    session = {"valid": True, "totp": False, "sid": sid, "validity": 1800}
    fake_pihole.fail("POST auth", raw=json.dumps({"session": session}).encode())
    client = PiholeClient(fake_pihole.url, PASSWORD)
    with pytest.raises(APIError, match="doesn't look like a Pi-hole v6 API"):
        client.login()
    assert client.sid is None


# --------------------------------------------------------------------------- #
# Network errors                                                               #
# --------------------------------------------------------------------------- #
def test_timeout(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("info/version", delay=2)
    client = PiholeClient(fake_pihole.url, timeout=0.2)
    with pytest.raises(ConnectionFailed, match=r"within 0\.2s") as info:
        client.get("info/version")
    assert info.value.hint
    assert "--timeout" in info.value.hint


def test_connect_timeout_is_not_a_slow_pihole(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead or unroutable address (e.g. a laptop away from home) never completes the
    TCP handshake; a longer --timeout won't help (unlike a slow reply, see test_timeout)."""
    attempts: list[Any] = []

    def unreachable(address: Any, timeout: Any = None, *args: Any) -> Any:
        attempts.append((address, timeout))
        raise TimeoutError("timed out")  # what a connect() that gets no answer raises

    monkeypatch.setattr(socket, "create_connection", unreachable)
    with pytest.raises(
        ConnectionFailed, match=r"no connection to 192\.0\.2\.1 within 0\.3s"
    ) as info:
        PiholeClient("http://192.0.2.1", PASSWORD, timeout=0.3).login()
    assert attempts == [(("192.0.2.1", 80), 0.3)]
    assert info.value.hint
    assert "--timeout" not in info.value.hint
    assert "network" in info.value.hint


def test_connection_dropped(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("info/version", drop=True)
    with pytest.raises(ConnectionFailed, match="closed the connection") as info:
        PiholeClient(fake_pihole.url).get("info/version")
    assert info.value.hint
    assert "https://" in info.value.hint


def test_get_retries_once_when_the_connection_drops(fake_pihole: FakePihole) -> None:
    # Pi-hole's web server occasionally cuts off one of several parallel TLS
    # handshakes ("unexpected eof while reading"); a single retry recovers.
    fake_pihole.fail("info/version", drop=True, times=1)
    with PiholeClient(fake_pihole.url, PASSWORD) as client:
        data = client.get("info/version")
    assert "version" in data
    assert len(fake_pihole.requests_to("info/version")) == 2


def test_get_gives_up_after_one_retry(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("info/version", drop=True)
    with pytest.raises(ConnectionFailed, match="closed the connection"):
        PiholeClient(fake_pihole.url).get("info/version")
    assert len(fake_pihole.requests_to("info/version")) == 2


def test_login_is_not_retried_when_the_connection_drops(fake_pihole: FakePihole) -> None:
    # A dropped login may still have created a session; never send it twice.
    fake_pihole.fail("POST auth", drop=True, times=1)
    with pytest.raises(ConnectionFailed):
        PiholeClient(fake_pihole.url, PASSWORD).login()
    assert len(fake_pihole.requests_to("auth", "POST")) == 1


@pytest.mark.parametrize(
    ("exc", "retry"),
    [
        (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), True),
        (ssl.SSLError(1, "[SSL: UNEXPECTED_EOF_WHILE_READING] unexpected eof while reading"), True),
        (ConnectionResetError(104, "Connection reset by peer"), True),
        (RemoteDisconnected("Remote end closed connection without response"), True),
        (BrokenPipeError(32, "Broken pipe"), True),
        (ssl.SSLCertVerificationError(1, "certificate verify failed"), False),
        (ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"), False),
        (ConnectionRefusedError(111, "Connection refused"), False),
        (TimeoutError("timed out"), False),
        (socket.gaierror(-2, "Name or service not known"), False),
    ],
)
def test_which_failures_are_retried(exc: BaseException, retry: bool) -> None:
    from pihello.client import _dropped_before_reply

    assert _dropped_before_reply(exc) is retry


def test_connection_refused() -> None:
    port = free_port()
    with pytest.raises(ConnectionFailed, match="refused") as info:
        PiholeClient(f"http://127.0.0.1:{port}", PASSWORD).login()
    assert info.value.hint == f"is Pi-hole running at http://127.0.0.1:{port}? is the port right?"
    assert not isinstance(info.value, TLSError)


def test_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(ConnectionFailed, match=r"could not resolve host 'pi\.hole'") as info:
        PiholeClient("http://pi.hole", PASSWORD).login()
    assert info.value.hint
    assert "IP address" in info.value.hint


def test_dns_lookup_respects_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The system resolver has no timeout urllib could apply: if Pi-hole is the only DNS
    server and it is down, looking up pi.hole blocks for the resolver's retries."""
    import time

    real_getaddrinfo = socket.getaddrinfo
    release = threading.Event()

    def hanging(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host == "pi.hole":
            release.wait(10)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", hanging)
    started = time.monotonic()
    try:
        with pytest.raises(ConnectionFailed, match=r"could not resolve host 'pi\.hole'") as info:
            PiholeClient("http://pi.hole", PASSWORD, timeout=0.3).login()
    finally:
        release.set()
    assert time.monotonic() - started < 2
    assert info.value.hint
    assert "DNS server" in info.value.hint


def test_slow_dns_lookup_within_the_timeout(
    fake_pihole: FakePihole, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    real_getaddrinfo = socket.getaddrinfo

    def slow(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host == "pi.hole":
            time.sleep(0.2)
            host = "127.0.0.1"
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    with PiholeClient(f"http://pi.hole:{fake_pihole.port}", PASSWORD, timeout=2) as client:
        assert client.get("dns/blocking")
    assert fake_pihole.requests[0].headers["Host"] == f"pi.hole:{fake_pihole.port}"
    assert fake_pihole.active_sessions == {}


def test_non_ascii_proxy_path(fake_pihole: FakePihole) -> None:
    with pytest.raises(APIError, match="doesn't look like a Pi-hole v6 API"):
        PiholeClient(fake_pihole.url + "/piholé").probe()
    assert fake_pihole.requests[0].path == "/pihol%C3%A9/api/info/login"


def test_proxy_bypassed_for_loopback(
    fake_pihole: FakePihole, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{free_port()}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert PiholeClient(fake_pihole.url).probe()["dns"] is True


def test_proxy_env_honoured_for_remote_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    opener = PiholeClient("http://pi.hole")._get_opener()
    handlers = getattr(opener, "handlers", [])
    proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies
    assert getattr(proxies[0], "proxies", {}).get("http") == "http://proxy.example:3128"


def test_proxy_resolves_the_pihole_host(
    fake_pihole: FakePihole, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through a proxy only the proxy's name is looked up locally (the fake is the proxy)."""
    real_getaddrinfo = socket.getaddrinfo
    looked_up: list[Any] = []

    def recording(host: Any, *args: Any, **kwargs: Any) -> Any:
        looked_up.append(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", recording)
    monkeypatch.setenv("http_proxy", f"http://localhost:{fake_pihole.port}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert PiholeClient("http://pi.hole.invalid:8080", timeout=2).probe()["dns"] is True
    assert "pi.hole.invalid" not in looked_up
    assert "localhost" in looked_up


def test_opener_is_built_once() -> None:
    client = PiholeClient("http://pi.hole")
    assert client._get_opener() is client._get_opener()


class _Raising:
    """Builds clients whose transport raises a given exception."""

    def __init__(self, url: str = "http://pi.hole") -> None:
        self.client = PiholeClient(url, timeout=1)

    def error_for(self, exc: BaseException) -> ConnectionFailed:
        return self.client._network_error(exc, 1.0)


def test_network_error_mapping() -> None:
    raising = _Raising()
    assert "refused" in str(raising.error_for(ConnectionRefusedError(111, "refused")))
    assert "within 1s" in str(raising.error_for(TimeoutError("timed out")))
    dropped = raising.error_for(RemoteDisconnected("gone"))
    assert "closed the connection" in str(dropped)
    reset = raising.error_for(ConnectionResetError(104, "reset"))
    assert "closed the connection" in str(reset)
    other = raising.error_for(OSError(113, "No route to host"))
    assert str(other) == "could not connect to pi.hole: No route to host"
    plain = raising.error_for(OSError("weird"))
    assert "weird" in str(plain)
    assert type(raising.error_for(ValueError())) is ConnectionFailed


def test_tls_error_mapping() -> None:
    raising = _Raising("https://pi.hole")
    cert = ssl.SSLCertVerificationError(1, "certificate verify failed")
    cert.verify_message = "Hostname mismatch, certificate is not valid for 'pi.hole'."
    mismatch = raising.error_for(cert)
    assert isinstance(mismatch, TLSError)
    assert mismatch.hint
    assert "--insecure" in mismatch.hint
    assert "pi.hole" in mismatch.hint
    generic = raising.error_for(ssl.SSLError(1, "[SSL] sslv3 alert handshake failure"))
    assert isinstance(generic, TLSError)
    assert generic.hint == "check that this port serves HTTPS"
    dropped = raising.error_for(RemoteDisconnected("gone"))
    assert dropped.hint
    assert "https://" not in dropped.hint


def test_urlerror_with_string_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    client = PiholeClient("http://pi.hole")

    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            raise urllib.error.URLError("unknown url type: weird")

    monkeypatch.setattr(client, "_opener", Opener())
    with pytest.raises(ConnectionFailed, match="unknown url type"):
        client.probe()


def test_read_timeout_during_body(monkeypatch: pytest.MonkeyPatch) -> None:
    client = PiholeClient("http://pi.hole", timeout=0.5)

    class Response:
        status, reason, headers = 200, "OK", None

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def read(self, limit: int) -> bytes:
            raise TimeoutError("timed out")

    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            return Response()

    monkeypatch.setattr(client, "_opener", Opener())
    with pytest.raises(ConnectionFailed, match=r"within 0\.5s"):
        client.probe()


# --------------------------------------------------------------------------- #
# TLS against the fake server                                                  #
# --------------------------------------------------------------------------- #
@pytest.fixture
def https_pihole(fake_pihole_factory: Callable[..., FakePihole]) -> FakePihole:
    return fake_pihole_factory(https=True)


def test_tls_handshakes_take_turns(
    https_pihole: FakePihole, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pi-hole FTL up to at least v6.5 fails, and can segfault, on parallel TLS
    # handshakes; every HTTPS connection must hand-shake under the shared lock
    # while the requests themselves still run in parallel.
    import time

    import pihello.client as client_module
    from pihello import sources

    real = threading.Lock()
    guard = threading.Lock()
    state = {"inside": 0, "most": 0, "entries": 0}

    class Tracking:
        def __enter__(self) -> None:
            real.acquire()
            with guard:
                state["inside"] += 1
                state["entries"] += 1
                state["most"] = max(state["most"], state["inside"])
            time.sleep(0.005)  # widen the window in which an overlap would show

        def __exit__(self, *exc: object) -> None:
            with guard:
                state["inside"] -= 1
            real.release()

    monkeypatch.setattr(client_module, "_TLS_HANDSHAKE_LOCK", Tracking())
    assert https_pihole.ca_cert_path is not None
    with PiholeClient(https_pihole.url, PASSWORD, verify=str(https_pihole.ca_cert_path)) as c:
        _, problems = sources.collect(c, None)
    assert not problems
    planned = len(sources.plan_all())
    assert state["entries"] >= planned + 1  # the login and every fetch hand-shook under the lock
    assert state["most"] == 1


def test_https_with_ca_bundle(https_pihole: FakePihole) -> None:
    assert https_pihole.ca_cert_path is not None
    with PiholeClient(https_pihole.url, PASSWORD, verify=str(https_pihole.ca_cert_path)) as c:
        assert c.get("dns/blocking")
    assert https_pihole.active_sessions == {}


def test_https_untrusted_certificate(https_pihole: FakePihole) -> None:
    with pytest.raises(TLSError, match="certificate verification failed") as info:
        PiholeClient(https_pihole.url, PASSWORD).login()
    assert info.value.hint
    assert "--ca-cert" in info.value.hint
    assert "--insecure" in info.value.hint


def test_https_insecure(https_pihole: FakePihole) -> None:
    with PiholeClient(https_pihole.url, PASSWORD, verify=False) as client:
        assert client.get("dns/blocking")


def test_https_hostname_mismatch(https_pihole: FakePihole, monkeypatch: pytest.MonkeyPatch) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        return real_getaddrinfo("127.0.0.1" if host == "pi.hole" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert https_pihole.ca_cert_path is not None
    url = f"https://pi.hole:{https_pihole.port}"
    client = PiholeClient(url, PASSWORD, verify=str(https_pihole.ca_cert_path))
    with pytest.raises(TLSError, match=r"not valid for 'pi\.hole'") as info:
        client.login()
    assert info.value.hint
    assert "--insecure" in info.value.hint


def test_https_to_plain_http_port(fake_pihole: FakePihole) -> None:
    url = fake_pihole.url.replace("http://", "https://")
    with pytest.raises(TLSError, match="TLS handshake") as info:
        PiholeClient(url, PASSWORD, verify=False).login()
    assert info.value.hint


def test_plain_http_to_https_port(https_pihole: FakePihole) -> None:
    url = https_pihole.url.replace("https://", "http://")
    with pytest.raises(ConnectionFailed) as info:
        PiholeClient(url, PASSWORD, timeout=2).login()
    assert not isinstance(info.value, TLSError)


# --------------------------------------------------------------------------- #
# Logging                                                                      #
# --------------------------------------------------------------------------- #
def test_debug_logging_never_leaks_secrets(
    fake_pihole: FakePihole, client: PiholeClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="pihello")
    with client:
        sid = client.sid
        client.get("dns/blocking")
        with pytest.raises(EndpointNotFound):
            client.get("does/not/exist")
    fake_pihole.stop()
    with pytest.raises(ConnectionFailed):
        client.get("dns/blocking")
    text = caplog.text
    assert "POST /api/auth -> 200" in text
    assert "GET /api/dns/blocking -> 200" in text
    assert "GET /api/does/not/exist -> 404" in text
    assert "failed after" in text
    assert PASSWORD not in text
    assert sid is not None
    assert sid not in text


# --------------------------------------------------------------------------- #
# Recorded login failures from every Pi-hole version                           #
# --------------------------------------------------------------------------- #
_LOGIN_VARIANTS: dict[str, type[Exception]] = {
    "wrong-password": AuthenticationError,
    "empty-password": AuthenticationError,
    "totp-required": TwoFactorRequired,
    "totp-wrong-password": TwoFactorRequired,
    "totp-invalid": TwoFactorRequired,
    "seats-exceeded": TooManySessions,
    "login-rate-limited": AuthenticationError,
}


def _recorded_login_variants() -> list[Any]:
    from helpers.fakepihole import available_fixtures, load_fixture

    cases = []
    for tag in available_fixtures():
        responses = load_fixture(tag)["responses"]
        for variant, expected in _LOGIN_VARIANTS.items():
            recorded = responses.get(f"POST /api/auth#{variant}")
            if recorded is not None:
                cases.append(pytest.param(recorded, expected, id=f"{tag}-{variant}"))
    return cases


@pytest.mark.parametrize(("recorded", "expected"), _recorded_login_variants())
def test_recorded_login_failures(
    fake_pihole: FakePihole, recorded: dict[str, Any], expected: type[Exception]
) -> None:
    import json

    raw = json.dumps(recorded["body"]).encode()
    fake_pihole.fail("POST auth", status=recorded["status"], raw=raw)
    client, _ = no_sleep_client(fake_pihole.url)
    with pytest.raises(expected) as info:
        client.login()
    assert type(info.value) is expected
    assert info.value.hint  # type: ignore[attr-defined]
