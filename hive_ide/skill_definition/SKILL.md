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
- `hive-ide capabilities`
- `hive-ide label --session-id=<ID> --key=<KEY> (--value=<VALUE> | --clear)`
- `hive-ide nudge --session-id=<ID> --text=<LINE> [--force]`
- `hive-ide driver-rename --session-id=<ID> [--name=<NAME>] [--force]`
- `hive-ide pane-lease --session-id=<ID> --role=plan --title=<TITLE> -- <argv…>`
- `hive-ide pane-status [--session-id=<ID>]`
- `hive-ide pane-release --lease=<ID> --token=<TOKEN>`
- `hive-ide plan [--session-id=<ID>]`
- `hive-ide chat [--session-id=<ID>]`
- `hive-ide plan-set --session-id=<ID> --path=<PATH>`
- `hive-ide attach-conversation --session-id=<ID> --reference=<REFERENCE>`
- `hive-ide working-dir-set --session-id=<ID> --working-dir=<PATH>`
- `hive-ide sleep [--session-id=<ID>]`
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

`sleep` stops the live agent process, leaves the session in the active list, and
marks it with the sleeping status glyph. `hive-ide chat --session-id=<ID>` is the
deliberate wake path. The session options menu exposes `sleep agent` for
Claude/Codex-style sessions. `archive` is stronger: it closes the live tmux window
before moving the session out of the active list. The JSON result includes
`archive.memory_released`; if a live window exists but cannot be closed, archive
refuses instead of hiding an agent that is still consuming memory.

Use `hive-ide monitor` (alias: `top`) to inspect local agent/sidebar memory by
session on Linux and macOS. Pass `--workspace` to limit the report to the current
workspace.

The right-side plan pane is read-only by default for known editors: `micro` uses
`-readonly true`, and `vim`/`nvim`/`vi`/`gvim` use `-R`. Unknown editors are launched
unchanged. Plan/task/scratchpad popups are intentional edit surfaces and stay editable.
To edit from a long-lived `micro` plan pane, press `Ctrl-e`, run `reload`, then press
`Ctrl-e` again and run `set readonly false`.

Plan paths may be relative to the workspace/current working directory or absolute.
Absolute paths are accepted as-is, so standalone/non-Hive users can attach personal
plan files outside the repository root.

Labels are a small key -> short-text map on a session (at most 8 keys; key
`^[a-z][a-z0-9_-]{0,15}$`; value at most 32 printable characters). The session
card shows one row per label. Old records read as having no labels.

`nudge` types one caller-supplied constant line plus Enter into the agent pane
and returns `{sent, reason}`; it never composes text. The line must be printable
ASCII, at most 200 characters, and must not start with `/`, `!`, `#` or `-`.
The gate never reads the screen (pane width and hidden windows do not matter); it
decides from hook state. Claude queues typed input, so a working or idle Claude is
allowed; it is refused (`claude_dialog_open`) while a dialog-type notification is
its latest hook event, and (`state_unknown`) before any hook has reported. Codex
is interrupted by typing mid-turn, so it is nudged only when the hook turn
sequence proves its last turn ended cleanly (`codex_not_idle` otherwise) unless
`--force` (which skips only that Codex check). Any driver is refused
(`human_recent`) while a human typed in that pane within 60 seconds, while the
pane is in copy mode or dead, or when the agent has exited to a shell. The result
carries `basis: "hooks"`. `driver-rename` follows the same per-driver rule. Probe `capabilities`
for `labels`, `nudge` and `turn-sequencing` before relying on them.

Pane leases lend the PLAN pane until the child command exits or is released. The
package restores the currently linked plan; `repair` recovers a dead supervisor.
Probe `capabilities` through the session's pinned interpreter before acquiring.
Live leases protect the pane from replacement unless `--force` is explicit.
`<prefix> m` focuses the lease; `<prefix> g` focuses without steering the editor
while leased. The sidebar plan slot shows `▶`.
