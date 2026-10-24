"""General CLI behaviour: help, version, dispatch, errors, exit codes, imports."""

from __future__ import annotations

import io
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from pihello import __version__, cli, config
from pihello.cli import COMMANDS, UI, build_parser, main
from pihello.errors import (
    EXIT_AUTH,
    EXIT_INTERNAL,
    EXIT_USAGE,
    AuthenticationError,
    ConfigError,
)

if TYPE_CHECKING:
    from helpers.fakepihole import FakePihole


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    out, err = capsys.readouterr()
    return code, out, err


# --------------------------------------------------------------------------- #
# Help and version                                                            #
# --------------------------------------------------------------------------- #
def test_help_lists_commands_groups_and_examples(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["--help"], capsys)
    assert code == 0
    assert err == ""
    for command in COMMANDS:
        assert f"  {command} " in out
    for group in ("Connection:", "Template:", "Output:", "Behaviour:", "pihello 1.x"):
        assert group in out
    assert "Examples:" in out
    assert "pihello init" in out
    assert "PIHELLO_PASSWORD" in out
    assert "-ts, --timestamp" in out or "-ts [FMT], --timestamp [FMT]" in out
    assert "%H:%M" in out


def test_show_help_is_the_main_help(capsys: pytest.CaptureFixture[str]) -> None:
    _, top, _ = run(["-h"], capsys)
    _, show, _ = run(["show", "--help"], capsys)
    assert top == show


@pytest.mark.parametrize("command", [c for c in COMMANDS if c != "show"])
def test_every_subcommand_has_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run([command, "--help"], capsys)
    assert code == 0
    assert f"usage: pihello {command}" in out


@pytest.mark.parametrize("flag", ["-v", "-V", "--version"])
def test_version(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run([flag], capsys)
    assert code == 0
    assert out.strip() == f"pihello {__version__}"


def test_python_m_pihello() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pihello", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"pihello {__version__}"


def test_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(["--no-such-flag"], capsys)
    assert code == EXIT_USAGE
    assert out == ""
    assert "unrecognized arguments" in err


def test_no_abbreviations(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["--temp", "x"], capsys)
    assert code == EXIT_USAGE
    assert "unrecognized" in err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["-W", "wide"], "expected 'auto' or a number"),
        (["-H", "-1"], "must be ≥ 0"),
        (["-H", "x"], "whole number"),
        (["--timeout", "0"], "must be > 0"),
        (["--timeout", "nan"], "must be a number"),
        (["--cache", "soon"], "number of seconds"),
        (["--style", "nonsense"], "NAME=WORDS"),
        (["--color", "pink"], "invalid choice"),
    ],
)
def test_argument_validation(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(argv, capsys)
    assert code == EXIT_USAGE
    assert message in err


def test_width_type_accepts_auto() -> None:
    args = build_parser().parse_args(["-W", "AUTO", "--style", "accent = bold red"])
    assert args.width == "auto"
    assert args.style == [("accent", "bold red")]


# --------------------------------------------------------------------------- #
# Unconfigured                                                                #
# --------------------------------------------------------------------------- #
def test_unconfigured_points_at_init(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run([], capsys)
    assert code == EXIT_USAGE
    assert out == ""
    assert err.splitlines() == [
        "pihello: no Pi-hole address configured",
        "hint: run `pihello init` to set pihello up, or pass --url / set PIHELLO_URL",
    ]


def test_url_missing_with_config(capsys: pytest.CaptureFixture[str]) -> None:
    path = config.default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("theme = 'default'\n")
    code, _, err = run([], capsys)
    assert code == EXIT_USAGE
    assert f'add url = "http://pi.hole" to {path}' in err


def test_quiet_suppresses_errors_but_not_the_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(["-q"], capsys)
    assert code == EXIT_USAGE
    assert out == err == ""


def test_config_errors_are_reported(capsys: pytest.CaptureFixture[str]) -> None:
    path = config.default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("colour = 'never'\n")
    code, _, err = run(["--demo"], capsys)
    assert code == EXIT_USAGE
    assert "unknown setting 'colour'" in err
    assert "hint: did you mean 'color'?" in err


# --------------------------------------------------------------------------- #
# Error handling                                                              #
# --------------------------------------------------------------------------- #
def _raise(exc: BaseException) -> Any:
    def handler(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return handler


def test_pihello_error_exit_code_and_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(
        cli._HANDLERS, "colors", _raise(AuthenticationError("nope", hint="try again"))
    )
    code, _, err = run(["colors"], capsys)
    assert code == EXIT_AUTH
    assert err == "pihello: nope\nhint: try again\n"


def test_keyboard_interrupt_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli._HANDLERS, "colors", _raise(KeyboardInterrupt()))
    code, out, err = run(["colors"], capsys)
    assert code == 130
    assert out == err == ""


def test_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli._HANDLERS, "doctor", _raise(ZeroDivisionError("boom")))
    code, _, err = run(["doctor"], capsys)
    assert code == EXIT_INTERNAL
    assert "internal error: ZeroDivisionError: boom" in err
    assert "https://github.com/pavelgar/pihello/issues" in err
    assert "--debug" in err
    assert "Traceback" not in err


def test_unexpected_exception_with_debug_shows_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli._HANDLERS, "doctor", _raise(ZeroDivisionError("boom")))
    code, _, err = run(["doctor", "--debug", "-q"], capsys)
    assert code == EXIT_INTERNAL
    assert "Traceback (most recent call last)" in err
    assert "ZeroDivisionError: boom" in err


def test_broken_pipe(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(cli._HANDLERS, "colors", _raise(BrokenPipeError()))
    code, _, err = run(["colors"], capsys)
    assert code == 0
    assert err == ""


def test_broken_pipe_real_process() -> None:
    code = "import sys; from pihello.cli import main;sys.exit(main(['vars', '--demo']))"
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert proc.stdout is not None
    proc.stdout.readline()
    proc.stdout.close()
    proc.stdout = None  # or communicate() on Windows reads the closed pipe in a thread
    _, err = proc.communicate(timeout=60)
    assert b"Traceback" not in err
    assert b"BrokenPipe" not in err
    assert proc.returncode == 0


@pytest.mark.parametrize(
    "argv",
    [["presets", "--no-preview"], ["completion", "bash"], ["presets", "default", "--source"]],
)
def test_broken_pipe_on_the_final_flush(argv: list[str]) -> None:
    # Short output stays in stdout's buffer until the interpreter flushes it at
    # exit; a reader that already went away (`pihello ... | true`) must not
    # cause "Exception ignored ... BrokenPipeError" and exit status 120.
    import os

    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pihello", *argv],
            stdout=write_end,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    finally:
        os.close(write_end)
    assert result.stderr == b""
    assert result.returncode == 0


@pytest.mark.parametrize(
    ("encoding", "errors"),
    [
        ("ascii", "strict"),  # PYTHONIOENCODING=ascii
        ("latin-1", "strict"),
        # Regression: Windows with output redirected to a file or pipe; Python's
        # default error handler there is surrogateescape, not strict
        ("cp1252", "surrogateescape"),
    ],
)
def test_stdout_that_cannot_encode_the_output(
    encoding: str, errors: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=encoding, errors=errors)
    monkeypatch.setattr(sys, "stdout", stream)
    assert main(["--demo", "--preset", "default"]) == 0
    assert main(["vars", "--demo", "gravity"]) == 0
    assert main(["presets", "default", "--source"]) == 0
    stream.flush()
    assert "internal error" not in capsys.readouterr().err
    assert b"Pi-hole" in raw.getvalue()
    assert b"?" in raw.getvalue()  # e.g. for the "\u2191" of "update available"


@pytest.mark.parametrize(("errors", "kept"), [("surrogateescape", True), ("strict", False)])
def test_utf8_stdout_keeps_its_error_handler(errors: str, kept: bool) -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors=errors)
    cli._tolerate_unencodable(stream)
    assert (stream.errors == errors) is kept
    other = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="backslashreplace")
    cli._tolerate_unencodable(other)
    assert other.errors == "backslashreplace"  # chosen explicitly


def test_closed_pipe_on_windows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: on Windows, writing to a pipe whose reader went away raises
    # OSError(EINVAL) instead of BrokenPipeError; it was an "internal error".
    import errno

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cli, "_stdout_is_pipe", lambda: True)
    closed = OSError(errno.EINVAL, "Invalid argument")
    monkeypatch.setitem(cli._HANDLERS, "colors", _raise(closed))
    assert run(["colors"], capsys) == (0, "", "")
    # ... but only that: EINVAL for a file name, or with stdout not a pipe, is a bug
    bad_name = OSError(errno.EINVAL, "Invalid argument", "a:b")
    monkeypatch.setitem(cli._HANDLERS, "colors", _raise(bad_name))
    assert run(["colors"], capsys)[0] == EXIT_INTERNAL
    monkeypatch.setitem(cli._HANDLERS, "colors", _raise(closed))
    monkeypatch.setattr(cli, "_stdout_is_pipe", lambda: False)
    assert run(["colors"], capsys)[0] == EXIT_INTERNAL
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cli, "_stdout_is_pipe", lambda: True)
    assert run(["colors"], capsys)[0] == EXIT_INTERNAL


def test_stdout_is_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    read_end, write_end = os.pipe()
    try:
        with open(write_end, "w", closefd=False) as pipe:
            monkeypatch.setattr(sys, "stdout", pipe)
            assert cli._stdout_is_pipe() is True
    finally:
        os.close(read_end)
        os.close(write_end)
    monkeypatch.setattr(sys, "stdout", io.StringIO())  # no file descriptor
    assert cli._stdout_is_pipe() is False


def test_options_before_command_are_redispatched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake_doctor(args: Any, _ui: UI) -> int:
        seen["config"] = args.config
        return 0

    monkeypatch.setitem(cli._HANDLERS, "doctor", fake_doctor)
    assert main(["--config", "/tmp/x.toml", "doctor"]) == 0
    assert seen["config"] == "/tmp/x.toml"


# --------------------------------------------------------------------------- #
# UI                                                                          #
# --------------------------------------------------------------------------- #
class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.usefixtures("posix_terminal")
def test_ui_colors_respect_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = TTY()
    UI(stream).error("plain")
    assert "\x1b[" in stream.getvalue()
    monkeypatch.setenv("NO_COLOR", "1")
    stream = TTY()
    UI(stream).error("plain")
    assert "\x1b[" not in stream.getvalue()


def test_ui_colors_forced_and_dumb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    stream = io.StringIO()
    UI(stream).warn("x")
    assert "\x1b[" in stream.getvalue()
    monkeypatch.delenv("FORCE_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    stream = TTY()
    UI(stream).warn("x")
    assert "\x1b[" not in stream.getvalue()


@pytest.mark.parametrize(
    ("build", "vt_enabled", "colored"), [(10240, False, False), (19045, True, True)]
)
def test_ui_colors_on_windows_consoles(
    build: int, vt_enabled: bool, colored: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Windows console only understands escape codes once VT processing is on;
    # an error printed before any greeting (Pi-hole unreachable, bad config)
    # must not show up as "←[1;31mpihello:←[0m".
    from pihello import terminal

    for name in ("TERM_PROGRAM", "ConEmuANSI", "ANSICON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(terminal, "_IS_WINDOWS", True)
    monkeypatch.setattr(terminal, "_windows_build", lambda: build)
    enabled: list[object] = []
    monkeypatch.setattr(
        terminal, "_enable_windows_vt", lambda stream: enabled.append(stream) or vt_enabled
    )
    stream = TTY()
    UI(stream).error("unreachable")
    assert ("\x1b[" in stream.getvalue()) is colored
    assert enabled == ([stream] if build >= 10586 else [])


def test_ui_redacts_secrets_and_quiet() -> None:
    stream = io.StringIO()
    ui = UI(stream)
    ui.add_secret("hunter2")
    ui.add_secret(None)
    ui.add_secret("")
    ui.error("password hunter2 rejected", "hunter2 again")
    ui.warn("hunter2")
    ui.log("not shown without debug")
    assert "hunter2" not in stream.getvalue()
    assert stream.getvalue().count("********") == 3
    assert "debug" not in stream.getvalue()
    ui.quiet = True
    ui.error("silent")
    ui.warn("silent")
    assert "silent" not in stream.getvalue()


def test_debug_logging_is_redacted_and_detached() -> None:
    import logging

    stream = io.StringIO()
    ui = UI(stream)
    ui.add_secret("s3cret")
    ui.enable_debug()
    logging.getLogger("pihello.client").debug("sending %s", "s3cret")
    ui.log("own s3cret")
    ui.close()
    logging.getLogger("pihello.client").debug("after close")
    text = stream.getvalue()
    assert "s3cret" not in text
    assert "sending ********" in text
    assert "own ********" in text
    assert "after close" not in text
    ui.close()  # idempotent


def test_supports_color_with_broken_isatty() -> None:
    class Broken(io.StringIO):
        def isatty(self) -> bool:
            raise ValueError("closed")

    assert cli._supports_color(Broken(), env={}) is False


# --------------------------------------------------------------------------- #
# Laziness                                                                    #
# --------------------------------------------------------------------------- #
def _imported_modules(argv: list[str]) -> set[str]:
    code = (
        "import sys, json\n"
        "from pihello.cli import main\n"
        f"code = main({argv!r})\n"
        "sys.stderr.write(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )
    import json

    return set(json.loads(result.stderr.splitlines()[-1]))


def test_demo_imports_only_what_it_needs() -> None:
    modules = _imported_modules(["--demo"])
    assert "pihello.template" in modules
    for heavy in (
        "pihello.wizard",
        "pihello.doctor",
        "pihello.completion",
        "pihello.client",
        "pihello.cache",
        "ssl",
        "urllib.request",
        "http.client",
        "subprocess",
        "logging",
        "tomllib",
        "getpass",
    ):
        assert heavy not in modules, heavy


def test_live_show_imports_only_what_it_needs(
    fake_pihole: FakePihole, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIHELLO_PASSWORD", "pihello-test")
    modules = _imported_modules(["-u", fake_pihole.url, "-e", "{blocking}"])
    assert "pihello.client" in modules
    for unneeded in (
        "pihello.wizard",
        "pihello.doctor",
        "pihello.completion",
        "pihello.cache",
        "pihello.demo",
        "subprocess",
        "tomllib",
        "getpass",
    ):
        assert unneeded not in modules, unneeded
    assert not fake_pihole.active_sessions


def test_cli_settings_errors(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["-k", "--ca-cert", "x.pem", "--demo"], capsys)
    assert code == EXIT_USAGE
    assert "--insecure and --ca-cert" in err
    code, _, err = run(["--password-stdin", "-f", "-", "--demo"], capsys)
    assert code == EXIT_USAGE
    assert "standard input" in err


def test_resolve_settings_uses_cli_flags_in_messages(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["--preset", "x", "-e", "y"], capsys)
    assert code == EXIT_USAGE
    assert "--preset and --template-string cannot be used together" in err


def test_internal_unknown_cli_setting() -> None:
    with pytest.raises(ConfigError, match="internal"):
        config.resolve({"nope": 1}, env={})
