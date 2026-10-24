"""Shell completion scripts for ``pihello completion {bash,zsh,fish}``.

The scripts are generated from the real :mod:`argparse` parsers, so every
subcommand and option is always covered. Preset, theme and profile names are
completed *dynamically* by calling ``pihello completion --names KIND`` (a
hidden option), so they stay correct after upgrades and config edits.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

__all__ = ["SHELLS", "names", "script"]

SHELLS = ("bash", "zsh", "fish")

_NAME_KINDS = ("commands", "presets", "themes", "profiles")
_FILE_DESTS = frozenset({"template", "config", "password_file", "ca_cert"})
_NAME_DESTS = {"preset": "presets", "theme": "themes", "profile": "profiles"}
_POSITIONAL_NAMES = {("presets", "name"): "presets", ("themes", "name"): "themes"}


def names(kind: str, *, describe: bool = False) -> list[str]:
    """Names for dynamic completion: ``commands``, ``presets``, ``themes``, ``profiles``.

    With ``describe``, commands, presets and themes come as ``name<TAB>description``
    (fish shows the description next to each candidate). Never raises for a
    broken config: completion must not print errors.
    """
    if kind == "commands":
        from pihello.cli import COMMANDS  # noqa: PLC0415

        return [f"{n}\t{d}" if describe else n for n, d in COMMANDS.items()]
    if kind == "presets":
        from pihello import presets  # noqa: PLC0415

        items = [(p.name, p.description) for p in presets.available()]
        return [f"{n}\t{d}" if describe else n for n, d in items]
    if kind == "themes":
        from pihello import themes  # noqa: PLC0415

        items = [(t.name, t.description) for t in themes.available()]
        return [f"{n}\t{d}" if describe else n for n, d in items]
    if kind == "profiles":
        from pihello import config  # noqa: PLC0415
        from pihello.errors import PiHelloError  # noqa: PLC0415

        try:
            path, _ = config.find_config_path()
            return sorted(config.load_file(path).profiles)
        except PiHelloError:
            return []
    raise ValueError(f"unknown completion kind {kind!r}; expected one of {_NAME_KINDS}")


@dataclass(frozen=True)
class _Option:
    flags: tuple[str, ...]
    help: str
    takes_value: bool
    optional_value: bool
    kind: str  # "" | "files" | "presets" | "themes" | "profiles" | "choices"
    choices: tuple[str, ...]
    metavar: str


@dataclass(frozen=True)
class _Positional:
    name: str
    help: str
    kind: str
    choices: tuple[str, ...]


@dataclass(frozen=True)
class _Command:
    name: str
    description: str
    options: tuple[_Option, ...]
    positionals: tuple[_Positional, ...]


def _value_kind(action: argparse.Action) -> str:
    if action.dest in _FILE_DESTS:
        return "files"
    if action.dest in _NAME_DESTS:
        return _NAME_DESTS[action.dest]
    if action.choices:
        return "choices"
    return ""


def _commands() -> list[_Command]:
    from pihello.cli import COMMANDS, build_parser  # noqa: PLC0415

    result = []
    for name, description in COMMANDS.items():
        parser = build_parser(name)
        options: list[_Option] = []
        positionals: list[_Positional] = []
        for action in parser._actions:
            if action.help == argparse.SUPPRESS:
                continue
            help_text = (action.help or "").replace("%%", "%")
            choices = tuple(str(c) for c in action.choices) if action.choices else ()
            if not action.option_strings:
                if name == "show":
                    continue  # ADDR / PASSWORD: nothing sensible to complete
                kind = _POSITIONAL_NAMES.get((name, action.dest), "choices" if choices else "")
                positionals.append(_Positional(action.dest, help_text, kind, choices))
                continue
            takes_value = action.nargs != 0
            options.append(
                _Option(
                    flags=tuple(action.option_strings),
                    help=help_text,
                    takes_value=takes_value,
                    optional_value=action.nargs == "?",
                    kind=_value_kind(action) if takes_value else "",
                    choices=choices,
                    metavar=str(action.metavar or action.dest).lower(),
                )
            )
        result.append(_Command(name, description, tuple(options), tuple(positionals)))
    return result


def script(shell: str) -> str:
    """Return the completion script for ``shell`` (bash, zsh or fish)."""
    commands = _commands()
    if shell == "bash":
        return _bash(commands)
    if shell == "zsh":
        return _zsh(commands)
    if shell == "fish":
        return _fish(commands)
    raise ValueError(f"unsupported shell {shell!r}; expected one of {', '.join(SHELLS)}")


# --------------------------------------------------------------------------- #
# bash                                                                        #
# --------------------------------------------------------------------------- #
def _bash(commands: list[_Command]) -> str:
    command_names = " ".join(c.name for c in commands)
    file_flags: set[str] = set()
    name_flags: dict[str, set[str]] = {"presets": set(), "themes": set(), "profiles": set()}
    choice_flags: dict[str, tuple[str, ...]] = {}
    value_flags: set[str] = set()
    for command in commands:
        for option in command.options:
            if not option.takes_value or option.optional_value:
                continue
            for flag in option.flags:
                if option.kind == "files":
                    file_flags.add(flag)
                elif option.kind in name_flags:
                    name_flags[option.kind].add(flag)
                elif option.kind == "choices":
                    choice_flags[flag] = option.choices
                else:
                    value_flags.add(flag)
    cases: list[str] = []
    if file_flags:
        cases.append(
            f"        {'|'.join(sorted(file_flags))})\n"
            "            _pihello_files\n"
            "            return ;;"
        )
    for kind, flags in name_flags.items():
        if flags:
            cases.append(
                f"        {'|'.join(sorted(flags))})\n"
                f'            _pihello_words "$(pihello completion --names {kind} 2>/dev/null)"\n'
                "            return ;;"
            )
    by_choices: dict[tuple[str, ...], list[str]] = {}
    for flag, choices in sorted(choice_flags.items()):
        by_choices.setdefault(choices, []).append(flag)
    for choices, choice_group in by_choices.items():
        cases.append(
            f"        {'|'.join(choice_group)})\n"
            f'            _pihello_words "{" ".join(choices)}"\n'
            "            return ;;"
        )
    if value_flags:
        cases.append(f"        {'|'.join(sorted(value_flags))})\n            return ;;")
    option_cases = []
    for command in commands:
        all_flags = " ".join(flag for option in command.options for flag in option.flags)
        option_cases.append(f'        {command.name}) opts="{all_flags}" ;;')
    positional_cases = []
    for command in commands:
        for positional in command.positionals[:1]:
            if positional.kind in name_flags:
                words = f'"$(pihello completion --names {positional.kind} 2>/dev/null)"'
            elif positional.kind == "choices":
                words = f'"{" ".join(positional.choices)}"'
            else:
                continue
            positional_cases.append(f"        {command.name}) _pihello_words {words} ;;")
    newline = "\n"
    return f"""\
# bash completion for pihello                              -*- shell-script -*-
# Install: pihello completion bash > ~/.local/share/bash-completion/completions/pihello
#      or: echo 'eval "$(pihello completion bash)"' >> ~/.bashrc

_pihello_words() {{
    COMPREPLY=($(compgen -W "$1" -- "$cur"))
}}

_pihello_files() {{
    if declare -F _filedir >/dev/null 2>&1; then
        _filedir
    else
        local IFS=$'\\n'
        COMPREPLY=($(compgen -f -- "$cur"))
    fi
}}

_pihello() {{
    local cur prev cmd opts
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    cmd=show
    if (( COMP_CWORD > 1 )); then
        case "${{COMP_WORDS[1]}}" in
            {"|".join(c.name for c in commands)}) cmd="${{COMP_WORDS[1]}}" ;;
        esac
    fi

    case "$prev" in
{newline.join(cases)}
    esac

    if [[ "$cur" == -* ]]; then
        case "$cmd" in
{newline.join(option_cases)}
        esac
        local IFS=$' \\t\\n'
        COMPREPLY=($(compgen -W "$opts" -- "$cur"))
        return
    fi

    if (( COMP_CWORD == 1 )); then
        COMPREPLY=($(compgen -W "{command_names}" -- "$cur"))
        return
    fi

    case "$cmd" in
{newline.join(positional_cases)}
    esac
}}

complete -F _pihello pihello
"""


# --------------------------------------------------------------------------- #
# zsh                                                                         #
# --------------------------------------------------------------------------- #
def _zsh_escape(text: str) -> str:
    """Escape help text for an ``_arguments`` spec inside single quotes."""
    for char in "\\[]:":
        text = text.replace(char, "\\" + char)
    return text.replace("'", "'\\''")


def _zsh_action(kind: str, choices: tuple[str, ...]) -> str:
    if kind == "files":
        return "_files"
    if kind in ("presets", "themes", "profiles"):
        return f"_pihello_names {kind}"
    if kind == "choices":
        return "(" + " ".join(choices) + ")"
    return " "


def _zsh_specs(command: _Command) -> list[str]:
    specs = []
    for option in command.options:
        help_text = _zsh_escape(option.help.splitlines()[0] if option.help else "")
        for flag in option.flags:
            spec = f"{flag}[{help_text}]"
            if option.takes_value:
                colons = "::" if option.optional_value else ":"
                metavar = _zsh_escape(option.metavar)
                spec += f"{colons}{metavar}:{_zsh_action(option.kind, option.choices)}"
            specs.append("'" + spec + "'")
    for index, positional in enumerate(command.positionals, start=1):
        action = _zsh_action(positional.kind, positional.choices)
        specs.append(f"'{index}::{_zsh_escape(positional.name)}:{action}'")
    return specs


def _zsh(commands: list[_Command]) -> str:
    described = "\n".join(f"        '{c.name}:{_zsh_escape(c.description)}'" for c in commands)
    branches = []
    for command in commands:
        specs = " \\\n                ".join(_zsh_specs(command))
        branches.append(
            f"        {command.name})\n"
            f"            _arguments -s -S : \\\n"
            f"                {specs}\n"
            "            ;;"
        )
    newline = "\n"
    return f"""\
#compdef pihello
# zsh completion for pihello
# Install: pihello completion zsh > ~/.zfunc/_pihello
#          and add  fpath+=~/.zfunc  before  compinit  in ~/.zshrc

_pihello_names() {{
    local -a items
    items=(${{(f)"$(pihello completion --names $1 2>/dev/null)"}})
    compadd -a items
}}

_pihello() {{
    local -a commands
    commands=(
{described}
    )
    local cmd=show
    if (( CURRENT > 2 )) && (( ${{commands[(I)${{words[2]}}:*]}} )); then
        cmd=${{words[2]}}
        shift words
        (( CURRENT-- ))
    elif (( CURRENT == 2 )) && [[ ${{words[2]}} != -* ]]; then
        _describe -t commands 'pihello command' commands
        return
    fi
    case $cmd in
{newline.join(branches)}
    esac
}}

if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
    _pihello "$@"
else
    compdef _pihello pihello
fi
"""


# --------------------------------------------------------------------------- #
# fish                                                                        #
# --------------------------------------------------------------------------- #
def _fish_quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _fish_flag_args(flags: tuple[str, ...]) -> str:
    parts = []
    for flag in flags:
        if flag.startswith("--"):
            parts.append(f"-l {flag[2:]}")
        elif len(flag) == 2:
            parts.append(f"-s {flag[1:]}")
        else:
            parts.append(f"-o {flag[1:]}")
    return " ".join(parts)


def _fish_values(kind: str, choices: tuple[str, ...]) -> str:
    if kind == "files":
        return "-r -F"
    if kind in ("presets", "themes", "profiles"):
        # name<TAB>description: fish shows the preset's description, not the option's
        return f"-x -a '(pihello completion --names {kind} --describe 2>/dev/null)'"
    if kind == "choices":
        return "-x -a " + _fish_quote(" ".join(choices))
    return "-x"


def _fish(commands: list[_Command]) -> str:
    command_list = " ".join(c.name for c in commands)
    lines = [
        "# fish completion for pihello",
        "# Install: mkdir -p ~/.config/fish/completions",
        "#          pihello completion fish > ~/.config/fish/completions/pihello.fish",
        "",
        "function __pihello_command",
        "    set -l tokens (commandline -opc)",
        f"    if test (count $tokens) -ge 2; and contains -- $tokens[2] {command_list}",
        "        echo $tokens[2]",
        "    else",
        "        echo show",
        "    end",
        "end",
        "",
        "function __pihello_using",
        "    test (__pihello_command) = $argv[1]",
        "end",
        "",
        "complete -c pihello -f",
    ]
    for command in commands:
        lines.append(
            f"complete -c pihello -n __fish_use_subcommand -a {command.name} "
            f"-d {_fish_quote(command.description)}"
        )
    for command in commands:
        lines.append("")
        condition = _fish_quote(f"__pihello_using {command.name}")
        for option in command.options:
            values = _fish_values(option.kind, option.choices) if option.takes_value else ""
            if option.takes_value and option.optional_value:
                values = ""
            help_text = option.help.splitlines()[0] if option.help else ""
            parts = [
                "complete -c pihello",
                f"-n {condition}",
                _fish_flag_args(option.flags),
                values,
                f"-d {_fish_quote(help_text)}" if help_text else "",
            ]
            lines.append(" ".join(part for part in parts if part))
        for positional in command.positionals[:1]:
            if positional.kind in ("presets", "themes", "profiles") or positional.kind == "choices":
                values = _fish_values(positional.kind, positional.choices).replace("-x ", "")
                lines.append(f"complete -c pihello -n {condition} {values}")
    return "\n".join(lines) + "\n"
