# Architecture

> [!IMPORTANT]
> This document is the source of truth for how pihello is put together and the contracts between its modules.
> If you change an interface, update this file in the same pull request.

pihello renders a short, styled status message about a Pi-hole **v6** instance (any `v6.x` release) for use as a terminal greeting, MOTD, status-bar segment or script input.
It has **zero runtime dependencies** on Python ≥ 3.11 (`tomli` is pulled in on 3.10 only) and must stay fast: it runs every time a shell opens.

```text
             ┌────────────┐   paths    ┌─────────────┐  endpoints  ┌──────────────┐
 template ──▶│ Template   │──────────▶ │ sources     │ ──────────▶ │ PiholeClient │──▶ Pi-hole /api
 (preset /   │ .parse()   │            │ .plan()     │             │ (urllib)     │
  file)      └────────────┘            │ .collect()  │ ◀────────── └──────────────┘
                   │                   └─────────────┘   JSON
                   │ .render(variables, theme, options)        ▲
                   ▼                                            │ cache (optional)
              ANSI text ──▶ stdout
```

User documentation lives next to this file; the [documentation index](README.md) lists it.
[Filters](filters.md) and [Variables](variables.md) are generated from `filters.catalog()` and `sources.catalog()` by `scripts/gen_docs.py`,
and the [preset](presets.md) and [theme](themes.md) galleries by `scripts/render_screenshots.py`.

**On this page:** [Package layout](#package-layout) · [Exit codes](#exit-codes-pihelloerrors) · [Template language](#template-language) ·
[Terminal](#terminal-pihelloterminal) · [Variables and data sources](#variables-and-data-sources-pihellosources) ·
[Pi-hole client](#pi-hole-client-pihelloclient) · [Cache](#cache-pihellocache) · [Demo data](#demo-data-pihellodemo) ·
[Presets and themes](#presets-and-themes) · [Configuration](#configuration-pihelloconfig) · [Command line](#command-line-pihellocli) ·
[Setup wizard](#setup-wizard-pihellowizard) · [Diagnostics](#diagnostics-pihellodoctor) ·
[Shell completion](#shell-completion-pihellocompletion) · [Testing](#testing)

## Package layout

```text
src/pihello/
├── __init__.py        # __version__ only (keep imports cheap)
├── __main__.py        # python -m pihello
├── errors.py          # exception hierarchy + exit codes (shared)
├── cli.py             # argparse, subcommand dispatch, show/--watch/vars/presets/themes/colors
├── config.py          # TOML config, profiles, env vars, precedence, secrets, TOML writer
├── wizard.py          # `pihello init` interactive setup, shell snippets
├── doctor.py          # `pihello doctor` diagnostics
├── completion.py      # `pihello completion` scripts, generated from the argparse parsers
├── client.py          # Pi-hole v6 REST client (auth, sessions, TLS, errors)
├── sources.py         # variable namespaces → endpoints, fetch planning, transforms, catalog
├── cache.py           # optional on-disk cache of collected variables
├── demo.py            # realistic offline demo data
├── terminal.py        # color-depth detection, terminal size
├── themes.py          # built-in themes (semantic style names → styles)
├── py.typed
├── presets/
│   ├── __init__.py    # preset registry
│   └── *.txt          # preset templates
└── template/
    ├── __init__.py    # public API: Template, RenderOptions, ColorDepth, TemplateError,
    │                  #             render(), escape(), sanitize(), strip_ansi(), visible_width()
    ├── parser.py      # tokenizer + parser → flat node list (Text, NEWLINE, Tag, Expr)
    ├── engine.py      # Template, RenderOptions, evaluation, render(), escape()
    ├── filters.py     # filter implementations, signatures, registry, catalog()
    ├── style.py       # style-tag words → Style objects → SGR sequences
    ├── color.py       # Color parsing, ANSI codes, depth downgrade
    ├── color_tables.py# xterm names + 256-color palette
    └── layout.py      # visible width, tab expansion, wrap/clip, ANSI serialisation
```

## Exit codes (`pihello.errors`)

| Code | Constant          | Description                                                        |
| ---: | :---------------- | :------------------------------------------------------------- |
|    0 | `EXIT_OK`         | success                                                        |
|    1 | `EXIT_INTERNAL`   | unexpected internal error (a bug)                              |
|    2 | `EXIT_USAGE`      | bad CLI usage, config error, template error (`ConfigError`, `TemplateError`) |
|    3 | `EXIT_CONNECTION` | Pi-hole unreachable: DNS, refused, timeout, TLS (`ConnectionFailed`, `TLSError`) |
|    4 | `EXIT_AUTH`       | authentication failed (`AuthenticationError`, `TwoFactorRequired`, `TooManySessions`) |
|    5 | `EXIT_API`        | unexpected API response (`APIError`, `EndpointNotFound`)       |

The CLI also returns 130 on Ctrl-C (`--watch` returns 0) and 0 on a broken pipe.

All intentional errors subclass `PiHelloError(message, *, hint=None)`, whose `exit_code` class attribute selects the code (`PiholeError` is the base of the network/API errors).
The CLI prints `pihello: <message>`, then, for a `TemplateError`, its `excerpt` indented, then `hint: <hint>` when present, all to **stderr**.
`TemplateError(message, *, source_name, line, column, hint, excerpt)` renders as `<source_name>:<line>:<column>: <message>` and keeps the bare message in `.reason`; `excerpt` is the offending source line with a caret under the column.
`APIError` carries `status`, `key`, `api_message` and `path`.

## Template language

Templates are plain UTF-8 text (a UTF-16 BOM, as Windows PowerShell 5.1's `>` writes, is honoured when a file is read).
Everything is literal except three constructs.
pihello 1.x templates keep working; the only syntax differences are that `\]`, `\}` and `\\` are now escapes (1.x printed them verbatim) and that `normal` resets the style (1.x emitted SGR 10).

### Style tags

A tag **replaces** the whole current style (it is not additive).
`[]` resets.
The active style carries across newlines.

Words inside a tag are separated by whitespace, case-insensitive, any order; within one tag later words override earlier ones for the same slot:

| Word                                  | Description                                  |
| :------------------------------------ | :--------------------------------------- |
| `bold`                                | SGR 1                                    |
| `dim`, `faint`                        | SGR 2                                    |
| `italic`                              | SGR 3                                    |
| `underline`                           | SGR 4                                    |
| `dunderline`                          | SGR 21 (double underline)                |
| `blink`                               | SGR 5                                    |
| `fblink`                              | SGR 6                                    |
| `reverse`                             | SGR 7                                    |
| `hidden`                              | SGR 8                                    |
| `strike`                              | SGR 9                                    |
| `overline`                            | SGR 53                                   |
| `normal`, `reset`                     | clears everything before it in the tag (`[normal]` = `[]`) |
| `<color>`                             | foreground color                         |
| `:<color>` or `on <color>`            | background color                         |
| `_<color>`                            | underline color (SGR 58)                 |
| `@<name>`                             | expands to the active theme's style `<name>` |
| `{expr}`                              | expression output is spliced in and parsed as words (conditional styling) |

Colors: an xterm color name (`steelblue`, `grey37`, … see `color_tables.py`; `_`, `-`, spaces and `gray`/`grey` spellings are normalised), `color(N)` with 0 ≤ N ≤ 255, `#rrggbb`, `#rgb`, `rgb(r, g, b)` or `default`.

Static tags are validated and built at parse time.
Tags with `@name` or `{expr}` are built at render time: an unknown `@name` written in the template (including inside the tag's expressions, e.g. `map(enabled=@good)`) is a `TemplateError` on every render, whatever branch the data selects.
Words that *data* produces and that aren't valid styles (or unknown `@name`s) are dropped, unless `RenderOptions.strict`, which makes them errors.

### Expressions

```text
{ head ( | filter )* ( :format_spec )? }
head    := path | "string" | 'string' | number
path    := key ( . key )*          key chars: A-Z a-z 0-9 _ % -
filter  := name | name ( arg, arg, key=value, ... )
arg     := "str" | 'str' | int | float | true | false | none | null | bareword
```

- Whitespace around tokens is ignored: `{ queries.total | number }`.
- Path segments that are integers index into lists; negative ones count from the end: `{top_domains.0.domain}`, `{top_domains.-1.domain}`.
  A flat mapping with dotted keys (`{"queries.total": 1}`) works too.
- Strings understand `\<quote>`, `\\`, `\n` and `\t`.
- A bareword argument (`@good`, `enabled`, `v6.5`) is a string; barewords that look like numbers are numbers.
  Arguments are literals only.
- Filter names are case-insensitive.
- `:format_spec` is a Python format spec applied last: `{queries.percent_blocked:.2f}`, `{x:>8}`.
  A `:` inside parentheses or quotes belongs to the filter arguments.
  Specs are checked at parse time (and limited to widths/precisions ≤ 999).
  At render time bools and lists are turned into text first and numeric strings are tried as numbers; a spec that doesn't fit the value falls back to the default rendering (strict: `TemplateError`).
- **Missing values**: a path that does not resolve (endpoint unavailable on this Pi-hole, key absent, value `null`) yields `None`.
  Filters pass `None` through unchanged, except `default`, `if`, `not` and the comparison filters.
  A filter that raises yields `None` (strict: `TemplateError`).
  A `None` that reaches the output renders as `RenderOptions.missing` (default `?`) or, with `RenderOptions.strict`, raises `TemplateError` naming the path (with a did-you-mean suggestion when a similar path exists).
  Expressions that use `default` are never reported as missing.
- Default rendering of final values: `float` → 1 decimal (`15.2`, 1.x behaviour; the result of `round(n)` keeps `n` decimals); `bool` → `yes`/`no`; `list` → items joined with `, `; `dict` → treated as missing (strict: error "… is a group; use e.g. {path.key}").
- Whole terminal escape sequences, other control characters (except `\n` and `\t`) and lone surrogates are removed from values.
  A value containing `\n` continues on a new output line.

### Escapes and comments

- `\[`, `\]`, `\{`, `\}`, `\\` produce the literal character.
  Any other `\x` is kept verbatim (so `C:\path` works).
  A lone `]` or `}` is literal.
- Raw CSI/OSC escape sequences in the source are passed through as literal text (stripped for `ColorDepth.NONE`); their `[` never starts a tag.
- `{# ... #}` is a comment (it may span lines).
  A line containing only comments/whitespace is removed entirely, including its newline.
- `\r\n` is normalised to `\n`.
  Exactly one trailing newline of the source is dropped; `render()` returns text without a trailing newline (the CLI adds one).

### Filters

All filters are pure functions `(value, *args) -> value`, registered with `_register(name, func, params, description, category, example)` in `filters.py`.
`filters.catalog()` returns a `FilterInfo(name, signature, description, category, example)` per filter; the documentation is generated from it, so keep descriptions and examples accurate.
Unknown filter names, wrong argument counts or types and failed argument validators (e.g. `bytes` units, `bar` chars, `spark` bounds, `datetime` formats) are **parse-time** `TemplateError`s.
Filters never raise on odd input: non-numeric input to a numeric filter yields `None`.

| Filter | Description | Example |
| :-- | :-- | :-- |
| `number(decimals=0)` | thousands separators | `48213` → `48,213` |
| `round(n=0)` | round half away from zero; `n=0` gives an int | `15.27` → `15` |
| `int` | truncate to int | |
| `percent(decimals=1)` | value already 0–100 → `15.2%` | |
| `compact(decimals=1)` | `950`, `48.2k`, `1.2M`, `3.4B`, `5.0T` | |
| `bytes(unit="B", decimals=1)` | IEC size; `unit` is the input unit (`B`, `KiB`, `MiB`, `GiB`, `TiB`, `PiB`, `EiB`) | `{system.memory.ram.total\|bytes(KiB)}` → `7.9 GiB` |
| `duration(style="short")` | seconds → `2d 3h` (two largest units) / `long`: `2 days, 3 hours` | |
| `ago` | unix timestamp → `3d 4h ago`, `just now`, `in 5m` (relative to `now`) | |
| `datetime(fmt="%Y-%m-%d %H:%M")` | unix timestamp → local time | |
| `upper`, `lower`, `title`, `capitalize` | case | |
| `truncate(n, end="…")` | limit visible width to `n` | |
| `ljust(n, fill=" ")`, `rjust(...)`, `center(...)` | pad to visible width `n`; `fill` is one one-column character | |
| `replace(old, new)` | substring replace (`old` non-empty) | |
| `repeat(n)` | repeat string | `{"─"\|repeat(40)}` |
| `prefix(s)`, `suffix(s)` | add text only when the value is non-empty | `{timer\|duration\|prefix(" for ")}` |
| `default(x)` | replace `None`/`""` (also suppresses missing/strict) | |
| `join(sep=", ")`, `first`, `last`, `length` | lists (`first`/`last`/`length` also work on strings) | |
| `take(n)`, `slice(start, stop=none)` | sub-list (or substring) | |
| `pluck(key)` | list of dicts → list of values; `key` may be dotted | `{history\|pluck(total)\|spark}` |
| `sum`, `min`, `max`, `avg` | numeric aggregates of a list | |
| `spark(width=0, min=none, max=none)` | numbers → `▁▂▃▅▇`; `width>0` resamples to that many cells; `min`/`max` fix the scale (values clamped; a missing bound comes from the data) so series can share it | `{history\|pluck(blocked)\|spark(24, min=0, max=500)}` |
| `bar(width=20, max=100, chars=none)` | number → smooth bar `█████▍░░░░` (eighth blocks); `chars` = exactly 2 one-column chars (full, empty) draws whole cells, rounded to the nearest cell | `{x\|bar(20, chars="#-")}` (x = 25) → `#####---------------` |
| `eq(x)`, `ne(x)`, `gt(x)`, `ge(x)`, `lt(x)`, `le(x)` | comparisons → bool (`None` → `false`); numbers numerically, bools with `yes`/`on`/`enabled`… words, other text in natural order (`v6.10` > `v6.9`) | |
| `not` | boolean negation | |
| `if(then, else="")` | truthy → `then` else `else` | `[{updates.available\|if(@warn, @muted)}]` |
| `map(k=v, ..., _=fallback)` | lookup by `str(value)` (case-insensitive fallback); no match → `_` if given, else value | `[{blocking\|map(enabled=@good, disabled=@bad, _=@warn)}]` |

### Public API (`pihello.template`)

```python
class ColorDepth(enum.IntEnum):
    NONE = 0        # no escape codes at all
    ANSI16 = 1
    ANSI256 = 2
    TRUECOLOR = 3

@dataclass(frozen=True)
class RenderOptions:             # ValueError for negative/non-int sizes or a bad overflow
    color: ColorDepth = ColorDepth.TRUECOLOR
    width: int = 0               # 0 = unlimited (visible columns)
    height: int = 0              # 0 = unlimited (lines)
    overflow: Literal["wrap", "clip"] = "wrap"
    tab_size: int = 4
    strict: bool = False
    missing: str = "?"
    now: float | None = None     # timestamp for `ago`; None = time.time()

class Template:
    source: str
    source_name: str
    def __init__(self, source: str, *, name: str = "<template>") -> None: ...  # TemplateError
    @classmethod
    def parse(cls, source: str, *, name: str = "<template>") -> Template: ...   # TemplateError
    def paths(self) -> frozenset[str]: ...   # every variable path referenced (incl. inside style tags)
    def render(self, variables: Mapping[str, Any], *,
               theme: Mapping[str, str] | None = None,
               options: RenderOptions = RenderOptions()) -> str: ...
               # TemplateError: unknown @theme style; with strict also missing values

def render(source: str, variables: Mapping[str, Any], **kwargs: Any) -> str: ...
    # kwargs: name=, theme=, options=, or any RenderOptions field; parsed templates are cached
def escape(text: str) -> str: ...           # escape [ ] { } \ so text renders literally
def strip_ansi(text: str) -> str: ...
def sanitize(text: str) -> str: ...         # remove escape sequences, C0/C1 controls (not \n),
                                            # lone surrogates and bidi/invisible format characters;
                                            # \t \v \f become one space; used for all Pi-hole data
def visible_width(text: str) -> int: ...    # ANSI-aware, East-Asian-wide aware
TemplateError                               # re-exported from pihello.errors
```

Rendering details: style state carries across lines; every output line that used styling ends with `ESC[0m` and the next line re-emits the active style, so wrapping/clipping never bleeds color.
Each style's SGR sequence starts with `0` (styles replace each other) and is only emitted before visible text.
Colors are downgraded to the requested `ColorDepth` (truecolor → nearest xterm-256 entry 16–255 → nearest of 0–15; palette colors 0–15 always use the classic 30–37/90–97 codes; underline colors are dropped at 16 colors); `ColorDepth.NONE` emits no escape sequences at all.
Lines are wrapped (at the last whitespace, else hard) or clipped by visible width after tab expansion; wrapped continuations keep the style.
`height` truncates to that many lines.

## Terminal (`pihello.terminal`)

```python
COLOR_MODES = ("auto", "always", "never", "16", "256", "truecolor")
def detect_color_depth(mode: str = "auto", *, stream: TextIO | None = None,
                       env: Mapping[str, str] | None = None) -> ColorDepth
    # ConfigError for an unknown mode. never → NONE; 16/256/truecolor → that depth,
    #       but whether to color at all is decided like auto (NO_COLOR, TTY, FORCE_COLOR).
    # auto: NO_COLOR (non-empty) → NONE; FORCE_COLOR (2 → ANSI256, 3 → TRUECOLOR,
    #       other non-empty → force on) / CLICOLOR_FORCE ≠ 0 → force on;
    #       not a TTY → NONE; TERM=dumb → NONE; COLORTERM truecolor|24bit, TERM *-direct,
    #       known truecolor terminals (kitty, ghostty, WezTerm, alacritty, foot, iTerm2,
    #       VS Code, …) → TRUECOLOR; TERM *256color* → ANSI256; Windows Terminal
    #       (WT_SESSION) → TRUECOLOR; linux console / vt* → ANSI16; Windows console:
    #       ConEmu, ANSICON or VT support by build number; else ANSI256.
    # always: like auto but ignores NO_COLOR and never returns NONE (falls back to ANSI16).
def terminal_size(stream: TextIO | None = None, *,
                  env: Mapping[str, str] | None = None) -> tuple[int, int]
    # (columns, lines); (0, 0) if not a TTY; positive COLUMNS / LINES override
```

## Variables and data sources (`pihello.sources`)

Templates reference **paths**; the first segment is a **namespace**.
Only the sources needed by the template's namespaces are fetched (in parallel).
[variables.md](variables.md) documents every path (generated from `catalog()`).

| Namespace(s) | Endpoint | Shape (abridged) |
| :-- | :-- | :-- |
| `queries`, `clients`, `gravity` | `stats/summary` | `queries.{total, blocked, percent_blocked, unique_domains, forwarded, cached, frequency, types.*, status.*, replies.*}`, `clients.{active,total}`, `gravity.{domains_being_blocked, last_update}` plus derived `gravity.relative.{days,hours,minutes}` |
| `blocking`, `timer` | `dns/blocking` | `"enabled"\|"disabled"\|"failed"\|"unknown"` (a bool is mapped to enabled/disabled, anything else to `"unknown"`), seconds or `None` |
| `recent_blocked`, `recent_blocked_list` | `stats/recent_blocked?count=10` | latest blocked domain (`""` if none); list of up to 10 |
| `version`, `updates` | `info/version` | `version.{core,web,ftl}.{local,remote}.{version,branch,hash,…}`, `version.docker.{local,remote}`, plus derived `version.<c>.update: bool \| None`; `updates.{available: bool, count: int, components: list[str], status: "available"\|"current"\|"unknown"}` |
| `system` | `info/system` | `uptime` (s), `memory.{ram,swap}.{total,free,used,available,%used}` (KiB), `procs`, `cpu.{nprocs,%cpu,load.{raw,percent}[3]}`, `ftl.{%mem,%cpu}` |
| `host` | `info/host` | `uname.{domainname,machine,nodename,release,sysname,version}`, `model`, `dmi.*` |
| `sensors` | `info/sensors` | `list`, `cpu_temp`, `hot_limit`, `unit` |
| `ftl` | `info/ftl` | `database.{gravity,antigravity,groups,lists,clients,domains.*.{total,enabled},regex.*.{total,enabled}}`, `privacy_level`, `query_frequency`, `clients.*`, `uptime` (s), `pid`, `%mem`, `%cpu`, `allow_destructive`, `dnsmasq.*` |
| `database` | `info/database` | `size`, `queries`, `earliest_timestamp`, … |
| `messages` | `info/messages/count` | `count` |
| `top_domains`, `top_blocked` | `stats/top_domains?count=10` (`?blocked=true&count=10`) | list of `{domain, count}` |
| `top_clients`, `top_blocked_clients` | `stats/top_clients?count=10` (`?blocked=true&count=10`) | list of `{name, ip, count, label}` (`label` = name or ip) |
| `upstreams` | `stats/upstreams` | list of `{ip, name, port, count, statistics.{response,variance}, label}` (`label` = `name#port` or `ip#port`) |
| `history` | `history` | list of `{timestamp, total, cached, blocked, forwarded}` (24 h, 10-min buckets) |
| `network` | `network/gateway` | `gateway`, `interface`, `address`, `addresses` (IPv4 route preferred) |
| *(builtin)* `now`, `pihello`, `pihole` | — | `now` (unix time), `pihello.version`, `pihole.{url,host}` |

Transforms:

- remove the `"took"` field from every payload;
- coerce values the catalog documents as `float` (or `list[float]`) to `float` (Pi-hole sends `0` for `0.0`);
- normalise per-version differences so paths are stable across v6.x: FTL v6.0's plain counts in `ftl.database.{domains,regex}.*` become `{"total": n}`; `ftl.uptime` is converted from milliseconds to seconds;
- map Pi-hole's "unknown" markers in `info/version` (`"null"`, `""`) to `None`.
  `version.<c>.update` compares versions on `master` and commit hashes on other branches; it is `None` when the remote side is unknown (offline, rate-limited update checker) or doesn't apply (`docker` outside Docker).
  In Docker only the image counts towards `updates`.
  `updates.status` is `"unknown"` when no counted component has a known update status (offline, GitHub rate limit, start-up);
- drop FTL's pseudo upstreams `blocklist` and `cache` from `upstreams`.

A malformed payload becomes an `APIError`.
Endpoints that do not exist (404) simply leave their namespaces missing.
Every endpoint above exists in every tested v6 release; the per-version field differences are recorded in [compatibility.md](compatibility.md).

```python
@dataclass(frozen=True)
class Source:
    name: str                       # e.g. "summary"
    endpoint: str                   # path below /api, may include a query string
    namespaces: tuple[str, ...]     # top-level variable names it provides
    transform: Callable[[Any], dict[str, Any]]   # raw JSON → {namespace: value}
    description: str

@dataclass(frozen=True)
class VariableInfo:
    path: str            # list items documented with N: "top_domains.N.domain"
    type: str            # int | float | str | bool | timestamp | list | list[str] | list[float]
    description: str
    source: str          # Source.name, "derived" or "builtin"

@dataclass(frozen=True)
class FetchProblem:
    source: Source
    error: PiholeError

SOURCES: tuple[Source, ...]
BUILTIN_NAMESPACES: frozenset[str]                 # {"now", "pihello", "pihole"}
NAMESPACES: frozenset[str]                         # all known namespaces incl. builtins

def plan(paths: Iterable[str]) -> tuple[Source, ...]          # minimal sources, in SOURCES order
def plan_all() -> tuple[Source, ...]
def fetch(client: PiholeClient, sources: Iterable[Source], *, max_workers: int = 6
          ) -> tuple[dict[str, Any], list[FetchProblem]]      # AuthenticationError propagates;
                                                              # after a ConnectionFailed, unstarted
                                                              # sources are skipped with that error;
                                                              # results merged in source order
def builtins(*, url: str | None, now: float | None = None) -> dict[str, Any]
def collect(client: PiholeClient, paths: Iterable[str] | None = None, *, now: float | None = None
            ) -> tuple[dict[str, Any], list[FetchProblem]]    # plan + fetch + derived + builtins;
                                                              # raises if every source hit ConnectionFailed
def from_responses(responses: Mapping[str, Any], *, url: str | None = None,
                   now: float | None = None) -> dict[str, Any]  # offline collect() from raw payloads
                                                              # keyed by Source.endpoint
def catalog() -> tuple[VariableInfo, ...]                     # documentation of every variable (cached)
def flatten(variables: Mapping[str, Any]) -> dict[str, Any]   # {"queries.total": 1, "top_domains.0.domain": ...};
                                                              # lists of scalars stay leaves
```

## Pi-hole client (`pihello.client`)

```python
DEFAULT_TIMEOUT = 5.0     # the CLI passes Settings.timeout (default 3)
LOGOUT_TIMEOUT = 2.0
MAX_TIMEOUT = 3600.0
LOGIN_RATE_LIMIT_RETRIES = 2

def normalize_url(url: str) -> str
    # "pi.hole" → "http://pi.hole"; bare IPv6 is bracketed; scheme and host lower-cased;
    # strips trailing "/", "/admin" (and admin pages such as /admin/login), "/api", query
    # and fragment; keeps ports, IPv6 brackets and reverse-proxy sub-paths.
    # ConfigError if empty, not http(s), contains credentials, or has a bad host/port.

class PiholeClient:
    def __init__(self, url: str, password: str | None = None, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 verify: bool | str = True,      # False = no verification, str = CA file or dir
                 user_agent: str | None = None) -> None   # ConfigError: URL, timeout (0 < t ≤ MAX_TIMEOUT), CA path
    base_url: str       # normalised, without /api
    api_url: str        # base_url + "/api"
    sid: str | None
    has_password: bool  # property: a password was given
    passwordless: bool  # property: the last login found a Pi-hole without a password
    def __enter__(self) -> PiholeClient          # login()
    def __exit__(self, *exc) -> None             # logout()
    def login(self) -> None
    def logout(self) -> None                     # never raises; short timeout
    def get(self, endpoint: str, params: Mapping[str, str] | None = None) -> Any
    def probe(self) -> dict[str, Any]            # unauthenticated GET /api/info/login, without "took"
```

- Login: `POST /api/auth {"password": …}`.
  A `200` whose `sid` is `null` means the Pi-hole has no password: proceed unauthenticated (`passwordless`).
  With no password configured, or a session already open, `login()` does nothing; a later `401` raises `AuthenticationError` with an app-password hint.
- Login failures (see compatibility.md for the observed replies):
  - 2FA enabled and no code sent → **`400` `bad_request`** "No 2FA token found in JSON payload" → `TwoFactorRequired` (Pi-hole does *not* answer `401` here); a `401` mentioning 2FA ("Invalid 2FA token") or carrying `session.totp == true` (wrong password on a 2FA Pi-hole) → `TwoFactorRequired`; app passwords bypass 2FA.
  - other `401` → `AuthenticationError`.
  - `429` `api_seats_exceeded` → `TooManySessions`.
  - `429` `rate_limiting`: FTL allows only 3 password checks per wall-clock second, server-wide, and rejects even a correct password beyond that (several shells starting at once can trip it).
    `login()` retries up to `LOGIN_RATE_LIMIT_RETRIES` times after a random 0.35–1.1 s delay, never exceeding `timeout` in total, then raises `AuthenticationError` (hint: enable `cache_ttl`).
    The 2FA variant ("Rate-limiting 2FA token requests", FTL ≥ v6.4) is not retried.
- Authenticated requests send the session id in the `X-FTL-SID` header (never in the URL).
  On a `401` the client logs in again once and retries (session expired, watch mode); with concurrent requests only one thread renews the session.
- `logout()` sends `DELETE /api/auth` (timeout ≤ `LOGOUT_TIMEOUT`) so sessions never leak (Pi-hole has a small `webserver.api.max_sessions` limit, default 16).
- Responses: non-JSON or non-Pi-hole responses → `APIError` (hint: wrong URL or Pi-hole v5); `404` with a Pi-hole error object → `EndpointNotFound` (without one → `APIError`); `502`/`503`/`504` without a Pi-hole error → `ConnectionFailed` (reverse proxy / restart).
  Server-provided text quoted in error messages (Pi-hole's `error.message`/`hint`, a login's `session.message`, a redirect's `Location`, HTTP reason phrases, a proxy's CONNECT reply) goes through `pihello.template.sanitize`, with whitespace collapsed; `APIError.api_message` is cleaned the same way.
  Bodies are read up to 32 MiB.
- Redirects are never followed (Pi-hole's own HTTP→HTTPS redirect points at `webserver.domain` without the port, and following could leak credentials).
  A redirect from `http://` to `https://` raises `ConnectionFailed` suggesting the `https://` URL; other redirects raise `APIError`.
- `timeout` bounds each request's *total* time, not each socket operation: the host-name lookup, TCP connect, waiting for the TLS handshake turn, the handshake, and reading the status line, headers and body all share one deadline, so a server that drips a byte now and then, or stalls TLS handshakes, can't stretch it.
  urllib doesn't bound the lookup; the client resolves names in a helper thread so a dead DNS server can't stall a shell start.
  `_send` puts the deadline (a `time.monotonic()` value) on the urllib `Request` as `request.deadline`; the connection classes set the socket timeout to the time left before each wait, and the response is read through a reader that gives up at the deadline (`ConnectionFailed` "no response from … within Ns").
  With several addresses, each connect attempt gets an equal share of the time left. Connections use the full socket address, so link-local IPv6 URLs with a zone (`http://[fe80::1%eth0]`) work.
  A GET's one retry and a login's rate-limit retries (delays included) stay within the same `timeout`, and error messages quote the configured `timeout`. `timeout` must be > 0 and at most `MAX_TIMEOUT` (3600 s); otherwise `ConfigError`.
  Non-ASCII characters in reverse-proxy sub-paths are percent-encoded; `user:pass@` in a URL is never echoed in error messages.
- Network errors map to `ConnectionFailed` with hints (timeout, DNS, refused, reset — "use https://?"); certificate failures to `TLSError` (hint: `--ca-cert` with `/etc/pihole/tls_ca.crt`, or `--insecure`; a separate hint for hostname mismatches, since Pi-hole's certificate is issued for `webserver.domain`); other TLS handshake failures to `TLSError` ("this port doesn't seem to speak HTTPS").
- TLS: `verify=True` uses the system CA store, a path a CA file (or a directory of hashed certificates).
  For every verifying context (system store included, since users may import Pi-hole's CA there) the client clears `ssl.VERIFY_X509_STRICT` (the default since Python 3.13), because the CA that Pi-hole generates lacks the key usage extension; chain and host name checks stay on.
- TLS handshakes take turns (a process-wide lock in the HTTPS connection's `connect()`; TCP connects and requests stay parallel), because Pi-hole FTL v6.5 fails parallel handshakes and can crash on them (see [Pi-hole compatibility](compatibility.md#tls)).
  Waiting for the turn counts against the request's `timeout` (`acquire(timeout=time left)`), so requests queued behind a stalled handshake time out together instead of one after another.
  A GET whose connection drops before any reply (TLS EOF, reset) is retried once; logins (POST) never are.
- No connection pooling or keep-alive, on purpose: every request uses a new connection, and the fetches run in parallel.
  Since FTL v6.1, Pi-hole's web server leaves Nagle's algorithm on, so every reused request arrives about 40 ms late; `TCP_NODELAY` on the client can't fix that, only `webserver.advancedOpts = ["tcp_nodelay=1"]` on the Pi-hole can (see [Pi-hole compatibility](compatibility.md#keep-alive)).
  Measured against `2026.10.0`: 6 requests took 2 ms over new HTTP connections and 205 ms over one reused connection; over HTTPS, 283 ms and 253 ms.
  pihello can't rely on users having changed that setting, so new connections stay the default.
  TLS session resumption could make HTTPS handshakes cheaper without keep-alive, but it's unmeasured on a real Raspberry Pi and not implemented.
- Proxies from `http_proxy`/`https_proxy`/`no_proxy` are honoured as by urllib, except that loopback hosts always bypass them.
- The client is thread-safe (sources are fetched in parallel).
  `repr()` never includes credentials.
- Debug logging goes through `logging.getLogger("pihello")` (method, path, status, duration — never the password or sid).

## Cache (`pihello.cache`)

Optional (`cache_ttl` seconds, default 0 = off).
Stores the collected variables (never credentials) as JSON in `$XDG_CACHE_HOME/pihello/` (`~/.cache/pihello/`; `%LOCALAPPDATA%\pihello\Cache` on Windows), file mode `0600` (the directory is created `0700` if missing; an existing one is used as-is), written atomically.

```python
FORMAT_VERSION = 1
def cache_dir() -> Path
def make_key(url: str, namespaces: Iterable[str]) -> str   # sha256 of format + URL + namespaces
def path_for(key: str) -> Path                              # ValueError for malformed keys
def load(key: str, ttl: float) -> dict[str, Any] | None     # None: off, missing, expired, from the
                                                            # future, corrupt or another pihello version
def store(key: str, variables: Mapping[str, Any]) -> None   # failures are logged, never raised
def clear() -> int                                          # entries removed
```

The CLI (`_Show.variables`) keys entries by URL + the template's namespaces, stores only when every problem was an `EndpointNotFound`, refreshes the builtins (`now`, `pihole.*`) on a hit and never obtains the password for one.
`--watch` bypasses the cache.

## Demo data (`pihello.demo`)

```python
NOW: float = 1792830600.0          # Saturday 2026-10-24 08:30:00 UTC
URL = "http://pi.hole"
def api_payloads() -> dict[str, Any]   # raw API JSON keyed by Source.endpoint (with "took")
def variables() -> dict[str, Any]      # sources.from_responses(api_payloads(), url=URL, now=NOW)
```

The payloads describe a typical home network (a Raspberry Pi 5, a dozen clients) on the latest Pi-hole release and cover **every** namespace in `sources.catalog()`.
Because they go through the real transforms, the variable tree is shaped exactly like `sources.collect()` output (derived values and builtins included).
Everything is deterministic and relative to `NOW`; render with `RenderOptions(now=demo.NOW)` for reproducible screenshots and tests.
`tests/helpers/fakepihole.py` serves the same payloads as the `"demo"` fixture.

## Presets and themes

```python
# pihello.presets
@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    source: str            # template text (loaded from presets/<name>.txt)
    min_width: int         # widest line rendered from demo data (tests verify it)
def names() -> tuple[str, ...]          # "default" first, then alphabetical
def available() -> tuple[Preset, ...]   # same order
def get(name: str) -> Preset            # case-insensitive; ConfigError with did-you-mean
```

Preset files are read with plain file reads (not `importlib.resources`, which costs milliseconds at every shell start) on first use and cached.

```python
# pihello.themes
STYLE_NAMES = ("title", "subtitle", "border", "label", "value", "muted",
               "accent", "good", "warn", "bad", "info", "highlight")
@dataclass(frozen=True)
class Theme:
    name: str
    description: str
    styles: Mapping[str, str]     # read-only; every STYLE_NAMES key → style-tag words
def available() -> tuple[Theme, ...]   # "default" first, then alphabetical
def get(name: str) -> Theme            # case-insensitive; ConfigError with did-you-mean
def resolve(name: str, overrides: Mapping[str, str] | None = None) -> dict[str, str]
```

`resolve()` returns the theme's styles with the `[styles]` overrides applied.
Override keys must be `STYLE_NAMES`; values are style words without `[ ]`/`{ }` and may use `@name` to refer to the **base** theme's style (expanded here, so the result only contains concrete words).
Values are validated with the template's style parser; every problem is a `ConfigError`.

Presets use the semantic `@name` styles so every preset works with every theme (except `classic`, the exact pihello 1.x look, which uses fixed colors).

## Configuration (`pihello.config`)

Precedence (highest first): CLI flags → environment → selected profile → top-level config → defaults.
Two groups are resolved **as a unit** (the first layer that sets any key of the group wins the whole group): the credential (`password` | `password_file` | `password_command`, plus `--password-stdin`) and the template (`preset` | `template`, plus `--template-string`).
Within one layer at most one key of each group may be set.
Styles are merged instead: `[styles]` < `[profiles.NAME.styles]` < `--style`.

Config file: `--config`, else `$PIHELLO_CONFIG` (both must exist), else `$XDG_CONFIG_HOME/pihello/config.toml` (`~/.config/pihello/config.toml`; `%APPDATA%\pihello\config.toml` on Windows; a missing default file is fine).
Relative `password_file`, `template` and `verify_tls` paths in the file are relative to its directory; on the command line and in the environment they are relative to the current directory.

```toml
url = "http://pi.hole"
password = "app-password"             # or exactly one of:
# password_file = "~/.config/pihello/password"
# password_command = "pass show pihole"            # or ["pass", "show", "pihole"]
verify_tls = true                     # true | false | "/path/to/ca.pem"
timeout = 3.0                         # > 0, at most 3600 (MAX_TIMEOUT)
preset = "default"                    # or: template = "~/.config/pihello/greeting.txt"
theme = "default"
color = "auto"                        # auto | always | never | 16 | 256 | truecolor
width = "auto"                        # "auto" (terminal width) | 0 (unlimited) | N
height = 0
overflow = "wrap"                     # wrap | clip
tab_size = 4
missing = "?"
strict = false
cache_ttl = 0
timestamp = false                     # false | true | "%H:%M"

[styles]                              # override theme styles
accent = "bold #ff8800"

[profiles.backup]                     # `pihello -P backup` / PIHELLO_PROFILE=backup
url = "https://pihole2.lan"
password_command = "pass show pihole2"

[profiles.backup.styles]
title = "bold red"
```

Environment: `PIHELLO_CONFIG`, `PIHELLO_PROFILE`, `PIHELLO_URL`, `PIHELLO_PASSWORD`, `PIHELLO_PASSWORD_FILE`, `PIHELLO_PRESET`, `PIHELLO_THEME` (empty values are ignored), plus `NO_COLOR`/`FORCE_COLOR`.
A config file that contains `password` (top level or a profile) and is readable by group/others triggers a warning (POSIX only).

```python
KEYS: dict[str, KeySpec]          # every key: validation function + description
DEFAULTS: dict[str, Any]
ENV_VARS: dict[str, str]          # environment variable → key
PASSWORD_COMMAND_TIMEOUT = 30.0

def default_config_path(env: Mapping[str, str] | None = None) -> Path   # ignores PIHELLO_CONFIG
def find_config_path(explicit: str | os.PathLike[str] | None = None,
                     env: Mapping[str, str] | None = None) -> tuple[Path, str]
    # origin: "--config" | "PIHELLO_CONFIG" | "default"
def load_file(path: str | os.PathLike[str], *, required: bool = False) -> ConfigFile
    # ConfigError: unreadable, invalid TOML/UTF-8, unknown key (did-you-mean), bad value,
    # conflicting keys; a missing file → ConfigFile(exists=False) unless required
def resolve(cli: Mapping[str, Any] | None = None, *, env: Mapping[str, str] | None = None,
            config_path: str | os.PathLike[str] | None = None,
            profile: str | None = None) -> Settings
    # cli: keys like the config file (None = not given) plus password_stdin, template_string,
    # styles and _flags (key → flag spelling for messages); profile falls back to PIHELLO_PROFILE
def read_password_file(path) -> str                  # first line, without the line ending
def run_password_command(command: str | Sequence[str], *, timeout: float = 30.0) -> str
def dumps(data: Mapping[str, Any], *, header: str | None = None) -> str   # minimal TOML writer
def write_private(path, text: str) -> None           # atomic, mode 0600, parents 0700

@dataclass(frozen=True)
class ConfigFile:      # path, exists, settings, styles, profiles, profile_styles, mode
    contains_password: bool       # property
    insecure_permissions: bool    # property: mode & 0o044
    def permission_warning(self) -> str | None

@dataclass(frozen=True)
class Credential:      # kind: "password" | "file" | "command" | "stdin"; value hidden from repr
    origin: str
    def describe(self) -> str     # never reveals the password; a command only by its program
    def obtain(self, *, stdin: TextIO | None = None) -> str

@dataclass(frozen=True)
class TemplateChoice:  # kind: "preset" | "file" | "string"; value; origin
    def resolved_path(self) -> Path
    def describe(self) -> str

@dataclass(frozen=True)
class Settings:
    url: str | None; credential: Credential | None; verify_tls: bool | str; timeout: float
    template: TemplateChoice; theme: str; color: str; width: int | Literal["auto"]
    height: int; overflow: Literal["wrap", "clip"]; tab_size: int; missing: str
    strict: bool; cache_ttl: float; timestamp: bool | str; styles: dict[str, str]
    profile: str | None; config: ConfigFile; config_origin: str
    origins: dict[str, str]       # key → "command line (--url)", "config file (…)", "default", …
    warnings: tuple[str, ...]
    def password(self, *, stdin: TextIO | None = None) -> str | None   # obtained on demand
    verify: bool | str            # property: verify_tls with "~" expanded
```

`run_password_command`: a list is executed directly; a string is split with `shlex` and executed directly unless it contains shell syntax (`| & ; < > ( ) $ \` * ?
[ ] { } ~ ! #` or a newline), then it runs through `/bin/sh -c` (`cmd /c` on Windows).
On Windows a string without shell syntax goes to `CreateProcess` unchanged, which splits it by the Windows rules (`shlex` would keep the quotes of `"C:\Program Files\…\bw.exe"` in the words, and `subprocess` would quote them again).
The command inherits stdin (pinentry works), stdout/stderr are captured, it is killed after the timeout, and the first line of stdout (stripped) is the password; a non-zero exit is a `ConfigError` quoting the last stderr line.
Its messages and `Credential.describe()` name only the program (`password_command (bw …)`), never the arguments, which may hold a token.

## Command line (`pihello.cli`)

`main(argv: Sequence[str] | None = None) -> int`.
When the first argument is not a known subcommand, `show` is implied, so `pihello` alone renders the greeting from config; options before the command name (`pihello --config x doctor`, `pihello -q vars queries`) are re-dispatched to that command, so a command name is never taken as the 1.x ADDR (and looked up as a host) unless it comes after `--`.
`build_parser(command="show")` returns a fresh parser per command; `COMMANDS` maps command names to their one-line descriptions (also used by the completion scripts).

| Command | Description |
| :-- | :-- |
| `pihello [show]` | render the greeting (default) |
| `pihello init` | interactive setup wizard (writes config, tests connection, prints shell snippet) |
| `pihello presets [NAME]` | list presets / preview them with demo (or `--live`) data; `--source` prints one, `-o FILE` writes it (UTF-8, creates folders, never overwrites) |
| `pihello themes [NAME]` | list themes with swatches and previews |
| `pihello vars [--demo] [--json] [--all] [PATTERN]` | list variables with live (or demo) values |
| `pihello doctor` | diagnose config, connectivity, TLS, auth, versions, sources, sessions |
| `pihello colors` | print the named color palette |
| `pihello completion {bash,zsh,fish}` | print a shell completion script (hidden `--names KIND` lists names for dynamic completion) |

pihello 1.x invocations keep working: `pihello ADDR [PASSWORD] [-f FILE] [-p] [-k] [-ts [FMT]] [-W N] [-H N] [-c] [-i N]`.
A positional password prints a warning (it leaks into shell history and `ps`), suppressed by `-q`.
With ADDR and no configured template, the `classic` preset (the exact 1.x look) is used.
`-p` normalises ADDR, `--url` or the configured URL first and then sets its scheme to `https`, so no spelling of `http://` (`HTTP://`, leading spaces) gets the password sent unencrypted.

Notes:

- Imports are lazy: the show path never imports the wizard, doctor or completion code, and `--demo` never imports the HTTP client.
  The password is only obtained when a request is about to be made.
- `UI` writes pihello's own messages to stderr and honours `NO_COLOR` and `-q`; with `-q`, errors with exit status 2 (`ConfigError`, `TemplateError`: mistakes in the command line, config or template) are still printed, warnings and errors with exit status 3–5 aren't.
  `UI.clean()` redacts every registered secret (≥ 4 characters) and then removes terminal controls (escape sequences, other C0 and C1 controls, bidi controls, via `pihello.template.sanitize`) from every message and `--debug` log line, because messages quote Pi-hole data.
- `vars` prints string values JSON-quoted with those controls escaped; `vars --json` escapes C1 and bidi controls as `\uXXXX` too (JSON itself only escapes C0).
- A fetch problem leaves its namespaces missing (logged with `--debug`).
  An `APIError` (including `EndpointNotFound`, or `500 internal_error` while FTL starts) means the Pi-hole answered: it never ends the run, even when its source was the only one.
  Only when *every* planned source failed with `ConnectionFailed` is the first error raised; an `AuthenticationError` always is.
  With `strict`, any fetch problem is an error.
- `--watch` uses one client and session, redraws on SIGWINCH, uses the alternate screen on a TTY, keeps the last data on connection errors and stops on `AuthenticationError` (other than `TooManySessions`).

## Setup wizard (`pihello.wizard`)

```python
def run(args: argparse.Namespace, ui: UI, *, stdin: TextIO | None = None,
        stdout: TextIO | None = None, getpass: Callable[[str], str] | None = None,
        env: Mapping[str, str] | None = None) -> int
def shell_snippet(shell: str, *, command: str = "pihello -q",
                  env: Mapping[str, str] | None = None) -> ShellSnippet
    # bash ~/.bashrc, zsh $ZDOTDIR/.zshrc, fish conf.d/pihello.fish (or a line for an
    # existing functions/fish_greeting.fish), powershell $PROFILE
def detect_shell(env: Mapping[str, str] | None = None) -> str | None
def append_snippet(snippet: ShellSnippet) -> bool     # no-op if the file mentions pihello
class Prompter: ...         # all prompts; injectable streams and getpass
class SetupCancelled(ConfigError): ...
```

Five steps: address (probe, HTTPS offer, certificate handling), password (`--store` config | file | command | env | none; up to three attempts), preset, theme (both with previews of live or demo data), save.
Nothing is written before the last step; an existing config needs confirmation (or `--force`) and is kept as `config.toml.bak`; files are written with `config.write_private`.
`--yes` takes every answer from the options.
`--color` only affects the wizard's own output and is not saved.

## Diagnostics (`pihello.doctor`)

`run(args, ui, *, stdout=None, env=None) -> int` runs `Doctor`'s checks in order: Python, config file (location, parse, permissions) and the effective settings with their origins, URL, DNS (with timeout), TCP, the unauthenticated API probe (TLS, HTTPS availability), password and login, versions and updates (`SUPPORTED_MAJOR = 6`), every data source, API sessions (lists them, warns about other pihello sessions, logs out and logs in again to verify the session is gone).
Each check prints one line (`✓`, `!`, `✗`, `·`, or ASCII when stdout can't encode them) plus a hint.
A failure that makes later checks pointless stops the run.
Exit status: 0 with only warnings, else the exit code of the first failure.
Every line goes through `UI.clean()`; URLs are shown without user info (`http://***@pi.hole`) and a `password_command` only by its program, so the output can be shared.
The update line follows `updates.status` (`available`, `current`, `unknown`), like the template variables.

## Shell completion (`pihello.completion`)

```python
SHELLS = ("bash", "zsh", "fish")
def script(shell: str) -> str       # generated from the real argparse parsers
def names(kind: str) -> list[str]   # "commands" | "presets" | "themes" | "profiles"; never raises
```

The scripts complete subcommands, options, choices and file paths, and call `pihello completion --names KIND` at completion time for preset, theme and profile names.

## Testing

- `tests/test_*.py` — fast unit tests, one or more files per module (default `pytest` run; no network, no containers), including Hypothesis property tests for the template engine (`test_template_properties.py`), fixture sanity checks (`test_fixtures.py`) and packaging checks that build the sdist and wheel (`test_packaging.py`).
  CI requires 90 % coverage.
- `tests/conftest.py` — every test runs with an isolated `HOME`/XDG directories and a scrubbed environment (`PIHELLO_*`, color and terminal variables); provides the `home`, `fake_pihole` and `fake_pihole_factory` fixtures.
- `tests/fixtures/pihole/<docker-tag>/responses.json` — real API responses recorded from every Pi-hole v6 FTL minor release by `scripts/record_fixtures.py` (index: `tests/fixtures/pihole/index.json`).
- `tests/helpers/fakepihole.py` — a threaded stdlib HTTP(S) server that replays a fixture set (or the `"demo"` payloads) and emulates auth, sessions, TOTP, seat limits and faults: `FakePihole(fixture="latest", *, password="pihello-test", totp=False, max_sessions=16, https=False)`.
- `tests/helpers/containers.py` — starts real Pi-hole containers (podman or docker); `tests/helpers/dnsgen.py` — a tiny DNS client that generates query traffic.
  Both are used by the integration tests and the fixture recorder.
- `tests/integration/` — `pytest -m integration`: starts real Pi-hole containers for each supported version and exercises the CLI end to end.
- `scripts/render_screenshots.py` (galleries and SVGs) and `scripts/gen_docs.py` (filter and variable reference) regenerate docs; both have a `--check` mode that CI runs.
