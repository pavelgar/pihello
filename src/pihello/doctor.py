"""``pihello doctor``: step-by-step diagnostics.

Each check prints one line as soon as it finishes — ``✓`` ok, ``!`` warning,
``✗`` failure, ``·`` information — followed by a hint when something can be
done about it. A failure that makes later checks pointless (no URL, DNS, TCP,
TLS, not a Pi-hole, login) stops the run. The exit status is ``0`` when there
are only warnings, else the exit code of the first failure.

The password is never printed: settings are shown via
:meth:`pihello.config.Credential.describe` and every line goes through the
UI's redaction filter.
"""

from __future__ import annotations

import platform
import socket
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TextIO

from pihello.errors import (
    EXIT_CONNECTION,
    EXIT_OK,
    EXIT_USAGE,
    AuthenticationError,
    EndpointNotFound,
    PiHelloError,
    PiholeError,
    TLSError,
    TooManySessions,
    TwoFactorRequired,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Mapping

    from pihello.cli import UI
    from pihello.client import PiholeClient
    from pihello.config import Settings

__all__ = ["SUPPORTED_MAJOR", "Doctor", "run"]

#: pihello 2 supports every Pi-hole v6.x release.
SUPPORTED_MAJOR = 6
MIN_PYTHON = (3, 10)

Status = Literal["ok", "warn", "fail", "info"]
_SYMBOLS = {"ok": ("✓", "32"), "warn": ("!", "33"), "fail": ("✗", "31"), "info": ("·", "2")}
_ASCII = {"ok": "ok", "warn": "!!", "fail": "XX", "info": "--"}

_SESSIONS_HINT = (
    "Pi-hole allows webserver.api.max_sessions (default 16) API sessions at once. Old "
    "sessions expire after webserver.session.timeout (default 30 min); you can also delete "
    "them under Settings → Web interface / API → Currently active sessions, or raise the "
    "limit: sudo pihole-FTL --config webserver.api.max_sessions 32"
)


class _Stop(Exception):
    """Raised to end the run after a fatal failure."""


@dataclass
class _Context:
    settings: Settings | None = None
    url: str | None = None
    host: str = ""
    port: int = 0
    https: bool = False
    addresses: tuple[str, ...] = ()
    client: PiholeClient | None = None
    password: str | None = None
    session_id: Any = None
    session_login_at: Any = None


class Doctor:
    """Runs the checks and prints the report."""

    def __init__(
        self,
        args: argparse.Namespace,
        ui: UI,
        stdout: TextIO,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Prepare a run; nothing is checked until :meth:`run`."""
        from pihello.cli import _supports_color  # noqa: PLC0415

        self.args = args
        self.ui = ui
        self.out = stdout
        self.env = env
        self.color = _supports_color(stdout, mode=getattr(args, "color", None) or "auto")
        encoding = getattr(stdout, "encoding", None) or "utf-8"
        try:
            "✓✗·→".encode(encoding)
            self.unicode = True
        except (LookupError, UnicodeEncodeError):
            self.unicode = False
        self.exit_code = EXIT_OK
        self.failures = 0
        self.warnings = 0
        self.ctx = _Context()

    # -- output ------------------------------------------------------------
    def _paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def _write(self, text: str) -> None:
        line = self.ui.redact(text)
        if not self.unicode:
            line = line.replace("→", "->").encode("ascii", "replace").decode("ascii")
        self.out.write(line + "\n")
        self.out.flush()

    def report(
        self,
        status: Status,
        text: str,
        *,
        hint: str | None = None,
        elapsed: float | None = None,
        exit_code: int | None = None,
    ) -> None:
        """Print one check result and record failures/warnings."""
        symbol, code = _SYMBOLS[status]
        mark = symbol if self.unicode else _ASCII[status]
        timing = self._paint(f" ({elapsed * 1000:.0f} ms)", "2") if elapsed is not None else ""
        self._write(f"  {self._paint(mark, code)} {text}{timing}")
        if hint:
            self._write(f"      {self._paint('hint:', '36')} {hint}")
        if status == "fail":
            self.failures += 1
            if self.exit_code == EXIT_OK:
                self.exit_code = exit_code if exit_code is not None else EXIT_USAGE
        elif status == "warn":
            self.warnings += 1

    def failure(self, text: str, error: PiHelloError) -> None:
        """Report ``error`` as a failure; later checks still run."""
        self.report("fail", f"{text}: {error}", hint=error.hint, exit_code=error.exit_code)

    def fatal(self, text: str, error: PiHelloError) -> NoReturn:
        """Report ``error`` as a failure and end the run."""
        self.failure(text, error)
        raise _Stop

    def section(self, title: str) -> None:
        """Print a section heading."""
        self._write("")
        self._write(self._paint(title, "1"))

    # -- run -----------------------------------------------------------------
    def run(self) -> int:
        """Run every check; return the exit code."""
        from pihello import __version__  # noqa: PLC0415

        started = time.perf_counter()
        self._write(
            self._paint(f"pihello doctor — pihello {__version__}", "1")
            + f" on Python {platform.python_version()} ({platform.system() or 'unknown OS'})"
        )
        try:
            self.check_python()
            self.check_config()
            self.check_url()
            self.check_dns()
            self.check_tcp()
            self.check_api()
            self.check_login()
            self.check_versions()
            self.check_sources()
            self.check_sessions()
        except _Stop:
            pass
        finally:
            if self.ctx.client is not None:
                self.ctx.client.logout()
        took = f"took {_duration(time.perf_counter() - started)}"
        self._write("")
        if self.failures:
            summary = f"{self.failures} problem{'s' if self.failures != 1 else ''} found"
            if self.warnings:
                summary += f", {self.warnings} warning{'s' if self.warnings != 1 else ''}"
            self._write(self._paint(summary, "1;31") + f" ({took})")
        elif self.warnings:
            self._write(
                self._paint(
                    f"Works, with {self.warnings} warning{'s' if self.warnings != 1 else ''}",
                    "1;33",
                )
                + f" ({took})"
            )
        else:
            self._write(self._paint("Everything looks good.", "1;32") + f" ({took})")
        return self.exit_code

    # -- checks ------------------------------------------------------------
    def check_python(self) -> None:
        """Python version."""
        self.section("Environment")
        version = platform.python_version()
        if sys.version_info >= MIN_PYTHON:
            self.report("ok", f"Python {version} ({sys.executable})")
        else:  # pragma: no cover - we cannot even import on older Pythons
            self.report("fail", f"Python {version}: pihello needs Python 3.10 or newer")

    def check_config(self) -> None:
        """Config file location, parse, permissions; effective settings and origins."""
        from pihello import cli, config  # noqa: PLC0415

        path, origin = config.find_config_path(getattr(self.args, "config", None), self.env)
        where = {"--config": "from --config", "PIHELLO_CONFIG": "from $PIHELLO_CONFIG"}.get(
            origin, "default location"
        )
        try:
            file = config.load_file(path, required=origin != "default")
        except PiHelloError as exc:
            self.fatal(f"config file {path} ({where})", exc)
        if file.exists:
            mode = f", mode {file.mode:o}" if file.mode is not None else ""
            profiles = f", profiles: {', '.join(sorted(file.profiles))}" if file.profiles else ""
            self.report("ok", f"config file {path} ({where}{mode}{profiles})")
            if file.permission_warning():
                import shlex  # noqa: PLC0415

                self.report(
                    "warn",
                    "the config file stores a password but other users can read it",
                    hint=f"chmod 600 {shlex.quote(str(path))}",
                )
        else:
            self.report(
                "warn",
                f"no config file at {path} ({where})",
                hint="run `pihello init` to create one (settings can also come from options "
                "and PIHELLO_* variables)",
            )
        quiet, self.ui.quiet = self.ui.quiet, True  # warnings are reported above instead
        try:
            settings = cli.resolve_settings(self.args, self.ui, env=self.env)
        except PiHelloError as exc:
            self.fatal("settings", exc)
        finally:
            self.ui.quiet = quiet
        self.ctx.settings = settings
        self._print_settings(settings)

    def _print_settings(self, settings: Settings) -> None:
        template = settings.template.describe()
        credential = settings.credential.describe() if settings.credential else "none"
        rows = [
            ("url", settings.url or "(not set)", settings.origins.get("url", "default")),
            ("password", credential, settings.origins.get("password", "not set")),
            ("verify_tls", _show(settings.verify_tls), settings.origins.get("verify_tls", "")),
            ("timeout", f"{settings.timeout:g} s", settings.origins.get("timeout", "")),
            ("template", template, settings.origins.get("template", "")),
            ("theme", settings.theme, settings.origins.get("theme", "")),
            ("color", settings.color, settings.origins.get("color", "")),
            ("width", str(settings.width), settings.origins.get("width", "")),
            ("cache_ttl", f"{settings.cache_ttl:g} s", settings.origins.get("cache_ttl", "")),
        ]
        if settings.profile:
            rows.insert(0, ("profile", settings.profile, settings.origins.get("profile", "")))
        if settings.styles:
            rows.append(
                ("styles", ", ".join(sorted(settings.styles)), settings.origins.get("styles", ""))
            )
        # The file's path is on the "config file" line above; don't repeat it per row.
        in_file = f" ({settings.config.path})"
        rows = [(key, value, origin.replace(in_file, "")) for key, value, origin in rows]
        self.report("info", "effective settings (value — where it comes from):")
        key_w = max(len(r[0]) for r in rows)
        origin_w = max(len(r[2]) for r in rows)
        room = 80 - 6 - key_w - 2 - 2 - origin_w  # fit 80 columns where possible
        value_w = max(min(max(len(r[1]) for r in rows), 44, room), 16)
        for key, value, origin in rows:
            shown = value if len(value) <= value_w else value[: value_w - 1] + "…"
            origin_text = self._paint(origin, "2")
            self._write(f"      {key.ljust(key_w)}  {shown.ljust(value_w)}  {origin_text}")

    def check_url(self) -> None:
        """URL normalisation."""
        from urllib.parse import urlsplit  # noqa: PLC0415

        from pihello.client import normalize_url  # noqa: PLC0415

        self.section("Connection")
        settings = self.ctx.settings
        assert settings is not None  # noqa: S101
        if not settings.url:
            self.report(
                "fail",
                "no Pi-hole address configured",
                hint="run `pihello init`, pass --url, or set url in the config / PIHELLO_URL",
                exit_code=EXIT_USAGE,
            )
            raise _Stop
        try:
            url = normalize_url(settings.url)
        except PiHelloError as exc:
            self.fatal(f"address {settings.url!r}", exc)
        parts = urlsplit(url)
        self.ctx.url = url
        self.ctx.host = parts.hostname or ""
        self.ctx.https = parts.scheme == "https"
        self.ctx.port = parts.port or (443 if self.ctx.https else 80)
        note = f" (from {settings.url!r})" if url != settings.url else ""
        self.report("ok", f"address {url}{note}; API at {url}/api")

    def check_dns(self) -> None:
        """DNS resolution of the host (with the configured timeout)."""
        import ipaddress  # noqa: PLC0415

        host, port = self.ctx.host, self.ctx.port
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            self.ctx.addresses = (host,)
            self.report("info", f"{host} is an IP address; no DNS lookup needed")
            return
        timeout = self.ctx.settings.timeout if self.ctx.settings else 3.0
        started = time.perf_counter()
        result: list[Any] = []

        def resolve() -> None:
            try:
                result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except OSError as exc:
                result.append(exc)

        import threading  # noqa: PLC0415

        worker = threading.Thread(target=resolve, daemon=True)
        worker.start()
        worker.join(timeout)
        elapsed = time.perf_counter() - started
        hint = (
            "check the name; `pi.hole` only resolves on machines that use the Pi-hole as DNS "
            "server — use its IP address or a hostname from your router instead"
        )
        if not result:
            self.report(
                "fail",
                f"DNS lookup of {host} timed out after {timeout:g} s",
                hint=hint,
                exit_code=EXIT_CONNECTION,
            )
            raise _Stop
        if isinstance(result[0], OSError):
            reason = getattr(result[0], "strerror", None) or str(result[0])
            self.report(
                "fail",
                f"cannot resolve {host}: {reason}",
                hint=hint,
                elapsed=elapsed,
                exit_code=EXIT_CONNECTION,
            )
            raise _Stop
        addresses = tuple(dict.fromkeys(str(info[4][0]) for info in result[0]))
        self.ctx.addresses = addresses
        self.report("ok", f"DNS {host} → {', '.join(addresses)}", elapsed=elapsed)

    def check_tcp(self) -> None:
        """TCP connect to the first reachable address."""
        timeout = self.ctx.settings.timeout if self.ctx.settings else 3.0
        errors: list[str] = []
        for address in self.ctx.addresses:
            started = time.perf_counter()
            try:
                with socket.create_connection((address, self.ctx.port), timeout=timeout):
                    pass
            except OSError as exc:
                timed_out = isinstance(exc, TimeoutError)
                reason = "timed out" if timed_out else (exc.strerror or str(exc))
                errors.append(f"{address}: {reason}")
                continue
            shown = f"[{address}]" if ":" in address else address
            elapsed = time.perf_counter() - started
            self.report("ok", f"TCP connection to {shown}:{self.ctx.port}", elapsed=elapsed)
            return
        hint = (
            "is the Pi-hole running and the port right? Pi-hole's web server listens on 80 "
            "and 443 by default (docker: check the published ports); a firewall may block it"
        )
        self.report(
            "fail",
            f"cannot connect to port {self.ctx.port}: {'; '.join(errors)}",
            hint=hint,
            exit_code=EXIT_CONNECTION,
        )
        raise _Stop

    def _make_client(self, password: str | None) -> PiholeClient:
        from pihello import __version__  # noqa: PLC0415
        from pihello.client import PiholeClient  # noqa: PLC0415

        settings = self.ctx.settings
        assert settings is not None  # noqa: S101
        assert self.ctx.url is not None  # noqa: S101
        return PiholeClient(
            self.ctx.url,
            password,
            timeout=settings.timeout,
            verify=settings.verify,
            user_agent=f"pihello/{__version__} (doctor)",
        )

    def check_api(self) -> None:
        """Unauthenticated ``/api/info/login`` probe (+ TLS and HTTPS availability)."""
        settings = self.ctx.settings
        assert settings is not None  # noqa: S101
        client = self._make_client(None)
        started = time.perf_counter()
        try:
            info = client.probe()
        except TLSError as exc:
            self.report(
                "fail",
                f"HTTPS certificate of {self.ctx.host}: {exc}",
                hint=exc.hint
                or 'use --ca-cert PATH (or verify_tls = "/path/ca.pem") for Pi-hole\'s '
                "self-signed certificate, or --insecure",
                exit_code=exc.exit_code,
            )
            raise _Stop from None
        except PiHelloError as exc:
            self.fatal("Pi-hole API", exc)
        elapsed = time.perf_counter() - started
        if self.ctx.https:
            if settings.verify is False:
                self.report(
                    "warn",
                    "HTTPS works, but the certificate is NOT verified (--insecure / verify_tls)",
                    hint="safer: give Pi-hole's certificate with --ca-cert or "
                    'verify_tls = "/path/to/tls_ca.crt"',
                )
            else:
                where = f" against {settings.verify}" if isinstance(settings.verify, str) else ""
                self.report("ok", f"HTTPS certificate verified{where}")
        self.report("ok", "Pi-hole v6 API answers at /api/info/login", elapsed=elapsed)
        https_port = info.get("https_port")
        if not self.ctx.https and isinstance(https_port, int) and https_port > 0:
            host = f"[{self.ctx.host}]" if ":" in self.ctx.host else self.ctx.host
            address = self.ctx.addresses[0] if self.ctx.addresses else self.ctx.host
            if tcp_reachable(address, https_port, min(settings.timeout, 2.0)):
                port = "" if https_port == 443 else f":{https_port}"
                self.report(
                    "warn",
                    f"using plain HTTP although this Pi-hole serves HTTPS on port {https_port}",
                    hint=f'use url = "https://{host}{port}" so the password is encrypted '
                    "(add --ca-cert or --insecure for the self-signed certificate)",
                )
            else:  # e.g. Docker: the container's port, not the published one
                self.report(
                    "info",
                    f"Pi-hole reports HTTPS on port {https_port}, but {host}:{https_port} "
                    "doesn't answer",
                    hint="behind a Docker port mapping? If HTTPS is published on another port, "
                    f'use url = "https://{host}:PORT" so the password is encrypted',
                )

    def check_login(self) -> None:
        """Obtain the password and log in."""
        settings = self.ctx.settings
        assert settings is not None  # noqa: S101
        self.section("Authentication")
        password: str | None = None
        if settings.credential is not None:
            started = time.perf_counter()
            try:
                password = settings.password()
            except PiHelloError as exc:
                self.fatal(f"password from {settings.credential.describe()}", exc)
            self.ui.add_secret(password)
            if settings.credential.kind != "password":
                self.report(
                    "ok",
                    f"password read from {settings.credential.describe()}",
                    elapsed=time.perf_counter() - started,
                )
        client = self._make_client(password)
        self.ctx.client = client
        self.ctx.password = password
        started = time.perf_counter()
        try:
            client.login()
            if password is None:
                client.get("info/version")  # does this Pi-hole need a password?
        except TwoFactorRequired as exc:
            self.report(
                "fail",
                f"login: {exc}",
                hint="two-factor authentication is enabled: pihello cannot type TOTP codes. "
                "Create an app password (Settings → Web interface / API → Configure app "
                "password) and use that instead",
                exit_code=exc.exit_code,
            )
            raise _Stop from None
        except TooManySessions as exc:
            self.report("fail", f"login: {exc}", hint=_SESSIONS_HINT, exit_code=exc.exit_code)
            raise _Stop from None
        except AuthenticationError as exc:
            hint = exc.hint
            if password is None:
                hint = (
                    "this Pi-hole has a password: run `pihello init`, or set password_file / "
                    "password_command / PIHELLO_PASSWORD (an app password is recommended)"
                )
            self.report("fail", f"login: {exc}", hint=hint, exit_code=exc.exit_code)
            raise _Stop from None
        except PiHelloError as exc:
            self.fatal("login", exc)
        elapsed = time.perf_counter() - started
        if password is None:
            self.report("ok", "this Pi-hole has no password; no login needed", elapsed=elapsed)
        elif client.sid is None:
            self.report(
                "warn",
                "this Pi-hole has no password, so the configured one is not needed",
                elapsed=elapsed,
            )
        else:
            self.report("ok", "logged in with the configured password", elapsed=elapsed)

    def check_versions(self) -> None:
        """Pi-hole component versions, updates and the supported range."""
        from pihello import sources  # noqa: PLC0415

        client = self.ctx.client
        assert client is not None  # noqa: S101
        self.section("Pi-hole")
        try:
            variables, problems = sources.fetch(client, sources.plan(["version"]))
        except PiHelloError as exc:
            self.failure("versions (info/version)", exc)
            return
        if problems:
            self.failure("versions (info/version)", problems[0].error)
            return
        version = variables.get("version") or {}
        updates = variables.get("updates") or {}
        labels = {"core": "core", "web": "web", "ftl": "FTL", "docker": "docker"}
        parts: list[str] = []
        upgrades: list[str] = []
        statuses: list[bool | None] = []
        for name, label in labels.items():
            entry = version.get(name)
            if not isinstance(entry, dict):
                continue
            local, remote = entry.get("local"), entry.get("remote")
            if isinstance(local, dict):
                local, remote = local.get("version"), (remote or {}).get("version")
            if not local:
                continue
            parts.append(f"{label} {local}")
            statuses.append(entry.get("update"))
            if name in (updates.get("components") or ()):
                upgrades.append(f"{label} {local} → {remote}")
        self.report("ok" if parts else "warn", "Pi-hole " + (", ".join(parts) or "version unknown"))
        core = ((version.get("core") or {}).get("local") or {}).get("version") or ""
        major = _major(str(core))
        if major is not None and major != SUPPORTED_MAJOR:
            self.report(
                "warn",
                f"pihello 2 is made for Pi-hole v{SUPPORTED_MAJOR}.x; this is {core}",
                hint="for Pi-hole v5 use pihello 0.3.0 (pip install 'pihello<1')"
                if major < SUPPORTED_MAJOR
                else "newer major versions are untested; please report problems",
            )
        if upgrades:
            # version.docker exists on every install; only Docker fills in the image tag
            in_docker = isinstance((version.get("docker") or {}).get("local"), str)
            self.report(
                "info",
                "update available: " + ", ".join(upgrades),
                hint="pull the new image and recreate the container"
                if in_docker
                else "update with `pihole -up`",
            )
        elif statuses and all(status is None for status in statuses):
            self.report(
                "info",
                "update status unknown",
                hint="Pi-hole has not checked for updates yet (it does so daily, "
                "or run `pihole updatechecker`)",
            )
        else:
            self.report("ok", "up to date")

    def check_sources(self) -> None:
        """Fetch every data source and report which ones are unavailable."""
        from pihello import sources  # noqa: PLC0415

        client = self.ctx.client
        assert client is not None  # noqa: S101
        planned = sources.plan_all()
        started = time.perf_counter()
        try:
            _variables, problems = sources.fetch(client, planned)
        except PiHelloError as exc:
            self.failure("data sources", exc)
            return
        elapsed = time.perf_counter() - started
        missing = [p for p in problems if isinstance(p.error, EndpointNotFound)]
        broken = [p for p in problems if not isinstance(p.error, EndpointNotFound)]
        available = len(planned) - len(problems)
        status: Status = "ok"
        if broken:
            status = "warn" if available else "fail"
        self.report(
            status,
            f"{available} of {len(planned)} data sources available",
            elapsed=elapsed,
            exit_code=broken[0].error.exit_code if broken else None,
        )
        for problem in missing:
            names = ", ".join(problem.source.namespaces)
            self.report(
                "info",
                f"{problem.source.endpoint}: not on this Pi-hole version ({names} show as missing)",
            )
        for problem in broken:
            self.report(
                "warn",
                f"{problem.source.endpoint}: {problem.error}",
                hint=problem.error.hint,
            )

    def check_sessions(self) -> None:
        """List API sessions, log out, and verify that our session is gone."""
        client = self.ctx.client
        assert client is not None  # noqa: S101
        self.section("Sessions")
        if client.sid is None:
            client.logout()
            self.ctx.client = None
            self.report("info", "no API session used (the Pi-hole has no password)")
            return
        try:
            before = _sessions(client.get("auth/sessions"))
        except PiholeError as exc:
            client.logout()
            self.ctx.client = None
            if isinstance(exc, EndpointNotFound):
                self.report("info", "this Pi-hole cannot list API sessions; logged out")
            else:
                self.report("warn", f"cannot list API sessions: {exc}; logged out", hint=exc.hint)
            return
        mine = next((s for s in before if s.get("current_session")), None)
        others = [s for s in before if not s.get("current_session")]
        stale = [s for s in others if str(s.get("user_agent") or "").startswith("pihello")]
        self.report(
            "info",
            f"{len(before)} active API session{'s' if len(before) != 1 else ''}"
            f" (including this one)",
        )
        if stale:
            self.report(
                "warn",
                f"{len(stale)} other pihello "
                + ("sessions are" if len(stale) != 1 else "session is")
                + " still open (another terminal, --watch, or a crashed run)",
                hint=_SESSIONS_HINT,
            )
        started = time.perf_counter()
        client.logout()
        self.ctx.client = None
        elapsed = time.perf_counter() - started
        if mine is None:
            self.report("ok", "logged out", elapsed=elapsed)
            return
        # Log in once more to check that the first session really disappeared.
        check = self._make_client(self.ctx.password)
        try:
            check.login()
            after = _sessions(check.get("auth/sessions"))
        except PiholeError as exc:
            self.report("warn", f"logged out, but could not verify it: {exc}", elapsed=elapsed)
            return
        finally:
            check.logout()
        leaked = [
            s
            for s in after
            if not s.get("current_session")
            and s.get("id") == mine.get("id")
            and s.get("login_at") == mine.get("login_at")
        ]
        if leaked:
            self.report(
                "fail",
                "the session was still open after logging out",
                hint="please report this at https://github.com/pavelgar/pihello/issues",
                exit_code=5,
            )
        else:
            self.report("ok", "logged out; no session left behind", elapsed=elapsed)


def tcp_reachable(host: str, port: int, timeout: float) -> bool:
    """Whether a TCP connection to ``host:port`` succeeds within ``timeout`` seconds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _sessions(data: Any) -> list[dict[str, Any]]:
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return [s for s in sessions if isinstance(s, dict)] if isinstance(sessions, list) else []


def _duration(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


def _major(version: str) -> int | None:
    digits = version.lstrip("vV").split(".", 1)[0]
    return int(digits) if digits.isdigit() else None


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def run(
    args: argparse.Namespace,
    ui: UI,
    *,
    stdout: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Entry point of ``pihello doctor``; returns the exit code."""
    from pihello.cli import _exit_on_termination  # noqa: PLC0415

    with _exit_on_termination():  # log out even when killed
        return Doctor(args, ui, sys.stdout if stdout is None else stdout, env=env).run()
