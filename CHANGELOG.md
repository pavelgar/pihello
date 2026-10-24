# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - Unreleased

A rewrite.
pihello 1.x command lines and templates keep working; see [Upgrading from 1.x](docs/upgrading.md).

### Added

- A config file (`~/.config/pihello/config.toml`, `%APPDATA%\pihello\config.toml` on Windows) with profiles (`-P NAME`), `[styles]` overrides and `PIHELLO_*` environment variables.
  `pihello doctor` shows where every setting comes from.
- `pihello init`: interactive setup that tests the address and password, previews presets and themes, writes the config (mode 600) and prints the snippet for bash, zsh, fish or PowerShell.
  `--yes` for unattended setup.
- `pihello doctor`: step-by-step diagnostics of config, DNS, TCP, HTTPS, login, versions, data sources and session clean-up.
- `pihello presets`, `pihello themes`, `pihello vars` (with `--json` for scripts), `pihello colors` and `pihello completion {bash,zsh,fish}`.
- `pihello presets NAME --source -o FILE` copies a preset into a new template file to start your own (UTF-8, creates folders, never overwrites).
  Template files saved as UTF-16 (Windows PowerShell 5.1's `>`) are read too.
- 15 presets (including `classic`, the exact 1.x look, and `status` for tmux and status bars) and 11 color themes.
- Template language: theme styles (`[@good]`), conditional styling (`[{blocking|map(enabled=@good, _=@bad)}]`), 44 filters (numbers, bytes, durations, `ago`, dates, padding, lists, sparklines, bars, comparisons), Python format specs, list indices, string literals, comments and `\]`, `\}`, `\\` escapes.
- 245 documented variables from 17 API endpoints (system, host, sensors, FTL, database, top lists, upstreams, 24 h history, network, update status, …).
  Only the endpoints a template uses are fetched, in parallel.
- Password options that keep it out of the command line: `password_file`, `password_command` (string or list), `--password-stdin`, `PIHELLO_PASSWORD`.
- `--ca-cert` / `verify_tls = "ca.crt"` to verify Pi-hole's self-signed certificate.
- `--watch` live view, `--cache` for faster shell start, `--demo` sample data, `-e` inline templates, `-f -` (stdin), `--style`, `--missing`, `--strict`, `--timeout`, `-q`, `--debug`.
- Colors adapt to the terminal (24-bit, 256 or 16 colors, none when piped); `--color`, `NO_COLOR` and `FORCE_COLOR`.
- Distinct exit codes: 2 usage/config/template, 3 unreachable, 4 authentication, 5 unexpected API response.
- `python -m pihello`; type information (`py.typed`).

### Changed

- Requires Python 3.10 or newer.
- A password given as a positional argument still works but prints a warning: it leaks into shell history and `ps`.
- `pihello` with a config file shows the new `default` preset; `pihello ADDR PASSWORD` without a configured template still shows the 1.x greeting (`classic`).
- Colors are only written to terminals (use `--color always` for pipes and files).
- Long lines wrap at the terminal width by default (`-W auto`); `-H` defaults to no limit; `-i`/`--indent` is the tab stop width.
- Errors are reported on stderr as one line with a hint and a specific exit status, instead of raw messages and tracebacks.
- `-ts` without a format no longer prints microseconds.
- `normal` in a style tag resets the style.
- Redirects are never followed; an HTTP→HTTPS redirect is reported with the `https://` URL to use.
  The session id is sent in the `X-FTL-SID` header.
- The project uses a `src/` layout, hatchling and uv instead of Poetry.
  `LICENCE` is now `LICENSE`, and this changelog moved to the repository root.

### Fixed

- Hex (`#ff8800`) and `rgb()` colors crashed.
- Unknown color names crashed; they are now an error with suggestions.
- A missing value (for example `{timer}` while no disable timer was running) crashed; it now renders as `?`.
- `-W`, `-H`, `-c` and `-i` were accepted but never implemented.
- A template ending in a backslash crashed.
- `rgb()` and hex underline colors were applied to the background.
- Connection and HTTP errors were printed to standard output, into the greeting.
- Output crashed with `UnicodeEncodeError` when the terminal's encoding isn't UTF-8 (e.g. a latin-1 locale); characters it can't show are now printed as `?` ([#6](https://github.com/pavelgar/pihello/issues/6)).

### Removed

- The `pihello.Console` class; the template engine is now `pihello.template`.
- `docs/SHOWCASE.md`, `docs/example.txt` and `docs/example_output.svg`, replaced by the [preset gallery](docs/presets.md).

## [1.0.0] - 2026-07-24

Pi-hole v6 support.
Thanks to [@scul86](https://github.com/scul86)!

### Changed (BREAKING)

- Targets the Pi-hole v6 REST API; Pi-hole v5 is no longer supported (use `pihello==0.3.0` for v5).
- The second positional argument is now your Pi-hole **app password** (Settings → Web interface / API) instead of the v5 API token.
- Template variables renamed to match the v6 API (see the README table), e.g. `{status}` → `{blocking}`, `{core_current}` → `{version.core.local.version}`, `{gravity_last_updated.relative.*}` → `{gravity.relative.*}`.
- Removed `-u`/`--uri` — the v6 API lives at a fixed `/api` path.

### Added

- `-k`/`--insecure` flag to skip TLS certificate verification for self-signed Pi-hole certs (verification is now ON by default).

### Fixed

- Failed authentication exits with code 1 instead of continuing and crashing.
- The API session is always closed on exit, even when a data fetch fails (Pi-hole v6 has a limited number of concurrent sessions).
- The session id is sent as a request header rather than in the URL query string.

## [0.3.0] - 2023-02-13

### Added

- Added initial Pi-hole API token support. Thanks to [@WillPresley](https://github.com/WillPresley)!  
  Now the `pihello` command has second required parameter: `token`.

## [0.2.7] - 2022-09-08

### Changed

- No new features added
- Bumping versions of dev-dependencies

## [0.2.6] - 2020-10-19

### Added

- Added option `-p` `-proto` for pihole instances with HTTPS.
  Thanks to [@jonzobrist](https://github.com/jonzobrist).
- Added option `-u` `-uri` for setting different path to `/admin`.
  Thanks to [@jonzobrist](https://github.com/jonzobrist).
- Added some usage examples

### Fixed

- Fixed missing newline after timestamp
- Updated some command line argument descriptions

## [0.2.5] - 2020-10-16

### Added

- Added entrypoint to `pyproject.toml`.
  Now `pihello` can be run as a standalone command!

## [0.2.4] - 2020-10-16

### Added

- Added repository url and keywords to `pyproject.toml`

### Changed

- Renamed `__main__.py` to `cli.py` for better compatibility as a runnable script

## [0.2.3] - 2020-10-16

### Added

- Added example image

### Fixed

- Fixed comparison between variables and None
- `--version` flag now displays the current version correctly

## [0.2.2] - 2020-10-15

### Added

- Added ability to read from file
- Added example script (`example.txt`)
- Added `normal` style (ANSI code 10)

### Changed

- No extra newline at the end when reading from file
- Pass keyword arguments to Python's print function instead

## [0.2.1] - 2020-10-12

### Added

- Added some project metadata to `pyproject.toml` for PyPI
- Created this `CHANGELOG.md`

## [0.2.0] - 2020-10-12

### Added

- Generated .gitignore from https://gitignore.io
- Added variable injection
- Added supporting documentation in README.md
- Began using [poetry](https://python-poetry.org) to manage the project
- Made the project into a callable module

### Changed

- Massive improvements on style parsing

### Removed

- `main.py` as this is now mostly in `__main__.py`
- `makefile` as this is not yet used
- `.vscode/`

## [0.1.0] - 2020-09-30

Initial prototype

### Added

- README.md
- LICENCE
- .gitignore
- Proof of concept on how to create this command line utility.

[2.0.0]: https://github.com/pavelgar/pihello/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/pavelgar/pihello/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/pavelgar/pihello/compare/v0.2.7...v0.3.0
[0.2.7]: https://github.com/pavelgar/pihello/compare/0.2.6...v0.2.7
[0.2.6]: https://github.com/pavelgar/pihello/releases/tag/0.2.6
[0.2.5]: https://pypi.org/project/pihello/0.2.5/
[0.2.4]: https://pypi.org/project/pihello/0.2.4/
[0.2.3]: https://pypi.org/project/pihello/0.2.3/
[0.2.2]: https://pypi.org/project/pihello/0.2.2/
[0.2.1]: https://pypi.org/project/pihello/0.2.1/
[0.2.0]: https://pypi.org/project/pihello/0.2.0/
[0.1.0]: https://github.com/pavelgar/pihello/tree/f3659fc9549d270b665845955052f4fb9d032481
