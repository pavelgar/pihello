"""Tests for pihello.config: loading, validation, precedence, secrets, TOML writing."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pihello import config
from pihello.errors import ConfigError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def cfg(home: Path) -> Path:
    """The default config path inside the isolated HOME."""
    return home / ".config" / "pihello" / "config.toml"


# --------------------------------------------------------------------------- #
# Locating                                                                    #
# --------------------------------------------------------------------------- #
def test_default_path_uses_xdg_config_home(home: Path) -> None:
    assert config.default_config_path() == home / ".config" / "pihello" / "config.toml"


def test_default_path_ignores_relative_xdg(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert config.default_config_path() == home / ".config" / "pihello" / "config.toml"


def test_default_path_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "_WINDOWS", True)
    env = {"APPDATA": str(tmp_path)}
    assert config.default_config_path(env) == tmp_path / "pihello" / "config.toml"


def test_find_config_path_precedence(tmp_path: Path, cfg: Path) -> None:
    env = {"PIHELLO_CONFIG": str(tmp_path / "env.toml")}
    assert config.find_config_path(tmp_path / "cli.toml", env) == (
        tmp_path / "cli.toml",
        "--config",
    )
    assert config.find_config_path(None, env) == (tmp_path / "env.toml", "PIHELLO_CONFIG")
    assert config.find_config_path(None, {})[1] == "default"


# --------------------------------------------------------------------------- #
# Loading and validation                                                      #
# --------------------------------------------------------------------------- #
def test_missing_default_file_is_fine(cfg: Path) -> None:
    file = config.load_file(cfg)
    assert not file.exists
    assert file.settings == {}


def test_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        config.load_file(tmp_path / "nope.toml", required=True)


def test_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="is a directory"):
        config.load_file(tmp_path)


def test_unreadable_file(tmp_path: Path) -> None:
    path = write(tmp_path / "c.toml", "url = 'x'", mode=0o000)
    if os.access(path, os.R_OK):  # pragma: no cover - running as root
        pytest.skip("root can read anything")
    with pytest.raises(ConfigError, match="cannot read config file"):
        config.load_file(path)


def test_invalid_toml(cfg: Path) -> None:
    write(cfg, "url = http://pi.hole\n")
    with pytest.raises(ConfigError, match="invalid TOML") as info:
        config.load_file(cfg)
    assert "quotes" in (info.value.hint or "")


def test_invalid_utf8(cfg: Path) -> None:
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(b'url = "\xff"\n')
    with pytest.raises(ConfigError, match="UTF-8"):
        config.load_file(cfg)


def test_full_valid_file(cfg: Path) -> None:
    write(
        cfg,
        """
url = "http://pi.hole"
password = "secret"
verify_tls = "/etc/ssl/pihole.pem"
timeout = 2
preset = "default"
theme = "default"
color = 256
width = "auto"
height = 10
overflow = "CLIP"
tab_size = 2
missing = "-"
strict = true
cache_ttl = 30
timestamp = "%H:%M"

[styles]
accent = "bold #ff8800"

[profiles.backup]
url = "https://pihole2.lan"
password_command = ["pass", "show", "pihole2"]

[profiles.backup.styles]
good = "green"
""",
    )
    file = config.load_file(cfg)
    assert file.exists
    assert file.settings["timeout"] == 2.0
    assert file.settings["color"] == "256"
    assert file.settings["overflow"] == "clip"
    assert file.styles == {"accent": "bold #ff8800"}
    assert file.profiles["backup"]["password_command"] == ("pass", "show", "pihole2")
    assert file.profile_styles["backup"] == {"good": "green"}
    assert file.contains_password
    assert "password" not in repr(file).lower() or "secret" not in repr(file)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("urll = 'x'", "unknown setting 'urll'"),
        ("timeout = 0", "timeout"),
        ("timeout = true", "timeout"),
        ("timeout = 'fast'", "timeout"),
        ("height = -1", "height"),
        ("height = 1.5", "height"),
        ("width = 'wide'", "width"),
        ("color = 'pink'", "color"),
        ("overflow = 'hide'", "overflow"),
        ("strict = 'yes'", "strict"),
        ("verify_tls = 1", "verify_tls"),
        ("verify_tls = ''", "verify_tls"),
        ("timestamp = 3", "timestamp"),
        ("timestamp = ''", "timestamp"),
        ("password_command = []", "password_command"),
        ("password_command = [1]", "password_command"),
        ("url = ''", "url must not be empty"),
        ("url = 5", "url"),
        ("cache_ttl = -1", "cache_ttl"),
        ("cache_ttl = inf", "cache_ttl"),
        ("styles = 'bold'", "styles must be a table"),
        ("styles = { accent = 1 }", "styles.accent"),
        ("profiles = 'x'", "profiles must be tables"),
        ("profiles = { x = 1 }", "profiles.x must be a table"),
        ("[profiles.x]\nurll = 'x'", "'profiles.x.urll'"),
        ("password = 'a'\npassword_file = 'b'", "mutually exclusive"),
        ("preset = 'a'\ntemplate = 'b'", "mutually exclusive"),
        ("unknown_thing = 1", "unknown setting"),
    ],
)
def test_invalid_values(cfg: Path, line: str, message: str) -> None:
    write(cfg, line + "\n")
    with pytest.raises(ConfigError, match=message):
        config.load_file(cfg)


@pytest.mark.parametrize("value", [33, 1000000, -1])
def test_tab_size_is_bounded(cfg: Path, value: int) -> None:
    write(cfg, f"tab_size = {value}\n")
    with pytest.raises(ConfigError, match=r"tab_size.*0 \(no tabs\) to 32"):
        config.load_file(cfg)
    write(cfg, "tab_size = 32\n[profiles.p]\ntab_size = 0\n")
    assert config.load_file(cfg).settings["tab_size"] == 32


@pytest.mark.parametrize(
    "line",
    [
        "password = 31415926",  # an unquoted numeric password
        "password = ['hunter22']",
        "[profiles.x]\npassword = 271828182",
    ],
)
def test_wrong_typed_password_is_not_echoed(cfg: Path, line: str) -> None:
    # The error is raised before the CLI can register the value for redaction.
    write(cfg, line + "\n")
    with pytest.raises(ConfigError, match="password") as info:
        config.load_file(cfg)
    message = str(info.value)
    for secret in ("31415926", "hunter22", "271828182"):
        assert secret not in message
    assert "expected a string" in message


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("thme = 'nord'\n", "unknown setting 'thme'"),
        ("timeout = 'x'\n", "invalid value for timeout"),
        ("[profiles.p]\ntimeout = 0\n", "invalid value for profiles.p.timeout"),
        ("preset = 'default'\ntemplate = 'g.txt'\n", "preset and template are mutually"),
    ],
)
def test_errors_in_the_file_name_the_file(cfg: Path, text: str, message: str) -> None:
    write(cfg, text)
    with pytest.raises(ConfigError) as info:
        config.load_file(cfg)
    assert str(info.value).startswith(f"{cfg}: ")
    assert message in str(info.value)


def test_preset_and_template_hint_says_which_line_to_delete(cfg: Path) -> None:
    # `pihello init` always writes `preset = ...`; adding `template = ...` later
    write(cfg, "preset = 'default'\ntemplate = 'g.txt'\n[profiles.p]\nurl = 'x'\n")
    with pytest.raises(ConfigError) as info:
        config.load_file(cfg)
    assert info.value.hint is not None
    assert f"delete the `preset` line in {cfg}" in info.value.hint
    write(cfg, "[profiles.p]\npreset = 'a'\ntemplate = 'b'\n")
    with pytest.raises(ConfigError) as info:
        config.load_file(cfg)
    assert "profiles.p.preset and profiles.p.template" in str(info.value)
    assert info.value.hint is not None
    assert "[profiles.p]" in info.value.hint


def test_unknown_key_suggestion(cfg: Path) -> None:
    write(cfg, "urll = 'x'\n")
    with pytest.raises(ConfigError) as info:
        config.load_file(cfg)
    assert info.value.hint == "did you mean 'url'?"


def test_unknown_key_without_suggestion_has_generic_hint(cfg: Path) -> None:
    write(cfg, "zzzzzz = 1\n")
    with pytest.raises(ConfigError) as info:
        config.load_file(cfg)
    assert "docs/configuration.md" in (info.value.hint or "")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_permission_warning_only_with_password(cfg: Path) -> None:
    write(cfg, "url = 'http://pi.hole'\n", mode=0o644)
    assert config.load_file(cfg).permission_warning() is None
    write(cfg, "password = 'x'\n", mode=0o644)
    warning = config.load_file(cfg).permission_warning()
    assert warning is not None
    assert "chmod 600" in warning
    write(cfg, "password = 'x'\n", mode=0o600)
    assert config.load_file(cfg).permission_warning() is None
    write(cfg, "[profiles.a]\npassword = 'x'\n", mode=0o640)
    assert config.load_file(cfg).permission_warning() is not None


# --------------------------------------------------------------------------- #
# Precedence                                                                  #
# --------------------------------------------------------------------------- #
def test_defaults_without_anything(cfg: Path) -> None:
    settings = config.resolve({}, env={})
    assert settings.url is None
    assert settings.credential is None
    assert settings.password() is None
    assert settings.template == config.TemplateChoice("preset", "default", "default")
    assert settings.theme == "default"
    assert settings.width == "auto"
    assert settings.timeout == 3.0
    assert settings.origins["url"] == "default"
    assert settings.origins["password"] == "not set"
    assert settings.config.exists is False


def test_precedence_cli_env_profile_config_default(cfg: Path) -> None:
    write(
        cfg,
        """
url = "http://config"
theme = "config-theme"
timeout = 9
missing = "config"
height = 3
[profiles.p]
url = "http://profile"
theme = "profile-theme"
timeout = 8
""",
    )
    env = {"PIHELLO_URL": "http://env", "PIHELLO_THEME": "env-theme"}
    settings = config.resolve({"url": "http://cli"}, env=env, profile="p")
    assert settings.url == "http://cli"
    assert settings.origins["url"] == "command line (--url)"
    assert settings.theme == "env-theme"
    assert settings.origins["theme"] == "environment (PIHELLO_THEME)"
    assert settings.timeout == 8.0
    assert settings.origins["timeout"].startswith("profile 'p'")
    assert settings.missing == "config"
    assert settings.origins["missing"].startswith("config file")
    assert settings.tab_size == 4
    assert settings.origins["tab_size"] == "default"
    assert settings.profile == "p"
    assert settings.origins["profile"] == "command line (--profile)"


def test_profile_from_environment(cfg: Path) -> None:
    write(cfg, "[profiles.home]\nurl = 'http://home'\n")
    settings = config.resolve({}, env={"PIHELLO_PROFILE": "home"})
    assert settings.url == "http://home"
    assert settings.origins["profile"] == "environment (PIHELLO_PROFILE)"


def test_unknown_profile(cfg: Path) -> None:
    write(cfg, "[profiles.backup]\nurl = 'http://b'\n")
    with pytest.raises(ConfigError, match="unknown profile 'backpu'") as info:
        config.resolve({}, env={}, profile="backpu")
    assert "did you mean 'backup'" in (info.value.hint or "")
    assert "backup" in (info.value.hint or "")


def test_unknown_profile_without_config(cfg: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist") as info:
        config.resolve({}, env={}, profile="x")
    assert "none defined" in (info.value.hint or "")


def test_explicit_config_path_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        config.resolve({}, env={}, config_path=tmp_path / "missing.toml")
    with pytest.raises(ConfigError, match="not found"):
        config.resolve({}, env={"PIHELLO_CONFIG": str(tmp_path / "missing.toml")})


def test_config_from_env_path(tmp_path: Path) -> None:
    path = write(tmp_path / "alt.toml", "url = 'http://alt'\n")
    settings = config.resolve({}, env={"PIHELLO_CONFIG": str(path)})
    assert settings.url == "http://alt"
    assert settings.config_origin == "PIHELLO_CONFIG"


def test_credential_group_resolves_as_a_unit(cfg: Path, tmp_path: Path) -> None:
    write(
        cfg,
        """
password = "config-pw"
[profiles.p]
password_command = "echo profile-pw"
""",
    )
    # The profile's password_command replaces the top-level password entirely.
    settings = config.resolve({}, env={}, profile="p")
    assert settings.credential is not None
    assert settings.credential.kind == "command"
    # The environment beats the profile.
    settings = config.resolve({}, env={"PIHELLO_PASSWORD": "env-pw"}, profile="p")
    assert settings.password() == "env-pw"
    # A command-line password file beats everything.
    pw = write(tmp_path / "pw", "file-pw\nsecond line\n")
    settings = config.resolve({"password_file": str(pw)}, env={"PIHELLO_PASSWORD": "env-pw"})
    assert settings.password() == "file-pw"
    assert settings.origins["password"] == "command line (--password-file)"


def test_two_password_env_vars_conflict(cfg: Path) -> None:
    with pytest.raises(ConfigError, match="set only one"):
        config.resolve({}, env={"PIHELLO_PASSWORD": "a", "PIHELLO_PASSWORD_FILE": "/b"})


def test_empty_env_vars_are_ignored(cfg: Path) -> None:
    settings = config.resolve({}, env={"PIHELLO_URL": "", "PIHELLO_PASSWORD": ""})
    assert settings.url is None
    assert settings.credential is None


def test_cli_conflicts(cfg: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be used together"):
        config.resolve({"password": "a", "password_file": "b"}, env={})
    with pytest.raises(ConfigError, match="--preset and -f"):
        config.resolve({"preset": "a", "template": "b", "_flags": {"template": "-f"}}, env={})


def test_cli_values_are_validated(cfg: Path) -> None:
    with pytest.raises(ConfigError, match="--width"):
        config.resolve({"width": "wide"}, env={})
    with pytest.raises(ConfigError, match="unknown command-line setting"):
        config.resolve({"bogus": 1}, env={})


def test_template_group(cfg: Path, tmp_path: Path) -> None:
    write(cfg, "template = 'greeting.txt'\n")
    settings = config.resolve({}, env={})
    assert settings.template.kind == "file"
    # relative paths in the config file are relative to the config directory
    assert settings.template.resolved_path() == cfg.parent / "greeting.txt"
    assert "template file" in settings.template.describe()
    settings = config.resolve({}, env={"PIHELLO_PRESET": "classic"})
    assert settings.template == config.TemplateChoice(
        "preset", "classic", "environment (PIHELLO_PRESET)"
    )
    assert settings.template.describe() == "preset 'classic'"
    settings = config.resolve({"template_string": "{x}"}, env={"PIHELLO_PRESET": "classic"})
    assert settings.template.kind == "string"
    assert settings.template.describe() == "inline template string"


def test_styles_merge_profile_over_config_over_cli(cfg: Path) -> None:
    write(
        cfg,
        """
[styles]
accent = "red"
good = "green"
[profiles.p.styles]
good = "lime"
""",
    )
    settings = config.resolve({"styles": {"bad": "maroon"}}, env={}, profile="p")
    assert settings.styles == {"accent": "red", "good": "lime", "bad": "maroon"}
    assert settings.origins["styles"] == "command line"
    assert config.resolve({}, env={}).origins["styles"] == "config file"


def test_relative_ca_path_is_relative_to_config_dir(cfg: Path) -> None:
    write(cfg, "verify_tls = 'certs/ca.pem'\n")
    assert config.resolve({}, env={}).verify == str(cfg.parent / "certs" / "ca.pem")
    assert config.resolve({"verify_tls": "rel.pem"}, env={}).verify == "rel.pem"


def test_verify_property_expands_home(home: Path, cfg: Path) -> None:
    settings = config.resolve({"verify_tls": "~/ca.pem"}, env={})
    assert settings.verify == str(home / "ca.pem")
    assert config.resolve({"verify_tls": False}, env={}).verify is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_resolve_reports_permission_warning(cfg: Path) -> None:
    write(cfg, "password = 'x'\n", mode=0o644)
    settings = config.resolve({}, env={})
    assert len(settings.warnings) == 1


def test_settings_repr_never_contains_the_password(cfg: Path) -> None:
    write(cfg, "password = 'hunter2-secret'\n")
    settings = config.resolve({}, env={})
    assert "hunter2-secret" not in repr(settings)
    assert settings.credential is not None
    assert "hunter2-secret" not in settings.credential.describe()
    assert settings.password() == "hunter2-secret"


# --------------------------------------------------------------------------- #
# Secrets                                                                     #
# --------------------------------------------------------------------------- #
def test_password_file(tmp_path: Path) -> None:
    path = write(tmp_path / "pw", "﻿secret \r\nignored\n")
    assert config.read_password_file(path) == "secret "


def test_password_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        config.read_password_file(tmp_path / "missing")
    with pytest.raises(ConfigError, match="empty"):
        config.read_password_file(write(tmp_path / "empty", ""))
    with pytest.raises(ConfigError, match="cannot read"):
        config.read_password_file(tmp_path)


def test_password_file_relative_to_config_dir(cfg: Path) -> None:
    write(cfg.parent / "password", "from-file\n")
    write(cfg, "password_file = 'password'\n")
    settings = config.resolve({}, env={})
    assert settings.password() == "from-file"
    assert settings.credential is not None
    assert str(cfg.parent / "password") in settings.credential.describe()


def test_password_from_stdin() -> None:
    import io

    cred = config.Credential("stdin", "", "cli")
    assert cred.obtain(stdin=io.StringIO("pw\n")) == "pw"
    assert cred.describe() == "standard input"
    with pytest.raises(ConfigError, match="no password"):
        cred.obtain(stdin=io.StringIO(""))


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_password_command_list() -> None:
    assert config.run_password_command(py("print('s3cret'); print('other')")) == "s3cret"


def test_password_command_string_without_shell() -> None:
    command = f"{sys.executable} -c \"print('quoted pw')\""
    assert config.run_password_command(command) == "quoted pw"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_password_command_with_shell_syntax() -> None:
    assert config.run_password_command("echo piped | tr a-z A-Z") == "PIPED"


def test_password_command_failures() -> None:
    with pytest.raises(ConfigError, match="exit status 3: boom") as info:
        config.run_password_command(py("import sys; sys.stderr.write('boom\\n'); sys.exit(3)"))
    assert "try running it yourself" in (info.value.hint or "")
    with pytest.raises(ConfigError, match="printed nothing"):
        config.run_password_command(py("pass"))
    with pytest.raises(ConfigError, match="not found"):
        config.run_password_command("definitely-not-a-command-xyz")
    with pytest.raises(ConfigError, match="timed out"):
        config.run_password_command(py("import time; time.sleep(5)"), timeout=0.2)
    with pytest.raises(ConfigError, match="No closing quotation"):
        config.run_password_command('echo "unterminated')


def test_password_command_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ConfigError, match="cannot run password_command"):
        config.run_password_command(["x"])


def test_password_command_empty_string() -> None:
    with pytest.raises(ConfigError, match="empty"):
        config.run_password_command("   ")


def test_credential_describe_command() -> None:
    assert config.Credential("command", ("pass", "show", "x")).describe() == (
        "password_command 'pass show x'"
    )
    assert config.Credential("password", "pw").describe() == "set (hidden)"


# --------------------------------------------------------------------------- #
# TOML writing                                                                #
# --------------------------------------------------------------------------- #
def test_dumps_layout() -> None:
    text = config.dumps(
        {
            "url": "http://pi.hole",
            "password": 'a"b\\c\n\t\x01é',
            "verify_tls": False,
            "timeout": 2.5,
            "height": 0,
            "skip": None,
            "command": ["pass", "show"],
            "styles": {"accent": "bold"},
            "profiles": {"backup": {"url": "https://b"}, "odd key": {"inline": {"a": 1}}},
        },
        header="written by test\n\nsecond line",
    )
    assert text.startswith("# written by test\n#\n# second line\n\nurl = ")
    assert "skip" not in text
    assert '[styles]\naccent = "bold"' in text
    assert "[profiles]" not in text
    assert '[profiles."odd key".inline]' in text
    data = tomllib.loads(text)
    assert data["password"] == 'a"b\\c\n\t\x01é'
    assert data["profiles"]["backup"]["url"] == "https://b"
    assert data["command"] == ["pass", "show"]


def test_dumps_special_values() -> None:
    text = config.dumps(
        {"a": float("inf"), "b": float("-inf"), "c": float("nan"), "d": [{"x": 1}], "e": {}}
    )
    data = tomllib.loads(text)
    assert data["a"] == float("inf")
    assert data["b"] == float("-inf")
    assert data["c"] != data["c"]
    assert data["d"] == [{"x": 1}]
    assert data["e"] == {}
    assert config._toml_value({}) == "{}"
    with pytest.raises(TypeError):
        config.dumps({"x": object()})


toml_keys = st.text(min_size=1, max_size=12)
toml_scalars = st.one_of(
    st.text(max_size=30),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=False),
)


@settings(max_examples=150, deadline=None)
@given(
    st.dictionaries(
        toml_keys,
        st.one_of(
            toml_scalars,
            st.lists(st.text(max_size=10), max_size=4),
            st.dictionaries(toml_keys, toml_scalars, max_size=4),
        ),
        max_size=8,
    )
)
def test_dumps_round_trips(data: dict[str, Any]) -> None:
    assert tomllib.loads(config.dumps(data)) == data


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_write_private_is_atomic_and_private(tmp_path: Path) -> None:
    target = tmp_path / "new" / "dir" / "config.toml"
    config.write_private(target, "a = 1\n")
    assert target.read_text() == "a = 1\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    config.write_private(target, "a = 2\n")
    assert target.read_text() == "a = 2\n"
    assert [p.name for p in target.parent.iterdir()] == ["config.toml"]


def test_write_private_cleans_up_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_self: Path, _target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        config.write_private(tmp_path / "x.toml", "a = 1\n")
    assert list(tmp_path.iterdir()) == []
