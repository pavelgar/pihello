# Contributing

## Setup

You need [uv](https://docs.astral.sh/uv/). It installs a suitable Python if you don't have one.

```sh
git clone https://github.com/pavelgar/pihello && cd pihello
uv sync                                          # .venv with pihello (editable) + dev tools
uv tool install pre-commit && pre-commit install # optional: ruff on every commit
```

[`docs/architecture.md`](docs/architecture.md) describes the modules and their contracts.
Read it before changing an interface, and update it in the same change.

## Checks

| Make              | Plain command                                                     |
| :---------------- | :---------------------------------------------------------------- |
| `make fmt`        | `uv run ruff format && uv run ruff check --fix`                   |
| `make lint`       | `uv run ruff format --check && uv run ruff check && uv run mypy`, plus the two `--check`s below |
| `make test`       | `uv run pytest --cov` (CI requires 90 % coverage)                 |
| `make build`      | `uv build` (`tests/test_packaging.py` checks what ships)          |

Unit tests never touch the network or your real config. They replay recorded
Pi-hole responses through `tests/helpers/fakepihole.py`. To try another Python
without touching `.venv`, run `uv run --isolated --python 3.10 pytest`.

## Integration tests

These start real Pi-hole containers, so you need **podman** or **docker**:

```sh
uv run pytest -m integration                     # make test-integration
PIHELLO_IT_RUNTIME=docker PIHELLO_IT_IMAGE=docker.io/pihole/pihole:2026.10.0 uv run pytest -m integration
```

By default the tests use podman and fall back to docker; `PIHELLO_IT_RUNTIME` overrides that.
`PIHELLO_IT_IMAGE` picks the image, `PIHELLO_IT_PORTS` the host ports (default `18400-18499`);
`PIHELLO_IT_URL` + `PIHELLO_IT_PASSWORD` run the read-only tests against an existing Pi-hole.
CI (`.github/workflows/integration.yml`) runs one job per recorded Pi-hole version plus
`latest`, and also runs every Monday. Locally, `make test-matrix`
(`uv run python scripts/test_matrix.py`) runs every recorded version, three at a time, and
prints a summary table. `tests/integration/support.py` explains the ground rules (login rate
limit, session hygiene).

## Recording fixtures

`tests/fixtures/pihole/<docker-tag>/responses.json` holds real API responses,
one set per Pi-hole v6 release. After a new Pi-hole release, record a set and commit it.
The integration matrix picks up the new directory automatically:

```sh
uv run python scripts/record_fixtures.py --help
```

## Generated docs

Some docs are generated, so don't edit them by hand: `docs/variables.md` and the filter
reference in `docs/templates.md` (from `sources.catalog()` and `filters.catalog()`), and the
SVGs under `docs/assets/` plus the galleries `docs/presets.md` / `docs/themes.md` (from demo
data). Regenerate them after changing a variable, filter, preset or theme (`make docs`):

```sh
uv run python scripts/gen_docs.py              # variables + filter reference
uv run python scripts/render_screenshots.py    # galleries + SVGs (make screenshots)
```

Both take `--check` to exit 1 if anything is stale; CI runs them in the lint job.

## Releasing

1. Set `__version__` in `src/pihello/__init__.py` and add a `CHANGELOG.md` entry.
2. Merge to `master`, then publish a GitHub release tagged `v<version>`.
   `.github/workflows/publish.yml` runs CI, checks that the tag matches the version,
   uploads to PyPI (trusted publishing) and attaches the files to the release.
