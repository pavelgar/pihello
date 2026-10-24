"""The ``pihello`` command line.

``main(argv)`` parses the arguments, dispatches to a subcommand and turns every
:class:`~pihello.errors.PiHelloError` into ``pihello: <message>`` /
``hint: <hint>`` on stderr plus the error's exit code. When the first argument
is not a subcommand, ``show`` is implied, so ``pihello`` alone renders the
greeting and pihello 1.x invocations (``pihello ADDR PASSWORD -f FILE``) keep
working.

pihello runs from shell startup files, so this module is careful to import only
what the requested command needs: the show path never imports the wizard,
doctor or completion code, and ``--demo`` never imports the HTTP client.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TextIO

from pihello import __version__
from pihello.errors import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USAGE,
    AuthenticationError,
    ConfigError,
    ConnectionFailed,
    PiHelloError,
    PiholeError,
    TooManySessions,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pihello.client import PiholeClient
    from pihello.config import Settings, TemplateChoice
    from pihello.sources import FetchProblem
    from pihello.template import Template

__all__ = ["COMMANDS", "ISSUES_URL", "build_parser", "main"]

ISSUES_URL = "https://github.com/pavelgar/pihello/issues"

#: Subcommand → one-line description (also used by completion scripts).
COMMANDS: dict[str, str] = {
    "show": "render the greeting (default when no command is given)",
    "init": "interactive setup: connect, test, write the config, shell snippet",
    "presets": "list the built-in templates and preview them",
    "themes": "list the color themes and preview them",
    "vars": "list template variables with live or demo values",
    "doctor": "diagnose config, connectivity, TLS, authentication and sessions",
    "colors": "print the named color palette",
    "completion": "print a shell completion script (bash, zsh, fish)",
}

COLOR_MODES = ("auto", "always", "never", "16", "256", "truecolor")
DEFAULT_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_WATCH_INTERVAL = 5.0
LEGACY_PRESET = "classic"
MIN_REDACTED = 4

_SHOW_USAGE = """\
pihello [show] [OPTIONS]
       pihello COMMAND [OPTIONS]
       pihello ADDR [PASSWORD] [OPTIONS]   (pihello 1.x style)"""

_MAIN_DESCRIPTION = (
    "Print a styled Pi-hole v6 status greeting in your terminal.\n\n"
    "Commands:\n"
    + "".join(f"  {name:<12}{text}\n" for name, text in COMMANDS.items())
    + '\nRun "pihello COMMAND --help" for the options of a command.'
)

_MAIN_EPILOG = """\
Examples:
  pihello init                       set everything up interactively
  pihello                            show the greeting using your config
  pihello -q                         for shell startup files: never print errors
  pihello --preset compact -T nord   another built-in template and theme
  pihello -u https://pi.hole -k      one-off, with a self-signed certificate
  pihello -e '{queries.percent_blocked|percent} blocked'
                                     inline template, e.g. for a status bar
  pihello --demo --preset dashboard  try a preset without a Pi-hole
  pihello --watch 10                 live dashboard, refreshed every 10 s
  pihello -P backup                  use [profiles.backup] from the config

Configuration: ~/.config/pihello/config.toml ($PIHELLO_CONFIG); run
"pihello doctor" to see where every setting comes from.
Environment: PIHELLO_URL, PIHELLO_PASSWORD, PIHELLO_PASSWORD_FILE,
PIHELLO_PRESET, PIHELLO_THEME, PIHELLO_PROFILE, PIHELLO_CONFIG, NO_COLOR.
Exit status: 0 ok, 2 usage/config/template, 3 unreachable, 4 authentication,
5 unexpected API response, 1 internal error.
"""


# --------------------------------------------------------------------------- #
# Messages on stderr                                                          #
# --------------------------------------------------------------------------- #
def _supports_color(
    stream: TextIO, env: Mapping[str, str] | None = None, *, mode: str = "auto"
) -> bool:
    """Whether to color our own text on ``stream`` (NO_COLOR, TTY, TERM, ``--color``).

    The same rules as for the greeting (:func:`pihello.terminal.detect_color_depth`),
    including turning on (or detecting the lack of) escape-code support in
    Windows consoles.
    """
    from pihello import terminal  # noqa: PLC0415

    depth = terminal.detect_color_depth(mode, stream=stream, env=env)
    return depth is not terminal.ColorDepth.NONE


class UI:
    """Writes pihello's own messages (errors, warnings, debug) to stderr.

    Honours ``--quiet`` and NO_COLOR, and redacts every registered secret, so a
    password can never leak through an error message or a debug log line.
    """

    def __init__(self, stderr: TextIO | None = None) -> None:
        """Create a UI writing to ``stderr`` (default ``sys.stderr``)."""
        self.stderr = sys.stderr if stderr is None else stderr
        self.quiet = False
        self.debug = False
        self.secrets: set[str] = set()
        self._handler: Any = None
        self._color = _supports_color(self.stderr)

    # -- formatting --------------------------------------------------------
    def paint(self, text: str, code: str) -> str:
        """Wrap ``text`` in SGR ``code`` when colors are enabled."""
        return f"\x1b[{code}m{text}\x1b[0m" if self._color else text

    def redact(self, text: str) -> str:
        """Replace every registered secret in ``text`` with ``********``.

        Secrets shorter than :data:`MIN_REDACTED` characters are left alone:
        pihello never puts a password into a message on purpose, this is only
        a safety net, and blanking out every ``a`` would garble all output.
        """
        for secret in sorted(self.secrets, key=len, reverse=True):
            if len(secret) >= MIN_REDACTED:
                text = text.replace(secret, "********")
        return text

    def add_secret(self, secret: str | None) -> None:
        """Register a value that must never be printed."""
        if secret:
            self.secrets.add(secret)

    def _write(self, text: str) -> None:
        try:
            self.stderr.write(self.redact(text) + "\n")
            self.stderr.flush()
        except (OSError, ValueError):  # pragma: no cover - stderr closed
            pass

    # -- messages ----------------------------------------------------------
    def error(self, message: str, hint: str | None = None, *, excerpt: str | None = None) -> None:
        """``pihello: message`` (+ an indented source ``excerpt`` and ``hint: …``), unless quiet."""
        if self.quiet:
            return
        self._write(f"{self.paint('pihello:', '1;31')} {message}")
        if excerpt:
            self._write("\n".join("    " + line for line in excerpt.splitlines()))
        if hint:
            self._write(f"{self.paint('hint:', '36')} {hint}")

    def warn(self, message: str) -> None:
        """``pihello: warning: message``, unless quiet."""
        if not self.quiet:
            self._write(f"{self.paint('pihello: warning:', '1;33')} {message}")

    def log(self, message: str) -> None:
        """A debug line (only with ``--debug``)."""
        if self.debug:
            self._write(self.paint(f"pihello: debug: {message}", "2"))

    # -- logging -----------------------------------------------------------
    def enable_debug(self) -> None:
        """Route the ``pihello`` logger to stderr, with secrets redacted."""
        import logging  # noqa: PLC0415 - only with --debug

        self.debug = True
        ui = self

        class _Redact(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                record.msg = ui.redact(record.getMessage())
                record.args = ()
                return True

        handler = logging.StreamHandler(self.stderr)
        handler.setFormatter(logging.Formatter("pihello: debug: %(message)s"))
        handler.addFilter(_Redact())
        logger = logging.getLogger("pihello")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self._handler = handler

    def close(self) -> None:
        """Detach the debug handler (``main`` may run many times in tests)."""
        if self._handler is not None:
            import logging  # noqa: PLC0415

            logger = logging.getLogger("pihello")
            logger.removeHandler(self._handler)
            logger.setLevel(logging.NOTSET)
            self._handler = None


# --------------------------------------------------------------------------- #
# argparse                                                                    #
# --------------------------------------------------------------------------- #
class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep our hand-formatted description/epilog; align option help."""

    def __init__(self, prog: str) -> None:
        import shutil  # noqa: PLC0415

        width = min(shutil.get_terminal_size((100, 24)).columns, 100) - 2
        super().__init__(prog, max_help_position=34, width=max(width, 60))


def _parser(prog: str, **kwargs: Any) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        formatter_class=_HelpFormatter,
        allow_abbrev=False,
        add_help=False,
        **kwargs,
    )
    parser._positionals.title = "Arguments"
    return parser


def _nonneg_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be ≥ 0, got {value}")
    return value


def _tab_size_type(text: str) -> int:
    from pihello.config import MAX_TAB_SIZE  # noqa: PLC0415

    value = _nonneg_int(text)
    if value > MAX_TAB_SIZE:
        raise argparse.ArgumentTypeError(
            f"must be a whole number from 0 (no tabs) to {MAX_TAB_SIZE}, got {value}"
        )
    return value


def _nonneg_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of seconds, got {text!r}") from None
    if not value >= 0 or value == float("inf"):
        raise argparse.ArgumentTypeError(f"must be a number ≥ 0, got {text!r}")
    return value


def _positive_float(text: str) -> float:
    value = _nonneg_float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {text!r}")
    return value


def _width_type(text: str) -> int | str:
    if text.strip().lower() == "auto":
        return "auto"
    try:
        return _nonneg_int(text)
    except argparse.ArgumentTypeError:
        raise argparse.ArgumentTypeError(
            f"expected 'auto' or a number of columns, got {text!r}"
        ) from None


def _style_type(text: str) -> tuple[str, str]:
    name, sep, words = text.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(
            f"expected NAME=WORDS (e.g. 'accent=bold red'), got {text!r}"
        )
    return name.strip(), words.strip()


def _add_help(group: Any) -> None:
    group.add_argument("-h", "--help", action="help", help="show this help and exit")


def _add_common(group: Any, *, color: bool = True) -> None:
    """``--config``, ``-P``, ``--color``, ``-q``, ``--debug``, ``-h``."""
    group.add_argument(
        "--config", metavar="PATH", help="config file (default: ~/.config/pihello/config.toml)"
    )
    group.add_argument(
        "-P", "--profile", metavar="NAME", help="use the settings of [profiles.NAME]"
    )
    if color:
        _add_color(group)
    group.add_argument("-q", "--quiet", action="store_true", help="do not print errors or warnings")
    group.add_argument(
        "--debug", action="store_true", help="log HTTP requests and timings to stderr"
    )
    _add_help(group)


def _add_color(group: Any) -> None:
    group.add_argument(
        "--color",
        choices=COLOR_MODES,
        metavar="WHEN",
        help="auto (default), always, never, 16, 256 or truecolor",
    )
    group.add_argument(
        "--no-color",
        dest="color",
        action="store_const",
        const="never",
        help="same as --color never (NO_COLOR is honoured too)",
    )


def _add_connection(group: Any, *, url: bool = True) -> None:
    """URL, password sources, TLS and timeout options."""
    if url:
        group.add_argument(
            "-u", "--url", metavar="URL", help="Pi-hole address, e.g. pi.hole or https://10.0.0.2"
        )
    group.add_argument(
        "--password-file", metavar="PATH", help="read the app password from the first line of PATH"
    )
    group.add_argument(
        "--password-command",
        metavar="CMD",
        help="run CMD (e.g. 'pass show pihole') and use the first line it prints",
    )
    group.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    group.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="do not verify the HTTPS certificate (self-signed)",
    )
    group.add_argument(
        "--ca-cert", metavar="PATH", help="verify HTTPS against this CA / self-signed cert"
    )
    group.add_argument(
        "--timeout", type=_positive_float, metavar="SECONDS", help="network timeout (default: 3)"
    )


def _show_parser() -> argparse.ArgumentParser:
    parser = _parser(
        "pihello", usage=_SHOW_USAGE, description=_MAIN_DESCRIPTION, epilog=_MAIN_EPILOG
    )
    conn = parser.add_argument_group("Connection")
    _add_connection(conn)
    conn.add_argument("-P", "--profile", metavar="NAME", help="use the settings of [profiles.NAME]")
    conn.add_argument(
        "--config", metavar="PATH", help="config file (default: ~/.config/pihello/config.toml)"
    )

    tmpl = parser.add_argument_group("Template")
    tmpl.add_argument(
        "--preset",
        metavar="NAME",
        help="built-in template (default: default); see: pihello presets",
    )
    tmpl.add_argument(
        "-f", "--template", "--file", metavar="FILE", help="render a template file ('-' = stdin)"
    )
    tmpl.add_argument(
        "-e",
        "--template-string",
        metavar="TEXT",
        help="render TEXT, e.g. '{queries.total|number} queries'",
    )
    tmpl.add_argument("-T", "--theme", metavar="NAME", help="color theme; see: pihello themes")
    tmpl.add_argument(
        "--style",
        action="append",
        type=_style_type,
        metavar="NAME=WORDS",
        help="override a theme style, e.g. 'accent=bold #ff8800'",
    )
    tmpl.add_argument(
        "--demo", action="store_true", help="use built-in demo data instead of a Pi-hole"
    )

    out = parser.add_argument_group("Output")
    _add_color(out)
    out.add_argument(
        "-W",
        "--width",
        type=_width_type,
        metavar="N",
        help="wrap at N columns: auto (terminal, default), 0 = no limit",
    )
    out.add_argument(
        "-H",
        "--height",
        type=_nonneg_int,
        metavar="N",
        help="print at most N lines (default: 0 = no limit)",
    )
    out.add_argument(
        "-c",
        "--clip",
        dest="overflow",
        action="store_const",
        const="clip",
        help="cut long lines instead of wrapping them",
    )
    out.add_argument(
        "--wrap",
        dest="overflow",
        action="store_const",
        const="wrap",
        help="wrap long lines (default)",
    )
    out.add_argument(
        "-i",
        "--tab-size",
        "--indent",
        type=_tab_size_type,
        metavar="N",
        help="tab stop width (default: 4)",
    )
    out.add_argument("--missing", metavar="TEXT", help="shown for unavailable values (default: ?)")
    out.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="fail on unavailable values instead of showing --missing",
    )
    out.add_argument(
        "-ts",
        "--timestamp",
        nargs="?",
        const=True,
        metavar="FMT",
        help="print the time first; FMT is strftime, e.g. '%%H:%%M %%a'",
    )

    beh = parser.add_argument_group("Behaviour")
    beh.add_argument(
        "--cache",
        dest="cache_ttl",
        type=_nonneg_float,
        metavar="SECONDS",
        help="reuse data fetched less than SECONDS ago (default: 0 = off)",
    )
    beh.add_argument(
        "--no-cache",
        dest="cache_ttl",
        action="store_const",
        const=0.0,
        help="always fetch fresh data",
    )
    beh.add_argument(
        "--watch",
        nargs="?",
        const=DEFAULT_WATCH_INTERVAL,
        type=_positive_float,
        metavar="SECONDS",
        help=f"live view, refreshed every SECONDS (default: {DEFAULT_WATCH_INTERVAL:g})",
    )
    beh.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print no errors or warnings; the exit status still tells",
    )
    beh.add_argument(
        "--debug", action="store_true", help="log HTTP requests, timings and problems to stderr"
    )
    _add_help(beh)
    beh.add_argument("-v", "-V", "--version", action="version", version=f"pihello {__version__}")

    legacy = parser.add_argument_group("pihello 1.x compatibility")
    legacy.add_argument("addr", nargs="?", metavar="ADDR", help="host or host:port (= --url)")
    legacy.add_argument(
        "password",
        nargs="?",
        metavar="PASSWORD",
        help="deprecated: leaks into shell history and ps; use pihello init",
    )
    legacy.add_argument(
        "-p",
        "--https",
        "--proto",
        dest="https",
        action="store_true",
        help="use https:// for ADDR / --url",
    )
    return parser


def _init_parser() -> argparse.ArgumentParser:
    parser = _parser(
        "pihello init",
        description=(
            "Set up pihello: find your Pi-hole, check the app password, pick a preset and a\n"
            "theme, write the config file and print the line to add to your shell startup file.\n"
            "With --yes nothing is asked: every choice comes from the options."
        ),
        epilog=(
            "Examples:\n"
            "  pihello init\n"
            "  pihello init --url https://pi.hole --password-file ~/.pihole-pw --yes\n"
            "  pass show pihole | pihello init --url pi.hole --password-stdin --yes"
        ),
    )
    conn = parser.add_argument_group("Connection")
    _add_connection(conn)
    conn.add_argument(
        "--store",
        choices=("config", "file", "command", "env", "none"),
        metavar="WHERE",
        help="keep the password in: config, file, command, env or none "
        "(default: ask; with --yes: from the password option)",
    )
    look = parser.add_argument_group("Look")
    look.add_argument("--preset", metavar="NAME", help="built-in template (default: default)")
    look.add_argument("-T", "--theme", metavar="NAME", help="color theme (default: default)")
    beh = parser.add_argument_group("Behaviour")
    beh.add_argument(
        "--color",
        choices=COLOR_MODES,
        metavar="WHEN",
        help="colors of the wizard and its previews (not saved): "
        "auto (default), always, never, 16, 256 or truecolor",
    )
    beh.add_argument(
        "--no-color",
        dest="color",
        action="store_const",
        const="never",
        help="same as --color never",
    )
    beh.add_argument("--config", metavar="PATH", help="config file to write")
    beh.add_argument(
        "-y", "--yes", action="store_true", help="non-interactive: accept every default"
    )
    beh.add_argument("--force", action="store_true", help="overwrite an existing config file")
    beh.add_argument(
        "--no-check", action="store_true", help="do not connect to the Pi-hole to test settings"
    )
    beh.add_argument(
        "--shell",
        choices=("bash", "zsh", "fish", "powershell"),
        metavar="SHELL",
        help="bash, zsh, fish or powershell: the startup snippet to print (default: $SHELL)",
    )
    beh.add_argument("-q", "--quiet", action="store_true", help="do not print warnings")
    beh.add_argument("--debug", action="store_true", help="log HTTP requests to stderr")
    _add_help(beh)
    return parser


def _live_options(group: Any) -> None:
    source = group.add_mutually_exclusive_group()
    source.add_argument(
        "--demo", dest="live", action="store_false", help="preview with demo data (default)"
    )
    source.add_argument(
        "--live", dest="live", action="store_true", help="preview with data from your Pi-hole"
    )
    group.set_defaults(live=False)


def _presets_parser() -> argparse.ArgumentParser:
    parser = _parser(
        "pihello presets",
        description="List the built-in templates (presets) and preview them.",
        epilog=(
            "Examples:\n"
            "  pihello presets                   every preset with a demo preview\n"
            "  pihello presets --no-preview      just the list\n"
            "  pihello presets default --live    preview with your Pi-hole's data\n"
            "  pihello presets default --source -o ~/.config/pihello/greeting.txt\n"
            "                                    start your own template from a preset"
        ),
    )
    parser.add_argument("name", nargs="?", metavar="NAME", help="show only this preset")
    opts = parser.add_argument_group("Options")
    opts.add_argument(
        "--preview",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="render each preset (default: yes)",
    )
    opts.add_argument("--source", action="store_true", help="print the template source of NAME")
    opts.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="with --source: write it to FILE as UTF-8 (creates folders, never overwrites)",
    )
    _live_options(opts)
    opts.add_argument("-T", "--theme", metavar="NAME", help="theme for the previews")
    opts.add_argument(
        "-W", "--width", type=_width_type, metavar="N", help="preview width (default: terminal)"
    )
    conn = parser.add_argument_group("Connection (with --live)")
    _add_connection(conn)
    common = parser.add_argument_group("Common")
    _add_common(common)
    return parser


def _themes_parser() -> argparse.ArgumentParser:
    parser = _parser(
        "pihello themes",
        description=(
            "List the color themes with their style swatches and a preview.\n"
            'Use a theme with -T NAME or theme = "NAME" in the config; restyle single\n'
            "roles in the [styles] table."
        ),
        epilog=(
            "Examples:\n"
            "  pihello themes\n"
            "  pihello themes nord --preset dashboard\n"
            "  pihello themes --no-preview"
        ),
    )
    parser.add_argument("name", nargs="?", metavar="NAME", help="show only this theme")
    opts = parser.add_argument_group("Options")
    opts.add_argument(
        "--preview",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="render a preset with each theme (default: yes)",
    )
    opts.add_argument(
        "--preset",
        metavar="NAME",
        help="preset used for previews (default: your configured preset)",
    )
    _live_options(opts)
    opts.add_argument(
        "-W", "--width", type=_width_type, metavar="N", help="preview width (default: terminal)"
    )
    conn = parser.add_argument_group("Connection (with --live)")
    _add_connection(conn)
    common = parser.add_argument_group("Common")
    _add_common(common)
    return parser


def _vars_parser() -> argparse.ArgumentParser:
    parser = _parser(
        "pihello vars",
        description=(
            "List the variables you can use in templates, with their current values.\n"
            "PATTERN filters paths: a glob ('queries.*') or a substring ('blocked')."
        ),
        epilog=(
            "Examples:\n"
            "  pihello vars                      every documented variable, live values\n"
            "  pihello vars --demo gravity       variables containing 'gravity', demo data\n"
            "  pihello vars 'top_*' --all        every concrete list item too\n"
            "  pihello vars --json | jq .queries\n"
            "                                    the raw variable tree, for scripts"
        ),
    )
    parser.add_argument("pattern", nargs="?", metavar="PATTERN", help="glob or substring")
    opts = parser.add_argument_group("Options")
    opts.add_argument("--demo", action="store_true", help="use demo data instead of your Pi-hole")
    opts.add_argument(
        "--json",
        action="store_true",
        help="print JSON: the nested tree, or {path: value} with PATTERN",
    )
    opts.add_argument(
        "--all",
        action="store_true",
        help="list every concrete path (each list item, undocumented keys)",
    )
    conn = parser.add_argument_group("Connection")
    _add_connection(conn)
    common = parser.add_argument_group("Common")
    _add_common(common)
    return parser


def _doctor_parser() -> argparse.ArgumentParser:
    parser = _parser(
        "pihello doctor",
        description=(
            "Check everything between pihello and your Pi-hole, step by step: Python, the\n"
            "config file, where each setting comes from, DNS, TCP, HTTPS, the API, the\n"
            "password, versions, every data source and session clean-up.\n"
            "Exit status: 0 when everything works (warnings allowed), else the code of the\n"
            "first failure (2 config, 3 connection, 4 authentication, 5 API)."
        ),
    )
    conn = parser.add_argument_group("Connection")
    _add_connection(conn)
    conn.add_argument(
        "-p", "--https", dest="https", action="store_true", help="use https:// for --url"
    )
    common = parser.add_argument_group("Common")
    _add_common(common)
    return parser


def _colors_parser() -> argparse.ArgumentParser:
    parser = _parser(
        "pihello colors",
        description=(
            "Print every named color with a swatch, its number and hex value.\n"
            "Use the names in style tags: [steelblue], [on grey23], [_red underline]."
        ),
    )
    opts = parser.add_argument_group("Options")
    _add_color(opts)
    opts.add_argument(
        "-W", "--width", type=_width_type, metavar="N", help="layout width (default: terminal)"
    )
    _add_help(opts)
    return parser


def _completion_parser() -> argparse.ArgumentParser:
    parser = _parser(
        "pihello completion",
        description="Print a shell completion script.",
        epilog=(
            "Install:\n"
            "  bash  pihello completion bash > ~/.local/share/bash-completion/completions/pihello\n"
            "  zsh   pihello completion zsh > ~/.zfunc/_pihello\n"
            "        (and fpath+=~/.zfunc before compinit in ~/.zshrc)\n"
            "  fish  mkdir -p ~/.config/fish/completions\n"
            "        pihello completion fish > ~/.config/fish/completions/pihello.fish"
        ),
    )
    parser.add_argument("shell", nargs="?", choices=("bash", "zsh", "fish"), help="target shell")
    parser.add_argument(
        "--names",
        choices=("commands", "presets", "themes", "profiles"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--describe", action="store_true", help=argparse.SUPPRESS)
    opts = parser.add_argument_group("Options")
    _add_help(opts)
    return parser


_PARSERS: dict[str, Callable[[], argparse.ArgumentParser]] = {
    "show": _show_parser,
    "init": _init_parser,
    "presets": _presets_parser,
    "themes": _themes_parser,
    "vars": _vars_parser,
    "doctor": _doctor_parser,
    "colors": _colors_parser,
    "completion": _completion_parser,
}


def build_parser(command: str = "show") -> argparse.ArgumentParser:
    """Return a fresh argument parser for ``command`` (``show`` = top level)."""
    return _PARSERS[command]()


# --------------------------------------------------------------------------- #
# Settings from arguments                                                     #
# --------------------------------------------------------------------------- #
_ARG_FLAGS = {
    "url": "--url",
    "password": "the PASSWORD argument",
    "password_file": "--password-file",
    "password_command": "--password-command",
    "password_stdin": "--password-stdin",
    "verify_tls": "--insecure/--ca-cert",
    "preset": "--preset",
    "template": "--template",
    "template_string": "--template-string",
    "theme": "--theme",
    "color": "--color",
    "width": "--width",
    "height": "--height",
    "overflow": "--clip/--wrap",
    "tab_size": "--tab-size",
    "missing": "--missing",
    "strict": "--strict",
    "cache_ttl": "--cache",
    "timestamp": "--timestamp",
    "timeout": "--timeout",
}


def _apply_https(url: str) -> str:
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    if "://" not in url:
        return "https://" + url
    return url


def cli_overrides(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    """Translate parsed arguments into the ``cli`` mapping of :func:`config.resolve`."""

    def get(name: str) -> Any:
        return getattr(args, name, None)

    flags = dict(_ARG_FLAGS)
    url = get("url")
    addr = get("addr")
    if addr is not None:
        if url is not None:
            raise ConfigError("give the Pi-hole address either as ADDR or with --url, not both")
        url = addr
        flags["url"] = "the ADDR argument"
    if url is not None and get("https"):
        url = _apply_https(url)
    if get("insecure") and get("ca_cert"):
        raise ConfigError("--insecure and --ca-cert cannot be used together")
    verify: bool | str | None = False if get("insecure") else get("ca_cert")
    password = get("password")
    if password is not None:
        ui.add_secret(password)
        ui.warn(
            "a password on the command line leaks into shell history and `ps`; "
            "run `pihello init`, or use PIHELLO_PASSWORD or password_file instead"
        )
    if get("password_stdin") and get("template") == "-":
        raise ConfigError("--password-stdin and --template - both want standard input")
    return {
        "url": url,
        "password": password,
        "password_file": get("password_file"),
        "password_command": get("password_command"),
        "password_stdin": bool(get("password_stdin")) or None,
        "verify_tls": verify,
        "timeout": get("timeout"),
        "preset": get("preset"),
        "template": get("template"),
        "template_string": get("template_string"),
        "theme": get("theme"),
        "color": get("color"),
        "width": get("width"),
        "height": get("height"),
        "overflow": get("overflow"),
        "tab_size": get("tab_size"),
        "missing": get("missing"),
        "strict": get("strict"),
        "cache_ttl": get("cache_ttl"),
        "timestamp": get("timestamp"),
        "styles": dict(get("style") or ()),
        "_flags": flags,
    }


def resolve_settings(
    args: argparse.Namespace, ui: UI, *, env: Mapping[str, str] | None = None
) -> Settings:
    """Resolve the effective :class:`~pihello.config.Settings` for ``args``."""
    from dataclasses import replace  # noqa: PLC0415

    from pihello import config  # noqa: PLC0415

    cli = cli_overrides(args, ui)
    settings = config.resolve(
        cli,
        env=env,
        config_path=getattr(args, "config", None),
        profile=getattr(args, "profile", None),
    )
    if getattr(args, "https", False) and cli["url"] is None and settings.url:
        settings = replace(settings, url=_apply_https(settings.url))
    for warning in settings.warnings:
        ui.warn(warning)
    if settings.credential is not None and settings.credential.kind == "password":
        ui.add_secret(str(settings.credential.value))
    ui.log(
        f"config {settings.config.path} ({'found' if settings.config.exists else 'not found'})"
        + (f", profile {settings.profile!r}" if settings.profile else "")
    )
    return settings


# --------------------------------------------------------------------------- #
# Shared helpers for commands that render or fetch                            #
# --------------------------------------------------------------------------- #
def _stdout() -> TextIO:
    return sys.stdout


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _write_new_file(name: str, text: str) -> Path:
    """Write ``text`` as UTF-8 with ``\\n`` line endings to a file that must not exist yet."""
    from pathlib import Path  # noqa: PLC0415

    path = Path(name).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError:
        raise ConfigError(
            f"{path} already exists", hint="choose another file name, or delete that file first"
        ) from None
    except OSError as exc:
        raise ConfigError(f"cannot write {path}: {exc.strerror or exc}") from None
    return path


def _decode_template(data: bytes) -> str:
    """Template files are UTF-8; a UTF-16 BOM (Windows PowerShell 5.1's ``>``) is honoured."""
    import codecs  # noqa: PLC0415 - already loaded at startup

    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def load_template(choice: TemplateChoice, *, stdin: TextIO | None = None) -> Template:
    """Read and parse the template described by ``choice``."""
    from pihello.template import Template  # noqa: PLC0415

    if choice.kind == "preset":
        from pihello import presets  # noqa: PLC0415

        preset = presets.get(choice.value)
        return Template.parse(preset.source, name=f"preset {preset.name!r}")
    if choice.kind == "string":
        return Template.parse(choice.value, name="--template-string")
    if choice.value == "-":
        stream = sys.stdin if stdin is None else stdin
        return Template.parse(stream.read(), name="<stdin>")
    path = choice.resolved_path()
    try:
        source = _decode_template(path.read_bytes())
    except FileNotFoundError:
        raise ConfigError(
            f"template file not found: {path}",
            hint="check --template / the template setting; `pihello presets` lists built-ins",
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        raise ConfigError(f"cannot read template file {path}: {reason}") from None
    return Template.parse(source, name=str(path))


def require_url(settings: Settings) -> str:
    """The configured URL, normalised (``pi.hole`` → ``http://pi.hole``).

    Raises a friendly :class:`ConfigError` pointing at init when there is none.
    """
    if not settings.url:
        hint = (
            "run `pihello init` to set pihello up, or pass --url / set PIHELLO_URL"
            if not settings.config.exists
            else f'add url = "http://pi.hole" to {settings.config.path}, or pass --url'
        )
        raise ConfigError("no Pi-hole address configured", hint=hint)
    from pihello.client import normalize_url  # noqa: PLC0415 - cheap: no urllib.request/ssl

    return normalize_url(settings.url)


def make_client(settings: Settings, ui: UI, *, stdin: TextIO | None = None) -> PiholeClient:
    """Create a :class:`~pihello.client.PiholeClient` (obtains the password)."""
    from pihello.client import PiholeClient  # noqa: PLC0415

    url = require_url(settings)
    started = time.perf_counter()
    password = settings.password(stdin=stdin)
    ui.add_secret(password)
    if settings.credential is not None and settings.credential.kind != "password":
        ui.log(
            f"password from {settings.credential.describe()} "
            f"in {(time.perf_counter() - started) * 1000:.0f} ms"
        )
    return PiholeClient(
        url,
        password,
        timeout=settings.timeout,
        verify=settings.verify,
        user_agent=f"pihello/{__version__}",
    )


def render_options(
    settings: Settings,
    stream: TextIO,
    *,
    now: float | None = None,
    width: int | str | None = None,
    height: int | None = None,
) -> Any:
    """Build :class:`~pihello.template.RenderOptions` for output to ``stream``."""
    from pihello import terminal  # noqa: PLC0415
    from pihello.template import RenderOptions  # noqa: PLC0415

    wanted = settings.width if width is None else width
    columns = terminal.terminal_size(stream)[0] if wanted == "auto" else int(wanted)
    return RenderOptions(
        color=terminal.detect_color_depth(settings.color, stream=stream),
        width=max(columns, 0),
        height=settings.height if height is None else height,
        overflow=settings.overflow,
        tab_size=settings.tab_size,
        strict=settings.strict,
        missing=settings.missing,
        now=now,
    )


def report_problems(problems: Sequence[FetchProblem], ui: UI, *, always: bool = False) -> None:
    """List fetch problems on stderr (debug only unless ``always``)."""
    for problem in problems:
        line = (
            f"{problem.source.endpoint} unavailable "
            f"({', '.join(problem.source.namespaces)} missing): {problem.error}"
        )
        if always:
            ui.warn(line)
        else:
            ui.log(line)


def _fatal_problem(planned: int, problems: Sequence[FetchProblem]) -> PiholeError | None:
    """When *every* source failed for a real reason, that is an error, not a gap."""
    from pihello.errors import EndpointNotFound  # noqa: PLC0415

    real = [p.error for p in problems if not isinstance(p.error, EndpointNotFound)]
    if planned and len(problems) >= planned and real:
        return real[0]
    return None


def collect_live(
    settings: Settings,
    ui: UI,
    paths: Sequence[str] | frozenset[str] | None,
    *,
    stdin: TextIO | None = None,
) -> tuple[dict[str, Any], list[FetchProblem]]:
    """Log in, collect the variables for ``paths`` (``None`` = all) and log out."""
    from pihello import sources  # noqa: PLC0415

    planned = sources.plan(paths) if paths is not None else sources.plan_all()
    url = require_url(settings)
    if not planned:
        ui.log("template needs no data from the Pi-hole")
        return sources.builtins(url=url), []
    ui.log("fetching " + ", ".join(source.endpoint for source in planned))
    started = time.perf_counter()
    client = make_client(settings, ui, stdin=stdin)
    with _exit_on_termination(), client:
        variables, problems = sources.collect(client, paths)
    ui.log(f"collected in {(time.perf_counter() - started) * 1000:.0f} ms (incl. login/logout)")
    fatal = _fatal_problem(len(planned), problems)
    if fatal is not None:
        raise fatal
    return variables, problems


# --------------------------------------------------------------------------- #
# pihello show                                                                #
# --------------------------------------------------------------------------- #
def _namespaces(paths: frozenset[str]) -> list[str]:
    return sorted({path.split(".", 1)[0] for path in paths})


class _Show:
    """State shared by a one-shot render and the ``--watch`` loop."""

    def __init__(
        self, settings: Settings, args: argparse.Namespace, ui: UI, stdout: TextIO
    ) -> None:
        self.settings = settings
        self.args = args
        self.ui = ui
        self.stdout = stdout
        self.demo = bool(getattr(args, "demo", False))
        choice = settings.template
        if getattr(args, "addr", None) is not None and choice.origin == "default":
            from dataclasses import replace  # noqa: PLC0415

            # pihello 1.x users get the exact look they had before.
            choice = replace(choice, value=LEGACY_PRESET, origin="pihello 1.x default")
        self.choice = choice
        started = time.perf_counter()
        self.template = load_template(choice)
        ui.log(
            f"template: {choice.describe()} from {choice.origin}, "
            f"{len(self.template.paths())} variable path(s), parsed in "
            f"{(time.perf_counter() - started) * 1000:.1f} ms"
        )
        self._theme: dict[str, str] | None = None

    @property
    def theme(self) -> dict[str, str]:
        if self._theme is None:
            from pihello import themes  # noqa: PLC0415

            self._theme = themes.resolve(self.settings.theme, self.settings.styles)
        return self._theme

    # -- data ----------------------------------------------------------------
    def demo_variables(self) -> tuple[dict[str, Any], float]:
        from pihello import demo  # noqa: PLC0415

        return demo.variables(), demo.NOW

    def variables(self) -> tuple[dict[str, Any], float | None]:
        """Variables for one render: demo data, the cache or a fresh fetch."""
        if self.demo:
            return self.demo_variables()
        settings = self.settings
        paths = self.template.paths()
        ttl = settings.cache_ttl
        key: str | None = None
        if ttl > 0:
            from pihello import cache, sources  # noqa: PLC0415

            url = require_url(settings)
            key = cache.make_key(url, _namespaces(paths))
            hit = cache.load(key, ttl)
            if hit is not None:
                self.ui.log(f"cache hit ({key}), younger than {ttl:g} s")
                hit.update(sources.builtins(url=url))
                return hit, None
            self.ui.log(f"cache miss ({key})")
        variables, problems = collect_live(settings, self.ui, paths)
        self.handle_problems(problems)
        if key is not None:
            from pihello import cache  # noqa: PLC0415
            from pihello.errors import EndpointNotFound  # noqa: PLC0415

            if all(isinstance(p.error, EndpointNotFound) for p in problems):
                cache.store(key, variables)
        return variables, None

    def handle_problems(self, problems: Sequence[FetchProblem]) -> None:
        if not problems:
            return
        if self.settings.strict:
            report_problems(problems, self.ui, always=True)
            raise problems[0].error
        report_problems(problems, self.ui)

    # -- output --------------------------------------------------------------
    def render(
        self,
        variables: Mapping[str, Any],
        now: float | None,
        *,
        width: int | str | None = None,
        height: int | None = None,
    ) -> str:
        settings = self.settings
        height = settings.height if height is None else height
        stamp = self.timestamp_line()
        if stamp is not None and height == 1:
            return stamp
        if stamp is not None and height > 1:
            height -= 1
        started = time.perf_counter()
        options = render_options(settings, self.stdout, now=now, width=width, height=height)
        text = self.template.render(variables, theme=self.theme, options=options)
        self.ui.log(f"rendered in {(time.perf_counter() - started) * 1000:.1f} ms")
        return text if stamp is None else f"{stamp}\n{text}"

    def timestamp_line(self) -> str | None:
        stamp = self.settings.timestamp
        if stamp is False:
            return None
        from datetime import datetime  # noqa: PLC0415 - only with --timestamp

        fmt = DEFAULT_TIMESTAMP_FORMAT if stamp is True else str(stamp)
        # datetime (like pihello 1.x) also knows %f; aware, so %z / %Z work too
        try:
            return datetime.now().astimezone().strftime(fmt)
        except ValueError as exc:  # Windows rejects unknown % directives
            raise ConfigError(
                f"invalid --timestamp format {fmt!r}: {exc}", hint="e.g. --timestamp '%H:%M %a'"
            ) from None


def _cmd_show(args: argparse.Namespace, ui: UI) -> int:
    started = time.perf_counter()
    stdout = _stdout()
    settings = resolve_settings(args, ui)
    show = _Show(settings, args, ui, stdout)
    if args.watch is not None:
        return _watch(show, args.watch)
    variables, now = show.variables()
    text = show.render(variables, now)
    stdout.write(text + "\n")
    stdout.flush()
    ui.log(f"total {(time.perf_counter() - started) * 1000:.0f} ms")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# --watch                                                                     #
# --------------------------------------------------------------------------- #
def _raise_exit(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def _catch_termination() -> dict[int, Any]:
    """Make SIGTERM and SIGHUP raise ``SystemExit(128 + signal)``; return the old handlers.

    So that ``finally`` blocks run (log out of the Pi-hole, restore the
    terminal) when pihello is killed or its terminal is closed, as on Ctrl-C.
    POSIX and main thread only; ignored signals (``nohup``) and handlers set by
    an embedding application are left alone. Undo with :func:`_restore_handlers`.
    """
    import signal  # noqa: PLC0415
    import threading  # noqa: PLC0415

    previous: dict[int, Any] = {}
    if threading.current_thread() is not threading.main_thread():  # pragma: no cover
        return previous
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None and signal.getsignal(signum) is signal.SIG_DFL:
            previous[signum] = signal.signal(signum, _raise_exit)
    return previous


def _restore_handlers(previous: Mapping[int, Any]) -> None:
    import signal  # noqa: PLC0415

    for signum, handler in previous.items():
        signal.signal(signum, signal.SIG_DFL if handler is None else handler)


@contextmanager
def _exit_on_termination() -> Iterator[None]:
    """While active, SIGTERM / SIGHUP unwind like Ctrl-C (see :func:`_catch_termination`)."""
    previous = _catch_termination()
    try:
        yield
    finally:
        _restore_handlers(previous)


class Ticker:
    """Sleeps between ``--watch`` refreshes and wakes up early on resize.

    ``wait(timeout)`` returns ``True`` when the terminal was resized (SIGWINCH)
    and ``False`` when the timeout elapsed. While it exists, SIGTERM and SIGHUP
    raise :class:`SystemExit` (see :func:`_catch_termination`), so the watch
    loop restores the terminal and logs out like on Ctrl-C. Tests replace this
    class.
    """

    #: How often :meth:`wait` checks for a resize. The signal handler only sets
    #: a flag: taking a lock there (``threading.Event.set``) can deadlock when
    #: the signal interrupts code that holds the same lock.
    POLL_INTERVAL = 0.05

    def __init__(self) -> None:
        """Install the signal handlers (POSIX, main thread only)."""
        import signal  # noqa: PLC0415
        import threading  # noqa: PLC0415

        self._resized = False
        self._previous = _catch_termination()
        winch = getattr(signal, "SIGWINCH", None)
        if winch is not None and threading.current_thread() is threading.main_thread():
            self._previous[winch] = signal.signal(winch, self._on_resize)

    def _on_resize(self, _signum: int, _frame: object) -> None:
        self._resized = True

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; ``True`` if woken by a resize."""
        deadline = time.monotonic() + max(timeout, 0.0)
        while not self._resized:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(remaining, self.POLL_INTERVAL))
        self._resized = False
        return True

    def close(self) -> None:
        """Restore the previous signal handlers."""
        previous, self._previous = self._previous, {}
        _restore_handlers(previous)


_ALT_SCREEN_ON = "\x1b[?1049h\x1b[?25l"
_ALT_SCREEN_OFF = "\x1b[?25h\x1b[?1049l"


def _draw(stream: TextIO, text: str, *, tty: bool) -> None:
    if tty:
        body = "\x1b[K\n".join(text.split("\n"))
        stream.write(f"\x1b[H{body}\x1b[K\x1b[J")
    else:
        stream.write(text + "\n\n")
    stream.flush()


def _watch(show: _Show, interval: float) -> int:
    """Re-render every ``interval`` seconds until Ctrl-C, with one session."""
    from pihello import sources, terminal  # noqa: PLC0415

    stdout, ui = show.stdout, show.ui
    tty = _isatty(stdout)
    paths = show.template.paths()
    client: PiholeClient | None = None
    planned: tuple[Any, ...] = ()
    if not show.demo:
        planned = sources.plan(paths)
        client = make_client(show.settings, ui) if planned else None
    logged_in = False
    variables: dict[str, Any] | None = None
    now: float | None = None
    error: PiholeError | None = None
    updated = ""
    ticker = Ticker()
    color = _supports_color(stdout, mode=show.settings.color)

    def paint(text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if color else text

    def frame() -> str:
        columns, rows = terminal.terminal_size(stdout) if tty else (0, 0)
        status_lines = 1 if tty else 0
        body_height = show.settings.height or max(rows - status_lines - (1 if error else 0), 0)
        parts: list[str] = []
        if variables is not None:
            parts.append(show.render(variables, now, height=body_height if tty else None))
        if error is not None:
            message = ui.redact(f"{error}")
            parts.append(paint(f"⚠ {message} — retrying every {interval:g}s", "33"))
            if not tty:
                ui.warn(message)
        if tty:
            status = f"updated {updated} · every {interval:g}s · Ctrl-C to quit"
            parts.append(paint(status[: columns or None], "2"))
        return "\n".join(parts)

    if tty:
        stdout.write(_ALT_SCREEN_ON)
        stdout.flush()
    try:
        while True:
            try:
                if show.demo:
                    variables, now = show.demo_variables()
                elif client is None:
                    variables = sources.builtins(url=require_url(show.settings))
                else:
                    if not logged_in:
                        client.login()
                        logged_in = True
                    fetched, problems = sources.collect(client, paths)
                    fatal = _fatal_problem(len(planned), problems)
                    if fatal is not None:
                        raise fatal
                    show.handle_problems(problems)
                    variables = fetched
                error = None
            except TooManySessions as exc:
                error = exc
            except AuthenticationError:
                raise
            except PiholeError as exc:
                error = exc
                ui.log(f"watch: {exc}")
            updated = time.strftime("%H:%M:%S")
            _draw(stdout, frame(), tty=tty)
            deadline = time.monotonic() + interval
            while ticker.wait(deadline - time.monotonic()):
                _draw(stdout, frame(), tty=tty)
    except KeyboardInterrupt:
        pass
    finally:
        ticker.close()
        try:
            if tty:
                stdout.write(_ALT_SCREEN_OFF)
                stdout.flush()
        except (OSError, ValueError):
            _silence_stdout()  # the terminal is gone (SIGHUP, EIO): still log out below
        finally:
            if client is not None:
                client.logout()
    return EXIT_OK


# --------------------------------------------------------------------------- #
# pihello presets / themes                                                    #
# --------------------------------------------------------------------------- #
def _preview_variables(
    args: argparse.Namespace, settings: Settings, ui: UI, paths: frozenset[str] | None
) -> tuple[dict[str, Any], float | None]:
    if getattr(args, "live", False):
        variables, problems = collect_live(settings, ui, paths)
        report_problems(problems, ui)
        return variables, None
    from pihello import demo  # noqa: PLC0415

    return demo.variables(), demo.NOW


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def _columns(stream: TextIO, width: int | str | None, fallback: int = 80) -> int:
    if width not in (None, "auto"):
        return int(width) if width else 0
    from pihello import terminal  # noqa: PLC0415

    return terminal.terminal_size(stream)[0] or fallback


def _cmd_presets(args: argparse.Namespace, ui: UI) -> int:
    from dataclasses import replace  # noqa: PLC0415

    from pihello import presets  # noqa: PLC0415
    from pihello.template import Template  # noqa: PLC0415

    stdout = _stdout()
    chosen = (presets.get(args.name),) if args.name else presets.available()
    if args.source:
        if not args.name:
            raise ConfigError(
                "--source needs a preset NAME", hint="e.g. pihello presets default --source"
            )
        source = chosen[0].source if chosen[0].source.endswith("\n") else chosen[0].source + "\n"
        if args.output:
            path = _write_new_file(args.output, source)
            stdout.write(f"wrote {path}\n")
            return EXIT_OK
        if not _isatty(stdout):
            import contextlib  # noqa: PLC0415

            # `> file` should give a UTF-8 file like the one templates are read as,
            # not the locale's encoding (cp1252 on Windows can't hold ● or ·).
            with contextlib.suppress(AttributeError, OSError, ValueError):
                stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        stdout.write(source)
        return EXIT_OK
    if args.output:
        raise ConfigError(
            "--output only works with --source",
            hint="e.g. pihello presets default --source -o greeting.txt",
        )
    settings = resolve_settings(args, ui)
    if args.theme:
        settings = replace(settings, theme=args.theme)
    color = _supports_color(stdout, mode=settings.color)

    def paint(text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if color else text

    if not args.preview:
        width = max(len(p.name) for p in chosen)
        for preset in chosen:
            stdout.write(
                f"{paint(preset.name.ljust(width), '1')}  {preset.min_width:>3} cols  "
                f"{preset.description}\n"
            )
        return EXIT_OK

    templates = {p.name: Template.parse(p.source, name=f"preset {p.name!r}") for p in chosen}
    paths = frozenset().union(*(t.paths() for t in templates.values()))
    variables, now = _preview_variables(args, settings, ui, paths)
    columns = _columns(stdout, getattr(args, "width", None))
    from pihello import themes  # noqa: PLC0415

    theme = themes.resolve(settings.theme, settings.styles)
    for index, preset in enumerate(chosen):
        if index:
            stdout.write("\n")
        note = f"min. {preset.min_width} columns"
        if columns and preset.min_width > columns:
            note += f" — wider than your {columns}"
        stdout.write(f"{paint(preset.name, '1')}  {preset.description} {paint(f'({note})', '2')}\n")
        options = render_options(
            settings, stdout, now=now, width=max(columns - 2, 0) if columns else 0
        )
        try:
            text = templates[preset.name].render(variables, theme=theme, options=options)
        except PiHelloError as exc:
            text = paint(f"cannot render: {exc}", "31")
        stdout.write(_indent(text) + "\n")
    if len(chosen) > 1:  # the full gallery is long: say how to get less of it
        stdout.write(
            "\n"
            + paint(
                "Preview one with `pihello presets NAME`, list them with "
                "`pihello presets --no-preview`.",
                "2",
            )
            + "\n"
        )
    return EXIT_OK


def _swatches(names: Sequence[str]) -> str:
    return " ".join(f"[@{name}]{name}[]" for name in names)


def _cmd_themes(args: argparse.Namespace, ui: UI) -> int:
    from pihello import presets, themes  # noqa: PLC0415
    from pihello.template import Template  # noqa: PLC0415

    stdout = _stdout()
    settings = resolve_settings(args, ui)
    chosen = (themes.get(args.name),) if args.name else themes.available()
    preset_name = args.preset or (
        settings.template.value if settings.template.kind == "preset" else "default"
    )
    preset = presets.get(preset_name)
    template = Template.parse(preset.source, name=f"preset {preset.name!r}")
    swatch = Template.parse(_swatches(themes.STYLE_NAMES), name="swatches")
    variables: dict[str, Any] = {}
    now: float | None = None
    if args.preview:
        variables, now = _preview_variables(args, settings, ui, template.paths())
    columns = _columns(stdout, getattr(args, "width", None))
    color = _supports_color(stdout, mode=settings.color)
    bold = (lambda text: f"\x1b[1m{text}\x1b[0m") if color else (lambda text: text)
    for index, theme in enumerate(chosen):
        if index:
            stdout.write("\n")
        styles = themes.resolve(theme.name, settings.styles)
        options = render_options(
            settings, stdout, now=now, width=max(columns - 2, 0) if columns else 0
        )
        stdout.write(f"{bold(theme.name)}  {theme.description}\n")
        stdout.write(_indent(swatch.render({}, theme=styles, options=options)) + "\n")
        if args.preview:
            try:
                text = template.render(variables, theme=styles, options=options)
            except PiHelloError as exc:
                text = f"cannot render: {exc}"
            stdout.write("\n" + _indent(text) + "\n")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# pihello vars                                                                #
# --------------------------------------------------------------------------- #
_MISSING = object()


def lookup(tree: Any, path: str) -> Any:
    """Resolve a dotted ``path`` in ``tree`` (``N`` = first list item)."""
    current = tree
    for segment in path.split("."):
        key = "0" if segment == "N" else segment
        if isinstance(current, Mapping):
            if key not in current:
                return _MISSING
            current = current[key]
        elif isinstance(current, list) and key.isdigit():
            index = int(key)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def format_value(value: Any, limit: int = 40) -> str:
    """A compact, typed rendering of a variable value for ``pihello vars``."""
    import json  # noqa: PLC0415

    if value is _MISSING or value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        text = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, float):
        text = f"{value:.6g}"
    elif isinstance(value, list):
        text = f"[{len(value)} item{'s' if len(value) != 1 else ''}]"
    elif isinstance(value, Mapping):
        text = f"{{{len(value)} keys}}"
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _matches(path: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    import fnmatch  # noqa: PLC0415

    path, pattern = path.lower(), pattern.lower()
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(path, pattern)
    return pattern in path


def _generic(path: str) -> str:
    return ".".join("N" if part.isdigit() else part for part in path.split("."))


def _cmd_vars(args: argparse.Namespace, ui: UI) -> int:
    import json  # noqa: PLC0415

    from pihello import sources  # noqa: PLC0415

    stdout = _stdout()
    settings = resolve_settings(args, ui)
    if args.demo:
        from pihello import demo  # noqa: PLC0415

        variables = demo.variables()
    else:
        try:
            variables, problems = collect_live(settings, ui, None)
        except ConfigError as exc:
            if settings.url:
                raise
            raise ConfigError(
                str(exc), hint="use --demo to see demo values, or run `pihello init`"
            ) from None
        report_problems(problems, ui, always=True)

    catalog = {info.path: info for info in sources.catalog()}
    if args.json:
        if args.pattern:
            flat = sources.flatten(variables)
            data: Any = {k: v for k, v in flat.items() if _matches(k, args.pattern)}
        else:
            data = variables
        stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")
        return EXIT_OK

    rows: list[tuple[str, Any, str]] = []
    if args.all:
        for path, value in sources.flatten(variables).items():
            if _matches(path, args.pattern):
                info = catalog.get(path) or catalog.get(_generic(path))
                rows.append((path, value, info.description if info else ""))
    else:
        for path, info in catalog.items():
            if _matches(path, args.pattern):
                rows.append((path, lookup(variables, path), info.description))
    if not rows:
        raise ConfigError(
            f"no variables match {args.pattern!r}",
            hint="a substring ('blocked') or a glob ('queries.*'); "
            "`pihello vars --demo` lists them all",
        )
    tty = _isatty(stdout)
    color = _supports_color(stdout, mode=settings.color)
    width = _columns(stdout, None, fallback=0) if tty else 0
    path_w = min(max(len(r[0]) for r in rows), 48)
    values = [format_value(r[1]) for r in rows]
    value_w = max(len("VALUE"), *(len(v) for v in values))
    paint = (lambda t, c: f"\x1b[{c}m{t}\x1b[0m") if color else (lambda t, c: t)
    header = f"{'PATH'.ljust(path_w)}  {'VALUE'.ljust(value_w)}  DESCRIPTION"
    stdout.write(paint(header, "1") + "\n")
    for (path, value, full_description), shown in zip(rows, values, strict=True):
        description = full_description
        room = width - path_w - value_w - 4 if width else 0
        if width and len(description) > room:
            description = description[: max(room - 1, 0)] + "…" if room > 1 else ""
        missing = value is _MISSING or value is None
        cell = paint(shown.ljust(value_w), "2") if missing else shown.ljust(value_w)
        stdout.write(f"{paint(path.ljust(path_w), '36')}  {cell}  {description}".rstrip() + "\n")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# pihello colors                                                              #
# --------------------------------------------------------------------------- #
def _cmd_colors(args: argparse.Namespace, ui: UI) -> int:
    from pihello import terminal  # noqa: PLC0415
    from pihello.template import RenderOptions, Template  # noqa: PLC0415
    from pihello.template.color_tables import COLOR_NAMES, EIGHT_BIT_PALETTE  # noqa: PLC0415

    del ui
    stdout = _stdout()
    depth = terminal.detect_color_depth(args.color or "auto", stream=stdout)
    columns = _columns(stdout, args.width) or 80
    entries = sorted(COLOR_NAMES.items(), key=lambda item: (item[1], item[0]))
    name_w = max(len(name) for name, _ in entries)
    cell_w = 4 + name_w + 1 + 3 + 1 + 7
    per_row = max(columns // (cell_w + 2), 1)
    lines: list[str] = []
    sections = (("Basic colors (0-15)", 0, 16), ("Extended colors (16-255)", 16, 256))
    for title, low, high in sections:
        group = [(n, i) for n, i in entries if low <= i < high]
        lines.append(f"[bold]{title}[]")
        for start in range(0, len(group), per_row):
            cells = []
            for name, number in group[start : start + per_row]:
                red, green, blue = EIGHT_BIT_PALETTE[number]
                hexa = f"#{red:02x}{green:02x}{blue:02x}"
                cells.append(f"[on {name}]   [] {name.ljust(name_w)} {number:>3} [dim]{hexa}[]")
            lines.append("  ".join(cells))
        lines.append("")
    lines.append(
        "Also: #rrggbb, #rgb, rgb(r, g, b), color(N), default. Background: \\[on NAME\\] or "
        "\\[:NAME\\]; underline color: \\[_NAME\\]."
    )
    source = "\n".join(lines)
    text = Template.parse(source, name="colors").render(
        {}, options=RenderOptions(color=depth, width=0)
    )
    stdout.write(text + "\n")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Other subcommands (lazy)                                                    #
# --------------------------------------------------------------------------- #
def _cmd_init(args: argparse.Namespace, ui: UI) -> int:
    from pihello import wizard  # noqa: PLC0415

    return wizard.run(args, ui)


def _cmd_doctor(args: argparse.Namespace, ui: UI) -> int:
    from pihello import doctor  # noqa: PLC0415

    return doctor.run(args, ui, stdout=_stdout())


def _cmd_completion(args: argparse.Namespace, ui: UI) -> int:
    from pihello import completion  # noqa: PLC0415

    stdout = _stdout()
    if args.names:
        for name in completion.names(args.names, describe=args.describe):
            stdout.write(name + "\n")
        return EXIT_OK
    if not args.shell:
        raise ConfigError(
            "which shell? pihello completion {bash,zsh,fish}",
            hint="for example: pihello completion bash > "
            "~/.local/share/bash-completion/completions/pihello",
        )
    stdout.write(completion.script(args.shell))
    return EXIT_OK


_HANDLERS: dict[str, Callable[[argparse.Namespace, UI], int]] = {
    "show": _cmd_show,
    "init": _cmd_init,
    "presets": _cmd_presets,
    "themes": _cmd_themes,
    "vars": _cmd_vars,
    "doctor": _cmd_doctor,
    "colors": _cmd_colors,
    "completion": _cmd_completion,
}


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #
def _dispatch(argv: list[str], ui: UI) -> int:
    command = "show"
    explicit = False
    if argv and argv[0] in COMMANDS:
        command, argv, explicit = argv[0], argv[1:], True
    parser = build_parser(command)
    args = parser.parse_args(argv)
    if (
        command == "show"
        and not explicit
        and args.addr in COMMANDS
        and args.password is None
        and "--" not in argv  # `pihello -- doctor`: a host that is called "doctor"
    ):
        # `pihello --config x doctor`: options before the command name.
        rest = list(argv)
        rest.remove(args.addr)
        return _dispatch([args.addr, *rest], ui)
    ui.quiet = bool(getattr(args, "quiet", False))
    if getattr(args, "debug", False):
        ui.enable_debug()
        ui.log(f"pihello {__version__}, Python {sys.version.split()[0]}, command {command!r}")
    try:
        if command == "show":
            _check_lone_addr(args, argv)
        return _HANDLERS[command](args, ui)
    except ConnectionFailed as exc:
        typo = None if explicit else _command_typo(getattr(args, "addr", None))
        if typo:
            exc.hint = f"did you mean `pihello {typo}`?"
        raise


def _check_lone_addr(args: argparse.Namespace, argv: Sequence[str]) -> None:
    """Reject ``pihello shwo``: a lone ADDR that doesn't look like a host.

    pihello 1.x always took ADDR *and* PASSWORD, so a single word is far more
    likely a mistyped command than a host -- and it must not be looked up
    (search domains, wildcard DNS) and sent the configured password. Host
    names with a dot or a port, IP addresses, ``localhost``, two positionals
    and anything after ``--`` are still taken as the Pi-hole address.
    """
    addr = getattr(args, "addr", None)
    if addr is None or getattr(args, "password", None) is not None or "--" in argv:
        return
    if "." in addr or ":" in addr or addr.lower() == "localhost":
        return
    typo = _command_typo(addr)
    hint = (
        f"did you mean `pihello {typo}`?"
        if typo
        else f"commands: {', '.join(COMMANDS)}; for a Pi-hole called {addr!r} "
        f"use `pihello -u {addr}`"
    )
    raise ConfigError(f"unknown command {addr!r}", hint=hint)


def _command_typo(addr: str | None) -> str | None:
    """The command a 1.x-style ADDR like ``vras`` was probably meant to be."""
    if not addr or "." in addr or ":" in addr:
        return None
    import difflib  # noqa: PLC0415 - error path only

    matches = difflib.get_close_matches(addr, list(COMMANDS), n=1, cutoff=0.75)
    return matches[0] if matches else None


def _silence_stdout() -> None:
    """After EPIPE, point stdout at /dev/null so the interpreter exits quietly."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        pass


def _tolerate_unencodable(stream: Any) -> None:
    """Print ``?`` for characters the output encoding lacks instead of crashing.

    E.g. on Windows with the output redirected to a file or pipe (cp1252), or
    with ``PYTHONIOENCODING=ascii``. Python's default error handler is
    ``strict``, but ``surrogateescape`` on Windows and in the C locale, which
    doesn't help with ``↑`` in cp1252 either; UTF-8 can encode everything, so
    ``surrogateescape`` is kept there, and so is any other error handler.
    """
    import codecs  # noqa: PLC0415 - already loaded at startup
    import contextlib  # noqa: PLC0415

    errors = getattr(stream, "errors", None)
    if errors == "surrogateescape":
        try:
            if codecs.lookup(stream.encoding).name == "utf-8":
                return
        except (AttributeError, LookupError, TypeError):
            return
    elif errors != "strict":
        return
    with contextlib.suppress(AttributeError, OSError, ValueError):  # exotic streams
        stream.reconfigure(errors="replace")


def _stdout_is_pipe() -> bool:
    import stat  # noqa: PLC0415 - already loaded at startup

    try:
        return stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode)
    except (AttributeError, OSError, ValueError):
        return False


def _is_windows_closed_pipe(exc: BaseException) -> bool:
    """Whether ``exc`` is Windows' version of :class:`BrokenPipeError`.

    Writing to a pipe whose reader went away (``pihello presets | more`` after
    quitting ``more``) fails with ``OSError(EINVAL)`` on Windows, not ``EPIPE``.
    """
    import errno  # noqa: PLC0415 - already loaded at startup

    return (
        sys.platform == "win32"
        and isinstance(exc, OSError)
        and exc.errno == errno.EINVAL
        and exc.filename is None  # a write, not e.g. opening a file Windows can't name
        and _stdout_is_pipe()
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run pihello with ``argv`` (default ``sys.argv[1:]``) and return the exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    ui = UI()
    _tolerate_unencodable(sys.stdout)
    try:
        try:
            status = _dispatch(arguments, ui)
            if sys.stdout is not None:
                # Flush here rather than at interpreter exit, so that a reader
                # that went away (`pihello presets | true`) is handled below.
                sys.stdout.flush()
            return status
        finally:
            ui.close()
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_OK
    except SystemExit as exc:  # argparse: --help, --version, usage errors
        code = exc.code
        if code is None:
            return EXIT_OK
        return code if isinstance(code, int) else EXIT_USAGE
    except PiHelloError as exc:
        ui.error(str(exc), exc.hint, excerpt=getattr(exc, "excerpt", None))
        return exc.exit_code
    except Exception as exc:
        if _is_windows_closed_pipe(exc):
            _silence_stdout()
            return EXIT_OK
        if ui.debug:
            import traceback  # noqa: PLC0415

            ui._write("".join(traceback.format_exception(exc)).rstrip())
        ui.quiet = False if ui.debug else ui.quiet
        ui.error(
            f"internal error: {type(exc).__name__}: {exc}",
            f"this is a bug; please report it at {ISSUES_URL}"
            + ("" if ui.debug else " (add --debug for the full traceback)"),
        )
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
