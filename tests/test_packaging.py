"""Build the sdist and wheel once and check what actually ships.

Guards the packaging configuration in ``pyproject.toml``: the wheel contains
the whole package (``py.typed``, preset templates, ...) and nothing else, its
metadata is right, and the sdist carries what downstream packagers need to run
the test suite. Skipped when neither ``uv`` nor ``build`` is available or when
not running from a source checkout.
"""

from __future__ import annotations

import configparser
import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

import pihello

if TYPE_CHECKING:
    from email.message import Message

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "src" / "pihello"

pytestmark = pytest.mark.skipif(
    not (ROOT / "pyproject.toml").is_file() or not PACKAGE_DIR.is_dir(),
    reason="packaging tests need a source checkout",
)


@dataclass(frozen=True)
class Dists:
    wheel: Path
    sdist: Path
    wheel_names: frozenset[str]
    sdist_names: frozenset[str]  # relative to the top-level "pihello-<version>/" directory
    metadata: Message
    dist_info: str


def _build_command(outdir: Path) -> list[str] | None:
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv:
        # Builds the sdist, then the wheel *from the sdist*, so this also
        # proves the sdist is complete enough to build from.
        return [uv, "build", "--no-sources", "--out-dir", str(outdir), str(ROOT)]
    if importlib.util.find_spec("build") is not None:
        return [sys.executable, "-m", "build", "--outdir", str(outdir), str(ROOT)]
    return None


@pytest.fixture(scope="module")
def dists(tmp_path_factory: pytest.TempPathFactory) -> Dists:
    outdir = tmp_path_factory.mktemp("dist")
    command = _build_command(outdir)
    if command is None:
        pytest.skip("neither uv nor the 'build' package is available")
    proc = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"{' '.join(command)} failed:\n{proc.stdout}\n{proc.stderr}")
    (wheel,) = outdir.glob("*.whl")
    (sdist,) = outdir.glob("*.tar.gz")

    with zipfile.ZipFile(wheel) as zf:
        wheel_names = frozenset(zf.namelist())
        (dist_info,) = {n.split("/")[0] for n in wheel_names if ".dist-info/" in n}
        metadata = Parser().parsestr(zf.read(f"{dist_info}/METADATA").decode())
    with tarfile.open(sdist) as tf:
        top = sdist.name.removesuffix(".tar.gz") + "/"
        sdist_names = frozenset(m.name.removeprefix(top) for m in tf.getmembers() if m.isfile())
    return Dists(wheel, sdist, wheel_names, sdist_names, metadata, dist_info)


def _source_files(base: Path) -> set[str]:
    """Files under ``base`` that belong in a distribution (no caches/editor junk)."""
    return {
        p.relative_to(base).as_posix()
        for p in base.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and not p.name.startswith(".")
        and p.suffix not in {".pyc", ".pyo", ".swp", ".orig", ".rej"}
        and not p.name.endswith("~")
    }


# --------------------------------------------------------------------------- #
# wheel
# --------------------------------------------------------------------------- #


def test_wheel_is_pure_python(dists: Dists) -> None:
    assert dists.wheel.name == f"pihello-{dists.metadata['Version']}-py3-none-any.whl"


def test_wheel_contains_the_whole_package(dists: Dists) -> None:
    expected = {f"pihello/{name}" for name in _source_files(PACKAGE_DIR)}
    missing = expected - dists.wheel_names
    assert not missing, f"files missing from the wheel: {sorted(missing)}"
    assert "pihello/py.typed" in dists.wheel_names
    assert "pihello/__main__.py" in dists.wheel_names


def test_wheel_contains_preset_templates(dists: Dists) -> None:
    presets = sorted((PACKAGE_DIR / "presets").glob("*.txt"))
    if not presets:
        pytest.skip("no preset templates in src/pihello/presets yet")
    shipped = {n for n in dists.wheel_names if n.startswith("pihello/presets/")}
    for preset in presets:
        assert f"pihello/presets/{preset.name}" in shipped


def test_wheel_contains_only_the_package(dists: Dists) -> None:
    unexpected = {
        n
        for n in dists.wheel_names
        if not (n.startswith(("pihello/", f"{dists.dist_info}/")))
        or "__pycache__" in n
        or n.endswith((".pyc", ".pyo"))
    }
    assert not unexpected, f"unexpected files in the wheel: {sorted(unexpected)}"


def test_wheel_ships_the_license(dists: Dists) -> None:
    assert f"{dists.dist_info}/licenses/LICENSE" in dists.wheel_names


def test_wheel_metadata(dists: Dists) -> None:
    meta = dists.metadata
    assert meta["Name"] == "pihello"
    assert Version(meta["Version"]) == Version(pihello.__version__)
    assert meta["Requires-Python"] == ">=3.10"
    assert meta["License-Expression"] == "MIT"
    assert meta["Description-Content-Type"] == "text/markdown"
    assert meta["Summary"]
    assert len(meta.get_payload()) > 500, "README missing from the long description"


def test_no_runtime_dependencies_on_python_311_and_later(dists: Dists) -> None:
    for spec in dists.metadata.get_all("Requires-Dist") or []:
        requirement = Requirement(spec)
        assert requirement.marker is not None, f"unconditional dependency: {spec}"
        for python in ("3.11", "3.12", "3.13", "3.14"):
            environment = {"python_version": python, "python_full_version": f"{python}.0"}
            assert not requirement.marker.evaluate(environment), f"{spec} applies to {python}"


def test_console_script_entry_point(dists: Dists) -> None:
    with zipfile.ZipFile(dists.wheel) as zf:
        text = zf.read(f"{dists.dist_info}/entry_points.txt").decode()
    entry_points = configparser.ConfigParser()
    entry_points.read_string(text)
    assert dict(entry_points["console_scripts"]) == {"pihello": "pihello.cli:main"}


# --------------------------------------------------------------------------- #
# sdist
# --------------------------------------------------------------------------- #


def test_sdist_has_project_files(dists: Dists) -> None:
    for name in (
        "PKG-INFO",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "docs/architecture.md",
        "src/pihello/__init__.py",
        "src/pihello/py.typed",
    ):
        assert name in dists.sdist_names, f"{name} missing from the sdist"


def test_sdist_has_the_test_suite(dists: Dists) -> None:
    expected = {f"tests/{name}" for name in _source_files(ROOT / "tests")}
    missing = expected - dists.sdist_names
    assert not missing, f"test files missing from the sdist: {sorted(missing)}"


def test_sdist_excludes_local_clutter(dists: Dists) -> None:
    unwanted = {
        n
        for n in dists.sdist_names
        if n.startswith((".git/", ".github/", ".venv/", "dist/", "build/"))
        or "__pycache__" in n
        or n.endswith(".pyc")
    }
    assert not unwanted, f"unexpected files in the sdist: {sorted(unwanted)}"
