# Changelog

All notable changes to `hive-ide` will be documented in this file.

## Unreleased

### Changed

- Replaced the public `rebuild` command with explicit `force-rebuild`; normal
  recovery stays on `repair`, and the internal `ensure` command is no longer
  exposed through the package CLI.

### Fixed

- `repair` now restores live windows that are missing required sidebar, chat, or
  plan panes, so a broken session does not require a separate rebuild command.
- `repair` no longer rebuilds a live window just because pane cwd differs from
  session metadata, preventing `on_merged` and worktree cleanup from killing the
  active Codex/Claude chat.

## [1.0.25] - 2026-07-30

### Fixed

- Claude and Codex hook setup now installs `SubagentStart` and `SubagentStop`
  receivers and tracks active subagents by structured `agent_id` hook payloads.
- Sidebar subagent counts now come from explicit hook status metadata only; the
  package does not scrape chat panes or transcripts for worker counts.
- `<prefix> g` now jumps directly to the first unfinished checkbox line instead
  of stopping at the containing section heading.

## [1.0.24] - 2026-07-30

### Fixed

- `current-plan` now runs safe session repair before opening the plan pane, so a
  deleted worktree cannot kill the command before the session is re-homed.
- Relative plan paths now resolve from the workspace root when a session's saved
  working directory is missing, preventing plan relinks from failing on stale
  worktree paths.
- Plan pane respawns now use a safe existing directory instead of blindly using
  stale session `working_dir` metadata.

## [1.0.23] - 2026-07-30

### Added

- Added `hive-ide repair` for safe session self-healing, including missing working
  directory re-home, missing-window ensure, pane-cwd rebuild, and session error
  recording when recovery needs operator attention.

### Fixed

- `hive-ide open`, `ensure`, and `rebuild` now run safe repair first, so a
  removed worktree no longer makes the session unclickable or blocks the whole
  frame from opening.
- `<prefix> r` now runs session repair before relayout.
- Session info cards now show the latest recorded session error and recovery hint.
- Session options rename now handles Backspace/Delete and preserves typed case.
- The session options modal now exposes `repair` as the normal recovery action
  instead of asking users to choose between repair and rebuild.
- Terminal titles now use the shorter folder-first form, for example
  `repo IDE`.
- Sidebar rows now show a tmux bell marker when a session window has a pending
  tmux bell/activity alert.

## [1.0.22] - 2026-07-30

### Fixed

- Relayout now uses the most recently active tmux client geometry and resizes
  both width and height, so switching between desktop and mobile clients restores
  the frame to the correct size instead of leaving stale desktop-height panes.
- Mobile sidebar, chat, and plan pane switching now synchronously transfers tmux
  zoom ownership to the selected pane, preventing the sidebar from getting stuck
  active in a one-column strip.
- Mobile popups now open near full-screen on narrow clients.
- Sidebar focus recovery now keeps a real `after-select-pane` relayout hook, so
  transient unzoomed mobile pane states self-correct on the next focus event.

## [1.0.21] - 2026-07-29

### Fixed

- Sidebar subagent counts now use only explicit `subagents.running` status
  metadata from hooks or commands. The sidebar no longer scrapes visible agent
  pane text, avoiding false positives from unrelated Claude background-session
  messages and other transcript text.
- Claude sessions now launch with the normal recorded `claude --resume <id>`
  command. Failed resume no longer falls through to `claude agents`; outside the
  frame, the fallback is a plain `claude` session.

## [1.0.20] - 2026-07-29

### Fixed

- Sidebar click and Enter activation now end sidebar browse focus after the
  target chat pane is selected, so the highlighted row follows the active
  session instead of leaving a stale focused item in the sidebar.
- Sidebar command execution is now routed through `SidebarCommandRunner`, giving
  tmux window selection, agent-pane focus, missing-window ensure, archive resume,
  and CLI mutation calls one tested boundary.
- Sidebar cursor reconciliation now uses `SidebarCursorState`, keeping selection
  identity, reorder behavior, and activation focus transitions isolated from the
  render loop.
- `relayout` help is pinned as a frame-level command and must not advertise
  per-session targeting.

## [1.0.19] - 2026-07-29

### Fixed

- Claude resume commands now fall back to `claude agents` when a resumed
  conversation is parked as a Claude Code background agent, so the pane offers
  Claude's attach UI instead of dropping to a dead shell.
- Claude resume commands now fall back to a plain `claude` launch when both the
  saved resume ID and `claude agents` are unavailable, so stale conversation IDs
  do not strand the session at a failed shell.
- `current-chat` now focuses or launches a plain agent command even before a
  conversation ID has been observed, which keeps freshly reset Claude sessions
  usable.
- Agent hooks no longer replace an existing session resume reference with a
  different hook-reported ID, preventing Claude background-agent IDs from
  poisoning the IDE session's resumable chat.
- Standalone `adopt` now requires an explicit `--reference` or `--limit` for
  non-dry-run imports, so a discovery command cannot accidentally create a
  sidebar full of adopted conversations.
- Sidebar keyboard focus now follows the selected session ID across automatic
  list reorders instead of staying on the old row index.
- Sidebar panes now derive the current/highlighted session from tmux's active
  IDE window, so the focused/current row stays synchronized across sessions.
- Selected current rows keep rendering their status glyph, making `▶` and
  waiting/error markers visible on green or teal backgrounds.
- Sidebar terminal-cell measurement now ignores ANSI color escapes, keeping
  styled relative timestamps separated from right-edge subagent counts.

## [1.0.18] - 2026-07-29

### Fixed

- `rebuild` now creates the replacement window before killing the old one, so an
  interrupted or failed rebuild cannot leave a live session record without a
  tmux window.
- Sidebar clicks and Enter activation focus the target session's agent pane
  instead of leaving focus in the sidebar.
- Current rows keep rendering their waiting/working status glyphs, including
  the configured `▶` working marker.
- Hook setup and verification now honor the configured stable interpreter
  instead of checking the retired managed stable environment path.
- The checkout slot now prefers live Git checkout inspection over historical
  merged-worktree metadata, so sessions re-homed to main no longer all show a
  green merged check.
- Live subagent fallback parsing no longer counts ordinary Claude transcript
  bullets as Codex rows, and subagent counts reserve a clearer gap from the
  relative timestamp.
- `--quiet` is accepted after a subcommand as well as before it, preventing
  wrapper/TUI argument ordering from dumping CLI JSON or argparse errors.

## [1.0.17] - 2026-07-29

### Fixed

- `current-plan` and `current-chat` are now quiet on success by default, so
  interactive pane actions open/focus their target without dumping JSON into the
  agent shell.

## [1.0.15] - 2026-07-29

### Fixed

- Sidebar subagent counts now persist live-pane fallback observations into
  session status and reserve a stable right-edge column, keeping counts visible
  for inactive rows and dense one-row layouts. Subagents render as a plain count,
  with no symbolic fallback icon.
- The default checkout busy marker now uses an hourglass emoji instead of an
  ellipsis, avoiding a visual clash with truncated text.
- The default working status marker is now `▶`, making active work read like a
  play/running indicator instead of another dot.
- The archive footer control now uses the larger one-cell `▼` marker.
- Relayout now repairs sidebar/agent/plan pane order by role tags before resizing,
  so manual or tmux-induced pane swaps do not leave the sidebar in the agent column.

## [1.0.14] - 2026-07-29

### Fixed

- Sidebar subagent counts now fall back to the visible live agent pane when
  hooks do not emit a count, covering Codex child-agent rows and Claude
  background-agent messages.
- Subagent counts now render even on the current row when there is no visible
  waiting/working status dot.

## [1.0.13] - 2026-07-29

### Fixed

- Creating or adopting a session from the new-session modal now switches to the
  newly created IDE window before focusing the agent pane.
- Restoring an archived session now repairs/ensures the tmux window, reapplies
  key bindings, and selects the restored session.
- The right-click session options menu now labels the existing info popup as
  `session info`.

## [1.0.12] - 2026-07-29

### Fixed

- Foreground workspace commands now opportunistically self-heal stale stable
  source version pins across active and archived sessions without rebuilding
  panes, so ordinary PyPI patch upgrades no longer require a manual
  `source-set` sweep.

## [1.0.11] - 2026-07-29

### Fixed

- Claude and Codex adoption candidates now show the conversation title or first
  useful message as the row label, with a compact relative timestamp and message
  preview instead of raw driver prefixes or conversation IDs.

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
- Stable sessions now self-refresh their stored package patch version when the
  installed package still matches the same protocol/schema, so missing-window
  recovery does not break after a normal package upgrade.

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
