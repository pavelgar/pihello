import sys
import json
from urllib import request, error
from datetime import datetime
from argparse import ArgumentParser
from pihello.console import Console


def get_args():
    parser = ArgumentParser(prog="pihello")
    parser.add_argument("addr", help="the address of your PiHole")
    parser.add_argument("-v", "--version", action="version", version="v0.0.1")
    parser.add_argument(
        "-i",
        "--indent",
        help="specify the indentation step (default: 4)",
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
        help='add a timestamp as the first line. Pass optional strftime format string. Example "%%H:%%M - %%a %%d/%%m/%%y"',
        nargs="?",
        const=True,
        default=False,
    )
    args = parser.parse_args()
    return args


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


def get_data(addr: str, query: str = "") -> dict:
    res = request.urlopen(f"http://{addr}/admin/api.php?{query}")
    try:
        data = json.loads(res.read())
    except json.decoder.JSONDecodeError as e:
        return None
    return data


if __name__ == "__main__":
    args = get_args()
    versions = get_data(args.addr, query="versions")
    summary = get_data(args.addr)
    versions = flatten_dict(versions)
    summary = flatten_dict(summary)
    console = Console(args.width, args.height, variables={**versions, **summary})
    if args.timestamp:
        ts = (
            datetime.now()
            if isinstance(args.timestamp, bool)
            else datetime.now().strftime(args.timestamp)
        )
        console.print(ts, end="")

    # TODO: Configurable header
    header = "\n[lightgreen]PiHole[grey] {core_current} (FTL {FTL_current})"
    console.print("[seagreen1]" + "─" * 64, header)
