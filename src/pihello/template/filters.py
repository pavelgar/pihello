"""Template filters: implementations, signatures and the filter registry.

Every filter is a pure function ``(value, *args) -> value``. Arguments are
bound and type-checked when the template is *parsed* (see :func:`bind`), so
unknown filters, wrong argument counts and bad literal values are reported
with the template position and the filter's usage string before anything is
fetched from Pi-hole.

Filters pass ``None`` (a missing value) through unchanged, except ``default``,
``if``, ``not`` and the comparison filters. Filters never raise on odd input:
non-numeric input to a numeric filter yields ``None``.

:func:`catalog` returns a machine-readable description of every filter (used
to generate the documentation).
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from .layout import pad, truncate, visible_width

if TYPE_CHECKING:
    import re
    from collections.abc import Callable, Mapping

__all__ = [
    "FILTERS",
    "MAX_TEXT",
    "Arg",
    "FilterArgError",
    "FilterInfo",
    "FilterSpec",
    "RoundedFloat",
    "bind",
    "catalog",
    "get",
    "to_number",
    "to_text",
]

_MAX_DECIMALS = 20
_MAX_WIDTH = 10_000
MAX_TEXT = 65_536
"""Most characters a single value prints (and ``repeat`` produces); the rest is cut."""
_MAX_NESTING = 32  # lists nested deeper than this are missing values (bounds recursion)


class _TooDeep(Exception):
    pass


# --------------------------------------------------------------------------- #
# Value helpers                                                                #
# --------------------------------------------------------------------------- #
def to_text(value: Any) -> str | None:
    """Default text rendering of a value (``None`` means "missing").

    ``float`` → one decimal, ``bool`` → ``yes``/``no``, lists → items joined
    with ``", "`` (missing items skipped), mappings → ``None`` (a group).
    Lists nested more than 32 deep are missing as a whole.
    """
    kind = type(value)
    if kind is str:
        return value  # type: ignore[no-any-return]
    if value is None:
        return None
    if kind is bool:
        return "yes" if value else "no"
    if kind is int:
        return str(value)
    if kind is float:
        return f"{value:.1f}"
    if isinstance(value, (list, tuple)):
        try:
            return _join(value, 0)
        except _TooDeep:
            return None
    if isinstance(value, dict) or hasattr(value, "keys"):
        return None
    if isinstance(value, float):
        return f"{value:.{getattr(value, 'decimals', 1)}f}"
    return str(value)


def _join(items: list[Any] | tuple[Any, ...], depth: int) -> str:
    """``to_text`` of a list; raises :class:`_TooDeep` instead of recursing without bound."""
    if depth >= _MAX_NESTING:
        raise _TooDeep
    texts: list[str] = []
    for item in items:
        text = _join(item, depth + 1) if isinstance(item, (list, tuple)) else to_text(item)
        if text is not None:
            texts.append(text)
    return ", ".join(texts)


class RoundedFloat(float):
    """A float that renders with a fixed number of ``decimals`` (from ``round(n)``)."""

    __slots__ = ("decimals",)
    decimals: int

    def __new__(cls, value: float, decimals: int) -> RoundedFloat:
        instance = super().__new__(cls, value)
        instance.decimals = decimals
        return instance


def to_number(value: Any) -> int | float | None:
    """Coerce to a number; ``None`` for anything non-numeric."""
    kind = type(value)
    if kind is int or kind is float:
        return value  # type: ignore[no-any-return]
    if kind is bool:
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 400:
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return value
    return None


def _finite(value: Any) -> int | float | None:
    number = to_number(value)
    if number is None or (type(number) is float and not math.isfinite(number)):
        return None
    return number


def _is_nan(number: float) -> bool:
    """``math.isnan`` that is safe for ints of any size (they are never NaN)."""
    return isinstance(number, float) and math.isnan(number)


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value)


def _clamp(value: int, low: int, high: int) -> int:
    return low if value < low else min(value, high)


def _round_half_up(number: float, digits: int) -> float:
    """Round half away from zero (``2.5`` → ``3``), unlike :func:`round`."""
    if not math.isfinite(number):
        return number
    digits = _clamp(digits, -30, 15)
    scale = 10.0**digits
    scaled = abs(number) * scale
    if scaled >= 2.0**52:  # already integral at this precision
        return number
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    result = whole / scale
    return -result if number < 0 else result


def _items(value: Any) -> list[Any] | None:
    """A list view of ``value``: lists as-is, scalars as one item, groups → ``None``."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None or isinstance(value, (dict, str)) or hasattr(value, "keys"):
        return None
    return [value]


def _lookup(item: Any, key: str) -> Any:
    for part in key.split("."):
        if isinstance(item, dict):
            item = item.get(part)
        elif isinstance(item, (list, tuple)) and part.removeprefix("-").isdecimal():
            index = int(part)
            item = item[index] if -len(item) <= index < len(item) else None
        else:
            return None
        if item is None:
            return None
    return item


# --------------------------------------------------------------------------- #
# Numbers                                                                      #
# --------------------------------------------------------------------------- #
def f_number(value: Any, decimals: int) -> str | None:
    """Thousands separators: ``48213`` → ``48,213``."""
    number = to_number(value)
    if number is None:
        return None
    decimals = _clamp(decimals, 0, _MAX_DECIMALS)
    if isinstance(number, float):  # includes RoundedFloat from round(n)
        if not math.isfinite(number):
            return str(number)
        if decimals == 0:
            return f"{int(_round_half_up(number, 0)):,}"
        return f"{_round_half_up(number, decimals):,.{decimals}f}"
    if decimals == 0:
        return f"{number:,}"
    return f"{number:,.{decimals}f}"


def f_round(value: Any, digits: int) -> int | float | None:
    """Round half away from zero; ``digits <= 0`` gives an ``int``."""
    number = to_number(value)
    if number is None:
        return None
    if type(number) is int:
        if digits >= 0:
            return number
        scale: int = 10 ** min(-digits, 30)
        magnitude: int = (abs(number) + scale // 2) // scale * scale
        return -magnitude if number < 0 else magnitude
    rounded = _round_half_up(number, digits)
    if not math.isfinite(rounded):
        return rounded
    if digits <= 0:
        return int(rounded)
    return RoundedFloat(rounded, min(digits, 15))  # floats carry ~15 significant digits


def f_int(value: Any) -> int | None:
    """Truncate toward zero."""
    number = _finite(value)
    return None if number is None else int(number)


def f_percent(value: Any, decimals: int) -> str | None:
    """A value that is already 0-100 → ``15.2%``."""
    number = to_number(value)
    if number is None:
        return None
    decimals = _clamp(decimals, 0, _MAX_DECIMALS)
    if not math.isfinite(number):
        return f"{number}%"
    return f"{_round_half_up(number, decimals):.{decimals}f}%"


def f_compact(value: Any, decimals: int) -> str | None:
    """``950``, ``48.2k``, ``1.2M``, ``3.4B``, ``5.0T``."""
    number = to_number(value)
    if number is None:
        return None
    if not math.isfinite(number):
        return str(number)
    decimals = _clamp(decimals, 0, _MAX_DECIMALS)
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    if _round_half_up(magnitude, decimals) < 1000:
        if magnitude == int(magnitude):
            return f"{sign}{int(magnitude)}"
        return f"{sign}{_round_half_up(magnitude, decimals):.{decimals}f}"
    for power, suffix in enumerate("kMBT", start=1):
        scaled = magnitude / 1000**power
        rounded = _round_half_up(scaled, decimals)
        if rounded < 1000 or suffix == "T":
            return f"{sign}{rounded:.{decimals}f}{suffix}"
    raise AssertionError("unreachable")  # pragma: no cover


_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
_BYTE_FACTORS = {unit.lower(): 1024**index for index, unit in enumerate(_BYTE_UNITS)}


def f_bytes(value: Any, unit: str, decimals: int) -> str | None:
    """IEC size; ``unit`` is the unit of the input: ``1536|bytes`` → ``1.5 KiB``."""
    number = _finite(value)
    if number is None:
        return None
    decimals = _clamp(decimals, 0, _MAX_DECIMALS)
    size = abs(number) * _BYTE_FACTORS.get(unit.lower(), 1)
    sign = "-" if number < 0 else ""
    index = 0
    while size >= 1024 and index < len(_BYTE_UNITS) - 1:
        size /= 1024
        index += 1
    if index and _round_half_up(size, decimals) >= 1024 and index < len(_BYTE_UNITS) - 1:
        size /= 1024
        index += 1
    if index == 0 and size == int(size):
        return f"{sign}{int(size)} B"
    return f"{sign}{_round_half_up(size, decimals):.{decimals}f} {_BYTE_UNITS[index]}"


# --------------------------------------------------------------------------- #
# Time                                                                         #
# --------------------------------------------------------------------------- #
_UNITS = (("d", "day", 86400), ("h", "hour", 3600), ("m", "minute", 60), ("s", "second", 1))


def _duration(seconds: float, style: str) -> str:
    total = int(abs(seconds))
    sign = "-" if seconds <= -1 else ""
    parts: list[tuple[int, str, str]] = []
    for short, long, size in _UNITS:
        amount, total = divmod(total, size)
        parts.append((amount, short, long))
    first = next((i for i, part in enumerate(parts) if part[0]), len(parts) - 1)
    chosen = [parts[first]]
    if first + 1 < len(parts) and parts[first + 1][0]:
        chosen.append(parts[first + 1])
    if style == "long":
        words = [f"{amount} {long}{'' if amount == 1 else 's'}" for amount, _, long in chosen]
        return sign + ", ".join(words)
    return sign + " ".join(f"{amount}{short}" for amount, short, _ in chosen)


def f_duration(value: Any, style: str) -> str | None:
    """Seconds → two largest units: ``2d 3h`` (``long``: ``2 days, 3 hours``)."""
    number = _finite(value)
    if number is None:
        return None
    return _duration(number, style)


def f_ago(value: Any, now: float | None = None) -> str | None:
    """Unix timestamp → ``3d 4h ago``, ``in 5m`` or ``just now`` (under a minute)."""
    timestamp = _finite(value)
    if timestamp is None:
        return None
    if now is None:
        now = time.time()
    delta = now - timestamp
    if abs(delta) < 60:
        return "just now"
    if delta > 0:
        return _duration(delta, "short") + " ago"
    return "in " + _duration(-delta, "short")


def f_datetime(value: Any, fmt: str) -> str | None:
    """Unix timestamp → local time formatted with ``strftime``."""
    timestamp = _finite(value)
    if timestamp is None:
        return None
    try:
        return time.strftime(fmt, time.localtime(timestamp))
    except (ValueError, OverflowError, OSError):
        return None


# --------------------------------------------------------------------------- #
# Text                                                                         #
# --------------------------------------------------------------------------- #
def f_upper(value: Any) -> str | None:
    """Upper case."""
    text = to_text(value)
    return None if text is None else text.upper()


def f_lower(value: Any) -> str | None:
    """Lower case."""
    text = to_text(value)
    return None if text is None else text.lower()


def f_title(value: Any) -> str | None:
    """Title Case."""
    text = to_text(value)
    return None if text is None else text.title()


def f_capitalize(value: Any) -> str | None:
    """Capitalize the first character."""
    text = to_text(value)
    return None if text is None else text.capitalize()


_WHITESPACE_CONTROLS = str.maketrans("\t\v\f", "   ")


def _measurable(value: Any) -> str | None:
    """``to_text(value)`` with tabs (and ``\\v``, ``\\f``) as single spaces.

    Widths count a tab as zero columns, but a tab in data prints as one space,
    so a padded or truncated value would come out too wide.
    """
    text = to_text(value)
    if text is None or text.isprintable():
        return text
    return text.translate(_WHITESPACE_CONTROLS)


def f_truncate(value: Any, width: int, end: str) -> str | None:
    """Limit to ``width`` visible columns, ending with ``end`` when cut."""
    text = _measurable(value)
    return None if text is None else truncate(text, _clamp(width, 0, _MAX_WIDTH), end)


def f_ljust(value: Any, width: int, fill: str) -> str | None:
    """Pad on the right to ``width`` visible columns."""
    text = _measurable(value)
    return None if text is None else pad(text, _clamp(width, 0, _MAX_WIDTH), fill, "<")


def f_rjust(value: Any, width: int, fill: str) -> str | None:
    """Pad on the left to ``width`` visible columns."""
    text = _measurable(value)
    return None if text is None else pad(text, _clamp(width, 0, _MAX_WIDTH), fill, ">")


def f_center(value: Any, width: int, fill: str) -> str | None:
    """Center within ``width`` visible columns."""
    text = _measurable(value)
    return None if text is None else pad(text, _clamp(width, 0, _MAX_WIDTH), fill, "^")


def f_replace(value: Any, old: str, new: str) -> str | None:
    """Replace every occurrence of ``old`` with ``new``."""
    text = to_text(value)
    return None if text is None else text.replace(old, new)


def f_repeat(value: Any, count: int) -> str | None:
    """Repeat the text ``count`` times; the result is cut at :data:`MAX_TEXT` characters."""
    text = to_text(value)
    if text is None:
        return None
    if count <= 0 or not text:
        return ""
    return (text * min(count, MAX_TEXT // len(text) + 1))[:MAX_TEXT]


def f_prefix(value: Any, prefix: str) -> Any:
    """Prepend ``prefix`` when the value is non-empty."""
    text = to_text(value)
    if not text:
        return value
    return prefix + text


def f_suffix(value: Any, suffix: str) -> Any:
    """Append ``suffix`` when the value is non-empty."""
    text = to_text(value)
    if not text:
        return value
    return text + suffix


def f_default(value: Any, fallback: Any) -> Any:
    """Replace a missing value or ``""`` with ``fallback``."""
    if value is None or (isinstance(value, str) and not value):
        return fallback
    return value


# --------------------------------------------------------------------------- #
# Lists                                                                        #
# --------------------------------------------------------------------------- #
def f_join(value: Any, separator: str) -> Any:
    """Join list items with ``separator``."""
    if not isinstance(value, (list, tuple)):
        return value
    items = (to_text(item) for item in value)
    return separator.join(item for item in items if item is not None)


def f_first(value: Any) -> Any:
    """First item of a list (first character of a string; a scalar is itself)."""
    if isinstance(value, (list, tuple, str)):
        return value[0] if value else None
    return None if _items(value) is None else value


def f_last(value: Any) -> Any:
    """Last item of a list (last character of a string; a scalar is itself)."""
    if isinstance(value, (list, tuple, str)):
        return value[-1] if value else None
    return None if _items(value) is None else value


def f_length(value: Any) -> int | None:
    """Number of items (or characters)."""
    if isinstance(value, (list, tuple, str, dict)):
        return len(value)
    return None


def f_take(value: Any, count: int) -> Any:
    """The first ``count`` items."""
    if isinstance(value, (list, tuple, str)):
        return value[: max(count, 0)]
    return None


def f_slice(value: Any, start: int, stop: int | None) -> Any:
    """Python-style slice ``[start:stop]`` (negative indices count from the end)."""
    if isinstance(value, (list, tuple, str)):
        return value[start:stop]
    return None


def f_pluck(value: Any, key: str) -> list[Any] | None:
    """List of mappings → list of their ``key`` values (``key`` may be dotted)."""
    if not isinstance(value, (list, tuple)):
        return None
    if "." not in key:  # fast path: plain key on a list of dicts (e.g. history)
        return [item.get(key) if type(item) is dict else _lookup(item, key) for item in value]
    return [_lookup(item, key) for item in value]


def _numbers(value: Any) -> list[int | float] | None:
    items = _items(value)
    if items is None:
        return None
    return [n for n in map(to_number, items) if n is not None and not _is_nan(n)]


def f_sum(value: Any) -> int | float | None:
    """Sum of the numeric items (``0`` for an empty list)."""
    numbers = _numbers(value)
    return None if numbers is None else sum(numbers)


def f_min(value: Any) -> int | float | None:
    """Smallest numeric item."""
    numbers = _numbers(value)
    return min(numbers) if numbers else None


def f_max(value: Any) -> int | float | None:
    """Largest numeric item."""
    numbers = _numbers(value)
    return max(numbers) if numbers else None


def f_avg(value: Any) -> float | None:
    """Mean of the numeric items."""
    numbers = _numbers(value)
    if not numbers:
        return None
    try:
        return math.fsum(numbers) / len(numbers)
    except (OverflowError, ValueError):
        return None


_SPARKS = "▁▂▃▄▅▆▇█"


def _float(value: Any) -> float | None:
    """A finite number as a float; ``None`` otherwise (ints beyond float range, too)."""
    kind = type(value)
    if kind is not float and kind is not int:
        value = _finite(value)
        if value is None:
            return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _resample(values: list[float | None], width: int) -> list[float | None]:
    count = len(values)
    if width <= 0 or width == count or not count:
        return values
    if count < width:
        return [values[index * count // width] for index in range(width)]
    result: list[float | None] = []
    for cell in range(width):
        low = cell * count // width
        high = max((cell + 1) * count // width, low + 1)
        bucket = [v for v in values[low:high] if v is not None]
        if not bucket:
            result.append(None)
            continue
        try:
            result.append(math.fsum(bucket) / len(bucket))
        except OverflowError:  # the sum, not the mean, exceeds the float range (~1e308)
            result.append(math.fsum(v / len(bucket) for v in bucket))
    return result


def f_spark(
    value: Any, width: int, minimum: float | None = None, maximum: float | None = None
) -> str | None:
    """Numbers → sparkline ``▁▂▃▅▇``; ``width > 0`` resamples to that many cells.

    ``min``/``max`` fix the scale (values are clamped to it) so several series can
    share one; a bound that is not given comes from the data.
    """
    items = _items(value)
    if items is None:
        return None
    values = _resample([_float(item) for item in items], _clamp(width, 0, 1000))
    numbers = [v for v in values if v is not None]
    if not numbers:
        return " " * len(values)
    low = min(numbers) if minimum is None else float(minimum)
    high = max(numbers) if maximum is None else float(maximum)
    if high < low:  # one fixed bound lies beyond all of the data
        if minimum is not None:
            high = low
        else:
            low = high
    # Halve everything when the range overflows (e.g. -1e308 to 1e308).
    scale = 1.0 if math.isfinite(high - low) else 0.5
    base = low * scale
    span = high * scale - base
    cells: list[str] = []
    for number in values:
        if number is None:
            cells.append(" ")
        elif span == 0:
            if minimum is not None:
                cells.append(_SPARKS[0])  # everything is at (or below) the fixed minimum
            elif maximum is not None:
                cells.append(_SPARKS[7])  # everything is at (or above) the fixed maximum
            else:
                cells.append(_SPARKS[3] if high > 0 else _SPARKS[0])
        else:
            clamped = min(max(number, low), high)
            cells.append(_SPARKS[min(7, int((clamped * scale - base) / span * 7 + 0.5))])
    return "".join(cells)


_EIGHTHS = " ▏▎▍▌▋▊▉"


def f_bar(value: Any, width: int, maximum: float, chars: str | None = None) -> str | None:
    """Number → bar of ``width`` cells, full at ``max``.

    By default the bar is smooth (eighth blocks, ``█████▍░░░░``); ``chars`` of
    exactly two characters (full, empty) draws whole cells, rounded to the
    nearest cell (``chars="#-"`` → ``#####-----``).
    """
    number = _finite(value)
    if number is None:
        return None
    width = _clamp(width, 0, 1000)
    ratio = number / maximum if maximum > 0 and math.isfinite(maximum) else 0.0
    ratio = min(max(ratio, 0.0), 1.0)
    if chars is not None:
        full = int(ratio * width + 0.5)
        return chars[0] * full + chars[1] * (width - full)
    eighths = int(ratio * width * 8 + 0.5)
    full, partial = divmod(eighths, 8)
    bar = "█" * full + (_EIGHTHS[partial] if partial else "")
    return bar + "░" * (width - full - (1 if partial else 0))


# --------------------------------------------------------------------------- #
# Logic                                                                        #
# --------------------------------------------------------------------------- #
_TRUE_WORDS = frozenset(("true", "yes", "on", "1", "enabled"))
_FALSE_WORDS = frozenset(("false", "no", "off", "0", "", "disabled"))


_token_regex: re.Pattern[str] | None = None


def _natural_compare(left: str, right: str) -> int:
    """-1/0/1 comparing digit runs numerically: ``v6.10`` > ``v6.9``.

    Runs of ASCII digits (numbers) sort before other text. Stops at the first
    run that differs, so a huge value costs no more than its first runs.
    """
    global _token_regex  # noqa: PLW0603 - compiled on first use to keep import cheap
    if _token_regex is None:
        import re  # noqa: PLC0415

        _token_regex = re.compile(r"[0-9]+|[^0-9]+")
    runs_left, runs_right = _token_regex.finditer(left), _token_regex.finditer(right)
    while True:
        a, b = next(runs_left, None), next(runs_right, None)
        if a is None or b is None:
            return (a is not None) - (b is not None)
        key_a, key_b = _natural_run(a.group()), _natural_run(b.group())
        if key_a != key_b:
            return 1 if key_a > key_b else -1


def _natural_run(run: str) -> tuple[int, int | str]:
    return (0, int(run[:300])) if "0" <= run[0] <= "9" else (1, run)


def _compare(left: Any, right: Any) -> int | None:
    """-1/0/1 comparing ``left`` with ``right``; ``None`` if not comparable."""
    if isinstance(left, bool) or isinstance(right, bool):
        values: list[bool] = []
        for item in (left, right):
            if isinstance(item, bool):
                values.append(item)
                continue
            text = (to_text(item) or "").strip().lower()
            if text in _TRUE_WORDS:
                values.append(True)
            elif text in _FALSE_WORDS:
                values.append(False)
            else:
                return None
        return (values[0] > values[1]) - (values[0] < values[1])
    a, b = to_number(left), to_number(right)
    if a is not None and b is not None:
        if _is_nan(a) or _is_nan(b):
            return None
        return (a > b) - (a < b)
    text_a, text_b = to_text(left), to_text(right)
    if text_a is None or text_b is None:
        return None
    if text_a == text_b:
        return 0
    return _natural_compare(text_a, text_b)


def f_eq(value: Any, other: Any) -> bool:
    """``value == other`` (numbers compare numerically)."""
    return value is not None and _compare(value, other) == 0


def f_ne(value: Any, other: Any) -> bool:
    """``value != other`` (a missing value gives ``false``)."""
    if value is None:
        return False
    result = _compare(value, other)
    return result is None or result != 0


def f_gt(value: Any, other: Any) -> bool:
    """``value > other``."""
    return value is not None and _compare(value, other) == 1


def f_ge(value: Any, other: Any) -> bool:
    """``value >= other``."""
    return value is not None and _compare(value, other) in (0, 1)


def f_lt(value: Any, other: Any) -> bool:
    """``value < other``."""
    return value is not None and _compare(value, other) == -1


def f_le(value: Any, other: Any) -> bool:
    """``value <= other``."""
    return value is not None and _compare(value, other) in (-1, 0)


def f_not(value: Any) -> bool:
    """Boolean negation (a missing value is false, so this gives ``true``)."""
    return not _truthy(value)


def f_if(value: Any, then: Any, otherwise: Any) -> Any:
    """``then`` if the value is truthy, else ``otherwise``."""
    return then if _truthy(value) else otherwise


def f_map(value: Any, mapping: Mapping[str, Any]) -> Any:
    """Look ``str(value)`` up in the ``key=value`` arguments; ``_`` is the fallback."""
    if isinstance(value, bool):
        keys = ["true" if value else "false", "yes" if value else "no"]
    else:
        keys = [str(value)]
        text = to_text(value)
        if text is not None and text != keys[0]:
            keys.append(text)
        if type(value) is float and value.is_integer():
            keys.append(str(int(value)))
    for key in keys:
        if key in mapping:
            return mapping[key]
    lowered = {k.lower(): v for k, v in mapping.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    if "_" in mapping:
        return mapping["_"]
    return value


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #
class _Required:
    __slots__ = ()

    def __repr__(self) -> str:
        return "REQUIRED"


REQUIRED: Any = _Required()

# Argument kinds
INT = "int"
OPT_INT = "int?"
NUM = "number"
OPT_NUM = "number?"
STR = "str"
OPT_STR = "str?"
ANY = "any"
_KIND_NAMES = {
    INT: "a whole number",
    OPT_INT: "a whole number or none",
    NUM: "a number",
    OPT_NUM: "a number or none",
    STR: "a string",
    OPT_STR: "a string or none",
    ANY: "a value",
}


class Arg:
    """A literal filter argument as written in the template.

    ``value`` is the converted literal (str/int/float/bool/None), ``raw`` the
    source text and ``quoted`` whether it was a quoted string. ``position`` is
    opaque data for error reporting (the parser stores ``(line, column)``).
    """

    __slots__ = ("position", "quoted", "raw", "value")

    def __init__(self, value: Any, raw: str, *, quoted: bool = False, position: Any = None) -> None:
        self.value = value
        self.raw = raw
        self.quoted = quoted
        self.position = position

    def __repr__(self) -> str:
        return f"Arg({self.value!r})"


class FilterArgError(ValueError):
    """Filter arguments do not match the filter's signature.

    ``arg`` is the offending :class:`Arg` (``None`` when the problem is a
    missing argument); ``hint`` usually carries the usage string. Validators
    set ``index`` (the parameter position) instead of ``arg``.
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        arg: Arg | None = None,
        index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.arg = arg
        self.index = index


class FilterSpec:
    """A registered filter: implementation, parameters and documentation."""

    __slots__ = (
        "accepts_none",
        "category",
        "description",
        "example",
        "func",
        "name",
        "params",
        "validate",
        "varkw",
        "wants_now",
    )

    def __init__(
        self,
        name: str,
        func: Callable[..., Any],
        params: tuple[tuple[str, str, Any], ...],
        description: str,
        *,
        category: str,
        example: str = "",
        accepts_none: bool = False,
        varkw: bool = False,
        wants_now: bool = False,
        validate: Callable[[list[Any]], None] | None = None,
    ) -> None:
        self.name = name
        self.func = func
        self.params = params
        self.description = description
        self.category = category
        self.example = example
        self.accepts_none = accepts_none
        self.varkw = varkw
        self.wants_now = wants_now
        self.validate = validate

    @property
    def signature(self) -> str:
        """Usage string such as ``truncate(n, end="…")``."""
        if self.varkw:
            return f"{self.name}(key=value, ..., _=fallback)"
        if not self.params:
            return self.name
        parts = []
        for name, _, default in self.params:
            parts.append(name if default is REQUIRED else f"{name}={_literal(default)}")
        return f"{self.name}({', '.join(parts)})"

    def __repr__(self) -> str:
        return f"<filter {self.signature}>"


def _literal(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(value)


class FilterInfo(NamedTuple):
    """Documentation record for one filter (see :func:`catalog`)."""

    name: str
    signature: str
    description: str
    category: str
    example: str


FILTERS: dict[str, FilterSpec] = {}
"""Every filter by name, in documentation order."""


def _register(  # noqa: PLR0917 - compact registration table below
    name: str,
    func: Callable[..., Any],
    params: tuple[tuple[str, str, Any], ...],
    description: str,
    category: str,
    example: str = "",
    **options: Any,
) -> None:
    FILTERS[name] = FilterSpec(
        name, func, params, description, category=category, example=example, **options
    )


def _check_unit(values: list[Any]) -> None:
    unit = values[0]
    if unit.lower() not in _BYTE_FACTORS:
        from .color import did_you_mean  # noqa: PLC0415

        hint = did_you_mean(unit, _BYTE_UNITS)
        raise FilterArgError(
            f"unknown unit {unit!r}",
            hint=f"{hint + ' ' if hint else ''}expected one of {', '.join(_BYTE_UNITS)}",
            index=0,
        )


def _check_style(values: list[Any]) -> None:
    if values[0] not in ("short", "long"):
        raise FilterArgError(f"unknown style {values[0]!r}", hint='use "short" or "long"', index=0)


def _check_fill(values: list[Any]) -> None:
    fill = values[1]
    if len(fill) != 1 or visible_width(fill) != 1:
        raise FilterArgError(
            f"fill must be a single one-column character, got {fill!r}",
            hint='e.g. fill="."',
            index=1,
        )


def _check_replace(values: list[Any]) -> None:
    if not values[0]:
        raise FilterArgError(
            "the text to replace must not be empty", hint='e.g. replace("-", " ")', index=0
        )


def _check_strftime(values: list[Any]) -> None:
    try:
        time.strftime(values[0], time.localtime(0))
    except ValueError as error:
        raise FilterArgError(
            f"invalid time format {values[0]!r}: {error}",
            hint='use strftime codes, e.g. "%Y-%m-%d %H:%M"',
            index=0,
        ) from None


def _check_bar_chars(values: list[Any]) -> None:
    chars = values[2]
    if chars is not None and (len(chars) != 2 or any(visible_width(c) != 1 for c in chars)):
        raise FilterArgError(
            f"chars must be exactly two one-column characters (full, empty), got {chars!r}",
            hint='e.g. bar(20, chars="#-")',
            index=2,
        )


def _check_spark_bounds(values: list[Any]) -> None:
    minimum, maximum = values[1], values[2]
    if minimum is not None and maximum is not None and not minimum < maximum:
        raise FilterArgError(
            f"min must be less than max, got min={minimum!r}, max={maximum!r}",
            hint="e.g. spark(24, min=0, max=100)",
            index=2,
        )


_PAD = (("n", INT, REQUIRED), ("fill", STR, " "))

# Numbers
_register(
    "number", f_number, (("decimals", INT, 0),), "thousands separators", "number", "48213 → 48,213"
)
_register(
    "round",
    f_round,
    (("n", INT, 0),),
    "round (halves away from zero); n=0 gives an integer",
    "number",
    "15.27 → 15",
)
_register("int", f_int, (), "truncate to an integer", "number", "15.9 → 15")
_register(
    "percent",
    f_percent,
    (("decimals", INT, 1),),
    "a value already 0-100 as a percentage",
    "number",
    "15.27 → 15.3%",
)
_register(
    "compact",
    f_compact,
    (("decimals", INT, 1),),
    "short number: 950, 48.2k, 1.2M, 3.4B",
    "number",
    "48213 → 48.2k",
)
_register(
    "bytes",
    f_bytes,
    (("unit", STR, "B"), ("decimals", INT, 1)),
    "IEC size; unit is the unit of the input (B, KiB, MiB, GiB, TiB, PiB, EiB)",
    "number",
    "{system.memory.ram.total|bytes(KiB)} → 7.9 GiB",
    validate=_check_unit,
)
# Time
_register(
    "duration",
    f_duration,
    (("style", STR, "short"),),
    'seconds → two largest units: 2d 3h ("long": 2 days, 3 hours)',
    "time",
    "183600 → 2d 3h",
    validate=_check_style,
)
_register(
    "ago",
    f_ago,
    (),
    "unix timestamp → 3d 4h ago, in 5m, or just now (under a minute)",
    "time",
    "{gravity.last_update|ago} → 3d 4h ago",
    wants_now=True,
)
_register(
    "datetime",
    f_datetime,
    (("fmt", STR, "%Y-%m-%d %H:%M"),),
    "unix timestamp → local time (strftime format)",
    "time",
    '{now|datetime("%H:%M")} → 08:30',
    validate=_check_strftime,
)
# Text
_register("upper", f_upper, (), "upper case", "text")
_register("lower", f_lower, (), "lower case", "text")
_register("title", f_title, (), "title case", "text")
_register("capitalize", f_capitalize, (), "capitalize the first letter", "text")
_register(
    "truncate",
    f_truncate,
    (("n", INT, REQUIRED), ("end", STR, "…")),
    "limit the visible width to n columns",
    "text",
    "{recent_blocked|truncate(20)}",
)
_register(
    "ljust", f_ljust, _PAD, "pad on the right to n visible columns", "text", validate=_check_fill
)
_register(
    "rjust", f_rjust, _PAD, "pad on the left to n visible columns", "text", validate=_check_fill
)
_register("center", f_center, _PAD, "center within n visible columns", "text", validate=_check_fill)
_register(
    "replace",
    f_replace,
    (("old", STR, REQUIRED), ("new", STR, REQUIRED)),
    "substring replace",
    "text",
    validate=_check_replace,
)
_register(
    "repeat",
    f_repeat,
    (("n", INT, REQUIRED),),
    "repeat the text n times (the result is cut at 65,536 characters)",
    "text",
    '{"─"|repeat(40)}',
)
_register(
    "prefix",
    f_prefix,
    (("s", STR, REQUIRED),),
    "add text before the value when it is non-empty",
    "text",
    '{timer|duration|prefix(" for ")|default("")}',
)
_register(
    "suffix",
    f_suffix,
    (("s", STR, REQUIRED),),
    "add text after the value when it is non-empty",
    "text",
)
_register(
    "default",
    f_default,
    (("x", ANY, REQUIRED),),
    'replace a missing value or "" (also suppresses missing/strict)',
    "text",
    '{timer|default("forever")}',
    accepts_none=True,
)
# Lists
_register("join", f_join, (("sep", STR, ", "),), "join list items with sep", "list")
_register("first", f_first, (), "first item of a list", "list")
_register("last", f_last, (), "last item of a list", "list")
_register("length", f_length, (), "number of items", "list")
_register("take", f_take, (("n", INT, REQUIRED),), "the first n items", "list")
_register(
    "slice",
    f_slice,
    (("start", INT, REQUIRED), ("stop", OPT_INT, None)),
    "sub-list [start:stop]",
    "list",
)
_register(
    "pluck",
    f_pluck,
    (("key", STR, REQUIRED),),
    "list of mappings → list of their key values",
    "list",
    "{history|pluck(total)|spark}",
)
_register("sum", f_sum, (), "sum of the numbers in a list", "list")
_register("min", f_min, (), "smallest number in a list", "list")
_register("max", f_max, (), "largest number in a list", "list")
_register("avg", f_avg, (), "average of the numbers in a list", "list")
_register(
    "spark",
    f_spark,
    (("width", INT, 0), ("min", OPT_NUM, None), ("max", OPT_NUM, None)),
    "numbers → sparkline ▁▂▃▅▇; width>0 resamples to that many cells; "
    "min/max fix the scale (values are clamped) so series can share it",
    "list",
    "{history|pluck(blocked)|spark(24, min=0, max=500)}",
    validate=_check_spark_bounds,
)
_register(
    "bar",
    f_bar,
    (("width", INT, 20), ("max", NUM, 100), ("chars", OPT_STR, None)),
    'number → smooth bar █████▍░░░░ (eighth blocks); chars="#-" (full, empty) '
    "draws whole cells instead",
    "number",
    '{queries.percent_blocked|bar(10)} or {queries.percent_blocked|bar(10, chars="#-")}',
    validate=_check_bar_chars,
)
# Logic
for _name, _func, _symbol in (
    ("eq", f_eq, "=="),
    ("ne", f_ne, "!="),
    ("gt", f_gt, ">"),
    ("ge", f_ge, ">="),
    ("lt", f_lt, "<"),
    ("le", f_le, "<="),
):
    _register(
        _name,
        _func,
        (("x", ANY, REQUIRED),),
        f"value {_symbol} x → true/false (missing → false)",
        "logic",
        accepts_none=True,
    )
_register("not", f_not, (), "boolean negation", "logic", accepts_none=True)
_register(
    "if",
    f_if,
    (("then", ANY, REQUIRED), ("else", ANY, "")),
    "truthy → then, else → else",
    "logic",
    "[{updates.available|if(@warn, @muted)}]",
    accepts_none=True,
)
_register(
    "map",
    f_map,
    (),
    "look str(value) up in key=value pairs; no match → _ if given, else the value",
    "logic",
    "[{blocking|map(enabled=@good, disabled=@bad, _=@warn)}]",
    varkw=True,
)
del _name, _func, _symbol


def get(name: str) -> FilterSpec | None:
    """The filter called ``name`` (case-insensitive), or ``None``."""
    spec = FILTERS.get(name)
    if spec is None:
        spec = FILTERS.get(name.lower())
    return spec


def catalog() -> tuple[FilterInfo, ...]:
    """Documentation records for every filter, in documentation order."""
    return tuple(
        FilterInfo(spec.name, spec.signature, spec.description, spec.category, spec.example)
        for spec in FILTERS.values()
    )


def _convert(arg: Arg, kind: str, spec: FilterSpec, param: str) -> Any:
    value = arg.value
    if kind == ANY:
        return value
    if kind in (OPT_INT, OPT_NUM, OPT_STR) and value is None and not arg.quoted:
        return None
    if kind in (STR, OPT_STR):
        return value if arg.quoted else arg.raw
    if kind in (INT, OPT_INT) and type(value) is int:
        return value
    if kind in (NUM, OPT_NUM) and type(value) in (int, float):
        return value
    raise FilterArgError(
        f"argument '{param}' of filter '{spec.name}' must be {_KIND_NAMES[kind]}, got {arg.raw}",
        hint=f"usage: {spec.signature}",
        arg=arg,
    )


def bind(spec: FilterSpec, args: list[Arg], kwargs: dict[str, Arg]) -> tuple[Any, ...]:
    """Check literal arguments against ``spec`` and return the bound values.

    The result is passed as ``spec.func(value, *bound)``. Raises
    :class:`FilterArgError` naming the problem, with the usage string as hint.
    """
    usage = f"usage: {spec.signature}"
    if spec.varkw:
        if args:
            raise FilterArgError(
                f"filter '{spec.name}' only takes key=value arguments", hint=usage, arg=args[0]
            )
        if not kwargs:
            raise FilterArgError(
                f"filter '{spec.name}' needs at least one key=value pair", hint=usage
            )
        return ({key: arg.value for key, arg in kwargs.items()},)
    params = spec.params
    if len(args) > len(params):
        if not params:
            message = f"filter '{spec.name}' takes no arguments"
        else:
            message = f"too many arguments for filter '{spec.name}' (at most {len(params)})"
        raise FilterArgError(message, hint=usage, arg=args[len(params)])
    names = [name for name, _, _ in params]
    for key, arg in kwargs.items():
        if key not in names:
            from .color import did_you_mean  # noqa: PLC0415

            suggestion = did_you_mean(key, names) if names else None
            raise FilterArgError(
                f"filter '{spec.name}' has no argument '{key}'",
                hint=f"{suggestion + ' ' if suggestion else ''}{usage}",
                arg=arg,
            )
    values: list[Any] = []
    for index, (name, kind, default) in enumerate(params):
        if index < len(args):
            if name in kwargs:
                raise FilterArgError(
                    f"filter '{spec.name}' got argument '{name}' twice",
                    hint=usage,
                    arg=kwargs[name],
                )
            values.append(_convert(args[index], kind, spec, name))
        elif name in kwargs:
            values.append(_convert(kwargs[name], kind, spec, name))
        elif default is REQUIRED:
            raise FilterArgError(f"filter '{spec.name}' needs argument '{name}'", hint=usage)
        else:
            values.append(default)
    if spec.validate is not None:
        try:
            spec.validate(values)
        except FilterArgError as error:
            if error.arg is None and error.index is not None:
                name = params[error.index][0]
                error.arg = args[error.index] if error.index < len(args) else kwargs.get(name)
            error.message = f"filter '{spec.name}': {error.message}"
            raise
    return tuple(values)
