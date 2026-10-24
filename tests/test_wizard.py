"""Tests for ``pihello init`` (pihello.wizard)."""

from __future__ import annotations

import io
import os
import stat
import sys
from typing import TYPE_CHECKING, Any

import pytest

from pihello import wizard
from pihello.cli import main
from pihello.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from helpers.fakepihole import FakePihole

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

PASSWORD = "pihello-test"


# --------------------------------------------------------------------------- #
# Prompter                                                                    #
# --------------------------------------------------------------------------- #
def prompter(answers: str, secrets: list[str] | None = None) -> tuple[wizard.Prompter, io.StringIO]:
    out = io.StringIO()
    pending = list(secrets or [])

    def fake_getpass(prompt: str) -> str:
        out.write(prompt)
        if not pending:
            raise EOFError
        return pending.pop(0)

    return wizard.Prompter(io.StringIO(answers), out, fake_getpass, color=False), out


def test_ask_default_and_retry() -> None:
    p, out = prompter("\n  value  \n\n\nlater\n")
    assert p.ask("Q", "dflt") == "dflt"
    assert p.ask("Q") == "value"
    assert p.ask("Q") == "later"  # empty answers without default ask again
    assert "Q [dflt]: " in out.getvalue()


def test_confirm() -> None:
    p, out = prompter("\ny\nNO\nmaybe\nyes\n")
    assert p.confirm("Ok?", default=True) is True
    assert p.confirm("Ok?") is True
    assert p.confirm("Ok?", default=True) is False
    assert p.confirm("Ok?") is True
    assert "please answer y or n" in out.getvalue()
    assert "[Y/n]" in out.getvalue()
    assert "[y/N]" in out.getvalue()


def test_choose_by_number_name_prefix() -> None:
    options = [("alpha", "first"), ("beta", "second"), ("bravo", "third")]
    p, out = prompter("2\nalpha\nbr\nb\n9\n\n")
    assert p.choose("Pick", options, "alpha") == "beta"
    assert p.choose("Pick", options, "alpha") == "alpha"
    assert p.choose("Pick", options, "alpha") == "bravo"
    # "b" is ambiguous and 9 out of range, then the default
    assert p.choose("Pick", options, "beta") == "beta"
    text = out.getvalue()
    assert "please enter a number from 1 to 3" in text
    assert " 1)  alpha" in text
    assert "*beta" in text


def test_choose_fits_long_descriptions_into_the_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    # 80 columns: cut descriptions with "…" instead of wrapping mid-word
    from pihello import terminal

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(terminal, "terminal_size", lambda _stream=None: (80, 24))
    out = Terminal()
    p = wizard.Prompter(io.StringIO("1\n"), out, lambda _p: "", color=False)
    options = [("dashboard", "word " * 30), ("box", "short")]
    assert p.choose("Pick", options, "box") == "dashboard"
    lines = out.getvalue().splitlines()
    assert all(len(line) < 80 for line in lines[:2])
    assert lines[0].endswith("…")
    assert lines[1].endswith("short")


def test_end_of_input_cancels() -> None:
    p, _ = prompter("")
    with pytest.raises(wizard.SetupCancelled, match="end of input"):
        p.ask("Q")
    with pytest.raises(wizard.SetupCancelled):
        p.secret("pw: ")


def test_secret_uses_getpass() -> None:
    p, out = prompter("", ["s3cret"])
    assert p.secret("App password: ") == "s3cret"
    assert "s3cret" not in out.getvalue()


def test_prompter_uses_input_on_real_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_in, fake_out = io.StringIO("typed\n"), io.StringIO()
    monkeypatch.setattr(sys, "stdin", fake_in)
    monkeypatch.setattr(sys, "stdout", fake_out)
    p = wizard.Prompter(color=False)
    assert p.ask("Name") == "typed"
    with pytest.raises(wizard.SetupCancelled):
        p.ask("Again")


def test_prompter_paints_only_with_color() -> None:
    p, _ = prompter("")
    assert p.paint("x", "1") == "x"
    colored = wizard.Prompter(io.StringIO(), io.StringIO(), lambda _p: "", color=True)
    assert colored.paint("x", "1") == "\x1b[1mx\x1b[0m"


# --------------------------------------------------------------------------- #
# Shell snippets                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("shell_path", "expected"),
    [
        ("/bin/bash", "bash"),
        ("/usr/bin/zsh", "zsh"),
        ("/usr/local/bin/fish", "fish"),
        ("/usr/bin/pwsh", "powershell"),
        ("/bin/tcsh", None),
        ("", None),
    ],
)
def test_detect_shell(shell_path: str, expected: str | None) -> None:
    assert wizard.detect_shell({"SHELL": shell_path}) == expected


def test_detect_powershell_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert wizard.detect_shell({"PSModulePath": "C:\\x"}) == "powershell"
    monkeypatch.setattr(wizard, "_WINDOWS", True)
    assert wizard.detect_shell({}) == "powershell"


def test_snippets(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    bash = wizard.shell_snippet("bash", env=env)
    assert bash.path == tmp_path / ".bashrc"
    assert bash.display_path == "~/.bashrc"
    assert "[[ $- == *i* ]]" in bash.text
    assert "pihello -q" in bash.text
    zsh = wizard.shell_snippet("zsh", env={**env, "ZDOTDIR": str(tmp_path / "z")})
    assert zsh.path == tmp_path / "z" / ".zshrc"
    assert "-o interactive" in zsh.text
    fish = wizard.shell_snippet("fish", env=env, command="pihello -q --config /x")
    assert fish.path == tmp_path / ".config" / "fish" / "conf.d" / "pihello.fish"
    assert "function fish_greeting" in fish.text
    assert "--config /x" in fish.text
    ps = wizard.shell_snippet("powershell", env=env)
    assert ps.display_path == "$PROFILE"
    assert "Get-Command pihello" in ps.text
    assert not ps.append_ok  # $PROFILE's location varies; never guess it
    assert "notepad $PROFILE" in ps.note
    with pytest.raises(ValueError, match="unknown shell"):
        wizard.shell_snippet("tcsh", env=env)


def test_fish_snippet_respects_existing_greeting(tmp_path: Path) -> None:
    own = tmp_path / ".config" / "fish" / "functions" / "fish_greeting.fish"
    own.parent.mkdir(parents=True)
    own.write_text("function fish_greeting\nend\n")
    snippet = wizard.shell_snippet("fish", env={"HOME": str(tmp_path)})
    assert snippet.path == own
    assert not snippet.append_ok
    assert "inside it" in snippet.note


def test_snippet_display_outside_home(tmp_path: Path) -> None:
    snippet = wizard.shell_snippet(
        "zsh", env={"HOME": str(tmp_path / "home"), "ZDOTDIR": str(tmp_path / "zdot")}
    )
    assert snippet.display_path == str(tmp_path / "zdot" / ".zshrc")


def test_append_snippet(tmp_path: Path) -> None:
    snippet = wizard.shell_snippet("bash", env={"HOME": str(tmp_path)})
    snippet.path.write_text("export A=1")  # no trailing newline
    assert wizard.append_snippet(snippet) is True
    text = snippet.path.read_text()
    assert text.startswith("export A=1\n\n# Pi-hole greeting")
    assert wizard.append_snippet(snippet) is False  # already there
    fresh = wizard.shell_snippet("fish", env={"HOME": str(tmp_path / "new")})
    assert wizard.append_snippet(fresh) is True
    assert fresh.path.read_text() == fresh.text
    other = wizard.shell_snippet("zsh", env={"HOME": str(tmp_path / "z")})
    other.path.parent.mkdir(parents=True)
    other.path.write_text("a\n")
    wizard.append_snippet(other)
    assert other.path.read_text().startswith("a\n\n#")


# --------------------------------------------------------------------------- #
# Full runs against the fake Pi-hole                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg(home: Path) -> Path:
    return home / ".config" / "pihello" / "config.toml"


def http_only(server: FakePihole) -> FakePihole:
    """Make ``server`` report no HTTPS port, so the wizard doesn't offer HTTPS."""
    server.set_response("GET /api/info/login", 200, {"dns": True, "https_port": 0})
    return server


@pytest.fixture
def fake_pihole(fake_pihole_factory: Callable[..., FakePihole]) -> FakePihole:
    """A fake Pi-hole without HTTPS (most tests don't care about the HTTPS offer)."""
    return http_only(fake_pihole_factory())


@pytest.fixture
def run_init(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., tuple[int, str, str]]:
    """Run ``pihello init`` with scripted answers and passwords."""
    import getpass

    def run(
        argv: list[str], answers: str = "", passwords: list[str] | None = None
    ) -> tuple[int, str, str]:
        pending = list(passwords or [])

        def fake_getpass(prompt: str = "Password: ", stream: Any = None) -> str:
            sys.stdout.write(prompt)
            if not pending:
                raise EOFError
            return pending.pop(0)

        monkeypatch.setattr(getpass, "getpass", fake_getpass)
        monkeypatch.setattr(sys, "stdin", io.StringIO(answers))
        code = main(["init", *argv])
        out, err = capsys.readouterr()
        return code, out, err

    return run


def answers(*lines: str) -> str:
    return "".join(line + "\n" for line in lines)


def read_config(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    return data


def test_interactive_happy_path(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path, home: Path
) -> None:
    code, out, err = run_init(
        ["--shell", "bash"],
        answers(
            fake_pihole.url,  # address
            "",  # store: default (config)
            "",  # preset: default
            "",  # use this preset? yes
            "",  # theme: default
            "",  # use it? yes
            "",  # write? yes
            "",  # append to .bashrc? no
        ),
        [PASSWORD],
    )
    assert code == 0, err
    assert "found a Pi-hole v6" in out
    assert "logged in" in out
    assert "Configure app password" in out
    data = read_config(cfg)
    assert data == {
        "url": fake_pihole.url,
        "password": PASSWORD,
        "preset": "default",
        "theme": "default",
    }
    if os.name != "nt":
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert "Add this to ~/.bashrc" in out
    assert "pihello -q" in out
    assert not (home / ".bashrc").exists()
    assert PASSWORD not in out
    assert PASSWORD not in err
    assert not fake_pihole.active_sessions


def test_interactive_retry_password_and_append(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path, home: Path
) -> None:
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"],
        answers("file", "", "2", "n", "1", "", "", "", "", "y"),
        ["wrong", PASSWORD],
    )
    assert code == 0, out
    assert out.count("Pi-hole rejected the password") == 1
    data = read_config(cfg)
    assert data["password_file"] == "~/.config/pihello/password"
    assert "password" not in data
    password_file = cfg.parent / "password"
    assert password_file.read_text() == PASSWORD + "\n"
    if os.name != "nt":
        assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    assert data["preset"] == "default"  # the second preset was rejected, default chosen
    assert "pihello -q" in (home / ".bashrc").read_text()
    assert not fake_pihole.active_sessions


def test_three_wrong_passwords_fail(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    code, out, err = run_init(
        ["--url", fake_pihole.url], answers("config"), ["wrong-1", "wrong-2", "wrong-3"]
    )
    assert code == 4
    assert out.count("rejected the password") == 3
    assert "rejected the password" in err
    assert not cfg.exists()
    assert not fake_pihole.active_sessions


def test_password_command_store(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    command = f"{sys.executable} -c \"print('{PASSWORD}')\""
    bad = f'{sys.executable} -c "import sys; sys.exit(1)"'
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "fish"],
        answers("command", bad, command, "", "", "", "", "", ""),
    )
    assert code == 0, out
    assert "password_command failed" in out
    assert read_config(cfg)["password_command"] == command
    assert "conf.d/pihello.fish" in out


def test_env_store(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole: FakePihole,
    cfg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIHELLO_PASSWORD", PASSWORD)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    code, out, _ = run_init(["--url", fake_pihole.url], answers("env", "", "", "", "", "", ""))
    assert code == 0, out
    assert "using the password from PIHELLO_PASSWORD" in out
    data = read_config(cfg)
    assert "password" not in data
    assert "password_file" not in data
    assert "remember to set PIHELLO_PASSWORD" in out
    assert "~/.zshrc" in out


def test_existing_password_file_is_reused(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    pw = cfg.parent / "password"
    pw.parent.mkdir(parents=True)
    pw.write_text(PASSWORD + "\n")
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"],
        answers("file", "", "", "", "", "", "", "", ""),
    )
    assert code == 0, out
    assert "Use the password already in" in out
    assert "wrote the password" not in out


@pytest.mark.parametrize("interactive", [True, False])
def test_relative_password_file_is_saved_as_absolute(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole: FakePihole,
    *,
    cfg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool,
) -> None:
    # The config resolves relative paths against its own directory, so a path
    # typed relative to the current directory must be stored absolute.
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    if interactive:
        code, out, _ = run_init(
            ["--url", fake_pihole.url, "--shell", "bash"],
            answers("file", "pw.txt", "", "", "", "", "", ""),
            [PASSWORD],
        )
    else:
        (workdir / "pw.txt").write_text(PASSWORD + "\n")
        argv = ["--yes", "--url", fake_pihole.url, "--password-file", "pw.txt", "--shell", "bash"]
        code, out, _ = run_init(argv)
    assert code == 0, out
    assert (workdir / "pw.txt").read_text() == PASSWORD + "\n"
    assert read_config(cfg)["password_file"] == str(workdir / "pw.txt")
    monkeypatch.chdir(tmp_path)
    assert main(["--color", "never"]) == 0
    assert not fake_pihole.active_sessions


def test_passwordless_pihole(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole_factory: Callable[..., FakePihole],
    cfg: Path,
) -> None:
    server = http_only(fake_pihole_factory(password=None))
    code, out, _ = run_init(["--shell", "bash"], answers(server.url, "", "", "", "", "", ""))
    assert code == 0, out
    assert "has no password" in out
    assert set(read_config(cfg)) == {"url", "preset", "theme"}


def test_unreachable_address_then_retry(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    code, out, _ = run_init(
        ["--timeout", "0.5", "--shell", "bash"],
        answers("http://127.0.0.1:9", "n", fake_pihole.url, "", "", "", "", "", "", ""),
        [PASSWORD],
    )
    assert code == 0, out
    assert "✗" in out
    assert read_config(cfg)["url"] == fake_pihole.url
    assert read_config(cfg)["timeout"] == 0.5


def test_unreachable_address_accepted_anyway(
    run_init: Callable[..., tuple[int, str, str]], cfg: Path
) -> None:
    code, out, _ = run_init(
        ["--timeout", "0.5", "--shell", "bash"],
        answers("bad url with spaces", "http://127.0.0.1:9", "y", "", "", "", "", "", "", ""),
        [PASSWORD],
    )
    assert code == 0, out
    assert read_config(cfg)["password"] == PASSWORD
    assert "the previews use demo data" in out


def test_existing_config_declined(run_init: Callable[..., tuple[int, str, str]], cfg: Path) -> None:
    cfg.parent.mkdir(parents=True)
    cfg.write_text("url = 'http://old'\n")
    code, out, err = run_init([], answers("n"))
    assert code == 2
    assert "already exists" in out
    assert "not changed" in err
    assert cfg.read_text() == "url = 'http://old'\n"


def test_existing_config_replaced_with_backup(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    cfg.parent.mkdir(parents=True)
    cfg.write_text("url = 'http://old'\n")
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"],
        answers("y", "", "", "", "", "", "", ""),
        [PASSWORD],
    )
    assert code == 0, out
    assert (cfg.parent / "config.toml.bak").read_text() == "url = 'http://old'\n"
    assert read_config(cfg)["url"] == fake_pihole.url


def test_force_replaces_a_config_that_is_not_utf8(
    run_init: Callable[..., tuple[int, str, str]], cfg: Path
) -> None:
    # e.g. saved as UTF-16 by an old Notepad: pihello can't read it, so the user
    # runs `pihello init --force`; the backup keeps the exact bytes.
    old = "url = 'http://old'\n".encode("utf-16")
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(old)
    code, _, err = run_init(["--yes", "--force", "--no-check", "--url", "pi.hole"])
    assert code == 0, err
    assert (cfg.parent / "config.toml.bak").read_bytes() == old
    assert read_config(cfg)["url"] == "http://pi.hole"


@pytest.mark.parametrize(
    "content",
    [b"# caf\xe9 (latin-1)\n", "# Windows PowerShell 5 profile\n".encode("utf-16")],
)
def test_startup_file_that_is_not_utf8_is_left_alone(
    run_init: Callable[..., tuple[int, str, str]], cfg: Path, home: Path, content: bytes
) -> None:
    # appending UTF-8 text could corrupt it (UTF-16); the setup still succeeds
    bashrc = home / ".bashrc"
    bashrc.write_bytes(content)
    code, out, err = run_init(
        ["--no-check", "--url", "pi.hole", "--store", "none", "--shell", "bash"],
        answers("", "", "", "", "", "y"),
    )
    assert code == 0, err
    assert "internal error" not in err
    assert bashrc.read_bytes() == content
    assert "by hand" in out
    assert cfg.exists()


def test_decline_writing_prints_config(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"],
        answers("", "", "", "", "", "n", ""),
        [PASSWORD],
    )
    assert code == 0, out
    assert not cfg.exists()
    assert "Nothing was written" in out
    assert f'url = "{fake_pihole.url}"' in out
    assert PASSWORD not in out
    assert "All set" not in out


def test_decline_writing_with_password_file_writes_nothing(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path, home: Path
) -> None:
    # "Nothing was written" must be true: no plain-text password file left behind,
    # and no startup snippet for a greeting that has no config.
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"],
        answers("file", "", "", "", "", "", "n", "y"),
        [PASSWORD],
    )
    assert code == 0, out
    assert "Nothing was written" in out
    assert not cfg.exists()
    assert not (cfg.parent / "password").exists()
    assert "wrote the password" not in out
    assert "Append it" not in out
    assert not (home / ".bashrc").exists()
    assert not fake_pihole.active_sessions


def test_end_of_input(run_init: Callable[..., tuple[int, str, str]], cfg: Path) -> None:
    code, _, err = run_init([], "")
    assert code == 2
    assert "setup cancelled" in err
    assert "--yes" in err


def test_ctrl_c(
    run_init: Callable[..., tuple[int, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(*_args: Any) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard.Prompter, "ask", interrupt)
    code, out, err = run_init([], "")
    assert code == 130
    assert err == ""
    assert out.rstrip().endswith("setup cancelled; nothing was written")


def test_ctrl_c_after_writing_says_what_was_written(
    run_init: Callable[..., tuple[int, str, str]], cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_confirm = wizard.Prompter.confirm

    def confirm(self: wizard.Prompter, question: str, default: bool = False) -> bool:
        if question.startswith("Append it"):
            raise KeyboardInterrupt
        return real_confirm(self, question, default)

    monkeypatch.setattr(wizard.Prompter, "confirm", confirm)
    code, out, _ = run_init(
        ["--no-check", "--url", "pi.hole", "--store", "none", "--shell", "bash"],
        answers("", "", "", "", ""),
    )
    assert code == 130
    assert cfg.exists()
    assert out.rstrip().endswith(f"setup cancelled; {cfg} was already written")


def test_rerun_prefills_the_existing_address(
    run_init: Callable[..., tuple[int, str, str]], cfg: Path
) -> None:
    cfg.parent.mkdir(parents=True)
    cfg.write_text("url = 'http://10.0.0.5:8080'\npreset = 'compact'\n")
    code, out, err = run_init(
        ["--no-check", "--store", "none", "--shell", "bash"],
        answers("y", "", "", "", "", "", "", ""),
    )
    assert code == 0, err
    assert "Pi-hole address [http://10.0.0.5:8080]: " in out
    assert read_config(cfg)["url"] == "http://10.0.0.5:8080"


def test_closing_line_names_a_custom_config(
    run_init: Callable[..., tuple[int, str, str]], tmp_path: Path
) -> None:
    target = tmp_path / "custom.toml"
    code, out, err = run_init(["--yes", "--no-check", "--config", str(target), "--shell", "bash"])
    assert code == 0, err
    assert f"Run `pihello --config {target}` to see your greeting" in out
    assert f"`pihello doctor --config {target}`" in out


# -- non-interactive ----------------------------------------------------------
def test_yes_with_password_file(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole: FakePihole,
    cfg: Path,
    tmp_path: Path,
) -> None:
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD + "\n")
    code, out, err = run_init(
        [
            "--yes",
            "--url",
            fake_pihole.url,
            "--password-file",
            str(pw),
            "--preset",
            "classic",
            "--shell",
            "bash",
        ]
    )
    assert code == 0, err
    assert read_config(cfg) == {
        "url": fake_pihole.url,
        "password_file": str(pw),
        "preset": "classic",
        "theme": "default",
    }
    assert "Append it" not in out
    assert not fake_pihole.active_sessions


def test_yes_with_password_stdin(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    code, out, err = run_init(
        ["--yes", "--url", fake_pihole.url, "--password-stdin", "--shell", "bash"],
        PASSWORD + "\n",
    )
    assert code == 0, err
    assert read_config(cfg)["password"] == PASSWORD
    assert PASSWORD not in out


def test_yes_with_password_command_and_insecure(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    command = f"{sys.executable} -c \"print('{PASSWORD}')\""
    code, _, err = run_init(
        ["-y", "-u", fake_pihole.url, "--password-command", command, "-k", "--shell", "bash"]
    )
    assert code == 0, err
    data = read_config(cfg)
    assert data["password_command"] == command
    assert data["verify_tls"] is False


def test_yes_wrong_password(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole: FakePihole,
    cfg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIHELLO_PASSWORD", "wrong")
    code, _, err = run_init(["--yes", "--url", fake_pihole.url])
    assert code == 4
    assert "rejected the password" in err
    assert not cfg.exists()


def test_yes_without_password_on_protected_pihole(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    code, _, err = run_init(["--yes", "--url", fake_pihole.url])
    assert code == 4
    assert "requires a password" in err


def test_yes_refuses_to_overwrite(run_init: Callable[..., tuple[int, str, str]], cfg: Path) -> None:
    cfg.parent.mkdir(parents=True)
    cfg.write_text("")
    code, _, err = run_init(["--yes", "--no-check"])
    assert code == 2
    assert "--force" in err


def test_yes_force_no_check_env_store(
    run_init: Callable[..., tuple[int, str, str]],
    cfg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg.parent.mkdir(parents=True)
    cfg.write_text("")
    monkeypatch.setenv("PIHELLO_PASSWORD", "pw")
    code, _out, err = run_init(
        ["--yes", "--force", "--no-check", "--url", "pi.hole", "--store", "config", "-T", "nord"]
    )
    assert code == 0, err
    data = read_config(cfg)
    assert data["url"] == "http://pi.hole"
    assert data["password"] == "pw"
    assert data["theme"] == "nord"


@pytest.mark.parametrize(
    ("option", "store", "expected"),
    [
        ("--password-file", "config", {"password": PASSWORD}),
        ("--password-command", "config", {"password": PASSWORD}),
        ("--password-command", "file", {"password_file": "~/.config/pihello/password"}),
        ("--password-file", "env", {}),
    ],
)
def test_yes_explicit_store_wins_over_the_password_option(
    run_init: Callable[..., tuple[int, str, str]],
    cfg: Path,
    tmp_path: Path,
    *,
    option: str,
    store: str,
    expected: dict[str, str],
) -> None:
    # --store's default follows from the password option; an explicit --store
    # must not be ignored silently.
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD + "\n")
    source = (
        str(pw) if option == "--password-file" else f"{sys.executable} -c \"print('{PASSWORD}')\""
    )
    argv = ["--yes", "--no-check", "--url", "pi.hole", option, source, "--store", store]
    code, _, err = run_init([*argv, "--shell", "bash"])
    assert code == 0, err
    data = read_config(cfg)
    assert {k: v for k, v in data.items() if k.startswith("password")} == expected
    if store == "file":
        assert (cfg.parent / "password").read_text() == PASSWORD + "\n"


def test_interactive_uses_the_given_password_file(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole: FakePihole,
    cfg: Path,
    tmp_path: Path,
) -> None:
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD + "\n")
    code, out, err = run_init(
        ["--url", fake_pihole.url, "--password-file", str(pw), "--shell", "bash"],
        answers(
            "",  # password file: the given one
            "",  # use the password already in it? yes
            "",  # preset
            "",  # use it? yes
            "",  # theme
            "",  # use it? yes
            "",  # write? yes
            "",  # append? no
        ),
    )
    assert code == 0, err
    assert "Where should pihello keep the password?" not in out
    assert "App password:" not in out
    data = read_config(cfg)
    assert data["password_file"] == str(pw)
    assert "password" not in data
    assert not fake_pihole.active_sessions


def test_interactive_uses_the_given_password_command(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    command = f"{sys.executable} -c \"print('{PASSWORD}')\""
    code, out, err = run_init(
        ["--url", fake_pihole.url, "--password-command", command, "--shell", "bash"],
        answers("", "", "", "", "", "", ""),
    )
    assert code == 0, err
    assert "Where should pihello keep the password?" not in out
    assert read_config(cfg)["password_command"] == command


def test_interactive_password_stdin_needs_yes(
    run_init: Callable[..., tuple[int, str, str]], cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The questions are read from standard input, too: the piped password would
    # be taken as the answer to "Pi-hole address" and shown in the DNS error.
    import socket

    def no_dns(*args: object, **kwargs: object) -> None:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    code, out, err = run_init(["--password-stdin"], PASSWORD + "\n")
    assert code == 2
    assert "--yes" in err
    assert PASSWORD not in out + err
    assert not cfg.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--password-file", "a", "--password-command", "b"],
        ["--password-file", "a", "--password-stdin", "--yes"],
    ],
)
def test_two_password_options_conflict_in_both_modes(
    run_init: Callable[..., tuple[int, str, str]], argv: list[str]
) -> None:
    code, _, err = run_init(["--no-check", *argv])
    assert code == 2
    assert "cannot be used together" in err


def test_yes_with_custom_config_path(
    run_init: Callable[..., tuple[int, str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    target = tmp_path / "custom.toml"
    code, out, err = run_init(["--yes", "--no-check", "--config", str(target)])
    assert code == 0, err
    assert target.exists()
    assert f"pihello -q --config {target}" in out
    assert "bash" in out
    assert "powershell" in out


def test_relative_config_path_in_snippet_is_absolute(
    run_init: Callable[..., tuple[int, str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The snippet runs in new shells, whose working directory is not this one.
    monkeypatch.chdir(tmp_path)
    code, out, err = run_init(["--yes", "--no-check", "--config", "my.toml", "--shell", "bash"])
    assert code == 0, err
    assert (tmp_path / "my.toml").exists()
    assert f"pihello -q --config {tmp_path / 'my.toml'}\n" in out


def test_snippet_names_the_config_when_pihello_config_points_elsewhere(
    run_init: Callable[..., tuple[int, str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIHELLO_CONFIG", str(tmp_path / "other.toml"))
    target = tmp_path / "custom.toml"
    code, out, err = run_init(["--yes", "--no-check", "--config", str(target), "--shell", "zsh"])
    assert code == 0, err
    assert f"pihello -q --config {target}\n" in out
    # ... but not when PIHELLO_CONFIG is the file that was written
    code, out, err = run_init(["--yes", "--no-check", "--force", "--shell", "zsh"])
    assert code == 0, err
    assert (tmp_path / "other.toml").exists()
    assert "pihello -q\n" in out


def test_relative_ca_cert_is_saved_as_absolute(
    run_init: Callable[..., tuple[int, str, str]],
    cfg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Relative paths in the config are relative to the config directory, so a
    # --ca-cert relative to the current directory must be stored absolute.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tls_ca.crt").write_text("not checked with --no-check\n")
    code, _, err = run_init(
        ["--yes", "--no-check", "--url", "https://pi.hole", "--ca-cert", "tls_ca.crt"]
    )
    assert code == 0, err
    assert read_config(cfg)["verify_tls"] == str(tmp_path / "tls_ca.crt")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--yes", "--no-check", "--url", "http://"], "invalid Pi-hole URL"),
        (["--yes", "--no-check", "--store", "file"], "needs a password"),
        (["--yes", "--no-check", "--store", "command"], "--password-command"),
        (["--yes", "--no-check", "--preset", "nope"], "unknown preset"),
        (["--yes", "--no-check", "-k", "--ca-cert", "x"], "cannot be used together"),
        (
            ["--yes", "--no-check", "--password-file", "a", "--password-command", "b"],
            "cannot be used together",
        ),
    ],
)
def test_yes_errors(
    run_init: Callable[..., tuple[int, str, str]], argv: list[str], message: str
) -> None:
    code, _, err = run_init(argv)
    assert code == 2
    assert message in err


def test_yes_password_stdin_empty(run_init: Callable[..., tuple[int, str, str]]) -> None:
    code, _, err = run_init(["--yes", "--no-check", "--password-stdin"], "")
    assert code == 2
    assert "no password on standard input" in err


def test_no_check_interactive_none(
    run_init: Callable[..., tuple[int, str, str]], cfg: Path
) -> None:
    code, out, err = run_init(
        ["--no-check", "--shell", "bash"], answers("pi.hole", "none", "", "", "", "", "", "")
    )
    assert code == 0, err
    assert "no password: this Pi-hole has none" in out
    assert set(read_config(cfg)) == {"url", "preset", "theme"}


def test_config_data_never_contains_password_for_other_stores(tmp_path: Path) -> None:
    from pihello.cli import UI, build_parser

    args = build_parser("init").parse_args(["--yes", "--no-check", "--config", str(tmp_path / "c")])
    w = wizard.Wizard(args, UI(io.StringIO()), prompter("")[0], env={})
    w.answers.password = "x"
    w.answers.store = "env"
    assert "password" not in w.config_data()
    assert "x" not in repr(w.answers)


def test_core_version() -> None:
    assert wizard._core_version({"version": {"core": {"local": {"version": "v6.1"}}}}) == "v6.1"
    assert wizard._core_version({}) == "v6"
    assert wizard._core_version({"version": {"core": {"local": {"version": None}}}}) == "v6"


def test_setup_cancelled_is_a_config_error() -> None:
    assert issubclass(wizard.SetupCancelled, ConfigError)


# -- HTTPS and certificates ---------------------------------------------------
@pytest.fixture
def http_and_https(
    fake_pihole_factory: Callable[..., FakePihole],
) -> tuple[FakePihole, FakePihole]:
    """An HTTP fake that advertises the HTTPS port of a second (TLS) fake."""
    tls = fake_pihole_factory(https=True)
    plain = fake_pihole_factory()
    plain.set_response("GET /api/info/login", 200, {"dns": True, "https_port": tls.port})
    tls.set_response("GET /api/info/login", 200, {"dns": True, "https_port": tls.port})
    return plain, tls


REST = ("", "", "", "", "", "", "")  # store, preset, ok, theme, ok, write, append


def test_https_offer_with_ca_certificate(
    run_init: Callable[..., tuple[int, str, str]],
    http_and_https: tuple[FakePihole, FakePihole],
    cfg: Path,
    tmp_path: Path,
) -> None:
    plain, tls = http_and_https
    assert tls.ca_cert_path is not None
    code, out, err = run_init(
        ["--shell", "bash"],
        answers(
            plain.url,
            "",  # use HTTPS? yes
            "",  # certificate: ca (default)
            str(tmp_path / "missing.pem"),  # not a file
            "ca",
            str(tls.ca_cert_path),
            *REST,
        ),
        [PASSWORD],
    )
    assert code == 0, err
    assert "also serves HTTPS" in out
    assert "is not a file" in out
    data = read_config(cfg)
    assert data["url"] == tls.url
    assert data["verify_tls"] == str(tls.ca_cert_path.resolve())
    assert not tls.active_sessions
    assert not plain.active_sessions


def test_https_offer_insecure(
    run_init: Callable[..., tuple[int, str, str]],
    http_and_https: tuple[FakePihole, FakePihole],
    cfg: Path,
) -> None:
    plain, tls = http_and_https
    code, out, _ = run_init(
        ["--shell", "bash"], answers(plain.url, "y", "insecure", *REST), [PASSWORD]
    )
    assert code == 0, out
    data = read_config(cfg)
    assert data["url"] == tls.url
    assert data["verify_tls"] is False


def test_https_offer_back_to_http(
    run_init: Callable[..., tuple[int, str, str]],
    http_and_https: tuple[FakePihole, FakePihole],
    cfg: Path,
) -> None:
    plain, _ = http_and_https
    code, out, _ = run_init(["--shell", "bash"], answers(plain.url, "y", "back", *REST), [PASSWORD])
    assert code == 0, out
    assert f"staying with {plain.url}" in out
    assert read_config(cfg)["url"] == plain.url


def test_https_offer_declined(
    run_init: Callable[..., tuple[int, str, str]],
    http_and_https: tuple[FakePihole, FakePihole],
    cfg: Path,
) -> None:
    plain, _ = http_and_https
    code, out, _ = run_init(["--shell", "bash"], answers(plain.url, "n", *REST), [PASSWORD])
    assert code == 0, out
    assert read_config(cfg)["url"] == plain.url


def closed_port() -> int:
    """A local TCP port nothing listens on (e.g. 443 inside a Docker container)."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_https_not_offered_when_its_port_does_not_answer(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole_factory: Callable[..., FakePihole],
    cfg: Path,
) -> None:
    # Behind a Docker port mapping, info/login reports the container's port.
    port = closed_port()
    plain = fake_pihole_factory()
    plain.set_response("GET /api/info/login", 200, {"dns": True, "https_port": port})
    code, out, _ = run_init(
        ["--shell", "bash", "--timeout", "1"], answers(plain.url, *REST), [PASSWORD]
    )
    assert code == 0, out
    assert "Use HTTPS?" not in out
    assert f"127.0.0.1:{port} doesn't answer" in out
    assert "https://127.0.0.1:PORT" in out
    assert read_config(cfg)["url"] == plain.url


def test_https_url_with_self_signed_certificate(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole_factory: Callable[..., FakePihole],
    cfg: Path,
) -> None:
    tls = fake_pihole_factory(https=True)
    code, out, _ = run_init(["--shell", "bash"], answers(tls.url, "2", *REST), [PASSWORD])
    assert code == 0, out
    assert "self-signed certificate" in out
    # where to find the CA, and that the address must use the certificate's name
    assert "/etc/pihole/tls_ca.crt" in out
    assert "pi.hole" in out
    assert read_config(cfg)["verify_tls"] is False


def test_https_url_back_then_other_address(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole_factory: Callable[..., FakePihole],
    fake_pihole: FakePihole,
    cfg: Path,
) -> None:
    tls = fake_pihole_factory(https=True)
    code, out, _ = run_init(
        ["--shell", "bash"],
        answers(tls.url, "back", "n", fake_pihole.url, *REST),
        [PASSWORD],
    )
    assert code == 0, out
    assert read_config(cfg)["url"] == fake_pihole.url


def test_yes_with_ca_cert(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole_factory: Callable[..., FakePihole],
    cfg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tls = fake_pihole_factory(https=True)
    monkeypatch.setenv("PIHELLO_PASSWORD", PASSWORD)
    code, out, err = run_init(
        ["--yes", "--url", tls.url, "--ca-cert", str(tls.ca_cert_path), "--shell", "zsh"]
    )
    assert code == 0, err
    assert read_config(cfg)["verify_tls"] == str(tls.ca_cert_path)
    assert "remember to set PIHELLO_PASSWORD" in out
    code, _, err = run_init(["--yes", "--force", "--url", tls.url])
    assert code == 3
    assert "certificate" in err.lower()


def test_totp_explains_app_passwords(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole_factory: Callable[..., FakePihole],
    cfg: Path,
) -> None:
    server = http_only(fake_pihole_factory(totp=True))
    code, out, _ = run_init(
        ["--url", server.url], answers("config"), [PASSWORD, PASSWORD, PASSWORD]
    )
    assert code == 4
    assert "please use an app password" in out
    assert not cfg.exists()


def test_seats_exceeded_aborts(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole_factory: Callable[..., FakePihole],
) -> None:
    server = http_only(fake_pihole_factory(max_sessions=0))
    code, _, err = run_init(["--url", server.url], answers("config"), [PASSWORD])
    assert code == 4
    assert "max_sessions" in err


def test_empty_password_is_asked_again(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"], answers(*REST), ["  ", PASSWORD]
    )
    assert code == 0, out
    assert "the password is empty" in out


def test_preview_fetch_failure_falls_back_to_demo(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole: FakePihole,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pihello import sources
    from pihello.errors import ConnectionFailed

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionFailed("gone")

    monkeypatch.setattr(sources, "collect", boom)
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"],
        answers("", "", "", "", "n", "", "", "", ""),
        [PASSWORD],
    )
    assert code == 0, out
    assert "cannot fetch data for the preview: gone" in out
    assert "the previews use demo data" in out


def test_preview_render_error_is_shown(
    run_init: Callable[..., tuple[int, str, str]],
    fake_pihole: FakePihole,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pihello.errors import TemplateError
    from pihello.template import Template

    def broken(*_args: Any, **_kwargs: Any) -> str:
        raise TemplateError("boom")

    monkeypatch.setattr(Template, "render", broken)
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"], answers(*REST), [PASSWORD]
    )
    assert code == 0, out
    assert "(cannot render this preset:" in out


def test_append_skipped_when_already_present(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, home: Path
) -> None:
    (home / ".bashrc").write_text("pihello -q\n")
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"],
        answers("", "", "", "", "", "", "y"),
        [PASSWORD],
    )
    assert code == 0, out
    assert "already mentions pihello" in out


def test_fish_with_own_greeting_is_not_appended(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, home: Path
) -> None:
    own = home / ".config" / "fish" / "functions" / "fish_greeting.fish"
    own.parent.mkdir(parents=True)
    own.write_text("function fish_greeting\nend\n")
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "fish"], answers("", "", "", "", "", ""), [PASSWORD]
    )
    assert code == 0, out
    assert "Put this in ~/.config/fish/functions/fish_greeting.fish" in out
    assert "Append it" not in out


def test_password_file_with_unreadable_content_is_replaced(
    run_init: Callable[..., tuple[int, str, str]], fake_pihole: FakePihole, cfg: Path
) -> None:
    pw = cfg.parent / "password"
    pw.parent.mkdir(parents=True)
    pw.write_text("")
    code, out, _ = run_init(
        ["--url", fake_pihole.url, "--shell", "bash"],
        answers("file", "", "", "", "", "", "", ""),
        [PASSWORD],
    )
    assert code == 0, out
    assert pw.read_text() == PASSWORD + "\n"


def test_windows_powershell_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(wizard, "_WINDOWS", True)
    snippet = wizard.shell_snippet("powershell", env={"HOME": str(tmp_path)})
    assert snippet.path.name == "Microsoft.PowerShell_profile.ps1"
    assert "Documents" in str(snippet.path)


def test_color_flag_controls_wizard_messages(
    run_init: Callable[..., tuple[int, str, str]],
) -> None:
    _, out, _ = run_init(["--yes", "--no-check", "--color", "always", "--shell", "bash"])
    assert "\x1b[32m✓\x1b[0m" in out
    _, out, _ = run_init(["--yes", "--no-check", "--force", "--color", "never"])
    assert "\x1b[" not in out


def test_powershell_snippet_quotes_the_config_path_for_powershell(
    run_init: Callable[..., tuple[int, str, str]], tmp_path: Path
) -> None:
    target = tmp_path / "it's here" / "config.toml"
    code, out, _ = run_init(
        ["--yes", "--no-check", "--config", str(target), "--shell", "powershell"]
    )
    assert code == 0, out
    quoted = "'" + str(target).replace("'", "''") + "'"
    assert f"pihello -q --config {quoted} }}" in out
    assert "Append it" not in out
