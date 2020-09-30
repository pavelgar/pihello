#!/usr/bin/python3

import json
from datetime import datetime
from urllib.request import urlopen
from argparse import ArgumentParser


class Color:
    WHITE = "#fff"
    LIGHT_GRAY = "#a8a8a8"
    LIME_GREEN = "#0f0"
    GREEN = "#008700"
    LIGHT_GREEN = "#5fd75f"
    YELLOW = "#ffd700"
    RED = "#af0000"
    LIGHT_RED = "#ff4a2e"
    CYAN = "#00afd7"


FMTCODE = {
    "b": 1,  # bold
    "f": 2,  # faint
    "i": 3,  # italic
    "u": 4,  # underline
    "x": 5,  # blinking
    "y": 6,  # fast blinking
    "r": 7,  # reverse
    "h": 8,  # hide
    "s": 9,  # strikethrough
}


def hex_to_rgb(s: str):
    convert = lambda val: min(int(val, 16), 255)
    hx = s.lstrip("#")
    if len(hx) == 3:
        return [convert(c * 2) for c in hx]
    return [convert(hx[i : i + 2]) for i in (0, 2, 4)]


def get_rgb(s: str):
    if isinstance(s, str):
        return hex_to_rgb(s)
    elif isinstance(s, (tuple, list)) and len(s) == 3:
        return s
    raise "Malformatted color value"


def colored(s: str, fg=None, bg=None, style: str = None):
    fmt = []
    if isinstance(style, str):
        fmt = [FMTCODE[x] for x in style]
    if fg:
        rgb = get_rgb(fg)
        fmt.extend([38, 2, *rgb])
    if bg:
        rgb = get_rgb(bg)
        fmt.extend([48, 2, *rgb])
    fmt = ";".join(str(code) for code in fmt)
    return f"\x1b[{fmt}m{s}\x1b[0m"


def header(versions, status: str):
    br_line = colored(" " + "─" * 64, fg=Color.LIME_GREEN)
    name = colored(" PiHole", fg=Color.WHITE, style="b")
    status = colored(
        status, fg=Color.LIGHT_GREEN if status == "enabled" else Color.LIGHT_RED
    )
    sep = colored("|", fg=Color.LIGHT_GRAY, style="b")
    core_version = colored(
        versions["core_current"],
        fg=Color.RED if versions["core_update"] else Color.GREEN,
    )
    ftl_version = colored(
        "(FTL: " + versions["FTL_current"] + ")",
        fg=Color.RED if versions["FTL_update"] else Color.GREEN,
    )
    timestamp = datetime.now().strftime("%H:%M - %a %d/%m/%y")

    title = " ".join(
        [name, f"({status})", sep, core_version, ftl_version, sep, timestamp]
    )
    return "\n".join([br_line, title, br_line])


def space(value: int, ljust=0) -> str:
    return f"{value:,}".replace(",", " ").ljust(ljust)


def row(key: str, value: str = "", percentage: float = None, indent=0, key_padding=24):
    key = colored(key.ljust(key_padding - indent), fg=Color.CYAN, style="b")
    value = space(value, 8) if isinstance(value, (int, float)) else value
    value = colored(value, fg=Color.WHITE)
    percentage = f"({percentage:.1f}%)" if not percentage == None else ""
    percentage = colored(percentage, fg=Color.LIGHT_GRAY)
    return " " * (indent + 1) + key + value + percentage


def statistics(summary, indent=4):
    gravity = row("Gravity:", summary["domains_being_blocked"])
    relative = summary["gravity_last_updated"]["relative"]
    timestamp = "{} Days {} Hours {} Minutes ago".format(
        relative["days"], relative["hours"], relative["minutes"]
    )
    last_updated = row("Last updated:", value=timestamp, indent=indent)

    today_title = row("Today:")
    total = summary["dns_queries_today"]
    today_total = row("Total queries:", total, indent=indent)
    blocked = summary["ads_blocked_today"]
    forwarded = summary["queries_forwarded"]
    cached = summary["queries_cached"]
    today_forwarded = row(
        "Forwarded:",
        value=forwarded,
        percentage=forwarded / total * 100,
        indent=indent * 2,
    )
    today_cached = row(
        "Cached:",
        value=cached,
        percentage=cached / total * 100,
        indent=indent * 2,
    )
    today_blocked = row(
        "Blocked:",
        value=blocked,
        percentage=blocked / total * 100,
        indent=indent * 2,
    )

    return "\n".join(
        [
            gravity,
            last_updated,
            "",
            today_title,
            today_total,
            today_forwarded,
            today_cached,
            today_blocked,
        ]
    )


def get_data(addr: str, query: str = ""):
    res = urlopen(f"http://{addr}/admin/api.php?{query}")
    data = json.loads(res.read())
    return data


def get_args():
    parser = ArgumentParser(prog="pihello")
    parser.add_argument("addr", help="The address of your PiHole")
    parser.add_argument("-v", "--version", action="version", version="v0.0.1")
    parser.add_argument(
        "-i",
        "--indent",
        help="Specifies the indentation step (default: 4)",
        default=4,
        type=int,
    )
    args = parser.parse_args()
    return args


def main():
    args = get_args()
    versions = get_data(args.addr, query="versions")
    summary = get_data(args.addr)
    h = header(versions, summary["status"])
    stats = statistics(summary, indent=args.indent)
    print(h)
    print(stats)


if __name__ == "__main__":
    main()
