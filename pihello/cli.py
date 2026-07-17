import sys
import json
import ssl
from urllib import request, error
from datetime import datetime, timezone
from argparse import ArgumentParser
from pihello import Console, __version__


def get_args():
    parser = ArgumentParser(prog="pihello")
    parser.add_argument("addr", help="the address of your Pi-hole")
    parser.add_argument("password", help="your Pi-hole app password from settings")
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


def get_data(addr: str, sid: str, ctx: ssl.SSLContext, query: str = "") -> dict:
    url = f"{addr}/api/{query}?sid={sid}"
    with request.urlopen(url, context=ctx) as res:
        if res.status != 200:
            raise f"HTTP(S) error {res.status}. Check your Pi-hole address and connection."
        raw = res.read()
        try:
            return json.loads(raw)
        except json.decoder.JSONDecodeError:
            return raw.decode("utf-8")
    return None


def auth(addr: str, password: str, ctx: ssl.SSLContext) -> str:
    url = f"{addr}/api/auth"
    data={'password': password}
    json_bytes = json.dumps(data).encode('utf-8')

    req = request.Request(url, data=json_bytes, method="POST")
    req.add_header('Content-Type', 'application/json; charset=utf-8')

    try:
        with request.urlopen(req, context=ctx) as response:
            auth_response = json.loads(response.read().decode("utf-8"))
        sid = auth_response.get("session", {}).get("sid")

    except error.HTTPError as e:
        print(f"\n[HTTP Error {e.code}]: {e.reason}")
        error_details = e.read().decode('utf-8')
        print(f"Server Message: {error_details}")
        return ""
    except Exception as e:
        print(f'General failure: {e}')
        return ""
    return sid


def logout(addr: str, sid: str, ctx: ssl.SSLContext) -> bool:
    url = f"{addr}/api/auth"

    req = request.Request(url, method="DELETE")
    req.add_header("sid", sid)

    try:
        with request.urlopen(req, context=ctx) as response:
            # HTTP 204 (No Content) is a successful logout
            return response.getcode() == 204
    except Exception as e:
        print(f"Failed to logout: {e}")
        return False


def time_ago(unix_timestamp: int) -> dict:
    now = datetime.now(timezone.utc)
    past_time = datetime.fromtimestamp(unix_timestamp, timezone.utc)

    delta = now - past_time

    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    return {'days': days, 'hours': hours, 'minutes': minutes}

DEFAULT_CONTENT = """\
[cyan2]─────────────────────────────────────────────────────[]
[white]PiHole[] ([green4]{blocking}[]) [lightgreen]{version.core.local.version}[white], Web [lightgreen]{version.web.local.version}[white], FTL [lightgreen]{version.ftl.local.version}
[cyan2]─────────────────────────────────────────────────────[]
Blocking [darkcyan]{gravity.domains_being_blocked}[] domains for [steelblue]{clients.active}[] clients
Blocked [fuchsia]{queries.blocked}[] out of [lightgreen]{queries.total}[] queries [underline]today[] ([steelblue]{queries.percent_blocked}%[])
[grey37]Gravity last updated [bold grey50]{days}[grey37] days [bold grey50]{hours}[grey37] hours and [bold grey50]{minutes}[grey37] minutes ago\
"""


def main():
    args = get_args()
    pihole = "{}://{}".format(args.proto, args.addr) #, args.uri.strip("/"))

    # Create an unverified SSL context
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    app_password = args.password
    sid = auth(pihole, app_password, ctx)
    versions = flatten_dict(get_data(pihole, sid, ctx, query="info/version"))
    blocking = flatten_dict(get_data(pihole, sid, ctx, query="dns/blocking"))
    summary = flatten_dict(get_data(pihole, sid, ctx, query="stats/summary"))

    logout(pihole, sid, ctx)

    console = Console(
        args.width,
        args.height,
        tab_size=args.indent,
        variables={**versions,
                   **summary,
                   **blocking,
                   **time_ago(summary['gravity.last_update'])},
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
