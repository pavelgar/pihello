# Upgrading from pihello 1.x

pihello 2.0 is a rewrite, but your 1.x command line and template files keep
working. This page lists what stays the same, what behaves differently, and
how to move to the new, safer setup in a few minutes.

Still on Pi-hole v5? pihello 1.x and 2.x need Pi-hole v6; stay on
`pihello==0.3.0`.

## The short version

```sh
pipx upgrade pihello        # or: uv tool upgrade pihello / pip install --user -U pihello
pihello init                # address + app password, preset, theme; writes the config
```

Then replace the `pihello ADDR PASSWORD …` line in your shell startup file
with `pihello -q` (`pihello init` prints the exact snippet). Pick the
`classic` preset in `pihello init` if you want to keep the 1.x look.

## What still works

**The 1.x command line.** These all behave as before:

```sh
pihello pi.hole MY-APP-PASSWORD
pihello pi.hole MY-APP-PASSWORD -f ~/greeting.txt
pihello pi.hole MY-APP-PASSWORD -p -k              # HTTPS, self-signed certificate
pihello 192.168.1.2:8080 MY-APP-PASSWORD -ts '%H:%M'
```

| 1.x option | In 2.0 |
| :-- | :-- |
| `ADDR` (first argument) | still accepted; same as `-u ADDR` / `url = "…"` |
| `PASSWORD` (second argument) | still accepted, with a warning (see below) |
| `-f`, `--file FILE` | same (also `--template FILE`, or `template = "…"` in the config) |
| `-p`, `--proto` | same (also `--https`); or write `https://` in the address |
| `-k`, `--insecure` | same; `--ca-cert` is the safer alternative |
| `-ts`, `--timestamp [FMT]` | same |
| `-W`, `--width N` | same, and now actually wraps (see below) |
| `-H`, `--height N` | same, and now actually limits the lines |
| `-c`, `--clip` | same, and now actually clips |
| `-i`, `--indent N` | same (also `--tab-size`): the tab stop width, now applied |
| `-v`, `--version` | same |

**The 1.x look.** With an address on the command line and no template
configured, pihello renders the `classic` preset: the exact 1.x default
greeting, colors included. Use `--preset classic` (or `preset = "classic"`)
to get it anywhere else.

**Templates and variables.** 1.x template files work unchanged: the style
tags, color names, `\[` / `\{` escapes and every 1.x variable
(`{blocking}`, `{queries.total}`, `{gravity.relative.days}`,
`{version.core.local.version}`, `{recent_blocked}`, …) are still there. The
template language only gained features: [theme styles](templates.md#theme-styles-name),
[filters](templates.md#filter-reference), format specs, comments and many
more [variables](variables.md).

## What changed

**The password argument is deprecated.** A password on the command line is
saved in your shell history and visible to other users in `ps`. It still
works, but pihello prints a warning (on stderr; `-q` hides it). Keep it in the
config file, a password file, a password manager or `PIHELLO_PASSWORD`
instead; `pihello init` sets that up ([details](configuration.md#passwords)).

**Missing values no longer crash.** 1.x stopped with an error when a value
was missing (for example `{timer}` whenever no disable timer was running).
pihello 2.0 prints `?` (change it with `--missing`), or `{timer|default("")}`
prints nothing. `--strict` turns missing values into errors that name the
variable.

**Colors only go to terminals.** 1.x always wrote color codes, even into
pipes and files. pihello 2.0 leaves them out when the output isn't a terminal,
and adapts 24-bit colors to terminals with 256 or 16 colors. Add
`--color always` where you relied on the old behaviour, e.g. when writing a
MOTD file. `NO_COLOR` is honoured.

**`-W`, `-H`, `-c` and `-i` work.** 1.x accepted them but ignored them. Long
lines are now wrapped at the terminal width (`-W auto`, the default; 1.x's
default of 80 was never applied). Use `-W 0` to never wrap.

**Small differences.**

- `-ts` without a format prints `2026-10-24 08:30:00`; 1.x added microseconds.
- `\]`, `\}` and `\\` are escapes now (for `]`, `}` and `\`). In 1.x they were
  printed as written, so `\\` gave two backslashes. A backslash before any
  other character is still printed as is.
- `normal` in a style tag resets the style, like `[]`. In 1.x it sent a code
  most terminals ignore.
- Errors exit with status 2 (configuration, template), 3 (Pi-hole
  unreachable), 4 (authentication) or 5 (unexpected answer) instead of 1,
  and come with a hint ([all codes](configuration.md#exit-status)).
- Python 3.10 or newer is required.

**Fixed 1.x bugs.** Hex and `rgb()` colors crashed, as did unknown color
names (now an error with suggestions) and a template ending in a backslash;
underline colors given as hex or `rgb()` were applied to the background.

## Moving to the new setup

1. Upgrade (see above) and run `pihello init`. Use an
   [app password](configuration.md#passwords) (*Settings → Web interface /
   API → Configure app password* in the Pi-hole web interface). Choose the
   `classic` preset to keep the 1.x look, or browse the
   [presets](presets.md) and [themes](themes.md).
2. Using your own template? Replace the `preset` line in the config with
   `template = "~/greeting.txt"`. Check it once with
   `pihello --demo -f ~/greeting.txt --strict`; it reports any variable name
   that doesn't exist. Names from pihello 0.3 were renamed in 1.0: `{status}`
   → `{blocking}`, `{core_current}` → `{version.core.local.version}` (`web_`,
   `FTL_` likewise), `{dns_queries_today}` → `{queries.total}`,
   `{ads_blocked_today}` → `{queries.blocked}`, `{ads_percentage_today}` →
   `{queries.percent_blocked}`, `{domains_being_blocked}` →
   `{gravity.domains_being_blocked}`, `{unique_clients}` → `{clients.active}`,
   `{gravity_last_updated.relative.*}` → `{gravity.relative.*}`.
3. Replace the old line in your shell startup file with `pihello -q`. The
   snippets for bash, zsh, fish and PowerShell are in the
   [README](../README.md#shell-greeting).
4. Run `pihello doctor` if anything looks off.
