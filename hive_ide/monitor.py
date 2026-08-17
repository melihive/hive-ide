"""Local process monitor for live Hive IDE agent memory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


AGENT_NEEDLES = ("codex", "claude", "agy")


@dataclass(frozen=True)
class ProcessSample:
    pid: int
    ppid: int | None
    rss_kb: int
    command: str
    env: dict[str, str]

    @property
    def kind(self) -> str:
        lowered = self.command.lower()
        if "hive_ide.sidebar" in lowered:
            return "sidebar"
        if any(needle in lowered for needle in AGENT_NEEDLES):
            return "agent"
        command_name = Path(self.command.split(" ", 1)[0]).name
        if any(shell in command_name for shell in ("sh", "bash", "fish", "zsh")):
            return "shell"
        return "helper"

    @property
    def driver(self) -> str | None:
        lowered = self.command.lower()
        for needle in AGENT_NEEDLES:
            if needle in lowered:
                return "antigravity" if needle == "agy" else needle
        return None

    @property
    def session_id(self) -> str | None:
        explicit = self.env.get("HIVE_IDE_SESSION_ID")
        if explicit:
            return explicit
        marker = "--session-id "
        if marker in self.command:
            return self.command.split(marker, 1)[1].split(None, 1)[0]
        marker = "--session-id="
        if marker in self.command:
            return self.command.split(marker, 1)[1].split(None, 1)[0]
        return None


def _read_environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        try:
            name = key.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            continue
        if not name.startswith("HIVE_IDE_"):
            continue
        env[name] = value.decode("utf-8", errors="replace")
    return env


def _read_status(pid: int) -> tuple[int | None, int]:
    ppid: int | None = None
    rss_kb = 0
    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ppid, rss_kb
    for line in lines:
        if line.startswith("PPid:"):
            value = line.split()[1]
            ppid = int(value) if value.isdigit() else None
        elif line.startswith("VmRSS:"):
            value = line.split()[1]
            rss_kb = int(value) if value.isdigit() else 0
    return ppid, rss_kb


def iter_local_processes() -> list[ProcessSample]:
    """Return relevant local processes.

    Linux exposes enough through `/proc` to map agent children back to the IDE
    session via inherited `HIVE_IDE_*` env. Other platforms fail open with an
    empty list; workspace-level tmux inspection can still be added later.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    rows: list[ProcessSample] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw_cmd = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw_cmd.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
        if not command:
            continue
        lowered = command.lower()
        if (
            "hive_ide.sidebar" not in lowered
            and not any(needle in lowered for needle in AGENT_NEEDLES)
            and "hive-ide" not in lowered
        ):
            continue
        env = _read_environ(pid)
        if (
            not env
            and "hive_ide.sidebar" not in lowered
            and not any(needle in lowered for needle in AGENT_NEEDLES)
        ):
            continue
        ppid, rss_kb = _read_status(pid)
        rows.append(
            ProcessSample(
                pid=pid, ppid=ppid, rss_kb=rss_kb, command=command, env=env
            )
        )
    return rows


def _load_session_index(home: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for collection, archived in (("archive", True), ("sessions", False)):
        for path in sorted((home / "workspaces").glob(f"*/{collection}/*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            session_id = record.get("id")
            if not isinstance(session_id, str) or not session_id:
                continue
            driver = record.get("driver") if isinstance(record.get("driver"), dict) else {}
            source = record.get("source") if isinstance(record.get("source"), dict) else {}
            index[session_id] = {
                "session_id": session_id,
                "name": record.get("name") or session_id,
                "workspace_key": record.get("workspace_key"),
                "state": "archived" if archived else "active",
                "driver": driver.get("id"),
                "source": " ".join(
                    str(value)
                    for value in (source.get("kind"), source.get("version"))
                    if value
                )
                or None,
            }
    return index


def _brief_command(command: str) -> str:
    parts = command.split()
    if not parts:
        return ""
    if " --name " in command:
        before, _, after = command.partition(" --name ")
        return f"{Path(before.split()[0]).name} --name {after.split(' --', 1)[0][:40]}"
    return " ".join(
        Path(part).name if index == 0 else part
        for index, part in enumerate(parts[:6])
    )


def build_monitor(
    *,
    samples: Iterable[ProcessSample],
    sessions: dict[str, dict[str, Any]],
    workspace_key: str | None = None,
) -> dict[str, Any]:
    by_session: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    totals = {"processes": 0, "rss_kb": 0}
    by_kind: dict[str, dict[str, int]] = {}

    for sample in samples:
        session_id = sample.session_id
        session = sessions.get(session_id or "")
        env_workspace = sample.env.get("HIVE_IDE_WORKSPACE_KEY")
        if (
            workspace_key
            and (session or {}).get("workspace_key") != workspace_key
            and env_workspace != workspace_key
        ):
            continue

        process = {
            "pid": sample.pid,
            "ppid": sample.ppid,
            "kind": sample.kind,
            "driver": sample.driver,
            "rss_kb": sample.rss_kb,
            "rss_mb": round(sample.rss_kb / 1024, 1),
            "command": _brief_command(sample.command),
        }
        totals["processes"] += 1
        totals["rss_kb"] += sample.rss_kb
        kind_total = by_kind.setdefault(sample.kind, {"processes": 0, "rss_kb": 0})
        kind_total["processes"] += 1
        kind_total["rss_kb"] += sample.rss_kb

        if not session_id:
            if sample.kind == "agent":
                unmatched.append(process)
            continue
        row = by_session.setdefault(
            session_id,
            {
                **(
                    session
                    or {
                        "session_id": session_id,
                        "name": sample.env.get("HIVE_IDE_SESSION") or session_id,
                    }
                ),
                "processes": 0,
                "rss_kb": 0,
                "rss_mb": 0.0,
                "by_kind": {},
                "process_list": [],
            },
        )
        row["processes"] += 1
        row["rss_kb"] += sample.rss_kb
        row["rss_mb"] = round(row["rss_kb"] / 1024, 1)
        session_kind = row["by_kind"].setdefault(
            sample.kind, {"processes": 0, "rss_kb": 0}
        )
        session_kind["processes"] += 1
        session_kind["rss_kb"] += sample.rss_kb
        row["process_list"].append(process)

    for kind in by_kind.values():
        kind["rss_mb"] = round(kind["rss_kb"] / 1024, 1)
    return {
        "scope": "workspace" if workspace_key else "all",
        "workspace_key": workspace_key,
        "totals": {
            **totals,
            "rss_mb": round(totals["rss_kb"] / 1024, 1),
        },
        "by_kind": dict(sorted(by_kind.items())),
        "sessions": sorted(
            by_session.values(), key=lambda row: row["rss_kb"], reverse=True
        ),
        "unmatched_agents": sorted(
            unmatched, key=lambda row: row["rss_kb"], reverse=True
        ),
        "unsupported": (
            None
            if Path("/proc").is_dir()
            else "process memory mapping requires /proc"
        ),
    }


def monitor_state(
    *,
    home: str | Path,
    workspace_key: str | None = None,
    all_workspaces: bool = True,
) -> dict[str, Any]:
    resolved_home = Path(home).expanduser().resolve()
    sessions = _load_session_index(resolved_home)
    selected_workspace = None if all_workspaces else workspace_key
    return build_monitor(
        samples=iter_local_processes(),
        sessions=sessions,
        workspace_key=selected_workspace,
    )
