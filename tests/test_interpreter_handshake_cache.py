"""The same question, asked once.

`inspect_interpreter` starts a whole interpreter to ask it its version, and
`repair_all` asks once per session. On a 15-session workspace that was 15
identical questions to the same `/usr/bin/python3` — 1.35s of a 4.06s warm
`open`, profiled 2026-11-12, where the frame needed no repair at all.

Cached for the life of the process, which is safe because an interpreter cannot
change version underneath one and every caller is a short-lived CLI command. The
two things that make a cache dangerous are pinned below: handing out the shared
object, and remembering a failure.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from hive_ide import source
from hive_ide.errors import UsageError


@pytest.fixture(autouse=True)
def _clean_cache():
    source._HANDSHAKES.clear()
    yield
    source._HANDSHAKES.clear()


def test_the_same_interpreter_is_only_asked_once(monkeypatch):
    calls: list[list[str]] = []
    real = subprocess.run

    def counting(argv, *a, **kw):
        calls.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(source.subprocess, "run", counting)

    for _ in range(15):
        source.inspect_interpreter(sys.executable)

    assert len(calls) == 1, (
        f"started {len(calls)} interpreters to ask one question 15 times"
    )


def test_each_caller_gets_its_own_copy(monkeypatch):
    """A shared dict lets one caller's edit rewrite what every later caller reads.

    Both directions, because they are guarded by different copies and a test that
    only does the first passes while the second is wide open — which is exactly
    what the mutation run caught. The FIRST return is protected by the copy taken
    when storing; a CACHED return is protected by the copy taken when reading.
    """
    first = source.inspect_interpreter(sys.executable)
    first["package_version"] = "TAMPERED-BY-FIRST"
    assert (
        source.inspect_interpreter(sys.executable)["package_version"]
        != "TAMPERED-BY-FIRST"
    ), "the stored handshake aliases the one handed to the first caller"

    cached = source.inspect_interpreter(sys.executable)
    cached["package_version"] = "TAMPERED-BY-CACHED"
    assert (
        source.inspect_interpreter(sys.executable)["package_version"]
        != "TAMPERED-BY-CACHED"
    ), "a cached read hands out the shared dict, so one caller rewrote it for all"


def test_two_interpreters_are_cached_apart(monkeypatch):
    seen: list[str] = []

    def fake(argv, *a, **kw):
        seen.append(argv[0])
        return subprocess.CompletedProcess(
            argv,
            0,
            f'{{"package_version":"1.0.0","protocol_version":{source.PROTOCOL_VERSION},'
            f'"schema_version":{source.SCHEMA_VERSION}}}',
            "",
        )

    monkeypatch.setattr(source.os, "access", lambda *a, **kw: True)
    monkeypatch.setattr(source.subprocess, "run", fake)

    source.inspect_interpreter("/usr/bin/python3")
    source.inspect_interpreter("/opt/venv/bin/python")
    source.inspect_interpreter("/usr/bin/python3")

    assert len(seen) == 2, f"a second interpreter reused the first one's answer: {seen}"


def _handshake_source(version: str) -> str:
    return (
        f'{{"package_version":"{version}","protocol_version":{source.PROTOCOL_VERSION},'
        f'"schema_version":{source.SCHEMA_VERSION}}}'
    )


def test_an_upgrade_in_this_process_is_not_answered_from_before_it(monkeypatch):
    """Codex review finding, 2026-11-12.

    `EnvironmentManager.install()` upgrades an environment and then inspects the
    same path. A handshake cached earlier in the same process describes the
    version the upgrade just replaced, so install would report the OLD version as
    the result of its own work — the one caller for which "an interpreter cannot
    change underneath one process" is false, because that caller is what changed
    it.
    """
    versions = iter(["1.0.0", "2.0.0"])
    monkeypatch.setattr(source.os, "access", lambda *a, **kw: True)
    monkeypatch.setattr(
        source.subprocess,
        "run",
        lambda argv, *a, **kw: subprocess.CompletedProcess(
            argv, 0, _handshake_source(next(versions)), ""
        ),
    )

    assert source.inspect_interpreter("/opt/venv/bin/python")["package_version"] == "1.0.0"
    # the upgrade happens here, outside this function's knowledge
    assert (
        source.inspect_interpreter("/opt/venv/bin/python", refresh=True)["package_version"]
        == "2.0.0"
    ), "reported the version it had just replaced"
    # and the refreshed answer replaces the cached one for later readers
    assert source.inspect_interpreter("/opt/venv/bin/python")["package_version"] == "2.0.0"


def test_install_asks_again_after_its_own_upgrade(monkeypatch, tmp_path):
    """The caller half of the finding — and the half a mutation run caught missing.

    Testing `inspect_interpreter(refresh=True)` proves the mechanism exists; it
    proves nothing about whether `install()` reaches for it. Removing the keyword
    from the one caller that needs it left every other test green.
    """
    from hive_ide.environments import EnvironmentManager

    seen: list[dict] = []

    monkeypatch.setattr(
        "hive_ide.environments.inspect_interpreter",
        lambda interpreter, **kwargs: seen.append(kwargs)
        or {"interpreter": str(interpreter), "package_version": "2.0.0"},
    )
    manager = EnvironmentManager(tmp_path)
    monkeypatch.setattr(
        EnvironmentManager, "_ensure_venv", lambda self, kind: tmp_path / "bin/python"
    )
    monkeypatch.setattr(EnvironmentManager, "_run", lambda self, argv, purpose: None)

    manager.install("stable", "hive-ide==2.0.0")

    assert seen == [{"refresh": True}], (
        f"install inspected the interpreter it had just upgraded without "
        f"refreshing: {seen}"
    )


def test_an_interpreter_that_has_since_been_removed_stops_answering(monkeypatch):
    """Codex review finding, 2026-11-12.

    The first version returned the cached handshake before checking the path was
    still executable, so an interpreter deleted after being inspected kept
    reporting itself healthy for the rest of the run. The check is a stat, not a
    subprocess, so there is no reason to skip it on a hit.
    """
    present = {"yes": True}
    monkeypatch.setattr(source.os, "access", lambda *a, **kw: present["yes"])
    monkeypatch.setattr(
        source.subprocess,
        "run",
        lambda argv, *a, **kw: subprocess.CompletedProcess(
            argv, 0, _handshake_source("1.0.0"), ""
        ),
    )

    source.inspect_interpreter("/opt/venv/bin/python")
    present["yes"] = False

    with pytest.raises(UsageError):
        source.inspect_interpreter("/opt/venv/bin/python")


def test_a_failed_handshake_is_not_remembered(monkeypatch):
    """A broken environment must be re-tested, not condemned for the whole run.

    Caching the failure would make one transient hiccup look permanent — and the
    remedy (install the package there) could then never clear it without
    restarting whatever is running.
    """
    attempts = {"n": 0}

    def flaky(argv, *a, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return subprocess.CompletedProcess(argv, 1, "", "no hive_ide here")
        return subprocess.CompletedProcess(
            argv,
            0,
            f'{{"package_version":"1.0.0","protocol_version":{source.PROTOCOL_VERSION},'
            f'"schema_version":{source.SCHEMA_VERSION}}}',
            "",
        )

    monkeypatch.setattr(source.os, "access", lambda *a, **kw: True)
    monkeypatch.setattr(source.subprocess, "run", flaky)

    with pytest.raises(UsageError):
        source.inspect_interpreter("/usr/bin/python3")

    assert source.inspect_interpreter("/usr/bin/python3")["package_version"] == "1.0.0"
    assert attempts["n"] == 2, "the failure was cached, so the retry never ran"


def test_a_rejected_protocol_is_not_remembered(monkeypatch):
    """Same rule one layer down: the handshake ran but the verdict was a refusal."""
    attempts = {"n": 0}

    def wrong_then_right(argv, *a, **kw):
        attempts["n"] += 1
        protocol = 0 if attempts["n"] == 1 else source.PROTOCOL_VERSION
        return subprocess.CompletedProcess(
            argv,
            0,
            f'{{"package_version":"1.0.0","protocol_version":{protocol},'
            f'"schema_version":{source.SCHEMA_VERSION}}}',
            "",
        )

    monkeypatch.setattr(source.os, "access", lambda *a, **kw: True)
    monkeypatch.setattr(source.subprocess, "run", wrong_then_right)

    with pytest.raises(UsageError):
        source.inspect_interpreter("/usr/bin/python3")
    assert source.inspect_interpreter("/usr/bin/python3")["package_version"] == "1.0.0"
    assert attempts["n"] == 2
