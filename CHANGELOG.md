# Changelog

All notable changes to `hive-ide` will be documented in this file.

## Unreleased

### Fixed

- Opening a workspace now isolates sessions with missing directories or invalid
  package sources, records a session-scoped error, and continues building every
  healthy window instead of aborting the entire IDE.

## [1.0.0] - 2026-07-28

### Added

- Standalone, directory-scoped session storage keyed by immutable session IDs.
- A tmux frame with responsive sidebar, agent pane, plan pane, and configurable keys.
- Claude Code, Codex, Antigravity, and terminal drivers behind plugin entry points.
- Configurable sidebar state and icon-slot providers.
- Stable and editable development environments with per-session source switching.
- Agent status and compaction lifecycle hooks.
