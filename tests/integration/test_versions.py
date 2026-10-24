"""Per-version behaviour (docs/compatibility.md) seen through pihello.

The matrix (``make test-matrix``) runs this module against every recorded tag.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from pihello import sources

from .support import (
    SETTINGS,
    Pihello,
    Target,
    nullable,
    recorded_versions,
    version_gated,
)

pytestmark = pytest.mark.integration


def lookup(tree: Any, path: str) -> tuple[bool, Any]:
    """``(found, value)`` for a catalog path (``N`` = first list item)."""
    current = tree
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment == "N" and current:
            current = current[0]
        else:
            return False, None
    return True, current


def test_versions_match_the_recording(pihole: Target) -> None:
    recorded = recorded_versions(SETTINGS.tag)
    if recorded is None or pihole.container is None:
        pytest.skip(f"no recorded versions for {SETTINGS.image_ref}")
    version = pihole.versions()
    reported = {c: version[c]["local"]["version"] for c in ("core", "web", "ftl")}
    reported["docker"] = version["docker"]["local"]
    assert reported == recorded  # 2025.02.0 reports FTL "vDev-eaa7dbb"


def test_catalog_matches_this_version(pihello: Pihello, pihole: Target) -> None:
    """Every documented variable is there, except what the catalog says may be missing.

    "(FTL v6.N+)" paths must be missing before v6.N and present from it on;
    "null if …" / "may be empty" paths may be missing; everything else must
    have a value. Items of empty lists and the host's sensors are skipped.
    """
    run = pihello("vars", "--json")
    assert run.returncode == 0, run
    tree = json.loads(run.stdout)
    ftl = pihole.ftl
    gated, optional = version_gated(), nullable()
    problems = []
    for info in sources.catalog():
        path = info.path
        parent = path.split(".N.", 1)[0] if ".N." in path else None
        if parent is not None and not lookup(tree, parent)[1]:
            assert pihole.seeded is None or parent.startswith("sensors."), f"{parent} is empty"
            continue  # an empty list has no items to check
        if path.startswith("sensors.list.N."):
            continue  # the host's hwmon devices, not Pi-hole's business
        found, value = lookup(tree, path)
        present = found and value is not None
        since = gated.get(path)
        if since is not None:
            if present != (ftl[:2] >= since):
                problems.append(
                    f"{path}: documented for FTL v{since[0]}.{since[1]}+, present={present}"
                )
        elif not present and path not in optional:
            problems.append(f"{path}: missing ({info.description})")
    assert not problems, f"FTL {ftl}:\n" + "\n".join(problems)


def test_database_counts(pihello: Pihello, pihole: Target) -> None:
    """FTL v6.3 changed ftl.database.{domains,regex}.* from numbers to {total, enabled}."""
    if pihole.seeded is None:
        pytest.skip("needs the seeded list entries")
    raw = pihole.get("info/ftl")["ftl"]["database"]["domains"]["denied"]
    assert isinstance(raw, dict) == (pihole.ftl[:2] >= (6, 3)), raw
    run = pihello("vars", "--json", "ftl.database.*")
    assert run.returncode == 0, run
    flat = json.loads(run.stdout)
    seed = pihole.seeded
    expected = {
        "domains.denied": len(seed.deny_exact),
        "regex.denied": len(seed.deny_regex),
        "domains.allowed": len(seed.allow_exact),
        "regex.allowed": 0,
    }
    for key, count in expected.items():
        assert flat[f"ftl.database.{key}.total"] == count, flat
        if pihole.ftl[:2] >= (6, 3):
            assert flat[f"ftl.database.{key}.enabled"] == count, flat
        else:
            assert f"ftl.database.{key}.enabled" not in flat, flat


def test_wrong_method_and_unknown_endpoint(pihole: Target) -> None:
    resp = pihole.observe("PUT", "info/version")
    if pihole.ftl >= (6, 7, 1):
        assert (resp.status, resp.body["error"]["key"]) == (405, "method_not_allowed"), resp.text
    else:
        assert (resp.status, resp.body["error"]["key"]) == (404, "not_found"), resp.text
    for sid in (None, "x"):
        resp = pihole.api("GET", "does/not/exist", sid=sid)
        assert resp.status == 404, resp.text
        assert resp.body["error"]["hint"] == "/api/does/not/exist"


def test_sensors_in_a_container(pihole: Target) -> None:
    sensors = pihole.get("info/sensors")["sensors"]
    assert sensors["unit"] in ("C", "F", "K")
    # What FTL sees in a container depends on the host's hwmon devices (and on the FTL
    # version), so only the shape is checked; presets must cope with an empty list.
    assert isinstance(sensors["list"], list)
    assert sensors["cpu_temp"] is None or isinstance(sensors["cpu_temp"], (int, float))


# --------------------------------------------------------------------------- #
# info/version right after start-up: 500 or a partial file                     #
# --------------------------------------------------------------------------- #
PARTIAL_VERSIONS = "CORE_VERSION=\nCORE_BRANCH=master\nGITHUB_DOCKER_VERSION=null\n"


def test_info_version_failures_degrade(pihello: Pihello, pihole: Target) -> None:
    container = pihole.require_container()
    path = "/etc/pihole/versions"
    container.exec("mv", path, path + ".saved")
    try:
        resp = pihole.observe("GET", "info/version")
        assert resp.status == 500, resp.text  # what FTL answers until the file exists

        run = pihello("--color", "never")
        assert run.returncode == 0, run
        assert run.stderr == "", run
        assert run.lines[2].strip() == "Versions   Core ? · Web ? · FTL ?", run
        for preset in ("full", "dashboard", "fetch", "box"):
            other = pihello("--preset", preset, "--color", "never")
            assert other.returncode == 0, other

        strict = pihello("--strict", "--color", "never")
        assert strict.returncode == 5, strict
        assert "info/version" in strict.stderr, strict
        assert "HTTP 500" in strict.stderr, strict

        doctor = pihello("doctor", "--color", "never", logins=2)
        assert doctor.returncode == 5, doctor
        assert "✗ versions (info/version): Pi-hole returned HTTP 500" in doctor.stdout, doctor
        assert "logged out; no session left behind" in doctor.stdout, doctor

        container.exec("sh", "-c", f"printf '{PARTIAL_VERSIONS}' > {path}")
        partial = pihole.observe("GET", "info/version").body["version"]
        assert partial["core"]["local"]["version"] in (None, ""), partial  # "" up to v6.0
        ftl = partial["ftl"]["local"]["version"] or "?"  # FTL knows its own version
        run = pihello("--color", "never")
        assert run.returncode == 0, run
        assert run.lines[2].strip() == f"Versions   Core ? · Web ? · FTL {ftl}", run
        updates = pihello("--preset", "updates", "--color", "never")
        assert updates.returncode == 0, updates
        assert re.search(r"^Core +- +- +unknown$", updates.stdout, re.MULTILINE), updates
        strict = pihello("--strict", "--color", "never")
        assert strict.returncode == 2, strict
        assert "variable 'version.core.local.version' has no value" in strict.stderr, strict
    finally:
        container.exec("mv", "-f", path + ".saved", path)
    assert pihole.observe("GET", "info/version").status == 200
