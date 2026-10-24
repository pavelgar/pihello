# Documentation style

How pihello's docs are organised and written.
Read this before you add a page or a section.

**On this page:** [Pages](#pages) · [Structure](#structure) · [Text](#text) · [Formatting](#formatting) ·
[Code blocks](#code-blocks) · [Callouts and collapsibles](#callouts-and-collapsibles) · [Links](#links)

## Pages

| Type | Pages | Rules |
| :-- | :-- | :-- |
| Landing | `README.md` | Also the PyPI description. Pitch, install, quick start, looks, the most common tasks, then a map of the docs. It only summarises: everything in it is also in `docs/`, in full. |
| Index | `docs/README.md` | One row per page, grouped by type. |
| Guide | `integrations.md`, `templates.md`, `troubleshooting.md`, `upgrading.md` | Task first. One H2 per task, tool or problem. Show the example before you explain it. Use numbered steps for procedures. Link to the reference instead of repeating it. |
| Reference | `commands.md`, `configuration.md`, `filters.md`, `variables.md`, `presets.md`, `themes.md` | Describe, don't teach. Start with an index table, then give every item a section with the same layout. Lists of items are tables. |
| Internals | `architecture.md`, `compatibility.md`, `CONTRIBUTING.md` | For contributors: contracts and recorded facts. |

`filters.md`, `variables.md`, `presets.md` and `themes.md` are generated.
Change the scripts, not the pages (see [Generated docs](../CONTRIBUTING.md#generated-docs)).
pihello itself prints the URL of `docs/configuration.md`, so don't rename that page.
`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/): one H2 per release, linked at the bottom of the file, with Added / Changed / Fixed / Removed H3s.

## Structure

- One H1, the page title. Below it, one or two sentences on what the page covers.
- A page with more than four H2 sections gets a contents line right after the intro (`**On this page:** [A](#a) · [B](#b)`).
  Pages that start with an index table don't need one, and neither does `README.md`, which has its row of quick links.
- Use H2 for sections and H3 for subsections. H4 is only for groups inside a reference item. Never skip a level.
- Headings are short noun phrases in sentence case: "Shell greeting", "HTTPS and certificates".
  When the heading is a literal name, write it as code: ``## `pihello init` ``, ``### `could not resolve host 'pi.hole'` ``.
  Headings never contain links, emoji or a trailing colon.
- Renaming a heading changes its anchor (lower case, spaces become `-`, punctuation is dropped). Update every link to it.

## Text

- Address the reader as "you". Use the present tense and the active voice. Keep sentences short; contractions are fine.
- Use British spelling (behaviour, customise), except *color*, which matches `--color`.
- Write *pihello* in lower case, even at the start of a sentence. Write *Pi-hole* with a hyphen. Pi-hole versions take a `v` (v6.8); pihello versions don't (2.0).
- Use the same word for the same thing: preset, theme, style, template, variable, filter, profile, app password.
- Use semantic line breaks: one sentence per line, and split a long sentence after a clause.
  Never break inside a heading, a table row or a link.

## Formatting

- `code` is for anything you type or pihello prints: commands, options, keys, values, paths, environment variables, template variables, and preset and theme names.
- Write options as `-W`, `--width N`: short form first, the placeholder once, as in `--help`.
  Placeholders are UPPER_CASE; explain them right after the block.
- Write a setting in its TOML form when you mean the file (`timeout = 1`) and as the option when you mean the command line (`--timeout 1`).
- Web interface labels are in *italics* (*Settings → Web interface / API*). Keys use <kbd>Ctrl</kbd>+<kbd>C</kbd>.
  Use **bold** for list lead-ins and for the one thing in a paragraph the reader must not miss.
- Tables: the first column is the item, in code. Free text goes in a column called **Description**, and examples go last.
  Cells start with a capital letter.
  A single phrase gets no full stop.

## Code blocks

- Always name the language. Use `sh` for commands to copy (no prompt, comments aligned) and `console` for a command with its output (`$ ` before the command).
  Use `text` for templates and plain output.
  For files, use the file's language: `toml`, `fish`, `powershell`, `tmux`, …
- A block that shows a file's content starts with a comment naming the file (`# ~/.config/pihello/config.toml`).
- Output must be real. Generate it with `--demo --color never` (add `TZ=UTC` for times).
  Cut long output with `…` on a line of its own.
- After a template, write "Output:" and put the rendered result in a `text` block.

## Callouts and collapsibles

- Use at most two GitHub alerts (`> [!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]`) per page.
  Never put one in `README.md`, because PyPI doesn't show them as alerts.
- Use `<details>` only for content where readers pick one option: a shell, a problem, a template source. Never hide something every reader needs.
  The summary names the content (**bash** · `~/.bashrc`), never "Click to expand".
  Leave a blank line after `</summary>`.

## Links

- Use relative links: `configuration.md#passwords` inside `docs/`, `docs/…` from `README.md`.
- The link text says where the link goes: [Passwords](configuration.md#passwords). Never use "here" or a file name.
  Link a term the first time it appears in a section, not every time.
- In `README.md`, write images as `<img src="docs/…">`.
  `pyproject.toml` rewrites those, and links to `docs/`, `CHANGELOG.md`, `CONTRIBUTING.md` and `LICENSE`, into absolute URLs for PyPI.
  Any other relative link breaks on PyPI.
