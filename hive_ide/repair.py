"""Session self-healing for the package frame."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .agents import AgentResumeState
from .conversation import ConversationGuard
from .drivers import DriverRegistry
from .errors import HiveIdeError
from .frame import Frame
from .health import SessionHealth
from .pane_lease import LeaseStore, PaneLeases, serialized
from .source import inspect_interpreter
from .store import StateStore, utc_now


class SessionRepair:
    """Validate and safely repair one IDE session record/window."""

    COMPONENT = "repair"
    REQUIRED_PANE_ROLES = ("sidebar", "agent", "plan")
    IN_PLACE_ROLES = ("sidebar", "plan")
    REPAIR_LOG_LIMIT = 50
    LOG_STAGES = ("planned", "completed", "deferred", "failed", "skipped")

    def __init__(
        self,
        store: StateStore,
        frame: Frame,
        *,
        registry: DriverRegistry | None = None,
    ):
        self.store = store
        self.frame = frame
        self.registry = registry or DriverRegistry()

    @serialized
    def repair(
        self, record: dict[str, Any], *, apply: bool = True, force: bool = False
    ) -> dict[str, Any]:
        session_id = record["id"]
        actions: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []
        repaired = dict(record)

        lease_store = LeaseStore(self.store)
        lease = lease_store.read(session_id)
        if lease:
            if lease_store.alive(lease) and not force:
                warnings.append(
                    "pane_leased: plan pane is borrowed; live content preserved"
                )
            elif apply:
                try:
                    PaneLeases(self.frame).restore(lease, force=force)
                    actions.append("plan: reaped pane lease and restored current default")
                except HiveIdeError as exc:
                    errors.append(str(exc))

        self._drop_legacy_record_plan(repaired, actions, apply=apply)

        working_dir = Path(str(repaired.get("working_dir") or "")).expanduser()
        if not working_dir.is_dir():
            previous = str(working_dir)
            fallback = self.store.workspace_key
            actions.append(f"working_dir: {previous} -> {fallback}")
            if apply:
                host = dict(repaired.get("host") or {})
                repair_meta = dict(host.get("repair") or {})
                repair_meta.update(
                    {
                        "previous_working_dir": previous,
                        "repaired_at": utc_now(),
                        "reason": "missing_working_dir",
                    }
                )
                host["repair"] = repair_meta
                repaired["host"] = host
                repaired["working_dir"] = fallback
                self.store.write("sessions", session_id, repaired)

        self._remove_duplicate_conversation_refs(repaired, actions, warnings, apply=apply)
        checked = ConversationGuard(self.store, registry=self.registry).check(
            repaired, working_dir=self.frame.safe_working_dir(repaired), apply=apply
        )
        actions.extend(checked["actions"])
        warnings.extend(checked["warnings"])
        self.refresh_driver(repaired, actions, apply=apply)

        source = repaired.get("source") or {}
        interpreter = source.get("interpreter")
        if isinstance(interpreter, str) and interpreter:
            if not Path(interpreter).exists():
                errors.append(f"source interpreter missing: {interpreter}")
            else:
                try:
                    inspect_interpreter(interpreter)
                except HiveIdeError as exc:
                    errors.append(f"source interpreter invalid: {exc}")

        plan = (repaired.get("plan") or {}).get("path")
        if isinstance(plan, str) and plan:
            candidates = [Path(plan).expanduser()]
            if not candidates[0].is_absolute():
                candidates.insert(0, Path(str(repaired["working_dir"])) / plan)
            if not any(candidate.is_file() for candidate in candidates):
                warnings.append(f"plan file missing: {plan}")

        warnings.extend(SessionHealth(self.store, self.frame).hook_warnings(repaired))

        window_id = self.frame.windows().get(session_id)
        caller_location = self.frame.caller_location(window_id) if window_id else False
        # `None` is "possibly inside": partial or conflicting caller evidence, or
        # a window whose panes could not be listed. Only an exact `False` is
        # "outside" for anything destructive.
        caller_inside = caller_location is not False
        pane_roles = self.frame.role_panes(session_id) if window_id else {}
        if window_id and pane_roles is None:
            warnings.append(f"could not observe panes of window {window_id}; no rebuild")
        cwd_observed = self._observe_pane_cwds(repaired)
        pane_cwd_warnings = cwd_observed["warnings"]
        warnings.extend(pane_cwd_warnings)
        agent_env_warnings, agent_env_observed = self._agent_env_warnings(repaired)
        warnings.extend(agent_env_warnings)
        sleeping = (repaired.get("sleep") or {}).get("state") == "sleeping"
        live_shell_agent = self._has_live_shell_agent(repaired)
        shell_agent = self._shell_agent_pane(repaired)

        # What the rebuild checks could not see this run. Any entry makes the run
        # inconclusive for a pending deferred rebuild.
        unobserved: list[str] = []
        if window_id:
            if pane_roles is None:
                unobserved.append("pane roles")
            if not cwd_observed["observed"]:
                unobserved.append("pane cwds")
            if not agent_env_observed:
                unobserved.append("agent pane environment")
        # Every reason this run's observations would want a whole-window rebuild,
        # independent of which branch below gets to act on it.
        rebuild_wanted: list[str] = []
        if window_id and pane_roles is not None:
            missing_now = self._missing_pane_roles(pane_roles)
            if "agent" in missing_now and not sleeping:
                rebuild_wanted.append("missing panes: " + ", ".join(missing_now))
            if agent_env_warnings and not live_shell_agent and not sleeping:
                rebuild_wanted.append("stale agent environment")
            rebuild_roles = [
                pane["role"]
                for pane in cwd_observed["deleted"]
                if pane["role"] not in self.IN_PLACE_ROLES or not pane["pane_id"]
            ]
            if rebuild_roles:
                rebuild_wanted.append("deleted pane cwd: " + ", ".join(rebuild_roles))

        pending_marker = self.deferred_rebuild_marker(repaired)
        requested_driver = (
            pending_marker.get("requested_driver") if pending_marker else None
        )
        current_driver = (repaired.get("driver") or {}).get("id")
        # The one marker-driven rebuild: `switch-driver` deferred from inside the
        # window, and the record still names the driver that switch requested.
        switch_owed = bool(
            pending_marker
            and pending_marker.get("reason") == "driver-switch"
            and requested_driver
            and requested_driver == current_driver
        )
        if pending_marker and apply and caller_inside:
            warnings.append(
                "a deferred window rebuild is pending; run "
                f"`hive-ide repair --session-id {session_id}` from outside the window"
            )

        log = {
            "op_id": uuid.uuid4().hex[:12],
            "caller_pane": self.frame.caller_pane(),
            "caller_in_window": caller_location,
            "target_window": window_id,
            "pane_roles": pane_roles,
            "pane_cwds": cwd_observed["panes"],
            "actions": actions,
            "warnings": warnings,
            "errors": errors,
            "written": 0,
            "failed_logged": False,
        }
        deferred: list[str] = []
        rebuilt = False
        built = False
        conclusive = False

        if apply and not errors:
            try:
                window_exists = window_id is not None
                if sleeping and not window_exists:
                    actions.append("window: sleeping; not built")
                elif self.frame.ensure(repaired):
                    actions.append("window: built")
                    built = True
                elif not window_exists:
                    # ensure() declined to build and there is no window to inspect:
                    # an absent window has no "missing panes" to rebuild for.
                    actions.append("window: absent; not built")
                elif switch_owed and not caller_inside:
                    rebuilt = self._rebuild(
                        repaired,
                        log,
                        reason=f"deferred driver switch to {requested_driver}",
                        branch="driver-switch",
                        deferred=deferred,
                    )
                elif pane_roles is None:
                    actions.append(
                        "window: panes unobservable; nothing destructive attempted"
                    )
                elif missing := self._missing_pane_roles(pane_roles):
                    if "agent" in missing:
                        if sleeping:
                            warnings.append(
                                "window missing sleeping agent pane; repair preserved sleep state"
                            )
                        else:
                            rebuilt = self._rebuild(
                                repaired,
                                log,
                                reason="missing panes: " + ", ".join(missing),
                                branch="missing-agent-pane",
                                deferred=deferred,
                            )
                    else:
                        restored = self.frame.restore_missing_panes(repaired, missing)
                        if restored:
                            actions.append("window: restored panes: " + ", ".join(restored))
                        still_missing = tuple(
                            role for role in missing if role not in restored
                        )
                        if still_missing:
                            warnings.append(
                                "window still missing panes: " + ", ".join(still_missing)
                            )
                elif agent_env_warnings:
                    if live_shell_agent:
                        actions.append(
                            "agent: stale environment observed; live driver preserved"
                        )
                    elif sleeping:
                        actions.append(
                            "agent: sleeping; stale environment preserved until wake"
                        )
                    else:
                        rebuilt = self._rebuild(
                            repaired,
                            log,
                            reason="stale agent environment",
                            branch="stale-agent-environment",
                            deferred=deferred,
                        )
                elif shell_agent:
                    if sleeping:
                        actions.append("agent: sleeping; exited driver pane preserved")
                    else:
                        self._log(
                            repaired,
                            log,
                            stage="planned",
                            reason="exited driver pane",
                            branch="exited-driver-pane",
                        )
                        if self.frame.respawn_agent(repaired, shell_agent):
                            actions.append("agent: respawned exited driver pane")
                            self._log(
                                repaired,
                                log,
                                stage="completed",
                                reason="exited driver pane",
                                branch="exited-driver-pane",
                            )
                        else:
                            warnings.append(
                                "agent pane may be the caller's own; not respawned"
                            )
                            self._log(
                                repaired,
                                log,
                                stage="skipped",
                                reason="exited driver pane: caller location not settled",
                                branch="exited-driver-pane",
                            )
                elif cwd_observed["deleted"]:
                    rebuilt = self._repair_deleted_cwds(
                        repaired, log, cwd_observed["deleted"], deferred=deferred
                    )
                elif pane_cwd_warnings:
                    actions.append("window: pane cwd differs; live panes preserved")
                if self.frame.sidebar_refresh_target(repaired):
                    self._log(
                        repaired,
                        log,
                        stage="planned",
                        reason="stale sidebar wrapper",
                        branch="sidebar-refresh",
                    )
                    refreshed = self.frame.refresh_sidebar_if_needed(repaired)
                    if refreshed:
                        actions.append("sidebar: refreshed hidden-aware wrapper")
                    self._log(
                        repaired,
                        log,
                        stage="completed" if refreshed else "skipped",
                        reason="stale sidebar wrapper",
                        branch="sidebar-refresh",
                    )
                if self.frame.retitle_panes(repaired):
                    actions.append("window: retitled panes")
                self.frame.apply_columns(repaired)
                self._clear_repair_error(session_id)
                # Conclusive = every observation the rebuild checks depend on
                # succeeded (an absent window has nothing to observe and nothing
                # to rebuild).
                conclusive = not unobserved
            except HiveIdeError as exc:
                errors.append(str(exc))
                if not log["failed_logged"]:
                    self._log(
                        repaired, log, stage="failed", reason=str(exc), branch="exception"
                    )
            self._settle_deferred_marker(
                repaired,
                pending_marker,
                log,
                caller_inside=caller_inside,
                conclusive=conclusive and not errors,
                rebuilt=rebuilt,
                built=built,
                deferred=deferred,
                rebuild_wanted=rebuild_wanted,
                unobserved=unobserved,
                actions=actions,
                warnings=warnings,
                errors=errors,
            )
            if not log["written"]:
                self._log(
                    repaired,
                    log,
                    stage="skipped",
                    reason="no destructive repair needed",
                    branch=None,
                )

        if errors and apply:
            self._record_error(repaired, errors, warnings, actions)

        return {
            "session_id": session_id,
            "name": repaired.get("name"),
            "ok": not errors,
            "applied": apply,
            "actions": actions,
            "warnings": warnings,
            "errors": errors,
            "deferred": deferred,
            "rebuilt": rebuilt,
            "working_dir": repaired.get("working_dir"),
            **(
                {"pane_leased": lease_store.summary(lease)}
                if lease and lease_store.alive(lease) and not force
                else {}
            ),
        }

    def _missing_pane_roles(self, roles: dict[str, str]) -> tuple[str, ...]:
        return tuple(role for role in self.REQUIRED_PANE_ROLES if role not in roles)

    # -- destructive steps -------------------------------------------------

    def _rebuild(
        self,
        record: dict[str, Any],
        log: dict[str, Any],
        *,
        reason: str,
        branch: str,
        deferred: list[str],
    ) -> bool:
        """Rebuild the window, or record a deferral when the caller may live in it."""
        actions: list[str] = log["actions"]
        warnings: list[str] = log["warnings"]
        self._log(record, log, stage="planned", reason=reason, branch=branch)
        try:
            result = self.frame.rebuild(record)
        except HiveIdeError as exc:
            self._log(record, log, stage="failed", reason=str(exc), branch=branch)
            log["failed_logged"] = True
            raise
        if result.get("deferred"):
            why = str(result.get("reason") or "caller-inside-window").replace("-", " ")
            self.mark_deferred_rebuild(
                self.store, record, reason=reason, op_id=log["op_id"]
            )
            deferred.append(reason)
            actions.append(f"window: rebuild deferred ({why}): {reason}")
            warnings.append(
                "the window was not rebuilt because this repair may be running "
                f"inside it; run `hive-ide repair --session-id {record['id']}` from "
                "outside the window (another pane or a plain terminal) to finish "
                "the rebuild"
            )
            self._log(record, log, stage="deferred", reason=reason, branch=branch)
            return False
        actions.append(f"window: rebuilt for {reason}")
        log["rebuilt_branch"] = branch
        log["rebuilt_reason"] = reason
        self._log(record, log, stage="completed", reason=reason, branch=branch)
        return True

    def _repair_deleted_cwds(
        self,
        record: dict[str, Any],
        log: dict[str, Any],
        deleted: list[dict[str, str]],
        *,
        deferred: list[str],
    ) -> bool:
        """Respawn sidebar/plan panes whose cwd vanished; rebuild only for the rest.

        A deleted cwd on a pane the frame can relaunch in place (sidebar, plan)
        touches only that pane. Only the agent pane, or an untagged pane that
        nothing can respawn, still costs the whole window.
        """
        actions: list[str] = log["actions"]
        warnings: list[str] = log["warnings"]
        in_place = [
            pane
            for pane in deleted
            if pane["role"] in self.IN_PLACE_ROLES and pane["pane_id"]
        ]
        rebuild_roles = [
            pane["role"]
            for pane in deleted
            if pane["role"] not in self.IN_PLACE_ROLES or not pane["pane_id"]
        ]
        if rebuild_roles:
            return self._rebuild(
                record,
                log,
                reason="deleted pane cwd: " + ", ".join(rebuild_roles),
                branch="deleted-pane-cwd",
                deferred=deferred,
            )
        reason = "deleted pane cwd: " + ", ".join(pane["role"] for pane in in_place)
        self._log(record, log, stage="planned", reason=reason, branch="deleted-pane-cwd")
        skipped = False
        for pane in in_place:
            if self.frame.respawn_role_pane(record, pane["role"], pane["pane_id"]):
                actions.append(f"{pane['role']} pane: respawned (cwd was deleted)")
            else:
                skipped = True
                warnings.append(
                    f"{pane['role']} pane: cwd was deleted but the pane may be the "
                    "caller's own; not respawned"
                )
        self._log(
            record,
            log,
            stage="skipped" if skipped else "completed",
            reason=reason,
            branch="deleted-pane-cwd",
        )
        return False

    # -- deferred-rebuild marker ------------------------------------------
    #
    # `host.repair.deferred_rebuild` records that a window rebuild was owed but
    # could not be performed because the caller may have been inside the window.
    # Repair and `switch-driver` write it; only an apply-mode repair run from
    # outside the window clears it, and only when that run was conclusive.
    #
    # A marker written by repair never causes a rebuild on its own: the outside
    # run re-evaluates the live checks and rebuilds on their evidence. The ONE
    # exception is `reason == "driver-switch"`: that marker records an explicit
    # user request and names `requested_driver`, the record is authoritative for
    # which driver the session should run, and no live observation can tell a
    # running driver's identity reliably — so while the record still names the
    # requested driver, the outside run rebuilds on the strength of the marker.
    # If the record names another driver again, the switch was superseded and
    # the marker is simply cleared.

    @staticmethod
    def deferred_rebuild_marker(record: dict[str, Any]) -> dict[str, Any] | None:
        host = record.get("host")
        repair_meta = host.get("repair") if isinstance(host, dict) else None
        marker = repair_meta.get("deferred_rebuild") if isinstance(repair_meta, dict) else None
        return marker if isinstance(marker, dict) else None

    @staticmethod
    def mark_deferred_rebuild(
        store: StateStore,
        record: dict[str, Any],
        *,
        reason: str,
        op_id: str,
        requested_driver: str | None = None,
    ) -> dict[str, Any]:
        """Record a deferred rebuild, never downgrading a pending driver switch.

        A `driver-switch` marker is an explicit user request; a later unrelated
        deferral (say, a stale agent environment seen from inside) must not
        replace it, or the switch intent is lost. Such causes are kept alongside
        in `also_pending`, which settling prunes as each check clears. A new
        switch request replaces an older one but inherits its `also_pending`.
        """
        existing = SessionRepair.deferred_rebuild_marker(record) or {}
        existing_reason = existing.get("reason")
        existing_also = [
            item
            for item in existing.get("also_pending") or []
            if isinstance(item, str) and item
        ]
        if existing_reason == "driver-switch" and reason != "driver-switch":
            marker = dict(existing)
            also = list(existing_also)
            if reason not in also:
                also.append(reason)
            marker["also_pending"] = also
        else:
            marker = {
                "reason": reason,
                "op_id": op_id,
                "requested_at": utc_now(),
            }
            if requested_driver:
                marker["requested_driver"] = requested_driver
            also = list(existing_also)
            if (
                existing_reason
                and existing_reason not in {reason, "driver-switch"}
                and existing_reason not in also
            ):
                also.append(existing_reason)
            also = [item for item in also if item != reason]
            if also:
                marker["also_pending"] = also
        host = dict(record.get("host") or {})
        repair_meta = dict(host.get("repair") or {})
        repair_meta["deferred_rebuild"] = marker
        host["repair"] = repair_meta
        record["host"] = host
        store.write("sessions", record["id"], record)
        return repair_meta["deferred_rebuild"]

    @staticmethod
    def clear_deferred_rebuild(store: StateStore, record: dict[str, Any]) -> bool:
        host = dict(record.get("host") or {})
        repair_meta = dict(host.get("repair") or {})
        if "deferred_rebuild" not in repair_meta:
            return False
        repair_meta.pop("deferred_rebuild", None)
        if repair_meta:
            host["repair"] = repair_meta
        else:
            host.pop("repair", None)
        record["host"] = host
        store.write("sessions", record["id"], record)
        return True

    def _settle_deferred_marker(
        self,
        record: dict[str, Any],
        pending: dict[str, Any] | None,
        log: dict[str, Any],
        *,
        caller_inside: bool,
        conclusive: bool,
        rebuilt: bool,
        built: bool,
        deferred: list[str],
        rebuild_wanted: list[str],
        unobserved: list[str],
        actions: list[str],
        warnings: list[str],
        errors: list[str],
    ) -> None:
        """Clear a pending marker only after a conclusive pass from outside.

        `rebuilt` clears it only when the rebuild performed is the one the marker
        was for and no error was recorded in the run. A non-switch marker is
        otherwise cleared only when every observation succeeded and none of them
        wants a rebuild any more; anything short of that keeps it and says why.
        """
        if not pending:
            return
        owed = pending.get("reason") or "unknown reason"
        also = [
            item
            for item in pending.get("also_pending") or []
            if isinstance(item, str) and item
        ]
        if caller_inside:
            return  # the pending warning already says to run from outside
        # A conclusive run (every observation succeeded, no error) settles each
        # extra pending cause on its own evidence: it stays only while the
        # observations still want that rebuild.
        settled = conclusive and not errors
        remaining = [item for item in also if item in rebuild_wanted] if settled else also
        if pending.get("reason") == "driver-switch":
            requested = pending.get("requested_driver")
            current = (record.get("driver") or {}).get("id")
            if not requested or requested != current:
                note = (
                    "window: deferred driver switch superseded "
                    f"(requested {requested or 'unknown'}, record now runs {current})"
                )
                if rebuilt and not errors:
                    # A rebuild from the record this run satisfies every other
                    # pending cause as well.
                    remaining = []
                if remaining:
                    # The switch intent is retired; the other deferred causes are
                    # not, so the marker becomes an ordinary one for them. Clear
                    # first: mark_deferred_rebuild never downgrades a switch.
                    self.clear_deferred_rebuild(self.store, record)
                    self.mark_deferred_rebuild(
                        self.store, record, reason=remaining[0], op_id=log["op_id"]
                    )
                    self._set_also_pending(record, remaining[1:])
                    note += "; still deferred: " + ", ".join(remaining)
                else:
                    self.clear_deferred_rebuild(self.store, record)
                actions.append(note)
                return
            if not errors and (
                built or (rebuilt and log.get("rebuilt_branch") == "driver-switch")
            ):
                # The rebuild from the record satisfies every pending cause too.
                self.clear_deferred_rebuild(self.store, record)
                actions.append(f"window: deferred driver switch to {requested} completed")
                return
            if deferred:
                return  # re-deferred; mark_deferred_rebuild kept the switch intent
            if remaining != also:
                self._set_also_pending(record, remaining)
            why = "; ".join(errors) if errors else "the rebuild was not performed"
            warnings.append(
                f"deferred rebuild marker kept (deferred for: driver-switch to "
                f"{requested}): {why}"
            )
            return
        owed_all = [owed, *also]
        if rebuilt and not errors and log.get("rebuilt_reason") in owed_all:
            self.clear_deferred_rebuild(self.store, record)
            actions.append(f"window: deferred rebuild completed (deferred for: {owed})")
            return
        if deferred:
            return  # re-deferred; mark_deferred_rebuild carried the earlier cause
        if rebuilt:
            warnings.append(
                f"deferred rebuild marker kept (deferred for: {owed}): the window was "
                f"rebuilt for {log.get('rebuilt_reason')} and the deferred check was "
                "not re-evaluated; run repair again"
            )
            return
        if errors:
            why: str | None = "; ".join(errors)
        elif unobserved:
            why = "could not observe: " + ", ".join(unobserved)
        elif rebuild_wanted:
            why = (
                "still warranted ("
                + "; ".join(rebuild_wanted)
                + ") but not performed this run; run repair again"
            )
        else:
            why = None
        if settled and why is None:
            self.clear_deferred_rebuild(self.store, record)
            actions.append(
                "window: deferred rebuild no longer warranted; marker cleared "
                f"(deferred for: {owed})"
            )
            return
        if remaining != also:
            self._set_also_pending(record, remaining)
        warnings.append(
            f"deferred rebuild marker kept (deferred for: {owed}): this run was "
            f"not conclusive — {why or 'observations incomplete'}"
        )

    def _set_also_pending(self, record: dict[str, Any], remaining: list[str]) -> None:
        """Rewrite the marker's `also_pending` list (dropping it when empty)."""
        marker = self.deferred_rebuild_marker(record)
        if marker is None:
            return
        marker = dict(marker)
        if remaining:
            marker["also_pending"] = list(remaining)
        else:
            marker.pop("also_pending", None)
        host = dict(record.get("host") or {})
        repair_meta = dict(host.get("repair") or {})
        repair_meta["deferred_rebuild"] = marker
        host["repair"] = repair_meta
        record["host"] = host
        self.store.write("sessions", record["id"], record)

    # -- repair log -------------------------------------------------------

    def _log(
        self,
        record: dict[str, Any],
        log: dict[str, Any],
        *,
        stage: str,
        reason: str | None,
        branch: str | None,
    ) -> None:
        """Append one entry to the session's bounded repair log.

        Only identity keys from pane environments ever land here (the roles map and
        cwd list carry none); the `pane_roles`/`pane_cwds` snapshots are what was
        observed at the start of this operation, not re-read per stage.
        """
        if stage not in self.LOG_STAGES:
            raise ValueError(f"Unknown repair log stage: {stage}")
        entry = {
            "at": utc_now(),
            "op_id": log["op_id"],
            "stage": stage,
            "reason": reason,
            "branch": branch,
            "caller_pane": log["caller_pane"],
            "caller_in_window": log["caller_in_window"],
            "target_window": log["target_window"],
            "pane_roles": log["pane_roles"],
            "pane_cwds": log["pane_cwds"],
            "actions": list(log["actions"]),
            "warnings": list(log["warnings"]),
            "errors": list(log["errors"]),
        }
        self.append_repair_log(self.store, record["id"], entry)
        log["written"] += 1

    @classmethod
    def append_repair_log(
        cls, store: StateStore, session_id: str, entry: dict[str, Any]
    ) -> dict[str, Any]:
        """Read-modify-write the log under the workspace mutation lock.

        The lock is process-reentrant, so a CLI command that already holds it
        (every command in `WORKSPACE_MUTATIONS`) just rides it; a caller without
        it takes it for the duration of the append.
        """
        with store.mutation_lock():
            current = store.read("repairs", session_id) or {}
            entries = [
                item for item in current.get("entries") or [] if isinstance(item, dict)
            ]
            entries.append(entry)
            document = {
                "schema_version": SCHEMA_VERSION,
                "session_id": session_id,
                "entries": entries[-cls.REPAIR_LOG_LIMIT :],
            }
            store.write("repairs", session_id, document)
        return document

    def _remove_duplicate_conversation_refs(
        self,
        record: dict[str, Any],
        actions: list[str],
        warnings: list[str],
        *,
        apply: bool,
    ) -> None:
        agents_data = record.get("agents")
        resume_ids = (
            agents_data.get("resume_ids") if isinstance(agents_data, dict) else None
        )
        if not isinstance(resume_ids, dict):
            return
        current_driver = record.get("driver") if isinstance(record.get("driver"), dict) else {}
        current_driver_id = current_driver.get("id")
        current_resume = current_driver.get("resume") if isinstance(current_driver, dict) else {}
        current_reference = (
            current_resume.get("reference") if isinstance(current_resume, dict) else None
        )
        changed = False
        for driver_id, reference in list(resume_ids.items()):
            if not isinstance(driver_id, str) or not isinstance(reference, str):
                continue
            owner = self.store.find_conversation_owner(
                driver_id=driver_id,
                reference=reference,
                exclude_session_id=record["id"],
            )
            if owner is None:
                continue
            AgentResumeState(record).forget(driver_id)
            changed = True
            actions.append(
                "driver: removed duplicate "
                f"{driver_id} conversation ref owned by {owner.get('name')}"
            )
            if current_driver_id == driver_id and current_reference == reference:
                warnings.append(
                    f"active {driver_id} conversation ref belonged to "
                    f"{owner.get('name')}; next launch will start without that ref"
                )
                try:
                    driver = self.registry.get(driver_id)
                except HiveIdeError:
                    continue
                record["driver"] = driver.resolve(
                    name=str(record.get("name") or ""),
                    working_dir=str(record.get("working_dir") or self.store.workspace_key),
                    conversation_reference=None,
                )
        if changed and apply:
            self.store.write("sessions", record["id"], record)

    def refresh_driver(
        self, record: dict[str, Any], actions: list[str], *, apply: bool
    ) -> None:
        driver_record = record.get("driver")
        if not isinstance(driver_record, dict):
            return
        driver_id = driver_record.get("id")
        resume = driver_record.get("resume")
        reference = resume.get("reference") if isinstance(resume, dict) else None
        if not isinstance(reference, str) or not reference:
            reference = None
        if not isinstance(driver_id, str) or not driver_id:
            return
        try:
            driver = self.registry.get(driver_id)
        except HiveIdeError:
            return
        refreshed = driver.resolve(
            name=str(record.get("name") or ""),
            working_dir=str(record.get("working_dir") or self.store.workspace_key),
            conversation_reference=reference,
        )
        if refreshed.get("launch_argv") == driver_record.get("launch_argv"):
            return
        record["driver"] = refreshed
        actions.append("driver: refreshed launch command")
        if apply:
            self.store.write("sessions", record["id"], record)

    def repair_all(self, *, apply: bool = True, force: bool = False) -> dict[str, Any]:
        pruned_legacy_plans = self.store.prune_dead_legacy_plan() if apply else []
        results = [
            self.repair(record, apply=apply, force=force)
            for record in self.store.list("sessions")
        ]
        return {
            "ok": all(result["ok"] for result in results),
            "applied": apply,
            "pruned_legacy_plans": pruned_legacy_plans,
            "sessions": results,
        }

    def _shell_agent_pane(self, record: dict[str, Any]) -> str | None:
        pane_id = (self.frame.role_panes(record["id"]) or {}).get("agent")
        if not pane_id:
            return None
        command = self.frame.agent_pane_command(record)
        if not command:
            return None
        if not self.frame.is_shell_agent_pane(record, command):
            return None
        if self._shell_agent_has_live_driver_child(record):
            return None
        return pane_id

    def _has_live_shell_agent(self, record: dict[str, Any]) -> bool:
        command = self.frame.agent_pane_command(record)
        if not command:
            return False
        if not self.frame.is_shell_agent_pane(record, command):
            return False
        return self._shell_agent_has_live_driver_child(record)

    def _shell_agent_has_live_driver_child(self, record: dict[str, Any]) -> bool:
        pane_pid = self.frame.agent_pane_pid(record)
        if pane_pid is None:
            return True
        driver = record.get("driver") or {}
        driver_id = str(driver.get("id") or "")
        launch = [str(part) for part in driver.get("launch_argv") or []]
        markers = {driver_id, *(Path(part).name for part in launch[:1])}
        markers.discard("")
        if not markers:
            return True
        pids = Frame._process_tree(pane_pid)
        inspected = False
        for pid in pids:
            if pid == pane_pid:
                continue
            line = SessionRepair._process_cmdline(pid)
            if line is None:
                continue
            inspected = True
            lowered = line.lower()
            if any(marker.lower() in lowered for marker in markers):
                return True
        if len(pids) > 1 and not inspected:
            return True
        return False

    @staticmethod
    def _process_cmdline(pid: int) -> str | None:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()

    def _drop_legacy_record_plan(
        self, record: dict[str, Any], actions: list[str], *, apply: bool
    ) -> None:
        if not self.store._drop_dead_legacy_plan(record):
            return
        actions.append("host: removed dead legacy_record.plan")
        if apply:
            self.store.write("sessions", record["id"], record)

    def _pane_cwd_warnings(self, record: dict[str, Any]) -> list[str]:
        return self._observe_pane_cwds(record)["warnings"]

    def _observe_pane_cwds(self, record: dict[str, Any]) -> dict[str, Any]:
        """Observe every pane's cwd in the session window.

        Returns `{"observed": bool, "warnings": [str], "deleted": [pane],
        "panes": [pane]}` where a pane is `{"role", "pane_id", "cwd"}`; `deleted`
        lists the panes whose cwd no longer exists so the caller can repair
        exactly those. `observed` is False when the listing itself failed — an
        empty result from a failed listing is not "no panes".
        """
        observed: dict[str, Any] = {
            "observed": True,
            "warnings": [],
            "deleted": [],
            "panes": [],
        }
        target = self.frame.windows().get(record["id"])
        if not target:
            return observed
        expected = str(Path(record["working_dir"]).expanduser().resolve())
        panes = self.frame.tmux(
            [
                "list-panes",
                "-t",
                target,
                "-F",
                "#{@hive_ide_pane}\t#{pane_id}\t#{pane_current_path}",
            ]
        )
        if panes.returncode != 0:
            observed["observed"] = False
            return observed
        for line in panes.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            role, pane_id, raw_path = parts
            if not raw_path.strip():
                continue
            label = role or "unknown"
            shown_path = raw_path.strip()
            pane = {"role": label, "pane_id": pane_id.strip(), "cwd": shown_path}
            observed["panes"].append(pane)
            clean_path = shown_path.removesuffix(" (deleted)")
            current = str(Path(clean_path).expanduser().resolve())
            if shown_path.endswith(" (deleted)") or not Path(clean_path).is_dir():
                observed["deleted"].append(pane)
                remedy = (
                    "repair will respawn that pane in place"
                    if label in self.IN_PLACE_ROLES and pane["pane_id"]
                    else "repair will rebuild the window from the session record"
                )
                observed["warnings"].append(
                    f"{label} pane cwd no longer exists: {shown_path}; {remedy}"
                )
            elif current != expected:
                observed["warnings"].append(
                    f"{label} pane cwd differs from session working_dir: "
                    f"{shown_path} != {expected}; repair preserves the live pane"
                )
        return observed

    def _agent_env_warnings(self, record: dict[str, Any]) -> tuple[list[str], bool]:
        """(warnings, observed). `observed` is False when the agent pane's
        environment could not be read at all, which is unknown, not healthy."""
        pane_id = (self.frame.role_panes(record["id"]) or {}).get("agent")
        if not pane_id:
            return [], True  # no agent pane: nothing to observe here
        env = self.frame.pane_hive_ide_env(pane_id)
        if env is None:
            return [], False
        observed = env.get("HIVE_IDE_SESSION_ID")
        if not observed or observed == record["id"]:
            return [], True
        owner = self.store.find_session(observed)
        owner_name = owner.get("name") if owner else None
        owner_label = f"{owner_name} ({observed})" if owner_name else observed
        return [
            "agent pane environment belongs to another IDE session: "
            f"{owner_label}; expected {record.get('name') or record['id']} "
            f"({record['id']}); repair will rebuild the window"
        ], True

    def _record_error(
        self,
        record: dict[str, Any],
        errors: list[str],
        warnings: list[str],
        actions: list[str],
    ) -> None:
        detail_parts = []
        if actions:
            detail_parts.append("Repair actions:\n- " + "\n- ".join(actions))
        if warnings:
            detail_parts.append("Warnings:\n- " + "\n- ".join(warnings))
        detail_parts.append("Errors:\n- " + "\n- ".join(errors))
        self.store.write(
            "errors",
            record["id"],
            {
                "schema_version": SCHEMA_VERSION,
                "workspace_key": self.store.workspace_key,
                "session_id": record["id"],
                "component": self.COMPONENT,
                "summary": f"Session {record.get('name') or record['id']} needs repair",
                "detail": "\n\n".join(detail_parts)[:8192],
                "retryable": True,
                "recovery": "Run hive-ide repair --session-id <id> or open the session info modal.",
                "observed_at": utc_now(),
            },
        )

    def _clear_repair_error(self, session_id: str) -> None:
        current = self.store.read("errors", session_id)
        if current and current.get("component") in {self.COMPONENT, "frame:open"}:
            self.store.delete("errors", session_id)
