"""Tests for the fake Pi-hole server used throughout the test suite."""

from __future__ import annotations

import base64
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest

from helpers import fakepihole
from helpers.fakepihole import (
    FakePihole,
    available_fixtures,
    latest_fixture,
    load_fixture,
    normalize_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PASSWORD = "pihello-test"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class Reply:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.raw = body

    @property
    def json(self) -> Any:
        return json.loads(self.raw) if self.raw else None


def call(
    fake: FakePihole,
    method: str,
    path: str,
    *,
    body: Any = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5,
    context: ssl.SSLContext | None = None,
) -> Reply:
    if body is not None:
        data = json.dumps(body).encode()
    request = urllib.request.Request(
        fake.url + path, data=data, method=method, headers=headers or {}
    )
    opener = _OPENER
    if context is not None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
        )
    try:
        with opener.open(request, timeout=timeout) as response:
            return Reply(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as error:
        with error:
            return Reply(error.code, dict(error.headers), error.read())


def login(fake: FakePihole, password: str = PASSWORD, **extra: Any) -> Reply:
    return call(fake, "POST", "/api/auth", body={"password": password, **extra})


def sid_of(reply: Reply) -> str:
    sid = reply.json["session"]["sid"]
    assert isinstance(sid, str)
    return sid


# --------------------------------------------------------------------------- #
# Lifecycle                                                                    #
# --------------------------------------------------------------------------- #
def test_lifecycle() -> None:
    fake = FakePihole()
    with pytest.raises(RuntimeError, match="not running"):
        _ = fake.url
    with pytest.raises(RuntimeError, match="not running"):
        _ = fake.port
    started = time.monotonic()
    with fake:
        assert time.monotonic() - started < 1
        port = fake.port
        assert fake.url == f"http://127.0.0.1:{port}"
        fake.start()  # idempotent
        assert call(fake, "GET", "/api/info/login").status == 200
    leftovers = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("FakePihole") or "process_request" in thread.name
    ]
    assert leftovers == []
    fake.stop()  # idempotent
    with pytest.raises(urllib.error.URLError):
        _OPENER.open(f"http://127.0.0.1:{port}/api/info/login", timeout=1)


def test_stop_interrupts_delays(fake_pihole: FakePihole) -> None:
    fake_pihole.fail("info/version", delay=30)
    errors: list[BaseException] = []

    def slow() -> None:
        try:
            call(fake_pihole, "GET", "/api/info/version", timeout=60)
        except Exception as exc:  # the server goes away mid-request
            errors.append(exc)

    worker = threading.Thread(target=slow)
    worker.start()
    time.sleep(0.2)
    started = time.monotonic()
    fake_pihole.stop()
    worker.join(10)
    assert time.monotonic() - started < 5


# --------------------------------------------------------------------------- #
# Fixture loading                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("stats/summary", "GET /api/stats/summary"),
        ("/api/stats/summary", "GET /api/stats/summary"),
        ("api/stats/summary/", "GET /api/stats/summary"),
        ("POST auth", "POST /api/auth"),
        (
            "GET /api/stats/top_domains?count=10&blocked=true",
            "GET /api/stats/top_domains?blocked=true&count=10",
        ),
        ("DELETE /api/auth#session-unknown", "DELETE /api/auth#session-unknown"),
        ("GET /api/x?#v", "GET /api/x#v"),
    ],
)
def test_normalize_key(key: str, expected: str) -> None:
    assert normalize_key(key) == expected


def test_demo_fixture() -> None:
    fixture = load_fixture("demo")
    assert fixture["meta"]["docker_tag"] == "demo"
    assert "GET /api/stats/summary" in fixture["responses"]
    assert "GET /api/stats/top_domains?blocked=true&count=10" in fixture["responses"]
    assert "GET /api/info/login" in fixture["responses"]


def test_latest_falls_back_to_demo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(fakepihole, "FIXTURES_DIR", tmp_path / "none")
    assert available_fixtures() == []
    assert latest_fixture() is None
    assert load_fixture("latest")["meta"]["docker_tag"] == "demo"


def _write_fixture(directory: Path, tag: str, responses: dict[str, Any]) -> Path:
    path = directory / tag / "responses.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"meta": {"docker_tag": tag}, "responses": responses}))
    return path


def test_fixture_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(fakepihole, "FIXTURES_DIR", tmp_path)
    body = {"status": 200, "body": {"blocking": "enabled", "timer": None}}
    for tag in ("2025.10.0", "2025.02.0", "2025.4.1"):
        _write_fixture(tmp_path, tag, {"GET /api/dns/blocking": body, "junk": 1})
    assert available_fixtures() == ["2025.02.0", "2025.4.1", "2025.10.0"]
    assert latest_fixture() == "2025.10.0"
    (tmp_path / "index.json").write_text(json.dumps({"latest": "2025.4.1"}))
    assert latest_fixture() == "2025.4.1"
    assert load_fixture("latest")["meta"]["docker_tag"] == "2025.4.1"
    (tmp_path / "index.json").write_text(json.dumps({"latest": "9999.1.0"}))
    assert latest_fixture() == "2025.10.0"
    (tmp_path / "index.json").write_text("not json")
    assert latest_fixture() == "2025.10.0"
    loaded = load_fixture("2025.02.0")
    assert loaded["responses"] == {"GET /api/dns/blocking": body}
    assert load_fixture(tmp_path / "2025.02.0")["meta"]["docker_tag"] == "2025.02.0"
    file_path = tmp_path / "2025.02.0" / "responses.json"
    assert load_fixture(str(file_path))["meta"]["docker_tag"] == "2025.02.0"
    with pytest.raises(FileNotFoundError, match=r"2025\.02\.0"):
        load_fixture("1999.1.0")


@pytest.mark.parametrize("tag", available_fixtures())
def test_recorded_fixture_files_are_well_formed(tag: str) -> None:
    fixture = load_fixture(tag)
    responses = fixture["responses"]
    assert "GET /api/stats/summary" in responses
    for key, value in responses.items():
        assert key == normalize_key(key)
        assert isinstance(value["status"], int)


# --------------------------------------------------------------------------- #
# Authentication                                                               #
# --------------------------------------------------------------------------- #
def test_login_and_sessions(fake_pihole: FakePihole) -> None:
    reply = login(fake_pihole)
    assert reply.status == 200
    session = reply.json["session"]
    assert session["valid"] is True
    assert session["validity"] == 1800
    sid = session["sid"]
    assert len(sid) == 24
    assert sid.endswith("=")
    assert base64.b64decode(sid)
    assert sid in fake_pihole.active_sessions
    assert fake_pihole.active_sessions[sid].csrf == session["csrf"]
    assert login(fake_pihole).json["session"]["sid"] != sid

    status = call(fake_pihole, "GET", "/api/auth", headers={"X-FTL-SID": sid})
    assert status.status == 200
    assert status.json["session"]["sid"] == sid

    assert call(fake_pihole, "DELETE", "/api/auth", headers={"X-FTL-SID": sid}).status == 204
    assert sid not in fake_pihole.active_sessions
    unknown = call(fake_pihole, "DELETE", "/api/auth", headers={"X-FTL-SID": sid})
    assert unknown.status == 401
    assert unknown.json["session"]["message"] == "session unknown"
    missing = call(fake_pihole, "DELETE", "/api/auth")
    assert missing.json["session"]["message"] == "no SID provided"
    assert call(fake_pihole, "GET", "/api/auth").status == 401


def test_wrong_password(fake_pihole: FakePihole) -> None:
    reply = login(fake_pihole, "nope")
    assert reply.status == 401
    assert reply.json["session"]["valid"] is False
    assert reply.json["session"]["totp"] is False
    assert fake_pihole.active_sessions == {}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "No request body data"),
        (b"notjson", "no valid JSON"),
        (b'{"pw": 1}', "No password found"),
        (b"[1]", "no valid JSON"),
    ],
)
def test_bad_login_requests(fake_pihole: FakePihole, data: bytes, message: str) -> None:
    reply = call(fake_pihole, "POST", "/api/auth", data=data)
    assert reply.status == 400
    assert message in reply.json["error"]["message"]


def test_unsupported_auth_method(fake_pihole: FakePihole) -> None:
    assert call(fake_pihole, "PUT", "/api/auth", body={}).status == 404


def test_totp(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(totp=True)
    required = login(fake)
    assert required.status in (400, 401)
    assert "2FA" in json.dumps(required.json)
    wrong = login(fake, "nope")
    assert wrong.status == 401
    assert wrong.json["session"]["totp"] is True
    invalid = login(fake, totp=111111)
    assert invalid.status == 401
    assert "2FA" in invalid.json["error"]["message"]
    ok = login(fake, totp=int(fake.totp_code))
    assert ok.status == 200
    assert ok.json["session"]["totp"] is True


def test_seat_limit(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(max_sessions=2)
    first, _ = login(fake), login(fake)
    third = login(fake)
    assert third.status == 429
    assert third.json["error"]["key"] == "api_seats_exceeded"
    call(fake, "DELETE", "/api/auth", headers={"X-FTL-SID": sid_of(first)})
    assert login(fake).status == 200


def test_passwordless(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(password=None)
    reply = login(fake, "anything")
    assert reply.status == 200
    assert reply.json["session"] == {**reply.json["session"], "valid": True, "sid": None}
    assert call(fake, "GET", "/api/stats/summary").status == 200
    assert call(fake, "GET", "/api/auth").json["session"]["valid"] is True
    assert call(fake, "DELETE", "/api/auth").status == 401
    assert fake.active_sessions == {}


def test_passwordless_defaults_without_recorded_variants(
    fake_pihole_factory: Callable[..., FakePihole],
) -> None:
    fake = fake_pihole_factory("demo", password=None)
    assert call(fake, "GET", "/api/auth").status == 200
    assert call(fake, "DELETE", "/api/auth").status == 401


@pytest.mark.parametrize(
    "transport",
    ["x-ftl-sid", "X-FTL-SID", "sid-header", "query", "json-body"],
)
def test_session_transports(fake_pihole: FakePihole, transport: str) -> None:
    sid = sid_of(login(fake_pihole))
    path, headers, body = "/api/dns/blocking", {}, None
    if transport.lower() == "x-ftl-sid":
        headers = {transport: sid}
    elif transport == "sid-header":
        headers = {"sid": sid}
    elif transport == "query":
        path += f"?sid={quote(sid, safe='')}"
    else:
        body = {"sid": sid}
    reply = call(fake_pihole, "GET", path, headers=headers, body=body)
    assert reply.status == 200
    assert reply.json["blocking"] == "enabled"


def test_cookie_is_rejected(fake_pihole: FakePihole) -> None:
    session = login(fake_pihole).json["session"]
    cookie = {"Cookie": f"sid={session['sid']}", "X-FTL-CSRF": session["csrf"]}
    assert call(fake_pihole, "GET", "/api/dns/blocking", headers=cookie).status == 401


def test_unauthenticated_and_invalid_sessions(fake_pihole: FakePihole) -> None:
    missing = call(fake_pihole, "GET", "/api/stats/summary")
    assert missing.status == 401
    assert missing.json["error"]["key"] == "unauthorized"
    invalid = call(fake_pihole, "GET", "/api/info/ftl", headers={"X-FTL-SID": "AAAA="})
    assert invalid.status == 401
    assert invalid.json["error"]["key"] == "unauthorized"
    assert call(fake_pihole, "GET", "/api/info/login").status == 200


def test_session_expiry(fake_pihole: FakePihole) -> None:
    sid = sid_of(login(fake_pihole))
    fake_pihole.active_sessions[sid].last_used -= 3600
    assert call(fake_pihole, "GET", "/api/dns/blocking", headers={"X-FTL-SID": sid}).status == 401
    assert sid not in fake_pihole.active_sessions
    sid = sid_of(login(fake_pihole))
    fake_pihole.expire_sessions()
    assert call(fake_pihole, "GET", "/api/dns/blocking", headers={"X-FTL-SID": sid}).status == 401


# --------------------------------------------------------------------------- #
# Routing                                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def sid(fake_pihole: FakePihole) -> str:
    return sid_of(login(fake_pihole))


def get(fake: FakePihole, path: str, sid: str) -> Reply:
    return call(fake, "GET", path, headers={"X-FTL-SID": sid})


def test_not_found(fake_pihole: FakePihole, sid: str) -> None:
    reply = get(fake_pihole, "/api/does/not/exist", sid)
    assert reply.status == 404
    assert reply.json["error"]["key"] == "not_found"
    assert reply.json["error"]["hint"] == "/api/does/not/exist"
    unauthenticated = call(fake_pihole, "GET", "/api/does/not/exist")
    assert unauthenticated.status == 404
    html = call(fake_pihole, "GET", "/admin/")
    assert html.status == 404
    assert html.headers["Content-Type"] == "text/html"


def test_not_found_without_recorded_404(
    fake_pihole_factory: Callable[..., FakePihole],
) -> None:
    fake = fake_pihole_factory("demo")
    reply = get(fake, "/api/nope", sid_of(login(fake)))
    assert reply.json == {
        "error": {"key": "not_found", "message": "Not found", "hint": "/api/nope"},
        "took": reply.json["took"],
    }


def test_query_matching(fake_pihole: FakePihole, sid: str) -> None:
    blocked = get(fake_pihole, "/api/stats/top_domains?count=10&blocked=true", sid).json
    permitted = get(fake_pihole, "/api/stats/top_domains?count=10", sid).json
    expected = load_fixture(fake_pihole.fixture)["responses"]
    assert blocked == expected["GET /api/stats/top_domains?blocked=true&count=10"]["body"]
    assert permitted == expected["GET /api/stats/top_domains?count=10"]["body"]
    assert get(fake_pihole, "/api/stats/top_domains?count=5", sid).json == permitted
    assert get(fake_pihole, "/api/stats/top_domains?blocked=true", sid).json == blocked
    assert get(fake_pihole, "/api/stats/summary?anything=1", sid).status == 200


def test_set_response(fake_pihole: FakePihole, sid: str) -> None:
    fake_pihole.set_response("stats/summary", 200, {"overridden": True})
    fake_pihole.set_response("GET /api/brand/new?x=1", 201, {"new": True})
    fake_pihole.set_response("info/raw", 200, b"raw bytes")
    assert get(fake_pihole, "/api/stats/summary", sid).json == {"overridden": True}
    created = get(fake_pihole, "/api/brand/new?x=1", sid)
    assert (created.status, created.json) == (201, {"new": True})
    raw = get(fake_pihole, "/api/info/raw", sid)
    assert raw.raw == b"raw bytes"
    assert raw.headers["Content-Type"] == "text/plain"


def test_remove(fake_pihole: FakePihole, sid: str) -> None:
    fake_pihole.remove("network/gateway")
    fake_pihole.remove("stats/top_domains?blocked=true&count=10")
    assert get(fake_pihole, "/api/network/gateway", sid).status == 404
    assert call(fake_pihole, "GET", "/api/network/gateway").status == 404
    assert get(fake_pihole, "/api/stats/top_domains?count=10&blocked=true", sid).status == 404
    assert get(fake_pihole, "/api/stats/top_domains?count=10", sid).status == 200


def test_fail_status_raw_and_headers(fake_pihole: FakePihole, sid: str) -> None:
    fake_pihole.fail("stats/summary", status=500)
    fake_pihole.fail("info/version", raw=b"<html>hi</html>")
    fake_pihole.fail("GET /api/info/host", status=308, raw=b"", headers={"Location": "https://x"})
    error = get(fake_pihole, "/api/stats/summary", sid)
    assert error.status == 500
    assert error.json["error"]["key"] == "injected"
    html = get(fake_pihole, "/api/info/version", sid)
    assert (html.status, html.raw, html.headers["Content-Type"]) == (
        200,
        b"<html>hi</html>",
        "text/html",
    )
    redirect = get(fake_pihole, "/api/info/host", sid)
    assert (redirect.status, redirect.headers["Location"]) == (308, "https://x")
    fake_pihole.clear_faults()
    assert get(fake_pihole, "/api/stats/summary", sid).status == 200


def test_fail_matching_by_method_and_query(fake_pihole: FakePihole, sid: str) -> None:
    fake_pihole.fail("POST auth", status=503)
    fake_pihole.fail("stats/top_domains?blocked=true&count=10", status=500)
    assert get(fake_pihole, "/api/auth", sid).status == 200
    assert login(fake_pihole).status == 503
    assert get(fake_pihole, "/api/stats/top_domains?blocked=true&count=10", sid).status == 500
    assert get(fake_pihole, "/api/stats/top_domains?count=10", sid).status == 200


def test_fail_times(fake_pihole: FakePihole, sid: str) -> None:
    fake_pihole.fail("dns/blocking", status=500, times=2)
    statuses = [get(fake_pihole, "/api/dns/blocking", sid).status for _ in range(3)]
    assert statuses == [500, 500, 200]


def test_fail_delay_then_normal_response(fake_pihole: FakePihole, sid: str) -> None:
    fake_pihole.fail("dns/blocking", delay=0.2)
    started = time.monotonic()
    reply = get(fake_pihole, "/api/dns/blocking", sid)
    assert reply.status == 200
    assert time.monotonic() - started >= 0.2


def test_fail_drop(fake_pihole: FakePihole, sid: str) -> None:
    fake_pihole.fail("dns/blocking", drop=True)
    with pytest.raises((RemoteDisconnected, ConnectionError, urllib.error.URLError)):
        get(fake_pihole, "/api/dns/blocking", sid)


def test_requests_are_recorded(fake_pihole: FakePihole, sid: str) -> None:
    get(fake_pihole, "/api/stats/summary?a=1", sid)
    request = fake_pihole.requests_to("stats/summary")[0]
    assert request.method == "GET"
    assert request.path == "/api/stats/summary"
    assert request.endpoint == "stats/summary"
    assert request.query == {"a": "1"}
    assert request.sid == sid
    assert request.headers["X-FTL-SID"] == sid
    assert request.headers["x-ftl-sid"] == sid
    assert "X-Ftl-Sid" in request.headers
    unusual_key: object = 42
    assert unusual_key not in request.headers
    login_request = fake_pihole.requests_to("auth", "POST")[0]
    assert login_request.json == {"password": PASSWORD}
    assert login_request.body == json.dumps({"password": PASSWORD}).encode()
    assert fake_pihole.requests_to("auth", "GET") == []


def test_https(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(https=True)
    assert fake.url.startswith("https://127.0.0.1:")
    assert fake.ca_cert_path is not None
    ca_path = fake.ca_cert_path
    context = ssl.create_default_context(cafile=str(ca_path))
    assert call(fake, "GET", "/api/info/login", context=context).status == 200
    with pytest.raises(urllib.error.URLError):
        call(fake, "GET", "/api/info/login")
    fake.stop()
    assert ca_path_of(fake) is None
    assert not ca_path.exists()


def ca_path_of(fake: FakePihole) -> Path | None:
    return fake.ca_cert_path


# --------------------------------------------------------------------------- #
# Replay fidelity for every recorded version                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tag", [*available_fixtures(), "demo"])
def test_replays_recorded_get_responses(
    tag: str, fake_pihole_factory: Callable[..., FakePihole]
) -> None:
    fake = fake_pihole_factory(tag)
    token = sid_of(login(fake))
    responses = load_fixture(tag)["responses"]
    for key, recorded in responses.items():
        method, _, target = key.partition(" ")
        if method != "GET" or "#" in key or target.startswith("/api/auth"):
            continue
        reply = get(fake, target, token)
        assert reply.status == recorded["status"], key
        if recorded["status"] != 404:
            assert reply.json == recorded["body"], key


def test_recorder_style_variants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Variants recorded by scripts/record_fixtures.py are replayed sensibly."""
    monkeypatch.setattr(fakepihole, "FIXTURES_DIR", tmp_path)
    unauthorized = {"error": {"key": "unauthorized", "message": "Unauthorized", "hint": None}}
    not_found = {
        "error": {"key": "not_found", "message": "Not found", "hint": "/api/does/not/exist"}
    }
    _write_fixture(
        tmp_path,
        "2030.1.0",
        {
            "GET /api/info/ftl": {"status": 200, "body": {"ftl": {}}},
            "GET /api/does/not/exist": {"status": 404, "body": not_found},
            "GET /api/does/not/exist#unauthenticated": {"status": 404, "body": not_found},
            "GET /api/stats/summary#unauthenticated": {"status": 401, "body": unauthorized},
            "DELETE /api/auth#unauthenticated": {
                "status": 401,
                "body": {"session": {"valid": False, "message": "no SID provided"}},
            },
            "POST /api/auth#totp-wrong-password": {
                "status": 401,
                "body": {"session": {"valid": False, "totp": True, "message": "recorded"}},
            },
        },
    )
    with FakePihole("2030.1.0", totp=True) as fake:
        reply = call(fake, "GET", "/api/info/ftl")
        assert (reply.status, reply.json) == (401, unauthorized)
        missing = call(fake, "GET", "/api/x/y")
        assert missing.status == 404
        assert missing.json["error"]["hint"] == "/api/x/y"
        assert login(fake, "wrong").json["session"]["message"] == "recorded"


# --------------------------------------------------------------------------- #
# Rate limits and wrong methods                                                #
# --------------------------------------------------------------------------- #
def test_login_rate_limit(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(login_rate_limit=3)
    now = [1000.2]
    fake.clock = lambda: now[0]
    fake.rate_limit_delay = 0
    assert [login(fake).status for _ in range(2)] == [200, 200]
    assert login(fake, "wrong").status == 401  # wrong passwords count too
    limited = login(fake)
    assert limited.status == 429
    assert limited.json["error"]["key"] == "rate_limiting"
    assert len(fake.active_sessions) == 2
    now[0] = 1001.0
    assert login(fake).status == 200
    assert call(fake, "POST", "/api/auth", data=b"").status == 400  # not a password check


def test_app_password_rate_limit_reports_ftl_took(
    fake_pihole_factory: Callable[..., FakePihole],
) -> None:
    """FTL's ``took`` (measured on 2026.10.0): 0.287-0.290 s for an app-password check
    over the limit (it includes the 250 ms pause), 0.07 s for an honest miss (two
    password checks). Reported whatever ``rate_limit_delay`` the fake really waits."""
    fake = fake_pihole_factory(login_rate_limit=3, app_password="app-secret")
    now = [1000.0]
    fake.clock = lambda: now[0]
    fake.rate_limit_delay = 0
    assert login(fake, "app-secret").status == 200  # checks 1 and 2
    disguised = login(fake, "app-secret")  # check 3 passes, the app-password check 4 doesn't
    assert disguised.status == 401
    assert disguised.json["session"]["message"] == "password incorrect"
    assert disguised.json["took"] == pytest.approx(0.288, abs=0.01)
    now[0] += 1
    honest = login(fake, "wrong")
    assert honest.status == 401
    assert honest.json["took"] == pytest.approx(0.07, abs=0.01)


def test_login_rate_limit_is_off_by_default(fake_pihole: FakePihole) -> None:
    assert [login(fake_pihole).status for _ in range(6)] == [200] * 6


def test_login_rate_limit_pauses_like_ftl(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(login_rate_limit=0)
    fake.clock = lambda: 5.0
    started = time.monotonic()
    assert login(fake).status == 429
    assert time.monotonic() - started >= 0.25


def test_totp_rate_limit(fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(totp=True, totp_rate_limit=True)
    now = [30 * 1000 + 1.0]
    fake.clock = lambda: now[0]
    assert login(fake, totp=111111).status == 401
    limited = login(fake, totp=int(fake.totp_code))
    recorded = load_fixture(fake.fixture)["responses"].get("POST /api/auth#totp-rate-limited")
    assert limited.status == (recorded["status"] if recorded else 429)
    now[0] += 30
    assert login(fake, totp=int(fake.totp_code)).status == 200


@pytest.mark.parametrize("tag", [*available_fixtures(), "demo"])
def test_wrong_method(tag: str, fake_pihole_factory: Callable[..., FakePihole]) -> None:
    fake = fake_pihole_factory(tag)
    token = sid_of(login(fake))
    reply = call(fake, "DELETE", "/api/info/version", headers={"X-FTL-SID": token})
    recorded = load_fixture(tag)["responses"].get("PUT /api/info/version#method-not-allowed")
    if recorded is not None and recorded["status"] == 405:
        assert reply.status == 405
        assert reply.json["error"] == {**reply.json["error"], "key": "method_not_allowed"}
        assert reply.json["error"]["hint"] == "DELETE"
    else:
        assert reply.status == 404
        assert reply.json["error"]["hint"] == "/api/info/version"
    assert call(fake, "DELETE", "/api/no/such/thing").status == 404


def test_meta_is_exposed_and_unknown_keys_ignored() -> None:
    with FakePihole("latest") as fake:
        assert isinstance(fake.meta, dict)
        if fake.fixture != "demo" and latest_fixture() is not None:
            assert "docker_tag" in fake.meta
        assert call(fake, "GET", "/api/info/login").status == 200
