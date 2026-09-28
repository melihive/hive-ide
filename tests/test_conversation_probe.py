"""Three-state conversation checks: exists, confirmed gone, and unknown."""

from pathlib import Path

import pytest

from hive_ide.drivers import bundled_drivers

REF = "01e5e081-63f1-4c38-a6c2-644acd2f655a"
WORKDIR = "/work/acme/app"
SLUG = "-work-acme-app"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return tmp_path


def _claude(reference: str = REF, working_dir: str = WORKDIR):
    return bundled_drivers()["claude"].conversation_exists(reference, working_dir)


def _codex(reference: str = REF):
    return bundled_drivers()["codex"].conversation_exists(reference, WORKDIR)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")


def test_claude_transcript_in_the_working_dir_project_exists(home):
    _touch(home / ".claude/projects" / SLUG / f"{REF}.jsonl")
    assert _claude() is True


def test_claude_missing_transcript_in_a_readable_store_is_gone(home):
    _touch(home / ".claude/projects" / SLUG / "other.jsonl")
    assert _claude() is False


def test_claude_without_a_store_is_unknown(home):
    assert _claude() is None


def test_claude_transcript_under_another_project_is_unknown_not_gone(home):
    _touch(home / ".claude/projects/-work-elsewhere" / f"{REF}.jsonl")
    assert _claude() is None


def test_claude_non_uuid_reference_is_unknown(home):
    (home / ".claude/projects").mkdir(parents=True)
    assert _claude("../../etc/passwd") is None


def test_claude_honours_claude_config_dir(home, monkeypatch):
    custom = home / "custom-claude"
    _touch(custom / "projects" / SLUG / f"{REF}.jsonl")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    assert _claude() is True


def test_claude_unreadable_store_is_unknown(home, monkeypatch):
    (home / ".claude/projects").mkdir(parents=True)

    def boom(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_file", boom)
    assert _claude() is None


def test_codex_rollout_exists(home):
    _touch(home / f".codex/sessions/2026/09/22/rollout-2026-09-22T13-38-52-{REF}.jsonl")
    assert _codex() is True


def test_codex_missing_rollout_in_a_readable_store_is_gone(home):
    _touch(home / ".codex/sessions/2026/09/22/rollout-2026-09-22T13-38-52-other.jsonl")
    assert _codex() is False


def test_codex_without_a_store_is_unknown(home):
    assert _codex() is None


def test_codex_archived_rollout_is_unknown_not_gone(home):
    (home / ".codex/sessions").mkdir(parents=True)
    _touch(home / f".codex/archived_sessions/rollout-2025-09-23T15-47-13-{REF}.jsonl")
    assert _codex() is None


def test_codex_session_name_reference_is_unknown(home):
    (home / ".codex/sessions").mkdir(parents=True)
    assert _codex("my named session") is None


def test_codex_honours_codex_home(home, monkeypatch):
    custom = home / "custom-codex"
    _touch(custom / f"sessions/2026/09/22/rollout-x-{REF}.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(custom))
    assert _codex() is True


def test_drivers_without_a_probe_are_unknown(home):
    drivers = bundled_drivers()
    assert drivers["term"].conversation_exists(REF, WORKDIR) is None
    assert drivers["antigravity"].conversation_exists(REF, WORKDIR) is None


def test_claude_slug_replaces_every_non_alphanumeric(home):
    _touch(home / ".claude/projects/-work-my-app-v2" / f"{REF}.jsonl")
    assert _claude(working_dir="/work/my.app_v2") is True


def test_claude_wrong_slug_degrades_to_unknown_never_gone(home):
    # Should Claude Code change its slug rule, the transcript is still found
    # under some project and must not be reported as gone.
    _touch(home / ".claude/projects/-work-my.app_v2" / f"{REF}.jsonl")
    assert _claude(working_dir="/work/my.app_v2") is None
