# Configuration

pihello reads its settings from a [TOML](https://toml.io) file, environment variables and the command line.
`pihello init` writes the file for you, and `pihello doctor` shows every effective setting and where it comes from.
The command-line options are listed with [`pihello show`](commands.md#show-options).

**On this page:** [Config file](#config-file) · [Precedence](#precedence) · [Settings](#settings) ·
[Environment variables](#environment-variables) · [Passwords](#passwords) · [Profiles](#profiles) · [Styles](#styles) ·
[HTTPS and certificates](#https-and-certificates) · [Caching](#caching) · [Complete example](#complete-example)

## Config file

| System | Path |
| :-- | :-- |
| Linux, macOS, BSD | `$XDG_CONFIG_HOME/pihello/config.toml`, by default `~/.config/pihello/config.toml` |
| Windows | `%APPDATA%\pihello\config.toml` |

`--config PATH` or the `PIHELLO_CONFIG` environment variable point pihello at another file, which then has to exist.
Without a file, pihello still works with command-line options and environment variables.

A minimal file:

```toml
# ~/.config/pihello/config.toml
url = "http://pi.hole"
password_file = "~/.config/pihello/password"
preset = "default"
theme = "nord"
```

- Strings need quotes.
- Unknown keys and invalid values are errors with a hint, so typos don't go unnoticed.
- Relative paths in the file (`password_file`, `template`, a CA path in `verify_tls`) are relative to the directory of the config file. `~` is expanded.

## Precedence

When a setting is given in several places, the first one wins:

1. command-line options
2. environment variables
3. the selected profile (`-P NAME` / `PIHELLO_PROFILE`)
4. the top level of the config file
5. the defaults in [Settings](#settings)

Two groups of settings are taken as a whole from the first place that sets any of them, so they never mix:

- **The password**: `password`, `password_file`, `password_command` (and `--password-stdin`).
  `PIHELLO_PASSWORD` therefore replaces a `password_command` from the file.
- **The template**: `preset`, `template` (and `-e`).
  `--preset compact` or `PIHELLO_PRESET=compact` replaces a `template` from the file.

Within one place (one table, the environment, the command line), only one setting of each group may be used.

## Settings

Every key can be used at the top level and in a [profile](#profiles); a profile's styles go in `[profiles.NAME.styles]`.
Most keys also have a [command-line option](commands.md#show-options).

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `url` | string | none | Pi-hole address: `pi.hole`, `192.168.1.2:8080`, `https://pi.hole`, `[fd00::2]`. `http://` is assumed; a trailing `/admin` or `/api` is removed; other paths (reverse proxies) are kept. |
| `password` | string | none | The [app password](#passwords), in plain text |
| `password_file` | string | none | File whose first line is the password |
| `password_command` | string or list | none | Command that prints the password, e.g. `"pass show pihole"` or `["pass", "show", "pihole"]` |
| `verify_tls` | bool or string | `true` | Check the HTTPS certificate: `true` (system CA store), `false` (don't), or the path of a CA certificate (PEM file). See [HTTPS and certificates](#https-and-certificates). |
| `timeout` | number | `3` | Network timeout per request, in seconds |
| `preset` | string | `"default"` | Built-in template; see `pihello presets` and the [preset gallery](presets.md) |
| `template` | string | none | Path of your own [template](templates.md) file. Use either `preset` or `template`. |
| `theme` | string | `"default"` | Color theme; see `pihello themes` and the [theme gallery](themes.md) |
| `color` | string | `"auto"` | `"auto"`, `"always"`, `"never"`, `"16"`, `"256"` or `"truecolor"` (`16` and `256` may be numbers). See [Colors](templates.md#colors). |
| `width` | `"auto"` or integer | `"auto"` | Wrap (or clip) lines at this many columns. `"auto"` is the terminal width (no limit when the output isn't a terminal); `0` means no limit. |
| `height` | integer | `0` | Print at most this many lines; `0` means no limit |
| `overflow` | string | `"wrap"` | What to do with lines that are too long: `"wrap"` or `"clip"` |
| `tab_size` | integer | `4` | Tab stop width for tabs in templates, 0–32 (`0` removes tabs) |
| `missing` | string | `"?"` | Shown for [unavailable values](templates.md#missing-values) |
| `strict` | bool | `false` | Fail on unavailable values instead of showing `missing` |
| `cache_ttl` | number | `0` | Reuse fetched data for this many seconds; `0` turns the [cache](#caching) off |
| `timestamp` | bool or string | `false` | Print the time as the first line: `true` (`2026-10-24 08:30:00`) or a [strftime](https://docs.python.org/3/library/time.html#time.strftime) format such as `"%H:%M %a"` |
| `[styles]` | table | none | Change single [theme styles](#styles) |
| `[profiles.NAME]` | table | none | Settings for another Pi-hole or another look; see [Profiles](#profiles) |

## Environment variables

| Variable | Description |
| :-- | :-- |
| `PIHELLO_CONFIG` | Config file to use instead of the default location |
| `PIHELLO_PROFILE` | Profile to use, like `-P` |
| `PIHELLO_URL` | Same as `url` |
| `PIHELLO_PASSWORD` | Same as `password` |
| `PIHELLO_PASSWORD_FILE` | Same as `password_file` (a relative path is relative to the current directory) |
| `PIHELLO_PRESET` | Same as `preset` |
| `PIHELLO_THEME` | Same as `theme` |
| `NO_COLOR` | Any non-empty value turns colors off (with `color = "auto"`) |
| `FORCE_COLOR`, `CLICOLOR_FORCE` | Turn colors on even when the output isn't a terminal (with `color = "auto"`) |
| `COLUMNS`, `LINES` | Override the detected terminal size (when the output is a terminal) |
| `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` | Base directories of the config file and the cache (`APPDATA` / `LOCALAPPDATA` on Windows) |
| `http_proxy`, `https_proxy`, `no_proxy` | Proxies, as usual for Python; `localhost` and loopback addresses never use a proxy |

Empty variables are ignored.
Only one of `PIHELLO_PASSWORD` and `PIHELLO_PASSWORD_FILE` may be set.

## Passwords

pihello logs in to the Pi-hole v6 API with a password.
Use an **app password**: in the Pi-hole web interface, go to *Settings → Web interface / API*, click *Configure app password*, and save it.

- An app password also works when two-factor authentication is on (pihello can't type TOTP codes).
- Pi-hole doesn't let an app password change settings unless you enable `webserver.api.app_sudo`.
- Your normal web interface password works too if 2FA is off. A Pi-hole without any password needs none.

There are four ways to give pihello the password, and `pihello init` can set up any of them:

| Place | Setting | Description |
| :-- | :-- | :-- |
| Config file | `password = "…"` | Simple. Keep the file private (`chmod 600`). |
| Password file | `password_file = "~/.config/pihello/password"`, `--password-file PATH`, `PIHELLO_PASSWORD_FILE` | The first line of the file is the password (line ending removed) |
| Password manager | `password_command = "pass show pihole"`, `--password-command CMD` | The first line the command prints, without surrounding spaces |
| Environment | `PIHELLO_PASSWORD` | Set it from wherever you keep secrets |

`--password-stdin` reads the first line of standard input instead (`pass show pihole | pihello --password-stdin`); it can't be combined with `-f -`.
The password is only obtained when pihello actually talks to the Pi-hole, so a [cache](#caching) hit or `--demo` never runs your password manager.

### How `password_command` runs

A list (`["pass", "show", "pihole"]`) is run directly.
A string is split into words like a shell would and also run directly, *unless* it contains shell syntax
(`|`, `&`, `;`, `<`, `>`, `(`, `)`, `$`, `` ` ``, `*`, `?`, `[`, `]`, `{`, `}`, `~`, `!`, `#` or a newline).
Then it runs through `/bin/sh -c` (`cmd /c` on Windows), so pipes and variables work:

```toml
# ~/.config/pihello/config.toml (one of these)
password_command = ["secret-tool", "lookup", "service", "pihole"]   # run directly
# password_command = "pass show pihole"                             # run directly
# password_command = "pass show pihole | head -n 1"                 # through /bin/sh
```

The command must exit with status 0 and is stopped after 30 seconds.
Its output is captured, but it can still ask on the terminal itself (like GPG's pinentry does for a passphrase).

### Security

- `pihello init` writes the config file and the password file with mode `600` (only you can read them).
- If the config file contains a `password` (at the top level or in a profile) and other users can read it, pihello prints a warning with the `chmod` command that fixes it; `pihello doctor` reports it too.
- The password never appears in pihello's output, error messages or `--debug` logs.
  It is sent only in the login request; the session id travels in a request header, and every session is logged out again.
- A password given as a command-line argument (`pihello ADDR PASSWORD`, the 1.x style) still works but prints a warning: it ends up in your shell history and is visible in `ps`.
  Move it into one of the places above.
- Passwords inside the URL (`http://user:pw@pi.hole`) are rejected.

## Profiles

Profiles are named sets of settings in the same file, for a second Pi-hole or just a different look.
A profile can contain every key from [Settings](#settings), plus its own `styles` table:

```toml
# ~/.config/pihello/config.toml
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

A profile's settings override the top-level ones; everything else comes from the top level (`-P tmux` above still uses the top-level `url` and password).
An unknown profile name is an error that lists the defined ones.

## Styles

The `[styles]` table changes single styles of the selected theme, without writing a whole theme.
The keys are the twelve [style names](themes.md#style-names): `title`, `subtitle`, `border`, `label`, `value`, `muted`, `accent`, `good`, `warn`, `bad`, `info` and `highlight`.
The values are the words you would write inside a [style tag](templates.md#style-tags), without the brackets:

```toml
# ~/.config/pihello/config.toml
theme = "nord"

[styles]
accent = "bold #ff8800"
highlight = "@bad underline"   # the theme's own "bad" style, underlined
```

`@name` refers to the *theme's* style of that name, before your overrides.
Styles from `[styles]`, `[profiles.NAME.styles]` and `--style NAME=WORDS` are combined.
For the same name, the command line wins over the profile, and the profile over the top-level table.
Invalid names and words are errors.

## HTTPS and certificates

Pi-hole v6 serves its web interface and API over HTTPS too (port 443), with a self-signed certificate it creates on first start.
pihello verifies certificates by default, so you have three options for `https://` URLs:

1. **Trust Pi-hole's certificate authority** (recommended).
   Copy `/etc/pihole/tls_ca.crt` from the Pi-hole and point pihello at it:

   ```toml
   # ~/.config/pihello/config.toml
   url = "https://pi.hole"
   verify_tls = "tls_ca.crt"   # relative to the config directory
   ```

   For a one-off, use `--ca-cert ~/tls_ca.crt`.
   Pi-hole's certificate is issued for the name in `webserver.domain`, `pi.hole` by default, so the URL has to use that name
   (add it to `/etc/hosts` if your DNS doesn't know it).
   Connecting by IP address fails with a certificate mismatch error.
2. **Use your own certificate** issued by a CA your system trusts: nothing to configure.
3. **Skip verification** with `-k` / `--insecure` or `verify_tls = false`.
   The connection is still encrypted, but not protected against impersonation; `pihello doctor` warns about it.

pihello never follows redirects, so the password can't be sent to another host by accident.
If your Pi-hole redirects HTTP to HTTPS, pihello says so and suggests the `https://` URL.

> [!NOTE]
> Python 3.13 and newer reject Pi-hole's generated CA by default: their strict X.509 mode wants a key usage extension the CA doesn't have.
> pihello turns that strict mode off; the certificate chain and the host name are still verified.

## Caching

A shell greeting runs every time you open a terminal.
With a cache, pihello reuses recent data instead of asking the Pi-hole each time:

```toml
# ~/.config/pihello/config.toml
cache_ttl = 60   # seconds
```

On the command line, use `--cache 60`; `--no-cache` turns the cache off for one run.

- **Location**: `$XDG_CACHE_HOME/pihello/` (by default `~/.cache/pihello/`), or `%LOCALAPPDATA%\pihello\Cache` on Windows.
  Files are private (mode `600`).
- **Contents**: only the fetched values, never the password or a session id. A cache hit doesn't log in at all.
- **Entries**: one per Pi-hole URL and set of data a template needs, so different presets don't overwrite each other's entries.
- **Not cached**: nothing is stored when an endpoint failed, and entries written by another pihello version are ignored. `--watch` always fetches fresh data.
- **Clearing**: delete the directory.

## Complete example

Every setting (with its default, where there is one), a style override and a profile:

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
