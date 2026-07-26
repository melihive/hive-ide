"""Modals resolve their own tmux context, and never exit without saying why.

The bug these pin: `display-popup` does NOT format-expand its shell-command. A binding
that passed `'#{window_name}'` handed the modal that literal string; the record lookup
missed and the modal returned 0, which with `-E` closes the popup instantly — "it flashes
and closes", with nothing on screen and nothing in a log.

Two independent failures made it survive:
  1. the binding passed a format into a context that does not expand formats, and
  2. the modal treated "I could not find the record" as a silent success.

So both are pinned here. A test that only covered (1) would go green the moment someone
"fixed" it by passing the value some other way, while the silent exit stayed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hive_ide.agentmodal import IdeAgentModal          # noqa: E402
from hive_ide.newmodal import IdeNewModal              # noqa: E402


class TestResolve:
    """`_resolve` prefers a real value and asks tmux for an unexpanded format."""

    def test_real_value_is_used_as_is(self):
        assert IdeNewModal._resolve("HIVE IDE", "#{window_name}") == "HIVE IDE"

    def test_unexpanded_format_is_not_trusted(self, monkeypatch):
        """The whole bug in one assertion: the literal `#{window_name}` must never be
        used as a lookup key. Before the fix this string WAS the key."""
        monkeypatch.setattr(IdeNewModal, "_ask_tmux", staticmethod(lambda fmt: "REAL WIN"))
        assert IdeNewModal._resolve("#{window_name}", "#{window_name}") == "REAL WIN"

    def test_missing_value_asks_tmux(self, monkeypatch):
        monkeypatch.setattr(IdeNewModal, "_ask_tmux", staticmethod(lambda fmt: "REAL WIN"))
        assert IdeNewModal._resolve(None, "#{window_name}") == "REAL WIN"

    def test_a_name_that_merely_contains_braces_is_still_a_real_value(self, monkeypatch):
        """Guard against over-matching: only a value that IS a format is suspect."""
        monkeypatch.setattr(IdeNewModal, "_ask_tmux",
                            staticmethod(lambda fmt: pytest.fail("must not ask tmux")))
        assert IdeNewModal._resolve("WEIRD #{X} NAME", "#{window_name}") == "WEIRD #{X} NAME"

    def test_tmux_failure_is_empty_not_an_exception(self, monkeypatch):
        """An unreachable tmux is UNKNOWN, and unknown must reach the caller as a value it
        can refuse on — never a traceback inside a popup nobody can read."""
        monkeypatch.setattr(IdeNewModal, "_ask_tmux", staticmethod(lambda fmt: ""))
        assert IdeNewModal._resolve("#{session_name}", "#{session_name}") == ""


class TestNeverSilentlyExits:
    """Every early exit reports. `_bail` returns non-zero and prints a reason."""

    def test_bail_is_nonzero_and_explains(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "hive_ide.newmodal.sys.stdin",
            type("S", (), {"isatty": lambda self: False})(),
        )
        rc = IdeNewModal._bail("No ide session record for 'X'.", "looked in /nowhere")
        out = capsys.readouterr().out
        assert rc != 0, "a bail must not look like success"
        assert "No ide session record" in out and "looked in /nowhere" in out

    def test_unresolvable_context_bails_rather_than_returning_zero(self, monkeypatch, tmp_path):
        """The exact regression: context cannot be resolved → the user must SEE why."""
        monkeypatch.setattr(IdeNewModal, "_ask_tmux", staticmethod(lambda fmt: ""))
        seen = {}

        def fake_bail(message, detail=""):
            seen["message"], seen["detail"] = message, detail
            return 1

        monkeypatch.setattr(IdeNewModal, "_bail", staticmethod(fake_bail))
        rc = IdeAgentModal.main(["ide_agentmodal.py", str(tmp_path)])
        assert rc == 1
        assert "session" in seen["message"].lower()

    def test_missing_record_bails_and_names_the_path(self, monkeypatch, tmp_path):
        """A window with no record used to `return 0`. It must now say where it looked —
        that message is what turns a mystery flash into a one-line diagnosis."""
        # Answer per-format, never by substring: `#{@ide_session_id}` also contains
        # "session", so a `"session" in fmt` stub silently drives the id path instead.
        answers = {"#{@hive_ide_session_id}": "", "#{@hive_ide_workspace_key}": "",
                   "#{session_name}": "/workspace/example", "#{window_name}": "GHOST"}
        monkeypatch.setattr(IdeNewModal, "_ask_tmux", staticmethod(lambda fmt: answers.get(fmt, "")))
        monkeypatch.setattr("hive_ide.agentmodal.sys.stdin",
                            type("S", (), {"isatty": lambda self: True, "fileno": lambda self: 0})())
        monkeypatch.setattr(IdeAgentModal, "_active", staticmethod(lambda *a, **k: None))
        seen = {}

        def fake_bail(message, detail=""):
            seen["message"], seen["detail"] = message, detail
            return 1

        monkeypatch.setattr(IdeNewModal, "_bail", staticmethod(fake_bail))
        rc = IdeAgentModal.main(["ide_agentmodal.py", str(tmp_path)])
        assert rc == 1
        assert "session" in seen["message"].lower()
        assert "resolvable name" in seen["detail"]


class TestBindingsCarryNoFormats:
    """The source-level guard: no `display-popup` command may embed a tmux format.

    This is the check that would have caught the bug before it shipped. It reads the
    generated binding strings out of the skill source rather than trusting a comment,
    because the comment saying "the repo comes from #{session_name}" was itself wrong.
    """

    SKILL = Path(__file__).resolve().parents[1] / "hive_ide" / "frame.py"

    def test_no_popup_command_embeds_a_format(self):
        src = self.SKILL.read_text(encoding="utf-8")
        offenders = []
        for i, line in enumerate(src.splitlines(), 1):
            if "display-popup" not in line:
                continue
            # Walk the whole call: a binding spans several lines.
            chunk = "\n".join(src.splitlines()[i - 1:i + 6])
            cmd = chunk.split("display-popup", 1)[1]
            # `-d "#{pane_current_path}"` is an OPTION arg — tmux DOES expand those.
            cmd = cmd.replace('"-d", "#{pane_current_path}"', "")
            if "#{" in cmd and "{{" in cmd:      # f-string escaped format → reaches tmux
                offenders.append(f"{self.SKILL.name}:{i}")
        assert not offenders, (
            "display-popup does not expand tmux formats in its shell-command; these "
            f"bindings would pass a literal '#{{...}}' string: {offenders}")

    def test_the_guard_actually_catches_it(self):
        """Meta-test: prove the check above is not vacuous by running it over the exact
        line that caused the bug."""
        bad = ('        self._tmux(["bind-key", "a", "display-popup", "-E",\n'
               '                    f"python3 -I {x} \'#{{window_name}}\'"])')
        cmd = bad.split("display-popup", 1)[1]
        cmd = cmd.replace('"-d", "#{pane_current_path}"', "")
        assert "#{" in cmd and "{{" in cmd, "the guard would not have caught the real bug"


class TestIdentityBeatsName:
    """Context resolves by the IMMUTABLE session id, not the mutable window name.

    A window name is a DISPLAY property: `ide rename`, tmux `automatic-rename`, and a hand
    rename all change it. The immutable id keeps those display changes from changing
    which record the modal targets.
    """

    @staticmethod
    def _tmux_answers(mapping):
        return staticmethod(lambda fmt: mapping.get(fmt, ""))

    def test_id_wins_and_the_window_name_is_never_consulted(self, monkeypatch, tmp_path):
        """The point of the whole change: a window RENAMED out from under the record still
        resolves, because the lookup never touches the name."""
        monkeypatch.setattr(IdeNewModal, "_ask_tmux", self._tmux_answers(
            {"#{@hive_ide_session_id}": "b48de262e16b",
             "#{@hive_ide_workspace_key}": "/workspace/example",
             "#{window_name}": "SOMETHING ELSE ENTIRELY"}))
        monkeypatch.setattr("hive_ide.agentmodal.StateIO.find_by_id",
                            staticmethod(lambda sd, repo, sid: (repo, "REAL NAME", {"id": sid})))
        assert IdeAgentModal._context(tmp_path, ["m", str(tmp_path)]) == (
            "/workspace/example",
            "b48de262e16b",
            "REAL NAME",
        )

    def test_a_stamped_id_that_resolves_to_nothing_is_an_error_not_a_fallback(
            self, monkeypatch, tmp_path):
        """Falling back to the name here would be worse than failing: a name can collide
        with a DIFFERENT session, so the modal would silently act on the wrong one."""
        monkeypatch.setattr(IdeNewModal, "_ask_tmux", self._tmux_answers(
            {"#{@hive_ide_session_id}": "deadbeef",
             "#{@hive_ide_workspace_key}": "/workspace/example",
             "#{window_name}": "HIVE IDE", "#{session_name}": "/workspace/example"}))
        monkeypatch.setattr("hive_ide.agentmodal.StateIO.find_by_id",
                            staticmethod(lambda sd, repo, sid: None))
        assert IdeAgentModal._context(tmp_path, ["m", str(tmp_path)]) is None

    def test_untagged_window_falls_back_to_names(self, monkeypatch, tmp_path):
        """A frame built before the tag existed must keep working until it is reopened."""
        monkeypatch.setattr(IdeNewModal, "_ask_tmux", self._tmux_answers(
            {"#{@hive_ide_session_id}": "", "#{@hive_ide_workspace_key}": "",
             "#{session_name}": "/workspace/example", "#{window_name}": "HIVE IDE"}))
        monkeypatch.setattr(
            "hive_ide.agentmodal.StateIO.find_by_identity",
            staticmethod(
                lambda sd, repo, name: (
                    repo,
                    name,
                    {"id": "compat-id", "name": name},
                )
            ),
        )
        assert IdeAgentModal._context(tmp_path, ["m", str(tmp_path)]) == (
            "/workspace/example",
            "compat-id",
            "HIVE IDE",
        )
