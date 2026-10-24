# Commands

Every pihello command, with examples and all its options.
`pihello COMMAND --help` lists the same options in your terminal.

| Command | Description |
| :-- | :-- |
| [`pihello`, `pihello show`](#pihello-show) | Print the greeting |
| [`pihello init`](#pihello-init) | Set up pihello interactively |
| [`pihello doctor`](#pihello-doctor) | Find out what's wrong, step by step |
| [`pihello vars`](#pihello-vars) | List the template variables with their values |
| [`pihello presets`](#pihello-presets) | Preview the presets, or copy one to edit |
| [`pihello themes`](#pihello-themes) | Preview the themes |
| [`pihello colors`](#pihello-colors) | List the color names for style tags |
| [`pihello completion`](#pihello-completion) | Print a Tab completion script |

The [exit status](#exit-status) is the same for every command.
For ready-made setups (shells, MOTD, tmux, status bars, scripts), see [Integrations](integrations.md).

## `pihello show`

`pihello` on its own prints the greeting, using your [config file](configuration.md); `pihello show` is the same.
Here it is with demo data:

```console
$ pihello --demo
Pi-hole on pi.hole · ● enabled  ↑ update available
────────────────────────────────────────────────────────────
Versions   Core v6.5 · Web v6.6.1 · FTL v6.7↑
Queries    48,220 today · 7,099 blocked
Blocked    █████▎░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 14.7%
Clients    12 active · 14 known
Gravity    121,837 domains · updated 3d 4h ago
Last block googleads.g.doubleclick.net
```

Options override the config file for one run:

```sh
pihello --preset compact -T nord                        # another preset and theme
pihello -f ~/greeting.txt                               # your own template file
pihello -e '{queries.percent_blocked|percent} blocked'  # an inline template
pihello --style 'accent=bold #ff8800'                   # restyle one theme style
pihello -u https://pi.hole -k                           # another Pi-hole, self-signed certificate
pihello -ts '%H:%M'                                     # print the time first
pihello -W 0                                            # don't wrap long lines
pihello -q                                              # no errors when the Pi-hole is offline (startup files)
```

### Errors and quiet mode

Without `-q`, a problem prints a message and a hint on stderr:

```console
$ pihello
pihello: no response from http://pi.hole within 3s
hint: Pi-hole may be down or overloaded; raise the limit with --timeout (or `timeout` in the config)
```

`-q` is for shell startup files: when the Pi-hole is unreachable, the shell opens without an error message.
The [exit status](#exit-status) still tells what went wrong.
Mistakes in the command line, the config file or a template (exit status 2) are printed anyway, so you notice them; warnings aren't.
The exception is a `password_command` that fails, times out or prints nothing, which usually means a locked password manager: `-q` keeps that quiet too.
[Troubleshooting](troubleshooting.md) explains the common messages.

`--debug` shows what pihello does and how long each step takes: the endpoints it fetches, the login, the cache, the rendering.
Passwords are never logged.

### Live dashboard

`--watch` keeps the greeting on screen and refreshes it every 5 seconds (or every `--watch SECONDS`), using a single API session.
It uses the whole terminal, redraws right away when the window is resized and shows when the data was last updated.
<kbd>Ctrl</kbd>+<kbd>C</kbd> quits.

```sh
pihello --watch                         # the configured preset
pihello --watch 10 --preset dashboard   # another preset, every 10 seconds
```

If the Pi-hole is unreachable for a while, the last data stays on screen with a warning and pihello keeps retrying.
A rejected password ends the watch.
When the output isn't a terminal, every refresh is printed below the previous one.

### Cache

With `--cache SECONDS` (or [`cache_ttl`](configuration.md#caching) in the config file), pihello reuses data it fetched less than SECONDS ago, without contacting the Pi-hole:

```sh
pihello -q --cache 60
```

In the [README's measurement](../README.md), a cache hit took 53 ms instead of 100 ms for the default preset.
Pi-hole also doesn't have to check the password, which matters more on a Raspberry Pi.
That helps when you open many terminals, and for status bars that refresh often.

### Demo data

`--demo` renders with realistic, built-in data instead of fetching it.
Use it to try presets and themes, to write [templates](templates.md), or to show pihello to someone:

```sh
pihello --demo --preset dashboard -T dracula
pihello --demo -f greeting.txt --strict
pihello vars --demo
```

### `show` options

The groups are the same as in `pihello --help`.
*Setting* is the [config file key](configuration.md#settings) that the option overrides.
Paths on the command line are relative to the current directory.

#### Connection

| Option | Setting | Description |
| :-- | :-- | :-- |
| `-u`, `--url URL` | `url` | Pi-hole address |
| `--password-file PATH` | `password_file` | Read the password from the first line of PATH |
| `--password-command CMD` | `password_command` | Run CMD and use the first line it prints |
| `--password-stdin` | | Read the password from standard input |
| `-k`, `--insecure` | `verify_tls = false` | Don't verify the HTTPS certificate |
| `--ca-cert PATH` | `verify_tls = "PATH"` | Verify HTTPS against this CA or self-signed certificate |
| `--timeout SECONDS` | `timeout` | Network timeout |
| `-P`, `--profile NAME` | `PIHELLO_PROFILE` | Use the settings of `[profiles.NAME]` |
| `--config PATH` | `PIHELLO_CONFIG` | Config file to read |

#### Template

| Option | Setting | Description |
| :-- | :-- | :-- |
| `--preset NAME` | `preset` | Built-in template |
| `-f`, `--template`, `--file FILE` | `template` | Template file; `-` reads standard input |
| `-e`, `--template-string TEXT` | | Template given inline |
| `-T`, `--theme NAME` | `theme` | Color theme |
| `--style NAME=WORDS` | `[styles]` | Override one theme style; repeat for more |
| `--demo` | | Use built-in demo data instead of a Pi-hole |

#### Output

| Option | Setting | Description |
| :-- | :-- | :-- |
| `--color WHEN` | `color` | `auto`, `always`, `never`, `16`, `256` or `truecolor` |
| `--no-color` | `color = "never"` | No colors |
| `-W`, `--width N` | `width` | `auto`, `0` (no limit) or a number of columns |
| `-H`, `--height N` | `height` | Print at most N lines |
| `-c`, `--clip` / `--wrap` | `overflow` | Cut long lines / wrap them |
| `-i`, `--tab-size`, `--indent N` | `tab_size` | Tab stop width, 0–32 |
| `--missing TEXT` | `missing` | Text for unavailable values |
| `--strict` / `--no-strict` | `strict` | Fail on unavailable values / don't |
| `-ts`, `--timestamp [FMT]` | `timestamp` | Print the time first, optionally in strftime format FMT |

#### Behaviour

| Option | Setting | Description |
| :-- | :-- | :-- |
| `--cache SECONDS` | `cache_ttl` | Reuse data fetched less than SECONDS ago |
| `--no-cache` | `cache_ttl = 0` | Always fetch fresh data |
| `--watch [SECONDS]` | | Live view, refreshed every SECONDS (default 5) |
| `-q`, `--quiet` | | Print no warnings, and no errors except mistakes (exit status 2); the exit status still tells |
| `--debug` | | Log HTTP requests, timings and problems to stderr |
| `-h`, `--help` | | Show the help |
| `-v`, `-V`, `--version` | | Show the version |

#### pihello 1.x compatibility

These keep 1.x command lines working; [Upgrading from 1.x](upgrading.md) has the details.

| Option | Setting | Description |
| :-- | :-- | :-- |
| `ADDR` | `url` | Host or `host:port`, as the first argument. On its own (without PASSWORD) it must contain a `.` or `:` or be `localhost`, so a mistyped command isn't looked up as a host; use `-u NAME` for single-label names. |
| `PASSWORD` | `password` | Second argument. Deprecated: it leaks into shell history and `ps`. |
| `-p`, `--https`, `--proto` | | Use `https://` for ADDR, `--url` or the configured URL, whatever scheme it has |

Options can come before a command: `pihello -q vars queries` is `pihello vars -q queries`, not ADDR `vars` with PASSWORD `queries`.
Put `--` first for a Pi-hole whose name is a command: `pihello -- doctor`.

## `pihello init`

`pihello init` asks five questions, checks each answer against your Pi-hole and writes the [config file](configuration.md):

1. **Address**: the address you open the Pi-hole web interface with (`pi.hole`, an IP address, `https://…`).
   It's checked right away.
   If the Pi-hole also serves HTTPS, you're offered to switch; for a self-signed certificate you can give the CA file or skip verification.
2. **Password**: where to keep the [app password](configuration.md#passwords): in the config file, a separate password file, a password manager command or the `PIHELLO_PASSWORD` environment variable.
   The login is tested.
   A Pi-hole without a password skips this step.
3. **Preset**: pick one of the built-in templates, with a preview of your own data (or demo data, if pihello can't fetch any).
4. **Theme**: pick the colors, again with a preview.
5. **Save**: writes `~/.config/pihello/config.toml` (mode `600`) and prints the snippet for your shell's startup file, offering to append it.

Nothing is written before the last step, and <kbd>Ctrl</kbd>+<kbd>C</kbd> cancels at any time.
An existing config file is only replaced after you confirm, and is kept as `config.toml.bak`.

For scripts and provisioning, `--yes` asks nothing and takes every answer from the options:

```sh
pihello init --url https://pi.hole --ca-cert ~/tls_ca.crt \
    --password-file ~/.config/pihello/password --yes
pass show pihole | pihello init --url pi.hole --password-stdin --yes
pihello init --url pi.hole --password-command 'pass show pihole' --preset compact -T nord --yes
```

### `init` options

| Option | Description |
| :-- | :-- |
| `-u`, `--url`, `--password-file`, `--password-command`, `--password-stdin`, `-k`, `--ca-cert`, `--timeout` | As for [`show`](#connection) |
| `--store WHERE` | Where the password goes: `config`, `file`, `command`, `env` or `none`. An explicit `--store` always wins. Otherwise, with `--yes`, it follows from the password option: `--password-file` → `file`, `--password-command` → `command`, `--password-stdin` → `config`, `PIHELLO_PASSWORD` set → `env`. |
| `--preset NAME`, `-T`, `--theme NAME` | The look to save |
| `--config PATH` | Write this file instead of the default one |
| `-y`, `--yes` | Don't ask anything |
| `--force` | Overwrite an existing config file (the old one is kept as `.bak`) |
| `--no-check` | Don't connect to the Pi-hole |
| `--shell SHELL` | Print the startup snippet for `bash`, `zsh`, `fish` or `powershell` instead of the one for `$SHELL` |
| `--color WHEN`, `--no-color` | Colors of the wizard itself and its previews (not saved) |
| `-q`, `--quiet` | Don't print warnings |
| `--debug` | Log HTTP requests to stderr |

`--password-stdin` needs `--yes`; otherwise the piped password would be read as the answer to the first question.
With `--store file` and `--password-stdin`, the password is written to `password` next to the config file (mode `600`).

## `pihello doctor`

`pihello doctor` checks everything between pihello and your Pi-hole, step by step, and explains what to do about each problem.
It never prints the password.
A sample run (addresses and paths shortened):

```console
$ pihello doctor
pihello doctor — pihello 2.0.0 on Python 3.13.16 (Linux)

Environment
  ✓ Python 3.13.16 (/home/pi/.local/share/pipx/venvs/pihello/bin/python)
  ✓ config file /home/pi/.config/pihello/config.toml (default location, mode 600)
  · effective settings (value — where it comes from):
      url         http://192.168.1.2                            config file
      password    password_file /home/pi/.config/pihello/pass…  config file
      verify_tls  true                                          default
      timeout     3 s                                           default
      template    preset 'default'                              config file
      theme       nord                                          config file
      color       auto                                          default
      width       auto                                          default
      cache_ttl   0 s                                           default

Connection
  ✓ address http://192.168.1.2; API at http://192.168.1.2/api
  · 192.168.1.2 is an IP address; no DNS lookup needed
  ✓ TCP connection to 192.168.1.2:80 (1 ms)
  ✓ Pi-hole v6 API answers at /api/info/login (15 ms)
  ! using plain HTTP although this Pi-hole serves HTTPS on port 443
      hint: use url = "https://192.168.1.2" so the password is encrypted (add --ca-cert or --insecure for the self-signed certificate)

Authentication
  ✓ password read from password_file /home/pi/.config/pihello/password (0 ms)
  ✓ logged in with the configured password (4 ms)

Pi-hole
  ✓ Pi-hole core v6.5, web v6.6.1, FTL v6.8, docker 2026.10.0
  · update status unknown
      hint: Pi-hole has not checked for updates yet (it does so daily, or run `pihole updatechecker`)
  ✓ 17 of 17 data sources available (7 ms)

Sessions
  · 1 active API session (including this one)
  ✓ logged out; no session left behind (0 ms)

Works, with 1 warning (took 47 ms)
```

| Mark | Description |
| :-- | :-- |
| `✓` | Works |
| `!` | Warning: works, but should be fixed |
| `✗` | Failure |
| `·` | Information |

The checks cover Python, the config file and its permissions, where every setting comes from, the address, DNS, TCP, HTTPS, the API, the password and login, versions and updates, every data source, and whether the API session is really closed afterwards.
A failure that makes the rest pointless (no address, DNS, connection, certificate or login) stops the run.

The exit status is 0 when everything works (warnings allowed), otherwise that of the first failure (see [Exit status](#exit-status)).

### `doctor` options

`doctor` takes the same connection options as `show`, so you can test settings before saving them:

```sh
pihello doctor -u https://pi.hole --ca-cert ~/tls_ca.crt
```

| Option | Description |
| :-- | :-- |
| `-u`, `--url`, `--password-file`, `--password-command`, `--password-stdin`, `-k`, `--ca-cert`, `--timeout` | As for [`show`](#connection) |
| `-p`, `--https` | Use `https://` for `--url` |
| `--config PATH`, `-P`, `--profile NAME` | Config file and profile to check |
| `--color WHEN`, `--no-color`, `-q`, `--debug` | As for [`show`](#show-options) |

## `pihello vars`

`pihello vars` lists every [template variable](variables.md) with its current value and a description.
A pattern filters the paths, either as a substring or as a glob:

```console
$ pihello vars --demo gravity
PATH                           VALUE       DESCRIPTION
queries.status.GRAVITY         6221        Queries blocked by a domain on a block list (gravity)
queries.status.GRAVITY_CNAME   454         Queries blocked by gravity during deep CNAME inspection
gravity.domains_being_blocked  121837      Domains on Pi-hole's block lists (gravity)
gravity.last_update            1792556249  When gravity (the block lists) was last updated; 0 if unknown
gravity.relative.days          3           Whole days since the last gravity update (pihello 1.x compatible)
gravity.relative.hours         4           Hours (0-23) since the last gravity update, after the whole days
gravity.relative.minutes       12          Minutes (0-59) since the last gravity update, after the whole hours
ftl.database.gravity           121837      Exact domains on block lists (gravity)
ftl.database.antigravity       0           Exact domains on allow lists (antigravity) (FTL v6.7+)
```

List entries are shown once, as `top_domains.N.domain` with the first item's value.
`--all` lists every concrete path instead: each list item, and keys your Pi-hole sends that pihello doesn't document.

```console
$ pihello vars --demo 'top_domains*' --all
PATH                  VALUE                            DESCRIPTION
top_domains.0.domain  "connectivitycheck.gstatic.com"  Domain name
top_domains.0.count   2814                             Number of queries for this domain
top_domains.1.domain  "www.google.com"                 Domain name
…
```

`--json` prints the data as JSON for scripts: the whole nested tree, or a flat `{"path": value}` object when you give a pattern.
Control and text-direction characters in the data come out escaped (`\u009b`, `\u202e`), so the JSON is safe to print on a terminal.

```console
$ pihello vars --demo --json | jq '.queries.percent_blocked'
14.72210700953961
$ pihello vars --demo --json | jq -r '.top_blocked[] | "\(.count)\t\(.domain)"' | head -3
812	pagead2.googlesyndication.com
655	googleads.g.doubleclick.net
591	app-measurement.com
$ pihello vars --demo --json 'gravity.relative.*'
{
  "gravity.relative.days": 3,
  "gravity.relative.hours": 4,
  "gravity.relative.minutes": 12
}
```

### `vars` options

| Option | Description |
| :-- | :-- |
| `PATTERN` | Only paths that match: a glob (`'queries.*'`) or a substring (`blocked`) |
| `--demo` | Use demo data instead of your Pi-hole |
| `--json` | Print JSON: the nested tree, or `{path: value}` with PATTERN |
| `--all` | List every concrete path (each list item, undocumented keys) |
| `-u`, `--url`, `--password-file`, `--password-command`, `--password-stdin`, `-k`, `--ca-cert`, `--timeout` | As for [`show`](#connection) |
| `--config PATH`, `-P`, `--profile NAME`, `--color WHEN`, `--no-color`, `-q`, `--debug` | As for [`show`](#show-options) |

## `pihello presets`

`pihello presets` renders every built-in template (preset) with demo data, in your terminal.
The [preset gallery](presets.md) shows the same as pictures.

```sh
pihello presets                     # every preset, previewed with demo data
pihello presets --no-preview        # just the names, widths and descriptions
pihello presets dashboard --live    # one preset with your Pi-hole's data
pihello presets dashboard -T nord   # ... in another theme
```

Each preset has a minimum width (`pihello presets --no-preview` lists it); narrower terminals wrap the lines.

`--source` prints a preset's template, and `-o FILE` saves it, to start [your own template](templates.md):

```sh
pihello presets default --source -o ~/.config/pihello/greeting.txt
```

`-o` writes UTF-8, creates missing folders and never replaces an existing file (that's an error, exit status 2).
Prefer it to `--source > FILE`: Windows PowerShell 5.1 saves redirected output as UTF-16 and loses symbols such as ● and ·.

### `presets` options

| Option | Description |
| :-- | :-- |
| `NAME` | Show only this preset |
| `--preview`, `--no-preview` | Render each preset (default: yes) |
| `--source` | Print the template source of NAME |
| `-o`, `--output FILE` | With `--source`: write it to FILE as UTF-8 (creates folders, never overwrites) |
| `--demo` / `--live` | Preview with demo data (the default) / with data from your Pi-hole |
| `-T`, `--theme NAME` | Theme for the previews |
| `-W`, `--width N` | Preview width (default: the terminal's) |
| `-u`, `--url`, `--password-file`, `--password-command`, `--password-stdin`, `-k`, `--ca-cert`, `--timeout` | As for [`show`](#connection), with `--live` |
| `--config PATH`, `-P`, `--profile NAME`, `--color WHEN`, `--no-color`, `-q`, `--debug` | As for [`show`](#show-options) |

## `pihello themes`

`pihello themes` shows every theme's style swatches and a preview.
The [theme gallery](themes.md) shows the same as pictures.

```sh
pihello themes                          # every theme with your configured preset
pihello themes nord --preset dashboard  # one theme with another preset
pihello themes --no-preview             # names and swatches only
```

### `themes` options

| Option | Description |
| :-- | :-- |
| `NAME` | Show only this theme |
| `--preview`, `--no-preview` | Render a preset with each theme (default: yes) |
| `--preset NAME` | Preset for the previews (default: your configured preset) |
| `--demo` / `--live` | Preview with demo data (the default) / with data from your Pi-hole |
| `-W`, `--width N` | Preview width (default: the terminal's) |
| `-u`, `--url`, `--password-file`, `--password-command`, `--password-stdin`, `-k`, `--ca-cert`, `--timeout` | As for [`show`](#connection), with `--live` |
| `--config PATH`, `-P`, `--profile NAME`, `--color WHEN`, `--no-color`, `-q`, `--debug` | As for [`show`](#show-options) |

## `pihello colors`

`pihello colors` prints every color name you can use in [style tags](templates.md#style-tags), with a swatch, its palette number and hex value.
`-W N` sets the layout width; `--color` and `--no-color` work as for [`show`](#output).

```console
$ pihello colors --no-color -W 80 | head -4
Basic colors (0-15)
    black               0 #000000      maroon              1 #800000
    green               2 #008000      olive               3 #808000
    navy                4 #000080      magenta             5 #800080
```

## `pihello completion`

`pihello completion SHELL` prints a completion script for `bash`, `zsh` or `fish`.
It completes commands, options, preset, theme and profile names, and file paths.
Install it once:

```sh
# bash (needs the bash-completion package)
mkdir -p ~/.local/share/bash-completion/completions
pihello completion bash > ~/.local/share/bash-completion/completions/pihello
# ... or load it from ~/.bashrc:  eval "$(pihello completion bash)"

# zsh: then add  fpath+=~/.zfunc  before  compinit  in ~/.zshrc
mkdir -p ~/.zfunc
pihello completion zsh > ~/.zfunc/_pihello

# fish
mkdir -p ~/.config/fish/completions
pihello completion fish > ~/.config/fish/completions/pihello.fish
```

Open a new shell afterwards.
Preset, theme and profile names are looked up when you press <kbd>Tab</kbd>, so they stay current after upgrades and config changes.
PowerShell completion isn't available.

## Exit status

| Code | Description |
| --: | :-- |
| 0 | Success |
| 1 | Internal error: a bug in pihello. Please [report it](https://github.com/pavelgar/pihello/issues), with the output of `--debug`. |
| 2 | Usage, configuration or template error |
| 3 | The Pi-hole can't be reached: name resolution, connection refused, timeout, TLS |
| 4 | Authentication failed: wrong password, two-factor authentication, no free API sessions, login rate limit |
| 5 | Unexpected answer from the Pi-hole API |
| 130 | Interrupted with <kbd>Ctrl</kbd>+<kbd>C</kbd> (`--watch` exits with 0) |
| 129, 143 | Ended by SIGHUP (terminal closed) or SIGTERM; pihello still logs out first |

With `-q`, warnings and the errors with exit status 3, 4 and 5 aren't printed, but the exit status is the same.
Errors with exit status 2 are mistakes to fix, so `-q` still prints them, except a `password_command` that fails, times out or prints nothing.
`pihello doctor` exits with the code of the first failed check.
