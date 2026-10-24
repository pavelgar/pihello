"""Exception hierarchy shared by every pihello module.

Every error that pihello raises on purpose derives from :class:`PiHelloError`.
The CLI catches ``PiHelloError``, prints ``str(error)`` (plus ``error.hint`` when
set) to stderr, and exits with ``error.exit_code``. Anything else that escapes
is a bug and exits with :data:`EXIT_INTERNAL`.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_CONNECTION = 3
EXIT_AUTH = 4
EXIT_API = 5


class PiHelloError(Exception):
    """Base class for all expected pihello errors."""

    exit_code: int = EXIT_INTERNAL

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------- #
# Usage / configuration / templates                                           #
# --------------------------------------------------------------------------- #
class ConfigError(PiHelloError):
    """The configuration file, environment or command line is invalid."""

    exit_code = EXIT_USAGE


class PasswordUnavailable(ConfigError):
    """``password_command`` ran but gave no password: it failed, timed out or printed nothing.

    Usually a runtime condition (a locked password manager) rather than a
    mistake, so ``-q`` keeps it quiet like an unreachable Pi-hole.
    """


class TemplateError(PiHelloError):
    """A template could not be parsed or rendered.

    ``line`` and ``column`` are 1-based positions in ``source_name`` when known;
    ``excerpt`` is the offending source line with a caret under ``column``.
    """

    exit_code = EXIT_USAGE

    def __init__(
        self,
        message: str,
        *,
        source_name: str = "<template>",
        line: int | None = None,
        column: int | None = None,
        hint: str | None = None,
        excerpt: str | None = None,
    ) -> None:
        location = source_name
        if line is not None:
            location += f":{line}"
            if column is not None:
                location += f":{column}"
        super().__init__(f"{location}: {message}", hint=hint)
        self.source_name = source_name
        self.line = line
        self.column = column
        self.reason = message
        self.excerpt = excerpt


# --------------------------------------------------------------------------- #
# Talking to Pi-hole                                                           #
# --------------------------------------------------------------------------- #
class PiholeError(PiHelloError):
    """Base class for errors talking to a Pi-hole instance."""

    exit_code = EXIT_API


class ConnectionFailed(PiholeError):
    """Pi-hole could not be reached (DNS, refused, timeout, TLS, not a Pi-hole)."""

    exit_code = EXIT_CONNECTION


class TLSError(ConnectionFailed):
    """The TLS handshake or certificate verification failed."""


class AuthenticationError(PiholeError):
    """Pi-hole rejected the credentials (or requires something we can't provide)."""

    exit_code = EXIT_AUTH


class TwoFactorRequired(AuthenticationError):
    """The account has TOTP enabled; an app password must be used instead."""


class TooManySessions(AuthenticationError):
    """Pi-hole has no free API seats (``webserver.api.max_sessions`` reached)."""


class APIError(PiholeError):
    """Pi-hole answered with an unexpected HTTP status or malformed payload.

    ``status`` is the HTTP status code (``None`` for malformed payloads), and
    ``key``/``api_message`` mirror Pi-hole's ``{"error": {"key", "message", "hint"}}``.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        key: str | None = None,
        api_message: str | None = None,
        path: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.key = key
        self.api_message = api_message
        self.path = path


class EndpointNotFound(APIError):
    """The endpoint does not exist on this Pi-hole version (HTTP 404)."""
