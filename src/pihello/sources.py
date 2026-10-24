"""Template variables: namespaces, the endpoints that provide them, and transforms.

Templates reference *paths* such as ``queries.total``; the first segment is a
*namespace*. Each :class:`Source` maps one Pi-hole API endpoint to the
namespaces it provides. :func:`plan` picks the minimal set of sources for a
template, :func:`fetch` downloads them in parallel and :func:`collect` adds the
derived values and builtins.

Transforms remove Pi-hole's ``took`` field, normalise per-version differences
(so that variable paths are stable across Pi-hole v6.0 … v6.x) and coerce
documented ``float`` values to ``float`` (Pi-hole sends ``0`` instead of
``0.0``). :func:`catalog` documents every variable.
"""

from __future__ import annotations

import functools
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pihello import __version__
from pihello.errors import APIError, AuthenticationError, ConnectionFailed, PiholeError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from pihello.client import PiholeClient

__all__ = [
    "BUILTIN_NAMESPACES",
    "NAMESPACES",
    "SOURCES",
    "FetchProblem",
    "Source",
    "VariableInfo",
    "builtins",
    "catalog",
    "collect",
    "fetch",
    "flatten",
    "from_responses",
    "plan",
    "plan_all",
]


@dataclass(frozen=True)
class Source:
    """One Pi-hole API endpoint and the template namespaces it provides."""

    name: str
    """Short identifier, e.g. ``"summary"``."""
    endpoint: str
    """Path below ``/api``; may include a query string."""
    namespaces: tuple[str, ...]
    """Top-level variable names this source provides."""
    transform: Callable[[Any], dict[str, Any]]
    """Raw JSON payload → ``{namespace: value}``."""
    description: str
    """Human-readable summary (shown by ``pihello vars``)."""


@dataclass(frozen=True)
class VariableInfo:
    """Documentation of one variable path."""

    path: str
    """Dotted path; list items use ``N``: ``top_domains.N.domain``."""
    type: str
    """One of ``int``, ``float``, ``str``, ``bool``, ``timestamp`` (unix seconds),
    ``list`` (of objects), ``list[str]`` or ``list[float]``."""
    description: str
    """What the value means, including its unit."""
    source: str
    """:attr:`Source.name`, ``"derived"`` or ``"builtin"``."""


@dataclass(frozen=True)
class FetchProblem:
    """A source that could not be fetched (its namespaces stay missing)."""

    source: Source
    error: PiholeError


# --------------------------------------------------------------------------- #
# Transform helpers                                                            #
# --------------------------------------------------------------------------- #
def _body(raw: Any) -> dict[str, Any]:
    """Return the top-level JSON object without ``took``."""
    if not isinstance(raw, dict):
        raise TypeError(f"expected a JSON object, got {type(raw).__name__}")
    return {key: value for key, value in raw.items() if key != "took"}


def _member(raw: Any, key: str) -> Any:
    """Return ``raw[key]``, raising a descriptive error when it is absent."""
    body = _body(raw)
    if key not in body:
        raise KeyError(f"missing {key!r} in response")
    return body[key]


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"expected {what} to be an object, got {type(value).__name__}")
    return cast("dict[str, Any]", value)


def _list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"expected {what} to be a list, got {type(value).__name__}")
    return value


def _summary(raw: Any) -> dict[str, Any]:
    body = _body(raw)
    return {
        "queries": _object(body.get("queries"), "queries"),
        "clients": _object(body.get("clients"), "clients"),
        "gravity": _object(body.get("gravity"), "gravity"),
    }


_BLOCKING_STATES = frozenset({"enabled", "disabled", "failed", "unknown"})


def _blocking(raw: Any) -> dict[str, Any]:
    body = _body(raw)
    state = body.get("blocking")
    if isinstance(state, bool):
        state = "enabled" if state else "disabled"
    elif not isinstance(state, str) or state not in _BLOCKING_STATES:
        state = "unknown"
    return {"blocking": state, "timer": body.get("timer")}


def _recent_blocked(raw: Any) -> dict[str, Any]:
    domains = [str(domain) for domain in _list(_member(raw, "blocked"), "blocked")]
    return {"recent_blocked": domains[0] if domains else "", "recent_blocked_list": domains}


_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)")


def _version_tuple(value: str) -> tuple[int, ...] | None:
    match = _VERSION_RE.fullmatch(value.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _known(value: Any) -> Any:
    """Map Pi-hole's "unknown" markers (``"null"``, ``""``) to ``None``.

    Without internet access Pi-hole reports ``remote.version: null`` and, for
    Docker, the *string* ``"null"``.
    """
    if isinstance(value, str) and value.strip() in ("", "null"):
        return None
    return value


def _is_newer(local: Any, remote: Any) -> bool | None:
    """Whether ``remote`` is newer than ``local``; ``None`` if either is unknown.

    Comparable versions (``v6.10`` vs ``v6.9``, ``2026.10.0``) compare numerically,
    anything else (``vDev-eaa7dbb``) counts as an update when it differs.
    """
    if not isinstance(remote, str) or not isinstance(local, str):
        return None
    local_tuple, remote_tuple = _version_tuple(local), _version_tuple(remote)
    if local_tuple is not None and remote_tuple is not None:
        return remote_tuple > local_tuple
    return local.strip() != remote.strip()


def _component_update(component: Mapping[str, Any]) -> bool | None:
    """Pi-hole's own rule: compare versions on ``master``, commit hashes on other branches.

    ``None`` when the answer is unknown (no remote information, e.g. offline, or
    the component doesn't apply, e.g. ``docker`` outside Docker).
    """
    local, remote = component.get("local"), component.get("remote")
    if not isinstance(local, dict) or not isinstance(remote, dict):
        return _is_newer(local, remote)  # Docker: plain tag strings
    branch = local.get("branch")
    if isinstance(branch, str) and branch not in ("", "master"):
        local_hash, remote_hash = local.get("hash"), remote.get("hash")
        if not isinstance(local_hash, str) or not isinstance(remote_hash, str):
            return None
        return local_hash != remote_hash
    return _is_newer(local.get("version"), remote.get("version"))


def _version(raw: Any) -> dict[str, Any]:
    components = _object(_member(raw, "version"), "version")
    version: dict[str, Any] = {}
    outdated: list[str] = []
    for name, component in components.items():
        if not isinstance(component, dict):
            version[name] = component
            continue
        entry = {
            key: {k: _known(v) for k, v in value.items()}
            if isinstance(value, dict)
            else _known(value)
            for key, value in component.items()
        }
        entry["update"] = _component_update(entry)
        version[name] = entry
        if entry["update"] is True:
            outdated.append(name)
    docker = version.get("docker")
    in_docker = isinstance(docker, dict) and isinstance(docker.get("local"), str)
    if in_docker:
        # In Docker, components are only updated by pulling a new image.
        outdated = [name for name in outdated if name == "docker"]
    counted = [
        entry
        for name, entry in version.items()
        if isinstance(entry, dict) and (name == "docker") == in_docker
    ]
    if outdated:
        status = "available"
    elif any(entry.get("update") is not None for entry in counted):
        status = "current"
    else:  # e.g. offline, GitHub rate limit, right after start-up
        status = "unknown"
    return {
        "version": version,
        "updates": {
            "available": bool(outdated),
            "count": len(outdated),
            "components": outdated,
            "status": status,
        },
    }


def _system(raw: Any) -> dict[str, Any]:
    return {"system": _object(_member(raw, "system"), "system")}


def _host(raw: Any) -> dict[str, Any]:
    return {"host": _object(_member(raw, "host"), "host")}


def _sensors(raw: Any) -> dict[str, Any]:
    return {"sensors": _object(_member(raw, "sensors"), "sensors")}


def _ftl(raw: Any) -> dict[str, Any]:
    ftl = dict(_object(_member(raw, "ftl"), "ftl"))
    database = ftl.get("database")
    if isinstance(database, dict):
        database = dict(database)
        # Pi-hole v6.0 reported plain totals; later versions {"total", "enabled"}.
        for group in ("domains", "regex"):
            kinds = database.get(group)
            if isinstance(kinds, dict):
                database[group] = {
                    kind: {"total": count} if isinstance(count, (int, float)) else count
                    for kind, count in kinds.items()
                }
        ftl["database"] = database
    uptime = ftl.get("uptime")
    if isinstance(uptime, (int, float)) and not isinstance(uptime, bool):
        ftl["uptime"] = uptime / 1000.0  # Pi-hole reports milliseconds
    return {"ftl": ftl}


def _database(raw: Any) -> dict[str, Any]:
    return {"database": _body(raw)}


def _messages(raw: Any) -> dict[str, Any]:
    return {"messages": {"count": _member(raw, "count")}}


def _top_domains(namespace: str) -> Callable[[Any], dict[str, Any]]:
    def transform(raw: Any) -> dict[str, Any]:
        domains = _list(_member(raw, "domains"), "domains")
        return {namespace: [dict(_object(item, "domain")) for item in domains]}

    transform.__name__ = f"_{namespace}"
    return transform


def _client_label(item: Mapping[str, Any]) -> str:
    name = item.get("name")
    return str(name) if name else str(item.get("ip") or "")


def _top_clients(namespace: str) -> Callable[[Any], dict[str, Any]]:
    def transform(raw: Any) -> dict[str, Any]:
        clients = []
        for item in _list(_member(raw, "clients"), "clients"):
            entry = dict(_object(item, "client"))
            entry["label"] = _client_label(entry)
            clients.append(entry)
        return {namespace: clients}

    transform.__name__ = f"_{namespace}"
    return transform


def _upstream_label(item: Mapping[str, Any]) -> str:
    base = _client_label(item)
    port = item.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and port > 0:
        return f"{base}#{port}"
    return base


_PSEUDO_UPSTREAMS = frozenset({"blocklist", "cache"})


def _upstreams(raw: Any) -> dict[str, Any]:
    upstreams = []
    for item in _list(_member(raw, "upstreams"), "upstreams"):
        entry = dict(_object(item, "upstream"))
        if entry.get("ip") in _PSEUDO_UPSTREAMS:
            continue  # FTL's pseudo-destinations; see queries.blocked / queries.cached
        entry["label"] = _upstream_label(entry)
        upstreams.append(entry)
    return {"upstreams": upstreams}


def _history(raw: Any) -> dict[str, Any]:
    history = _list(_member(raw, "history"), "history")
    return {"history": [dict(_object(item, "history entry")) for item in history]}


def _network(raw: Any) -> dict[str, Any]:
    routes = [item for item in _list(_member(raw, "gateway"), "gateway") if isinstance(item, dict)]
    ipv4 = [route for route in routes if route.get("family") in ("inet", "ipv4")]
    ordered = ipv4 + [route for route in routes if route not in ipv4]
    addresses: list[str] = []
    for route in ordered:
        for address in route.get("local") or ():
            if isinstance(address, str) and address not in addresses:
                addresses.append(address)
    chosen: Mapping[str, Any] = ordered[0] if ordered else {}
    local = [a for a in chosen.get("local") or () if isinstance(a, str)]
    return {
        "network": {
            "gateway": chosen.get("address"),
            "interface": chosen.get("interface"),
            "address": local[0] if local else (addresses[0] if addresses else None),
            "addresses": addresses,
        }
    }


# --------------------------------------------------------------------------- #
# Sources                                                                      #
# --------------------------------------------------------------------------- #
SOURCES: tuple[Source, ...] = (
    Source(
        "summary",
        "stats/summary",
        ("queries", "clients", "gravity"),
        _summary,
        "Query totals for the last 24 hours, client counts and gravity (block list) status",
    ),
    Source(
        "blocking",
        "dns/blocking",
        ("blocking", "timer"),
        _blocking,
        "Whether ad blocking is enabled, and the remaining time of a temporary toggle",
    ),
    Source(
        "recent_blocked",
        "stats/recent_blocked?count=10",
        ("recent_blocked", "recent_blocked_list"),
        _recent_blocked,
        "The most recently blocked domains",
    ),
    Source(
        "version",
        "info/version",
        ("version", "updates"),
        _version,
        "Installed and latest versions of Pi-hole Core, Web, FTL and the Docker image",
    ),
    Source(
        "system",
        "info/system",
        ("system",),
        _system,
        "Host uptime, memory, processes and CPU load",
    ),
    Source("host", "info/host", ("host",), _host, "Host name, kernel and hardware model"),
    Source("sensors", "info/sensors", ("sensors",), _sensors, "Temperature sensors"),
    Source(
        "ftl",
        "info/ftl",
        ("ftl",),
        _ftl,
        "FTL (Pi-hole's DNS engine) process, database counts and resolver metrics",
    ),
    Source(
        "database",
        "info/database",
        ("database",),
        _database,
        "Long-term query database (pihole-FTL.db) file and contents",
    ),
    Source(
        "messages",
        "info/messages/count",
        ("messages",),
        _messages,
        "Number of Pi-hole diagnosis messages",
    ),
    Source(
        "top_domains",
        "stats/top_domains?count=10",
        ("top_domains",),
        _top_domains("top_domains"),
        "The 10 most queried permitted domains",
    ),
    Source(
        "top_blocked",
        "stats/top_domains?blocked=true&count=10",
        ("top_blocked",),
        _top_domains("top_blocked"),
        "The 10 most blocked domains",
    ),
    Source(
        "top_clients",
        "stats/top_clients?count=10",
        ("top_clients",),
        _top_clients("top_clients"),
        "The 10 most active clients",
    ),
    Source(
        "top_blocked_clients",
        "stats/top_clients?blocked=true&count=10",
        ("top_blocked_clients",),
        _top_clients("top_blocked_clients"),
        "The 10 clients with the most blocked queries",
    ),
    Source(
        "upstreams",
        "stats/upstreams",
        ("upstreams",),
        _upstreams,
        "Upstream DNS servers and how often each was used",
    ),
    Source(
        "history",
        "history",
        ("history",),
        _history,
        "Query activity over the last 24 hours in 10-minute buckets",
    ),
    Source(
        "network",
        "network/gateway",
        ("network",),
        _network,
        "Default gateway, interface and the Pi-hole's own addresses",
    ),
)

BUILTIN_NAMESPACES: frozenset[str] = frozenset({"now", "pihello", "pihole"})
"""Namespaces that are always available without contacting Pi-hole."""

_BY_NAMESPACE: dict[str, Source] = {ns: source for source in SOURCES for ns in source.namespaces}

NAMESPACES: frozenset[str] = frozenset(_BY_NAMESPACE) | BUILTIN_NAMESPACES
"""Every known top-level variable name, including builtins."""


def plan(paths: Iterable[str]) -> tuple[Source, ...]:
    """Return the minimal sources (in :data:`SOURCES` order) needed for ``paths``.

    Unknown namespaces and builtins need no source and are ignored.
    """
    namespaces = {path.strip().split(".", 1)[0] for path in paths}
    return tuple(source for source in SOURCES if any(ns in namespaces for ns in source.namespaces))


def plan_all() -> tuple[Source, ...]:
    """Return every source."""
    return SOURCES


# --------------------------------------------------------------------------- #
# Fetching                                                                     #
# --------------------------------------------------------------------------- #
def _run(client: PiholeClient, source: Source) -> dict[str, Any]:
    data = client.get(source.endpoint)
    try:
        return _coerce_floats(source.transform(data))
    except Exception as exc:  # any malformed payload becomes an APIError
        path = source.endpoint.split("?", 1)[0]
        raise APIError(
            f"unexpected response from /api/{path}: {exc}",
            path=path,
            hint="this Pi-hole version may not be supported yet; please report it",
        ) from exc


def fetch(
    client: PiholeClient, sources: Iterable[Source], *, max_workers: int = 6
) -> tuple[dict[str, Any], list[FetchProblem]]:
    """Fetch ``sources`` in parallel and return ``(variables, problems)``.

    Each failing source is recorded as a :class:`FetchProblem` and leaves its
    namespaces missing — except :class:`AuthenticationError`, which is raised
    (no point continuing with bad credentials). Once a source fails with
    :class:`ConnectionFailed` (Pi-hole unreachable or not answering in time),
    sources that haven't started yet are skipped and get the same error, so a
    stalled Pi-hole costs one ``timeout``, not one per wave of workers.
    Results are merged in source order, so the outcome is deterministic.
    """
    todo = tuple(dict.fromkeys(sources))
    outcomes: dict[Source, dict[str, Any] | PiholeError] = {}
    unreachable: ConnectionFailed | None = None
    if len(todo) <= 1 or max_workers <= 1:
        for source in todo:
            if unreachable is not None:
                outcomes[source] = unreachable
                continue
            try:
                outcomes[source] = _run(client, source)
            except AuthenticationError:
                raise
            except PiholeError as exc:
                outcomes[source] = exc
                if isinstance(exc, ConnectionFailed):
                    unreachable = exc
    else:
        import threading  # noqa: PLC0415
        from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

        first_failure: list[ConnectionFailed] = []
        lock = threading.Lock()

        def task(source: Source) -> dict[str, Any] | None:
            # The failing worker records the error before it can pick up the next
            # source, so no new request starts once Pi-hole is known to be unreachable.
            if first_failure:
                return None
            try:
                return _run(client, source)
            except ConnectionFailed as exc:
                with lock:
                    if not first_failure:
                        first_failure.append(exc)
                raise

        pool = ThreadPoolExecutor(
            max_workers=min(max_workers, len(todo)), thread_name_prefix="pihello-fetch"
        )
        try:
            futures = {pool.submit(task, source): source for source in todo}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    result = future.result()
                except AuthenticationError:
                    raise
                except PiholeError as exc:
                    outcomes[source] = exc
                else:
                    outcomes[source] = first_failure[0] if result is None else result
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    variables: dict[str, Any] = {}
    problems: list[FetchProblem] = []
    for source in todo:
        outcome = outcomes[source]
        if isinstance(outcome, PiholeError):
            problems.append(FetchProblem(source, outcome))
        else:
            variables.update(outcome)
    return variables, problems


def builtins(*, url: str | None, now: float | None = None) -> dict[str, Any]:
    """Return the builtin variables ``now``, ``pihello.*`` and ``pihole.*``."""
    host = None
    if url:
        from urllib.parse import urlsplit  # noqa: PLC0415

        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
    return {
        "now": float(time.time() if now is None else now),
        "pihello": {"version": __version__},
        "pihole": {"url": url, "host": host},
    }


def _derive(variables: dict[str, Any], now: float) -> None:
    """Add time-dependent derived values (``gravity.relative``) in place."""
    gravity = variables.get("gravity")
    if isinstance(gravity, dict):
        last = gravity.get("last_update")
        if isinstance(last, (int, float)) and not isinstance(last, bool) and last > 0:
            delta = max(0, int(now - last))
            gravity = dict(gravity)
            gravity["relative"] = {
                "days": delta // 86400,
                "hours": delta % 86400 // 3600,
                "minutes": delta % 3600 // 60,
            }
            variables["gravity"] = gravity


def _finish(variables: dict[str, Any], *, url: str | None, now: float | None) -> dict[str, Any]:
    stamp = time.time() if now is None else float(now)
    _derive(variables, stamp)
    variables.update(builtins(url=url, now=stamp))
    return variables


def collect(
    client: PiholeClient, paths: Iterable[str] | None = None, *, now: float | None = None
) -> tuple[dict[str, Any], list[FetchProblem]]:
    """Plan, fetch and assemble the variables for ``paths`` (``None`` → everything).

    Returns ``(variables, problems)``; ``variables`` includes derived values and
    :func:`builtins`. Raises :class:`AuthenticationError` and, when *every*
    planned source failed with :class:`ConnectionFailed`, that error.
    """
    sources = plan_all() if paths is None else plan(paths)
    variables, problems = fetch(client, sources)
    if (
        sources
        and len(problems) == len(sources)
        and all(isinstance(problem.error, ConnectionFailed) for problem in problems)
    ):
        raise problems[0].error
    return _finish(variables, url=client.base_url, now=now), problems


def from_responses(
    responses: Mapping[str, Any], *, url: str | None = None, now: float | None = None
) -> dict[str, Any]:
    """Build variables from raw API payloads keyed by :attr:`Source.endpoint`.

    Endpoints missing from ``responses`` leave their namespaces missing. This is
    the offline equivalent of :func:`collect` (used for demo data and tests).
    """
    variables: dict[str, Any] = {}
    for source in SOURCES:
        if source.endpoint in responses:
            variables.update(_coerce_floats(source.transform(responses[source.endpoint])))
    return _finish(variables, url=url, now=now)


def flatten(variables: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten nested variables to dotted paths.

    Dicts and lists of dicts are expanded (``top_domains.0.domain``); lists of
    scalars and empty containers are kept as leaf values.
    """
    flat: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict) and value:
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), item)
        elif isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value):
            for index, item in enumerate(value):
                walk(f"{prefix}.{index}", item)
        else:
            flat[prefix] = value

    walk("", dict(variables))
    return flat


# --------------------------------------------------------------------------- #
# Catalog                                                                      #
# --------------------------------------------------------------------------- #
# One variable per line: "path | type | description". "@name" switches the
# source for the following lines. "{A,B}" expands to one line per alternative,
# substituting "{}" in the description with the alternative.
_CATALOG = r"""
@summary
queries.total | int | DNS queries in the last 24 hours
queries.blocked | int | Queries blocked in the last 24 hours
queries.percent_blocked | float | Share of queries blocked in the last 24 hours, in percent (0-100)
queries.unique_domains | int | Distinct domains queried in the last 24 hours
queries.forwarded | int | Queries forwarded to an upstream DNS server
queries.cached | int | Queries answered from Pi-hole's cache or local configuration
queries.frequency | float | Average query rate, in queries per second
queries.types.A | int | Queries for A records (IPv4 addresses)
queries.types.AAAA | int | Queries for AAAA records (IPv6 addresses)
queries.types.ANY | int | Queries for ANY records
queries.types.SRV | int | Queries for SRV records (service locations)
queries.types.SOA | int | Queries for SOA records (zone authority)
queries.types.PTR | int | Queries for PTR records (reverse lookups)
queries.types.TXT | int | Queries for TXT records
queries.types.NAPTR | int | Queries for NAPTR records
queries.types.MX | int | Queries for MX records (mail servers)
queries.types.DS | int | Queries for DS records (DNSSEC delegation signers)
queries.types.RRSIG | int | Queries for RRSIG records (DNSSEC signatures)
queries.types.DNSKEY | int | Queries for DNSKEY records (DNSSEC keys)
queries.types.NS | int | Queries for NS records (name servers)
queries.types.SVCB | int | Queries for SVCB records (service bindings)
queries.types.HTTPS | int | Queries for HTTPS records (HTTPS service bindings)
queries.types.OTHER | int | Queries for all other record types
queries.status.UNKNOWN | int | Queries with an unknown status
queries.status.GRAVITY | int | Queries blocked by a domain on a block list (gravity)
queries.status.FORWARDED | int | Queries forwarded to an upstream server
queries.status.CACHE | int | Queries answered from the cache
queries.status.REGEX | int | Queries blocked by a regex deny filter
queries.status.DENYLIST | int | Queries blocked by an exact domain on the deny list
queries.status.EXTERNAL_BLOCKED_IP | int | Queries blocked upstream (the answer was a known blocking address)
queries.status.EXTERNAL_BLOCKED_NULL | int | Queries blocked upstream (the answer was 0.0.0.0 or ::)
queries.status.EXTERNAL_BLOCKED_NXRA | int | Queries blocked upstream (NXDOMAIN without the recursion-available flag)
queries.status.GRAVITY_CNAME | int | Queries blocked by gravity during deep CNAME inspection
queries.status.REGEX_CNAME | int | Queries blocked by a regex filter during deep CNAME inspection
queries.status.DENYLIST_CNAME | int | Queries blocked by the deny list during deep CNAME inspection
queries.status.RETRIED | int | Queries forwarded again after the first attempt got no answer
queries.status.RETRIED_DNSSEC | int | Queries retried because of DNSSEC validation
queries.status.IN_PROGRESS | int | Queries still waiting for an upstream reply
queries.status.DBBUSY | int | Queries blocked because the gravity database was busy
queries.status.SPECIAL_DOMAIN | int | Queries for special domains (e.g. Apple Private Relay or Mozilla's DoH canary)
queries.status.CACHE_STALE | int | Queries answered from expired (stale) cache entries
queries.status.EXTERNAL_BLOCKED_EDE15 | int | Queries blocked upstream (Extended DNS Error 15, "blocked")
queries.replies.UNKNOWN | int | Replies of unknown type (e.g. query still in progress)
queries.replies.NODATA | int | NODATA replies (the name exists but has no record of that type)
queries.replies.NXDOMAIN | int | NXDOMAIN replies (the domain does not exist)
queries.replies.CNAME | int | CNAME replies
queries.replies.IP | int | Replies with an IP address
queries.replies.DOMAIN | int | Replies with a domain name (e.g. reverse lookups)
queries.replies.RRNAME | int | Replies with a resource record name
queries.replies.SERVFAIL | int | SERVFAIL replies (upstream failure)
queries.replies.REFUSED | int | REFUSED replies
queries.replies.NOTIMP | int | NOTIMP replies (query type not implemented)
queries.replies.OTHER | int | Replies of other types
queries.replies.DNSSEC | int | DNSSEC replies
queries.replies.NONE | int | Queries that got no reply (e.g. dropped)
queries.replies.BLOB | int | Replies with binary data
clients.active | int | Clients that sent queries in the last 24 hours
clients.total | int | Clients FTL has seen since it started (including inactive ones)
gravity.domains_being_blocked | int | Domains on Pi-hole's block lists (gravity)
gravity.last_update | timestamp | When gravity (the block lists) was last updated; 0 if unknown
@derived
gravity.relative.days | int | Whole days since the last gravity update (pihello 1.x compatible)
gravity.relative.hours | int | Hours (0-23) since the last gravity update, after the whole days
gravity.relative.minutes | int | Minutes (0-59) since the last gravity update, after the whole hours
@blocking
blocking | str | Blocking state: "enabled", "disabled", "failed" or "unknown"
timer | float | Seconds until a temporary enable/disable is reverted; null without a timer
@derived
recent_blocked | str | The most recently blocked domain ("" if none yet)
@recent_blocked
recent_blocked_list | list[str] | The 10 most recent blocked queries' domains, newest first (a domain may repeat, e.g. A + AAAA)
@version
version.{core,web}.local.version | str | Installed Pi-hole {} version (e.g. "v6.5")
version.{core,web}.local.branch | str | Git branch of the installed Pi-hole {} ("master" for releases)
version.{core,web}.local.hash | str | Git commit of the installed Pi-hole {}
version.{core,web}.remote.version | str | Latest released Pi-hole {} version on GitHub (null if unknown, e.g. offline, or on custom branches)
version.{core,web}.remote.hash | str | Git commit of the latest Pi-hole {} release (null if unknown, e.g. offline)
version.ftl.local.version | str | Installed FTL version (e.g. "v6.8"; development builds report e.g. "vDev-eaa7dbb")
version.ftl.local.branch | str | Git branch of the installed FTL ("master" for releases)
version.ftl.local.hash | str | Git commit of the installed FTL
version.ftl.local.date | str | Build time of the installed FTL (e.g. "2026-10-17 13:08:09 +0200")
version.ftl.remote.version | str | Latest released FTL version on GitHub (null if unknown, e.g. offline, or on custom branches)
version.ftl.remote.hash | str | Git commit of the latest FTL release (null if unknown, e.g. offline)
version.docker.local | str | Docker image tag (e.g. "2026.10.0"); null when not running in Docker
version.docker.remote | str | Latest Docker image tag on Docker Hub; null if unknown or not running in Docker
@derived
version.{core,web,ftl}.update | bool | Whether a newer {} release is available (non-master branches: new commits); null if unknown, e.g. offline
version.docker.update | bool | Whether a newer Docker image is available; null if unknown or not running in Docker
updates.available | bool | Whether an update is known to be available (in Docker only the image counts)
updates.count | int | Number of components known to have an update available
updates.components | list[str] | Components with an update available ("core", "web", "ftl", "docker")
updates.status | str | "available" (an update is known to be available), "current" (no update known) or "unknown" (no update information yet, e.g. offline or rate-limited by GitHub)
@system
system.uptime | int | Host uptime in seconds
system.memory.ram.total | int | Total RAM in KiB
system.memory.ram.free | int | Unused RAM in KiB
system.memory.ram.used | int | Used RAM in KiB
system.memory.ram.available | int | RAM available to new programs (free + reclaimable cache) in KiB
system.memory.ram.%used | float | Used RAM in percent
system.memory.swap.total | int | Total swap space in KiB
system.memory.swap.free | int | Unused swap space in KiB
system.memory.swap.used | int | Used swap space in KiB
system.memory.swap.%used | float | Used swap space in percent (0 without swap)
system.procs | int | Number of running processes
system.cpu.nprocs | int | Number of CPU cores
system.cpu.%cpu | float | Total CPU usage in percent as reported by FTL (FTL v6.1+); raw value, can be far above 100 or negative when FTL cannot compute it
system.cpu.load.raw | list[float] | 1, 5 and 15 minute load averages (index with .0, .1, .2)
system.cpu.load.percent | list[float] | 1, 5 and 15 minute load averages relative to the core count, in percent
system.ftl.%mem | float | Share of total RAM used by FTL, in percent (FTL v6.3+; see ftl.%mem)
system.ftl.%cpu | float | CPU used by FTL, in percent (FTL v6.3+; see ftl.%cpu)
@host
host.uname.domainname | str | NIS domain name of the host (usually "(none)")
host.uname.machine | str | CPU architecture (e.g. "aarch64", "x86_64")
host.uname.nodename | str | Host name (the container ID when running in Docker)
host.uname.release | str | Kernel release (e.g. "6.12.47+rpt-rpi-2712")
host.uname.sysname | str | Operating system name ("Linux")
host.uname.version | str | Kernel build string
host.model | str | Device model (e.g. "Raspberry Pi 5 Model B Rev 1.0"); null if unknown
host.dmi.bios.vendor | str | BIOS vendor (null if unknown, e.g. on a Raspberry Pi)
host.dmi.board.name | str | Mainboard name (null if unknown)
host.dmi.board.vendor | str | Mainboard vendor (null if unknown)
host.dmi.board.version | str | Mainboard version (null if unknown)
host.dmi.product.name | str | Product name (null if unknown)
host.dmi.product.family | str | Product family (null if unknown)
host.dmi.product.version | str | Product version (null if unknown)
host.dmi.sys.vendor | str | System vendor (null if unknown)
@sensors
sensors.list | list | Temperature sensors found on the host (may be empty, e.g. in a container with FTL v6.7.0 or older)
sensors.list.N.name | str | Sensor driver name (e.g. "cpu_thermal", "k10temp")
sensors.list.N.path | str | Short sysfs path of the sensor (e.g. "hwmon0")
sensors.list.N.source | str | Device the sensor belongs to (sysfs path)
sensors.list.N.temps | list | Temperature readings of this sensor
sensors.list.N.temps.N.name | str | Reading label (e.g. "Tctl"; null if unlabelled)
sensors.list.N.temps.N.value | float | Current temperature in sensors.unit
sensors.list.N.temps.N.max | float | Maximum temperature in sensors.unit (null if unknown)
sensors.list.N.temps.N.crit | float | Critical temperature in sensors.unit (null if unknown)
sensors.list.N.temps.N.sensor | str | Reading id (e.g. "temp1")
sensors.cpu_temp | float | CPU temperature in sensors.unit (best guess); null if FTL found no CPU sensor (always in containers before FTL v6.7.1)
sensors.hot_limit | float | Temperature above which Pi-hole considers the CPU hot, in sensors.unit
sensors.unit | str | Temperature unit: "C", "F" or "K"
@ftl
ftl.database.gravity | int | Exact domains on block lists (gravity)
ftl.database.antigravity | int | Exact domains on allow lists (antigravity) (FTL v6.7+)
ftl.database.groups | int | Number of groups
ftl.database.lists | int | Number of block/allow lists (adlists)
ftl.database.clients | int | Number of configured clients
ftl.database.domains.allowed.total | int | Exact domains on the allow list (FTL < v6.3 reports only totals)
ftl.database.domains.allowed.enabled | int | Enabled exact domains on the allow list (FTL v6.3+)
ftl.database.domains.denied.total | int | Exact domains on the deny list (FTL < v6.3 reports only totals)
ftl.database.domains.denied.enabled | int | Enabled exact domains on the deny list (FTL v6.3+)
ftl.database.regex.allowed.total | int | Regex allow filters (FTL < v6.3 reports only totals)
ftl.database.regex.allowed.enabled | int | Enabled regex allow filters (FTL v6.3+)
ftl.database.regex.denied.total | int | Regex deny filters (FTL < v6.3 reports only totals)
ftl.database.regex.denied.enabled | int | Enabled regex deny filters (FTL v6.3+)
ftl.privacy_level | int | Privacy level: 0 (show everything) to 3 (anonymous mode)
ftl.query_frequency | float | Average query rate, in queries per second
ftl.clients.total | int | Clients FTL has seen since it started
ftl.clients.active | int | Clients that sent queries in the last 24 hours
ftl.pid | int | Process ID of pihole-FTL
ftl.uptime | float | FTL uptime in seconds (Pi-hole reports milliseconds; pihello converts)
ftl.%mem | float | Share of total RAM used by FTL, in percent
ftl.%cpu | float | CPU used by FTL, in percent (ten-second average)
ftl.allow_destructive | bool | Whether FTL allows destructive API actions (e.g. flushing logs)
ftl.dnsmasq.dns_cache_inserted | int | Entries inserted into the DNS cache
ftl.dnsmasq.dns_cache_live_freed | int | Live DNS cache entries evicted to make room
ftl.dnsmasq.dns_queries_forwarded | int | DNS queries forwarded upstream
ftl.dnsmasq.dns_auth_answered | int | DNS queries answered for authoritative zones
ftl.dnsmasq.dns_local_answered | int | DNS queries answered locally (cache or configuration)
ftl.dnsmasq.dns_stale_answered | int | DNS queries answered from stale cache entries
ftl.dnsmasq.dns_unanswered | int | DNS queries left unanswered
ftl.dnsmasq.dnssec_max_crypto_use | int | DNSSEC: highest crypto work for a single query
ftl.dnsmasq.dnssec_max_sig_fail | int | DNSSEC: highest signature failures for a single RRset
ftl.dnsmasq.dnssec_max_work | int | DNSSEC: highest subqueries for a single query
ftl.dnsmasq.bootp | int | BOOTP requests
ftl.dnsmasq.pxe | int | PXE requests
ftl.dnsmasq.dhcp_ack | int | DHCP ACKs sent
ftl.dnsmasq.dhcp_decline | int | DHCP DECLINEs received
ftl.dnsmasq.dhcp_discover | int | DHCP DISCOVERs received
ftl.dnsmasq.dhcp_inform | int | DHCP INFORMs received
ftl.dnsmasq.dhcp_nak | int | DHCP NAKs sent
ftl.dnsmasq.dhcp_offer | int | DHCP OFFERs sent
ftl.dnsmasq.dhcp_release | int | DHCP RELEASEs received
ftl.dnsmasq.dhcp_request | int | DHCP REQUESTs received
ftl.dnsmasq.noanswer | int | DHCP requests left unanswered (rapid commit)
ftl.dnsmasq.leases_allocated_4 | int | IPv4 DHCP leases allocated
ftl.dnsmasq.leases_pruned_4 | int | IPv4 DHCP leases pruned
ftl.dnsmasq.leases_allocated_6 | int | IPv6 DHCP leases allocated
ftl.dnsmasq.leases_pruned_6 | int | IPv6 DHCP leases pruned
ftl.dnsmasq.tcp_connections | int | Dedicated TCP worker processes
ftl.dnsmasq.dhcp_leasequery | int | DHCP LEASEQUERY requests (FTL v6.2+)
ftl.dnsmasq.dhcp_lease_unassigned | int | DHCP LEASEQUERY answers "unassigned" (FTL v6.2+)
ftl.dnsmasq.dhcp_lease_actve | int | DHCP LEASEQUERY answers "active" (FTL v6.2+; FTL's spelling)
ftl.dnsmasq.dhcp_lease_unknown | int | DHCP LEASEQUERY answers "unknown" (FTL v6.2+)
@database
database.size | int | Size of the query database file in bytes
database.type | str | File type of the database (e.g. "Regular file")
database.mode | str | File permissions of the database (e.g. "rw-r-----")
database.atime | timestamp | When the database file was last accessed
database.mtime | timestamp | When the database file was last modified
database.ctime | timestamp | When the database file's status last changed
database.owner.user.uid | int | User ID owning the database file
database.owner.user.name | str | User name owning the database file
database.owner.user.info | str | GECOS info of the owning user
database.owner.group.gid | int | Group ID owning the database file
database.owner.group.name | str | Group name owning the database file
database.queries | int | Queries held in FTL's in-memory database; before FTL v6.4, in the on-disk database
database.earliest_timestamp | timestamp | Oldest query in the in-memory database; 0 if empty (FTL v6.4+)
database.queries_disk | int | Queries stored in the on-disk database (FTL v6.4+)
database.earliest_timestamp_disk | timestamp | Oldest query in the on-disk database; 0 if empty (FTL v6.4+)
database.sqlite_version | str | Version of the SQLite engine embedded in FTL
@messages
messages.count | int | Number of Pi-hole diagnosis messages (Tools > Pi-hole diagnosis)
@top_domains
top_domains | list | The 10 most queried permitted domains (last 24 hours), most queried first
top_domains.N.domain | str | Domain name
top_domains.N.count | int | Number of queries for this domain
@top_blocked
top_blocked | list | The 10 most blocked domains (last 24 hours), most blocked first
top_blocked.N.domain | str | Domain name
top_blocked.N.count | int | Number of blocked queries for this domain
@top_clients
top_clients | list | The 10 clients with the most queries (last 24 hours)
top_clients.N.name | str | Client host name ("" if unknown)
top_clients.N.ip | str | Client IP address (IPv4 or IPv6)
top_clients.N.count | int | Number of queries from this client
@derived
top_clients.N.label | str | Client host name, or its IP address if the name is unknown
@top_blocked_clients
top_blocked_clients | list | The 10 clients with the most blocked queries (last 24 hours)
top_blocked_clients.N.name | str | Client host name ("" if unknown)
top_blocked_clients.N.ip | str | Client IP address (IPv4 or IPv6)
top_blocked_clients.N.count | int | Number of blocked queries from this client
@derived
top_blocked_clients.N.label | str | Client host name, or its IP address if the name is unknown
@upstreams
upstreams | list | Upstream DNS servers, busiest first (FTL's "blocklist" and "cache" pseudo-entries are left out)
upstreams.N.ip | str | Upstream server address (IPv4 or IPv6)
upstreams.N.name | str | Upstream server host name ("" if unknown)
upstreams.N.port | int | Upstream server port (e.g. 53)
upstreams.N.count | int | Queries forwarded to this upstream server
upstreams.N.statistics.response | float | Average response time in seconds
upstreams.N.statistics.variance | float | Standard deviation of the response time in seconds
@derived
upstreams.N.label | str | Display name: "name#port", or "ip#port" when the host name is unknown
@history
history | list | Query counts over the last 24 hours in 10-minute buckets, oldest first
history.N.timestamp | timestamp | Center of the 10-minute bucket
history.N.total | int | Queries in this bucket
history.N.cached | int | Cached queries in this bucket
history.N.blocked | int | Blocked queries in this bucket
history.N.forwarded | int | Forwarded queries in this bucket
@network
network.gateway | str | Default gateway address (IPv4 preferred; null if the route has none)
network.interface | str | Interface of the default route (e.g. "eth0"; null for multipath routes)
network.address | str | The Pi-hole's own address on the default-route interface
network.addresses | list[str] | All addresses of the default-route interfaces, IPv4 first
@builtin
now | timestamp | Current time (unix seconds) when the variables were collected
pihello.version | str | pihello version
pihole.url | str | Pi-hole base URL pihello is connected to (e.g. "http://pi.hole")
pihole.host | str | Host name or address from pihole.url
"""  # noqa: E501

_ALTERNATIVES_RE = re.compile(r"\{([^{}]+)\}")
_DISPLAY_NAMES = {"core": "Core", "web": "Web", "ftl": "FTL", "docker": "Docker"}


@functools.cache
def catalog() -> tuple[VariableInfo, ...]:
    """Document every variable path (list items use ``N``), in display order."""
    entries: list[VariableInfo] = []
    source = ""
    for line in _CATALOG.strip().splitlines():
        if line.startswith("@"):
            source = line[1:]
            continue
        path, type_, description = (part.strip() for part in line.split("|", 2))
        match = _ALTERNATIVES_RE.search(path)
        if match is None:
            entries.append(VariableInfo(path, type_, description, source))
            continue
        for option in match.group(1).split(","):
            expanded = path[: match.start()] + option + path[match.end() :]
            text = description.replace("{}", _DISPLAY_NAMES.get(option, option))
            entries.append(VariableInfo(expanded, type_, text, source))
    return tuple(entries)


@functools.cache
def _float_patterns() -> frozenset[str]:
    return frozenset(info.path for info in catalog() if info.type in ("float", "list[float]"))


def _coerce_floats(values: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``values`` with ints converted wherever the catalog says float."""
    patterns = _float_patterns()

    def walk(pattern: str, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value) if pattern in patterns else value
        if isinstance(value, dict):
            return {key: walk(f"{pattern}.{key}", item) for key, item in value.items()}
        if isinstance(value, list):
            item_pattern = pattern if pattern in patterns else f"{pattern}.N"
            return [walk(item_pattern, item) for item in value]
        return value

    return {namespace: walk(namespace, value) for namespace, value in values.items()}
