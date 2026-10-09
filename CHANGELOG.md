# Changelog

All notable changes to `hive-ide` will be documented in this file.

## Unreleased

### Fixed

- The sidebar's `leased` plan state no longer reuses the `working` status arrow.
  One glyph meant two unrelated states, told apart only by colour and column; a
  leased PLAN pane now shows its own icon.
- `SidebarGrid.cell_width` measures emoji presentation as two cells. U+1F441 EYE
  is East_Asian_Width Neutral, so an icon using it measured one cell while every
  terminal draws two, padding its column one past every other icon.

### Added

- The `monitor` activity icon ships as a package default, in emoji presentation,
  so consumers no longer seed it per machine.

## [1.0.88] - 2026-10-08

### Fixed

- Ordinary PLAN pane mutations now reap dead leases and restore pane settings
  before replacing content, preventing later repair or release from killing
  the recovered editor through a stale lease.
- Lease child arguments containing semicolons, spaces, quotes, or shell syntax
  are passed literally through tmux's command parser.
- New lease tokens start with `t`, preventing leading dashes from breaking
  `pane-release --token TOKEN`. The supported `--token=TOKEN` form also accepts
  older tokens beginning with a dash.
- Lease restoration preserves inherited `remain-on-exit` settings by removing
  the temporary pane override, and restores explicit pane values such as `failed`.

### Added

- Public PLAN pane leases: `pane-lease`, token-protected `pane-release`,
  `pane-status`, and a pinned-interpreter `capabilities` probe. A package-owned
  tty supervisor restores the currently linked plan after child exit or release;
  repair and reacquisition recover a dead supervisor.
- Live lease protection for plan changes, repair, missing-pane restoration, and
  rebuild, with an explicit `--force` override that retains the caller-pane guard.
- `HIVE_IDE_PANE_ROLE` on every pane spawn and `HIVE_IDE_PANE_LEASE` for borrowed
  commands; sidebar `▶` lease indicator and `<prefix> m` lease focus. Plan focus
  (`<prefix> g`) does not send editor commands while the pane is borrowed.

## [1.0.87] - 2026-10-05

### Fixed

- Waking a sleeping agent from the session options menu always refused with
  "the agent pane is idle but this command may be running inside it". The menu
  runs in a tmux popup, which has `$TMUX` but no `$TMUX_PANE`, so the
  caller-identity verdict was "cannot tell" and the relaunch declined itself.
  When tmux places the caller on this server and gives it no pane, the caller is
  not in a pane here and cannot be the pane being respawned. `$TMUX` is required
  for this, not the `HIVE_IDE_TMUX_SOCKET` marker, which a process can inherit
  from elsewhere — so marker-only evidence still defers, and the repair
  self-guard is unchanged.

### Added

- Abandoned tmux sockets are swept when a frame opens. A socket file outlives
  its server, so every workspace that has ever opened a frame and every test run
  leaves one behind; one machine had 934 entries, most of them dead.

  Deleting a live server's socket would cut off every client attached to it, so
  the sweep is timid by construction. A file is removed only when all of these
  hold, and any one that cannot be proven keeps it: it sits directly in this
  frame's own tmux directory; its name is one this package hands out; it is a
  socket by `lstat`, not a symlink, file or directory; it is owned by this user;
  it is not the socket in use; it has not been touched for an hour, so a server
  binding now is not caught mid-start; connecting is explicitly refused, any
  other error being unknown; and its device, inode and mtime are unchanged when
  re-checked immediately before the unlink, so a server that starts while the
  sweep is deciding is skipped rather than deleted.

  The sweep is bounded, each removal is independent, and `open` swallows every
  failure — a socket left behind costs nothing but clutter.

## [1.0.86] - 2026-10-04

### Fixed

- Repair could kill the agent that invoked it. `repair` runs from inside the
  session's own window when the Hive skill calls it from the agent pane, and
  three of its branches (agent pane missing, agent environment belonging to
  another session, any pane cwd deleted) rebuilt the window: build a replacement,
  `kill-window` the old one, agent dead with exit 137. `Frame.rebuild` now asks
  where the calling process lives before it builds anything, and the answer is
  tri-state and fails closed. `$TMUX_PANE`, `$TMUX` and the IDE marker
  `HIVE_IDE_TMUX_SOCKET` are combined: "outside" (the only verdict that permits
  a kill) requires either no tmux evidence at all or the marker and `$TMUX`
  agreeing on a different server (compared by full socket path, honouring
  `TMUX_TMPDIR`), "inside" requires the evidence to agree on this server and the
  pane to be listed in the window, and every partial or contradictory reading —
  a pane id with no server evidence, a server marker without a pane id, a
  marker that disagrees with `$TMUX`, a `$TMUX` that does not parse as
  `<absolute-socket-path>,<pid>,<session>`, a window whose panes cannot be
  listed — is unknown and defers. A deferred rebuild destroys nothing and is reported as
  `deferred`. When `SessionRepair` defers it also leaves a
  `host.repair.deferred_rebuild` marker on the record and names the command to
  run from outside the window. `force-rebuild` from inside (or from an unknown
  location) refuses with an error rather than deferring silently.
- A deferred rebuild could be forgotten or finished too early. The marker is now
  cleared only by an apply-mode repair run from outside the window that was
  conclusive: every observation the rebuild checks depend on succeeded (pane
  roles listed, pane cwds listed, agent pane environment read), no error was
  recorded, and either the rebuild the marker was for completed or none of the
  observations wants a rebuild any more. A run that could not observe
  something, hit an error, rebuilt for a different reason, or found the owed
  condition still present but was pre-empted by another branch keeps the marker
  and says exactly why. A marker written by repair never triggers a rebuild on
  its own: the outside run re-evaluates the live checks and rebuilds only on
  their evidence.
- `switch-driver` from inside the window left the previous driver running with
  nothing to finish the job: it persisted the new driver, `rebuild` deferred,
  and ordinary repair had no way to notice the window still ran the old one. No
  live observation can tell a running driver's identity reliably (a launch and
  a resume may be different executables, and a driver can be a module behind
  one interpreter), so `switch-driver` now records the request itself: the
  deferred-rebuild marker carries `reason: driver-switch` and
  `requested_driver`, and names the exact command to run from outside. This is
  the one marker-driven rebuild: an apply-mode repair run from provably outside
  the window, while the record still names the requested driver, rebuilds on
  the strength of that marker (branch `driver-switch`) and clears it on
  completion. If the record names another driver again the switch was
  superseded and the marker is simply retired. From inside or from an unknown
  location the marker is kept and the same next step is reported. A pending
  switch survives unrelated deferrals: a later inside repair that has to defer
  for, say, a stale agent environment records that cause in the marker's
  `also_pending` list instead of replacing the switch, and settling prunes
  those causes on their own evidence while the switch itself is retired only by
  completion or supersession. A `switch-driver` or `force-rebuild` that does
  rebuild the window (run from outside) retires any marker still pending, since
  the rebuild it just performed satisfies it; otherwise the stale marker would
  rebuild the healthy window again on the next repair.
- The agent pane's identity was read from the deepest process in the pane that
  carried any `HIVE_IDE_*` variable. Agents spawn background jobs that drop or
  override `HIVE_IDE_SESSION_ID` for their subprocesses, so such a descendant
  could mask a genuine mismatch of the pane (a job without the id hid a wrapper
  that belonged to another session) and, worse, fake one — a job launched with
  another session's id would have had an outside repair rebuild a healthy
  window. The identity is now the environment of the pane's own root process
  (`#{pane_pid}`, the wrapper tmux spawned with the window's `-e` values);
  only when that environ is unreadable do the root's direct children stand in,
  in pid order and never deeper.
- A deleted sidebar or plan cwd rebuilt the whole window. The incident trigger
  was exactly this: `plan-set` had respawned the plan pane with the worktree as
  its cwd, the worktree was deleted by merge cleanup, and the plan pane's dead
  cwd took the agent pane down with it. Repair now observes which pane lost its
  cwd and respawns only sidebar/plan panes in place; a rebuild is reserved for
  the agent pane or an untagged pane nothing can relaunch.
- Respawned panes inherited the tmux session environment, which keeps the
  `HIVE_IDE_SESSION_ID` of whichever window first started the server, so a pane
  relaunched by `respawn_agent`, the sidebar refresh, the plan refresh or
  `current_plan` carried another session's identity and later tripped the
  stale-environment rebuild. Every `respawn-pane` and `split-window` the frame
  issues now passes the record's own `HIVE_IDE_*` environment (`sleep_agent`
  already did). The repair-driven respawn helpers (`respawn_agent`,
  `respawn_role_pane` and the sidebar/plan refreshes built on it, and the
  `current_plan` reopen) kill a pane only when it is provably not the caller's
  own; `sleep_agent` is deliberately exempt, because sleeping the agent from its
  own pane is the explicit request.
- A failed `list-panes` read as "every pane role is missing" and could authorize
  a rebuild. Pane observation now distinguishes unobservable (`None`) from
  absent (`{}`); when the roles cannot be observed repair warns "could not
  observe panes of window X; no rebuild" and skips every rebuild and respawn
  branch for that window (the sidebar refresh still runs its own, separate
  observation and respawns only on a successful one).
- A `kill-window` that failed after the replacement was built went unnoticed.
  `rebuild` now raises, naming both windows, so the caller never believes the
  old window is gone.

### Added

- A per-session repair log in the `repairs` state collection (newest 50 entries):
  every destructive repair step — a rebuild, a deleted-cwd respawn, an exited
  driver respawn, a stale sidebar refresh — is recorded as `planned` before its
  tmux call and then once more as `completed`, `deferred`, `failed` or `skipped`,
  with the caller pane, the caller-location verdict, the observed pane roles and
  cwds, and the run's actions, warnings and errors. A run with no destructive
  step records one `skipped` entry. Only identity environment keys are ever
  logged. The append runs under the workspace mutation lock, which is now
  owner-aware and fork-safe: the thread holding it may nest, any other thread
  waits for the real `flock`, a forked child gets a fresh guard and table (an
  `os.register_at_fork` hook, so a guard a sibling thread held at fork time
  cannot deadlock the child), and a child that unwinds out of a context the
  parent entered closes only its own fd copy — never `LOCK_UN`, which would
  release the parent's lock. `plan` joined the commands that hold it.
- `repair` results gain `deferred` (the reasons a rebuild was put off) and
  `rebuilt`.

### Changed

- Python API, for callers outside this package: `Frame.rebuild()` now returns
  `{"rebuilt", "deferred", "reason", "window"}` instead of `None`, and
  `Frame.role_panes()` returns `None` (not `{}`) when the window's panes cannot
  be listed. Older Hive skill wrappers keep working on the JSON surface, but a
  wrapper that treats a missing `rebuilt` key as "not rebuilt" reports an older
  package's completed rebuilds as not rebuilt; read `actions` when `rebuilt` is
  absent.

## [1.0.85] - 2026-10-02

### Fixed

- A session's activity, status dot and conversation id could land on a session
  in a different workspace. A hook's `HIVE_IDE_*` identity is inherited from the
  process that starts the agent, and Codex runs every TUI's commands and hooks
  as children of one shared `app-server --managed-daemon` that keeps the
  environment of whichever pane first started it. An event is now routed by its
  conversation reference, which the agent mints per event: an event for a
  conversation some session already owns goes to that owner, in whatever
  workspace it lives, and a new conversation whose recorded origin lies outside
  the named session's workspace is refused rather than claimed. An unknown
  origin changes nothing.
- A relayed hook re-ran identity discovery. The relay resolves identity on the
  originating hop and passes it explicitly, then runs the hook through
  `tmux run-shell` on the IDE server, whose own environment can carry an
  unrelated `TMUX_PANE` — which then overrode the identity the relay was told to
  write. Relayed events use the explicit identity only.
- The hook's pane lookup named no tmux server. A pane id is unique only within
  one server and every workspace runs its own, so a `TMUX_PANE` inherited from
  another server's pane resolved to a real but unrelated window. The lookup now
  targets the marked server, and a pane id is addressable only when the process
  is attached to that same server.
- A `dev` source refused any package-version drift, which left a dev-pinned
  session unopenable after every release: an editable install does not restamp
  its metadata when the checkout's version changes, so the pin drifted with
  nothing broken. A dev source now floats like a stable one. Protocol and schema
  remain the compatibility gate, and an explicit source stays strict.
- Adoption searched only the local workspace, and only each record's active
  driver reference, so it could create a second wrapper for a conversation
  another workspace already owned or that was parked on a session which had
  since switched driver. It now asks the same question the ownership check does,
  over the same ground.

## [1.0.84] - 2026-09-28

### Fixed

- The session options menu opened from the sidebar crashed as soon as it drew,
  so the popup flashed and closed. 1.0.83 read the linked plan as a mapping,
  but the menu loads sessions with the plan as a path string.

## [1.0.83] - 2026-09-28

### Added

- The session options rename prompt now has an "also rename driver" checkbox
  (Tab toggles) that sends `/rename` to the live agent in the same step. It
  defaults on for Claude and off for Codex, which a mid-turn `/rename` can stop.
- A "clear plan" session option unlinks the session plan and reloads the plan
  pane, shown only when a plan is linked.

### Fixed

- A session whose saved Claude or Codex conversation no longer exists now
  starts a fresh conversation when its agent pane opens, instead of a pane
  that exits on `--resume`. The same check runs in `repair`, whose preview
  reports the change without writing it. Only a conversation the driver's own
  store confirms is gone is dropped; an unreadable or unrecognised reference
  is left alone.
- An archived Codex conversation is kept and reported with the
  `codex unarchive <id>` remedy, since Codex refuses to resume it as-is.
- `attach-conversation` refuses a gone or archived conversation, and a
  conversation already owned by a session in any other workspace.
- Renaming a session now retitles the agent pane titlebar, not just the window.
- Changing or clearing a session plan with `plan-set` now reloads the plan pane
  and its title instead of leaving the previous plan on screen.
- Sidebar keep-alive loops now bound their tmux visibility probe, so a wedged
  tmux server cannot accumulate stuck `display-message` clients and make an IDE
  frame stop reacting after resize or stale-socket failures.

## [1.0.82] - 2026-09-09

### Fixed

- Activity markers now prefer the live tmux pane tags over stale inherited
  process environment, so release and procedure work keeps the correct sidebar
  icon when a long-lived chat pane was renamed, rebuilt, or adopted in place.

## [1.0.81] - 2026-09-09

### Fixed

- Terminal titles now always include the IDE host label, including local frames,
  so titles consistently render as `workspace IDE vivo` rather than only appending
  the host for SSH-opened sessions.

## [1.0.80] - 2026-09-09

### Fixed

- The package release now carries the `1.0.79` mouse-binding repair and
  visible-pane hook adoption fixes to PyPI after the tag-only `1.0.79` attempt
  failed before upload.
- CI now verifies tmux's default `MouseDown1Pane` repair without depending on
  tmux's column spacing in `list-keys`, unblocking the package publish after the
  tag-only `1.0.79` release attempt failed before PyPI upload.

## [1.0.79] - 2026-09-09

### Fixed

- `hive-ide open` now restores tmux's default `MouseDown1Pane` binding
  (`select-pane -t = ; send-keys -M`) after the `1.0.77` sidebar mouse
  regression, so refreshing an existing frame repairs a damaged live tmux
  key table instead of merely avoiding the bad binding for new frames.
- Agent status hooks now prefer the visible tmux pane's immutable
  `@hive_ide_session_id` over inherited `HIVE_IDE_SESSION_ID`, so a live chat pane
  whose shell environment is stale still updates and adopts the correct IDE
  session after `/clear` or an in-place restart.

## [1.0.78] - 2026-09-09

### Fixed

- Removed the frame-level `MouseDown1Pane` override introduced in `1.0.77`.
  Sidebar clicks now use the sidebar process's normal SGR mouse mode again, so
  left-click session selection is not intercepted by tmux before it reaches the
  sidebar.

## [1.0.77] - 2026-09-09

### Fixed

- `hive-ide repair` now reapplies live frame column widths for healthy
  existing windows, so a repair fixes drifted sidebar and plan pane geometry
  without rebuilding or respawning panes.
- Claude `/clear` sessions now stay attached to their IDE session: Claude
  `SessionStart` hooks are installed, and unowned new Claude session IDs
  reported from the active IDE driver pane replace the old resume reference.
- Sidebar mouse clicks now route through a frame-level tmux binding for Hive
  sidebar panes, so session switching does not depend on fragile per-pane
  application mouse mode after terminal, tmux, or resize state changes.

## [1.0.76] - 2026-08-31

### Fixed

- Post-restart `hive-ide open` now prebuilds sleeping session windows with
  shell-only agent panes, so first selecting an asleep session does not cause a
  delayed layout rebuild or wake the agent.

## [1.0.75] - 2026-08-31

### Fixed

- `hive-ide open` no longer wakes sleeping sessions after a machine restart.
  Missing sleeping windows are skipped during startup, and the all-sleeping
  case opens a single shell-only sleeping placeholder instead of launching an
  agent.

## [1.0.74] - 2026-08-27

### Fixed

- Sidebar click/Enter no longer wakes sleeping sessions or repairs missing
  sleeping windows. `hive-ide chat` remains the deliberate wake action.

## [1.0.73] - 2026-08-24

### Fixed

- Sleeping sessions now stay below awake sessions in the active sidebar order
  even when the sleep action just stamped a fresh activity time.
- Sidebar relative ages no longer count seconds; new activity stays blank until
  the first minute, then advances by minute/hour/day buckets.

## [1.0.72] - 2026-08-21

### Fixed

- Old workspace config snapshots that still contain the former sleeping-session
  crescent glyph now migrate to the current `💤` default at render time.

## [1.0.71] - 2026-08-21

### Fixed

- Sidebar panes now carry a source/version marker, so `repair` refreshes live
  sidebars after a package source upgrade instead of leaving old sidebar code
  visible until a manual pane respawn.

## [1.0.70] - 2026-08-21

### Changed

- Sleeping sessions now use the `💤` status emoji in the sidebar instead of the
  thin crescent glyph.

## [1.0.69] - 2026-08-21

### Added

- Added `hive-ide sleep` to stop a live agent process while keeping the session
  in the active sidebar list, with `hive-ide chat` as the deliberate wake path.
- The session options menu now offers `sleep agent` for agent-backed sessions.
- Documented and pinned absolute plan path support so standalone/non-Hive
  sessions can attach personal plan files outside the workspace root.

### Fixed

- Repair now preserves intentionally sleeping shell agent panes instead of
  treating them as crashed agents that should be respawned.

## [1.0.68] - 2026-08-17

### Fixed

- External release activity markers remain visible in package sidebars by
  bridging legacy activity state; active release markers now outrank
  compacting markers so deploy rows show the rocket instead of a stale brain.
- `source-set` no longer creates detached duplicate tmux frames when it only
  needs to update session source metadata; it repairs only an already-open
  matching window.

## [1.0.67] - 2026-08-17

### Fixed

- Frame startup and snap relayout now force tmux `aggressive-resize` off on
  existing IDE windows as well as future windows, preventing attached-client
  resizes from widening the fixed sidebar column again.
- Hidden session sidebars now exit instead of keeping one Python renderer alive
  per inactive IDE window; repair refreshes only stale sidebar wrappers so the
  new hidden-aware loop lands without rebuilding chat or plan panes.

## [1.0.66] - 2026-08-17

### Fixed

- `hive-ide monitor` no longer counts tmux frame server memory as session
  sidebar memory, so session info reports actual sidebar RSS instead of
  workspace frame overhead.
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
