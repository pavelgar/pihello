"""Color-depth detection and terminal size."""

from __future__ import annotations

import io
import os
import sys
from typing import Any

import pytest

from pihello import terminal
from pihello.errors import ConfigError
from pihello.template import ColorDepth
from pihello.terminal import COLOR_MODES, detect_color_depth, terminal_size

T, C256, C16, NONE = (
    ColorDepth.TRUECOLOR,
    ColorDepth.ANSI256,
    ColorDepth.ANSI16,
    ColorDepth.NONE,
)


class FakeTTY(io.StringIO):
    def __init__(self, tty: bool = True, fileno: int | None = None) -> None:
        super().__init__()
        self._tty = tty
        self._fileno = fileno

    def isatty(self) -> bool:
        return self._tty

    def fileno(self) -> int:
        if self._fileno is None:
            raise io.UnsupportedOperation("no fileno")
        return self._fileno


class BrokenStream:
    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file")


TTY = FakeTTY(True)
PIPE = FakeTTY(False)


def detect(mode: str = "auto", stream: Any = TTY, **env: str) -> ColorDepth:
    return detect_color_depth(mode, stream=stream, env=env)


@pytest.mark.parametrize(
    ("mode", "depth"),
    [
        ("16", C16),
        ("256", C256),
        ("truecolor", T),
        (" TrueColor ", T),
    ],
)
def test_explicit_depths(mode: str, depth: ColorDepth) -> None:
    """A depth only chooses *how many* colors; *whether* to color is decided as for auto.

    Regression: ``--color 256`` (which users set to fix a misdetected depth) also
    forced colors into pipes and cron logs and overrode ``NO_COLOR``.
    """
    assert detect(mode, TTY) == depth
    assert detect(mode, TTY, COLORTERM="truecolor", TERM="linux") == depth  # not auto-detected
    assert detect(mode, PIPE) == NONE
    assert detect(mode, TTY, NO_COLOR="1") == NONE
    assert detect(mode, TTY, TERM="dumb") == NONE
    assert detect(mode, TTY, NO_COLOR="1", FORCE_COLOR="1") == NONE  # NO_COLOR wins
    assert detect(mode, PIPE, FORCE_COLOR="1") == depth
    assert detect(mode, PIPE, FORCE_COLOR="3") == depth  # FORCE_COLOR forces, the mode picks
    assert detect(mode, PIPE, TERM="dumb", CLICOLOR_FORCE="1") == depth
    assert detect(mode, PIPE, CLICOLOR_FORCE="0") == NONE


@pytest.mark.parametrize("mode", ["never", "NEVER", " never "])
def test_never(mode: str) -> None:
    assert detect(mode, TTY, FORCE_COLOR="3", CLICOLOR_FORCE="1") == NONE


def test_invalid_mode() -> None:
    with pytest.raises(ConfigError, match="invalid color mode 'rainbow'") as info:
        detect("rainbow")
    assert info.value.hint is not None
    assert "auto, always, never, 16, 256, truecolor" in info.value.hint
    assert COLOR_MODES == ("auto", "always", "never", "16", "256", "truecolor")


@pytest.mark.parametrize(
    ("stream", "env", "depth"),
    [
        (TTY, {}, C256),
        (PIPE, {}, NONE),
        (BrokenStream(), {}, NONE),
        (TTY, {"NO_COLOR": "1"}, NONE),
        (TTY, {"NO_COLOR": ""}, C256),  # empty NO_COLOR is ignored
        (TTY, {"NO_COLOR": "1", "FORCE_COLOR": "1"}, NONE),  # NO_COLOR wins
        (PIPE, {"FORCE_COLOR": "1"}, C256),
        (PIPE, {"FORCE_COLOR": "true", "COLORTERM": "truecolor"}, T),
        (PIPE, {"FORCE_COLOR": "0"}, C256),  # any non-empty value forces (force-color.org)
        (PIPE, {"FORCE_COLOR": "2"}, C256),
        (PIPE, {"FORCE_COLOR": "3"}, T),
        (PIPE, {"FORCE_COLOR": ""}, NONE),
        (PIPE, {"CLICOLOR_FORCE": "1"}, C256),
        (PIPE, {"CLICOLOR_FORCE": "0"}, NONE),
        (TTY, {"TERM": "dumb"}, NONE),
        (PIPE, {"TERM": "dumb", "FORCE_COLOR": "1"}, C256),
        (TTY, {"COLORTERM": "truecolor"}, T),
        (TTY, {"COLORTERM": "24bit", "TERM": "xterm-256color"}, T),
        (TTY, {"COLORTERM": "yes", "TERM": "xterm-256color"}, C256),
        (TTY, {"TERM": "xterm-256color"}, C256),
        (TTY, {"TERM": "screen-256color"}, C256),
        (TTY, {"TERM": "xterm-direct"}, T),
        (TTY, {"TERM": "xterm-kitty"}, T),
        (TTY, {"TERM": "xterm", "TERM_PROGRAM": "iTerm.app"}, T),
        (TTY, {"TERM": "xterm", "TERM_PROGRAM": "Apple_Terminal"}, C256),
        (TTY, {"WT_SESSION": "abc"}, T),
        (TTY, {"TERM": "xterm-256color", "WT_SESSION": "abc"}, C256),
        (TTY, {"TERM": "linux"}, C16),
        (TTY, {"TERM": "vt100"}, C16),
        (TTY, {"TERM": "xterm"}, C256),
    ],
)
@pytest.mark.usefixtures("posix_terminal")
def test_auto(stream: Any, env: dict[str, str], depth: ColorDepth) -> None:
    assert detect("auto", stream, **env) == depth


@pytest.mark.parametrize(
    ("env", "depth"),
    [
        ({}, C256),
        ({"NO_COLOR": "1"}, C256),
        ({"TERM": "dumb"}, C256),
        ({"COLORTERM": "truecolor"}, T),
        ({"TERM": "linux"}, C16),
    ],
)
@pytest.mark.usefixtures("posix_terminal")
def test_always_is_never_none(env: dict[str, str], depth: ColorDepth) -> None:
    assert detect("always", PIPE, **env) == depth


def test_defaults_use_os_environ_and_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", FakeTTY(True))
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert detect_color_depth() == T
    monkeypatch.setenv("NO_COLOR", "1")
    assert detect_color_depth() == NONE


# --------------------------------------------------------------------------- #
# Windows                                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"build": 19045, "vt": True}
    monkeypatch.setattr(terminal, "_IS_WINDOWS", True)
    monkeypatch.setattr(terminal, "_windows_build", lambda: state["build"])
    monkeypatch.setattr(terminal, "_enable_windows_vt", lambda stream: state["vt"])
    return state


@pytest.mark.parametrize(
    ("build", "vt", "env", "depth"),
    [
        (19045, True, {}, T),  # Windows 10 22H2 conhost
        (19045, True, {"WT_SESSION": "x"}, T),
        (10586, True, {}, C256),
        (10240, True, {}, NONE),  # before VT support
        (19045, False, {}, NONE),  # VT mode could not be enabled (e.g. redirected console)
        (10240, False, {"ConEmuANSI": "ON"}, C256),
        (10240, False, {"ConEmuANSI": "OFF"}, NONE),
        (10240, False, {"ANSICON": "80x25"}, C16),
        (10240, False, {"TERM": "xterm-256color"}, C256),  # mintty / MSYS
    ],
)
def test_windows(
    windows: dict[str, Any], build: int, vt: bool, env: dict[str, str], depth: ColorDepth
) -> None:
    windows.update(build=build, vt=vt)
    assert detect("auto", TTY, **env) == depth


@pytest.mark.parametrize(
    ("mode", "env", "depth"),
    [
        ("16", {}, C16),
        ("256", {}, C256),
        ("truecolor", {}, T),
        ("auto", {"FORCE_COLOR": "2"}, C256),
        ("auto", {"FORCE_COLOR": "3"}, T),
    ],
)
def test_windows_explicit_depths_enable_vt(
    monkeypatch: pytest.MonkeyPatch,
    windows: dict[str, Any],
    mode: str,
    env: dict[str, str],
    depth: ColorDepth,
) -> None:
    """Regression: conhost ignores escape sequences unless VT processing is on;
    only auto detection turned it on, so ``--color 256`` printed raw ``←[0;…m``."""
    streams: list[Any] = []
    monkeypatch.setattr(terminal, "_enable_windows_vt", streams.append)
    assert detect(mode, TTY, **env) == depth
    assert streams == [TTY]
    streams.clear()
    assert detect("never", TTY) == NONE
    assert detect("256", PIPE) == NONE
    assert streams == []


def test_explicit_depths_leave_the_console_alone_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(stream: Any) -> bool:
        raise AssertionError("not on Windows")

    monkeypatch.setattr(terminal, "_IS_WINDOWS", False)
    monkeypatch.setattr(terminal, "_enable_windows_vt", fail)
    assert detect("truecolor", TTY) == T
    assert detect("auto", PIPE, FORCE_COLOR="3") == T


def test_windows_always_falls_back_to_16(windows: dict[str, Any]) -> None:
    windows.update(build=10240, vt=False)
    assert detect("always", PIPE) == C16


def test_windows_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.delattr(sys, "getwindowsversion", raising=False)
    assert terminal._windows_build() == 0
    version = SimpleNamespace(build=22631)
    monkeypatch.setattr(sys, "getwindowsversion", lambda: version, raising=False)
    assert terminal._windows_build() == 22631


# --------------------------------------------------------------------------- #
# Terminal size                                                                #
# --------------------------------------------------------------------------- #
def test_size_not_a_tty() -> None:
    assert terminal_size(PIPE) == (0, 0)
    assert terminal_size(BrokenStream()) == (0, 0)  # type: ignore[arg-type]


def test_size_from_os(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((120, 40)))
    assert terminal_size(FakeTTY(True, fileno=1), env={}) == (120, 40)


def test_size_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((120, 40)))
    stream = FakeTTY(True, fileno=1)
    assert terminal_size(stream, env={"COLUMNS": "80"}) == (80, 40)
    assert terminal_size(stream, env={"COLUMNS": "80", "LINES": "20"}) == (80, 20)
    assert terminal_size(stream, env={"COLUMNS": "abc", "LINES": "-5"}) == (120, 40)
    assert terminal_size(stream, env={"COLUMNS": ""}) == (120, 40)
    monkeypatch.setenv("COLUMNS", "99")
    assert terminal_size(stream) == (99, 40)


def test_size_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # a TTY without a usable file descriptor (or ioctl failure) → unknown size
    assert terminal_size(FakeTTY(True), env={}) == (0, 0)

    def fail(fd: int) -> os.terminal_size:
        raise OSError("not a terminal")

    monkeypatch.setattr(os, "get_terminal_size", fail)
    assert terminal_size(FakeTTY(True, fileno=1), env={"LINES": "30"}) == (0, 30)


def test_size_default_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", PIPE)
    assert terminal_size() == (0, 0)
