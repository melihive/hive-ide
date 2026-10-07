# Pane leases (protocol 1)

A lease temporarily lends a session's PLAN pane to an argv command. The package
owns tmux and the supervisor; consumers never need to call tmux.

```sh
hive-ide pane-lease --session-id ID --role plan --title Monitor --owner-label client -- python -m my_monitor
hive-ide pane-status --session-id ID
hive-ide pane-release --lease LEASE_ID --token TOKEN
# The equals form also accepts older tokens beginning with a dash:
hive-ide pane-release --lease LEASE_ID --token=TOKEN
```

`--cwd DIR` overrides the child's working directory. Without it, the session's
working directory is used. Arguments after `--` are passed literally, including
`--quiet`, spaces, quotes, and semicolons; they are not interpreted as shell or
tmux commands. Use `sh -c '…'` explicitly when a shell is needed. Acquisition
returns immediately after spawning the
supervisor; the child may subsequently fail to start.

All commands accept the usual global `--state-home` and `--workspace-key` before
the subcommand. Acquisition/release also accept `--tmux-socket` for an explicitly
selected frame. Otherwise the saved frame configuration selects the socket.

Acquire returns `{lease_id, token, pane_id, session_id, role}`. Keep the token for
release. New tokens start with `t` followed by a URL-safe random string, so they
never begin with `-`. Treat tokens as opaque; existing tokens remain valid. Use
`--token=TOKEN` when supplying an older token that starts with `-`.
Status returns `{leases: [...]}`, with `alive` indicating supervisor
liveness. Status and refusal summaries omit the token. A live lease refuses a
second acquire with exit 2 and `{error: "pane_leased", lease: {...}}`. Release
errors also exit 2: `forbidden` carries `status: 403`; `lease_not_found` carries
`status: 404`. Tokens protect against accidental release by another client;
state is local to the same OS user, not a security boundary between users.

Probe `python -m hive_ide.cli capabilities` through the session record's
`source.interpreter`. The response is `{protocol_version, version, features}`;
require `pane-lease` in `features`. Do not infer support from version strings,
especially for editable installations. Acquisition also checks the pinned
interpreter before replacing content.

## Restoration and recovery

The child inherits the actual pane tty and owns its foreground process group.
The supervisor forwards SIGTERM/SIGINT and waits. On normal exit, failure, or
release it re-reads the session record and editor settings, restores the
**currently** linked plan using the regular plan command builder, reapplies both
pane titles, clears the lease, and execs the default command in place. A cleared
or unavailable plan displays `No plan linked.` The default command ends in an
interactive shell when the editor exits.

Restoration preserves the pane's previous `remain-on-exit` setting, including
`failed`. If the setting was inherited from the window or global scope, the
temporary pane override is removed so inheritance continues. Older lease
records without inheritance metadata restore their saved value.

Missing executables and nonzero exits in the first second print a reason and
pause briefly before restoration. All fast failures are reported, including
silent failures, so stdout/stderr can remain attached directly to the tty.
Release asks the supervisor to terminate the child; after five seconds it kills
the child's process group, keeping the supervisor alive to restore the pane.
Ordinary descendants in that process group are cleaned up when the command ends;
programs that deliberately daemonize into another session are outside this scope.

If restoration itself fails, the supervisor prints the reason and execs `$SHELL`
(with `/bin/sh` as a fallback) and clears the lease when state storage is writable.
SIGKILL cannot run cleanup: leased
panes use tmux's `remain-on-exit` until restored. `repair`, release, the next
acquire, and ordinary pane mutations (`plan`, path-changing `plan-set` including
`--clear`, plan refresh, missing-pane recovery, and rebuild) reap a dead
supervisor under the mutation lock before replacing content. Reaping restores
pane settings and clears the lease, so later repair or release cannot replace
the recovered content through a stale lease. Destroyed panes/windows
are recreated from their session record when recovery runs. There is no guarantee
of automatic recovery after loss of the tmux server, machine, or state storage.

`plan`, path-changing `plan-set`, and `force-rebuild` refuse a live lease.
`repair` and missing-pane recovery preserve it and report `pane_leased`.
`--force` explicitly revokes a lease before those commands replace content;
it never bypasses the caller-pane guard. Updating only `plan-set --active-task`
does not replace pane content and remains allowed. The title stays the borrowed
title across session rename and routine repair.

Every pane receives `HIVE_IDE_PANE_ROLE`. Borrowed commands additionally receive
`HIVE_IDE_PANE_LEASE`; the supervisor removes that value before returning to the
default command. The frame continues to identify roles from `@hive_ide_pane`.
Records live at `workspaces/<workspace-hash>/sessions/<session-id>/panes/plan.lease.json`
under the state home; all writes, releases, and restoration checks serialize on
the workspace `.mutation.lock`.

## Keys and sidebar

The default sidebar plan slot displays `▶` while leased (configurable as
`sidebar.icons.providers.plan.leased`). `<prefix> m` focuses the leased pane in
the current session; when there is none it displays a message. `<prefix> g`
focuses a leased PLAN pane without sending editor commands. See [keys](keys.md).
