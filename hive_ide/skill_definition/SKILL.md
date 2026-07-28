---
name: hive-ide
description: Manage agent-aware tmux sessions scoped to the current directory.
---

# Hive IDE

Use `hive-ide` to manage the current directory's coding sessions.

## Commands

- `hive-ide list`
- `hive-ide open [--driver=claude|codex|antigravity|term] [--working-dir=<PATH>]`
- `hive-ide create [--name=<NAME>] [--driver=claude|codex|antigravity|term] [--working-dir=<PATH>]`
- `hive-ide show --session-id=<ID>`
- `hive-ide current`
- `hive-ide current-plan [--session-id=<ID>]`
- `hive-ide current-chat [--session-id=<ID>]`
- `hive-ide plan-set --session-id=<ID> --path=<PATH>`
- `hive-ide attach-conversation --session-id=<ID> --reference=<REFERENCE>`
- `hive-ide working-dir-set --session-id=<ID> --working-dir=<PATH>`
- `hive-ide archive --session-id=<ID>`
- `hive-ide resume --session-id=<ID>`
- `hive-ide rebuild --session-id=<ID>`
- `hive-ide relayout`
- `hive-ide switch-driver --session-id=<ID> --driver=<DRIVER>`
- `hive-ide source-set --session-id=<ID> --source=stable|dev|<python-path>`
- `hive-ide environment-setup --dev-checkout=<PATH>`
- `hive-ide hook-setup` (dry run), then `hive-ide hook-setup --apply`
- `hive-ide verify`

Sessions are directory-scoped. `hive-ide open` creates a default session for the
current directory when none exists. Run commands from the directory whose sessions
should be listed or changed. Do not guess a session ID when a command reports ambiguity.
