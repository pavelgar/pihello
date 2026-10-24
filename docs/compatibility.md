# Pi-hole v6 compatibility

pihello supports every Pi-hole **v6.x** release.
This page is for contributors and other API client authors; to use pihello, you don't need any of it.
This page records what was *observed* against real Pi-hole containers (one Docker tag per FTL minor release, plus the very first v6 image) by `scripts/record_fixtures.py`.
The recordings live in `tests/fixtures/pihole/<docker-tag>/responses.json` (index: `tests/fixtures/pihole/index.json`) and are what `tests/helpers/fakepihole.py` replays.
Every statement below is backed by a recorded response unless it says "per FTL source".

Recorded on 2026-10-24 with rootless podman 5.8, `TZ=UTC`, `FTLCONF_dns_listeningMode=ALL`.
Regenerate the machine-readable parts of this page with `uv run python scripts/record_fixtures.py --report`.

**On this page:** [Tested versions](#tested-versions) · [Quick reference for API clients](#quick-reference-for-api-clients) ·
[Authentication and sessions](#authentication-and-sessions-in-detail) · [Endpoint availability](#endpoint-availability) ·
[Field changes by version](#field-changes-by-version) · [Value quirks](#value-quirks) · [TLS](#tls) ·
[Test infrastructure](#behaviour-that-matters-for-test-infrastructure) · [How the fixtures are recorded](#how-the-fixtures-are-recorded)

## Tested versions

Versions are what `GET /api/info/version` reports (`version.<component>.local.version`).

| Docker tag | Core | Web | FTL | FTL minor |
| :-- | :-- | :-- | :-- | :-- |
| `2025.02.0` | v6.0 | v6.0 | vDev-eaa7dbb ¹ | v6.0 |
| `2025.03.0` | v6.0.5 | v6.0.2 | v6.0.4 | v6.0 |
| `2025.04.0` | v6.0.6 | v6.1 | v6.1 | v6.1 |
| `2025.08.0` | v6.1.4 | v6.2.1 | v6.2.3 | v6.2 |
| `2025.11.0` | v6.2.2 | v6.3 | v6.3.3 | v6.3 |
| `2025.11.1` | v6.3 | v6.4 | v6.4.1 | v6.4 |
| `2026.02.0` | v6.4 | v6.4.1 | v6.5 | v6.5 |
| `2026.06.0` | v6.4.2 | v6.5.1 | v6.6.2 | v6.6 |
| `2026.07.2` | v6.4.3 | v6.6 | v6.7 | v6.7 |
| `2026.09.0` | v6.4.3 | v6.6 | v6.7.1 | v6.7 (security release) |
| `2026.10.0` | v6.5 | v6.6.1 | v6.8 | v6.8 (current) |

¹ The first v6 image ships the v6.0 FTL binary but reports a development version string (fixed in `2025.02.1`, "cosmetic bug only").

> [!IMPORTANT]
> **Version parsers must accept `vDev-<hash>`** (and `null`, see below) without crashing.
> Do not assume `core`, `web` and `ftl` share a version number: they are released independently.

## Quick reference for API clients

The rules pihello's client (`pihello.client`) and sources (`pihello.sources`) must follow, all true for **every** tested version unless a version is named:

1. **Login** `POST /api/auth {"password": …}` → `200` with `session.{valid, totp, sid, csrf, validity, message}`.
   `validity` is the session lifetime in seconds (1800 everywhere).
2. **Wrong password** → `401` with a *session object* (`{"session": {"valid": false, "totp": false, "sid": null, "validity": -1, "message": "password incorrect"}}`), not an error object.
   An empty password against a protected Pi-hole is the same `401`.
3. **No password configured** → `POST /api/auth` returns `200` with `session.sid == null`, `valid: true`, `message: "no password set"` (for *any* password, then `message: "password incorrect"` but still `valid: true`).
   Proceed without a session.
4. **2FA (TOTP) enabled**: a login without `totp` returns **`400`** `bad_request` "No 2FA token found in JSON payload" — *not* `401` with `session.totp == true`.
   A wrong token → `401` error object `unauthorized` "Invalid 2FA token".
   A wrong *password* → `401` session object with `session.totp: true`.
   The `totp` field is an integer. ⇒ Treat `400` + message containing "2FA" **or** a `401` whose `session.totp` is `true` as `TwoFactorRequired`.
5. **`429` has two meanings** — distinguish by `error.key`: `api_seats_exceeded` (hint "increase webserver.api.max_sessions") → `TooManySessions`; `rate_limiting` → login rate limit (more than 3 password checks in the same wall-clock second, global, all versions; message "Rate-limiting login attempts") or, from FTL **v6.4** on, more than one 2FA attempt per 30 s window ("Rate-limiting 2FA token requests, try again later").
   Rate limiting is transient: wait ≥ 1 s (TOTP: next 30 s window).
   Even a *correct* password is rejected while the login rate limit is active.
   **An app password costs two password checks** (FTL tries the normal password first, as does any wrong password when an app password is set).
   When only the second check is over the limit, FTL answers **`401` "password incorrect"** (session object) after its 250 ms pause — not `429`.
   So two shells logging in with an app password in the same second can get a "wrong password" reply (observed on FTL v6.0 and v6.8 by the integration tests).
   The reply's own `took` gives it away: ≈ 0.29 s (the pause) versus ≈ 0.07 s for a genuinely wrong password, independent of network latency.
   pihello retries a `401` whose `took` is ≥ 0.25 s (client-side elapsed time only if `took` is missing).
6. **Sending the sid**: `X-FTL-SID: <sid>` works everywhere (as do a `sid:` header and `?sid=`).
   A `sid` cookie alone is rejected (`401`) — cookie auth also needs the CSRF token in `X-CSRF-TOKEN`.
   `Authorization: Bearer` is not supported (`401`).
7. **Not authenticated / invalid / expired / logged-out sid** on a data endpoint → `401` *error object* `{"error": {"key": "unauthorized", "message": "Unauthorized", "hint": null}}`.
   Re-login once and retry.
8. **Logout** `DELETE /api/auth` → **`204`, empty body**.
   Repeating it, or without a sid → `401` session object (`message` "session unknown" / "no SID provided").
   On a password-less Pi-hole → `401` with `session.valid: true`, "no password set".
   A client must treat any of these as "logged out".
9. **Unknown endpoint** → `404` error object `not_found` with `hint` = the path, **with or without authentication** (so a 404 never means "log in").
10. **Wrong method** on an existing endpoint → `404` `not_found` up to FTL v6.7.0, **`405`** `method_not_allowed` (hint = method) from FTL v6.7.1.
11. **`info/version` can fail right after start-up** (any version, it is a race with the image's update checker writing `/etc/pihole/versions`): FTL answers `500` `internal_error` "Failed to read /etc/pihole/versions", or `200` with a partial file — any of `local.version`, `remote.version`, `docker.local` may be `null` or `""` (see `GET /api/info/version#no-password`, recorded ~2 s after start).
    Treat a `500` as "source unavailable", not as fatal, and every version field as optional.
12. **Every endpoint pihello reads exists in every tested version** (all `200`); there is no endpoint that a v6.x Pi-hole lacks.
    Missing *fields* are the only differences (see [Field changes](#field-changes-by-version)).
13. Every JSON body carries a float `took` (seconds); `204` bodies are empty.

## Authentication and sessions in detail

Status per recorded variant (identical in every tested version unless a range is given).
"session" = `{"session": {...}}` body, "error" = `{"error": {"key", "message", "hint"}}` body.

| Fixture key | Situation | Status | Body |
| :-- | :-- | --: | :-- |
| `POST /api/auth` | correct password | 200 | session, `message` "password correct" |
| `POST /api/auth#wrong-password` | wrong password | 401 | session, "password incorrect" |
| `POST /api/auth#empty-password` | `""` on a protected Pi-hole | 401 | session, "password incorrect" |
| `POST /api/auth#no-body` | no request body | 400 | error `bad_request` "No request body data" |
| `POST /api/auth#login-rate-limited` | > 3 attempts in one second | 429 | error `rate_limiting` "Rate-limiting login attempts" (FTL also sleeps 250 ms) |
| `POST /api/auth#seats-exceeded` | all `max_sessions` seats taken | 429 | error `api_seats_exceeded`, hint "increase webserver.api.max_sessions" |
| `POST /api/auth#app-password` | app password | 200 | session, "app-password correct" |
| `GET /api/auth#app-password` | app session | 200 | session, "correct password" |
| `PATCH /api/config#app-password-forbidden` | config change with an app session | 403 | error `forbidden` (needs `webserver.api.app_sudo`) |
| `POST /api/auth#totp-required` | TOTP on, no `totp` | **400** | error `bad_request` "No 2FA token found in JSON payload" |
| `POST /api/auth#totp-invalid` | TOTP on, wrong token | 401 | error `unauthorized` "Invalid 2FA token" |
| `POST /api/auth#totp-rate-limited` | second attempt in the same 30 s window | 401 (FTL ≤ v6.3) / **429** (FTL ≥ v6.4) | ≤ v6.3: "Invalid 2FA token"; ≥ v6.4: error `rate_limiting` |
| `POST /api/auth#totp-wrong-password` | TOTP on, wrong password | 401 | session with `totp: true` |
| `POST /api/auth#totp` | TOTP on, valid token | 200 | session with `totp: true` |
| `GET /api/auth#totp-enabled-old-session` | session created before TOTP was enabled | 401 | session `totp: true`, "session unknown" |
| `GET /api/auth` | valid session | 200 | session, "correct password" |
| `GET /api/auth#unauthenticated` | no sid | 401 | session, "no SID provided" |
| `GET /api/stats/summary#unauthenticated` | no sid | 401 | error `unauthorized` |
| `GET /api/stats/summary#invalid-sid` | unknown sid | 401 | error `unauthorized` |
| `GET /api/stats/summary#session-expired` | sid after logout | 401 | error `unauthorized` |
| `GET /api/stats/summary#sid-header` | sid in `sid:` header | 200 | data |
| `GET /api/stats/summary#sid-query` | sid in `?sid=` | 200 | data |
| `GET /api/stats/summary#sid-cookie` | sid cookie, no CSRF header | 401 | error `unauthorized` |
| `DELETE /api/auth` | logout | **204** | empty |
| `DELETE /api/auth#session-unknown` | logout twice | 401 | session, "session unknown" |
| `DELETE /api/auth#unauthenticated` | logout without sid | 401 | session, "no SID provided" |
| `GET /api/does/not/exist` | unknown path (with or without sid) | 404 | error `not_found`, hint = path |
| `PUT /api/info/version#method-not-allowed` | wrong method | 404 (FTL ≤ v6.7.0) / **405** (FTL ≥ v6.7.1) | `not_found` / `method_not_allowed` (hint = method) |
| `GET /api/info/login` | unauthenticated probe | 200 | `{"dns": true, "https_port": 443}` |

Password-less Pi-hole (container started with `FTLCONF_webserver_api_password=`):

| Fixture key | Status | Body |
| :-- | --: | :-- |
| `POST /api/auth#no-password` (`""`) | 200 | session `valid: true, sid: null, validity: -1`, "no password set" |
| `POST /api/auth#no-password-any-password` | 200 | same, but "password incorrect" |
| `POST /api/auth#no-password-no-body` | 400 | error `bad_request` |
| `GET /api/auth#no-password` | 200 | session `valid: true, sid: null` |
| `GET /api/stats/summary#no-password`, `GET /api/dns/blocking#no-password` | 200 | data, no sid needed |
| `GET /api/auth/sessions#no-password` | 200 | `{"sessions": []}` |
| `DELETE /api/auth#no-password` | 401 | session `valid: true`, "no password set" |

Session id transport, in the order FTL looks for it (per FTL source, `src/api/auth.c`; statuses recorded in `meta.survey.sid_transport_status`): JSON body field `sid` → header `sid` → header `X-FTL-SID` → cookie `sid` (only together with header `X-CSRF-TOKEN: <csrf>`) → query `?sid=` (`+` may be sent as space).
A `sid` header wins over `X-FTL-SID`.

Other session facts (all versions, see `meta.survey`):

- Default `webserver.api.max_sessions` is 16.
  Sessions survive FTL restarts (FTL logs "Restored N API sessions from the database").
- **Changing `webserver.api.app_pwhash`, `webserver.api.totp_secret`, `webserver.api.password` or `webserver.api.pwhash` invalidates all sessions**, including the one that made the change (`FLAG_INVALIDATE_SESSIONS`; recorded for `app_pwhash` and `totp_secret` on every version).
- Changing an item flagged `FLAG_RESTART_FTL` **restarts FTL** about a second after the `200` (log: "Restarting FTL: dnsmasq config changed"); requests in flight then fail with "connection reset".
  Relevant items (v6.0–v6.8): `webserver.api.max_sessions`, `webserver.api.cli_pw`, `webserver.port`, `webserver.domain`, `webserver.acl`, `webserver.tls.cert`, `dns.upstreams`, `dns.listeningMode` (v6.8 adds `webserver.threads`, `webserver.headers`, `webserver.api.maxHistory`).
  Sessions are kept across the restart; a new `max_sessions` limit applies only after it.
- Settings forced by an `FTLCONF_*` environment variable are read-only via the API: `PATCH /api/config` → `400` "Config items set via environment variables cannot be changed via the API".
- Creating an app password is two steps: `GET /api/auth/app` returns `{"app": {"password", "hash"}}`, then `PATCH /api/config` with `{"config": {"webserver": {"api": {"app_pwhash": hash}}}}`.
- `GET /api/auth/totp` returns a suggested secret plus the current codes (`{"totp": {"type", "account", "issuer", "algorithm": "SHA1", "digits": 6, "period": 30, "offset", "secret", "codes": [5 ints]}}`); enable 2FA by PATCHing `webserver.api.totp_secret`.
  A TOTP code can be used once.
- `GET /api/auth/sessions` entries: `id, current_session, valid, tls.{login, mixed}, login_at, last_active, valid_until, remote_addr, user_agent, x_forwarded_for, app, cli`.

## Endpoint availability

Every endpoint below returned `200` (authenticated, with the exact query string shown) on **all eleven** tested versions:

`auth`, `auth/sessions`, `auth/app`, `auth/totp`, `info/login`, `info/version`, `info/system`, `info/host`, `info/sensors`, `info/ftl`, `info/database`, `info/messages`, `info/messages/count`, `info/metrics`, `info/client`, `stats/summary`, `stats/upstreams`, `stats/query_types`, `stats/recent_blocked?count=10`, `stats/top_domains?count=10`, `stats/top_domains?blocked=true&count=10`, `stats/top_clients?count=10`, `stats/top_clients?blocked=true&count=10`, `stats/database/summary?from=1&until=4102444800`, `history`, `history/clients`, `dns/blocking` (GET and POST), `network/gateway`, `network/interfaces`, `network/devices`, `network/routes`, `config/webserver/api/max_sessions`, `config/dns/upstreams`, `dhcp/leases`, `lists`, `groups`, `domains`, `clients`, `padd`, `endpoints`.

`stats/database/summary` requires both `from` and `until`; `from=0` counts as missing (`400` "You need to specify both "from" and "until"").

`GET /api/endpoints` (83 routes in v6.0, 85 in v6.8) differs only by:

| FTL | Change |
| :-- | :-- |
| v6.0 | `GET /api/clients`, `/api/domains`, `/api/groups`, `/api/lists` are listed only with their path parameters (e.g. `/{client}`); the routes themselves work |
| v6.3 | added `POST /api/action/flush/network` |
| v6.6 | added `GET /api/config/_properties` |

## Field changes by version

Field-level differences between consecutive recordings (`took` ignored; data-dependent keys such as client IPs, empty lists, `null` and int-vs-float are not counted as changes).
Nothing changed in `stats/*`, `history`, `dns/blocking`, `info/host`, `info/sensors`, `info/version`, `network/gateway`, `auth*` across the whole v6 series.

| FTL (tag) | Endpoint | Change |
| :-- | :-- | :-- |
| v6.0.4 (`2025.03.0`) | — | no field changes vs v6.0 |
| v6.1 (`2025.04.0`) | `info/system`, `padd` | added `system.cpu.%cpu` (float; total CPU %, may exceed 100 and be negative if unknown) |
| v6.2 (`2025.08.0`) | `info/ftl` | added `ftl.dnsmasq.{dhcp_leasequery, dhcp_lease_unassigned, dhcp_lease_actve, dhcp_lease_unknown}` (sic: `actve`) |
| v6.2 (`2025.08.0`) | `padd` | added `config.privacy_level` |
| v6.3 (`2025.11.0`) | `info/ftl` | **type change**: `ftl.database.domains.{allowed,denied}` and `ftl.database.regex.{allowed,denied}` were `int`, now `{"total": int, "enabled": int}` |
| v6.3 (`2025.11.0`) | `info/system`, `padd` | added `system.ftl.{%mem, %cpu}` |
| v6.4 (`2025.11.1`) | `info/database` | added `earliest_timestamp`, `queries_disk`, `earliest_timestamp_disk`; `queries` now counts the *in-memory* database (before: long-term database) |
| v6.5 (`2026.02.0`) | — | no field changes |
| v6.6 (`2026.06.0`) | `padd` | added `queries.query_frequency` |
| v6.7 (`2026.07.2`) | `info/ftl` | added `ftl.database.antigravity` (domains on allow-lists) |
| v6.7.1 (`2026.09.0`) | — | no field changes |
| v6.8 (`2026.10.0`) | — | no field changes |

Stable shapes pihello relies on (all versions):

- `stats/summary`: `queries.{total, blocked, percent_blocked, unique_domains, forwarded, cached, frequency, types{16 keys}, status{19 keys}, replies{14 keys}}`, `clients.{active, total}`, `gravity.{domains_being_blocked, last_update}`.
  There is **no** `gravity.relative` — pihello must derive it from `last_update`.
  `types`: A, AAAA, ANY, SRV, SOA, PTR, TXT, NAPTR, MX, DS, RRSIG, DNSKEY, NS, SVCB, HTTPS, OTHER.
  `status`: UNKNOWN, GRAVITY, FORWARDED, CACHE, REGEX, DENYLIST, EXTERNAL_BLOCKED_IP, EXTERNAL_BLOCKED_NULL, EXTERNAL_BLOCKED_NXRA, GRAVITY_CNAME, REGEX_CNAME, DENYLIST_CNAME, RETRIED, RETRIED_DNSSEC, IN_PROGRESS, DBBUSY, SPECIAL_DOMAIN, CACHE_STALE, EXTERNAL_BLOCKED_EDE15.
  `replies`: UNKNOWN, NODATA, NXDOMAIN, CNAME, IP, DOMAIN, RRNAME, SERVFAIL, REFUSED, NOTIMP, OTHER, DNSSEC, NONE, BLOB.
- `dns/blocking`: `{"blocking": "enabled"|"disabled"|…, "timer": null|int}` (`timer` = seconds left; `POST` with `{"blocking": false, "timer": 300}` echoes the new state).
- `stats/recent_blocked?count=10`: `{"blocked": [str, …]}` — newest first, one entry per *query*, so the same domain usually appears twice (A + AAAA).
- `stats/top_domains`: `{"domains": [{"domain", "count"}], "total_queries", "blocked_queries"}`; `stats/top_clients`: `{"clients": [{"name", "ip", "count"}], …}` — `name` is `""` when unresolved (common for IPv6 clients).
- `stats/upstreams`: `{"upstreams": [{"ip", "name", "port", "count", "statistics": {"response", "variance"}}], "total_queries", "forwarded_queries"}`.
  The list includes the pseudo upstreams `{"ip": "blocklist", "port": -1}` and `{"ip": "cache", "port": -1}`.
- `history`: `{"history": [{"timestamp", "total", "cached", "blocked", "forwarded"}]}` — **145** 10-minute buckets (24 h + the current one).
- `network/gateway`: `{"gateway": [{"family": "inet"|"inet6", "interface", "address", "local": [str]}]}` — one entry per address family.
- `info/version`: `version.{core,web,ftl}.{local.{version, branch, hash} (ftl also `date`), remote.{version, hash}}`, `version.docker.{local, remote}`.
  See the next section for `null`s.

## Value quirks

- **Remote versions** come from the image's update checker, which calls `api.github.com` (unauthenticated, 60 requests/hour/IP, 4 per container start).
  In the fixtures they are real: every tag reports the releases that were current on 2026-10-24 (core v6.5, web v6.6.1, FTL v6.8, docker 2026.10.0), so older tags are genuine "update available" cases and `2026.10.0` is "up to date".
  When the check fails (offline, rate limited) FTL reports `remote.version: null` for core/web/ftl but **`version.docker.remote` as the *string* `"null"`** (`jq .tag_name` on GitHub's error JSON; observed in a rate-limited recording run, not present in the committed fixtures); `remote.hash` is filled independently (`git ls-remote`).
  `version.<c>.update` must be `False`/unknown when the remote version is missing or empty — never compare against `"null"` or `""`.
- Right after start-up `info/version` may answer `500` or return a partial file with `null`/`""` fields (any version), see quick reference item 11.
- Numbers: counters that are `0` are often serialised as integers even where the field is normally a float (`earliest_timestamp_disk: 0`, `query_frequency: 0`, `statistics.response: 0`).
  Accept `int | float`.
- `info/sensors`: inside a container, FTL ≤ v6.7.0 reported `list: []` and `cpu_temp: null`; FTL ≥ v6.7.1 found the host's hwmon sensors.
  On real hardware it depends on the board — `cpu_temp` may always be `null`.
- `info/host.model` is `null` on machines without a device-tree model (x86); `dmi.*` values are free-form strings.
- `top_clients[].name` is `""` (not `null`) when unresolved; the Pi-hole itself shows up as `pi.hole` and `127.0.0.1` as `localhost`.
- `info/system` (memory, load, uptime, process count) always describes the *host kernel*, also inside a container.
  `network/*` describes the container's network namespace — with rootless podman's default (pasta) network that mirrors the host's interfaces and addresses, which is why the recorder uses its own bridge network.

## TLS

Every version creates `/etc/pihole/tls.pem` (key + cert), `tls.crt` and a CA `/etc/pihole/tls_ca.crt` on first start; the server certificate's CN and only SAN is **`pi.hole`** (from `webserver.domain`), issued by a CA also named `pi.hole`.
`info/login` reports `https_port: 443`.

**Python 3.13+ rejects this CA by default**: `ssl.create_default_context()` sets `VERIFY_X509_STRICT`, and the handshake fails with "CA cert does not include key usage extension" on every tested version.
With `ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT` verification against `tls_ca.crt` succeeds, and hostname checking succeeds for `pi.hole` and fails for any other name (e.g. `localhost`, an IP).
pihello therefore clears `VERIFY_X509_STRICT` for every verifying context — not only for `--ca-cert`, because users also import `tls_ca.crt` into the system trust store — while chain and hostname checks stay on, and it explains hostname mismatches.
Details per tag: `meta.survey.tls`.

## Behaviour that matters for test infrastructure

- `FTLCONF_dns_listeningMode=ALL` (upper case accepted everywhere) is needed for queries arriving through a published port.
- After PATCHing a `FLAG_RESTART_FTL` item (e.g. `max_sessions` to provoke `#seats-exceeded`) wait until the container log shows "FTL started on" again before sending more requests (`scripts/record_fixtures.py`: `await_possible_restart()`).
  `dns.hosts` only rewrites the hosts file.
- `tests/helpers/containers.py` wraps all of this: runtime detection (`PIHELLO_IT_RUNTIME`), free ports (default 18100–18199, `port_range=`), readiness (API, gravity, DNS; `wait_versions()` for a complete `/etc/pihole/versions`), `exec`, `copy_out`, `tls_ca_cert()`, `create_app_password()`, `totp_code()`, `pause()`.
- `/etc/pihole/versions` is written line by line after start-up; it is complete once `GITHUB_DOCKER_VERSION=` is present.
  Its empty values come back as `""` (FTL v6.0) or `null` (later).
  FTL fills in its own `version.ftl.local.{version,branch,date}` even without the file; removing the file reproduces the start-up `500` deterministically.
- Pi-hole's default upstreams (8.8.8.8, 8.8.4.4) are not reachable from every network; the integration tests use the resolver the runtime gives containers.
- Deny/allow list changes via the API take effect asynchronously (FTL reloads its lists): queries sent right after `POST /api/domains/...` may still be forwarded.
  Probe until the domain is blocked.
- Changing `debug.api` via the API needs no restart; FTL then logs every request (`-> POST /api/auth`) to `/var/log/pihole/FTL.log`.
- A request that reaches a paused (frozen) container is processed when it resumes: a login whose client already gave up leaves a session behind.

## How the fixtures are recorded

```sh
uv run python scripts/record_fixtures.py --tag TAG [--tag TAG …]   # these Docker tags
uv run python scripts/record_fixtures.py --all-v6                  # every v6 tag
```

1. A podman network `pihello-survey-net` with documentation ranges `192.0.2.0/24` / `2001:db8:53::/64` (gateway `.1` = the runtime's DNS forwarder, used as Pi-hole's only upstream and named `router.lan`).
   The Pi-hole runs at `192.0.2.2` (`02:00:00:00:00:02`, hostname `pihole`), a second password-less Pi-hole at `192.0.2.3`.
   Host ports are published on `127.0.0.1` only.
2. Auth variants first (they change config, invalidate sessions and restart FTL), then `dns.hosts` (local names for the LAN clients), then traffic: 3 exact deny entries, 1 deny regex, 1 allow entry, 1 extra group; ~690 queries (A, AAAA, HTTPS, MX, TXT, NS, SOA, SRV, PTR, CNAME, CAA, DNSKEY, DS, NAPTR, SVCB, ANY; popular sites, gravity ad domains, deny/regex hits, NXDOMAINs) from the host through the published port (client `192.0.2.2` "pi.hole"), from four short-lived LAN client containers (`192.0.2.21–23`, `2001:db8:53::24`) and from inside the container (`127.0.0.1`).
3. 65 s settle time (database flush with `FTLCONF_database_DBinterval=10`, ARP table), then all endpoints, blocking disable/enable, transports, TLS, errors and finally the rate-limit burst.
4. Every session created is logged out; containers and the network are removed.
   The password-less container gets `--add-host api.github.com:127.0.0.1` so a full run (11 tags × 4 update-check calls) stays inside GitHub's anonymous API limit and `info/version` carries real remote versions.

Fixture format (see [Testing](architecture.md#testing)): keys are `METHOD /api/<path>[?<sorted query>][#variant]`, entries `{"status", "body"}` (`body` = parsed JSON or `null`).
`meta.survey` holds per-version observations (start-up time, session invalidation, restarts, TLS probe results, sid transport statuses, rate-limit burst, image digest).

### Sanitisation

Applied by `sanitize_responses()` before writing; `find_leaks()` then refuses to write a fixture that still contains any of the recording machine's host name, user name, kernel strings, DMI values, interface names, MAC or IP addresses, or resolver search domains, or any non-documentation address.
Deterministic: replacement numbering only depends on the sorted set of addresses found.

| Data | Replacement |
| :-- | :-- |
| sid / csrf (any `sid`/`csrf` key and every occurrence of the real token) | `cGloZWxsby1mYWtlLXNpZA=` / `cGloZWxsby1mYWtlY3NyZg=` |
| app password / hash (`auth/app`) | fixed fake base64 password / `$BALLOON-SHA256$v=1$s=1024,t=32$…` |
| TOTP secret / codes (`auth/totp`) | fixed base32 secret; codes recomputed for that secret at recording time |
| IPv4 outside documentation ranges, loopback, multicast and public space (private, CGNAT, link-local) | `198.51.100.10+n`; `in-addr.arpa` names are rewritten consistently |
| IPv6 except `2001:db8::/32`, `::1`, `::`, multicast, `fe80::ff:fe00:xx` | link-local → `fe80::ff:fe00:a0+n`, others → `2001:db8:ffff::10+n` |
| MAC addresses other than `00:…`, `ff:…`, `02:00:00:00:00:xx` | `02:00:00:00:00:a0+n` |
| host name, user name | `pihole`; host interface names → `eth0` |
| `info/host` | `uname.nodename` `pihole`, `machine` `aarch64`, `release` `6.12.47+rpt-rpi-v8`, `version` `#1 SMP PREEMPT Debian 1:6.12.47-1+rpt1 (2025-09-16)`, `model` `Raspberry Pi 4 Model B Rev 1.5`, every `dmi.*` string → Raspberry Pi values |
| `info/sensors.list` | `cpu_thermal` (temp = recorded `cpu_temp`) + `rpi_volt`, same item keys as recorded |
| `padd` | `node_name` `pihole`, `host_model` as `info/host.model` |
| `info/client` headers, sessions' `user_agent` | `Host: pi.hole`, `User-Agent: pihello-recorder`, fake sid |

Kept as recorded: public IPv4 addresses (e.g. resolvers), every number (memory, load, uptime, temperatures, `took`), timestamps, git hashes.
`tests/test_fixtures.py` re-checks all of this.
