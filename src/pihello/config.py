"""Configuration: the TOML file, profiles, environment variables and secrets.

Settings are resolved from several *layers*; the first layer that sets a key
wins (highest precedence first)::

    command line  →  environment  →  selected profile  →  top-level config  →  defaults

Two groups of keys are resolved *as a unit*, so that a higher layer replaces the
whole choice instead of mixing with a lower one:

* the **credential** — ``password`` | ``password_file`` | ``password_command``
  (plus ``--password-stdin`` on the command line);
* the **template** — ``preset`` | ``template`` (plus ``--template-string``).

Within a single layer at most one key of each group may be set.

Every resolved value remembers where it came from (``Settings.origins``) so
``pihello doctor`` can explain the effective configuration. The password itself
is only obtained on demand (:meth:`Settings.password`) — reading a file or running
a password manager is skipped entirely when the greeting is served from the
cache or demo data — and it never appears in ``repr()`` output.

This module also contains the small, dependency-free TOML *writer* used by
``pihello init`` (:func:`dumps`) and an atomic, private file writer
(:func:`write_private`).
"""

from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TextIO

from pihello.errors import ConfigError

__all__ = [
    "COLOR_MODES",
    "DEFAULTS",
    "ENV_VARS",
    "KEYS",
    "MAX_TAB_SIZE",
    "PASSWORD_COMMAND_TIMEOUT",
    "ConfigFile",
    "Credential",
    "Settings",
    "TemplateChoice",
    "default_config_path",
    "dumps",
    "find_config_path",
    "load_file",
    "read_password_file",
    "resolve",
    "run_password_command",
    "write_private",
]

_WINDOWS = os.name == "nt"

#: Seconds a ``password_command`` may run before it is killed.
PASSWORD_COMMAND_TIMEOUT = 30.0

COLOR_MODES = ("auto", "always", "never", "16", "256", "truecolor")
OVERFLOW_MODES = ("wrap", "clip")

#: Characters that make a ``password_command`` string run through the shell
#: (quotes alone do not: ``shlex`` handles them).
_SHELL_CHARS = frozenset("|&;<>()$`*?[]{}~!#\n")


# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #
def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (_is_int(value) or isinstance(value, float)) and math.isfinite(value)


def _check_str(key: str, value: Any) -> str:
    if not isinstance(value, str):
        raise _type_error(key, value, "a string")
    return value


def _check_secret(key: str, value: Any) -> str:
    """Like :func:`_check_str`, but the error never shows the (secret) value."""
    if not isinstance(value, str):
        kinds = (
            (bool, "true/false"),
            ((int, float), "a number"),
            (list, "a list"),
            (dict, "a table"),
        )
        kind = next((name for types, name in kinds if isinstance(value, types)), "a date or time")
        raise ConfigError(
            f"invalid value for {key}: {kind} (value hidden); expected a string",
            hint='put the password in quotes: password = "…"',
        )
    return value


def _check_nonempty_str(key: str, value: Any) -> str:
    text = _check_str(key, value)
    if not text.strip():
        raise ConfigError(f"{key} must not be empty")
    return text


def _check_bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _type_error(key, value, "true or false")
    return value


def _check_nonneg_int(key: str, value: Any) -> int:
    if not _is_int(value) or value < 0:
        raise _type_error(key, value, "a whole number ≥ 0")
    return int(value)


#: Largest accepted ``tab_size`` (0 removes tabs).
MAX_TAB_SIZE = 32


def _check_tab_size(key: str, value: Any) -> int:
    if not _is_int(value) or not 0 <= value <= MAX_TAB_SIZE:
        raise _type_error(key, value, f"a whole number from 0 (no tabs) to {MAX_TAB_SIZE}")
    return int(value)


def _check_positive_number(key: str, value: Any) -> float:
    if not _is_number(value) or value <= 0:
        raise _type_error(key, value, "a number of seconds > 0")
    return float(value)


def _check_nonneg_number(key: str, value: Any) -> float:
    if not _is_number(value) or value < 0:
        raise _type_error(key, value, "a number of seconds ≥ 0")
    return float(value)


def _check_verify(key: str, value: Any) -> bool | str:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value
    raise _type_error(key, value, 'true, false or the path of a CA bundle ("/path/to/ca.pem")')


def _check_color(key: str, value: Any) -> str:
    text = str(value) if _is_int(value) else value  # color = 256 is fine too
    if not isinstance(text, str) or text.strip().lower() not in COLOR_MODES:
        raise _type_error(key, value, "one of " + ", ".join(COLOR_MODES))
    return text.strip().lower()


def _check_width(key: str, value: Any) -> int | Literal["auto"]:
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    if _is_int(value) and value >= 0:
        return int(value)
    raise _type_error(key, value, '"auto", 0 (unlimited) or a number of columns')


def _check_overflow(key: str, value: Any) -> str:
    if not isinstance(value, str) or value.lower() not in OVERFLOW_MODES:
        raise _type_error(key, value, '"wrap" or "clip"')
    return value.lower()


def _check_timestamp(key: str, value: Any) -> bool | str:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value:
        return value
    raise _type_error(key, value, 'true, false or a strftime format such as "%H:%M"')


def _check_command(key: str, value: Any) -> str | tuple[str, ...]:
    if isinstance(value, str) and value.strip():
        return value
    if (
        isinstance(value, list | tuple)
        and value
        and all(isinstance(part, str) for part in value)
        and value[0]
    ):
        return tuple(value)
    raise _type_error(key, value, 'a command string or a list such as ["pass", "show", "pihole"]')


def _type_error(key: str, value: Any, expected: str) -> ConfigError:
    shown = repr(value) if not isinstance(value, dict) else "a table"
    return ConfigError(f"invalid value for {key}: {shown}; expected {expected}")


@dataclass(frozen=True)
class KeySpec:
    """Validation rule and documentation of one configuration key."""

    check: Callable[[str, Any], Any]
    description: str


#: Every key accepted at the top level of the config file and in profiles.
KEYS: dict[str, KeySpec] = {
    "url": KeySpec(_check_nonempty_str, "Pi-hole address, e.g. http://pi.hole"),
    "password": KeySpec(_check_secret, "app password (keep the file private!)"),
    "password_file": KeySpec(_check_nonempty_str, "file whose first line is the password"),
    "password_command": KeySpec(_check_command, "command that prints the password"),
    "verify_tls": KeySpec(_check_verify, "verify HTTPS certificates: true, false or CA path"),
    "timeout": KeySpec(_check_positive_number, "network timeout in seconds"),
    "preset": KeySpec(_check_nonempty_str, "built-in template name"),
    "template": KeySpec(_check_nonempty_str, "path of a template file"),
    "theme": KeySpec(_check_nonempty_str, "color theme name"),
    "color": KeySpec(_check_color, "auto, always, never, 16, 256 or truecolor"),
    "width": KeySpec(_check_width, '"auto", 0 (unlimited) or columns'),
    "height": KeySpec(_check_nonneg_int, "maximum lines (0 = unlimited)"),
    "overflow": KeySpec(_check_overflow, "wrap or clip long lines"),
    "tab_size": KeySpec(_check_tab_size, "tab stop width (0 = remove tabs, at most 32)"),
    "missing": KeySpec(_check_str, "placeholder for unavailable values"),
    "strict": KeySpec(_check_bool, "fail on unavailable values"),
    "cache_ttl": KeySpec(_check_nonneg_number, "seconds to reuse fetched data (0 = off)"),
    "timestamp": KeySpec(_check_timestamp, "print a timestamp line first"),
}

#: Default value of every setting that has one.
DEFAULTS: dict[str, Any] = {
    "url": None,
    "verify_tls": True,
    "timeout": 3.0,
    "preset": "default",
    "theme": "default",
    "color": "auto",
    "width": "auto",
    "height": 0,
    "overflow": "wrap",
    "tab_size": 4,
    "missing": "?",
    "strict": False,
    "cache_ttl": 0.0,
    "timestamp": False,
}

#: Environment variable → setting key.
ENV_VARS: dict[str, str] = {
    "PIHELLO_URL": "url",
    "PIHELLO_PASSWORD": "password",
    "PIHELLO_PASSWORD_FILE": "password_file",
    "PIHELLO_PRESET": "preset",
    "PIHELLO_THEME": "theme",
}

_CREDENTIAL_KEYS = ("password", "password_file", "password_command", "password_stdin")
_TEMPLATE_KEYS = ("preset", "template", "template_string")
_PATH_KEYS = ("password_file", "template")
_SIMPLE_KEYS = tuple(
    key for key in KEYS if key not in _CREDENTIAL_KEYS and key not in _TEMPLATE_KEYS
)
_TOP_LEVEL_TABLES = ("styles", "profiles")


def _did_you_mean(word: str, choices: Sequence[str]) -> str | None:
    import difflib  # noqa: PLC0415 - error path only

    close = difflib.get_close_matches(word, list(choices), n=1, cutoff=0.6)
    return f"did you mean {close[0]!r}?" if close else None


# --------------------------------------------------------------------------- #
# Locating and loading the file                                               #
# --------------------------------------------------------------------------- #
def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the per-user config path (ignoring ``PIHELLO_CONFIG``).

    ``$XDG_CONFIG_HOME/pihello/config.toml`` (default ``~/.config/…``), or
    ``%APPDATA%\\pihello\\config.toml`` on Windows.
    """
    env = os.environ if env is None else env
    if _WINDOWS and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "pihello" / "config.toml"
    base = env.get("XDG_CONFIG_HOME") or ""
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
    return root / "pihello" / "config.toml"


def find_config_path(
    explicit: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None
) -> tuple[Path, str]:
    """Return ``(path, origin)`` of the config file to use.

    ``explicit`` (``--config``) beats ``$PIHELLO_CONFIG``, which beats
    :func:`default_config_path`. ``origin`` is ``"--config"``,
    ``"PIHELLO_CONFIG"`` or ``"default"``.
    """
    env = os.environ if env is None else env
    if explicit:
        return Path(explicit).expanduser(), "--config"
    if env.get("PIHELLO_CONFIG"):
        return Path(env["PIHELLO_CONFIG"]).expanduser(), "PIHELLO_CONFIG"
    return default_config_path(env), "default"


@dataclass(frozen=True)
class ConfigFile:
    """A parsed and validated config file (or the absence of one)."""

    path: Path
    exists: bool
    settings: dict[str, Any] = field(default_factory=dict, repr=False)
    styles: dict[str, str] = field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    profile_styles: dict[str, dict[str, str]] = field(default_factory=dict)
    mode: int | None = None

    @property
    def contains_password(self) -> bool:
        """Whether the file stores a plain-text ``password`` anywhere."""
        return "password" in self.settings or any(
            "password" in table for table in self.profiles.values()
        )

    @property
    def insecure_permissions(self) -> bool:
        """Whether group or others can read the file (POSIX only)."""
        return self.mode is not None and bool(self.mode & 0o044)

    def permission_warning(self) -> str | None:
        """A warning when the file stores a password and is readable by others."""
        if self.contains_password and self.insecure_permissions:
            return (
                f"{self.path} contains a password but is readable by other users; "
                f"fix it with: chmod 600 {_shell_quote(str(self.path))}"
            )
        return None


def _validate_table(
    table: Mapping[str, Any], where: str, path: Path | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate settings + an optional ``styles`` sub-table. Returns both."""
    settings: dict[str, Any] = {}
    styles: dict[str, str] = {}
    for key, value in table.items():
        if key == "styles":
            styles = _validate_styles(value, f"{where}styles")
            continue
        spec = KEYS.get(key)
        if spec is None:
            hint = _did_you_mean(key, [*KEYS, "styles"])
            raise ConfigError(
                f"unknown setting '{where}{key}'",
                hint=hint
                or "valid keys: https://github.com/pavelgar/pihello/blob/master/docs/configuration.md",
            )
        settings[key] = spec.check(f"{where}{key}", value)
    _check_exclusive(settings, ("password", "password_file", "password_command"), where, path)
    _check_exclusive(settings, ("preset", "template"), where, path)
    return settings, styles


def _validate_styles(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table of style names to style words")
    styles: dict[str, str] = {}
    for name, words in value.items():
        if not isinstance(words, str):
            raise _type_error(f"{where}.{name}", words, 'style words such as "bold #ff8800"')
        styles[name] = words
    return styles


def _check_exclusive(
    settings: Mapping[str, Any], keys: Sequence[str], where: str, path: Path | None = None
) -> None:
    present = [key for key in keys if key in settings]
    if len(present) > 1:
        names = " and ".join(f"{where}{key}" for key in present)
        table = f"[{where.rstrip('.')}] of " if where else ""
        hint = f"keep only one: delete the other lines in {table}{path or 'the config file'}"
        if present == ["preset", "template"]:  # `pihello init` always writes `preset`
            hint = (
                f"to use your own template, delete the `preset` line in {table}"
                f"{path or 'the config file'}"
            )
        raise ConfigError(f"{names} are mutually exclusive", hint=hint)


def load_file(path: str | os.PathLike[str], *, required: bool = False) -> ConfigFile:
    """Read and validate a config file.

    A missing file yields an empty :class:`ConfigFile` (``exists=False``) unless
    ``required`` is true, which raises :class:`ConfigError`.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise ConfigError(
                f"config file not found: {path}",
                hint="create it with `pihello init`, or check --config / PIHELLO_CONFIG",
            ) from None
        return ConfigFile(path=path, exists=False)
    except IsADirectoryError:
        raise ConfigError(f"config path is a directory: {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror or exc}") from None

    if sys.version_info >= (3, 11):
        import tomllib  # noqa: PLC0415 - only needed when a config file exists
    else:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # noqa: PLC0415

    try:
        text = raw.decode("utf-8-sig")
        data = tomllib.loads(text)
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not valid UTF-8") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{path}: invalid TOML: {exc}",
            hint='strings need quotes, e.g. url = "http://pi.hole"',
        ) from None

    try:
        top = {k: v for k, v in data.items() if k != "profiles"}
        settings, styles = _validate_table(top, where="", path=path)
        profiles: dict[str, dict[str, Any]] = {}
        profile_styles: dict[str, dict[str, str]] = {}
        raw_profiles = data.get("profiles", {})
        if not isinstance(raw_profiles, dict):
            raise ConfigError("profiles must be tables, e.g. [profiles.backup]")
        for name, table in raw_profiles.items():
            if not isinstance(table, dict):
                raise ConfigError(f"profiles.{name} must be a table ([profiles.{name}])")
            where = f"profiles.{name}."
            profiles[name], profile_styles[name] = _validate_table(table, where, path)
    except ConfigError as exc:  # name the file: there may be several (--config, profiles)
        raise ConfigError(f"{path}: {exc.message}", hint=exc.hint) from None

    mode: int | None = None
    if os.name != "nt":
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:  # pragma: no cover - raced deletion
            mode = None
    return ConfigFile(
        path=path,
        exists=True,
        settings=settings,
        styles=styles,
        profiles=profiles,
        profile_styles=profile_styles,
        mode=mode,
    )


# --------------------------------------------------------------------------- #
# Credentials                                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Credential:
    """How to obtain the Pi-hole password; the secret itself is never in ``repr``."""

    kind: Literal["password", "file", "command", "stdin"]
    value: str | tuple[str, ...] = field(repr=False)
    origin: str = "default"
    base_dir: Path | None = field(default=None, repr=False)

    def describe(self) -> str:
        """Human readable description that never reveals the password."""
        if self.kind == "password":
            return "set (hidden)"
        if self.kind == "file":
            return f"password_file {self.resolved_path()}"
        if self.kind == "command":
            shown = self.value if isinstance(self.value, str) else " ".join(self.value)
            return f"password_command {shown!r}"
        return "standard input"

    def resolved_path(self) -> Path:
        """The password file path (``~`` expanded, relative to ``base_dir``)."""
        return _resolve_path(str(self.value), self.base_dir)

    def obtain(self, *, stdin: TextIO | None = None) -> str:
        """Return the password, reading files / running commands as needed."""
        if self.kind == "password":
            return str(self.value)
        if self.kind == "file":
            return read_password_file(self.resolved_path())
        if self.kind == "command":
            return run_password_command(self.value)
        stream = sys.stdin if stdin is None else stdin
        line = stream.readline()
        password = line.rstrip("\r\n")
        if not password:
            raise ConfigError("--password-stdin: no password on standard input")
        return password


def _resolve_path(value: str, base_dir: Path | None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path


def read_password_file(path: str | os.PathLike[str]) -> str:
    """Return the first line of ``path`` (without the line ending)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(
            f"password file not found: {path}",
            hint="check password_file / PIHELLO_PASSWORD_FILE, or run `pihello init`",
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        raise ConfigError(f"cannot read password file {path}: {reason}") from None
    password = text.splitlines()[0] if text else ""
    if not password:
        raise ConfigError(f"password file is empty: {path}")
    return password


def _needs_shell(command: str) -> bool:
    return any(ch in _SHELL_CHARS for ch in command)


def run_password_command(
    command: str | Sequence[str], *, timeout: float = PASSWORD_COMMAND_TIMEOUT
) -> str:
    """Run a password manager command and return the first line it prints.

    A list/tuple is executed directly. A string is split like a POSIX shell
    would (``shlex``) and executed directly too, *unless* it contains shell
    syntax (pipes, redirections, ``$VAR``, ``~``, globs, ``;`` …), in which case
    it is run through ``/bin/sh -c`` (``cmd /c`` on Windows). The command
    inherits stdin (so pinentry/TTY prompts work); stdout and stderr are
    captured. It is killed after ``timeout`` seconds.
    """
    import shlex  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    shell = False
    argv: str | list[str]
    if isinstance(command, str):
        shown = command
        if _needs_shell(command):
            shell = True
            argv = command
        else:
            try:
                argv = shlex.split(command, posix=os.name != "nt")
            except ValueError as exc:
                raise ConfigError(f"password_command {command!r}: {exc}") from None
            if not argv:
                raise ConfigError("password_command is empty")
    else:
        argv = list(command)
        shown = shlex.join(argv)
    try:
        result = subprocess.run(
            argv,
            shell=shell,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise ConfigError(
            f"password_command not found: {shown!r}",
            hint="use the full path of the program, or check that it is installed",
        ) from None
    except subprocess.TimeoutExpired:
        raise ConfigError(
            f"password_command timed out after {timeout:g}s: {shown!r}",
            hint="does it wait for input? It must print the password and exit",
        ) from None
    except OSError as exc:
        raise ConfigError(f"cannot run password_command {shown!r}: {exc}") from None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = f": {detail[-1]}" if detail else ""
        raise ConfigError(
            f"password_command failed with exit status {result.returncode}{reason}",
            hint=f"try running it yourself: {shown}",
        )
    output = result.stdout.decode("utf-8", "replace")
    password = output.splitlines()[0].strip() if output.strip() else ""
    if not password:
        raise ConfigError(f"password_command printed nothing: {shown!r}")
    return password


# --------------------------------------------------------------------------- #
# Resolution                                                                  #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TemplateChoice:
    """Which template to render: a preset name, a file path or an inline string."""

    kind: Literal["preset", "file", "string"]
    value: str
    origin: str = "default"
    base_dir: Path | None = field(default=None, repr=False)

    def resolved_path(self) -> Path:
        """The template file path (``~`` expanded, relative to ``base_dir``)."""
        return _resolve_path(self.value, self.base_dir)

    def describe(self) -> str:
        """Short human readable description, e.g. ``preset 'default'``."""
        if self.kind == "preset":
            return f"preset {self.value!r}"
        if self.kind == "file":
            return f"template file {self.resolved_path()}"
        return "inline template string"


@dataclass(frozen=True)
class Settings:
    """The effective configuration after applying every layer."""

    url: str | None
    credential: Credential | None
    verify_tls: bool | str
    timeout: float
    template: TemplateChoice
    theme: str
    color: str
    width: int | Literal["auto"]
    height: int
    overflow: Literal["wrap", "clip"]
    tab_size: int
    missing: str
    strict: bool
    cache_ttl: float
    timestamp: bool | str
    styles: dict[str, str]
    profile: str | None
    config: ConfigFile
    config_origin: str
    origins: dict[str, str]
    warnings: tuple[str, ...] = ()

    def password(self, *, stdin: TextIO | None = None) -> str | None:
        """Obtain the password (``None`` when none is configured)."""
        return None if self.credential is None else self.credential.obtain(stdin=stdin)

    @property
    def verify(self) -> bool | str:
        """``verify_tls`` with a CA path expanded, as accepted by the client."""
        if isinstance(self.verify_tls, str):
            return str(Path(self.verify_tls).expanduser())
        return self.verify_tls


@dataclass
class _Layer:
    label: str
    values: dict[str, Any]
    origins: dict[str, str]
    base_dir: Path | None = None
    styles: dict[str, str] = field(default_factory=dict)


def _env_layer(env: Mapping[str, str]) -> _Layer:
    values: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for var, key in ENV_VARS.items():
        raw = env.get(var)
        if raw is None or raw == "":
            continue
        values[key] = raw if key == "password" else KEYS[key].check(var, raw)
        origins[key] = f"environment ({var})"
    present = [var for var, key in ENV_VARS.items() if key in _CREDENTIAL_KEYS and key in values]
    if len(present) > 1:
        raise ConfigError(f"{' and '.join(present)} are both set; set only one of them")
    return _Layer("environment", values, origins)


def _cli_layer(cli: Mapping[str, Any]) -> _Layer:
    values: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for key, raw in cli.items():
        if raw is None or key.startswith("_"):
            continue
        if key == "styles":
            continue
        if key == "password_stdin":
            if raw:
                values[key] = True
                origins[key] = "command line (--password-stdin)"
            continue
        if key == "template_string":
            values[key] = raw
        elif key in KEYS:
            values[key] = KEYS[key].check(_flag_name(key, cli), raw)
        else:
            raise ConfigError(f"internal: unknown command-line setting {key!r}")
        origins[key] = f"command line ({_flag_name(key, cli)})"
    for group in (_CREDENTIAL_KEYS, _TEMPLATE_KEYS):
        present = [key for key in group if key in values]
        if len(present) > 1:
            flags = " and ".join(_flag_name(key, cli) for key in present)
            raise ConfigError(f"{flags} cannot be used together")
    styles = dict(cli.get("styles") or {})
    return _Layer("command line", values, origins, base_dir=None, styles=styles)


def _flag_name(key: str, cli: Mapping[str, Any]) -> str:
    flags: Mapping[str, str] = cli.get("_flags") or {}
    return flags.get(key) or "--" + key.replace("_", "-")


def resolve(
    cli: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    profile: str | None = None,
) -> Settings:
    """Combine command line, environment, profile, config file and defaults.

    Args:
        cli: settings given on the command line, keyed like the config file
            (``None`` values are ignored). Extra keys: ``password_stdin``
            (bool), ``template_string`` (str), ``styles`` (dict) and
            ``_flags`` (key → flag spelling, for error messages).
        env: the environment (default ``os.environ``).
        config_path: ``--config`` value, if any.
        profile: ``--profile`` value, if any (falls back to ``PIHELLO_PROFILE``).

    Raises:
        ConfigError: invalid file, values, unknown profile or conflicting keys.
    """
    env = os.environ if env is None else env
    path, path_origin = find_config_path(config_path, env)
    file = load_file(path, required=path_origin != "default")

    profile_name = profile or env.get("PIHELLO_PROFILE") or None
    profile_origin = "command line (--profile)" if profile else "environment (PIHELLO_PROFILE)"
    layers = [_cli_layer(cli or {}), _env_layer(env)]
    config_dir = path.parent
    if profile_name is not None:
        if profile_name not in file.profiles:
            available = ", ".join(sorted(file.profiles)) or "none defined"
            hint = _did_you_mean(profile_name, list(file.profiles))
            where = str(path) if file.exists else f"{path} (which does not exist)"
            raise ConfigError(
                f"unknown profile {profile_name!r} in {where}",
                hint=f"{hint + ' ' if hint else ''}available profiles: {available}",
            )
        origin = f"profile {profile_name!r} ({path})"
        layers.append(
            _Layer(
                f"profile {profile_name!r}",
                dict(file.profiles[profile_name]),
                dict.fromkeys(file.profiles[profile_name], origin),
                base_dir=config_dir,
                styles=file.profile_styles.get(profile_name, {}),
            )
        )
    layers.append(
        _Layer(
            "config file",
            dict(file.settings),
            dict.fromkeys(file.settings, f"config file ({path})"),
            base_dir=config_dir,
            styles=file.styles,
        )
    )

    values: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for key in _SIMPLE_KEYS:
        for layer in layers:
            if key in layer.values:
                values[key] = layer.values[key]
                origins[key] = layer.origins[key]
                if key == "verify_tls" and isinstance(values[key], str):
                    # a CA path in the config file is relative to the config directory
                    values[key] = str(_resolve_path(values[key], layer.base_dir))
                break
        else:
            values[key] = DEFAULTS[key]
            origins[key] = "default"

    credential: Credential | None = None
    for layer in layers:
        ckey = next((k for k in _CREDENTIAL_KEYS if k in layer.values), None)
        if ckey is None:
            continue
        value = layer.values[ckey]
        kind: Literal["password", "file", "command", "stdin"] = {
            "password": "password",
            "password_file": "file",
            "password_command": "command",
            "password_stdin": "stdin",
        }[ckey]  # type: ignore[assignment]
        credential = Credential(kind, value, layer.origins[ckey], layer.base_dir)
        origins["password"] = layer.origins[ckey]
        break
    else:
        origins["password"] = "not set"  # noqa: S105 - an origin label, not a secret

    template = TemplateChoice("preset", DEFAULTS["preset"], "default")
    for layer in layers:
        tkey = next((k for k in _TEMPLATE_KEYS if k in layer.values), None)
        if tkey is None:
            continue
        tkind: Literal["preset", "file", "string"] = {
            "preset": "preset",
            "template": "file",
            "template_string": "string",
        }[tkey]  # type: ignore[assignment]
        template = TemplateChoice(tkind, layer.values[tkey], layer.origins[tkey], layer.base_dir)
        break
    origins["template"] = template.origin

    styles: dict[str, str] = {}
    style_origin = "default"
    for layer in reversed(layers):
        if layer.styles:
            styles.update(layer.styles)
            style_origin = layer.label
    origins["styles"] = style_origin if styles else "default"

    if profile_name is not None:
        origins["profile"] = profile_origin
    origins["config"] = path_origin

    warnings: list[str] = []
    warning = file.permission_warning()
    if warning:
        warnings.append(warning)

    return Settings(
        url=values["url"],
        credential=credential,
        verify_tls=values["verify_tls"],
        timeout=values["timeout"],
        template=template,
        theme=values["theme"],
        color=values["color"],
        width=values["width"],
        height=values["height"],
        overflow=values["overflow"],
        tab_size=values["tab_size"],
        missing=values["missing"],
        strict=values["strict"],
        cache_ttl=values["cache_ttl"],
        timestamp=values["timestamp"],
        styles=styles,
        profile=profile_name,
        config=file,
        config_origin=path_origin,
        origins=origins,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- #
# Writing                                                                     #
# --------------------------------------------------------------------------- #
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_string(text: str) -> str:
    out = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_string(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        items = ", ".join(
            f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items() if v is not None
        )
        return "{ " + items + " }" if items else "{}"
    raise TypeError(f"cannot write {type(value).__name__} values to TOML")


def dumps(data: Mapping[str, Any], *, header: str | None = None) -> str:
    """Serialise ``data`` as TOML.

    Nested mappings become ``[tables]`` (``[a.b]`` for deeper levels); ``None``
    values are skipped; lists are written inline. ``header`` lines are written
    as ``#`` comments at the top.
    """
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
        lines.append("")
    _dump_table(data, [], lines)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def _dump_table(data: Mapping[str, Any], prefix: list[str], lines: list[str]) -> None:
    scalars = [(k, v) for k, v in data.items() if v is not None and not isinstance(v, Mapping)]
    tables = [(k, v) for k, v in data.items() if isinstance(v, Mapping)]
    if prefix and (scalars or not tables):
        if lines and lines[-1]:
            lines.append("")
        lines.append("[" + ".".join(_toml_key(part) for part in prefix) + "]")
    lines.extend(f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in scalars)
    for key, table in tables:
        _dump_table(table, [*prefix, str(key)], lines)


def write_private(path: str | os.PathLike[str], text: str | bytes) -> None:
    """Atomically write ``text`` (UTF-8, ``\\n`` line endings; or raw bytes) to ``path``.

    The file gets mode ``0600``; missing parent directories are created with
    mode ``0700``. The content is written to a temporary file in the same
    directory, flushed to disk and renamed over the target, so readers never
    observe a partial file.
    """
    import tempfile  # noqa: PLC0415

    data = text.encode("utf-8") if isinstance(text, str) else text
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _shell_quote(text: str) -> str:
    import shlex  # noqa: PLC0415

    return shlex.quote(text)
