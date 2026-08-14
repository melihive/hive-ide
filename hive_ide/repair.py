"""Session self-healing for the package frame."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .agents import AgentResumeState
from .drivers import DriverRegistry
from .errors import HiveIdeError
from .frame import Frame
from .health import SessionHealth
from .source import inspect_interpreter
from .store import StateStore, utc_now


class SessionRepair:
    """Validate and safely repair one IDE session record/window."""

    COMPONENT = "repair"
    REQUIRED_PANE_ROLES = ("sidebar", "agent", "plan")

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

    def repair(self, record: dict[str, Any], *, apply: bool = True) -> dict[str, Any]:
        session_id = record["id"]
        actions: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []
        repaired = dict(record)

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
        pane_cwd_warnings = self._pane_cwd_warnings(repaired)
        warnings.extend(pane_cwd_warnings)
        agent_env_warnings = self._agent_env_warnings(repaired)
        warnings.extend(agent_env_warnings)
        shell_agent = self._shell_agent_pane(repaired)

        if apply and not errors:
            try:
                if self.frame.ensure(repaired):
                    actions.append("window: built")
                elif missing := self._missing_pane_roles(repaired):
                    if "agent" in missing:
                        self.frame.rebuild(repaired)
                        actions.append(
                            "window: rebuilt for missing panes: " + ", ".join(missing)
                        )
                    else:
                        restored = self.frame.restore_missing_panes(repaired, missing)
                        if restored:
                            actions.append(
                                "window: restored panes: " + ", ".join(restored)
                            )
                        still_missing = tuple(
                            role for role in missing if role not in restored
                        )
                        if still_missing:
                            warnings.append(
                                "window still missing panes: "
                                + ", ".join(still_missing)
                            )
                elif agent_env_warnings:
                    self.frame.rebuild(repaired)
                    actions.append("window: rebuilt for stale agent environment")
                elif shell_agent:
                    self.frame.respawn_agent(repaired, shell_agent)
                    actions.append("agent: respawned exited driver pane")
                elif pane_cwd_warnings:
                    if self._has_deleted_pane_cwd(pane_cwd_warnings):
                        self.frame.rebuild(repaired)
                        actions.append("window: rebuilt for deleted pane cwd")
                    else:
                        actions.append("window: pane cwd differs; live panes preserved")
                self._clear_repair_error(session_id)
            except HiveIdeError as exc:
                errors.append(str(exc))

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
            "working_dir": repaired.get("working_dir"),
        }

    def _missing_pane_roles(self, record: dict[str, Any]) -> tuple[str, ...]:
        if record["id"] not in self.frame.windows():
            return ()
        roles = self.frame.role_panes(record["id"])
        return tuple(role for role in self.REQUIRED_PANE_ROLES if role not in roles)

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

    def repair_all(self, *, apply: bool = True) -> dict[str, Any]:
        pruned_legacy_plans = (
            self.store.prune_dead_legacy_plan() if apply else []
        )
        results = [
            self.repair(record, apply=apply) for record in self.store.list("sessions")
        ]
        return {
            "ok": all(result["ok"] for result in results),
            "applied": apply,
            "pruned_legacy_plans": pruned_legacy_plans,
            "sessions": results,
        }

    def _has_deleted_pane_cwd(self, warnings: list[str]) -> bool:
        return any("pane cwd no longer exists:" in warning for warning in warnings)

    def _shell_agent_pane(self, record: dict[str, Any]) -> str | None:
        pane_id = self.frame.role_panes(record["id"]).get("agent")
        if not pane_id:
            return None
        command = self.frame.agent_pane_command(record)
        if not command:
            return None
        return pane_id if self.frame.is_shell_agent_pane(record, command) else None

    def _drop_legacy_record_plan(
        self, record: dict[str, Any], actions: list[str], *, apply: bool
    ) -> None:
        if not self.store._drop_dead_legacy_plan(record):
            return
        actions.append("host: removed dead legacy_record.plan")
        if apply:
            self.store.write("sessions", record["id"], record)

    def _pane_cwd_warnings(self, record: dict[str, Any]) -> list[str]:
        target = self.frame.windows().get(record["id"])
        if not target:
            return []
        expected = str(Path(record["working_dir"]).expanduser().resolve())
        panes = self.frame.tmux(
            [
                "list-panes",
                "-t",
                target,
                "-F",
                "#{@hive_ide_pane}\t#{pane_current_path}",
            ]
        )
        if panes.returncode != 0:
            return []
        warnings: list[str] = []
        for line in panes.stdout.splitlines():
            role, _, raw_path = line.partition("\t")
            if not raw_path.strip():
                continue
            label = role or "unknown"
            shown_path = raw_path.strip()
            clean_path = shown_path.removesuffix(" (deleted)")
            current = str(Path(clean_path).expanduser().resolve())
            if shown_path.endswith(" (deleted)") or not Path(clean_path).is_dir():
                warnings.append(
                    f"{label} pane cwd no longer exists: {shown_path}; "
                    "repair will rebuild the window from the session record"
                )
            elif current != expected:
                warnings.append(
                    f"{label} pane cwd differs from session working_dir: "
                    f"{shown_path} != {expected}; repair preserves the live pane"
                )
        return warnings

    def _agent_env_warnings(self, record: dict[str, Any]) -> list[str]:
        pane_id = self.frame.role_panes(record["id"]).get("agent")
        if not pane_id:
            return []
        env = self.frame.pane_hive_ide_env(pane_id)
        observed = env.get("HIVE_IDE_SESSION_ID")
        if not observed or observed == record["id"]:
            return []
        owner = self.store.find_session(observed)
        owner_name = owner.get("name") if owner else None
        owner_label = f"{owner_name} ({observed})" if owner_name else observed
        return [
            "agent pane environment belongs to another IDE session: "
            f"{owner_label}; expected {record.get('name') or record['id']} "
            f"({record['id']}); repair will rebuild the window"
        ]

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
