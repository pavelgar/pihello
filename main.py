#!/usr/bin/python3

import sys
import json
from urllib import request, error
from datetime import datetime
from argparse import ArgumentParser
from pihello import Console


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


def get_data(addr: str, query: str = "") -> dict:
    res = request.urlopen(f"http://{addr}/admin/api.php?{query}")
    try:
        data = json.loads(res.read())
    except json.decoder.JSONDecodeError as e:
        return None
    return data


def get_args():
    parser = ArgumentParser(prog="pihello")
    parser.add_argument("addr", help="the address of your PiHole")
    parser.add_argument("-v", "--version", action="version", version="v0.0.1")
    parser.add_argument(
        "-i",
        "--indent",
        help="Specify the indentation step (default: 4)",
        default=4,
        type=int,
    )
    parser.add_argument(
        "-W",
        "--width",
        help="specify the screen width (0 = no limit). Default: 80",
        default=80,
        type=int,
    )
    parser.add_argument(
        "-H",
        "--height",
        help="specify the screen height (0 = no limit). Default: 25",
        default=25,
        type=int,
    )
    parser.add_argument(
        "-ts",
        "--timestamp",
        help="add a timestamp as the first line. Pass optional strftime format string",
        nargs="?",
        const=True,
        default=False,
    )
    args = parser.parse_args()
    return args


def print_dict(d):
    """Used for making the README table"""
    for k, v in d.items():
        t = type(v).__name__
        print(f"| `{k}` | `{v}` | {t} |")


def flatten_dict(d, tld="") -> dict:
    """Flatten the given dict recursively while prepending the upper level key to all the lower level keys separated by a dot (.)"""
    new_dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            lower = flatten_dict(v, tld=f"{tld}{k}.")
            new_dict.update(lower)
        else:
            key = tld + k
            new_dict[key] = v

    return new_dict


def main():
    args = get_args()
    versions = flatten_dict(get_data(args.addr, query="versions"))
    summary = flatten_dict(get_data(args.addr))

    header = "\n[lightgreen]PiHole[grey] {core_current} (FTL {FTL_current})"

    console = Console(args.width, args.height, variables={**versions, **summary})
    if args.timestamp:
        ts = (
            datetime.now()
            if isinstance(args.timestamp, bool)
            else datetime.now().strftime(args.timestamp)
        )
        console.print(ts, end="")
    console.print("[seagreen1]" + "─" * 64, header)


if __name__ == "__main__":
    main()
