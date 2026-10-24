# Templates

A template is a plain UTF-8 text file that pihello prints as is, except for three things:

- **Style tags** in square brackets: `[bold steelblue]`, `[@good]`, `[]`
- **Expressions** in curly braces: `{queries.total|number}`
- **Comments**: `{# ... #}`

```text
[@title]Pi-hole[] is [{blocking|map(enabled=@good, _=@bad)}]{blocking}[]
Blocked [@highlight]{queries.blocked|number}[] of {queries.total|number} queries today
```

Output:

```text
Pi-hole is enabled
Blocked 7,099 of 48,220 queries today
```

All examples on this page are rendered with pihello's demo data (`--demo`) and shown without colors.
pihello 1.x template files keep working; [What changed](upgrading.md#what-changed) lists the few small differences.
The references list every [filter](filters.md) and every [variable](variables.md).

**On this page:** [Start from a preset](#start-from-a-preset) · [Style tags](#style-tags) · [Expressions](#expressions) ·
[Missing values](#missing-values) · [Escapes and comments](#escapes-and-comments) · [Lines, width and tabs](#lines-width-and-tabs) ·
[Colors](#colors) · [Errors](#errors) · [Examples](#examples)

## Start from a preset

The quickest way to your own greeting is to copy a built-in preset and edit it:

```sh
pihello presets default --source -o ~/.config/pihello/greeting.txt   # copy a preset
pihello --demo -f ~/.config/pihello/greeting.txt --strict            # try it with demo data
pihello -f ~/.config/pihello/greeting.txt                            # and with your Pi-hole
```

Then make it permanent in the [config file](configuration.md):

```toml
# ~/.config/pihello/config.toml
template = "~/.config/pihello/greeting.txt"   # instead of preset = "..."
```

Tips for iterating:

- `--demo` renders with built-in sample data, so you don't need a Pi-hole (or the network) while editing.
- `--strict` turns a typo into an error that names the line, the column and a likely fix, instead of printing `?`.
- `pihello vars --demo` lists every variable with an example value; `pihello vars` shows your own values.
- For one-liners, `-e` takes the template on the command line: `pihello --demo -e '{queries.total|number} queries'`.
- `-f -` reads the template from standard input.

The [preset gallery](presets.md) shows every preset with its source.

## Style tags

A style tag sets the style of the text that follows it.
Write the words in any order, separated by spaces; case doesn't matter.

```text
[bold steelblue]Pi-hole[] [italic grey50]on[] [white on navy] pi.hole []
```

- A tag **replaces** the current style; it doesn't add to it. `[bold]a[italic]b` prints `a` bold and `b` italic but not bold.
- `[]` goes back to the terminal's default style.
- A style stays active across line breaks until the next tag.

| Word | Description |
| :-- | :-- |
| `bold` | Bold |
| `dim`, `faint` | Dim |
| `italic` | Italic |
| `underline` | Underline |
| `dunderline` | Double underline (many terminals draw a single one) |
| `strike` | Strike-through |
| `overline` | Line above the text |
| `reverse` | Swap foreground and background |
| `blink`, `fblink` | Slow / fast blink (rarely supported) |
| `hidden` | Invisible text |
| `normal`, `reset` | Clear everything before it in the same tag (same as `[]` on its own) |
| *color* | Foreground color |
| `on` *color* or `:`*color* | Background color: `[white on navy]`, `[:navy]` |
| `_`*color* | Underline color, used with `underline`: `[underline _red]` |
| `@`*name* | The style *name* from the active theme; see [Theme styles](#theme-styles) |

A *color* is one of these:

| Form | Example | Description |
| :-- | :-- | :-- |
| An xterm color name | `steelblue`, `grey37`, `dark_slate_gray2` | `pihello colors` lists all of them. `_`, `-` and `gray`/`grey` spellings are accepted. |
| `color(N)` | `color(45)` | Palette number 0–255 |
| `#rrggbb` or `#rgb` | `#ff8800`, `#f80` | 24-bit color |
| `rgb(r, g, b)` | `rgb(255, 136, 0)` | 24-bit color, spaces allowed |
| `default` | `[default on default]` | The terminal's default color |

Names follow the xterm palette, so `red` is the bright red (palette number 9) and `maroon` the darker one (1).
Colors 0–15 come out in your terminal's own palette, everything else as the exact color.
[Colors](#colors) explains how pihello adapts them to the terminal.

### Theme styles

Presets don't hard-code colors.
They use the twelve semantic styles of the active [theme](themes.md):
`@title`, `@subtitle`, `@border`, `@label`, `@value`, `@muted`, `@accent`, `@good`, `@warn`, `@bad`, `@info` and `@highlight`.
Do the same in your templates, and they'll follow `--theme` and your [`[styles]` overrides](configuration.md#styles):

```text
[@label]Queries [@accent]{queries.total|number}[@muted] today
```

`@name` can be mixed with other words, e.g. `[@good underline]`.
An unknown name is an error.
[Style names](themes.md#style-names) describes what each style is meant for.

### Conditional styling

An expression inside a tag is evaluated first, and its result is read as style words.
That's how presets turn the status dot green or red:

```text
[{blocking|map(enabled=@good, disabled=@bad, _=@warn)}]●[] {blocking}
[{system.cpu.load.percent.0|gt(80)|if(@bad, @good)}]load {system.cpu.load.percent.0|percent}
[{sensors.cpu_temp|gt(60)|if("bold red", "green")}]{sensors.cpu_temp}°C
```

[`map`](filters.md#logic) picks a style by value (`_` is the fallback) and [`if`](filters.md#logic) picks one of two.
Filter arguments are literals, so the threshold has to be written out (`gt(60)`, not `gt(sensors.hot_limit)`).
Words produced by data that aren't valid styles are skipped (with `--strict` they're an error).

## Expressions

```text
{ head | filter | filter(arg, key=value) :format_spec }
```

Spaces around the parts are allowed: `{ queries.total | number }`.

### Variable paths

The head is usually a variable path: keys separated by dots.
Keys may contain letters, digits, `_`, `-` and `%` (`{system.memory.ram.%used}`).

- **List items** are numbered from 0: `{top_domains.0.domain}`. Negative numbers count from the end: `{top_domains.-1.domain}` is the last item.
- **Groups** (objects such as `queries` or `top_domains.0`) can't be printed as a whole; pick a key, e.g. `{top_domains.0.domain}`.
- `pihello vars` and [Variables](variables.md) list every path.

### Literal heads

Instead of a variable, the head can be a quoted string or a number.
That's mostly useful with filters:

| Expression | Output |
| :-- | :-- |
| `{"─"\|repeat(40)}` | A 40-column rule |
| `{'Queries'\|ljust(10)}` | `Queries` padded to 10 columns |
| `{42}` | `42` |

In strings, `\"` (or `\'` in single quotes) is the quote character, `\\` a backslash, `\n` a newline and `\t` a tab.

### Filters

Filters transform the value from left to right: `{recent_blocked_list|take(3)|join(" · ")}`.
Arguments are written in parentheses, by position or by name, and can be:

- strings: `"…"` or `'…'` (quote anything with spaces, commas or parentheses)
- numbers: `10`, `-1`, `0.5`
- `true`, `false`, `none` (or `null`)
- bare words, which are strings: `bytes(KiB)`, `if(@warn, @muted)`

Arguments are always literals; they can't refer to other variables.
Unknown filters and wrong arguments are reported when the template is read, before anything is fetched.
[Filters](filters.md) lists every filter.

### Format specs

A `:` followed by a [Python format spec](https://docs.python.org/3/library/string.html#format-specification-mini-language) formats the final value.
It's applied after all filters, so it can also pad a filter's text output:

| Expression | Output |
| :-- | :-- |
| `{queries.percent_blocked:.2f}` | `14.72` |
| `{queries.total:,}` | `48,220` |
| `{queries.total:>10}` | `     48220` |
| `{queries.total\|number:>10}` | `    48,220` |
| `{blocking:*^13}` | `***enabled***` |
| `{system.cpu.load.raw.0:.2f}` | `0.21` |

A `:` inside quotes or parentheses belongs to the filter arguments.

### How values are printed

Without a filter or format spec:

- whole numbers as they are: `48220` (use `number` for `48,220`)
- decimals with one digit: `14.7` (as in pihello 1.x)
- `true`/`false` as `yes`/`no`
- lists as their items joined with `, `
- strings as they are

Terminal escape sequences and other control characters in Pi-hole's data (except tabs and newlines) are removed,
so a hostile domain name can't send escape sequences to your terminal or throw off the alignment of padded columns.
A value that contains a newline continues on the next output line.

## Missing values

A value is *missing* when the path doesn't exist, the Pi-hole sent `null`, the endpoint isn't available,
or a filter can't handle the value (e.g. `number` on a word).
Missing values never crash the greeting:

- They print as `?`. Change that with `--missing TEXT` or `missing = "n/a"` in the config file (an empty string hides them).
- Filters pass a missing value through untouched, except `default`, `if`, `not` and the comparisons (`eq`, `gt`, …), which treat it as false.
- `default(x)` replaces a missing value (or an empty string) with `x`: `{timer|duration|prefix(" for ")|default("")}` prints nothing when there's no disable timer.
- `--strict` (or `strict = true`) turns a missing value into an error with a suggestion, e.g. `variable 'queries.totl' is not defined` / `did you mean 'queries.total'?`.
  Expressions that use `default` never count as missing.

## Escapes and comments

| Write | To get |
| :-- | :-- |
| `\[` `\]` | `[` `]` |
| `\{` `\}` | `{` `}` |
| `\\` | `\` |

Any other backslash is kept as is (`C:\path` works), and a lone `]` or `}` is literal too.
Remember to escape brackets around values: `[{queries.total}]` is a style tag, `\[{queries.total}\]` prints `[48220]`.

`{# ... #}` is a comment and may span several lines.
A line that contains nothing but comments and spaces disappears completely (including its line break), so you can document a template without leaving blank lines:

```text
{# My greeting. Needs 60 columns. #}
[@title]Pi-hole[] {version.core.local.version}
```

## Lines, width and tabs

- **Lines**: the output has exactly the lines of the template. One trailing newline at the end of the file is ignored.
  A line whose expressions are all empty still prints as an empty line.
- **Width**: by default (`--width auto`), long lines are wrapped at the terminal width.
  When the output isn't a terminal (a pipe, a file, a status bar) there's no limit.
  `-W N` / `width = N` sets a fixed width, and `-W 0` means no limit.
- **Wrap or clip**: lines are wrapped at the last space that fits (words longer than the line are split).
  `-c` / `--clip` (or `overflow = "clip"`) cuts them off instead.
  Styles continue on wrapped lines.
- **Height**: `-H N` / `height = N` prints at most N lines, counting the `--timestamp` line. `0` means no limit.
- **Tabs** are expanded to the next multiple of `-i N` / `--tab-size N` columns (default 4), which makes simple columns easy.
- **Character widths** are counted the way terminals do: escape sequences and combining marks take no space,
  and East Asian wide characters (and the emoji Unicode classifies as wide) take two columns.
  Filters such as `truncate`, `ljust` and `center` use the same rules.

## Colors

pihello writes colors only where they work:

| `--color` / `color =` | Description |
| :-- | :-- |
| `auto` (default) | Colors on a terminal, none when piped or redirected; the depth (16, 256 or 24-bit colors) is detected |
| `always` | Like `auto`, but colors even when piped (and despite `NO_COLOR`) |
| `never` (or `--no-color`) | No escape sequences at all |
| `16`, `256`, `truecolor` | That many colors; on or off like `auto` (still none when piped or with `NO_COLOR`) |

In `auto` mode, a non-empty `NO_COLOR` turns colors off, and `FORCE_COLOR` (or `CLICOLOR_FORCE=1`) turns them on even when piped.
`FORCE_COLOR=2` and `FORCE_COLOR=3` also select 256 and 24-bit colors.
The depth is guessed from `COLORTERM`, `TERM` and known terminals; set `--color truecolor` if your terminal supports more than pihello detects.
To force a particular depth into a pipe, add `FORCE_COLOR=1`:

```sh
FORCE_COLOR=1 pihello --color 256 | less -R
```

24-bit colors are converted to the nearest of the 256 palette colors, and those to the nearest of the 16 basic ones, as the terminal requires.
Underline colors are dropped at 16 colors.

## Errors

Template mistakes stop pihello with exit status 2 and a message that points at the problem:

```console
$ pihello --demo -e '{queries.total|nubmer}'
pihello: --template-string:1:16: unknown filter 'nubmer'
    1 | {queries.total|nubmer}
      |                ^
hint: did you mean 'number'?
```

Syntax errors (unclosed brackets, unknown filters or style words, bad filter arguments or format specs) are always errors.
Missing values are only errors with `--strict`.

## Examples

Each example is a template file rendered with `pihello --demo --color never -f example.txt` (and `TZ=UTC`).

### Status bar segment

```text
[{blocking|map(enabled=@good, disabled=@bad, _=@warn)}]●[] {queries.percent_blocked|percent} blocked of {queries.total|compact}[@warn]{timer|duration|prefix(" · off for ")|default("")}
```

Output:

```text
● 14.7% blocked of 48.2k
```

While blocking is disabled with a timer, `· off for 4m 59s` is appended.

### Aligned numbers

```text
[@label]Queries  [@accent]{queries.total|number:>8}
[@label]Blocked  [@highlight]{queries.blocked|number:>8} [@muted]({queries.percent_blocked|percent})
[@label]Domains  [@info]{gravity.domains_being_blocked|number:>8}
[@label]Clients  [@accent]{clients.active:>8}[@muted] of {clients.total}
```

Output:

```text
Queries    48,220
Blocked     7,099 (14.7%)
Domains   121,837
Clients        12 of 14
```

### Updates, only when there are some

```text
[@title]Pi-hole {version.core.local.version}[@muted] · FTL {version.ftl.local.version}
[@warn]{updates.available|if("↑ updates for: ")}{updates.components|join(", ")}
```

Output:

```text
Pi-hole v6.5 · FTL v6.7
↑ updates for: ftl
```

When everything is up to date, the second line is empty.

### Top list

```text
[@subtitle]Most blocked today
[@muted] 1 [@highlight]{top_blocked.0.domain|truncate(30)|ljust(30)} [@accent]{top_blocked.0.count|number:>5}
[@muted] 2 [@highlight]{top_blocked.1.domain|truncate(30)|ljust(30)} [@accent]{top_blocked.1.count|number:>5}
[@muted] 3 [@highlight]{top_blocked.2.domain|truncate(30)|ljust(30)} [@accent]{top_blocked.2.count|number:>5}
[@muted]Recently: {recent_blocked_list|take(3)|join(", ")|truncate(48)}
```

Output:

```text
Most blocked today
 1 pagead2.googlesyndication.com    812
 2 googleads.g.doubleclick.net      655
 3 app-measurement.com              591
Recently: googleads.g.doubleclick.net, app-measurement.co…
```

### History on one scale

`history` has one entry per 10 minutes.
`pluck` takes one number from each entry, `spark(48, …)` squeezes them into 48 cells, and the fixed `min`/`max` make both lines comparable:

```text
[@label]total   [@accent]{history|pluck(total)|spark(48, min=0, max=600)}[@muted] max {history|pluck(total)|max}
[@label]blocked [@highlight]{history|pluck(blocked)|spark(48, min=0, max=600)}[@muted] max {history|pluck(blocked)|max}
```

Output:

```text
total   ▆▆▆▆▆▆▆▆▆▆▆▅▆▆▆▆▆▆▇▇▇▇█▇▇▇▆▆▅▄▄▃▃▂▂▂▂▂▂▂▂▂▃▃▄▅▅▅ max 586
blocked ▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▂▂▂ max 88
```

### System health

```text
[@label]Memory [@value]{system.memory.ram.used|bytes(KiB)}[@muted] of {system.memory.ram.total|bytes(KiB)} [@accent]{system.memory.ram.%used|bar(20)} [@value]{system.memory.ram.%used|percent(0)}
[@label]Uptime [@value]{system.uptime|duration(long)}[@muted] · load {system.cpu.load.raw.0:.2f}
[@label]Temp   [{sensors.cpu_temp|gt(60)|if(@bad, @good)}]{sensors.cpu_temp|round(1)|default("n/a")}°{sensors.unit|default("C")}
[@label]Gravity[@value] {gravity.last_update|ago}[@muted] ({gravity.last_update|datetime("%a %d %b %H:%M")})
```

Output:

```text
Memory 1.1 GiB of 7.9 GiB ██▉░░░░░░░░░░░░░░░░░ 14%
Uptime 18 days, 6 hours · load 0.21
Temp   52.1°C
Gravity 3d 4h ago (Wed 21 Oct 04:17)
```

Memory values are in KiB, hence `bytes(KiB)`.
`datetime` uses the local time zone.

### Log line for scripts

```text
{now|datetime("%Y-%m-%dT%H:%M")} blocking={blocking} total={queries.total} blocked={queries.blocked} pct={queries.percent_blocked:.2f}
```

Output:

```text
2026-10-24T08:30 blocking=enabled total=48220 blocked=7099 pct=14.72
```

Piped output has no colors, so `pihello -q -f log.txt >> pihole.log` from cron gives clean lines.
For structured data, use [`pihello vars --json`](commands.md#pihello-vars).
