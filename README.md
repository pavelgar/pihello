# pihello

![pihello showing a Pi-hole status greeting in a terminal](https://raw.githubusercontent.com/pavelgar/pihello/master/docs/assets/hero.svg)

pihello prints a short, colorful summary of your [Pi-hole](https://pi-hole.net)
every time you open a terminal: whether ad blocking is on, how many queries were
blocked today, versions and pending updates, and whatever else you want to see.
It also works as a MOTD, a tmux or status bar segment, or a live dashboard.
It's a small Python package without dependencies (only `tomli` on Python 3.10),
made for Pi-hole v6.

- **Set up in a minute**: `pihello init` checks the address and password,
  writes the config and prints the line for your shell's startup file.
- **15 presets and 11 themes**, from a one-line status bar segment to a
  full dashboard; every preset works with every theme.
- **Your own templates** with styles, conditional colors, filters, bars and
  sparklines, and over 200 [variables](https://github.com/pavelgar/pihello/blob/master/docs/variables.md).
- **Quiet and fast**: only fetches what the template needs, can cache it, and
  `-q` keeps new shells clean when the Pi-hole is offline.
- **Careful with secrets**: the password can live in a private file, a
  password manager or an environment variable, and is never printed.
- `pihello doctor` explains what's wrong when something doesn't work.

## Install

```sh
pipx install pihello
# or
uv tool install pihello
# or (refused by the system pip on Debian, Ubuntu and Raspberry Pi OS)
pip install --user pihello
```

You need Python 3.10 or newer and Pi-hole **v6.0** or newer (every v6
release is [tested](https://github.com/pavelgar/pihello/blob/master/docs/compatibility.md)). For Pi-hole v5, use
`pihello==0.3.0`.

## Quick start

```sh
pihello init     # answer a few questions
pihello          # there's your greeting
```

pihello logs in with a Pi-hole **app password**. To create one, open the
Pi-hole web interface, go to *Settings → Web interface / API*, click
*Configure app password* and save it. App passwords also work with two-factor
authentication; your normal password works too if 2FA is off.

No Pi-hole at hand? `pihello --demo` shows everything with built-in sample
data.

## Shell greeting

`pihello init` prints the snippet for your shell and can add it for you. Use
`-q`, so an unreachable Pi-hole never puts error messages into a new shell.

**bash**, in `~/.bashrc`:

```bash
# Pi-hole greeting (pihello)
if [[ $- == *i* ]] && command -v pihello >/dev/null 2>&1; then
    pihello -q
fi
```

**zsh**, in `~/.zshrc`:

```zsh
# Pi-hole greeting (pihello)
if [[ -o interactive ]] && (( $+commands[pihello] )); then
    pihello -q
fi
```

**fish**, in `~/.config/fish/conf.d/pihello.fish` (if you already have a
`fish_greeting` function, add the `type -q …` line to it instead):

```fish
# Pi-hole greeting (pihello): replaces fish's default greeting
function fish_greeting
    type -q pihello; and pihello -q
end
```

**PowerShell**, in `$PROFILE` (open it with `notepad $PROFILE`; create it first
with `New-Item -Force $PROFILE` if it doesn't exist):

```powershell
# Pi-hole greeting (pihello)
if (Get-Command pihello -ErrorAction SilentlyContinue) { pihello -q }
```

**MOTD** for every login on a machine, in `/etc/profile.d/pihello.sh`:

```sh
case $- in
    *i*) command -v pihello >/dev/null 2>&1 && pihello -q ;;
esac
```

**tmux and status bars**: the `status` preset is a short segment
(`● 14.7% of 48.2k`). Bars do their own coloring, so turn colors off:

```tmux
set -g status-interval 30
set -g status-right '#(pihello -q --preset status --color never --cache 30) '
```

[docs/usage.md](https://github.com/pavelgar/pihello/blob/master/docs/usage.md#shells-motd-and-status-bars) has more, including
starship, waybar and polybar.

## Presets and themes

Choose the layout with `--preset NAME` and the colors with `--theme NAME` (or
`preset =` / `theme =` in the config file). A few of the presets:

| Preset | Shows |
| :-- | :-- |
| `default` | status, versions, queries with a bar, clients, gravity |
| `compact` | the essentials in three short lines |
| `minimal` | `● Pi-hole · 48.2k queries · 14.7% blocked · 12 clients` |
| `dashboard` | a PADD-style dashboard with bars and 24 h sparklines |
| `fetch` | neofetch-style, with the Pi-hole logo |
| `status` | a tiny segment for tmux and status bars |
| `classic` | exactly the pihello 1.x greeting |

Themes include `nord`, `dracula`, `gruvbox`, `catppuccin`, `solarized`,
`tokyonight`, `monokai`, `pihole`, `classic16` and `mono`. See them all in the
[preset gallery](https://github.com/pavelgar/pihello/blob/master/docs/presets.md) and the [theme gallery](https://github.com/pavelgar/pihello/blob/master/docs/themes.md), or
in your own terminal with `pihello presets` and `pihello themes`.

## Customising

Templates are plain text with `[style tags]` and `{expressions}`. Start from
a preset (`pihello presets default --source > ~/.config/pihello/greeting.txt`)
or from scratch:

```text
[@title]Pi-hole {version.core.local.version}[@muted] · [{blocking|map(enabled=@good, _=@bad)}]● {blocking}
[@label]Blocked [@highlight]{queries.blocked|number}[@muted] of {queries.total|number} ({queries.percent_blocked|percent})
[@label]Top ad  [@highlight]{top_blocked.0.domain|default("none")}
[@label]24h     [@accent]{history|pluck(total)|spark(30)}
```

```text
Pi-hole v6.5 · ● enabled
Blocked 7,099 of 48,220 (14.7%)
Top ad  pagead2.googlesyndication.com
24h     ▆▆▆▆▆▆▆▆▆▆▇▇███▇▇▅▄▃▂▁▁▁▁▁▂▃▄▅
```

Try it with `pihello --demo -f ~/.config/pihello/greeting.txt`, then replace
the `preset` line in the config file with
`template = "~/.config/pihello/greeting.txt"`. The
[template guide](https://github.com/pavelgar/pihello/blob/master/docs/templates.md) covers the language and every filter;
[docs/variables.md](https://github.com/pavelgar/pihello/blob/master/docs/variables.md) lists every variable.

## Commands

| Command | What it does |
| :-- | :-- |
| `pihello` | print the greeting (same as `pihello show`) |
| `pihello init` | interactive setup: address, password, preset, theme, shell snippet |
| `pihello doctor` | check config, network, HTTPS, login and API step by step |
| `pihello presets [NAME]` | preview the presets; `--source` prints one to copy |
| `pihello themes [NAME]` | preview the themes |
| `pihello vars [PATTERN]` | list the template variables with their values; `--json` for scripts |
| `pihello colors` | list the color names for templates |
| `pihello completion SHELL` | Tab completion for bash, zsh or fish |
| `pihello --watch [SECONDS]` | live dashboard, refreshed every 5 s |
| `pihello --cache SECONDS` | reuse data fetched less than SECONDS ago |
| `pihello --demo` | use built-in demo data instead of a Pi-hole |

Every command has `--help`; [docs/usage.md](https://github.com/pavelgar/pihello/blob/master/docs/usage.md) has examples for
all of them.

## Configuration

`pihello init` writes `~/.config/pihello/config.toml` (`%APPDATA%\pihello\config.toml`
on Windows):

```toml
url = "https://pi.hole"
password_file = "~/.config/pihello/password"
verify_tls = "tls_ca.crt"
preset = "default"
theme = "nord"
cache_ttl = 30

[profiles.backup]          # pihello -P backup
url = "http://192.168.1.3"
password_command = "pass show pihole-backup"
```

Command-line options and `PIHELLO_*` environment variables override the file.
[docs/configuration.md](https://github.com/pavelgar/pihello/blob/master/docs/configuration.md) describes every setting,
password options, profiles, style overrides, HTTPS certificates and caching.

## Troubleshooting

Run **`pihello doctor`** first: it checks every step from the config file to
the API session and says what to do about each problem.

- **Nothing appears in new shells**: that's `-q` hiding an error. Run
  `pihello` without `-q` to see it.
- **New shells are slow when the Pi-hole is offline**: lower the network
  timeout (`timeout = 1`, the default is 3 seconds).
- **`could not resolve host 'pi.hole'`**: `pi.hole` only resolves on machines
  that use the Pi-hole as their DNS server. Use its IP address or another
  host name.
- **`TLS certificate verification failed`**: Pi-hole's HTTPS certificate is
  self-signed. Copy `/etc/pihole/tls_ca.crt` from the Pi-hole and use
  `--ca-cert tls_ca.crt` (or `verify_tls = "tls_ca.crt"`). The certificate is
  issued for `pi.hole`, so connect with that name. `-k` / `--insecure` skips
  the check. [More on HTTPS](https://github.com/pavelgar/pihello/blob/master/docs/configuration.md#https-and-certificates).
- **`two-factor authentication enabled`**: pihello can't enter TOTP codes.
  Create an app password (see [Quick start](#quick-start)) and use that.
- **`Pi-hole has no free API sessions`**: Pi-hole allows 16 API sessions at
  a time by default (`webserver.api.max_sessions`). pihello always logs out, but other
  tools may not. Old sessions expire after 30 minutes, or delete them under
  *Settings → Web interface / API*.
- **`Pi-hole is rate-limiting login attempts`**: Pi-hole accepts at most 3
  logins per second, so opening many terminals at once (e.g. tmux restoring
  panes) can trip it. pihello waits and retries briefly; set `cache_ttl = 30`
  so new shells within 30 seconds reuse the last result instead of logging in.
- **`?` instead of a value**: the value isn't available on your Pi-hole, or
  the variable name has a typo. `pihello --strict` names the culprit,
  `pihello vars` shows what's there.
- **Wrong or no colors**: set `--color 256` or `--color truecolor`, check
  that `NO_COLOR` isn't set, and use `--color always` when piping.

Still stuck? [Open an issue](https://github.com/pavelgar/pihello/issues) with
the output of `pihello doctor` (it never contains your password).

## Upgrading from 1.x

Your `pihello ADDR PASSWORD -f FILE …` command and template files keep
working, and the `classic` preset is the exact 1.x look. Running
`pihello init` once moves the password out of your shell history.
[docs/upgrading.md](https://github.com/pavelgar/pihello/blob/master/docs/upgrading.md) lists every change.

## Documentation

- [Usage](https://github.com/pavelgar/pihello/blob/master/docs/usage.md): every command, with examples and integrations
- [Configuration](https://github.com/pavelgar/pihello/blob/master/docs/configuration.md): the config file, passwords, profiles, HTTPS, caching, all options
- [Templates](https://github.com/pavelgar/pihello/blob/master/docs/templates.md): the template language and filter reference
- [Variables](https://github.com/pavelgar/pihello/blob/master/docs/variables.md): every value you can show
- [Presets](https://github.com/pavelgar/pihello/blob/master/docs/presets.md) and [themes](https://github.com/pavelgar/pihello/blob/master/docs/themes.md): the galleries
- [Upgrading from 1.x](https://github.com/pavelgar/pihello/blob/master/docs/upgrading.md)
- [Pi-hole compatibility](https://github.com/pavelgar/pihello/blob/master/docs/compatibility.md): tested versions and API details
- [Architecture](https://github.com/pavelgar/pihello/blob/master/docs/architecture.md): how the code fits together
- [Changelog](https://github.com/pavelgar/pihello/blob/master/CHANGELOG.md)

## Development

Contributions are welcome. [CONTRIBUTING.md](https://github.com/pavelgar/pihello/blob/master/CONTRIBUTING.md) explains the
setup (`uv sync`), the checks and the tests.

## License

[MIT](https://github.com/pavelgar/pihello/blob/master/LICENSE)
