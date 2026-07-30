"""Session self-healing for the package frame."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .errors import HiveIdeError
from .frame import Frame
from .store import StateStore, utc_now


class SessionRepair:
    """Validate and safely repair one IDE session record/window."""

    COMPONENT = "repair"
    REQUIRED_PANE_ROLES = ("sidebar", "agent", "plan")

    def __init__(self, store: StateStore, frame: Frame):
        self.store = store
        self.frame = frame

    def repair(self, record: dict[str, Any], *, apply: bool = True) -> dict[str, Any]:
        session_id = record["id"]
        actions: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []
        repaired = dict(record)

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
                repaired["last_active"] = utc_now()
                self.store.write("sessions", session_id, repaired)

        source = repaired.get("source") or {}
        interpreter = source.get("interpreter")
        if isinstance(interpreter, str) and interpreter and not Path(interpreter).exists():
            errors.append(f"source interpreter missing: {interpreter}")

        plan = (repaired.get("plan") or {}).get("path")
        if isinstance(plan, str) and plan:
            candidates = [Path(plan).expanduser()]
            if not candidates[0].is_absolute():
                candidates.insert(0, Path(str(repaired["working_dir"])) / plan)
            if not any(candidate.is_file() for candidate in candidates):
                warnings.append(f"plan file missing: {plan}")

        if apply and not errors:
            try:
                if self.frame.ensure(repaired):
                    actions.append("window: built")
                elif missing := self._missing_pane_roles(repaired):
                    self.frame.rebuild(repaired)
                    actions.append(
                        "window: rebuilt for missing panes: " + ", ".join(missing)
                    )
                elif self._pane_cwd_mismatch(repaired):
                    actions.append("window: pane cwd differs; live panes preserved")
                    warnings.append(
                        "live pane cwd differs from session working_dir; use "
                        "force-rebuild only if you intentionally want to restart panes"
                    )
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

    def repair_all(self, *, apply: bool = True) -> dict[str, Any]:
        results = [
            self.repair(record, apply=apply) for record in self.store.list("sessions")
        ]
        return {
            "ok": all(result["ok"] for result in results),
            "applied": apply,
            "sessions": results,
        }

    def _pane_cwd_mismatch(self, record: dict[str, Any]) -> bool:
        target = self.frame.windows().get(record["id"])
        if not target:
            return False
        expected = str(Path(record["working_dir"]).expanduser().resolve())
        panes = self.frame.tmux(
            ["list-panes", "-t", target, "-F", "#{pane_current_path}"]
        )
        if panes.returncode != 0:
            return False
        return any(
            str(Path(line).expanduser().resolve()) != expected
            for line in panes.stdout.splitlines()
            if line.strip()
        )

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
