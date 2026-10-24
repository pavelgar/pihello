# Configuration

pihello reads its settings from a [TOML](https://toml.io) file, environment
variables and the command line. `pihello init` writes the file for you;
`pihello doctor` shows every effective setting and where it comes from.

**Contents:** [The config file](#the-config-file) · [Settings](#settings) ·
[Precedence](#precedence) · [Environment variables](#environment-variables) ·
[Passwords](#passwords) · [Profiles](#profiles) · [Styles](#styles) ·
[HTTPS and certificates](#https-and-certificates) · [Caching](#caching) ·
[Complete example](#complete-example) · [Command-line options](#command-line-options) ·
[Exit status](#exit-status)

## The config file

| System | Location |
| :-- | :-- |
| Linux, macOS, BSD | `$XDG_CONFIG_HOME/pihello/config.toml`, by default `~/.config/pihello/config.toml` |
| Windows | `%APPDATA%\pihello\config.toml` |

`--config PATH` or the `PIHELLO_CONFIG` environment variable point pihello at
another file (which then has to exist). Without a file pihello still works
with command-line options and environment variables.

A minimal file:

```toml
url = "http://pi.hole"
password_file = "~/.config/pihello/password"
preset = "default"
theme = "nord"
```

Strings need quotes. Unknown keys and invalid values are errors with a hint,
so typos don't go unnoticed. Relative paths in the file
(`password_file`, `template`, a CA path in `verify_tls`) are relative to the
directory of the config file; `~` is expanded.

## Settings

Every key can be used at the top level and in a [profile](#profiles) (a profile's
styles go in `[profiles.NAME.styles]`).

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `url` | string | none | Pi-hole address: `pi.hole`, `192.168.1.2:8080`, `https://pi.hole`, `[fd00::2]`. `http://` is assumed; a trailing `/admin` or `/api` is removed; other paths (reverse proxies) are kept. |
| `password` | string | none | The [app password](#passwords), in plain text. |
| `password_file` | string | none | File whose first line is the password. |
| `password_command` | string or list | none | Command that prints the password, e.g. `"pass show pihole"` or `["pass", "show", "pihole"]`. |
| `verify_tls` | bool or string | `true` | Check the HTTPS certificate: `true` (system CA store), `false` (don't), or the path of a CA certificate (PEM file). See [HTTPS](#https-and-certificates). |
| `timeout` | number | `3` | Network timeout per request, in seconds. |
| `preset` | string | `"default"` | Built-in template; see `pihello presets` and the [gallery](presets.md). |
| `template` | string | none | Path of your own [template](templates.md) file. Use either `preset` or `template`. |
| `theme` | string | `"default"` | Color theme; see `pihello themes` and the [gallery](themes.md). |
| `color` | string | `"auto"` | `"auto"`, `"always"`, `"never"`, `"16"`, `"256"` or `"truecolor"` (`16` and `256` may be numbers). See [Colors](templates.md#colors). |
| `width` | `"auto"` or integer | `"auto"` | Wrap (or clip) lines at this many columns. `"auto"` is the terminal width (no limit when the output isn't a terminal); `0` means no limit. |
| `height` | integer | `0` | Print at most this many lines; `0` means no limit. |
| `overflow` | string | `"wrap"` | What to do with lines that are too long: `"wrap"` or `"clip"`. |
| `tab_size` | integer | `4` | Tab stop width for tabs in templates, 0–32 (`0` removes tabs). |
| `missing` | string | `"?"` | Shown for [unavailable values](templates.md#missing-values). |
| `strict` | bool | `false` | Fail on unavailable values instead of showing `missing`. |
| `cache_ttl` | number | `0` | Reuse fetched data for this many seconds; `0` turns the [cache](#caching) off. |
| `timestamp` | bool or string | `false` | Print the time as the first line: `true` (`2026-10-24 08:30:00`) or a [strftime](https://docs.python.org/3/library/time.html#time.strftime) format such as `"%H:%M %a"`. |
| `[styles]` | table | none | Change single [theme styles](#styles). |
| `[profiles.NAME]` | table | none | Settings for another Pi-hole or another look, see [Profiles](#profiles). |

## Precedence

When a setting is given in several places, the first one wins:

1. command-line options
2. environment variables
3. the selected profile (`-P NAME` / `PIHELLO_PROFILE`)
4. the top level of the config file
5. the defaults above

Two groups of settings are taken as a whole from the first place that sets
any of them, so they never mix:

- **the password**: `password`, `password_file`, `password_command` (and
  `--password-stdin`). `PIHELLO_PASSWORD` therefore replaces a
  `password_command` from the file.
- **the template**: `preset`, `template` (and `-e`). `--preset compact` or
  `PIHELLO_PRESET=compact` replaces a `template` from the file.

Within one place (one table, the environment, the command line) only one
setting of each group may be used.

## Environment variables

| Variable | Meaning |
| :-- | :-- |
| `PIHELLO_CONFIG` | Config file to use (instead of the default location). |
| `PIHELLO_PROFILE` | Profile to use, like `-P`. |
| `PIHELLO_URL` | Same as `url`. |
| `PIHELLO_PASSWORD` | Same as `password`. |
| `PIHELLO_PASSWORD_FILE` | Same as `password_file` (a relative path is relative to the current directory). |
| `PIHELLO_PRESET` | Same as `preset`. |
| `PIHELLO_THEME` | Same as `theme`. |
| `NO_COLOR` | Any non-empty value turns colors off (with `color = "auto"`). |
| `FORCE_COLOR`, `CLICOLOR_FORCE` | Turn colors on even when the output isn't a terminal (with `color = "auto"`). |
| `COLUMNS`, `LINES` | Override the detected terminal size (when the output is a terminal). |
| `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` | Base directories of the config file and the cache (`APPDATA` / `LOCALAPPDATA` on Windows). |
| `http_proxy`, `https_proxy`, `no_proxy` | Proxies, as usual for Python; `localhost` and loopback addresses never use a proxy. |

Empty variables are ignored. Only one of `PIHELLO_PASSWORD` and
`PIHELLO_PASSWORD_FILE` may be set.

## Passwords

pihello logs in to the Pi-hole v6 API with a password. Use an **app
password**: in the Pi-hole web interface go to *Settings → Web interface /
API*, click *Configure app password*, and save it. An app password also works
when two-factor authentication is on (pihello can't type TOTP codes), and
Pi-hole doesn't let it change settings unless you enable
`webserver.api.app_sudo`. Your normal web interface password works too if 2FA
is off. A Pi-hole without any password needs none.

There are four ways to give pihello the password. `pihello init` sets up
any of them.

| Where | How | Notes |
| :-- | :-- | :-- |
| config file | `password = "…"` | Simple. Keep the file private (`chmod 600`). |
| password file | `password_file = "~/.config/pihello/password"`, `--password-file PATH`, `PIHELLO_PASSWORD_FILE` | The first line of the file is the password (line ending removed). |
| password manager | `password_command = "pass show pihole"`, `--password-command CMD` | The first line the command prints, without surrounding spaces. |
| environment | `PIHELLO_PASSWORD` | Set it from wherever you keep secrets. |

`--password-stdin` reads the first line of standard input instead
(`pass show pihole | pihello --password-stdin`); it can't be combined with
`-f -`. The password is only obtained when pihello actually talks to the
Pi-hole, so a [cache](#caching) hit or `--demo` never runs your password
manager.

**How `password_command` runs.** A list (`["pass", "show", "pihole"]`) is run
directly. A string is split into words like a shell would and also run
directly, *unless* it contains shell syntax (`|`, `&`, `;`, `<`, `>`, `(`, `)`,
`$`, `` ` ``, `*`, `?`, `[`, `]`, `{`, `}`, `~`, `!`, `#` or a newline): then
it runs through `/bin/sh -c` (`cmd /c` on Windows), so pipes and variables
work:

```toml
# one of:
password_command = ["secret-tool", "lookup", "service", "pihole"]   # run directly
# password_command = "pass show pihole"                             # run directly
# password_command = "pass show pihole | head -n 1"                 # through /bin/sh
```

The command must exit with status 0 and is stopped after 30 seconds. Its
output is captured, but it can still ask on the terminal itself (like GPG's
pinentry does for a passphrase).

**Security notes**

- `pihello init` writes the config file and the password file with mode
  `600` (only you can read them).
- If the config file contains a `password` (at the top level or in a profile)
  and other users can read it, pihello prints a warning with the `chmod`
  command that fixes it; `pihello doctor` reports it too.
- The password never appears in pihello's output, error messages or
  `--debug` logs. It is sent only in the login request; the session id
  travels in a request header, and every session is logged out again.
- A password given as a command-line argument (`pihello ADDR PASSWORD`, the
  1.x style) still works but prints a warning: it ends up in your shell
  history and is visible in `ps`. Move it into one of the places above.
- Passwords inside the URL (`http://user:pw@pi.hole`) are rejected.

## Profiles

Profiles are named sets of settings in the same file, for a second Pi-hole or
just a different look. A profile can contain every key from
[Settings](#settings), plus its own `styles` table:

```toml
url = "http://pi.hole"
password_file = "~/.config/pihello/password"
theme = "nord"

[profiles.backup]
url = "https://pihole2.lan"
password_command = ["pass", "show", "pihole2"]
verify_tls = "pihole2-ca.crt"           # next to config.toml

[profiles.backup.styles]
title = "bold #ff8800"

[profiles.tmux]
preset = "status"
color = "never"
```

Select a profile with `-P NAME` (`--profile NAME`) or `PIHELLO_PROFILE=NAME`:

```sh
pihello -P backup
pihello -P tmux
```

A profile's settings override the top-level ones; everything else comes from
the top level (`-P tmux` above still uses the top-level `url` and password).
An unknown profile name is an error that lists the defined ones.

## Styles

The `[styles]` table changes single styles of the selected theme, without
writing a whole theme. Keys are the twelve style names (`title`, `subtitle`,
`border`, `label`, `value`, `muted`, `accent`, `good`, `warn`, `bad`, `info`,
`highlight`, see [themes.md](themes.md#style-names)); values are the words you
would write inside a [style tag](templates.md#style-tags), without the
brackets:

```toml
theme = "nord"

[styles]
accent = "bold #ff8800"
highlight = "@bad underline"   # the theme's own "bad" style, underlined
```

`@name` refers to the *theme's* style of that name (before your overrides).
Styles from `[styles]`, `[profiles.NAME.styles]` and `--style NAME=WORDS`
are combined; for the same name the command line wins over the profile, and
the profile over the top-level table. Invalid names and words are errors.

## HTTPS and certificates

Pi-hole v6 serves its web interface and API over HTTPS too (port 443),
with a self-signed certificate it creates on first start. pihello verifies
certificates by default, so you have three options for `https://` URLs:

1. **Trust Pi-hole's certificate authority** (recommended). Copy
   `/etc/pihole/tls_ca.crt` from the Pi-hole and point pihello at it:

   ```toml
   url = "https://pi.hole"
   verify_tls = "tls_ca.crt"   # relative to the config directory
   ```

   or `--ca-cert ~/tls_ca.crt` for a one-off. Pi-hole's certificate is
   issued for the name in `webserver.domain`, `pi.hole` by default, so the
   URL has to use that name (add it to `/etc/hosts` if your DNS doesn't know
   it). Connecting by IP address fails with a certificate mismatch error.
2. **Use your own certificate** issued by a CA your system trusts: nothing to
   configure.
3. **Skip verification** with `-k` / `--insecure` or `verify_tls = false`.
   The connection is still encrypted, but not protected against
   impersonation; `pihello doctor` warns about it.

Python 3.13 and newer reject Pi-hole's generated CA by default (its strict
X.509 mode wants a key usage extension the CA doesn't have). pihello turns
that strict mode off; the certificate chain and the host name are still
verified.

pihello never follows redirects, so the password can't be sent to another
host by accident. If your Pi-hole redirects HTTP to HTTPS, pihello says so and
suggests the `https://` URL.

## Caching

A shell greeting runs every time you open a terminal. With a cache, pihello
reuses recent data instead of asking the Pi-hole each time:

```toml
cache_ttl = 60   # seconds
```

or `--cache 60` on the command line (`--no-cache` turns it off for one run).

- Location: `$XDG_CACHE_HOME/pihello/` (by default `~/.cache/pihello/`),
  `%LOCALAPPDATA%\pihello\Cache` on Windows. Files are private (mode `600`).
- Only the fetched values are stored, never the password or a session id. A
  cache hit doesn't log in at all.
- There is one entry per Pi-hole URL and set of data a template needs, so
  different presets don't overwrite each other's entries.
- Nothing is cached when an endpoint failed, and entries written by another
  pihello version are ignored.
- `--watch` always fetches fresh data.
- To clear the cache, delete the directory.

## Complete example

Every setting (with its default, where there is one), a style override and a
profile:

```toml
# ~/.config/pihello/config.toml

# Connection
url = "http://pi.hole"
password_file = "~/.config/pihello/password"   # or: password = "…" / password_command = "…"
verify_tls = true                              # true | false | "path/to/ca.crt"
timeout = 3

# Look
preset = "default"                             # or: template = "~/.config/pihello/greeting.txt"
theme = "default"
color = "auto"                                 # auto | always | never | 16 | 256 | truecolor

# Layout
width = "auto"                                 # "auto" | 0 (no limit) | columns
height = 0
overflow = "wrap"                              # wrap | clip
tab_size = 4

# Values
missing = "?"
strict = false
timestamp = false                              # false | true | "%H:%M"

# Behaviour
cache_ttl = 0

[styles]
accent = "bold #ff8800"

[profiles.backup]
url = "https://pihole2.lan"
password_command = ["pass", "show", "pihole2"]
verify_tls = "pihole2-ca.crt"
```

## Command-line options

Options of `pihello` / `pihello show`, as grouped in `pihello --help`. The
other commands are described in [usage.md](usage.md). Paths given on the
command line are relative to the current directory.

**Connection**

| Option | Setting | Description |
| :-- | :-- | :-- |
| `-u`, `--url URL` | `url` | Pi-hole address. |
| `--password-file PATH` | `password_file` | Read the password from the first line of PATH. |
| `--password-command CMD` | `password_command` | Run CMD and use the first line it prints. |
| `--password-stdin` | | Read the password from standard input. |
| `-k`, `--insecure` | `verify_tls = false` | Don't verify the HTTPS certificate. |
| `--ca-cert PATH` | `verify_tls = "PATH"` | Verify HTTPS against this CA or self-signed certificate. |
| `--timeout SECONDS` | `timeout` | Network timeout. |
| `-P`, `--profile NAME` | `PIHELLO_PROFILE` | Use `[profiles.NAME]`. |
| `--config PATH` | `PIHELLO_CONFIG` | Config file to read. |

**Template**

| Option | Setting | Description |
| :-- | :-- | :-- |
| `--preset NAME` | `preset` | Built-in template. |
| `-f`, `--template`, `--file FILE` | `template` | Template file; `-` reads standard input. |
| `-e`, `--template-string TEXT` | | Template given inline. |
| `-T`, `--theme NAME` | `theme` | Color theme. |
| `--style NAME=WORDS` | `[styles]` | Override one theme style; repeat for more. |
| `--demo` | | Use built-in demo data instead of a Pi-hole. |

**Output**

| Option | Setting | Description |
| :-- | :-- | :-- |
| `--color WHEN` | `color` | `auto`, `always`, `never`, `16`, `256` or `truecolor`. |
| `--no-color` | `color = "never"` | No colors. |
| `-W`, `--width N` | `width` | `auto`, `0` (no limit) or columns. |
| `-H`, `--height N` | `height` | At most N lines. |
| `-c`, `--clip` / `--wrap` | `overflow` | Cut long lines / wrap them. |
| `-i`, `--tab-size`, `--indent N` | `tab_size` | Tab stop width, 0–32. |
| `--missing TEXT` | `missing` | Text for unavailable values. |
| `--strict` / `--no-strict` | `strict` | Fail on unavailable values / don't. |
| `-ts`, `--timestamp [FMT]` | `timestamp` | Print the time first, optionally in strftime format FMT. |

**Behaviour**

| Option | Setting | Description |
| :-- | :-- | :-- |
| `--cache SECONDS` | `cache_ttl` | Reuse data fetched less than SECONDS ago. |
| `--no-cache` | `cache_ttl = 0` | Always fetch fresh data. |
| `--watch [SECONDS]` | | Live view, refreshed every SECONDS (default 5). |
| `-q`, `--quiet` | | Print no errors or warnings; only the exit status tells. |
| `--debug` | | Log HTTP requests, timings and problems to stderr. |
| `-h`, `--help` | | Show the help. |
| `-v`, `-V`, `--version` | | Show the version. |

**pihello 1.x compatibility** (see [upgrading.md](upgrading.md))

| Option | Setting | Description |
| :-- | :-- | :-- |
| `ADDR` | `url` | Host or `host:port`, as the first argument. On its own (without PASSWORD) it must contain a `.` or `:` or be `localhost`, so a mistyped command isn't looked up as a host; use `-u NAME` for single-label names. |
| `PASSWORD` | `password` | Second argument. Deprecated: it leaks into shell history and `ps`. |
| `-p`, `--https`, `--proto` | | Use `https://` for ADDR, `--url` or the configured URL. |

## Exit status

| Code | Meaning |
| --: | :-- |
| 0 | Success. |
| 1 | Internal error: a bug in pihello. Please [report it](https://github.com/pavelgar/pihello/issues), with the output of `--debug`. |
| 2 | Usage, configuration or template error. |
| 3 | The Pi-hole can't be reached: name resolution, connection refused, timeout, TLS. |
| 4 | Authentication failed: wrong password, two-factor authentication, no free API sessions, login rate limit. |
| 5 | Unexpected answer from the Pi-hole API. |
| 130 | Interrupted with Ctrl-C (`--watch` exits with 0). |
| 129, 143 | Ended by SIGHUP (terminal closed) or SIGTERM; pihello still logs out first. |

With `-q` runtime errors and warnings aren't printed (mistakes on the command
line still are), but the exit status is the same.
`pihello doctor` exits with the code of the first failed check.
