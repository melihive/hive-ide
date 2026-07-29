"""Plain argparse command surface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, SCHEMA_VERSION, __version__
from .adoption import AdoptableConversation, ConversationAdopter
from .config import configured_registry, load_config, normalized_snapshot
from .errors import HiveIdeError, UsageError
from .environments import EnvironmentManager
from .frame import Frame
from .hooks import HookInstaller
from .paths import config_path, state_home, workspace_key
from .relayout import IdeRelayout
from .source import inspect_interpreter, resolve_source
from .store import StateStore, utc_now


WORKSPACE_MUTATIONS = frozenset(
    {
        "archive",
        "adopt",
        "attach-conversation",
        "clear-error",
        "create",
        "ensure",
        "open",
        "plan-set",
        "purge",
        "rebuild",
        "record-error",
        "relayout",
        "rename",
        "resume",
        "snapshot",
        "source-set",
        "status-event",
        "switch-driver",
        "working-dir-set",
    }
)

QUIET_SUCCESS_COMMANDS = frozenset(
    {
        "current-chat",
        "current-plan",
    }
)


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _context(args: argparse.Namespace) -> tuple[StateStore, dict[str, Any]]:
    key = workspace_key(
        args.workspace_key or os.environ.get("HIVE_IDE_WORKSPACE_KEY")
    )
    store = StateStore(state_home(args.state_home), key)
    config = load_config(config_path(args.config))
    return store, config


def _refresh_workspace_sources(store: StateStore) -> None:
    store.refresh_stable_sources(collections=("sessions", "archive"))


def cmd_create(args: argparse.Namespace) -> dict[str, Any]:
    store, config = _context(args)
    if args.adopt:
        adopted = _adopt_conversations(
            store,
            config,
            driver_id=args.driver or "claude",
            working_dir=args.working_dir,
            plan=args.plan,
            source=args.source,
            name=args.name,
            reference=args.reference,
            limit=1,
            dry_run=False,
        )
        if not adopted["created"]:
            raise UsageError(
                f"No new {args.driver or 'claude'} sessions were found to adopt "
                "for this workspace."
            )
        return adopted["created"][0]
    return _create_session(
        store,
        config,
        name=args.name,
        driver_id=args.driver,
        working_dir=args.working_dir,
        plan=args.plan,
        source=args.source,
    )


def _default_session_name(working_dir: str) -> str:
    path = Path(working_dir).expanduser().resolve()
    return path.name or "workspace"


def _default_driver_id(config: dict[str, Any]) -> str:
    configured = config.get("default_driver")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return "term"


def _create_session(
    store: StateStore,
    config: dict[str, Any],
    *,
    name: str | None,
    driver_id: str | None,
    working_dir: str | None,
    plan: str | None,
    source: str | None,
) -> dict[str, Any]:
    registry = configured_registry(config)
    selected_driver = driver_id or _default_driver_id(config)
    driver = registry.get(selected_driver)
    availability = driver.detect()
    if not availability.available and selected_driver != "term":
        raise UsageError(
            f"Driver {selected_driver!r} is unavailable: {availability.detail}."
        )
    working_dir = workspace_key(working_dir or store.workspace_key)
    session_name = " ".join((name or _default_session_name(working_dir)).split())
    resolved = driver.resolve(
        name=session_name, working_dir=working_dir, conversation_reference=None
    )
    return store.create_session(
        name=session_name,
        working_dir=working_dir,
        source=resolve_source(
            source or config.get("default_source") or "stable",
            config,
            default_interpreter=sys.executable,
        ),
        driver=resolved,
        plan={"path": plan, "active_task": None},
    )


def _unique_session_name(store: StateStore, label: str) -> str:
    clean = " ".join(label.split()) or "SESSION"
    if store.find_by_name(clean) is None:
        return clean
    suffix = 2
    while store.find_by_name(f"{clean} {suffix}") is not None:
        suffix += 1
    return f"{clean} {suffix}"


def _create_adopted_session(
    store: StateStore,
    config: dict[str, Any],
    conversation: AdoptableConversation,
    *,
    plan: str | None,
    source: str | None,
    name: str | None,
) -> dict[str, Any]:
    registry = configured_registry(config)
    driver = registry.get(conversation.driver_id)
    session_name = " ".join((name or conversation.label).split())
    resolved = driver.resolve(
        name=session_name,
        working_dir=conversation.working_dir,
        conversation_reference=conversation.reference,
    )
    record = store.create_session(
        name=_unique_session_name(store, session_name),
        working_dir=conversation.working_dir,
        source=resolve_source(
            source or config.get("default_source") or "stable",
            config,
            default_interpreter=sys.executable,
        ),
        driver=resolved,
        plan={"path": plan, "active_task": None},
        host={
            "adopted": {
                "driver": conversation.driver_id,
                "source": conversation.source_path,
                "updated_at": conversation.updated_at,
            }
        },
    )
    return record


def _adopt_conversations(
    store: StateStore,
    config: dict[str, Any],
    *,
    driver_id: str,
    working_dir: str | None,
    plan: str | None,
    source: str | None,
    name: str | None = None,
    reference: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    selected_working_dir = workspace_key(working_dir or store.workspace_key)
    if not dry_run and reference is None and limit is None:
        raise UsageError(
            "Adopting sessions requires --reference=<conversation-id> or "
            "--limit=<count>. Use --dry-run to list adoptable conversations first."
        )
    adopter = ConversationAdopter(store, config)
    existing = adopter.existing_references(driver_id=driver_id)
    available = [
        conversation
        for conversation in adopter.available(
            driver_id=driver_id, working_dir=selected_working_dir
        )
        if conversation.reference not in existing
    ]
    if reference is not None:
        available = [
            conversation
            for conversation in available
            if conversation.reference == reference
        ]
    if limit is not None:
        available = available[:limit]
    created = []
    for conversation in available:
        if dry_run:
            created.append(
                {
                    "driver": conversation.driver_id,
                    "reference": conversation.reference,
                    "name": " ".join((name or conversation.label).split()),
                    "label": conversation.label,
                    "working_dir": conversation.working_dir,
                    "updated_at": conversation.updated_at,
                    "title": conversation.title,
                    "preview": conversation.preview,
                }
            )
        else:
            created.append(
                _create_adopted_session(
                    store,
                    config,
                    conversation,
                    plan=plan,
                    source=source,
                    name=name,
                )
            )
    return {
        "driver": driver_id,
        "working_dir": str(Path(selected_working_dir).expanduser().resolve()),
        "created": created,
        "skipped_existing": len(existing),
        "dry_run": dry_run,
    }


def cmd_adopt(args: argparse.Namespace) -> dict[str, Any]:
    store, config = _context(args)
    return _adopt_conversations(
        store,
        config,
        driver_id=args.driver,
        working_dir=args.working_dir,
        plan=args.plan,
        source=args.source,
        name=None,
        reference=args.reference,
        limit=args.limit,
        dry_run=args.dry_run,
    )


def cmd_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    store, _ = _context(args)
    _refresh_workspace_sources(store)
    return store.list("archive" if args.archived else "sessions")


def cmd_show(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    _refresh_workspace_sources(store)
    record = store.find_session(args.session_id, archived=args.archived)
    if record is None:
        raise UsageError(f"No session with id {args.session_id}.")
    return record


def cmd_archive(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    record = store.archive_session(args.session_id)
    Frame(store, socket=_socket(store, args.tmux_socket)).close(args.session_id)
    return record


def cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    _refresh_workspace_sources(store)
    record = store.resume_session(args.session_id)
    frame = Frame(store, socket=_socket(store, args.tmux_socket))
    frame.ensure(record)
    frame.bind_keys()
    frame.select_session(record["id"])
    return record


def cmd_rename(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    if existing := store.find_by_name(" ".join(args.name.split())):
        if existing["id"] != args.session_id:
            raise UsageError(f"Session {args.name!r} already exists in this workspace.")
    record = store.find_session(args.session_id)
    if record is None:
        raise UsageError(f"No session with id {args.session_id}.")
    record["name"] = " ".join(args.name.split())
    record["last_active"] = utc_now()
    store.write("sessions", args.session_id, record)
    Frame(store, socket=_socket(store, args.tmux_socket)).rename(
        args.session_id, record["name"]
    )
    return record


def cmd_current(args: argparse.Namespace) -> dict[str, Any]:
    session_id = args.session_id or os.environ.get("HIVE_IDE_SESSION_ID")
    if not session_id:
        raise UsageError("No current session id is available.")
    store, _ = _context(args)
    _refresh_workspace_sources(store)
    record = store.find_session(session_id)
    if record is None:
        raise UsageError(f"No session with id {session_id}.")
    return record


def _current_record(args: argparse.Namespace) -> tuple[StateStore, dict[str, Any]]:
    session_id = args.session_id or os.environ.get("HIVE_IDE_SESSION_ID")
    if not session_id:
        raise UsageError("No current session id is available.")
    store, _ = _context(args)
    _refresh_workspace_sources(store)
    return store, _session(store, session_id, None)


def cmd_current_plan(args: argparse.Namespace) -> dict[str, Any]:
    store, record = _current_record(args)
    return Frame(store, socket=_socket(store, args.tmux_socket)).current_plan(
        record, focus=args.focus
    )


def cmd_current_chat(args: argparse.Namespace) -> dict[str, Any]:
    store, record = _current_record(args)
    _, config = _context(args)
    driver_record = record.get("driver") or {}
    driver_id = driver_record.get("id")
    if not driver_id:
        raise UsageError("The session has no driver to resume.")
    reference = (driver_record.get("resume") or {}).get("reference")
    driver = configured_registry(config).get(driver_id)
    availability = driver.detect()
    if not availability.available and driver_id != "term":
        raise UsageError(f"Driver {driver_id!r} is unavailable: {availability.detail}.")
    if reference:
        record["driver"] = driver.resolve(
            name=record["name"],
            working_dir=record["working_dir"],
            conversation_reference=reference,
        )
        with store.mutation_lock():
            store.write("sessions", record["id"], record)
    return Frame(store, socket=_socket(store, args.tmux_socket)).current_chat(record)


def cmd_plan_set(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    record = _session(store, args.session_id, None)
    plan = dict(record.get("plan") or {})
    if args.clear:
        plan = {"path": None, "active_task": None}
    else:
        if args.path is not None:
            candidate = Path(args.path).expanduser()
            if not candidate.is_absolute():
                candidate = Path(record["working_dir"]) / candidate
            if not candidate.is_file():
                raise UsageError(f"Plan file does not exist: {candidate}")
            plan["path"] = args.path
        if args.active_task is not None:
            plan["active_task"] = args.active_task
    record["plan"] = {
        "path": plan.get("path"),
        "active_task": plan.get("active_task"),
    }
    record["last_active"] = utc_now()
    store.write("sessions", record["id"], record)
    return record


def cmd_attach_conversation(args: argparse.Namespace) -> dict[str, Any]:
    store, config = _context(args)
    record = _session(store, args.session_id, None)
    driver_id = args.driver or (record.get("driver") or {}).get("id")
    if not driver_id:
        raise UsageError("The session has no driver to attach.")
    driver = configured_registry(config).get(driver_id)
    exists = driver.conversation_exists(args.reference, record["working_dir"])
    if exists is False:
        raise UsageError(
            f"Driver {driver_id!r} cannot find conversation {args.reference!r}."
        )
    record["driver"] = driver.resolve(
        name=record["name"],
        working_dir=record["working_dir"],
        conversation_reference=args.reference,
    )
    record["last_active"] = utc_now()
    store.write("sessions", record["id"], record)
    return record


def cmd_working_dir_set(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    record = _session(store, args.session_id, None)
    directory = Path(args.working_dir).expanduser().resolve()
    if not directory.is_dir():
        raise UsageError(f"Working directory does not exist: {directory}")
    record["working_dir"] = str(directory)
    record["last_active"] = utc_now()
    store.write("sessions", record["id"], record)
    frame = Frame(store, socket=_socket(store, args.tmux_socket))
    frame.rebuild(record)
    frame.bind_keys()
    return record


def cmd_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    record = _session(store, args.session_id, None)
    frame = Frame(store, socket=_socket(store, args.tmux_socket))
    frame.rebuild(record)
    frame.bind_keys()
    return {"session_id": record["id"], "rebuilt": True}


def cmd_relayout(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    frame = Frame(store, socket=_socket(store, args.tmux_socket))
    if not frame.exists():
        raise UsageError("The workspace frame is not running.")
    code = IdeRelayout.main(
        [
            "hive_ide.relayout",
            "--state-home",
            str(store.home),
            "--workspace-key",
            store.workspace_key,
            "--tmux-socket",
            frame.socket,
            "--mode",
            args.mode,
        ]
    )
    return {"relayout": code == 0, "mode": args.mode}


def cmd_purge(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm:
        raise UsageError("Permanent removal requires --confirm.")
    store, _ = _context(args)
    Frame(store, socket=_socket(store, args.tmux_socket)).close(args.session_id)
    return {
        "session_id": args.session_id,
        "purged": store.purge_session(args.session_id),
    }


def cmd_status_event(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    if store.find_session(args.session_id) is None:
        raise UsageError(f"No session with id {args.session_id}.")
    document = {
        "schema_version": SCHEMA_VERSION,
        "session_id": args.session_id,
        "workspace_key": store.workspace_key,
        "state": args.state,
        "driver": args.driver,
        "conversation_reference": args.conversation_reference,
        "observed_at": utc_now(),
    }
    if args.subagents_running is not None:
        document["subagents"] = {"running": max(0, args.subagents_running)}
        document["subagents_running"] = max(0, args.subagents_running)
    store.write("status", args.session_id, document)
    return document


def cmd_error(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    document = {
        "schema_version": SCHEMA_VERSION,
        "workspace_key": store.workspace_key,
        "session_id": args.session_id,
        "component": args.component,
        "summary": args.summary,
        "detail": (args.detail or "")[:8192],
        "retryable": args.retryable,
        "recovery": args.recovery,
        "observed_at": utc_now(),
    }
    if args.session_id:
        store.write("errors", args.session_id, document)
    else:
        store.write_path(store.frame_error_path(), document)
    return document


def cmd_clear_error(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    removed = (
        store.delete("errors", args.session_id)
        if args.session_id
        else _unlink(store.frame_error_path())
    )
    return {"cleared": removed}


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def cmd_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    store, config = _context(args)
    registry = configured_registry(config)
    snapshot = normalized_snapshot(
        state_home=store.home,
        workspace_key=store.workspace_key,
        workspace_hash=store.workspace_hash,
        socket=args.tmux_socket,
        registry=registry,
        config=config,
    )
    store.write_path(store.config_snapshot_path(), snapshot)
    return snapshot


def _session(store: StateStore, session_id: str | None, name: str | None) -> dict[str, Any]:
    record = store.find_session(session_id) if session_id else store.find_by_name(name or "")
    if record is None:
        label = session_id or name
        raise UsageError(f"No session matching {label!r}.")
    return record


def _socket(store: StateStore, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    snapshot = store.read_path(store.config_snapshot_path())
    tmux = snapshot.get("tmux") if snapshot else None
    return tmux.get("socket") if isinstance(tmux, dict) else None


def cmd_ensure(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
    record = _session(store, args.session_id, args.name)
    frame = Frame(store, socket=_socket(store, args.tmux_socket))
    built = frame.ensure(record)
    frame.bind_keys()
    return {"session_id": record["id"], "built": built}


def cmd_switch_driver(args: argparse.Namespace) -> dict[str, Any]:
    store, config = _context(args)
    record = _session(store, args.session_id, None)
    registry = configured_registry(config)
    driver = registry.get(args.driver)
    availability = driver.detect()
    if not availability.available and args.driver != "term":
        raise UsageError(f"Driver {args.driver!r} is unavailable: {availability.detail}.")
    reference = ((record.get("driver") or {}).get("resume") or {}).get("reference")
    record["driver"] = driver.resolve(
        name=record["name"],
        working_dir=record["working_dir"],
        conversation_reference=reference if (record.get("driver") or {}).get("id") == args.driver else None,
    )
    record["last_active"] = utc_now()
    store.write("sessions", record["id"], record)
    Frame(store, socket=_socket(store, args.tmux_socket)).rebuild(record)
    return record


def cmd_source_set(args: argparse.Namespace) -> dict[str, Any]:
    store, config = _context(args)
    record = _session(store, args.session_id, None)
    record["source"] = resolve_source(
        args.source, config, default_interpreter=sys.executable
    )
    store.write("sessions", record["id"], record)
    if not args.no_rebuild and Path(record["working_dir"]).is_dir():
        Frame(store, socket=_socket(store, args.tmux_socket)).rebuild(record)
    return record


def cmd_skill_install(args: argparse.Namespace) -> dict[str, Any]:
    if args.target:
        target = Path(args.target).expanduser().resolve()
    else:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        target = codex_home / "skills" / "hive-ide" / "SKILL.md"
    content = files("hive_ide").joinpath("skill_definition/SKILL.md").read_text(
        encoding="utf-8"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"installed": str(target)}


def cmd_environment_setup(args: argparse.Namespace) -> dict[str, Any]:
    return EnvironmentManager(args.environment_home).setup(
        stable_spec=args.stable_spec,
        dev_checkout=args.dev_checkout,
    )


def _hook_installer(args: argparse.Namespace) -> HookInstaller:
    return HookInstaller(
        home=args.home,
        stable_python=args.stable_python,
        selected_state_home=args.state_home,
    )


def cmd_hook_setup(args: argparse.Namespace) -> dict[str, Any]:
    return _hook_installer(args).setup(apply=args.apply)


def cmd_open(args: argparse.Namespace) -> dict[str, Any]:
    store, config = _context(args)
    _refresh_workspace_sources(store)
    registry = configured_registry(config)
    if not store.list("sessions"):
        _create_session(
            store,
            config,
            name=args.name,
            driver_id=args.driver,
            working_dir=args.working_dir,
            plan=args.plan,
            source=args.source,
        )
    snapshot = normalized_snapshot(
        state_home=store.home,
        workspace_key=store.workspace_key,
        workspace_hash=store.workspace_hash,
        socket=args.tmux_socket or f"hive-ide-{store.workspace_hash[:8]}",
        registry=registry,
        config=config,
    )
    store.write_path(store.config_snapshot_path(), snapshot)
    return Frame(store, socket=args.tmux_socket).open(no_attach=args.no_attach)


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    store, config = _context(args)
    _refresh_workspace_sources(store)
    registry = configured_registry(config)
    sessions = store.list("sessions")
    source_errors = []
    for session in sessions:
        try:
            handshake = inspect_interpreter(session["source"]["interpreter"])
            if handshake["package_version"] != session["source"]["version"]:
                source_errors.append(
                    f"{session['name']}: expected {session['source']['version']}, "
                    f"found {handshake['package_version']}"
                )
        except HiveIdeError as exc:
            source_errors.append(f"{session['name']}: {exc}")
    frame = Frame(store)
    config_findings = frame.verify_user_config()
    hook_findings = HookInstaller(
        stable_python=args.stable_python,
        selected_state_home=args.state_home,
    ).verify()
    return {
        "ok": (
            shutil.which("tmux") is not None
            and not source_errors
            and not hook_findings
        ),
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "package_version": __version__,
        "tmux": shutil.which("tmux"),
        "state_home": str(store.home),
        "workspace_key": store.workspace_key,
        "sessions": len(sessions),
        "source_errors": source_errors,
        "config_findings": config_findings,
        "hook_findings": hook_findings,
        "drivers": {
            driver_id: registry.get(driver_id).detect().__dict__
            for driver_id in registry.ids()
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hive-ide",
        description=(
            "Repository-scoped tmux IDE for Codex, Claude, Antigravity, and "
            "terminal sessions."
        ),
        epilog=(
            "Examples:\n"
            "  hive-ide open\n"
            "  hive-ide create --driver=claude --name='API work'\n"
            "  hive-ide adopt --driver=codex\n"
            "  hive-ide current-chat\n"
            "  hive-ide current-plan --focus\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--state-home",
        help="Override the XDG state directory that stores IDE session records.",
    )
    parser.add_argument(
        "--config",
        help="Read package settings from this JSON config file.",
    )
    parser.add_argument(
        "--workspace-key",
        help="Workspace identity. Defaults to the current directory.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the final JSON result for TUI side-effect commands.",
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
        title="commands",
    )

    command_help = {
        "create": "Create a session, optionally adopting a known conversation.",
        "adopt": "List or import existing Claude/Codex conversations for this directory.",
        "list": "List active sessions for the workspace.",
        "show": "Show one session record.",
        "archive": "Archive an active session and close its window.",
        "resume": "Restore an archived session and rebuild its window.",
        "rename": "Rename a session without changing its immutable ID.",
        "current": "Show the current session selected by ID or environment.",
        "current-plan": "Open the current session's plan pane.",
        "current-chat": "Focus or resume the current session's agent pane.",
        "plan-set": "Attach, change, or clear a session plan.",
        "attach-conversation": "Attach a driver conversation ID to an existing session.",
        "working-dir-set": "Move a session to an existing working directory.",
        "rebuild": "Rebuild one tmux window from its session record.",
        "relayout": "Reapply or adopt the tmux frame geometry.",
        "purge": "Permanently remove a session and all package state for it.",
        "status-event": "Record agent activity for hooks.",
        "record-error": "Record a recoverable frame/sidebar error.",
        "clear-error": "Clear a recorded session error.",
        "snapshot": "Inspect live tmux frame state.",
        "ensure": "Build one missing session window if needed.",
        "switch-driver": "Switch a session between configured drivers.",
        "source-set": "Pin one session to stable, dev, or an explicit source.",
        "open": "Open the tmux IDE, bootstrapping a TERM session if needed.",
        "skill-install": "Install the packaged agent skill definition.",
        "environment-setup": "Install stable/dev Python environments.",
        "hook-setup": "Install shell/agent hooks used by the IDE.",
        "verify": "Verify package, drivers, hooks, source, and frame health.",
        "version": "Print package, protocol, schema, and interpreter versions.",
    }

    def command(name: str, **kwargs):
        summary = command_help[name]
        return sub.add_parser(
            name,
            help=summary,
            description=summary,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            **kwargs,
        )

    create = command("create")
    create.add_argument("--name")
    create.add_argument("--driver")
    create.add_argument("--working-dir")
    create.add_argument("--plan")
    create.add_argument("--source")
    create.add_argument("--adopt", action="store_true")
    create.add_argument("--reference")
    create.set_defaults(handler=cmd_create)

    adopt = command("adopt")
    adopt.add_argument("--driver", default="claude")
    adopt.add_argument("--working-dir")
    adopt.add_argument("--plan")
    adopt.add_argument("--source")
    adopt.add_argument("--reference")
    adopt.add_argument("--limit", type=int)
    adopt.add_argument("--dry-run", action="store_true")
    adopt.set_defaults(handler=cmd_adopt)

    listing = command("list", aliases=["ls"])
    listing.add_argument("--archived", action="store_true")
    listing.set_defaults(handler=cmd_list)

    show = command("show")
    show.add_argument("--session-id", required=True)
    show.add_argument("--archived", action="store_true")
    show.set_defaults(handler=cmd_show)

    archive = command("archive")
    archive.add_argument("--session-id", required=True)
    archive.add_argument("--tmux-socket")
    archive.set_defaults(handler=cmd_archive)

    resume = command("resume")
    resume.add_argument("--session-id", required=True)
    resume.add_argument("--tmux-socket")
    resume.set_defaults(handler=cmd_resume)

    rename = command("rename")
    rename.add_argument("--session-id", required=True)
    rename.add_argument("--name", required=True)
    rename.add_argument("--tmux-socket")
    rename.set_defaults(handler=cmd_rename)

    current = command("current")
    current.add_argument("--session-id")
    current.set_defaults(handler=cmd_current)

    current_plan = command("current-plan", aliases=["plan"])
    current_plan.add_argument("--session-id")
    current_plan.add_argument("--tmux-socket")
    current_plan.add_argument("--focus", action="store_true")
    current_plan.set_defaults(handler=cmd_current_plan)

    current_chat = command("current-chat", aliases=["chat"])
    current_chat.add_argument("--session-id")
    current_chat.add_argument("--tmux-socket")
    current_chat.set_defaults(handler=cmd_current_chat)

    plan = command("plan-set")
    plan.add_argument("--session-id", required=True)
    plan.add_argument("--path")
    plan.add_argument("--active-task")
    plan.add_argument("--clear", action="store_true")
    plan.set_defaults(handler=cmd_plan_set)

    attach = command("attach-conversation")
    attach.add_argument("--session-id", required=True)
    attach.add_argument("--reference", required=True)
    attach.add_argument("--driver")
    attach.set_defaults(handler=cmd_attach_conversation)

    directory = command("working-dir-set")
    directory.add_argument("--session-id", required=True)
    directory.add_argument("--working-dir", required=True)
    directory.add_argument("--tmux-socket")
    directory.set_defaults(handler=cmd_working_dir_set)

    rebuild = command("rebuild")
    rebuild.add_argument("--session-id", required=True)
    rebuild.add_argument("--tmux-socket")
    rebuild.set_defaults(handler=cmd_rebuild)

    relayout = command("relayout")
    relayout.add_argument("--tmux-socket")
    relayout.add_argument("--mode", choices=("snap", "adopt"), default="snap")
    relayout.set_defaults(handler=cmd_relayout)

    purge = command("purge")
    purge.add_argument("--session-id", required=True)
    purge.add_argument("--confirm", action="store_true")
    purge.add_argument("--tmux-socket")
    purge.set_defaults(handler=cmd_purge)

    status = command("status-event")
    status.add_argument("--session-id", required=True)
    status.add_argument("--state", choices=("working", "waiting", "idle"), required=True)
    status.add_argument("--driver", required=True)
    status.add_argument("--conversation-reference")
    status.add_argument("--subagents-running", type=int)
    status.set_defaults(handler=cmd_status_event)

    error = command("record-error")
    error.add_argument("--session-id")
    error.add_argument("--component", required=True)
    error.add_argument("--summary", required=True)
    error.add_argument("--detail")
    error.add_argument("--retryable", action="store_true")
    error.add_argument("--recovery", required=True)
    error.set_defaults(handler=cmd_error)

    clear = command("clear-error")
    clear.add_argument("--session-id")
    clear.set_defaults(handler=cmd_clear_error)

    snapshot = command("snapshot")
    snapshot.add_argument("--tmux-socket", default="hive-ide")
    snapshot.set_defaults(handler=cmd_snapshot)

    ensure = command("ensure")
    target = ensure.add_mutually_exclusive_group(required=True)
    target.add_argument("--session-id")
    target.add_argument("--name")
    ensure.add_argument("--tmux-socket")
    ensure.set_defaults(handler=cmd_ensure)

    switch = command("switch-driver")
    switch.add_argument("--session-id", required=True)
    switch.add_argument("--driver", required=True)
    switch.add_argument("--tmux-socket")
    switch.set_defaults(handler=cmd_switch_driver)

    source = command("source-set")
    source.add_argument("--session-id", required=True)
    source.add_argument("--source", required=True)
    source.add_argument("--tmux-socket")
    source.add_argument("--no-rebuild", action="store_true")
    source.set_defaults(handler=cmd_source_set)

    open_command = command("open")
    open_command.add_argument("--no-attach", action="store_true")
    open_command.add_argument("--tmux-socket")
    open_command.add_argument("--name")
    open_command.add_argument("--driver")
    open_command.add_argument("--working-dir")
    open_command.add_argument("--plan")
    open_command.add_argument("--source")
    open_command.set_defaults(handler=cmd_open)

    skill = command("skill-install")
    skill.add_argument("--target")
    skill.set_defaults(handler=cmd_skill_install)

    environments = command("environment-setup")
    environments.add_argument("--environment-home")
    environments.add_argument("--stable-spec", default="hive-ide")
    environments.add_argument("--dev-checkout")
    environments.set_defaults(handler=cmd_environment_setup)

    hooks = command("hook-setup")
    hooks.add_argument("--apply", action="store_true")
    hooks.add_argument("--home")
    hooks.add_argument("--stable-python")
    hooks.set_defaults(handler=cmd_hook_setup)

    verify = command("verify")
    verify.add_argument("--stable-python")
    verify.set_defaults(handler=cmd_verify)

    version = command("version")
    version.set_defaults(
        handler=lambda _args: {
            "package_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "interpreter": sys.executable,
        }
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        raw_argv = list(argv) if argv is not None else sys.argv[1:]
        if "--quiet" in raw_argv[1:]:
            raw_argv = ["--quiet", *[item for item in raw_argv if item != "--quiet"]]
        args = parser.parse_args(raw_argv)
        if args.command in WORKSPACE_MUTATIONS:
            store, _ = _context(args)
            with store.mutation_lock():
                result = args.handler(args)
        else:
            result = args.handler(args)
        if not args.quiet and args.command not in QUIET_SUCCESS_COMMANDS:
            print(_json(result))
        return 0
    except HiveIdeError as exc:
        print(f"hive-ide: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
