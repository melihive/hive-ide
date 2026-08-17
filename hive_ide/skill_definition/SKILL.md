---
name: hive-ide
description: Manage agent-aware tmux sessions scoped to the current directory.
---

# Hive IDE

Use `hive-ide` to manage the current directory's coding sessions.

## Install

On macOS, prefer `pipx` because Homebrew-managed Python may reject user-site
installs:

```bash
brew install pipx
pipx ensurepath
pipx install hive-ide
```

On Linux, `python3 -m pip install --user --upgrade hive-ide` is acceptable when
the Python environment allows user-site installs. If it reports an externally
managed environment, use `pipx install hive-ide`.

## Commands

- `hive-ide list`
- `hive-ide open [--driver=claude|codex|antigravity|term] [--working-dir=<PATH>]`
- `hive-ide create [--name=<NAME>] [--driver=claude|codex|antigravity|term] [--working-dir=<PATH>]`
- `hive-ide adopt --driver=claude|codex [--working-dir=<PATH>] [--limit=<N>]`
- `hive-ide create --driver=claude|codex --adopt [--reference=<ID>]`
- `hive-ide show --session-id=<ID>`
- `hive-ide current`
- `hive-ide plan [--session-id=<ID>]`
- `hive-ide chat [--session-id=<ID>]`
- `hive-ide plan-set --session-id=<ID> --path=<PATH>`
- `hive-ide attach-conversation --session-id=<ID> --reference=<REFERENCE>`
- `hive-ide working-dir-set --session-id=<ID> --working-dir=<PATH>`
- `hive-ide archive --session-id=<ID>`
- `hive-ide resume --session-id=<ID>`
- `hive-ide monitor` or `hive-ide top`
- `hive-ide repair --session-id=<ID>` or `hive-ide repair --all`
- `hive-ide force-rebuild --session-id=<ID>`
- `hive-ide clear-error --session-id=<ID>`
- `hive-ide relayout`
- `hive-ide switch-driver --session-id=<ID> --driver=<DRIVER>`
- `hive-ide source-set --session-id=<ID> --source=stable|dev|<python-path>`
- `hive-ide environment-setup --dev-checkout=<PATH>`
- `hive-ide hook-setup` (dry run), then `hive-ide hook-setup --apply`
- `hive-ide verify`

Sessions are directory-scoped. `hive-ide open` creates a default session for the
current directory when none exists. Run commands from the directory whose sessions
should be listed or changed. Do not guess a session ID when a command reports ambiguity.
Use the new-session modal's `new` / `adopt existing` toggle to visually adopt
Claude Code or Codex conversations for the current directory. The CLI `adopt`
command is mainly for automation or agent-driven maintenance.

`archive` closes the session's live tmux window before moving it out of the
active list. The JSON result includes `archive.memory_released`; if a live
window exists but cannot be closed, archive refuses instead of hiding an agent
that is still consuming memory.

Use `hive-ide monitor` (alias: `top`) to inspect local agent/sidebar memory by
session on Linux and macOS. Pass `--workspace` to limit the report to the current
workspace.

The right-side plan pane is read-only by default for known editors: `micro` uses
`-readonly true`, and `vim`/`nvim`/`vi`/`gvim` use `-R`. Unknown editors are launched
unchanged. Plan/task/scratchpad popups are intentional edit surfaces and stay editable.
To edit from a long-lived `micro` plan pane, press `Ctrl-e`, run `reload`, then press
`Ctrl-e` again and run `set readonly false`.
