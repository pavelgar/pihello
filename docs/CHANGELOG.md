# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.6] - 2020-10-19

### Added

- Added option `-p` `-proto` for pihole instances with HTTPS. Thanks to [@jonzobrist](https://github.com/jonzobrist).
- Added option `-u` `-uri` for setting different path to `/admin`. Thanks to [@jonzobrist](https://github.com/jonzobrist).
- Added some usage examples

### Fixed

- Fixed missing newline after timestamp
- Updated some command line argument descriptions

## [0.2.5] - 2020-10-16

### Added

- Added entrypoint to `pyproject.toml`. Now `pihello` can be run as a standalone command!

## [0.2.4] - 2020-10-16

### Added

- Added repository url and keywords to `pyproject.toml`

### Changed

- Renamed `__main__.py` to `cli.py` for better compatibility as a runnable script

## [0.2.3] - 2020-10-16

### Added

- Added example image  
  ![example output](./example_output.svg)

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
