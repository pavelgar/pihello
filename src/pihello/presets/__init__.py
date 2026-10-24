"""Built-in preset templates.

Each preset is a template file ``presets/<name>.txt`` shipped inside the
package, plus the metadata registered in :data:`_REGISTRY` below. Templates are
read from the package directory the first time they are needed and cached, so
importing this module costs nothing. (Plain file reads are used instead of
:mod:`importlib.resources`, which adds a few milliseconds to every shell start;
pihello is installed as a regular wheel, so the files are on disk.)

Every preset except ``classic`` only uses the semantic theme styles
(``[@label]``, ``[@good]`` …), so it works with every theme.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path

from pihello.errors import ConfigError

__all__ = ["Preset", "available", "get", "names"]


@dataclass(frozen=True)
class Preset:
    """A built-in template with a short description and its recommended width."""

    name: str
    description: str
    source: str = field(repr=False)
    min_width: int


# name -> (description, min_width). ``min_width`` is the widest line rendered
# from demo data, also with blocking disabled on a timer and updates pending
# (tests verify it), i.e. the narrowest terminal that shows the preset unwrapped.
_REGISTRY: dict[str, tuple[str, int]] = {
    "default": ("the flagship greeting: status, versions, queries, clients, gravity", 62),
    "box": ("framed MOTD-style greeting with rounded borders", 56),
    "classic": ("the exact pihello 1.x look, fixed colors", 56),
    "compact": ("three short lines: status, queries, gravity", 60),
    "dashboard": ("PADD-inspired sectioned dashboard with bars and 24h sparklines", 66),
    "emoji": ("playful, emoji-decorated summary", 52),
    "fetch": ("neofetch-style: Pi-hole logo on the left, key: value info on the right", 66),
    "full": ("everything useful: versions, queries, system, top lists, upstream", 75),
    "history": ("24 hour sparklines of total, blocked and cached queries with min/max/avg", 57),
    "minimal": ("a single line with status and today's numbers", 63),
    "plain": ("ASCII only, no box drawing or emoji; safe for logs and old terminals", 60),
    "status": ("ultra-short segment for tmux, starship and status bars", 25),
    "system": ("host focus: uptime, load, CPU, memory and swap bars, temperature, network", 60),
    "top": ("top 5 permitted domains, blocked domains and clients", 64),
    "updates": ("component versions table with update markers", 56),
}


def names() -> tuple[str, ...]:
    """Return every preset name, ``default`` first, then alphabetically."""
    return ("default", *sorted(name for name in _REGISTRY if name != "default"))


@functools.cache
def _load(name: str) -> Preset:
    """Read ``<name>.txt`` from the package (once) and wrap it in a :class:`Preset`."""
    description, min_width = _REGISTRY[name]
    source = Path(__file__).with_name(f"{name}.txt").read_text(encoding="utf-8")
    return Preset(name=name, description=description, source=source, min_width=min_width)


def available() -> tuple[Preset, ...]:
    """Return every built-in preset, ``default`` first, then alphabetically."""
    return tuple(_load(name) for name in names())


def get(name: str) -> Preset:
    """Return the built-in preset called ``name``.

    Raises:
        ConfigError: no such preset (the hint suggests the closest name).
    """
    key = name.strip().lower()
    if key not in _REGISTRY:
        from difflib import get_close_matches  # noqa: PLC0415 - only needed on errors

        choices = list(names())
        close = get_close_matches(key, choices, n=1, cutoff=0.6)
        listing = ", ".join(choices)
        hint = (
            f"did you mean {close[0]!r}? Choose one of: {listing}"
            if close
            else f"choose one of: {listing} (or use a template file)"
        )
        raise ConfigError(f"unknown preset {name!r}", hint=hint)
    return _load(key)
