"""``pihello init``: the interactive setup wizard.

The wizard walks through five steps — Pi-hole address, password, preset, theme,
save — testing each answer against the real Pi-hole, and finally prints the
exact snippet for the user's shell startup file (offering to append it).
Nothing is written before the last step.

Every prompt goes through :class:`Prompter`, whose input/output streams and
``getpass`` function are injectable, so the whole wizard is testable with
``io.StringIO``. With ``--yes`` nothing is asked: answers come from the options
(``--url``, ``--password-file`` …) and defaults.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from pihello.errors import (
    EXIT_OK,
    AuthenticationError,
    ConfigError,
    PiholeError,
    TLSError,
    TooManySessions,
    TwoFactorRequired,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Mapping, Sequence

    from pihello.cli import UI
    from pihello.client import PiholeClient

__all__ = ["Prompter", "SetupCancelled", "ShellSnippet", "detect_shell", "run", "shell_snippet"]

DEFAULT_URL = "http://pi.hole"
_WINDOWS = os.name == "nt"
APP_LOGIN_HELP = (
    "pihello logs in with an app password. Create one in the Pi-hole web interface:\n"
    "  Settings → Web interface / API → Configure app password\n"
    "(App passwords also work when two-factor authentication is enabled; your normal\n"
    "web interface password works too, unless 2FA is on.)"
)
MAX_PASSWORD_ATTEMPTS = 3


class SetupCancelled(ConfigError):
    """The user ended the wizard (end of input) before it finished."""


# --------------------------------------------------------------------------- #
# Prompts                                                                     #
# --------------------------------------------------------------------------- #
class Prompter:
    """Asks questions on a pair of text streams.

    When both streams are the real ``sys.stdin``/``sys.stdout``, the builtin
    :func:`input` is used (line editing via readline); otherwise lines are read
    from ``stdin`` directly. ``getpass`` reads secrets without echo.
    """

    def __init__(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        getpass: Callable[[str], str] | None = None,
        *,
        color: bool | None = None,
    ) -> None:
        """Create a prompter on the given streams (default: the terminal)."""
        self.stdin = sys.stdin if stdin is None else stdin
        self.stdout = sys.stdout if stdout is None else stdout
        if getpass is None:
            import getpass as getpass_module  # noqa: PLC0415

            getpass = getpass_module.getpass
        self._getpass = getpass
        if color is None:
            from pihello.cli import _supports_color  # noqa: PLC0415

            color = _supports_color(self.stdout)
        self.color = color

    # -- output ------------------------------------------------------------
    def paint(self, text: str, code: str) -> str:
        """Wrap ``text`` in SGR ``code`` when colors are enabled."""
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def say(self, text: str = "") -> None:
        """Print a line."""
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def ok(self, text: str) -> None:
        """Print a success line (``✓``)."""
        self.say(f"  {self.paint('✓', '32')} {text}")

    def fail(self, text: str, hint: str | None = None) -> None:
        """Print a failure line (``✗``) with an optional hint."""
        self.say(f"  {self.paint('✗', '31')} {text}")
        if hint:
            self.say(f"    {self.paint('hint:', '36')} {hint}")

    def note(self, text: str) -> None:
        """Print an attention line (``!``)."""
        self.say(f"  {self.paint('!', '33')} {text}")

    def heading(self, text: str) -> None:
        """Print a step heading."""
        self.say()
        self.say(self.paint(text, "1"))

    # -- input -------------------------------------------------------------
    def _readline(self, prompt: str) -> str:
        if self.stdin is sys.stdin and self.stdout is sys.stdout:
            try:
                return input(prompt)
            except EOFError:
                self.say()
                raise SetupCancelled(
                    "setup cancelled (end of input)",
                    hint="for unattended setup use: pihello init --yes --url URL ...",
                ) from None
        self.stdout.write(prompt)
        self.stdout.flush()
        line = self.stdin.readline()
        if not line:
            self.say()
            raise SetupCancelled(
                "setup cancelled (end of input)",
                hint="for unattended setup use: pihello init --yes --url URL ...",
            )
        return line.rstrip("\r\n")

    def ask(self, question: str, default: str | None = None) -> str:
        """Ask for free text; an empty answer returns ``default`` (asks again if none)."""
        suffix = f" [{default}]" if default else ""
        while True:
            answer = self._readline(f"{question}{suffix}: ").strip()
            if answer:
                return answer
            if default is not None:
                return default

    def confirm(self, question: str, default: bool = False) -> bool:
        """Ask a yes/no question."""
        choices = "Y/n" if default else "y/N"
        while True:
            answer = self._readline(f"{question} [{choices}] ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes", "j", "ja", "o", "oui", "s", "si", "sí"):
                return True
            if answer in ("n", "no", "nein", "non"):
                return False
            self.say("  please answer y or n")

    def choose(self, question: str, options: Sequence[tuple[str, str]], default: str) -> str:
        """Pick one of ``options`` (``(key, description)``) by number or key."""
        from pihello import terminal  # noqa: PLC0415
        from pihello.template.layout import truncate  # noqa: PLC0415

        width = max(len(key) for key, _ in options)
        columns = terminal.terminal_size(self.stdout)[0]  # 0: not a terminal, no limit
        for index, (key, description) in enumerate(options, start=1):
            marker = self.paint("*", "32") if key == default else " "
            shown = description
            if columns:  # cut instead of wrapping mid-word (and never at the last column)
                shown = truncate(description, max(columns - 1 - (width + 9), 10))
            self.say(f"  {index:>2}) {marker}{key.ljust(width)}  {self.paint(shown, '2')}")
        keys = [key for key, _ in options]
        while True:
            answer = self.ask(question, default).strip()
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return keys[int(answer) - 1]
            lowered = answer.lower()
            if lowered in keys:
                return lowered
            matches = [key for key in keys if key.startswith(lowered)]
            if len(matches) == 1:
                return matches[0]
            self.say(f"  please enter a number from 1 to {len(options)} or a name")

    def secret(self, prompt: str) -> str:
        """Read a secret without echo."""
        try:
            return self._getpass(prompt)
        except EOFError:
            self.say()
            raise SetupCancelled("setup cancelled (end of input)") from None


# --------------------------------------------------------------------------- #
# Shell snippets                                                              #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ShellSnippet:
    """What to add to which startup file for one shell."""

    shell: str
    path: Path
    text: str
    display_path: str
    append_ok: bool = True
    note: str = ""


def detect_shell(env: Mapping[str, str] | None = None) -> str | None:
    """Guess the user's shell: ``bash``, ``zsh``, ``fish``, ``powershell`` or ``None``."""
    env = os.environ if env is None else env
    name = Path(env.get("SHELL", "")).name.lower()
    if name in ("bash", "zsh", "fish"):
        return name
    if name in ("pwsh", "powershell", "pwsh.exe", "powershell.exe"):
        return "powershell"
    if (_WINDOWS and not name) or (env.get("PSModulePath") and not name):
        return "powershell"
    return None


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME") or env.get("USERPROFILE")
    return Path(home) if home else Path.home()


def _display(path: Path, env: Mapping[str, str]) -> str:
    home = _home(env)
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _absolute(value: str, env: Mapping[str, str]) -> str:
    """Anchor a user-entered path to the current directory, for display in the config.

    Relative paths in the config file are resolved against the config directory,
    so a path typed relative to the current directory must be made absolute first.
    """
    return _display(Path(value).expanduser().absolute(), env)


def shell_snippet(
    shell: str, *, command: str = "pihello -q", env: Mapping[str, str] | None = None
) -> ShellSnippet:
    """Return the startup-file snippet that runs ``command`` in new interactive shells."""
    env = os.environ if env is None else env
    home = _home(env)
    if shell == "bash":
        path = home / ".bashrc"
        text = (
            "# Pi-hole greeting (pihello)\n"
            "if [[ $- == *i* ]] && command -v pihello >/dev/null 2>&1; then\n"
            f"    {command}\n"
            "fi\n"
        )
        return ShellSnippet("bash", path, text, _display(path, env))
    if shell == "zsh":
        zdotdir = env.get("ZDOTDIR")
        path = (Path(zdotdir) if zdotdir else home) / ".zshrc"
        text = (
            "# Pi-hole greeting (pihello)\n"
            "if [[ -o interactive ]] && (( $+commands[pihello] )); then\n"
            f"    {command}\n"
            "fi\n"
        )
        return ShellSnippet("zsh", path, text, _display(path, env))
    if shell == "fish":
        xdg = env.get("XDG_CONFIG_HOME")
        fish_dir = (Path(xdg) if xdg else home / ".config") / "fish"
        own = fish_dir / "functions" / "fish_greeting.fish"
        if own.exists():
            text = f"    type -q pihello; and {command}\n"
            return ShellSnippet(
                "fish",
                own,
                text,
                _display(own, env),
                append_ok=False,
                note="you already have a fish_greeting function; add this line inside it",
            )
        path = fish_dir / "conf.d" / "pihello.fish"
        text = (
            "# Pi-hole greeting (pihello): replaces fish's default greeting\n"
            "function fish_greeting\n"
            f"    type -q pihello; and {command}\n"
            "end\n"
        )
        return ShellSnippet("fish", path, text, _display(path, env))
    if shell == "powershell":
        if _WINDOWS:
            path = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        else:
            xdg = env.get("XDG_CONFIG_HOME")
            base = Path(xdg) if xdg else home / ".config"
            path = base / "powershell" / "Microsoft.PowerShell_profile.ps1"
        text = (
            "# Pi-hole greeting (pihello)\n"
            f"if (Get-Command pihello -ErrorAction SilentlyContinue) {{ {command} }}\n"
        )
        # $PROFILE differs between PowerShell 5.1 and 7 and may live in OneDrive,
        # so never guess the file to append to; let PowerShell open its own.
        return ShellSnippet(
            "powershell",
            path,
            text,
            "$PROFILE",
            append_ok=False,
            note="open it with `notepad $PROFILE`; create it first with "
            "`New-Item -Force $PROFILE` if it doesn't exist",
        )
    raise ValueError(f"unknown shell {shell!r}")


def append_snippet(snippet: ShellSnippet) -> bool:
    """Append ``snippet`` to its file unless pihello is already mentioned there.

    Returns ``True`` when the file was changed.
    """
    try:
        current = snippet.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    except UnicodeDecodeError:  # e.g. a UTF-16 PowerShell profile: appending would corrupt it
        raise ConfigError(
            f"{snippet.path} is not UTF-8 text, so pihello leaves it alone",
            hint="add the lines above by hand",
        ) from None
    if "pihello" in current:
        return False
    snippet.path.parent.mkdir(parents=True, exist_ok=True)
    separator = (
        ""
        if not current or current.endswith("\n\n")
        else ("\n" if current.endswith("\n") else "\n\n")
    )
    with snippet.path.open("a", encoding="utf-8") as handle:
        handle.write(separator + snippet.text)
    return True


# --------------------------------------------------------------------------- #
# The wizard                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class _Answers:
    url: str = DEFAULT_URL
    verify: bool | str = True
    timeout: float = 3.0
    password: str | None = None
    store: str = "none"  # config | file | command | env | none
    password_file: str | None = None
    password_command: str | None = None
    preset: str = "default"
    theme: str = "default"
    variables: dict[str, Any] | None = field(default=None, repr=False)
    now: float | None = None

    def __repr__(self) -> str:  # never show the password
        return f"_Answers(url={self.url!r}, store={self.store!r}, preset={self.preset!r})"


class Wizard:
    """One run of ``pihello init``."""

    def __init__(
        self,
        args: argparse.Namespace,
        ui: UI,
        prompter: Prompter,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Prepare a run for ``args``; nothing happens until :meth:`run`."""
        self.args = args
        self.ui = ui
        self.io = prompter
        self.env = os.environ if env is None else env
        self.interactive = not getattr(args, "yes", False)
        self.answers = _Answers()
        timeout = getattr(args, "timeout", None)
        if timeout:
            self.answers.timeout = float(timeout)
        if getattr(args, "insecure", False) and getattr(args, "ca_cert", None):
            raise ConfigError("--insecure and --ca-cert cannot be used together")
        given = [
            name
            for name in ("password_file", "password_command", "password_stdin")
            if getattr(args, name, None)
        ]
        if len(given) > 1:
            flags = " and ".join("--" + name.replace("_", "-") for name in given)
            raise ConfigError(f"{flags} cannot be used together")
        if self.interactive and getattr(args, "password_stdin", False):
            raise ConfigError(
                "--password-stdin needs --yes: without it, the questions are read from "
                "standard input",
                hint="e.g. pass show pihole | pihello init --url pi.hole --password-stdin --yes",
            )
        if getattr(args, "insecure", False):
            self.answers.verify = False
        elif getattr(args, "ca_cert", None):
            # absolute: relative paths in the config are relative to its directory
            self.answers.verify = str(Path(args.ca_cert).expanduser().absolute())
        self.check = not getattr(args, "no_check", False)
        self.saved = False  # whether the config file was written
        from pihello import config  # noqa: PLC0415

        self.config_path, _ = config.find_config_path(getattr(args, "config", None), self.env)

    # -- helpers -----------------------------------------------------------
    def _client(self, password: str | None = None) -> PiholeClient:
        from pihello import __version__  # noqa: PLC0415
        from pihello.client import PiholeClient  # noqa: PLC0415

        return PiholeClient(
            self.answers.url,
            password,
            timeout=self.answers.timeout,
            verify=self.answers.verify,
            user_agent=f"pihello/{__version__} (init)",
        )

    # -- steps -------------------------------------------------------------
    def run(self) -> int:
        """Run every step; returns the exit code."""
        io = self.io
        if self.interactive:
            io.say(io.paint("Welcome to pihello!", "1") + " Let's set up your Pi-hole greeting.")
            io.say("Press Ctrl-C at any time to cancel; nothing is written until the end.")
        self._check_existing()
        self._step_url()
        client = self._step_password()
        try:
            self._step_look(client)
        finally:
            if client is not None:
                client.logout()
        if self._step_save():  # no snippet for a greeting without a config
            self._step_shell(written=True)
        return EXIT_OK

    def _check_existing(self) -> None:
        if not self.config_path.exists() or getattr(self.args, "force", False):
            return
        if not self.interactive:
            raise ConfigError(
                f"{self.config_path} already exists",
                hint="add --force to overwrite it (a backup is kept as config.toml.bak)",
            )
        self.io.say()
        self.io.note(f"{self.config_path} already exists.")
        if not self.io.confirm(
            "Replace it at the end (the old file is kept as a .bak backup)?", default=False
        ):
            raise SetupCancelled(
                "setup cancelled; the existing config was not changed",
                hint="edit it by hand, or run `pihello doctor` to check it",
            )

    def _step_url(self) -> None:
        io, answers = self.io, self.answers
        from pihello.client import normalize_url  # noqa: PLC0415

        given = getattr(self.args, "url", None) or self.env.get("PIHELLO_URL") or None
        default = self._existing_url() or DEFAULT_URL  # running init again: keep the address
        if self.interactive:
            io.heading("[1/5] Pi-hole address")
            io.say("The address you open the Pi-hole web interface with, without /admin.")
        while True:
            raw = (
                given
                if given
                else (io.ask("Pi-hole address", default) if self.interactive else default)
            )
            given = None
            try:
                answers.url = normalize_url(raw)
            except ConfigError as exc:
                if not self.interactive:
                    raise
                io.fail(str(exc), exc.hint)
                continue
            if not self.check:
                return
            try:
                info = self._probe()
            except PiholeError as exc:
                if not self.interactive:
                    raise
                if isinstance(exc, TLSError) and self._handle_tls(exc):
                    return
                io.fail(str(exc), exc.hint)
                if io.confirm("Use this address anyway?", default=False):
                    self.check = False
                    return
                continue
            self._offer_https(info)
            return

    def _existing_url(self) -> str | None:
        from pihello import config  # noqa: PLC0415

        try:
            url = config.load_file(self.config_path).settings.get("url")
        except ConfigError:
            return None
        return url if isinstance(url, str) else None

    def _config_option(self, shell: str | None = None) -> str:
        """`` --config PATH`` when new shells wouldn't find the written file by themselves."""
        from pihello import config  # noqa: PLC0415

        written = self.config_path.absolute()  # new shells start in another directory
        if written == config.find_config_path(None, self.env)[0].absolute():
            return ""
        if shell == "powershell":  # single quotes, embedded ' doubled
            return " --config '" + str(written).replace("'", "''") + "'"
        import shlex  # noqa: PLC0415

        return " --config " + shlex.quote(str(written))

    def _probe(self) -> dict[str, Any]:
        started = time.perf_counter()
        info = self._client().probe()
        elapsed = (time.perf_counter() - started) * 1000
        self.io.ok(f"found a Pi-hole v6 at {self.answers.url} ({elapsed:.0f} ms)")
        return info

    def _offer_https(self, info: Mapping[str, Any]) -> None:
        answers, io = self.answers, self.io
        port = info.get("https_port")
        if not answers.url.startswith("http://") or not isinstance(port, int) or port <= 0:
            return
        from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415

        from pihello.doctor import tcp_reachable  # noqa: PLC0415

        parts = urlsplit(answers.url)
        hostname = parts.hostname or ""
        host = f"[{hostname}]" if ":" in hostname else hostname
        if not tcp_reachable(hostname, port, min(answers.timeout, 2.0)):
            # e.g. Docker: info/login reports the container's port, not the published one
            io.note(
                f"Pi-hole reports HTTPS on port {port}, but {host}:{port} doesn't answer "
                "(a Docker port mapping?). If HTTPS is published on another port, use "
                f"https://{host}:PORT as the address."
            )
            return
        netloc = host if port == 443 else f"{host}:{port}"
        https_url = urlunsplit(("https", netloc, parts.path, "", ""))
        io.note(f"this Pi-hole also serves HTTPS ({https_url}).")
        if not self.interactive:
            return
        if not io.confirm("Use HTTPS? It keeps the password private on your network.", True):
            return
        previous = answers.url
        answers.url = https_url
        try:
            self._probe()
        except TLSError as exc:
            if not self._handle_tls(exc):
                answers.url = previous
                io.say(f"  staying with {previous}")
        except PiholeError as exc:
            io.fail(str(exc), exc.hint)
            answers.url = previous
            io.say(f"  staying with {previous}")

    def _handle_tls(self, exc: TLSError) -> bool:
        """Offer a CA path or --insecure for a certificate problem. ``True`` = solved."""
        io, answers = self.io, self.answers
        io.fail(str(exc))
        io.say("  Pi-hole uses a self-signed certificate unless you installed your own. Its CA is")
        io.say("  /etc/pihole/tls_ca.crt on the Pi-hole; it is issued for the name in")
        io.say("  webserver.domain (pi.hole by default), so the address must use that name.")
        while True:
            choice = io.choose(
                "How should pihello handle the certificate?",
                [
                    ("ca", "give a copy of /etc/pihole/tls_ca.crt or your own CA (most secure)"),
                    ("insecure", "do not verify the certificate (like --insecure)"),
                    ("back", "go back and use another address"),
                ],
                default="ca",
            )
            if choice == "back":
                return False
            if choice == "insecure":
                answers.verify = False
            else:
                path = Path(io.ask("Certificate / CA file (PEM)")).expanduser()
                if not path.is_file():
                    io.fail(f"{path} is not a file")
                    continue
                answers.verify = str(path.resolve())
            try:
                self._probe()
            except TLSError as again:
                io.fail(str(again), again.hint)
                answers.verify = True
                continue
            except PiholeError as again:
                io.fail(str(again), again.hint)
                return False
            return True

    # -- password ----------------------------------------------------------
    def _needs_password(self) -> bool:
        """Ask the Pi-hole (without credentials) whether a password is set."""
        try:
            self._client().get("info/version")
        except AuthenticationError:
            return True
        return False

    def _step_password(self) -> PiholeClient | None:
        io, answers, args = self.io, self.answers, self.args
        if self.interactive:
            io.heading("[2/5] Password")
        if self.check and not self._needs_password():
            io.ok("this Pi-hole has no password, so pihello needs none")
            answers.store = "none"
            client = self._client()
            client.login()
            return client
        if not self.interactive:
            self._noninteractive_password()
            return self._verify(answers.password) if self.check else None
        io.say(APP_LOGIN_HELP)
        io.say()
        places = [
            ("config", "in the config file, readable only by you"),
            ("file", "in a separate password file, readable only by you"),
            ("command", "ask a password manager command, e.g. pass show pihole"),
            ("env", "the PIHELLO_PASSWORD environment variable (you set it up)"),
        ]
        if not self.check:
            places.append(("none", "no password: this Pi-hole has none"))
        # --password-file / --password-command answer "where" and pre-fill the path / command
        implied = None
        if getattr(args, "password_file", None):
            answers.password_file = _absolute(args.password_file, self.env)
            implied = "file"
        elif getattr(args, "password_command", None):
            answers.password_command = args.password_command
            implied = "command"
        store = (
            getattr(args, "store", None)
            or implied
            or io.choose("Where should pihello keep the password?", places, default="config")
        )
        answers.store = store
        if store == "none":
            return self._verify(None) if self.check else None
        for attempt in range(1, MAX_PASSWORD_ATTEMPTS + 1):
            password = self._obtain_interactive(store, retry=attempt > 1)
            if not self.check:
                answers.password = password
                return None
            try:
                client = self._verify(password)
            except TooManySessions:
                raise
            except AuthenticationError as exc:
                io.fail(str(exc), exc.hint)
                if isinstance(exc, TwoFactorRequired):
                    io.say("  Two-factor authentication is on: please use an app password.")
                if attempt == MAX_PASSWORD_ATTEMPTS:
                    raise
                continue
            answers.password = password
            return client
        raise AssertionError("unreachable")  # pragma: no cover

    def _obtain_interactive(self, store: str, *, retry: bool) -> str:
        io, answers = self.io, self.answers
        from pihello import config  # noqa: PLC0415

        if store == "command":
            while True:
                command = io.ask(
                    "Command that prints the password",
                    None if retry else (answers.password_command or "pass show pihole"),
                )
                try:
                    password = config.run_password_command(command)
                except ConfigError as exc:
                    io.fail(str(exc), exc.hint)
                    continue
                answers.password_command = command
                return password
        if store == "file" and not retry:
            default = answers.password_file or _display(
                self.config_path.parent / "password", self.env
            )
            answers.password_file = _absolute(io.ask("Password file", default), self.env)
            path = Path(answers.password_file).expanduser()
            if path.is_file():
                try:
                    existing = config.read_password_file(path)
                except ConfigError:
                    existing = ""
                if existing and io.confirm(f"Use the password already in {path}?", True):
                    self.ui.add_secret(existing)
                    return existing
        if store == "env" and self.env.get("PIHELLO_PASSWORD") and not retry:
            io.ok("using the password from PIHELLO_PASSWORD")
            return self.env["PIHELLO_PASSWORD"]
        while True:
            password = io.secret("App password: ")
            if password.strip():
                self.ui.add_secret(password)
                return password
            io.say("  the password is empty; press Ctrl-C to cancel")

    def _noninteractive_password(self) -> None:
        args, answers = self.args, self.answers
        from pihello import config  # noqa: PLC0415

        store = getattr(args, "store", None)  # an explicit --store beats the implied one
        if getattr(args, "password_file", None):
            answers.store = store or "file"
            if answers.store == "file":
                answers.password_file = _absolute(args.password_file, self.env)
            if self.check or answers.store == "config":
                path = Path(args.password_file).expanduser()
                answers.password = config.read_password_file(path)
        elif getattr(args, "password_command", None):
            answers.store = store or "command"
            if answers.store == "command":
                answers.password_command = args.password_command
            if self.check or answers.store in ("config", "file"):
                answers.password = config.run_password_command(args.password_command)
        elif getattr(args, "password_stdin", False):
            line = self.io.stdin.readline().rstrip("\r\n")
            if not line:
                raise ConfigError("--password-stdin: no password on standard input")
            answers.password = line
            answers.store = store or "config"
        elif self.env.get("PIHELLO_PASSWORD"):
            answers.password = self.env["PIHELLO_PASSWORD"]
            answers.store = store or "env"
        elif store in ("config", "file"):
            raise ConfigError(
                f"--store {store} needs a password",
                hint="pipe it in with --password-stdin, or set PIHELLO_PASSWORD",
            )
        else:
            answers.store = store or "none"
        if answers.store == "file" and answers.password_file is None:
            answers.password_file = _display(self.config_path.parent / "password", self.env)
        if answers.store == "command" and answers.password_command is None:
            raise ConfigError("--store command needs --password-command CMD")
        self.ui.add_secret(answers.password)

    def _verify(self, password: str | None) -> PiholeClient:
        started = time.perf_counter()
        client = self._client(password)
        client.login()
        try:
            data = client.get("info/version")
        except BaseException:
            client.logout()
            raise
        elapsed = (time.perf_counter() - started) * 1000
        version = _core_version(data)
        what = "logged in" if client.sid else "connected"
        self.io.ok(f"{what} — Pi-hole {version} ({elapsed:.0f} ms)")
        return client

    # -- look --------------------------------------------------------------
    def _step_look(self, client: PiholeClient | None) -> None:
        io, answers, args = self.io, self.answers, self.args
        from pihello import presets, themes  # noqa: PLC0415

        answers.preset = presets.get(getattr(args, "preset", None) or "default").name
        answers.theme = themes.get(getattr(args, "theme", None) or "default").name
        if not self.interactive:
            return
        if client is not None:
            from pihello import sources  # noqa: PLC0415

            try:
                answers.variables, _problems = sources.collect(client, None)
            except PiholeError as exc:
                io.note(f"cannot fetch data for the preview: {exc}")
        if answers.variables is None:
            from pihello import demo  # noqa: PLC0415

            answers.variables = demo.variables()
            answers.now = demo.NOW
            io.note("the previews use demo data")

        io.heading("[3/5] Preset — what the greeting shows")
        options = [(p.name, f"{p.description} (≥{p.min_width} cols)") for p in presets.available()]
        while True:
            answers.preset = io.choose("Preset", options, answers.preset)
            self._preview()
            if io.confirm("Use this preset?", default=True):
                break

        io.heading("[4/5] Theme — the colors")
        theme_options = [(t.name, t.description) for t in themes.available()]
        while True:
            answers.theme = io.choose("Theme", theme_options, answers.theme)
            self._preview()
            if io.confirm("Use this theme?", default=True):
                break

    def _preview(self) -> None:
        from pihello import presets, terminal, themes  # noqa: PLC0415
        from pihello.errors import PiHelloError  # noqa: PLC0415
        from pihello.template import RenderOptions, Template  # noqa: PLC0415

        io, answers = self.io, self.answers
        preset = presets.get(answers.preset)
        columns = terminal.terminal_size(io.stdout)[0] or 80
        color_mode = getattr(self.args, "color", None) or "auto"
        options = RenderOptions(
            color=terminal.detect_color_depth(color_mode, stream=io.stdout),
            width=max(columns - 4, 20),
            now=answers.now,
        )
        try:
            text = Template.parse(preset.source, name=preset.name).render(
                answers.variables or {}, theme=themes.resolve(answers.theme), options=options
            )
        except PiHelloError as exc:
            text = f"(cannot render this preset: {exc})"
        io.say()
        for line in text.split("\n"):
            io.say("    " + line)
        io.say()

    # -- save --------------------------------------------------------------
    def config_data(self) -> dict[str, Any]:
        """The settings to write, in file order."""
        answers = self.answers
        data: dict[str, Any] = {"url": answers.url}
        if answers.store == "config":
            data["password"] = answers.password
        elif answers.store == "file":
            data["password_file"] = answers.password_file
        elif answers.store == "command":
            data["password_command"] = answers.password_command
        if answers.verify is not True:
            data["verify_tls"] = answers.verify
        if answers.timeout != 3.0:
            data["timeout"] = answers.timeout
        data["preset"] = answers.preset
        data["theme"] = answers.theme
        return data

    def _step_save(self) -> bool:
        io, answers = self.io, self.answers
        from pihello import __version__, config  # noqa: PLC0415

        if self.interactive:
            io.heading("[5/5] Save")
        header = (
            f"pihello configuration, written by `pihello init` (pihello {__version__}) on "
            f"{time.strftime('%Y-%m-%d')}.\n"
            "Every setting: https://github.com/pavelgar/pihello/blob/master/docs/configuration.md\n"
            "Check it with: pihello doctor"
        )
        text = config.dumps(self.config_data(), header=header)
        if self.interactive and not io.confirm(f"Write {self.config_path}?", default=True):
            io.say("Nothing was written. Here is the configuration if you want it:")
            io.say()
            io.say(self.ui.redact(text) if answers.store == "config" else text)
            return False
        if answers.store == "file":  # only now: "Nothing was written" above must be true
            assert answers.password_file is not None  # noqa: S101 - set together with store
            path = Path(answers.password_file).expanduser()
            current = None
            if path.is_file():
                try:
                    current = config.read_password_file(path)
                except ConfigError:
                    current = None
            if answers.password is not None and current != answers.password:
                config.write_private(path, answers.password + "\n")
                io.ok(f"wrote the password to {path} (mode 600)")
        if self.config_path.exists():
            backup = self.config_path.with_name(self.config_path.name + ".bak")
            config.write_private(backup, self.config_path.read_bytes())  # exact bytes
            io.note(f"kept the previous config as {backup}")
        config.write_private(self.config_path, text)
        self.saved = True
        io.ok(f"wrote {self.config_path} (mode 600)")
        if answers.store == "env":
            io.note(
                "remember to set PIHELLO_PASSWORD in your environment "
                "(e.g. from your password manager); it is not stored anywhere"
            )
        return True

    def _step_shell(self, written: bool) -> None:
        io, args = self.io, self.args
        shell = getattr(args, "shell", None) or detect_shell(self.env)
        command = "pihello -q" + self._config_option(shell)
        io.heading("Greeting in every new terminal")
        if shell is None:
            io.say(f"Add `{command}` to your shell's startup file, for example:")
            for name in ("bash", "zsh", "fish", "powershell"):
                snippet = shell_snippet(name, command=command, env=self.env)
                io.say(f"  {name:<10} {snippet.display_path}")
            self._finish(written)
            return
        snippet = shell_snippet(shell, command=command, env=self.env)
        where = snippet.display_path
        verb = "Add this to" if snippet.append_ok else "Put this in"
        io.say(f"{verb} {where}:" + (f"  ({snippet.note})" if snippet.note else ""))
        io.say()
        for line in snippet.text.rstrip("\n").split("\n"):
            io.say("    " + line)
        io.say()
        io.say(io.paint("-q keeps new shells quiet when the Pi-hole is unreachable.", "2"))
        if (
            self.interactive
            and snippet.append_ok
            and io.confirm(f"Append it to {snippet.path} now?", default=False)
        ):
            try:
                appended = append_snippet(snippet)
            except ConfigError as exc:
                io.fail(str(exc), exc.hint)
            except OSError as exc:
                io.fail(f"cannot update {snippet.path}: {exc.strerror or exc}", "add it by hand")
            else:
                if appended:
                    io.ok(f"updated {snippet.path}")
                else:
                    io.note(f"{snippet.path} already mentions pihello; left unchanged")
        self._finish(written)

    def _finish(self, written: bool) -> None:
        if written:
            option = self._config_option()
            self.io.say()
            self.io.say(
                f"All set! Run `pihello{option}` to see your greeting, "
                f"`pihello doctor{option}` if anything looks off."
            )


def _core_version(data: Any) -> str:
    """``"v6.1.2"`` from an ``info/version`` reply (``"v6"`` when unknown)."""
    try:
        version = data["version"]["core"]["local"]["version"]
    except (KeyError, TypeError):
        return "v6"
    return str(version) if version else "v6"


def run(
    args: argparse.Namespace,
    ui: UI,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    getpass: Callable[[str], str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Entry point of ``pihello init``; returns the exit code."""
    mode = getattr(args, "color", None)
    color = {"never": False, "always": True}.get(mode or "")
    prompter = Prompter(stdin, stdout, getpass, color=color)
    from pihello.cli import _exit_on_termination  # noqa: PLC0415

    wizard = Wizard(args, ui, prompter, env=env)
    try:
        with _exit_on_termination():  # the session stays open while presets are picked
            return wizard.run()
    except KeyboardInterrupt:
        prompter.say()  # leave the shell prompt on a fresh line
        done = (
            f"{wizard.config_path} was already written" if wizard.saved else "nothing was written"
        )
        prompter.say(f"setup cancelled; {done}")
        raise
