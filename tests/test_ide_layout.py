"""`ide_layout` — THE single owner of ide geometry, plus the gate that keeps it single.

Three ide bugs shared one shape: two places computing layout independently and drifting
(render vs click hit-testing, the ladder vs `rebuild`, a hook wired to its own trigger).
Individual tests catch each; `test_no_geometry_defined_outside_the_owner` removes the
category by making a second definition fail the suite.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hive_ide.layout import IdeLayout  # noqa: E402

LIB = Path(__file__).resolve().parents[1]
SKILL_DIR = LIB.parent / "ide"

# Names that may be DEFINED only in ide_layout.py. Elsewhere they may be aliased
# (`X = IdeLayout.X`) but never assigned a literal.
OWNED = ("SIDEBAR_W", "PLAN_W", "AGENT_MIN", "PLAN_MIN", "AGENT_PREF",
         "HEADER_ROWS", "HEADER_ROWS_ARCHIVE", "ENTRY_ROWS", "FOOTER_ROWS",
         "FOOTER_ROWS_ARCHIVE")


def test_no_geometry_defined_outside_the_owner():
    """THE GATE. A geometry name assigned a LITERAL anywhere but `ide_layout.py` is a
    second source of truth — exactly how render and hit-testing drifted apart. Aliases
    (`SIDEBAR_W = IdeLayout.SIDEBAR_W`) are fine; literals are not."""
    offenders = []
    for path in list(LIB.glob("ide_*.py")) + list(SKILL_DIR.glob("*.py")):
        if path.name == "ide_layout.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in OWNED:
                # `NAME = 20` or `NAME, OTHER = 2, 3` — a literal, not an alias
                if re.match(rf"\s*{name}\b[^=\n]*=\s*[\d(]", line) and "IdeLayout" not in line:
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "geometry defined outside ide_layout.py — this is a second source of truth:\n  "
        + "\n  ".join(offenders))


def test_mobile_threshold_is_derived():
    assert IdeLayout.mobile_threshold() == (
        IdeLayout.SIDEBAR_W + IdeLayout.AGENT_MIN + IdeLayout.PLAN_MIN)
    assert IdeLayout.is_mobile(IdeLayout.mobile_threshold() - 1)
    assert not IdeLayout.is_mobile(IdeLayout.mobile_threshold())


def test_columns_returns_none_for_mobile_and_sums_to_the_window():
    assert IdeLayout.columns(100) is None                      # mobile → zoom instead
    for w in (120, 140, 166, 200, 300):
        side, agent, plan = IdeLayout.columns(w)
        assert side + agent + plan == w, w                     # nothing lost or invented
        assert agent >= IdeLayout.AGENT_MIN, w
        assert IdeLayout.PLAN_MIN <= plan <= IdeLayout.PLAN_W, w


def test_agent_keeps_its_preferred_width_before_the_plan_grows():
    """The 'plan too big on smaller screens' regression, now owned here."""
    for w in (150, 166, 180, 200):
        _, agent, _ = IdeLayout.columns(w)
        assert agent >= IdeLayout.AGENT_PREF, w


def test_row_math_round_trips_at_every_density():
    """`session_row` and `session_at_row` are inverses — render and hit-test cannot drift
    because they are the same arithmetic, in one place."""
    for entry_rows in (1, 2, 3):
        for i in range(6):
            row = IdeLayout.session_row(i, entry_rows)
            assert IdeLayout.session_at_row(row, entry_rows) == i, (entry_rows, i)


def test_spacer_and_header_rows_map_to_no_session():
    assert IdeLayout.session_at_row(0, 3) == 0                  # active: no header
    assert IdeLayout.session_at_row(0, 3, archive_mode=True) is None
    assert IdeLayout.session_at_row(1, 3, archive_mode=True) is None
    spacer = IdeLayout.HEADER_ROWS + IdeLayout.SPACER_OFFSET    # blank inside entry 0
    assert IdeLayout.session_at_row(spacer, 3) is None
    # compacted layouts have no spacer, so that same row IS a session
    assert IdeLayout.session_at_row(spacer, 2) is not None


def test_entry_rows_degrades_and_never_returns_zero():
    assert IdeLayout.entry_rows(4, 40) == IdeLayout.ENTRY_ROWS
    assert IdeLayout.entry_rows(4, 14) == 2
    assert IdeLayout.entry_rows(4, 10) == 1
    assert IdeLayout.entry_rows(50, 5) == 1                     # pathological still usable


def test_session_capacity_accounts_for_header_and_footer():
    assert IdeLayout.session_capacity(40, 3) == 12
    assert IdeLayout.session_capacity(12, 1) == 9
    assert IdeLayout.session_capacity(4, 1) == 1
    assert IdeLayout.session_capacity(7, 1, archive_mode=True) == 3


def test_consumers_alias_rather_than_redefine():
    """The skill and sidebar must expose the SAME values as the owner — proving the
    aliases are live, not stale copies that happen to match today."""
    sys.path.insert(0, str(SKILL_DIR))
    from hive_ide.sidebar import IdeSidebar
    from hive_ide.frame import Frame

    assert (Frame.SIDEBAR_W, Frame.PLAN_W) == (IdeLayout.SIDEBAR_W, IdeLayout.PLAN_W)
    assert Frame.AGENT_PREF == IdeLayout.AGENT_PREF
    assert Frame.SIDEBAR_ZOOM_MAX == IdeLayout.mobile_threshold()
    assert (
        IdeSidebar.HEADER_ROWS,
        IdeSidebar.HEADER_ROWS_ARCHIVE,
        IdeSidebar.ENTRY_ROWS,
    ) == (IdeLayout.HEADER_ROWS, IdeLayout.HEADER_ROWS_ARCHIVE, IdeLayout.ENTRY_ROWS)


# ---- verify's duplicate-tmux-option check (the mouse off/on that hid for a session) ----

def test_verify_flags_duplicate_depended_options(tmp_path, monkeypatch):
    """A tmux option the ide depends on, set twice, is legal-but-silent (last wins) — and
    that is exactly how `mouse off` then `mouse on` hid while arrows appeared dead."""
    sys.path.insert(0, str(SKILL_DIR))
    from hive_ide.frame import Frame
    from hive_ide.store import StateStore

    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse off\nset -g prefix C-b\n"
                    "# ...\nset -g mouse on\nset -g focus-events on\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    out = "\n".join(Frame(StateStore(tmp_path / "state", tmp_path)).verify_user_config())
    assert "`mouse` set 2x" in out, out
    assert "mouse on" in out, "must report which value actually wins"
    assert "prefix" not in out, "set once → not flagged"
    assert "focus-events" not in out, "set once → not flagged"


def test_verify_user_conf_clean_when_no_duplicates(tmp_path, monkeypatch):
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\nset -g prefix C-a\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from hive_ide.frame import Frame
    from hive_ide.store import StateStore
    out = Frame(StateStore(tmp_path / "state", tmp_path)).verify_user_config()
    assert out == []


def test_generated_tmux_config_keeps_prefix_and_disables_continuum(
    tmp_path, monkeypatch
):
    from hive_ide.frame import Frame
    from hive_ide.store import StateStore

    config = tmp_path / ".tmux.conf"
    config.write_text(
        "set -g prefix C-a\n"
        "set -g @continuum-restore 'on'\n"
        "run '~/.tmux/plugins/tpm/tpm'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    frame = Frame(StateStore(tmp_path / "state", tmp_path / "workspace"))

    generated = frame._ide_conf()
    text = generated.read_text(encoding="utf-8")

    assert "set -g prefix C-a" in text
    assert "@continuum-restore 'off'" in text
    assert "@continuum-save-interval '0'" in text
    assert text.index("@continuum-save-interval '0'") < text.index("/tpm'")


def test_name_max_matches_the_sidebar_column():
    """NAME_MAX is DERIVED from the sidebar width, not an independent product choice:
    a row is `<2-cell icon><space><name><space><1-cell dot>` inside SIDEBAR_W. It stays a
    literal so `ide_state` remains import-free for spawned panes — so this test is what
    keeps the two from drifting."""
    from hive_ide.state_compat import StateIO
    # Per ide_state's own derivation: SIDEBAR_W - icon(2) - space(1) - space+dot(2) = 15
    # cells of budget, and NAME_MAX is one less so a full-length name never ellipsis-
    # truncates in the list.
    budget = IdeLayout.SIDEBAR_W - 2 - 1 - 2
    assert StateIO.NAME_MAX == budget - 1, (
        f"NAME_MAX={StateIO.NAME_MAX} no longer fits SIDEBAR_W={IdeLayout.SIDEBAR_W} "
        f"(budget {budget}, expected {budget - 1})")
