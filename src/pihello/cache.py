"""Optional on-disk cache of collected variables.

With ``cache_ttl`` > 0 the CLI stores the variables returned by
:func:`pihello.sources.collect` and reuses them for ``cache_ttl`` seconds, so
opening many shells in a row doesn't hit Pi-hole every time.

- Location: ``$XDG_CACHE_HOME/pihello/`` (``~/.cache/pihello/``), or
  ``%LOCALAPPDATA%\\pihello\\Cache`` on Windows.
- One JSON file per key (URL + namespaces), mode ``0600`` in a ``0700``
  directory, written atomically (temporary file + :func:`os.replace`).
- Only variables are stored — never passwords or session ids.
- Unreadable, corrupt, expired or foreign-version files are ignored, and write
  failures are logged at debug level instead of breaking the greeting.

Cached variables include the builtins (``now``) and derived values
(``gravity.relative``) as of the time they were stored; callers that need a
current ``now`` should refresh it after :func:`load`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pihello import __version__

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["FORMAT_VERSION", "cache_dir", "clear", "load", "make_key", "path_for", "store"]

FORMAT_VERSION = 1
"""Version of the cache file format; files with another version are ignored."""

_KEY_RE = re.compile(r"^[0-9a-f]{16,64}$")
_SUFFIX = ".json"
_MAX_FUTURE_SKEW = 60.0


def cache_dir() -> Path:
    """Return pihello's cache directory (it may not exist yet)."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "pihello" / "Cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".cache"
    return base / "pihello"


def make_key(url: str, namespaces: Iterable[str]) -> str:
    """Return the cache key for a Pi-hole URL and the set of namespaces fetched.

    The key is a hash, so neither the URL nor anything else leaks into file names.
    """
    import hashlib  # noqa: PLC0415 - only needed when caching is enabled

    material = json.dumps(
        {"format": FORMAT_VERSION, "url": url.strip().rstrip("/"), "ns": sorted(set(namespaces))},
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def path_for(key: str) -> Path:
    """Return the cache file for ``key``; :class:`ValueError` for malformed keys."""
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid cache key {key!r}")
    return cache_dir() / f"{key}{_SUFFIX}"


def load(key: str, ttl: float) -> dict[str, Any] | None:
    """Return the cached variables for ``key`` if younger than ``ttl`` seconds.

    Returns ``None`` when caching is off (``ttl <= 0``) or the entry is missing,
    expired, from the future, corrupt or written by another pihello version.
    """
    if ttl <= 0:
        return None
    path = path_for(key)
    try:
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    created = document.get("created")
    variables = document.get("variables")
    if (
        document.get("format") != FORMAT_VERSION
        or document.get("pihello") != __version__
        or isinstance(created, bool)
        or not isinstance(created, (int, float))
        or not isinstance(variables, dict)
    ):
        return None
    age = time.time() - created
    if age > ttl or age < -_MAX_FUTURE_SKEW:
        return None
    return variables


def store(key: str, variables: Mapping[str, Any]) -> None:
    """Atomically write ``variables`` for ``key``; failures are logged, not raised."""
    path = path_for(key)
    document = {
        "format": FORMAT_VERSION,
        "pihello": __version__,
        "created": time.time(),
        "variables": dict(variables),
    }
    temporary = path.with_name(f".{path.stem}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        payload = json.dumps(document, separators=(",", ":"), default=str).encode("utf-8")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    except (OSError, TypeError, ValueError) as exc:
        import logging  # noqa: PLC0415 - only on failure

        logging.getLogger("pihello").debug("could not write cache file %s: %s", path, exc)


def clear() -> int:
    """Delete every cache entry (and stray temporary files); return the number of entries."""
    directory = cache_dir()
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return 0
    removed = 0
    for candidate in candidates:
        is_entry = candidate.suffix == _SUFFIX and _KEY_RE.match(candidate.stem) is not None
        is_temporary = candidate.name.startswith(".") and candidate.suffix == ".tmp"
        if not (is_entry or is_temporary):
            continue
        try:
            candidate.unlink()
        except OSError:
            continue
        removed += is_entry
    return removed
