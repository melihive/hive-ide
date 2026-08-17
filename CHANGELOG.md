# Changelog

All notable changes to `hive-ide` will be documented in this file.

## Unreleased

### Fixed

- The session info modal now includes live memory usage from `hive-ide monitor`,
  including total RSS/process count and agent/sidebar split when available.
- `hive-ide monitor` now supports macOS by reading RSS from `ps` and
  attributing agent processes through driver resume references when `/proc`
  environment data is unavailable.

## [1.0.65] - 2026-08-17

### Added

- Added `hive-ide monitor` / `hive-ide top` to report live local Hive IDE
  agent/sidebar memory grouped by session, with unmatched agent processes called
  out separately.

### Fixed

- `hive-ide archive` now closes a live tmux window before moving the session to
  archive state, reports whether memory was released, and refuses to hide a
  session if the live window exists but cannot be killed.

## [1.0.64] - 2026-08-16

### Fixed

- Snap relayout now clears tmux's per-window `window-size manual` override after
  correcting stale geometry, so the IDE keeps following attached client resizes.
- Removed IDE-managed Micro `repopath.maxwidth` updates; the Micro plugin owns
  statusline fitting again, independent of tmux hooks and relayout.
- Restored tmux pane titlebars with the IDE-owned `#{@hive_ide_title}` format.
- Reduced `client-resized` relayout back to a targeted current-window snap and
  kept Micro/statusline work out of that path, so Niri/Ghostty resize storms do
  not run package helpers across every IDE window.
- Resize relayout now prefers the latest attached tmux client geometry after
  debounce instead of trusting the hook's stale intermediate `window_width`.
- Removed the duplicate-geometry snap skip; tmux can leave panes proportionally
  drifted at the same final window size, so equal `window_width` is not proof
  that the sidebar and plan columns are already repaired.
- Removed the narrow-frame `after-select-pane` relayout hook and the sidebar
  heartbeat geometry repair path; sidebars render/input only and no longer race
  normal agent switching or chat pane redraws with their own tmux resize calls.

## [1.0.63] - 2026-08-16

### Fixed

- Disabled tmux pane titlebar rows in the IDE frame after rapid Ghostty/Niri
  window resizes proved they can block the tmux server for tens of seconds even
  with resize hooks removed.
- `hive-ide open` now preserves an existing saved tmux socket when refreshing a
  workspace, preventing detached duplicate IDE servers during package upgrades.
- Hidden sidebar panes now require both active window and active pane before
  treating themselves as focused, reducing background tmux polling from
  inactive session windows.

## [1.0.62] - 2026-08-16

### Fixed

- Resize relayout hooks now pass tmux window geometry directly, avoiding extra
  client/status geometry queries while tmux is already handling a resize burst.
- Relayout tmux subprocess calls now have short timeouts, so a stuck tmux
  `display-message` cannot leave long-lived background helpers that make the IDE
  feel frozen.

## [1.0.61] - 2026-08-15

### Fixed

- Relayout now removes redundant desktop `client-active` and `client-focus-in`
  snap hooks, leaving resize snaps on real client resizes and mobile-only focus
  snaps on narrow frames.
- Snap relayout now skips duplicate same-geometry events before entering the
  all-window tmux resize loop, and skipped debug trace entries no longer query
  tmux for expensive per-window state.

## [1.0.60] - 2026-08-15

### Fixed

- Current-plan handling now detects shell-wrapped live `micro` plan panes from
  the descendant process tree, so opening or repairing a plan can set read-only
  state in place without respawning the pane or interrupting adjacent chats.
- Hive IDE relayout now syncs the Micro `repopath.maxwidth` option from tmux
  pane geometry with `setlocal`, keeping responsive breadcrumbs pane-local and
  preventing statusline width updates from dirtying `settings.json`.

## [1.0.59] - 2026-08-15

### Fixed

- Plan panes now open known editors in read-only mode by default (`micro -readonly true`,
  `vim`/`nvim`/`vi`/`gvim -R`) so long-lived monitoring buffers cannot overwrite newer
  plan content. Existing live `micro` plan panes are switched to read-only in place
  instead of being respawned. Plan, task, and scratchpad popups remain editable.
- Repair now scans the full descendant process tree before treating a shell-wrapped
  agent pane as exited, preventing live Codex/Claude chats from being respawned when
  the driver is nested below an intermediate wrapper.
- Repair no longer rebinds frame keys and hooks as a side effect of checking or healing
  one session, so a healthy repair leaves the active chat pane alone.

## [1.0.58] - 2026-08-14

### Fixed

- Repair now preserves live shell-wrapped agent panes even when their IDE
  environment marker is stale, so repairing one session cannot interrupt an
  active Codex/Claude driver that is still running under the pane.

## [1.0.57] - 2026-08-14

### Fixed

- Repair now refreshes stale live pane titles for existing windows, so updated
  titlebar/chrome settings do not leave panes untitled after a package upgrade.
- Repair now preserves agent panes whose shell wrapper still has a live driver
  child process, avoiding accidental Codex/Claude interruption.

## [1.0.56] - 2026-08-14

### Fixed

- Sidebar and tmux pane chrome now keep workspace/session/plan labels in pane
  titlebars, keep the filter/archive/create footer visible at the bottom, and
  avoid one-row chat truncation by sizing windows from client height after tmux
  status rows are accounted for.
- Repair now treats a non-terminal agent pane that has fallen back to
  `sh`/`bash`/`fish`/`zsh` as an exited driver pane and respawns only that
  agent pane, while leaving real terminal sessions untouched.

## [1.0.55] - 2026-08-13

### Fixed

- Repair now validates that a session's pinned source interpreter can import
  `hive_ide`, so a broken dev environment is reported as a repair error instead
  of leaving the sidebar keepalive loop to print command fragments into the pane.

## [1.0.54] - 2026-08-13

### Added

- Added `hive-ide scratchpad` and the default `<prefix> s` shortcut to open a
  plan Scratchpad popup in `micro`, creating `## Scratchpad` before `## Tasks`
  when needed.
- Added plan and tasks popup actions to the session options modal; tasks opens
  at the first unfinished task when a `## Tasks` section exists.
- Grouped the session options modal into Open, Session, and Maintenance actions.

### Fixed

- Sidebar redraws now clear the visible pane before repainting, preventing stale
  path or header rows from surviving after resize, relayout, or a failed draw.

## [1.0.53] - 2026-08-11

### Fixed

- Each sidebar now treats its own session window as the current row instead of
  polling tmux for a global active session, eliminating delayed or wrong active
  highlights after switching sessions.

## [1.0.52] - 2026-08-10

### Fixed

- Relative plan links now resolve against the workspace root before a session
  worktree, so plan volume rolls can relink and respawn worktree-attached IDE
  sessions whose authoritative plan file exists only in the main checkout.

## [1.0.51] - 2026-08-10

### Fixed

- Rebuilding an inactive session now wakes the rebuilt agent pane and restores
  the previously selected pane, preventing Codex panes from staying visually
  blank until the session is manually selected.

## [1.0.50] - 2026-08-10

### Fixed

- Terminal title normalization now stamps both the dedicated tmux server's
  global title format and the active IDE session title format, so user tmux
  config cannot slowly restore a path-based title after relayout.

## [1.0.49] - 2026-08-10

### Fixed

- SSH-opened terminal titles now append the IDE host name, not the SSH client
  name, so a `gpd` terminal connected to `vivo` shows `workspace IDE vivo`.

## [1.0.48] - 2026-08-10

### Fixed

- Terminal titles append an SSH context label when the IDE is opened from a
  different machine, without changing tmux session or window labels.

## [1.0.47] - 2026-08-09

### Fixed

- Sidebar session activation now wakes the target window's sidebar process
  immediately after switching panes, so the active-row background tracks the
  chat pane without waiting for the idle refresh tick.

## [1.0.46] - 2026-08-08

### Fixed

- Repair now detects a live agent pane whose process environment belongs to a
  different IDE session and rebuilds the window, preventing hooks from updating
  the wrong sidebar row after a stale shell snapshot restores old
  `HIVE_IDE_*` variables.
- Session writes and repair now remove the dead `host.hive.legacy_record.plan`
  key while preserving the live legacy sidebar fields for plan status,
  subagent count, and merged-worktree state.

## [1.0.45] - 2026-08-06

### Fixed

- Driver conversation references are now owned by one active IDE session per
  driver. Switching agents no longer resumes another session's Claude/Codex
  chat when a stale parked resume id points at a conversation already attached
  elsewhere; repair removes those duplicate parked refs.

## [1.0.44] - 2026-08-06

### Fixed

- Driver handoff now passes the handoff prompt into resumed Claude and Codex
  sessions instead of only printing it before launch.
- Agent panes now leave a visible `hive-ide` error message when the driver
  command exits nonzero.

## [1.0.43] - 2026-08-06

### Fixed

- Plan jump now targets the last completed checkbox when every checkbox in the
  linked plan is already done.
- Crowded sidebars now render a fitting viewport instead of scrolling the repo
  header off-screen when there are more sessions than visible rows.

## [1.0.42] - 2026-08-05

### Fixed

- Switching drivers now rehomes the session working directory to the workspace
  root before resolving and rebuilding the new driver, so a previous worktree
  cwd cannot leak into the new Claude or Codex session.

## [1.0.41] - 2026-08-05

### Fixed

- Claude driver commands now propagate the IDE session display name with
  `--name`, including new sessions, adopted conversations, driver switching,
  rename, repair, and fresh fallback launches after a stale resume.
- The session options modal now exposes an explicit driver-name sync action for
  Claude and Codex that sends `/rename <IDE session name>` to the live agent
  pane when the user knows it is idle.

## [1.0.40] - 2026-08-04

### Fixed

- Relayout now repairs swapped sidebar/agent panes by live pane index with
  bounded retries, avoiding tmux-version-dependent pane ordering during
  resize and snap repair.

## [1.0.39] - 2026-08-04

### Fixed

- Codex and Claude subagent lifecycle hooks without a structured child ID now
  maintain a bounded anonymous running count, so IDE sidebar counts still update
  when a driver emits start/stop events without a payload.

## [1.0.38] - 2026-08-04

### Fixed

- Read-only popups now close on any keypress instead of mixing Enter-only and
  any-key behavior across info/help/error modals.
- The session options modal now includes archive and supports mouse clicks on
  action rows.

## [1.0.37] - 2026-08-04

### Fixed

- `repair` now refreshes a session driver's stored resume command after a
  working-directory repair, so Codex resumes do not keep `-C` pointed at a
  deleted worktree after the session is re-homed.
- `working-dir-set` now updates the saved driver resume command through the
  configured driver registry instead of leaving stale launch arguments behind.
- `repair` now rebuilds windows whose live panes are sitting in a deleted cwd,
  rather than preserving panes that cannot accept new turns.

## [1.0.36] - 2026-08-01

### Fixed

- Real tmux integration tests now clean up pytest-owned tmux/sidebar/agent
  children by test temp path and at pytest session finish, preventing failed or
  interrupted release gates from leaving CPU-burning sidebar loops alive.
- macOS install documentation now defaults to `pipx` so Homebrew-managed Python
  environments do not fail on PEP 668 externally managed package installs.

## [1.0.35] - 2026-08-01

### Added

- Enriched optional driver handoff packages with a target-driver prompt that
  summarizes the IDE session, working directory, plan, active task, and previous
  driver reference for the newly selected agent.
- Relayout tracing is now a normal config-backed diagnostic:
  `{"diagnostics": {"relayout_trace": true}}` writes JSONL records with client
  geometry, tmux chrome options, and pane geometry before/after each relayout.

### Changed

- Moved driver handoff payload construction into a dedicated `HandoffPackage`
  class so the switch-driver command no longer owns that state-shaping logic.
- The change-agent modal now renders the switch mode as an explicit
  `quick switch` / `handoff package` selector instead of a vague on/off toggle.

## [1.0.34] - 2026-08-01

### Added

- Added opt-in relayout debug tracing. Creating `layout.json.debug.enable`
  beside a workspace's layout state, or setting `HIVE_IDE_RELAYOUT_DEBUG=1`,
  writes JSONL records with hook, client, active-window, latest-client, and
  per-window geometry decisions so transient tmux resize jitter can be
  diagnosed without affecting normal users.

### Fixed

- Coalesced bursty snap relayout hooks so transient one-row client height
  changes do not resize every IDE window at intermediate heights.

## [1.0.33] - 2026-07-31

### Fixed

- `repair --dry-run` now reports live pane cwd drift, including sidebar panes
  still running from deleted worktree directories, instead of only detecting it
  on mutating repair runs.
- `repair` now warns when status-hook timestamps lag behind session activity or
  omit the remembered conversation reference, making stale/partial hook state
  visible without using tmux focus as activity.

## [1.0.32] - 2026-07-31

### Fixed

- `repair` no longer stamps `last_active` when it only re-homes broken session
  metadata, so clicking a broken session does not make it sort as recently
  agent-active.
- `repair` now reports a warning when a live agent pane has no status-hook
  state, making stale hook setups visible without scraping chat transcripts or
  treating tmux focus/redraw as activity.

## [1.0.31] - 2026-07-31

### Fixed

- `repair --name` now targets the named session instead of being overridden by an
  ambient `HIVE_IDE_SESSION_ID` from the current pane.

## [1.0.30] - 2026-07-31

### Added

- Added optional driver-switch handoff support. `switch-driver --handoff` now
  records the previous and target driver references, current plan, active task,
  and working directory, exposes the payload as `HIVE_IDE_HANDOFF_JSON`, and
  prints a short handoff preamble in the new driver pane.
- The change-agent modal can toggle the handoff package with left/right before
  switching drivers.

### Fixed

- Handoff payloads are now consumed after a successful agent-pane rebuild or
  respawn, so later repairs do not replay stale handoff context.

## [1.0.29] - 2026-07-31

### Added

- Added `hive-ide map`, a read-only local workspace/session tree that can
  filter by root or exact workspace and marks missing workspace/session
  directories.

### Changed

- Simplified the public session reopen commands to `hive-ide plan` and
  `hive-ide chat`, and updated TUI bindings to use those command names.

## [1.0.28] - 2026-07-31

### Fixed

- Publish verification is now CI-safe for the driver-switch resume regression;
  the test stubs driver availability instead of requiring Claude Code on the
  GitHub runner.
- Stable sessions pick up the existing `current-plan --focus` CLI support once
  refreshed, fixing plan-pane focus commands that failed on older package builds.

## [1.0.27] - 2026-07-31

### Fixed

- Agent switches now preserve per-driver resume ids, so switching from Claude to
  another driver and back resumes the original Claude Code conversation instead
  of starting a new one.

## [1.0.26] - 2026-07-30

### Changed

- Replaced the public `rebuild` command with explicit `force-rebuild`; normal
  recovery stays on `repair`, and the internal `ensure` command is no longer
  exposed through the package CLI.
- `source-set` and `working-dir-set` now update session metadata and run safe
  repair without rebuilding the live window; `force-rebuild` is required for an
  intentional process restart.

### Fixed

- `repair` now restores live windows that are missing required sidebar, chat, or
  plan panes, so a broken session does not require a separate rebuild command.
- `repair` no longer rebuilds a live window just because pane cwd differs from
  session metadata, preventing `on_merged` and worktree cleanup from killing the
  active Codex/Claude chat.
- `repair` restores missing sidebar or plan panes around an existing agent pane
  instead of killing the window; only a missing agent pane permits a full rebuild.

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
