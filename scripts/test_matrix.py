"""Run the integration tests against every recorded Pi-hole version and print a summary.

    uv run python scripts/test_matrix.py                 # every tag, 3 at a time
    uv run python scripts/test_matrix.py --jobs 1 --tag 2025.02.0 --tag 2026.10.0
    uv run python scripts/test_matrix.py -- -k auth -x   # extra pytest arguments

The tags are the ones recorded in tests/fixtures/pihole/index.json. Each tag
runs ``pytest -m integration tests/integration`` with
``PIHELLO_IT_IMAGE=<repo>:<tag>``; parallel runs get disjoint slices of the host
port range (``PIHELLO_IT_PORTS``, default 18400-18499). Logs and JUnit files go
to a temporary directory, printed at the end. Exit status 1 if any run failed.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "tests" / "fixtures" / "pihole" / "index.json"
DEFAULT_REPO = "docker.io/pihole/pihole"
DEFAULT_PORTS = "18400-18499"
RUNNING: set[subprocess.Popen[bytes]] = set()
RUNNING_LOCK = threading.Lock()


@dataclass
class Result:
    tag: str
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0
    returncode: int | None = None
    log: Path | None = None
    perf: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.failed and not self.errors


def recorded_tags() -> tuple[list[str], dict[str, dict[str, str]]]:
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    tags = [tag for tag in index if re.fullmatch(r"\d{4}\.\d{2}\.\d+", tag)]
    tags.sort(key=lambda tag: tuple(int(part) for part in tag.split(".")))
    return tags, index


def port_slices(text: str, count: int) -> list[str]:
    start, end = (int(part) for part in text.split("-"))
    size = (end - start + 1) // count
    if size < 15:
        raise SystemExit(f"port range {text} is too small for {count} parallel runs")
    return [f"{start + i * size}-{start + (i + 1) * size - 1}" for i in range(count)]


def run_tag(tag: str, repo: str, ports: str, workdir: Path, extra: list[str]) -> Result:
    result = Result(tag, log=workdir / f"{tag}.log")
    junit = workdir / f"{tag}.xml"
    env = {**os.environ, "PIHELLO_IT_IMAGE": f"{repo}:{tag}", "PIHELLO_IT_PORTS": ports}
    command = [
        sys.executable, "-m", "pytest", "-m", "integration", "tests/integration",
        "-p", "no:cacheprovider", "-rfEs", "--junitxml", str(junit), *extra,
    ]  # fmt: skip
    started = time.monotonic()
    with result.log.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        with RUNNING_LOCK:
            RUNNING.add(proc)
        try:
            result.returncode = proc.wait()
        finally:
            with RUNNING_LOCK:
                RUNNING.discard(proc)
    result.duration = time.monotonic() - started
    if junit.exists():
        suite = ET.parse(junit).getroot()  # noqa: S314 - our own pytest output
        suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
        if suite is not None:
            total = int(suite.get("tests", 0))
            result.failed = int(suite.get("failures", 0))
            result.errors = int(suite.get("errors", 0))
            result.skipped = int(suite.get("skipped", 0))
            result.passed = total - result.failed - result.errors - result.skipped
    match = re.search(r"default greeting median (\d+) ms", result.log.read_text("utf-8"))
    result.perf = f"{match.group(1)} ms" if match else "-"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--tag", action="append", help="only this tag (repeatable)")
    parser.add_argument("--jobs", "-j", type=int, default=3, help="parallel runs (default: 3)")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"image repository ({DEFAULT_REPO})")
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments (after --)")
    args = parser.parse_args(argv)
    tags, index = recorded_tags()
    if args.tag:
        unknown = sorted(set(args.tag) - set(tags))
        if unknown:
            print(f"note: not recorded, testing anyway: {', '.join(unknown)}")
        tags = [tag for tag in tags if tag in args.tag] + unknown
    jobs = max(1, min(args.jobs, len(tags)))
    slices = port_slices(os.environ.get("PIHELLO_IT_PORTS") or DEFAULT_PORTS, jobs)
    workdir = Path(tempfile.mkdtemp(prefix="pihello-matrix-"))
    todo: queue.Queue[str] = queue.Queue()
    for tag in tags:
        todo.put(tag)
    results: dict[str, Result] = {}
    lock = threading.Lock()
    print(f"testing {len(tags)} tag(s), {jobs} at a time; logs in {workdir}", flush=True)

    def worker(ports: str) -> None:
        while True:
            try:
                tag = todo.get_nowait()
            except queue.Empty:
                return
            result = run_tag(tag, args.repo, ports, workdir, args.pytest_args)
            with lock:
                results[tag] = result
                state = "ok" if result.ok else "FAILED"
                print(f"  {tag:<10} {state} ({result.duration:.0f} s)", flush=True)

    started = time.monotonic()
    threads = [threading.Thread(target=worker, args=(ports,), daemon=True) for ports in slices]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        # Let each pytest tear its fixtures down, which removes its containers.
        while not todo.empty():
            todo.get_nowait()
        with RUNNING_LOCK:
            running = list(RUNNING)
        print(f"\ninterrupted; stopping {len(running)} run(s)...", flush=True)
        for proc in running:
            proc.send_signal(signal.SIGINT)
        for proc in running:
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
        return 130

    header = (
        "tag",
        "core",
        "web",
        "FTL",
        "passed",
        "failed",
        "errors",
        "skipped",
        "time",
        "greeting",
    )
    rows = []
    for tag in tags:
        result = results[tag]
        versions = index.get(tag) if isinstance(index.get(tag), dict) else {}
        rows.append(
            (
                tag,
                versions.get("core", "?"),
                versions.get("web", "?"),
                versions.get("ftl", "?"),
                str(result.passed),
                str(result.failed),
                str(result.errors),
                str(result.skipped),
                f"{result.duration:.0f} s",
                result.perf,
            )
        )
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]
    print()
    print("  ".join(cell.ljust(width) for cell, width in zip(header, widths, strict=True)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))
    failed = [results[tag] for tag in tags if not results[tag].ok]
    print(
        f"\n{len(tags) - len(failed)}/{len(tags)} tags passed in {time.monotonic() - started:.0f} s"
    )
    for result in failed:
        print(f"  {result.tag}: exit {result.returncode}, log {result.log}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
