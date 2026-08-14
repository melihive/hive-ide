"""`ide_sidebar` self-heal — the reload-watch that lets a long-running sidebar exit when
its source changes so the keep-alive wrapper restarts it on fresh code (kills the Phase-35
stale-code ghost). The interactive render loop is verified by hand in the tmux frame."""
from __future__ import annotations

import sys
import subprocess
import pytest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hive_ide.sidebar import IdeSidebar, SidebarCommandRunner, SidebarCursorState  # noqa: E402
from hive_ide.sidebar_grid import SidebarGrid  # noqa: E402
from hive_ide.sidebar_plugins import SidebarProviderRegistry, SubagentsProvider  # noqa: E402
from hive_ide.state_compat import StateIO  # noqa: E402
from hive_ide.store import StateStore  # noqa: E402
from hive_ide.config import _sidebar_config  # noqa: E402
from hive_ide.errors import UsageError  # noqa: E402
from hive_ide.git_status import inspect_linked_checkout  # noqa: E402


def test_version_handshake_allows_stable_package_upgrades(monkeypatch):
    monkeypatch.setenv("HIVE_IDE_PROTOCOL_VERSION", "1")
    monkeypatch.setenv("HIVE_IDE_VERSION", "different")
    monkeypatch.setenv("HIVE_IDE_SOURCE", "stable")
    assert IdeSidebar._version_error() is None
    monkeypatch.setenv("HIVE_IDE_SOURCE", "dev")
    assert IdeSidebar._version_error() is None


def test_version_handshake_rejects_protocol_skew(monkeypatch):
    monkeypatch.setenv("HIVE_IDE_PROTOCOL_VERSION", "99")
    assert "incompatible" in IdeSidebar._version_error()


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        check=True,
        text=True,
    )


def test_linked_checkout_status_distinguishes_clean_modified_and_main(tmp_path):
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "worktree", "add", "-b", "feature/status", str(linked))

    assert inspect_linked_checkout(repo) is None
    assert inspect_linked_checkout(linked).state == "shipped"
    (linked / "tracked.txt").write_text("modified\n", encoding="utf-8")
    assert inspect_linked_checkout(linked).state == "live"
    _git(linked, "add", "tracked.txt")
    _git(linked, "commit", "-m", "feature")
    assert inspect_linked_checkout(linked).state == "live"


def test_sidebar_linked_checkout_marker_is_cached_and_reports_unknown(monkeypatch, tmp_path):
    directory = tmp_path / "linked"
    directory.mkdir()
    calls = []

    def probe(path):
        calls.append(path)
        return SimpleNamespace(state="unknown")

    monkeypatch.setattr("hive_ide.sidebar_plugins.inspect_linked_checkout", probe)
    provider = SidebarProviderRegistry().get("checkout")
    session = {"working_dir": str(directory)}
    assert provider.value(tmp_path, session) == "unknown"
    assert provider.value(tmp_path, session) == "unknown"
    assert calls == [str(directory)]


def test_missing_linked_checkout_has_visible_marker(tmp_path):
    provider = SidebarProviderRegistry().get("checkout")
    assert provider.value(
        tmp_path, {"working_dir": str(tmp_path / "gone")}
    ) == "missing"


def test_merged_checkout_marker_is_suppressed_while_subagents_run(tmp_path):
    provider = SidebarProviderRegistry().get("checkout")
    session = {
        "id": "session-id",
        "workspace_key": str(tmp_path / "workspace"),
        "worktree_merged": True,
        "subagents": {"running": 2},
    }

    registry = SidebarProviderRegistry()
    sidebar = _sidebar_config({}, registry)

    assert provider.value(tmp_path, session) == "busy"
    assert sidebar["icons"]["providers"]["checkout"]["busy"] == "⏳"
    session["subagents"]["running"] = 0
    assert provider.value(tmp_path, session) == "missing"


def test_ordinary_main_checkout_ignores_historical_merged_marker(tmp_path):
    provider = SidebarProviderRegistry().get("checkout")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")

    assert (
        provider.value(
            tmp_path,
            {
                "working_dir": str(repo),
                "host": {"hive": {"worktree_merged": True}},
            },
        )
        is None
    )


def test_merged_checkout_uses_explicit_subagent_count(tmp_path):
    provider = SidebarProviderRegistry().get("checkout")
    session = {
        "id": "session-id",
        "workspace_key": str(tmp_path / "workspace"),
        "worktree_merged": True,
        "subagents": {"running": 2},
    }

    assert provider.value(tmp_path, session) == "busy"


def test_sidebar_still_reads_live_legacy_record_keys(tmp_path):
    registry = SidebarProviderRegistry()
    session = {
        "id": "session-id",
        "workspace_key": str(tmp_path / "workspace"),
        "plan": {"path": "plans/current.md", "active_task": None},
        "host": {
            "hive": {
                "legacy_record": {
                    "plan": "plans/stale-dead-key.md",
                    "plan_status": "merged",
                    "subagents": {"running": 2},
                    "worktree_merged": True,
                }
            }
        },
    }

    assert registry.get("plan").value(tmp_path, session) == "done"
    assert registry.get("subagents").value(tmp_path, session) == "count:2"
    assert registry.get("checkout").value(tmp_path, session) == "busy"


def test_subagent_count_renders_under_the_status_dot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="BUSY",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
        plan={"path": "plans/x.md", "active_task": None},
    )
    store.write(
        "status",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "state": "working",
            "driver": "term",
            "subagents": {"running": 3},
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )
    registry = SidebarProviderRegistry()
    sidebar = _sidebar_config({}, registry)

    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        "none",
        0,
        24,
        focused=False,
        sidebar=sidebar,
        providers=registry,
    )

    assert _plain(lines[0]).rstrip().endswith("▶")
    assert _plain(lines[1]).rstrip().endswith("3")


def test_tmux_bell_alert_renders_in_sidebar_right_status(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="ALERT",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
        plan={"path": "plans/x.md", "active_task": None},
    )
    registry = SidebarProviderRegistry()
    sidebar = _sidebar_config({}, registry)

    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        "none",
        0,
        24,
        focused=False,
        sidebar=sidebar,
        providers=registry,
        tmux_alerts={session["id"]: "🔔"},
    )

    assert _plain(lines[1]).rstrip().endswith("🔔")


def test_subagent_count_takes_priority_over_tmux_bell_alert(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="BUSY ALERT",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
    )
    store.write(
        "status",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "state": "working",
            "driver": "term",
            "subagents": {"running": 2},
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )
    registry = SidebarProviderRegistry()
    sidebar = _sidebar_config({}, registry)

    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        "none",
        0,
        24,
        focused=False,
        sidebar=sidebar,
        providers=registry,
        tmux_alerts={session["id"]: "🔔"},
    )

    assert _plain(lines[1]).rstrip().endswith("2")
    assert "🔔" not in _plain(lines[1])


def test_current_waiting_session_still_renders_status_dot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="WAITING",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
    )
    store.write(
        "status",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "state": "waiting",
            "driver": "term",
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )
    registry = SidebarProviderRegistry()
    sidebar = _sidebar_config({}, registry)

    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        session["id"],
        0,
        24,
        focused=False,
        sidebar=sidebar,
        providers=registry,
    )

    assert "●" in _plain(lines[0])


def test_subagent_provider_ignores_visible_agent_pane_text(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="BUSY",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "claude"},
    )
    session["transcript_excerpt"] = (
        "○ codex  L1 queue cleanup\n"
        "Session abc is currently running as a background agent (bg).\n"
        "* Waiting for 1 background agent to finish\n"
    )

    assert SubagentsProvider().value(store.home, session) is None


def test_sidebar_cursor_follows_session_id_across_reorder():
    cursor, selected = IdeSidebar._reconcile_cursor(
        ["a", "b", "c"],
        1,
        focused=True,
        free=False,
        archive_mode=False,
        current_session_id="a",
        cursor_session_id="b",
    )
    assert (cursor, selected) == (1, "b")

    cursor, selected = IdeSidebar._reconcile_cursor(
        ["c", "a", "b"],
        cursor,
        focused=True,
        free=False,
        archive_mode=False,
        current_session_id="a",
        cursor_session_id=selected,
    )
    assert (cursor, selected) == (2, "b")


def test_sidebar_unfocused_cursor_tracks_local_session():
    assert IdeSidebar._reconcile_cursor(
        ["c", "a", "b"],
        2,
        focused=False,
        free=False,
        archive_mode=False,
        current_session_id="a",
        cursor_session_id="b",
    ) == (1, "a")


def test_sidebar_cursor_state_follows_session_id_across_reorder():
    state = SidebarCursorState(cursor=1, session_id="b", focused=True)
    state = state.reconcile(
        ["a", "b", "c"],
        free=False,
        archive_mode=False,
        current_session_id="a",
    )
    assert state == SidebarCursorState(cursor=1, session_id="b", focused=True)

    state = state.reconcile(
        ["c", "a", "b"],
        free=False,
        archive_mode=False,
        current_session_id="a",
    )
    assert state == SidebarCursorState(cursor=2, session_id="b", focused=True)


def test_sidebar_cursor_activation_drops_sidebar_focus_but_keeps_session_id():
    state = SidebarCursorState(cursor=2, session_id="old", focused=True)

    assert state.activate_chat(["a", "b", "clicked"]) == SidebarCursorState(
        cursor=2,
        session_id="clicked",
        focused=False,
    )
    assert IdeSidebar._after_activation(["a", "b", "clicked"], 2) == (
        False,
        "clicked",
    )


def test_sidebar_has_no_global_active_session_lookup():
    assert not hasattr(IdeSidebar, "_active_session_id")
    assert not hasattr(SidebarCommandRunner(Path("."), "repo"), "active_session_id")


def test_sidebar_current_session_id_is_process_local(monkeypatch):
    def fake_run(argv, **_kwargs):
        raise AssertionError(f"unexpected tmux active-session lookup: {argv}")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert IdeSidebar._current_session_id("local-session") == "local-session"


def test_sidebar_current_row_is_local_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_home = tmp_path / "state"
    first = {"id": "first", "name": "FIRST", "driver": {"id": "term"}}
    second = {"id": "second", "name": "SECOND", "driver": {"id": "term"}}

    lines = IdeSidebar.render_lines(
        state_home,
        [first, second],
        str(workspace),
        "second",
        0,
        42,
        focused=False,
    )

    first_line = next(line for line in lines if "FIRST" in line)
    second_line = next(line for line in lines if "SECOND" in line)
    assert IdeSidebar.CUR_BG not in first_line
    assert IdeSidebar.CUR_BG in second_line


def test_sidebar_unfocused_cursor_uses_supplied_current_session():
    assert IdeSidebar._reconcile_cursor(
        ["c", "local", "b"],
        2,
        focused=False,
        free=False,
        archive_mode=False,
        current_session_id="local",
        cursor_session_id="b",
    ) == (1, "local")


def test_sidebar_pane_active_reads_tmux_pane_state(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%12")
    seen = {}

    def fake_run(argv, **_kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "0\n", "")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert IdeSidebar._pane_active() is False
    assert seen["argv"] == [
        "tmux",
        "display-message",
        "-p",
        "-t",
        "%12",
        "#{pane_active}",
    ]


def test_subagent_count_renders_for_current_row_without_status_dot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="CURRENT",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
        plan={"path": "plans/x.md", "active_task": None},
    )
    store.write(
        "status",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "state": "idle",
            "driver": "term",
            "subagents": {"running": 2},
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )

    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        session["id"],
        0,
        24,
        focused=False,
        sidebar=_sidebar_config({}, SidebarProviderRegistry()),
        providers=SidebarProviderRegistry(),
    )

    assert _plain(lines[1]).rstrip().endswith("2")


def test_subagent_count_renders_for_selected_current_row(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="CURRENT",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
        plan={"path": "plans/x.md", "active_task": None},
    )
    store.write(
        "status",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "state": "idle",
            "driver": "term",
            "subagents": {"running": 2},
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )

    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        session["id"],
        0,
        24,
        focused=True,
        sidebar=_sidebar_config({}, SidebarProviderRegistry()),
        providers=SidebarProviderRegistry(),
    )

    assert _plain(lines[1]).rstrip().endswith("2")


def test_subagent_count_renders_for_selected_current_dense_row(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="CURRENT",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
        plan={"path": "plans/x.md", "active_task": None},
    )
    store.write(
        "status",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "state": "idle",
            "driver": "term",
            "subagents": {"running": 2},
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )

    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        session["id"],
        0,
        24,
        focused=True,
        entry_rows=1,
        sidebar=_sidebar_config({}, SidebarProviderRegistry()),
        providers=SidebarProviderRegistry(),
    )

    assert _plain(lines[0]).rstrip().endswith("2")


def test_selected_current_row_keeps_status_glyph_visible(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="CURRENT",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
    )
    store.write(
        "status",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "state": "working",
            "driver": "term",
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )

    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        session["id"],
        0,
        24,
        focused=True,
        sidebar=_sidebar_config({}, SidebarProviderRegistry()),
        providers=SidebarProviderRegistry(),
    )

    assert "▶" in _plain(lines[0])


def test_subagent_count_renders_when_metadata_row_collapses(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="BUSY",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
        plan={"path": "plans/x.md", "active_task": None},
    )
    store.write(
        "status",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "state": "idle",
            "driver": "term",
            "subagents": {"running": 1},
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )

    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        "none",
        0,
        6,
        focused=False,
        entry_rows=1,
        sidebar=_sidebar_config({}, SidebarProviderRegistry()),
        providers=SidebarProviderRegistry(),
    )

    assert _plain(lines[0]).rstrip().endswith("1")


def test_subagent_provider_reads_explicit_status_count(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="BUSY",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "codex"},
        plan={"path": None, "active_task": None},
    )
    store.write(
        "status",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "state": "working",
            "driver": "codex",
            "subagents": {"running": 2},
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )

    assert SubagentsProvider().value(store.home, session) == "count:2"


def test_compacting_activity_has_a_distinct_configurable_state_icon(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    session = store.create_session(
        name="COMPACT",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver={"id": "term"},
    )
    store.write(
        "activity",
        session["id"],
        {
            "schema_version": 1,
            "session_id": session["id"],
            "workspace_key": store.workspace_key,
            "kind": "compacting",
            "state": "running",
            "observed_at": "2099-01-01T00:00:00+00:00",
        },
    )
    registry = SidebarProviderRegistry()
    provider = registry.get("activity")
    assert provider.value(store.home, session) == "compacting"
    assert provider.default_icons["compacting"] == "🧠"

    sidebar = _sidebar_config({}, registry)
    lines = IdeSidebar.render_lines(
        store.home,
        [session],
        str(workspace),
        "none",
        0,
        20,
        focused=False,
        entry_rows=1,
        sidebar=sidebar,
        providers=registry,
    )
    assert _plain(lines[0]).startswith("🧠 COMPACT")
    assert "💻" not in _plain(lines[0])


def _plain(line: str) -> str:
    return IdeSidebar._strip_ansi(line)


def test_sidebar_body_hides_workspace_folder_name(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hive_ide.sidebar_plugins.inspect_linked_checkout",
        lambda _path: SimpleNamespace(state="live"),
    )
    monkeypatch.setattr(IdeSidebar, "_rel_time", staticmethod(lambda _ts: "14h"))
    lines = IdeSidebar.render_lines(
        tmp_path,
        [
            {
                "id": "session-id",
                "name": "HIVE IDE PYPI",
                "driver": {"id": "codex"},
                "plan": {"path": "plans/x.md"},
                "working_dir": str(tmp_path),
                "last_active": "ignored",
            }
        ],
        "/workspace/project/worktree/example-feature",
        "session-id",
        0,
        20,
        focused=False,
    )
    assert "example-feature" not in "\n".join(_plain(line) for line in lines[:2])
    assert "/home/" not in _plain(lines[0])
    assert _plain(lines[1]) == "   📝 🔀 14h"


def test_sidebar_footer_groups_archive_and_create(tmp_path):
    lines = IdeSidebar.render_lines(
        tmp_path,
        [],
        "/workspace/project",
        "none",
        0,
        20,
        focused=False,
    )

    assert "filter" in _plain(lines[-3])
    footer = _plain(lines[-2])
    assert "show archive" in footer
    assert footer.rstrip().endswith("+")
    assert footer.index("+") == 18
    assert _plain(lines[-1]).strip() == ""
    assert IdeSidebar._footer_create_col(20) == 18
    assert _plain(lines[0]).strip() == "(no ide sessions)"


def test_sidebar_grid_reflows_metadata_by_terminal_cell_width():
    full = SidebarGrid(width=20, entry_rows=3)
    assert full.metadata_row(
        state="", slots=["📝", "🔀"], age="14h"
    ) == "   📝 🔀 14h"
    assert SidebarGrid.cell_width(
        full.metadata_row(state="", slots=["📝", "🔀"], age="14h")
    ) <= 20

    compact = SidebarGrid(width=9, entry_rows=2)
    compact_row = compact.metadata_row(
        state="", slots=["📝", "🔀"], age="14h"
    )
    assert "🔀" in compact_row
    assert "14h" in compact_row
    assert "📝" not in compact_row
    assert SidebarGrid.cell_width(compact_row) <= 9

    narrow = SidebarGrid(width=6, entry_rows=1)
    narrow_row = narrow.metadata_row(
        state="", slots=["📝", "🔀"], age="14h"
    )
    assert "14h" in narrow_row
    assert SidebarGrid.cell_width(narrow_row) <= 6


def test_sidebar_grid_reserves_right_status_before_age_and_slots():
    grid = SidebarGrid(width=12, entry_rows=2)
    row = grid.metadata_row(
        state="", slots=["📝", "✅"], age="13m", right_status="12"
    )

    assert row.endswith("12")
    assert SidebarGrid.cell_width(row) == 12


def test_sidebar_grid_keeps_right_status_separate_with_styled_age():
    grid = SidebarGrid(width=10, entry_rows=2)
    row = grid.metadata_row(
        state="",
        slots=["📝", "❎"],
        age="28s",
        age_style="\x1b[38;5;244m",
        reset="\x1b[0m",
        right_status="1",
    )

    plain = _plain(row)
    assert plain.rstrip().endswith("28s   1")
    assert SidebarGrid.cell_width(row) == 10


def test_sidebar_grid_supports_three_slots_and_mixed_icon_widths():
    grid = SidebarGrid(
        width=20,
        entry_rows=2,
        leading_cells=2,
        status_cells=2,
        slot_cells=(1, 2, 1),
    )
    row = grid.metadata_row(
        state="!", slots=["P", "🔀", "C"], age="14h"
    )
    assert row == "!  P 🔀 C 14h"
    assert SidebarGrid.cell_width(row) <= 20
    assert grid.name_width == 14


def test_sidebar_provider_configuration_is_ordered_and_swappable():
    registry = SidebarProviderRegistry()
    sidebar = _sidebar_config(
        {
            "sidebar": {
                "state": None,
                "slots": ["plan", "checkout", "ci"],
                "providers": {
                    "ci": {
                        "region": "slot",
                        "source": "session",
                        "path": ["ci"],
                        "icons": {"passing": "✓", "failing": "❌"},
                    }
                },
                "icons": {
                    "drivers": {"codex": "C"},
                    "providers": {"ci": {"passing": "P"}},
                },
            }
        },
        registry,
    )
    assert sidebar["state"] is None
    assert sidebar["slots"] == ["plan", "checkout", "ci"]
    assert sidebar["icons"]["drivers"]["codex"] == "C"
    assert sidebar["icons"]["providers"]["ci"]["passing"] == "P"
    assert sidebar["icons"]["providers"]["ci"]["failing"] == "❌"
    pane_registry = SidebarProviderRegistry.from_snapshot(sidebar["providers"])
    lines = IdeSidebar.render_lines(
        Path("/no-state"),
        [{"id": "one", "name": "ONE", "driver": {"id": "codex"}, "ci": "passing"}],
        "/workspace/example",
        "none",
        0,
        20,
        focused=False,
        sidebar=sidebar,
        providers=pane_registry,
    )
    assert "P" in _plain(lines[1])


def test_pane_registry_rehydrates_snapshot_without_plugin_discovery(monkeypatch):
    monkeypatch.setattr(
        "hive_ide.sidebar_plugins.entry_points",
        lambda: (_ for _ in ()).throw(AssertionError("pane loaded entry points")),
    )
    sidebar = _sidebar_config({}, SidebarProviderRegistry())
    registry = SidebarProviderRegistry.from_snapshot(sidebar["providers"])
    assert registry.ids() == ["activity", "checkout", "plan", "subagents"]


def test_default_sidebar_icons_are_terminal_cell_safe():
    sidebar = _sidebar_config({}, SidebarProviderRegistry())

    assert sidebar["icons"]["status"]["working"] == "▶"
    assert sidebar["icons"]["controls"]["archive"] == "▼"
    assert sidebar["icons"]["providers"]["checkout"]["busy"] == "⏳"
    assert "subagents" not in sidebar["icons"]["providers"]
    for value in (
        sidebar["icons"]["status"]["working"],
        sidebar["icons"]["controls"]["archive"],
        sidebar["icons"]["providers"]["checkout"]["busy"],
    ):
        assert SidebarGrid.cell_width(value) in {1, 2}


def test_sidebar_rejects_icons_wider_than_a_grid_track():
    with pytest.raises(UsageError, match="one or two terminal cells"):
        _sidebar_config(
            {"sidebar": {"icons": {"drivers": {"codex": "ABC"}}}},
            SidebarProviderRegistry(),
        )


def test_one_and_two_cell_driver_icons_keep_names_aligned(tmp_path):
    registry = SidebarProviderRegistry()
    sidebar = _sidebar_config(
        {
            "sidebar": {
                "icons": {"drivers": {"claude": "C", "codex": "🌀"}}
            }
        },
        registry,
    )
    lines = IdeSidebar.render_lines(
        tmp_path,
        [
            {"id": "one", "name": "ONE", "driver": {"id": "claude"}},
            {"id": "two", "name": "TWO", "driver": {"id": "codex"}},
        ],
        "/workspace/example",
        "none",
        0,
        20,
        focused=False,
        sidebar=sidebar,
        providers=registry,
    )
    one, two = _plain(lines[0]), _plain(lines[3])
    assert SidebarGrid.cell_width(one[: one.index("ONE")]) == 3
    assert SidebarGrid.cell_width(two[: two.index("TWO")]) == 3


def test_sidebar_grid_uses_the_shared_height_density_ladder():
    assert SidebarGrid.for_view(20, 15, 4).entry_rows == 3
    assert SidebarGrid.for_view(20, 14, 4).entry_rows == 2
    assert SidebarGrid.for_view(20, 10, 4).entry_rows == 1


def test_selected_rows_use_the_legacy_green_and_teal_palette():
    assert "48;5;46" in IdeSidebar.SEL_CUR
    assert "38;5;16" in IdeSidebar.SEL_CUR
    assert "48;5;51" in IdeSidebar.SEL_ALT
    assert "38;5;16" in IdeSidebar.SEL_ALT


def test_mutations_use_the_selected_python_module(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    IdeSidebar._repo_hint = str(workspace)
    seen = {}
    monkeypatch.setattr("subprocess.run", lambda argv, **kw: (
        seen.update(argv=argv, cwd=kw.get("cwd")) or SimpleNamespace(returncode=0)))

    assert IdeSidebar._cli(tmp_path, ["repair", "--session-id=session-id"]) is True
    assert seen["cwd"] == str(workspace)
    assert seen["argv"][:3] == [sys.executable, "-m", "hive_ide.cli"]
    assert seen["argv"][-2:] == ["repair", "--session-id=session-id"]


def test_missing_window_is_built_on_the_current_ide_socket(tmp_path, monkeypatch):
    calls = []
    windows = iter((None, "@9"))
    monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", "hive-ide-next")
    monkeypatch.setattr(
        SidebarCommandRunner,
        "window_id",
        lambda _self, _session_id: next(windows),
    )
    monkeypatch.setattr(
        SidebarCommandRunner,
        "cli",
        lambda _self, args: calls.append(args) or True,
    )
    monkeypatch.setattr(
        "hive_ide.sidebar.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    IdeSidebar._switch("session-id", tmp_path)

    assert calls == [
        [
            "repair",
            "--session-id=session-id",
            "--tmux-socket=hive-ide-next",
        ]
    ]


def test_sidebar_command_runner_switches_window_and_agent_pane(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        SidebarCommandRunner,
        "window_id",
        lambda _self, session_id: "@7" if session_id == "session-id" else None,
    )

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("hive_ide.sidebar.subprocess.run", fake_run)

    assert SidebarCommandRunner(tmp_path, str(tmp_path)).switch("session-id")
    assert calls == [
        ["tmux", "select-window", "-t", "@7"],
        ["tmux", "select-pane", "-t", "@7.1"],
        ["tmux", "send-keys", "-t", "@7.0", "-l", "\x1b[O"],
    ]


def test_sidebar_command_runner_does_not_select_after_failed_repair(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", "hive-ide-next")
    monkeypatch.setattr(SidebarCommandRunner, "window_id", lambda _self, _sid: None)
    monkeypatch.setattr(
        SidebarCommandRunner,
        "cli",
        lambda _self, args: calls.append(args) or False,
    )

    assert not SidebarCommandRunner(tmp_path, str(tmp_path)).switch("missing")
    assert calls == [["repair", "--session-id=missing", "--tmux-socket=hive-ide-next"]]


def test_normalized_driver_id_selects_the_legacy_icon(tmp_path):
    lines = IdeSidebar.render_lines(
        tmp_path,
        [
            {
                "id": "codex-id",
                "name": "CODEX",
                "driver": {"id": "codex"},
                "plan": {"path": None},
            }
        ],
        "/workspace/example",
        "other-id",
        0,
        30,
        focused=False,
    )
    assert any("🌀" in line for line in lines)
    assert all("📝" not in line for line in lines)


def test_entry_rows_compacts_as_the_pane_shrinks():
    """Full 3-row layout when it fits; drop the spacer, then the sub-line — never below 1.
    Losing decoration beats losing sessions off the bottom of the pane."""
    # 4 sessions, tall pane (3 footer + 12 = 15) → full layout
    assert IdeSidebar._entry_rows(4, 40) == 3
    # exactly enough for 3 rows each
    assert IdeSidebar._entry_rows(4, 3 + 4 * 3) == 3
    # one row short → drop the spacer
    assert IdeSidebar._entry_rows(4, 3 + 4 * 3 - 1) == 2
    # too short for 2 rows each → name-only
    assert IdeSidebar._entry_rows(4, 3 + 4 * 2 - 1) == 1
    # pathologically short still returns a usable 1
    assert IdeSidebar._entry_rows(40, 6) == 1
    # archive mode has a smaller footer, so it fits sooner
    assert IdeSidebar._entry_rows(4, 2 + 2 + 4 * 3, archive_mode=True) == 3


def _report(button: int, row_1based: int, col_1based: int = 3) -> "re.Match[bytes]":
    import re as _re
    return IdeSidebar.MOUSE_RE.search(
        f"\x1b[<{button};{col_1based};{row_1based}M".encode())


def test_click_index_uses_the_row_count_that_was_rendered():
    """The wrong-session bug: hit-testing must divide by the SAME entry_rows the draw
    used. With a compacted 2-row layout, dividing by 3 selects the wrong session."""
    # 2-row layout: rel 0,1 → session0; rel 2,3 → session1
    assert IdeSidebar._click_index(_report(0, 1), 2) == 0     # rel 0
    assert IdeSidebar._click_index(_report(0, 3), 2) == 1     # rel 2
    # THE BUG: that same physical row, hit-tested with the full 3-row assumption, is read
    # as the blank spacer instead of session 1 — a click that selects the wrong thing (or
    # nothing). This is why render and hit-test must share one entry_rows.
    assert IdeSidebar._click_index(_report(0, 3), 3) is None
    # 3-row layout: rel 0,1 → session0, rel 2 = spacer, rel 3 → session1
    assert IdeSidebar._click_index(_report(0, 1), 3) == 0
    assert IdeSidebar._click_index(_report(0, 4), 3) == 1
    # 1-row layout: every row is its own session
    assert IdeSidebar._click_index(_report(0, 1), 1) == 0
    assert IdeSidebar._click_index(_report(0, 2), 1) == 1
    # The footer action row owns both `show archive` and the `+` button.
    assert IdeSidebar._click_index(
        _report(0, 12, 19), 2, archive_row=11, create_col=18
    ) == IdeSidebar.CREATE_FOOTER_HIT
    assert IdeSidebar._click_index(
        _report(0, 12, 20), 2, archive_row=11, create_col=18
    ) == IdeSidebar.CREATE_FOOTER_HIT
    assert IdeSidebar._click_index(
        _report(0, 12, 1), 2, archive_row=11, create_col=18
    ) == IdeSidebar.ARCHIVE_HIT
    assert IdeSidebar._click_index(
        _report(0, 11, 1), 2, filter_row=10, archive_row=11, create_col=18
    ) == IdeSidebar.FILTER_HIT
    right_click = IdeSidebar._click_index(_report(2, 4), 3)
    assert right_click == IdeSidebar.OPTIONS_HIT_BASE - 1
    assert IdeSidebar._options_index(right_click) == 1
    # active footer row is a real clickable control, not part of the session list
    assert (
        IdeSidebar._click_index(_report(0, 12), 2, archive_row=11)
        == IdeSidebar.ARCHIVE_HIT
    )


def test_wheel_delta_decodes_scroll_and_ignores_clicks():
    """The wheel must be CLAIMED (not dropped) — that's what keeps tmux from hijacking the
    scroll into copy-mode, whose Up/Down bindings then eat the arrow keys."""
    # Natural/content direction: the wheel moves the LIST under a fixed cursor, so
    # wheel-up walks the selection DOWN. (Inverted reads backwards in the pane.)
    assert IdeSidebar._wheel_delta(_report(IdeSidebar.WHEEL_UP, 5)) == 1
    assert IdeSidebar._wheel_delta(_report(IdeSidebar.WHEEL_DOWN, 5)) == -1
    assert IdeSidebar._wheel_delta(_report(0, 5)) == 0          # a left click is not scroll


def test_drain_accumulates_wheel_and_still_returns_keys():
    """A fast flick coalesces several reports into one read; they must sum, and ordinary
    keystrokes sharing that read must still come through."""
    buf = (b"\x1b[<65;3;5M" b"\x1b[<65;3;5M" b"x" b"\x1b[<64;3;5M")
    click, wheel, keys, tail = IdeSidebar._drain(buf, 3)
    assert click is None
    assert wheel == -1         # down(-1) + down(-1) + up(+1), natural direction
    assert keys == b"x"
    assert tail == b""


def test_drain_maps_archive_footer_click():
    click, wheel, keys, tail = IdeSidebar._drain(
        b"\x1b[<0;3;12M",
        2,
        archive_row=11,
    )
    assert click == IdeSidebar.ARCHIVE_HIT
    assert wheel == 0
    assert keys == b""
    assert tail == b""


def test_drain_maps_create_click_on_archive_footer_row():
    click, wheel, keys, tail = IdeSidebar._drain(
        b"\x1b[<0;19;12M",
        2,
        archive_row=11,
        create_col=18,
    )
    assert click == IdeSidebar.CREATE_FOOTER_HIT
    assert wheel == 0
    assert keys == b""
    assert tail == b""


def test_drain_maps_filter_footer_click():
    click, wheel, keys, tail = IdeSidebar._drain(
        b"\x1b[<0;3;11M",
        2,
        filter_row=10,
        archive_row=11,
    )
    assert click == IdeSidebar.FILTER_HIT
    assert wheel == 0
    assert keys == b""
    assert tail == b""


# ---- the invariant behind the wrong-session clicks ----

def _sessions(n):
    return [{"id": f"id-{i}", "name": f"S{i}", "agents": {"active": "claude"},
             "last_active": "2026-07-22T05:00:00+00:00"} for i in range(n)]


def test_render_and_click_agree_for_every_density_and_size(tmp_path):
    """ROUND TRIP: whatever row `render_lines` draws a session on, `_click_index` must map
    back to that SAME session — at every density and list length.

    This is the invariant that actually broke: render dropped rows on a short pane while
    hit-testing still divided by 3, so clicks selected the wrong session. Checking the two
    against each other (rather than each against a hardcoded number) is what makes this
    class of drift impossible to reintroduce.
    """
    for entry_rows in (1, 2, 3):
        for n in (1, 2, 5, 9):
            lines = IdeSidebar.render_lines(
                tmp_path, _sessions(n), "/workspace/example", "id-0", 0, 30,
                entry_rows=entry_rows)
            for i in range(n):
                name_row0 = IdeSidebar.HEADER_ROWS + i * entry_rows      # 0-based
                got = IdeSidebar._click_index(_report(0, name_row0 + 1), entry_rows)
                assert got == i, (
                    f"entry_rows={entry_rows} n={n}: clicking session {i}'s name row "
                    f"({name_row0}) resolved to {got}")
            # and the drawn block is exactly as tall as the click math assumes
            assert len(lines) >= IdeSidebar.HEADER_ROWS + n * entry_rows


def test_entry_rows_choice_always_fits_the_rendered_block(tmp_path):
    """`_entry_rows` must pick a density whose ACTUAL rendered output fits the pane —
    otherwise the list overflows and screen rows stop matching sessions again."""
    for height in (10, 12, 16, 20, 30, 40):
        for n in (1, 3, 6, 10):
            er = IdeSidebar._entry_rows(n, height)
            lines = IdeSidebar.render_lines(
                tmp_path, _sessions(n), "/workspace/example", "id-0", 0, 30,
                entry_rows=er)
            assert er in (1, 2, 3)
            # the entry block itself must fit; footer/header may still push a very short
            # pane over, but the sessions must never be the part that overflows
            assert IdeSidebar.HEADER_ROWS + n * er <= max(height, IdeSidebar.HEADER_ROWS + n)
            assert len(lines) > 0


def test_crowded_sidebar_viewport_keeps_render_inside_pane(tmp_path):
    height = 14
    sessions = _sessions(30)
    cursor = 20
    entry_rows = IdeSidebar._entry_rows(len(sessions), height)
    visible, visible_cursor, start = IdeSidebar._viewport(
        sessions, cursor, height, entry_rows
    )
    lines = IdeSidebar.render_lines(
        tmp_path,
        visible,
        "/workspace/example",
        "id-20",
        visible_cursor,
        20,
        entry_rows=entry_rows,
    )[:height]

    assert len(lines) <= height
    assert start <= cursor < start + len(visible)
    assert all("example" not in line for line in lines[:2])
    assert any("S20" in line for line in lines)


def test_crowded_sidebar_keeps_filter_and_actions_visible(tmp_path):
    height = 14
    sessions = _sessions(30)
    cursor = 20
    entry_rows = IdeSidebar._entry_rows(len(sessions), height)
    visible, visible_cursor, _start = IdeSidebar._viewport(
        sessions, cursor, height, entry_rows
    )
    lines = IdeSidebar.render_lines(
        tmp_path,
        visible,
        "/workspace/example",
        "id-20",
        visible_cursor,
        20,
        entry_rows=entry_rows,
    )
    lines = IdeSidebar._pin_footer(lines, height, IdeSidebar.FOOTER_ROWS)[:height]

    assert len(lines) == height
    assert "filter" in _plain(lines[-3])
    footer = _plain(lines[-2])
    assert "show archive" in footer
    assert footer.rstrip().endswith("+")
    assert _plain(lines[-1]).strip() == ""


def test_sidebar_paint_clears_visible_pane_before_redraw():
    painted = IdeSidebar._paint(["one", "two"])

    assert painted.startswith(
        IdeSidebar.NO_WRAP
        + IdeSidebar.HOME
        + IdeSidebar.CLEAR_SCREEN
        + IdeSidebar.HOME
    )
    assert "one\ntwo" in painted
    assert painted.endswith(IdeSidebar.CLEAR_BELOW)
