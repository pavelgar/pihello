#!/usr/bin/python3

import json
from datetime import datetime
from urllib.request import urlopen
from argparse import ArgumentParser


class Color:
    WHITE = "#fff"
    LIGHT_GRAY = "#bcbcbc"
    LIME_GREEN = "#0f0"
    GREEN = "#008700"
    YELLOW = "#ffd700"
    RED = "#af0000"


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


def hex_to_rgb(s):
    convert = lambda val: min(int(val, 16), 255)
    hx = s.lstrip("#")
    if len(hx) == 3:
        return [convert(c * 2) for c in hx]
    return [convert(hx[i : i + 2]) for i in (0, 2, 4)]


def get_rgb(s):
    if isinstance(s, str):
        return hex_to_rgb(s)
    elif isinstance(s, (tuple, list)) and len(s) == 3:
        return s
    raise "Malformatted color value"


def colored(s, fg=None, bg=None, style=None):
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


def header(v):
    br_line = colored(" " + "─" * 64, fg=Color.LIME_GREEN)
    name = colored(" PiHole", fg=Color.WHITE, style="b")
    sep = colored("|", fg=Color.LIGHT_GRAY, style="b")
    core_version = colored(
        v["core_current"],
        fg=Color.RED if v["core_update"] else Color.GREEN,
    )
    ftl_version = colored(
        "(FTL: " + v["FTL_current"] + ")",
        fg=Color.RED if v["FTL_update"] else Color.GREEN,
    )
    timestamp = datetime.now().strftime("%H:%M - %a %d/%m/%y")

    title = " ".join([name, sep, core_version, ftl_version, sep, timestamp])
    return "\n".join([br_line, title, br_line])


def get_data(addr, query=""):
    res = urlopen(f"http://{addr}/admin/api.php?{query}")
    data = json.loads(res.read())
    return data


def get_args():
    parser = ArgumentParser(prog="pihello")
    parser.add_argument("addr", help="The address of your PiHole")
    args = parser.parse_args()
    return args


def main():
    args = get_args()
    versions = get_data(args.addr, query="versions")
    h = header(versions)
    print(h)


if __name__ == "__main__":
    main()
