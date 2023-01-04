import sys
import json
from urllib import request, error
from datetime import datetime
from argparse import ArgumentParser
from pihello import Console, __version__


def get_args():
    parser = ArgumentParser(prog="pihello")
    parser.add_argument("addr", help="the address of your Pi-hole")
    parser.add_argument("token", help="your Pi-hole API Token from settings")
    parser.add_argument("-v", "--version", action="version", version=__version__)
    parser.add_argument(
        "-i",
        "--indent",
        help="set the indentation step (default: 4)",
        default=4,
        type=int,
    )
    parser.add_argument("-f", "--file", help="specify path to the output config file")
    parser.add_argument(
        "-c",
        "--clip",
        help="set whether overflowing text will be discarded or added to the next line",
        action="store_true",
    )
    parser.add_argument(
        "-W",
        "--width",
        help="set the screen width (0 = no limit). (default: 80)",
        default=80,
        type=int,
    )
    parser.add_argument(
        "-H",
        "--height",
        help="set the screen height (0 = no limit). (default: 25)",
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
    parser.add_argument(
        "-p",
        "--proto",
        dest="proto",
        help="use HTTPS instead of HTTP.",
        action="store_const",
        const="https",
        default="http",
    )
    parser.add_argument(
        "-u",
        "--uri",
        dest="uri",
        help="set a different path (URI) for pihole. (default: /admin)",
        default="admin",
        type=str,
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


def get_data(addr: str, token: str, query: str = "") -> dict:
    url = f"{addr}/api.php?{query}&auth={token}"
    with request.urlopen(url) as res:
        if res.status != 200:
            raise f"HTTP(S) error {res.status}. Check your Pi-hole address and connection."
        raw = res.read()
        try:
            return json.loads(raw)
        except json.decoder.JSONDecodeError:
            return raw.decode("utf-8")
    return None


DEFAULT_CONTENT = """\
[cyan2]─────────────────────────────────────────────────────[]
[white]PiHole[] ([green4]{status}[]) [lightgreen]{core_current}[white], Web [lightgreen]{web_current}[white], FTL [lightgreen]{FTL_current}
[cyan2]─────────────────────────────────────────────────────[]
Blocking [darkcyan]{domains_being_blocked}[] domains for [steelblue]{unique_clients}[] clients
Blocked [fuchsia]{ads_blocked_today}[] out of [lightgreen]{dns_queries_today}[] queries [underline]today[] ([steelblue]{ads_percentage_today}%[])
[grey37]Gravity last updated [bold grey50]{gravity_last_updated.relative.days}[grey37] days [bold grey50]{gravity_last_updated.relative.hours}[grey37] hours and [bold grey50]{gravity_last_updated.relative.minutes}[grey37] minutes ago\
"""


def main():
    args = get_args()
    pihole = "{}://{}/{}".format(args.proto, args.addr, args.uri.strip("/"))
    auth_token = args.token
    versions = flatten_dict(get_data(pihole, auth_token, query="versions"))
    recent_blocked = {"recent_blocked": get_data(pihole, auth_token, query="recentBlocked")}
    summary = flatten_dict(get_data(pihole, auth_token, query="summary"))

    console = Console(
        args.width,
        args.height,
        tab_size=args.indent,
        variables={**versions, **summary, **recent_blocked},
    )
    if args.timestamp:
        ts = (
            datetime.now()
            if isinstance(args.timestamp, bool)
            else datetime.now().strftime(args.timestamp)
        )
        console.print(ts)

    if args.file:
        with open(args.file) as f:
            content = f.read()
            console.print(content, end="")
    else:
        console.print(DEFAULT_CONTENT)
