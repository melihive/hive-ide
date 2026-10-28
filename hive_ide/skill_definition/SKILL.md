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
- `hive-ide create [--name=<NAME>] [--driver=claude|codex|antigravity|term] [--working-dir=<PATH>] [--prompt=<TEXT>]`
- `hive-ide adopt --driver=claude|codex [--working-dir=<PATH>] [--limit=<N>]`
- `hive-ide create --driver=claude|codex --adopt [--reference=<ID>]`
- `hive-ide show --session-id=<ID>`
- `hive-ide current`
- `hive-ide capabilities`
- `hive-ide label --session-id=<ID> --key=<KEY> (--value=<VALUE> | --clear)`
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
- `hive-ide wake --session=<ID> --prompt=<TEXT>`
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

The right-side plan pane is a viewer: for known editors it is launched read-only and
with the editor's own mouse handling off, so tmux keeps the mouse. `micro` uses
`-readonly true -mouse false`, and `vim`/`nvim`/`vi`/`gvim` use `-R -c 'set mouse='`.
Unknown editors are launched unchanged. Leaving the mouse to tmux is what makes a click
focus the pane, a drag select copyable text, and the wheel scroll; an editor that grabs
the mouse turns every drag into its own selection instead. These are launch flags, so a
plan pane that is already open keeps the editor it was started with until the pane is
reopened — `micro`'s own `set mouse false` is not used because micro writes a global
option through to the user's `settings.json`. Plan/task/scratchpad popups
are intentional edit surfaces and stay editable. To edit from a long-lived `micro` plan
pane, press `Ctrl-e`, run `reload`, then press `Ctrl-e` again and run
`set readonly false`.

Plan paths may be relative to the workspace/current working directory or absolute.
Absolute paths are accepted as-is, so standalone/non-Hive users can attach personal
plan files outside the repository root.

Labels are a small key -> short-text map on a session (at most 8 keys; key
`^[a-z][a-z0-9_-]{0,15}$`; value at most 32 printable characters). The session
card shows one row per label. Old records read as having no labels.

hive-ide never types a message into an agent pane. A session that needs a first
instruction gets it at launch: `create --driver=claude|codex --prompt=<TEXT>`
stores it and the first launch passes it as the driver's initial prompt argument
(`claude <TEXT>`, `codex <TEXT>`); it is then dropped, so a relaunch never repeats
it. The prompt is at most 4000 characters, has no control characters other than
newline and tab, and must not start with `-`. It is refused with `--adopt` and for
any other driver. Probe `capabilities` for `launch-prompt` before relying on it.

`wake --session=<ID> --prompt=<TEXT>` wakes a SLEEPING session (one put to sleep
with `sleep`, shown with the sleeping glyph) and resumes its conversation with
`<TEXT>` as a one-shot launch prompt: `claude --resume <id> <TEXT>`,
`codex resume <thread> <TEXT>`. It builds the session's window, or relaunches the
agent pane of the window it already has; nothing is typed, the window is not
selected, and the prompt is dropped once delivered. The prompt follows the
`create --prompt` rules. It prints one JSON object: `{"ok": true, "woke": true,
"session": "<id>"}` and exits 0; a refusal exits 2 with `{"ok": false, "woke":
false, "reason": ...}` and changes nothing, where reason is `not_sleeping`,
`unknown_session`, `bad_prompt` or `driver_unsupported`. Any other failure exits 1
with a reason (`no_conversation` when there is nothing to resume,
`handoff_pending`, `conversation_backgrounded`, `launch_failed` — which puts the
session back to sleep — or `failed`). Probe `capabilities` for `wake-prompt`.

`driver-rename` is the one typed input left: the human asks for it, and it types
`/rename <name>` into their own agent pane. The gate never reads the screen; it
decides from hook state. Claude queues typed input, so a working or idle Claude is
allowed; it is refused (`claude_dialog_open`) while a dialog-type notification is
its latest hook event, and (`state_unknown`) before any hook has reported. Codex is
interrupted by typing mid-turn, so it is renamed only when the hook turn sequence
proves its last turn ended cleanly unless `--force`. It is refused while the pane is
in copy mode or dead, or when the agent has exited to a shell. Probe `capabilities`
for `labels` and `turn-sequencing` before relying on them.

Pane leases lend the PLAN pane until the child command exits or is released. The
package restores the currently linked plan; `repair` recovers a dead supervisor.
Probe `capabilities` through the session's pinned interpreter before acquiring.
Live leases protect the pane from replacement unless `--force` is explicit.
`<prefix> m` focuses the lease; `<prefix> g` focuses without steering the editor
while leased. The sidebar plan slot shows `▶`.
