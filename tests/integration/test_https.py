"""HTTPS with Pi-hole's self-signed certificate (CN/SAN ``pi.hole``) and HTTP→HTTPS redirects."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from .support import NOTES, Pihello, Target, running_target

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ca_cert(pihole: Target, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return pihole.require_container().tls_ca_cert(tmp_path_factory.mktemp("tls"))


def test_self_signed_certificate_is_rejected(pihello: Pihello, pihole: Target) -> None:
    run = pihello("--url", pihole.https_url, "--color", "never")
    assert run.returncode == 3, run
    assert "TLS certificate verification failed" in run.stderr, run
    hint = run.stderr.split("hint:", 1)[1]
    assert "--ca-cert" in hint, run
    assert "--insecure" in hint, run


def test_insecure(pihello: Pihello, pihole: Target) -> None:
    run = pihello("--url", pihole.https_url, "-k", "--color", "never")
    assert run.returncode == 0, run
    assert run.stdout.startswith("Pi-hole on 127.0.0.1 · ● enabled"), run


def test_ca_cert_with_an_ip_address(pihello: Pihello, pihole: Target, ca_cert: Path) -> None:
    """The chain verifies, but the certificate is only valid for ``pi.hole``."""
    run = pihello("--url", pihole.https_url, "--ca-cert", str(ca_cert), "--color", "never")
    assert run.returncode == 3, run
    assert "TLS certificate verification failed" in run.stderr, run
    assert "Hostname mismatch" in run.stderr or "not valid for" in run.stderr, run
    hint = run.stderr.split("hint:", 1)[1]
    assert "pi.hole" in hint, run


def _hosts_prefix(tmp_path: Path) -> tuple[str, ...] | None:
    """Run a command with ``pi.hole`` → 127.0.0.1 in /etc/hosts (user + mount namespace)."""
    if shutil.which("unshare") is None:
        return None
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost pi.hole\n::1 localhost\n")
    prefix = (
        "unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "sh",
        "-c",
        'mount --bind "$0" /etc/hosts && exec "$@"',
        str(hosts),
    )
    probe = subprocess.run(
        [*prefix, "cat", "/etc/hosts"], capture_output=True, text=True, timeout=30, check=False
    )
    return prefix if probe.returncode == 0 and "pi.hole" in probe.stdout else None


def test_ca_cert_with_the_pi_hole_name(
    pihello: Pihello, pihole: Target, ca_cert: Path, tmp_path: Path
) -> None:
    prefix = _hosts_prefix(tmp_path)
    if prefix is None:
        NOTES.append(
            "https: --ca-cert with the name pi.hole not tested (unprivileged user namespaces "
            "unavailable); only the hostname-mismatch case ran"
        )
        pytest.skip("cannot map pi.hole to 127.0.0.1 without root (unshare -rm unavailable)")
    port = pihole.https_url.rsplit(":", 1)[1]
    url = f"https://pi.hole:{port}"
    run = pihello("--url", url, "--ca-cert", str(ca_cert), "--color", "never", prefix=prefix)
    assert run.returncode == 0, run
    assert run.stdout.startswith("Pi-hole on pi.hole · ● enabled"), run
    doctor = pihello(
        "doctor", "--url", url, "--ca-cert", str(ca_cert), "--color", "never",
        prefix=prefix, logins=2,
    )  # fmt: skip
    assert doctor.returncode == 0, doctor
    assert f"✓ HTTPS certificate verified against {ca_cert}" in doctor.stdout, doctor


# --------------------------------------------------------------------------- #
# webserver.port = "80r,443s": the HTTP port only redirects                    #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def redirecting_pihole() -> Iterator[Target]:
    with running_target(
        "redirect", https=True, env={"FTLCONF_webserver_port": "80r,443s"}
    ) as target:
        yield target


def test_http_redirect_to_https(
    make_pihello: Callable[[Target], Pihello], redirecting_pihole: Target
) -> None:
    container = redirecting_pihole.require_container()
    pihello = make_pihello(redirecting_pihole)
    http_url = container.base_url
    run = pihello("--url", http_url, "--color", "never")
    assert run.returncode == 3, run
    assert "redirects to HTTPS (https://pi.hole/api/auth)" in run.stderr, run
    hint = run.stderr.split("hint:", 1)[1]
    assert "e.g. https://127.0.0.1 (" in hint, run
    assert f":{container.http_port}" not in hint, run  # the HTTP port does not speak HTTPS

    quiet = pihello("-q", "--url", http_url)
    assert quiet.returncode == 3, quiet
    assert quiet.stdout == quiet.stderr == "", quiet

    doctor = pihello("doctor", "--url", http_url, "--color", "never", logins=0)
    assert doctor.returncode == 3, doctor
    assert "redirects to HTTPS" in doctor.stdout, doctor

    run = pihello("--url", redirecting_pihole.url, "-k", "--color", "never")
    assert run.returncode == 0, run
