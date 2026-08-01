# hive-ide

An agent-aware tmux workspace for running coding agents across multiple sessions.

> **Status: stable.** Version 1.x follows semantic versioning for its public interfaces.

## Requirements

Unix (Linux/macOS), `tmux`, and Python 3.10+.

## Install

macOS:

```sh
brew install pipx
pipx ensurepath
pipx install hive-ide
hive-ide open
```

Linux:

```sh
python3 -m pip install --user --upgrade hive-ide
hive-ide open
```

If your Python reports an externally managed environment, use `pipx install
hive-ide` instead. `pipx` is the recommended install method for CLI app users
because it keeps `hive-ide` in its own environment while exposing the command on
`PATH`.

Bundled drivers are `claude`, `codex`, `antigravity`, and `term`. State is user-local and
directory-scoped. Agent drivers require their corresponding local command; `term` opens a
regular shell. On first open, `hive-ide` creates a default session for the current directory.
It uses `term` by default so the IDE opens without any agent installed. Set a configured
default driver or pass `--driver=codex|claude|antigravity` when you want an agent session.

An existing Git worktree is just a session working directory. The IDE attaches to it but
never creates, merges, or removes it:

```sh
hive-ide open --driver=codex --working-dir=../project-feature
hive-ide working-dir-set --session-id=<ID> --working-dir=../project-feature
```

The sidebar marks linked worktrees as clean, modified, missing, or unknown.

Reopen the current session's plan or resume its recorded agent conversation:

```sh
hive-ide plan
hive-ide chat
```

Both commands use `HIVE_IDE_SESSION_ID` inside the frame, or accept
`--session-id=<ID>`. They target the session pane when its frame is running and
otherwise open in the current terminal.

Inside the tmux frame, press the new-session shortcut, enter an IDE label, choose
Claude or Codex, flip the bottom mode from `new` to `adopt existing`, then type to
filter and select the conversation to adopt. The same adoption backend is available
from the CLI for automation:

```sh
hive-ide adopt --driver=claude
hive-ide adopt --driver=codex
hive-ide open
```

To create one IDE session from a known or most recent conversation:

```sh
hive-ide create --name="FEATURE" --driver=claude --adopt --reference=<ID>
hive-ide create --name="FEATURE" --driver=codex --adopt --reference=<ID>
```

Local settings live in `~/.config/hive-ide/config.json` (or `HIVE_IDE_CONFIG`).
The plan editor accepts any command string or argv list. Resolution is configured
editor, `HIVE_IDE_EDITOR`, `micro` when installed, then `less`:

```json
{
  "editor": "nvim --clean",
  "keys": {
    "prefix": "C-a",
    "bindings": {"next": "n", "previous": "p", "error": null}
  },
  "diagnostics": {
    "relayout_trace": false
  },
  "sidebar": {
    "state": "activity",
    "slots": ["plan", "checkout", "ci"],
    "providers": {
      "ci": {
        "region": "slot",
        "source": "session",
        "path": ["host", "ci", "state"],
        "icons": {"passing": "✓", "failing": "❌"}
      }
    },
    "icons": {
      "drivers": {"codex": "C"},
      "providers": {"checkout": {"live": "W"}}
    }
  }
}
```

Sidebar symbols may occupy one or two terminal cells. `state` selects one mutually
exclusive provider; `slots` is an ordered list and may contain any number of columns.
Additional field providers can be declared in config. Python entry points in
`hive_ide.sidebar_providers` may contribute the same normalized field definitions; pane
processes read only the resulting JSON snapshot and never import plugin code.

Create managed stable and editable development environments, then switch one session
without affecting the others:

```sh
hive-ide environment-setup --dev-checkout=.
hive-ide source-set --session-id=<ID> --source=dev
hive-ide source-set --session-id=<ID> --source=stable
```

Preview machine-global Claude and Codex status hooks before applying them:

```sh
hive-ide hook-setup
hive-ide hook-setup --apply
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).

## About

`hive-ide` is part of [Meli Hive](https://melihive.com). It is being built as a standalone
MIT-licensed project with no dependency on the rest of the platform.
