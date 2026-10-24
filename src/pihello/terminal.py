"""Terminal capability detection: color depth and size.

Kept dependency-free and cheap to import; everything that can be is injectable
(``env``, ``stream``) so the logic is testable without a real terminal.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from .errors import ConfigError
from .template.color import ColorDepth

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import TextIO

__all__ = ["COLOR_MODES", "ColorDepth", "detect_color_depth", "terminal_size"]

COLOR_MODES = ("auto", "always", "never", "16", "256", "truecolor")
"""Accepted values for the ``color`` setting / ``--color`` option."""

_EXPLICIT = {"16": ColorDepth.ANSI16, "256": ColorDepth.ANSI256, "truecolor": ColorDepth.TRUECOLOR}
_TRUECOLOR_TERMS = frozenset(
    ("xterm-kitty", "xterm-ghostty", "ghostty", "wezterm", "alacritty", "foot", "contour")
)
_TRUECOLOR_PROGRAMS = frozenset(("iTerm.app", "WezTerm", "vscode", "ghostty", "Hyper", "Tabby"))
_SIXTEEN_COLOR_TERMS = frozenset(("linux", "cons25", "ansi", "cygwin"))

# Windows 10 1607 (build 14393) brought 24-bit color to conhost; 1511 (10586)
# introduced VT processing with 256 colors.
_WIN_TRUECOLOR_BUILD = 14393
_WIN_VT_BUILD = 10586

_IS_WINDOWS = sys.platform == "win32"


def _windows_build() -> int:
    """The Windows build number (0 when unknown or not on Windows)."""
    getwindowsversion = getattr(sys, "getwindowsversion", None)
    if getwindowsversion is None:
        return 0
    try:
        return int(getwindowsversion().build)
    except (OSError, AttributeError, ValueError):  # pragma: no cover - defensive
        return 0


def _enable_windows_vt(stream: TextIO) -> bool:  # pragma: no cover - Windows only
    """Turn on ENABLE_VIRTUAL_TERMINAL_PROCESSING for the console behind ``stream``."""
    try:
        import ctypes  # noqa: PLC0415
        import msvcrt  # noqa: PLC0415

        handle = msvcrt.get_osfhandle(stream.fileno())  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except (OSError, AttributeError, ValueError, ImportError):
        return False


def _with_vt(depth: ColorDepth, stream: TextIO) -> ColorDepth:
    """``depth``, after turning on VT processing when the colors go to a Windows console.

    For depths that skip :func:`_depth_from_env` (explicit modes, ``FORCE_COLOR=2/3``):
    a Windows 10 console shows escape sequences as text unless VT processing is on.
    """
    if _IS_WINDOWS:
        _enable_windows_vt(stream)
    return depth


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _depth_from_env(env: Mapping[str, str], stream: TextIO) -> ColorDepth:
    """Best color depth for a color-capable terminal described by ``env``."""
    colorterm = env.get("COLORTERM", "").strip().lower()
    if colorterm in ("truecolor", "24bit"):
        return ColorDepth.TRUECOLOR
    term = env.get("TERM", "").strip().lower()
    if term.endswith("-direct") or term in _TRUECOLOR_TERMS:
        return ColorDepth.TRUECOLOR
    if env.get("TERM_PROGRAM", "") in _TRUECOLOR_PROGRAMS:
        return ColorDepth.TRUECOLOR
    if "256color" in term:
        return ColorDepth.ANSI256
    if env.get("WT_SESSION"):
        return ColorDepth.TRUECOLOR
    if term in _SIXTEEN_COLOR_TERMS or term.startswith("vt"):
        return ColorDepth.ANSI16
    if _IS_WINDOWS and not term:
        if env.get("ConEmuANSI", "").upper() == "ON":
            return ColorDepth.ANSI256
        if env.get("ANSICON"):
            return ColorDepth.ANSI16
        build = _windows_build()
        if build >= _WIN_VT_BUILD and _enable_windows_vt(stream):
            return ColorDepth.TRUECOLOR if build >= _WIN_TRUECOLOR_BUILD else ColorDepth.ANSI256
        return ColorDepth.NONE  # legacy console: no ANSI support
    return ColorDepth.ANSI256


def detect_color_depth(
    mode: str = "auto",
    *,
    stream: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> ColorDepth:
    """Decide how many colors to use for output to ``stream`` (default stdout).

    ``mode`` is one of :data:`COLOR_MODES`:

    * ``never`` → ``NONE``.
    * ``auto`` and the depths ``16`` / ``256`` / ``truecolor`` decide *whether*
      to color the same way: ``NO_COLOR`` (non-empty) → ``NONE``;
      ``FORCE_COLOR`` (any non-empty value) or ``CLICOLOR_FORCE`` (≠ ``0``)
      force color on; otherwise not a TTY → ``NONE``; ``TERM=dumb`` → ``NONE``.
    * A depth mode then uses exactly that depth. ``auto`` detects it:
      ``FORCE_COLOR=2``/``3`` → 256/truecolor; ``COLORTERM=truecolor|24bit`` →
      ``TRUECOLOR``; ``TERM`` containing ``256color`` → ``ANSI256``; Windows
      Terminal (``WT_SESSION``) → ``TRUECOLOR``; else ``ANSI256``. Known
      truecolor terminals (kitty, WezTerm, iTerm2, ...) are recognised, the
      Linux console is ``ANSI16``, and on Windows ``ConEmuANSI=ON``/``ANSICON``
      and the console's VT support (Windows 10+) are taken into account.
    * ``always``: colors even when piped and despite ``NO_COLOR``; the depth is
      detected like ``auto`` but never ``NONE`` (falls back to ``ANSI16``
      where ``auto`` would give ``NONE`` for lack of terminal support).

    Raises :class:`~pihello.errors.ConfigError` for an unknown ``mode``.
    """
    normalized = str(mode).strip().lower()
    if normalized == "never":
        return ColorDepth.NONE
    explicit = _EXPLICIT.get(normalized)
    if explicit is None and normalized not in ("auto", "always"):
        raise ConfigError(
            f"invalid color mode {mode!r}", hint=f"use one of: {', '.join(COLOR_MODES)}"
        )
    if env is None:
        env = os.environ
    if stream is None:
        stream = sys.stdout
    always = normalized == "always"

    if not always and env.get("NO_COLOR"):
        return ColorDepth.NONE
    force = env.get("FORCE_COLOR", "")
    forced = always or bool(force) or env.get("CLICOLOR_FORCE", "0") not in ("", "0")
    if not forced:
        if not _isatty(stream):
            return ColorDepth.NONE
        if env.get("TERM", "").strip().lower() == "dumb":
            return ColorDepth.NONE
    if explicit is not None:
        return _with_vt(explicit, stream)
    if force == "3":
        return _with_vt(ColorDepth.TRUECOLOR, stream)
    if force == "2":
        return _with_vt(ColorDepth.ANSI256, stream)
    depth = _depth_from_env(env, stream)
    if forced and depth == ColorDepth.NONE:
        return ColorDepth.ANSI16
    return depth


def _positive_int(value: str | None) -> int:
    if not value:
        return 0
    try:
        number = int(value)
    except ValueError:
        return 0
    return max(number, 0)


def terminal_size(
    stream: TextIO | None = None, *, env: Mapping[str, str] | None = None
) -> tuple[int, int]:
    """``(columns, lines)`` of the terminal behind ``stream`` (default stdout).

    Returns ``(0, 0)`` when ``stream`` is not a TTY. Positive ``COLUMNS`` /
    ``LINES`` environment variables override the queried size (as with
    :func:`shutil.get_terminal_size`); a dimension that cannot be determined
    is 0.
    """
    if stream is None:
        stream = sys.stdout
    if not _isatty(stream):
        return (0, 0)
    if env is None:
        env = os.environ
    columns = _positive_int(env.get("COLUMNS"))
    lines = _positive_int(env.get("LINES"))
    if not columns or not lines:
        try:
            size = os.get_terminal_size(stream.fileno())
        except (AttributeError, ValueError, OSError):
            size = os.terminal_size((0, 0))
        columns = columns or size.columns
        lines = lines or size.lines
    return (columns, lines)
