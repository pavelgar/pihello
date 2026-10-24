# Integrations

Recipes for putting pihello where you'll see it: new shells, logins, tmux, prompts, status bars and scripts.

**On this page:** [Shell greeting](#shell-greeting) · [MOTD](#motd) · [tmux](#tmux) · [starship](#starship) ·
[waybar, polybar and other bars](#waybar-polybar-and-other-bars) · [Scripts and cron](#scripts-and-cron) ·
[Several Pi-holes](#several-pi-holes)

## Shell greeting

`pihello init` prints the snippet for your shell and offers to add it; `pihello init --shell SHELL` prints the one for another shell.
The snippets run pihello only in interactive shells and only when it's installed.

Always use `-q` in startup files, so an offline Pi-hole doesn't print errors into every new shell.
If it makes new shells slow, lower the timeout (see [Troubleshooting](troubleshooting.md#new-shells-are-slow-when-the-pi-hole-is-offline)).
With many shells at once, add the [cache](configuration.md#caching) (`cache_ttl = 30`).

### bash

```bash
# ~/.bashrc
# Pi-hole greeting (pihello)
if [[ $- == *i* ]] && command -v pihello >/dev/null 2>&1; then
    pihello -q
fi
```

### zsh

```zsh
# ~/.zshrc
# Pi-hole greeting (pihello)
if [[ -o interactive ]] && (( $+commands[pihello] )); then
    pihello -q
fi
```

### fish

This replaces fish's default greeting.
If you already have a `fish_greeting` function, add the `type -q …` line to it instead.

```fish
# ~/.config/fish/conf.d/pihello.fish
# Pi-hole greeting (pihello): replaces fish's default greeting
function fish_greeting
    type -q pihello; and pihello -q
end
```

### PowerShell

Open your profile with `notepad $PROFILE`; create it first with `New-Item -Force $PROFILE` if it doesn't exist.

```powershell
# $PROFILE
# Pi-hole greeting (pihello)
if (Get-Command pihello -ErrorAction SilentlyContinue) { pihello -q }
```

## MOTD

To greet everyone who logs in to a machine, add a script to `/etc/profile.d/`, which `sh` and `bash` login shells run:

```sh
# /etc/profile.d/pihello.sh
case $- in
    *i*) command -v pihello >/dev/null 2>&1 && pihello -q ;;
esac
```

pihello has to be on every user's `PATH`, and each user needs their own pihello config.
Alternatively, set `PIHELLO_CONFIG` to a shared file, but don't put a plain-text password in a file everyone can read.

On Debian and Ubuntu you can use `/etc/update-motd.d/` instead.
Those scripts run as root and their output is collected before it's shown, so pihello needs a config that root can read (`PIHELLO_CONFIG=/etc/pihello.toml`)
and `--color always` to keep its colors (output that isn't a terminal gets none).

## tmux

The `status` preset is a short segment made for status bars.
Status bars draw their own colors, so turn pihello's off:

```tmux
# ~/.tmux.conf
set -g status-interval 30
set -g status-right '#(pihello -q --preset status --color never --cache 30) '
```

That shows `● 14.7% of 48.2k` on the right.

## starship

```toml
# ~/.config/starship.toml
[custom.pihole]
command = "pihello -q --preset status --color never --cache 60"
when = true
format = "[$output]($style) "
style = "green"
```

starship gives custom commands 500 ms by default (`command_timeout`).
pihello took about 100 ms live and 50 ms from the cache on a desktop, and live takes longer when the Pi-hole is a Raspberry Pi, so the cache keeps a safe margin.

## waybar, polybar and other bars

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

For a segment of your own, use an inline template:

```sh
pihello -q --color never -e '{queries.blocked|compact} blocked'
```

## Scripts and cron

- The [exit status](commands.md#exit-status) tells what happened: 0 ok, 2 configuration, 3 Pi-hole unreachable, 4 authentication, 5 unexpected API answer.
- `pihello vars --json` gives all data as JSON (see [`pihello vars`](commands.md#pihello-vars)).
- Piped output has no colors (unless `--color always` or `FORCE_COLOR`) and isn't wrapped.

`-e` with `--color never` prints exactly one value:

```sh
if [ "$(pihello -q --color never -e '{blocking}')" != enabled ]; then
    echo "Pi-hole is not blocking!"
fi
```

For a log, for example from cron, write a template file such as `log.txt`:

```text
{now|datetime("%Y-%m-%dT%H:%M")} blocking={blocking} total={queries.total} blocked={queries.blocked} pct={queries.percent_blocked:.2f}
```

```sh
pihello -q -f log.txt >> pihole.log
```

Each run appends one line (here with the demo data):

```text
2026-10-24T08:30 blocking=enabled total=48220 blocked=7099 pct=14.72
```

## Several Pi-holes

Put each Pi-hole in a [profile](configuration.md#profiles) and pick one with `-P`:

```toml
# ~/.config/pihello/config.toml
url = "http://pi.hole"
password_file = "~/.config/pihello/password"

[profiles.backup]
url = "http://192.168.1.3"
password_file = "~/.config/pihello/password-backup"
```

```sh
pihello                          # the main Pi-hole
pihello -P backup                # the second one
PIHELLO_PROFILE=backup pihello   # the same, from the environment
```

A greeting for both, one line each:

```sh
pihello -q --preset minimal
pihello -q --preset minimal -P backup
```
