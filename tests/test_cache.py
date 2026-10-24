"""Tests for :mod:`pihello.cache`."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pihello import __version__, cache

VARIABLES: dict[str, Any] = {
    "queries": {"total": 48213, "percent_blocked": 15.2},
    "top_domains": [{"domain": "example.com", "count": 3}],
    "blocking": "enabled",
    "timer": None,
    "now": 1792830600.0,
}
KEY = cache.make_key("http://pi.hole", ["queries", "blocking"])

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


# --------------------------------------------------------------------------- #
# Location                                                                     #
# --------------------------------------------------------------------------- #
def test_cache_dir_uses_xdg(home: Path) -> None:
    assert cache.cache_dir() == home / ".cache" / "pihello"


def test_cache_dir_custom_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache.cache_dir() == tmp_path / "xdg" / "pihello"


@pytest.mark.parametrize("value", ["", "relative/dir"])
def test_cache_dir_ignores_empty_or_relative_xdg(
    home: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", value)
    assert cache.cache_dir() == home / ".cache" / "pihello"


def test_cache_dir_windows(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(home / "Local"))
    assert cache.cache_dir() == home / "Local" / "pihello" / "Cache"
    monkeypatch.delenv("LOCALAPPDATA")
    assert cache.cache_dir() == Path.home() / "AppData" / "Local" / "pihello" / "Cache"


# --------------------------------------------------------------------------- #
# Keys                                                                         #
# --------------------------------------------------------------------------- #
def test_make_key_is_stable_and_order_insensitive() -> None:
    a = cache.make_key("http://pi.hole", ["queries", "blocking", "queries"])
    b = cache.make_key(" http://pi.hole/ ", ("blocking", "queries"))
    assert a == b
    assert len(a) == 32
    assert int(a, 16) >= 0


def test_make_key_differs_by_url_and_namespaces() -> None:
    keys = {
        cache.make_key("http://pi.hole", ["queries"]),
        cache.make_key("http://pi.hole:8080", ["queries"]),
        cache.make_key("https://pi.hole", ["queries"]),
        cache.make_key("http://pi.hole", ["queries", "blocking"]),
        cache.make_key("http://pi.hole", []),
    }
    assert len(keys) == 5


def test_key_does_not_leak_url() -> None:
    assert "pi.hole" not in str(cache.path_for(cache.make_key("http://pi.hole", ["x"])))


@pytest.mark.parametrize("key", ["", "../../etc/passwd", "ABCDEF0123456789", "a" * 65, "xyz"])
def test_invalid_keys(key: str) -> None:
    with pytest.raises(ValueError, match="invalid cache key"):
        cache.path_for(key)
    with pytest.raises(ValueError, match="invalid cache key"):
        cache.load(key, 60)


# --------------------------------------------------------------------------- #
# store / load                                                                 #
# --------------------------------------------------------------------------- #
def test_round_trip() -> None:
    cache.store(KEY, VARIABLES)
    assert cache.load(KEY, 60) == VARIABLES
    document = json.loads(cache.path_for(KEY).read_text())
    assert set(document) == {"format", "pihello", "created", "variables"}
    assert document["format"] == cache.FORMAT_VERSION
    assert document["pihello"] == __version__


def test_ttl_zero_or_negative_disables_cache() -> None:
    cache.store(KEY, VARIABLES)
    assert cache.load(KEY, 0) is None
    assert cache.load(KEY, -5) is None


def test_missing_entry() -> None:
    assert cache.load(KEY, 60) is None


def test_expired_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    cache.store(KEY, VARIABLES)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61)
    assert cache.load(KEY, 60) is None
    assert cache.load(KEY, 120) == VARIABLES


def test_entry_from_the_future_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    cache.store(KEY, VARIABLES)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 3600)
    assert cache.load(KEY, 10**9) is None


def _write(document: Any) -> None:
    path = cache.path_for(KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document))


@pytest.mark.parametrize(
    "document",
    [
        "{not json",
        "",
        "\xff",
        [],
        {"format": 99, "pihello": __version__, "created": 1e18, "variables": {}},
        {"format": 1, "pihello": "0.0.1", "created": 1e18, "variables": {}},
        {"format": 1, "pihello": __version__, "created": "now", "variables": {}},
        {"format": 1, "pihello": __version__, "created": True, "variables": {}},
        {"format": 1, "pihello": __version__, "created": 1e18, "variables": []},
        {"format": 1, "pihello": __version__, "variables": {}},
    ],
)
def test_corrupt_or_foreign_entries_are_ignored(document: Any) -> None:
    _write(document)
    assert cache.load(KEY, 10**12) is None


def test_unreadable_entry_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    cache.store(KEY, VARIABLES)

    def boom(self: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert cache.load(KEY, 60) is None


def test_store_overwrites_atomically() -> None:
    cache.store(KEY, {"a": 1})
    cache.store(KEY, VARIABLES)
    assert cache.load(KEY, 60) == VARIABLES
    leftovers = [p.name for p in cache.cache_dir().iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_store_serialises_unusual_values() -> None:
    cache.store(KEY, {"path": Path("/x"), "n": 1})
    assert cache.load(KEY, 60) == {"path": "/x", "n": 1}


@posix_only
def test_permissions() -> None:
    cache.store(KEY, VARIABLES)
    path = cache.path_for(KEY)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_store_failure_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    directory = cache.cache_dir()
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.write_text("a file where the directory should be")
    caplog.set_level("DEBUG", logger="pihello")
    cache.store(KEY, VARIABLES)
    assert "could not write cache file" in caplog.text
    assert cache.load(KEY, 60) is None


def test_failed_replace_removes_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: Path, target: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    cache.store(KEY, VARIABLES)
    assert list(cache.cache_dir().iterdir()) == []


def test_interrupted_write_removes_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(self: Path, target: Any) -> Path:
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cache.store(KEY, VARIABLES)
    assert list(cache.cache_dir().iterdir()) == []


def test_store_never_writes_credentials() -> None:
    cache.store(KEY, VARIABLES)
    text = cache.path_for(KEY).read_text()
    for secret in ("password", "sid", "X-FTL-SID"):
        assert secret not in text


# --------------------------------------------------------------------------- #
# clear                                                                        #
# --------------------------------------------------------------------------- #
def test_clear() -> None:
    assert cache.clear() == 0  # no directory yet
    other_key = cache.make_key("http://other", ["x"])
    cache.store(KEY, VARIABLES)
    cache.store(other_key, VARIABLES)
    directory = cache.cache_dir()
    (directory / f".{KEY}.123.456.tmp").write_text("partial")
    (directory / "notes.txt").write_text("keep me")
    (directory / "not-a-key.json").write_text("keep me too")
    assert cache.clear() == 2
    assert sorted(p.name for p in directory.iterdir()) == ["not-a-key.json", "notes.txt"]
    assert cache.load(KEY, 60) is None
    assert cache.clear() == 0


def test_clear_skips_undeletable_files(monkeypatch: pytest.MonkeyPatch) -> None:
    cache.store(KEY, VARIABLES)

    def refuse(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("busy")

    monkeypatch.setattr(Path, "unlink", refuse)
    assert cache.clear() == 0
