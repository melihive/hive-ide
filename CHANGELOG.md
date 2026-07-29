# Changelog

All notable changes to `hive-ide` will be documented in this file.

## Unreleased

## [1.0.10] - 2026-07-29

### Added

- Sidebar status can now show a running subagent count under the right-side
  status dot when hooks report `subagents.running` or `subagents_running`.
- The session options modal opens from the configured shortcut or right-clicking
  a sidebar session, with actions for chat, plan, agent switch, rename, rebuild,
  and session card.

### Fixed

- Merged-worktree checkout icons no longer show the green merged check while the
  session still reports running subagents.

## [1.0.9] - 2026-07-29

### Added

- The new-session adoption picker now shows a short conversation preview and
  searches that preview text, making existing Claude and Codex conversations
  identifiable before adoption.
- The package CLI help now includes readable command summaries, global option
  descriptions, aliases, and examples.

### Fixed

- `hive-ide current-chat` now focuses an existing live agent pane instead of
  respawning it, avoiding accidental interruption of active Codex or Claude
  sessions.
- Codex resume commands now pass the session working directory with `-C`, so
  Codex no longer asks which directory to use when a conversation was last
  recorded from another cwd.
- The package CLI now supports `--quiet` for wrapper/TUI commands that should
  perform an action without printing JSON into the pane.
- Sidebar browse selection now uses the legacy high-contrast green/teal palette
  for focused rows.

## [1.0.8] - 2026-07-29

### Fixed

- The new-session modal now keeps the New/Adopt choice on the agent selection
  screen and uses left/right arrows to change it.

## [1.0.7] - 2026-07-29

### Added

- The new-session modal now supports a visual new/adopt toggle for Claude and
  Codex sessions. Adopt mode opens a searchable picker and creates the IDE
  session from the highlighted conversation.
- Codex CLI conversations for the current directory can now be adopted into
  `hive-ide` sessions.

## [1.0.6] - 2026-07-29

### Added

- Existing Claude Code conversations for the current directory can now be adopted
  into `hive-ide` sessions with `hive-ide adopt --driver=claude`; `create
  --driver=claude --adopt` imports the most recent one.

## [1.0.5] - 2026-07-29

### Fixed

- `hive-ide open` now bootstraps an empty workspace by creating one default
  terminal session named from the current folder, so a first-time user no longer
  needs to run `create` or choose a display name before opening the IDE.
- Bare `hive-ide create` now uses the configured default driver or `term`
  instead of silently defaulting to Claude.

## [1.0.4] - 2026-07-28

### Fixed

- Stable sidebar panes now tolerate package patch upgrades instead of exiting
  repeatedly when the installed `hive-ide` version changes under a live frame.

## [1.0.3] - 2026-07-28

### Fixed

- Session source repair can now update stale session records without requiring
  the session working directory to still exist.
- Source repair supports a quiet no-rebuild mode for metadata-only maintenance
  without disrupting live panes.
- The `<prefix> g` plan-jump binding no longer paints CLI JSON output into the
  plan pane while jumping to the first unfinished task.

## [1.0.2] - 2026-07-28

### Fixed

- Normal user-site, global, pipx, venv, and other standard Python installs now
  work without a managed `hive-ide` environment because internal helpers launch
  through the selected Python environment instead of isolated mode.
- Internal `python -m hive_ide...` command construction is centralized behind a
  single `PythonCommand` helper.

## [1.0.1] - 2026-07-28

### Fixed

- Opening a workspace now isolates sessions with missing directories or invalid
  package sources, records a session-scoped error, and continues building every
  healthy window instead of aborting the entire IDE.
- Relayout no longer crashes when every detached window starts at mobile size;
  the attached window's geometry is propagated across the frame, clearing stale
  zoom state, and package-source maintenance no longer changes session recency.
- Reopening an existing frame preserves its selected session instead of jumping
  to a record touched by maintenance.
- Clicking the sidebar `show archive` footer now opens the archived-session view.
- The change-agent modal now targets the active tmux socket and shows switch
  failures instead of silently closing.

## [1.0.0] - 2026-07-28

### Added

- Standalone, directory-scoped session storage keyed by immutable session IDs.
- A tmux frame with responsive sidebar, agent pane, plan pane, and configurable keys.
- Claude Code, Codex, Antigravity, and terminal drivers behind plugin entry points.
- Configurable sidebar state and icon-slot providers.
- Stable and editable development environments with per-session source switching.
- Agent status and compaction lifecycle hooks.
