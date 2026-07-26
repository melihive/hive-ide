from __future__ import annotations

from hive_ide.drivers import bundled_drivers
from hive_ide.nav import IdeNav
from hive_ide.store import StateStore


def _seed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path, workspace)
    ids = {}
    for name, timestamp in (
        ("A", "2026-07-01T00:00:00+00:00"),
        ("B", "2026-07-02T00:00:00+00:00"),
        ("C", "2026-07-03T00:00:00+00:00"),
    ):
        record = store.create_session(
            name=name,
            working_dir=workspace,
            source={"kind": "stable", "interpreter": "/python", "version": "test"},
            driver=bundled_drivers()["term"].resolve(
                name=name, working_dir=str(workspace), conversation_reference=None
            ),
        )
        ids[name] = record["id"]
        record["last_active"] = timestamp
        store.write("sessions", record["id"], record)
    return str(workspace), ids


def test_order_matches_sidebar_activity_order(tmp_path):
    workspace, ids = _seed(tmp_path)
    assert IdeNav._order(tmp_path, workspace, list(ids.values())) == [
        ids["C"],
        ids["B"],
        ids["A"],
    ]


def test_order_filters_closed_and_untagged_windows(tmp_path):
    workspace, ids = _seed(tmp_path)
    assert IdeNav._order(tmp_path, workspace, [ids["A"], ids["C"]]) == [
        ids["C"],
        ids["A"],
    ]
    order = IdeNav._order(tmp_path, workspace, [*ids.values(), "ORPHAN"])
    assert order == [ids["C"], ids["B"], ids["A"]]
