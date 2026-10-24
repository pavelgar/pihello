# Using pihello

A tour of every command, with examples. `pihello COMMAND --help` lists all
options of a command; the options of the greeting itself are also in
[configuration.md](configuration.md#command-line-options).

**Contents:** [show](#show-the-greeting) · [init](#init-set-up-pihello) ·
[doctor](#doctor-find-out-whats-wrong) · [vars](#vars-list-the-variables) ·
[presets and themes](#presets-and-themes-browse-the-looks) ·
[colors](#colors-the-color-palette) · [completion](#completion-tab-completion) ·
[--watch](#--watch-a-live-dashboard) · [--cache](#--cache-faster-shell-start) ·
[--demo](#--demo-no-pi-hole-needed) · [Several Pi-holes](#several-pi-holes) ·
[Shells, MOTD and status bars](#shells-motd-and-status-bars) ·
[Scripts](#scripts)

## `show`: the greeting

`pihello` on its own prints the greeting, using your config file;
`pihello show` is the same. Here with demo data:

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
pihello --preset compact -T nord            # another preset and theme
pihello -f ~/greeting.txt                   # your own template file
pihello -e '{queries.percent_blocked|percent} blocked'   # an inline template
pihello --style 'accent=bold #ff8800'       # restyle one theme style
pihello -u https://pi.hole -k               # another Pi-hole, self-signed certificate
pihello -ts '%H:%M'                         # print the time first
pihello -W 0                                # don't wrap long lines
pihello -q                                  # never print errors (for startup files)
```

`-q` is meant for shell startup files: when the Pi-hole is unreachable, the
shell opens without an error message (the exit status still tells what went
wrong). Without `-q` you get a message and a hint:

```console
$ pihello
pihello: no response from http://pi.hole within 3s
hint: Pi-hole may be down or overloaded; raise the limit with --timeout (or `timeout` in the config)
```

`--debug` shows what pihello does and how long it takes: which endpoints it
fetches, the login, the cache, the rendering. Passwords are never logged.

## `init`: set up pihello

`pihello init` asks five questions, checks each answer against your Pi-hole
and writes the [config file](configuration.md):

1. **Address**: the address you open the Pi-hole web interface with
   (`pi.hole`, an IP address, `https://…`). It's checked right away; if the
   Pi-hole also serves HTTPS, you're offered to switch, and for a
   self-signed certificate you can give the CA file or skip verification.
2. **Password**: where to keep the [app password](configuration.md#passwords):
   in the config file, a separate password file, a password manager command
   or the `PIHELLO_PASSWORD` environment variable. The login is tested.
   A Pi-hole without a password skips this step.
3. **Preset**: pick one of the built-in templates, with a preview of your
   own data (or demo data, if pihello can't fetch any).
4. **Theme**: pick the colors, again with a preview.
5. **Save**: writes `~/.config/pihello/config.toml` (mode `600`) and prints
   the snippet for your shell's startup file, offering to append it.

Nothing is written before the last step, and Ctrl-C cancels at any time. An
existing config file is only replaced after you confirm, and kept as
`config.toml.bak`.

For scripts and provisioning, `--yes` asks nothing and takes every answer
from the options:

```sh
pihello init --url https://pi.hole --ca-cert ~/tls_ca.crt \
    --password-file ~/.config/pihello/password --yes
pass show pihole | pihello init --url pi.hole --password-stdin --yes
pihello init --url pi.hole --password-command 'pass show pihole' --preset compact -T nord --yes
```

| Option | Meaning |
| :-- | :-- |
| `-u`, `--url`, `--password-file`, `--password-command`, `--password-stdin`, `-k`, `--ca-cert`, `--timeout` | as for the greeting |
| `--store WHERE` | where the password goes: `config`, `file`, `command`, `env` or `none`. An explicit `--store` always wins; otherwise, with `--yes`, it follows from the password option: `--password-file` → `file`, `--password-command` → `command`, `--password-stdin` → `config`, `PIHELLO_PASSWORD` set → `env`. |
| `--preset NAME`, `-T`, `--theme NAME` | the look to save |
| `--config PATH` | write this file instead of the default one |
| `-y`, `--yes` | don't ask anything |
| `--force` | overwrite an existing config file (the old one is kept as `.bak`) |
| `--no-check` | don't connect to the Pi-hole |
| `--shell SHELL` | print the startup snippet for `bash`, `zsh`, `fish` or `powershell` instead of the one for `$SHELL` |
| `--color WHEN`, `--no-color` | colors of the wizard itself and its previews (not saved) |

`--password-stdin` needs `--yes` (otherwise the piped password would be read
as the answer to the first question). With `--store file` and
`--password-stdin`, the password is written to `password` next to the config
file (mode `600`).

## `doctor`: find out what's wrong

`pihello doctor` checks everything between pihello and your Pi-hole, step by
step, and explains what to do about each problem. It never prints the
password. A sample run (addresses and paths shortened):

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

`✓` is fine, `!` a warning, `✗` a failure and `·` information. The checks
are: Python, the config file and its permissions, where every setting comes
from, the address, DNS, TCP, HTTPS, the API, the password and login,
versions and updates, every data source, and that the API session is really
closed afterwards. A failure that makes the rest pointless (no address, DNS,
connection, certificate, login) stops the run.

`doctor` takes the connection options, `-p`/`--https`, `-P`, `--config` and
`--color`, so you can test settings before saving them:
`pihello doctor -u https://pi.hole --ca-cert ~/tls_ca.crt`. The exit status
is 0 when everything works (warnings allowed), otherwise that of the first
failure (see [Exit status](configuration.md#exit-status)).

## `vars`: list the variables

`pihello vars` lists every [template variable](variables.md) with its
current value and a description. `--demo` uses demo data instead of your
Pi-hole. A pattern filters the paths, either as a substring or as a glob:

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

List entries are shown once, as `top_domains.N.domain` with the first item's
value. `--all` lists every concrete path instead (each list item, and keys
your Pi-hole sends that pihello doesn't document):

```console
$ pihello vars --demo 'top_domains*' --all
PATH                  VALUE                            DESCRIPTION
top_domains.0.domain  "connectivitycheck.gstatic.com"  Domain name
top_domains.0.count   2814                             Number of queries for this domain
top_domains.1.domain  "www.google.com"                 Domain name
…
```

`--json` prints the data as JSON for scripts: the whole nested tree, or a
flat `{"path": value}` object when you give a pattern.

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

## `presets` and `themes`: browse the looks

`pihello presets` renders every built-in template with demo data, in your
terminal; `pihello themes` shows every theme's style swatches and a preview.
The [preset gallery](presets.md) and [theme gallery](themes.md) show the same
as pictures.

```sh
pihello presets                       # every preset, previewed with demo data
pihello presets --no-preview          # just the names, widths and descriptions
pihello presets dashboard --live      # one preset with your Pi-hole's data
pihello presets dashboard -T nord     # ... in another theme
pihello presets default --source > ~/.config/pihello/greeting.txt   # copy one to edit
pihello themes                        # every theme with your configured preset
pihello themes nord --preset dashboard
pihello themes --no-preview           # names and swatches only
```

Each preset has a minimum width (`pihello presets --no-preview` lists it);
narrower terminals wrap the lines. Both commands take `-W N` for the preview
width and the connection options for `--live`.

## `colors`: the color palette

`pihello colors` prints every color name you can use in
[style tags](templates.md#style-tags), with a swatch, its palette number and
hex value. `-W N` sets the layout width.

```console
$ pihello colors --no-color -W 80 | head -4
Basic colors (0-15)
    black               0 #000000      maroon              1 #800000
    green               2 #008000      olive               3 #808000
    navy                4 #000080      magenta             5 #800080
```

## `completion`: Tab completion

`pihello completion SHELL` prints a completion script for bash, zsh or fish.
It completes commands, options, preset, theme and profile names, and file
paths. Install it once:

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

Open a new shell afterwards. Preset, theme and profile names are looked up
when you press Tab, so they stay current after upgrades and config changes.
PowerShell completion isn't available.

## `--watch`: a live dashboard

`--watch` keeps the greeting on screen and refreshes it every 5 seconds (or
every `--watch SECONDS`), using a single API session. It uses the whole
terminal, redraws right away when the window is resized and shows when the
data was last updated. Ctrl-C quits.

```sh
pihello --watch                       # the configured preset
pihello --watch 10 --preset dashboard
```

If the Pi-hole is unreachable for a while, the last data stays on screen with
a warning, and pihello keeps retrying (a rejected password ends the watch).
When the output isn't a terminal, every refresh is printed below the
previous one.

## `--cache`: faster shell start

With `--cache SECONDS` (or `cache_ttl` in the config file), pihello reuses
data it fetched less than SECONDS ago, without contacting the Pi-hole. Useful
when you open many terminals, or for a status bar that refreshes often:

```sh
pihello -q --cache 60
```

Details are in [configuration.md](configuration.md#caching).

## `--demo`: no Pi-hole needed

`--demo` renders with realistic, built-in data instead of fetching it. Use it
to try presets and themes, to write [templates](templates.md), or to show
pihello to someone:

```sh
pihello --demo --preset dashboard -T dracula
pihello --demo -f greeting.txt --strict
pihello vars --demo
```

## Several Pi-holes

Put each Pi-hole in a [profile](configuration.md#profiles) and pick one with
`-P`:

```toml
url = "http://pi.hole"
password_file = "~/.config/pihello/password"

[profiles.backup]
url = "http://192.168.1.3"
password_file = "~/.config/pihello/password-backup"
```

```sh
pihello                  # the main Pi-hole
pihello -P backup        # the second one
PIHELLO_PROFILE=backup pihello
```

A greeting for both, one line each:

```sh
pihello -q --preset minimal
pihello -q --preset minimal -P backup
```

## Shells, MOTD and status bars

### Shell startup files

`pihello init` prints the right snippet for your shell. They are the same as
in the [README](../README.md#shell-greeting): bash (`~/.bashrc`), zsh
(`~/.zshrc`), fish (`~/.config/fish/conf.d/pihello.fish`, replacing fish's
greeting) and PowerShell (`$PROFILE`). Always use `-q` there, so an offline
Pi-hole doesn't print errors into every new shell. If it makes new shells
slow, see [Troubleshooting](../README.md#troubleshooting).

### MOTD on the Pi-hole host

To greet everyone who logs in to a machine, add a script to
`/etc/profile.d/`, which `sh` and `bash` login shells run. Each user needs
their own pihello config (or set `PIHELLO_CONFIG` to a shared file; don't put a
plain-text password in a file everyone can read):

```sh
# /etc/profile.d/pihello.sh
case $- in
    *i*) command -v pihello >/dev/null 2>&1 && pihello -q ;;
esac
```

pihello has to be on every user's `PATH`.

On Debian and Ubuntu you can use `/etc/update-motd.d/` instead. Those scripts
run as root and their output is collected before it is shown, so pihello
needs a config that root can read (`PIHELLO_CONFIG=/etc/pihello.toml`) and
`--color always` to keep its colors (output that isn't a terminal gets none).

### tmux

The `status` preset is a short segment made for status bars. Status bars
draw their own colors, so turn pihello's off:

```tmux
# ~/.tmux.conf
set -g status-interval 30
set -g status-right '#(pihello -q --preset status --color never --cache 30) '
```

which shows `● 14.7% of 48.2k` on the right.

### starship

```toml
# ~/.config/starship.toml
[custom.pihole]
command = "pihello -q --preset status --color never --cache 60"
when = true
format = "[$output]($style) "
style = "green"
```

starship gives custom commands 500 ms by default (`command_timeout`); the
cache keeps pihello well below that after the first prompt.

### waybar, polybar and others

Any bar that runs a command and shows its output works the same way:

```jsonc
// ~/.config/waybar/config
"custom/pihole": {
    "exec": "pihello -q --preset status --color never",
    "interval": 60
}
```

```ini
; ~/.config/polybar/config.ini
[module/pihole]
type = custom/script
exec = pihello -q --preset status --color never
interval = 60
```

For your own segment, use an inline template, e.g.
`pihello -q --color never -e '{queries.blocked|compact} blocked'`.

## Scripts

- The exit status tells what happened: 0 ok, 2 configuration, 3 Pi-hole
  unreachable, 4 authentication, 5 unexpected API answer
  ([all codes](configuration.md#exit-status)).
- `pihello vars --json` gives all data as JSON (see [`vars`](#vars-list-the-variables)).
- `-e` with `--color never` prints exactly one value:

  ```sh
  if [ "$(pihello -q --color never -e '{blocking}')" != enabled ]; then
      echo "Pi-hole is not blocking!"
  fi
  ```

- Piped output has no colors (unless `--color always` or `FORCE_COLOR`) and
  isn't wrapped.
