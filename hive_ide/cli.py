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


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _context(args: argparse.Namespace) -> tuple[StateStore, dict[str, Any]]:
    key = workspace_key(
        args.workspace_key or os.environ.get("HIVE_IDE_WORKSPACE_KEY")
    )
    store = StateStore(state_home(args.state_home), key)
    config = load_config(config_path(args.config))
    return store, config


def cmd_create(args: argparse.Namespace) -> dict[str, Any]:
    store, config = _context(args)
    registry = configured_registry(config)
    driver = registry.get(args.driver)
    availability = driver.detect()
    if not availability.available and args.driver != "term":
        raise UsageError(f"Driver {args.driver!r} is unavailable: {availability.detail}.")
    working_dir = workspace_key(args.working_dir or store.workspace_key)
    resolved = driver.resolve(
        name=args.name, working_dir=working_dir, conversation_reference=None
    )
    return store.create_session(
        name=args.name,
        working_dir=working_dir,
        source=resolve_source(
            args.source or config.get("default_source") or "stable",
            config,
            default_interpreter=sys.executable,
        ),
        driver=resolved,
        plan={"path": args.plan, "active_task": None},
    )


def cmd_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    store, _ = _context(args)
    return store.list("archive" if args.archived else "sessions")


def cmd_show(args: argparse.Namespace) -> dict[str, Any]:
    store, _ = _context(args)
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
    record = store.resume_session(args.session_id)
    frame = Frame(store, socket=_socket(store, args.tmux_socket))
    frame.ensure(record)
    frame.bind_keys()
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
    record = store.find_session(session_id)
    if record is None:
        raise UsageError(f"No session with id {session_id}.")
    return record


def _current_record(args: argparse.Namespace) -> tuple[StateStore, dict[str, Any]]:
    session_id = args.session_id or os.environ.get("HIVE_IDE_SESSION_ID")
    if not session_id:
        raise UsageError("No current session id is available.")
    store, _ = _context(args)
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
    if Path(record["working_dir"]).is_dir():
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
    registry = configured_registry(config)
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
    parser = argparse.ArgumentParser(prog="hive-ide")
    parser.add_argument("--state-home")
    parser.add_argument("--config")
    parser.add_argument("--workspace-key")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--driver", default="claude")
    create.add_argument("--working-dir")
    create.add_argument("--plan")
    create.add_argument("--source")
    create.set_defaults(handler=cmd_create)

    listing = sub.add_parser("list", aliases=["ls"])
    listing.add_argument("--archived", action="store_true")
    listing.set_defaults(handler=cmd_list)

    show = sub.add_parser("show")
    show.add_argument("--session-id", required=True)
    show.add_argument("--archived", action="store_true")
    show.set_defaults(handler=cmd_show)

    archive = sub.add_parser("archive")
    archive.add_argument("--session-id", required=True)
    archive.add_argument("--tmux-socket")
    archive.set_defaults(handler=cmd_archive)

    resume = sub.add_parser("resume")
    resume.add_argument("--session-id", required=True)
    resume.add_argument("--tmux-socket")
    resume.set_defaults(handler=cmd_resume)

    rename = sub.add_parser("rename")
    rename.add_argument("--session-id", required=True)
    rename.add_argument("--name", required=True)
    rename.add_argument("--tmux-socket")
    rename.set_defaults(handler=cmd_rename)

    current = sub.add_parser("current")
    current.add_argument("--session-id")
    current.set_defaults(handler=cmd_current)

    current_plan = sub.add_parser("current-plan", aliases=["plan"])
    current_plan.add_argument("--session-id")
    current_plan.add_argument("--tmux-socket")
    current_plan.add_argument("--focus", action="store_true")
    current_plan.set_defaults(handler=cmd_current_plan)

    current_chat = sub.add_parser("current-chat", aliases=["chat"])
    current_chat.add_argument("--session-id")
    current_chat.add_argument("--tmux-socket")
    current_chat.set_defaults(handler=cmd_current_chat)

    plan = sub.add_parser("plan-set")
    plan.add_argument("--session-id", required=True)
    plan.add_argument("--path")
    plan.add_argument("--active-task")
    plan.add_argument("--clear", action="store_true")
    plan.set_defaults(handler=cmd_plan_set)

    attach = sub.add_parser("attach-conversation")
    attach.add_argument("--session-id", required=True)
    attach.add_argument("--reference", required=True)
    attach.add_argument("--driver")
    attach.set_defaults(handler=cmd_attach_conversation)

    directory = sub.add_parser("working-dir-set")
    directory.add_argument("--session-id", required=True)
    directory.add_argument("--working-dir", required=True)
    directory.add_argument("--tmux-socket")
    directory.set_defaults(handler=cmd_working_dir_set)

    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("--session-id", required=True)
    rebuild.add_argument("--tmux-socket")
    rebuild.set_defaults(handler=cmd_rebuild)

    relayout = sub.add_parser("relayout")
    relayout.add_argument("--tmux-socket")
    relayout.add_argument("--mode", choices=("snap", "adopt"), default="snap")
    relayout.set_defaults(handler=cmd_relayout)

    purge = sub.add_parser("purge")
    purge.add_argument("--session-id", required=True)
    purge.add_argument("--confirm", action="store_true")
    purge.add_argument("--tmux-socket")
    purge.set_defaults(handler=cmd_purge)

    status = sub.add_parser("status-event")
    status.add_argument("--session-id", required=True)
    status.add_argument("--state", choices=("working", "waiting", "idle"), required=True)
    status.add_argument("--driver", required=True)
    status.add_argument("--conversation-reference")
    status.set_defaults(handler=cmd_status_event)

    error = sub.add_parser("record-error")
    error.add_argument("--session-id")
    error.add_argument("--component", required=True)
    error.add_argument("--summary", required=True)
    error.add_argument("--detail")
    error.add_argument("--retryable", action="store_true")
    error.add_argument("--recovery", required=True)
    error.set_defaults(handler=cmd_error)

    clear = sub.add_parser("clear-error")
    clear.add_argument("--session-id")
    clear.set_defaults(handler=cmd_clear_error)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--tmux-socket", default="hive-ide")
    snapshot.set_defaults(handler=cmd_snapshot)

    ensure = sub.add_parser("ensure")
    target = ensure.add_mutually_exclusive_group(required=True)
    target.add_argument("--session-id")
    target.add_argument("--name")
    ensure.add_argument("--tmux-socket")
    ensure.set_defaults(handler=cmd_ensure)

    switch = sub.add_parser("switch-driver")
    switch.add_argument("--session-id", required=True)
    switch.add_argument("--driver", required=True)
    switch.add_argument("--tmux-socket")
    switch.set_defaults(handler=cmd_switch_driver)

    source = sub.add_parser("source-set")
    source.add_argument("--session-id", required=True)
    source.add_argument("--source", required=True)
    source.add_argument("--tmux-socket")
    source.set_defaults(handler=cmd_source_set)

    open_command = sub.add_parser("open")
    open_command.add_argument("--no-attach", action="store_true")
    open_command.add_argument("--tmux-socket")
    open_command.set_defaults(handler=cmd_open)

    skill = sub.add_parser("skill-install")
    skill.add_argument("--target")
    skill.set_defaults(handler=cmd_skill_install)

    environments = sub.add_parser("environment-setup")
    environments.add_argument("--environment-home")
    environments.add_argument("--stable-spec", default="hive-ide")
    environments.add_argument("--dev-checkout")
    environments.set_defaults(handler=cmd_environment_setup)

    hooks = sub.add_parser("hook-setup")
    hooks.add_argument("--apply", action="store_true")
    hooks.add_argument("--home")
    hooks.add_argument("--stable-python")
    hooks.set_defaults(handler=cmd_hook_setup)

    verify = sub.add_parser("verify")
    verify.add_argument("--stable-python")
    verify.set_defaults(handler=cmd_verify)

    version = sub.add_parser("version")
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
        args = parser.parse_args(argv)
        if args.command in WORKSPACE_MUTATIONS:
            store, _ = _context(args)
            with store.mutation_lock():
                result = args.handler(args)
        else:
            result = args.handler(args)
        print(_json(result))
        return 0
    except HiveIdeError as exc:
        print(f"hive-ide: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
