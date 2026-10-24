"""Tests for shell completion scripts (``pihello completion``)."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from pihello import completion
from pihello.cli import COMMANDS, build_parser, main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def shim(tmp_path: Path) -> Path:
    """A directory with a ``pihello`` executable running this checkout."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "pihello"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m pihello "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def run_shell(argv: list[str], shim: Path) -> str:
    import os

    env = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}"}
    result = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_names() -> None:
    assert completion.names("commands") == list(COMMANDS)
    assert completion.names("presets")[0] == "default"
    assert "classic" in completion.names("presets")
    assert completion.names("themes")[0] == "default"
    assert completion.names("profiles") == []
    with pytest.raises(ValueError, match="unknown completion kind"):
        completion.names("bogus")


def test_profile_names_from_config(home: Path) -> None:
    path = home / ".config" / "pihello" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("[profiles.zeta]\nurl='a'\n[profiles.alpha]\nurl='b'\n")
    assert completion.names("profiles") == ["alpha", "zeta"]
    path.write_text("broken = = toml")
    assert completion.names("profiles") == []


def test_names_via_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["completion", "--names", "commands"]) == 0
    assert capsys.readouterr().out.split() == list(COMMANDS)


def test_completion_requires_shell(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["completion"]) == 2
    assert "which shell" in capsys.readouterr().err
    assert main(["completion", "tcsh"]) == 2


def test_unknown_shell() -> None:
    with pytest.raises(ValueError, match="unsupported shell"):
        completion.script("tcsh")


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_scripts_cover_every_command_and_flag(
    shell: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["completion", shell]) == 0
    text = capsys.readouterr().out
    for command in COMMANDS:
        assert command in text
    for command in COMMANDS:
        for action in build_parser(command)._actions:
            for flag in action.option_strings:
                if action.help == "==SUPPRESS==":
                    continue
                expected = flag
                if shell == "fish":
                    if flag.startswith("--"):
                        expected = f"-l {flag[2:]}"
                    elif len(flag) == 2:
                        expected = f"-s {flag[1:]}"
                    else:
                        expected = f"-o {flag[1:]}"
                assert expected in text, (shell, command, flag)
    for kind in ("presets", "themes", "profiles"):
        if shell == "zsh":
            assert f"_pihello_names {kind}" in text
        else:
            assert f"completion --names {kind}" in text
    assert "--names" not in text.replace("completion --names", "")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_bash_script_is_valid_and_completes(shim: Path, tmp_path: Path) -> None:
    script = tmp_path / "pihello.bash"
    script.write_text(completion.script("bash"))
    run_shell(["bash", "-n", str(script)], shim)

    def complete(*words: str) -> list[str]:
        quoted = " ".join(f"'{w}'" for w in words)
        code = (
            f"source '{script}'; COMP_WORDS=({quoted}); COMP_CWORD={len(words) - 1}; "
            "_pihello; printf '%s\\n' \"${COMPREPLY[@]}\""
        )
        return run_shell(["bash", "-c", code], shim).split()

    assert "init" in complete("pihello", "")
    assert "doctor" in complete("pihello", "do")
    assert "--password-file" in complete("pihello", "--pass")
    assert "--force" in complete("pihello", "init", "--fo")
    assert "--force" not in complete("pihello", "--fo")
    assert "classic" in complete("pihello", "--preset", "")
    assert complete("pihello", "--color", "tr") == ["truecolor"]
    assert "default" in complete("pihello", "themes", "")
    assert "zsh" in complete("pihello", "completion", "")
    assert complete("pihello", "--url", "") == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_bash_option_candidates_are_separate_words(shim: Path, tmp_path: Path) -> None:
    # `pihello doctor --p<TAB>` must offer four options, not one candidate
    # "--password-file\n--password-command\n..." (word splitting on spaces only).
    script = tmp_path / "pihello.bash"
    script.write_text(completion.script("bash"))
    code = (
        f"source '{script}'; COMP_WORDS=(pihello doctor --p); COMP_CWORD=2; "
        "_pihello; printf '<%s>' \"${COMPREPLY[@]}\""
    )
    output = run_shell(["bash", "-c", code], shim)
    assert output == "<--password-file><--password-command><--password-stdin><--profile>"


@pytest.mark.skipif(shutil.which("fish") is None, reason="fish not installed")
def test_fish_script_is_valid_and_completes(shim: Path, tmp_path: Path) -> None:
    script = tmp_path / "pihello.fish"
    script.write_text(completion.script("fish"))
    run_shell(["fish", "--no-config", "-n", str(script)], shim)

    def complete(line: str) -> list[str]:
        code = f"source '{script}'; complete -C '{line}'"
        output = run_shell(["fish", "--no-config", "-c", code], shim)
        return [entry.split("\t")[0] for entry in output.splitlines()]

    assert "doctor" in complete("pihello ")
    assert "--password-file" in complete("pihello --pass")
    assert "--force" in complete("pihello init --fo")
    assert "classic" in complete("pihello --preset ")
    assert "default" in complete("pihello themes ")
    assert "-ts" in complete("pihello -t")


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_zsh_script_is_valid(shim: Path, tmp_path: Path) -> None:  # pragma: no cover
    script = tmp_path / "_pihello"
    script.write_text(completion.script("zsh"))
    run_shell(["zsh", "-n", str(script)], shim)


def test_zsh_escaping() -> None:
    text = completion.script("zsh")
    assert text.startswith("#compdef pihello")
    # help texts with brackets/colons must be escaped inside _arguments specs
    assert "\\[" in text or "[" not in completion._zsh_escape("a[b]")
    assert completion._zsh_escape("it's [x]: y") == "it'\\''s \\[x\\]\\: y"


def test_install_instructions_create_the_directory(capsys: pytest.CaptureFixture[str]) -> None:
    assert "mkdir -p ~/.config/fish/completions" in completion.script("fish")
    assert main(["completion", "--help"]) == 0
    assert "mkdir -p ~/.config/fish/completions" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("fish") is None, reason="fish not installed")
def test_fish_shows_preset_and_theme_descriptions(shim: Path, tmp_path: Path) -> None:
    from pihello import presets, themes

    script = tmp_path / "pihello.fish"
    script.write_text(completion.script("fish"))

    def complete(line: str) -> dict[str, str]:
        code = f"source '{script}'; complete -C '{line}'"
        output = run_shell(["fish", "--no-config", "-c", code], shim)
        return dict(entry.split("\t", 1) for entry in output.splitlines())

    default = presets.get("default")
    assert complete("pihello --preset ")["default"] == default.description
    assert complete("pihello presets ")["default"] == default.description
    assert complete("pihello -T ")["nord"] == themes.get("nord").description


def test_names_with_descriptions(capsys: pytest.CaptureFixture[str]) -> None:
    from pihello import presets

    assert main(["completion", "--names", "presets", "--describe"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"default\t{presets.get('default').description}"
    assert main(["completion", "--names", "presets"]) == 0  # bash/zsh: names only
    assert capsys.readouterr().out.splitlines()[0] == "default"
