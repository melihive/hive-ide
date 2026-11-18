"""Repair must resolve a conversation stranded in a RETIRED frame — and only then.

The stalemate, observed in the field on 2026-11-17. A frame had been rebuilt
onto the current `hive-ide-next-<hash>` socket while the pre-2.0 server on
`hive-ide-<hash>` kept running, with the session's `claude` alive inside it for
31 days. No client was attached to that server, so nobody could see or type at
the driver, but it still owned the conversation. Every `hive-ide chat` therefore
hit the launch guard ("already held by pid N") and every `hive-ide repair`
rebuilt a frame whose agent could never start. Repairing harder never helps:
the holder has to be released.

`holders.orphaned` does not describe this shape — the driver's ppid is its own
`sh -c` wrapper, so it reads as perfectly parented. What proves it abandoned is
three facts together: another server, no client attached, a socket this package
hands out.

The refusals matter as much as the fix. Everything here is a process this
package did not start and the user never asked it to stop, so each gate is
pinned by its own test, and `unknown` protects rather than acts.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import sys
import subprocess
import tempfile
import time
import uuid

import pytest

from hive_ide.drivers import DriverRegistry, bundled_drivers
from hive_ide.frame import Frame
from hive_ide.holders import ConversationHolders
from hive_ide.repair import SessionRepair
from hive_ide.store import StateStore

from conftest import tmux_wait_budget


REFERENCE = "01352fc0-f747-4934-8771-3e3b357fb92b"
OTHER = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
LIVE_SOCKET = "hive-ide-next-abc12345"
RETIRED_SOCKET = "hive-ide-abc12345"


def _generated_launch(driver_id, *, reference=REFERENCE, name="SESSION"):
    """The argv this package really generates, asked of the driver itself.

    Never a copy of it. Ownership on the kill path is identity with our own
    launch, so a test that hand-wrote the expected shape would be pinning the
    test's guess instead of the package's behaviour — which is the whole class
    of mistake this predicate was rewritten to remove.
    """
    resolved = bundled_drivers()[driver_id].resolve(
        name=name, working_dir="/w", conversation_reference=reference
    )
    return [str(part) for part in resolved["launch_argv"]]


CLAUDE_LAUNCH = _generated_launch("claude")
CODEX_LAUNCH = _generated_launch("codex")


def _rows(*, server_argv: str = f"tmux -L {RETIRED_SOCKET} -f /x/ide.conf new-session -d"):
    """The reported process shape: server -> `sh -c` wrapper -> driver."""
    driver = " ".join(CLAUDE_LAUNCH)
    return [
        (100, 1, server_argv),
        (200, 100, f"sh -c unset NO_COLOR; {driver}"),
        (201, 200, driver),
    ]


def _argv_from(rows, pid):
    """argv for a fabricated row — the stand-in for the `/proc` reader."""
    for candidate, _ppid, command in rows:
        if candidate == pid:
            return command.split() or None
    return None


def _stranded(rows, *, live_socket=LIVE_SOCKET, launched_as=None,
              has_clients=lambda _socket, _server: False, argv_of=None):
    """`stranded` over fabricated rows, reading argv from those same rows.

    The production reader is `/proc`, which fabricated PIDs do not have. Splitting
    the row's command is the honest stand-in for a shape with no quoted argument
    in it; the tests that care about ARGUMENT BOUNDARIES pass real argv lists.
    """
    commands = {pid: command for pid, _ppid, command in rows}
    default = lambda pid: (commands.get(pid) or "").split() or None
    return ConversationHolders.stranded(
        REFERENCE,
        launched_as=CLAUDE_LAUNCH if launched_as is None else launched_as,
        live_socket=live_socket, has_clients=has_clients,
        argv_of=argv_of or default, rows=rows,
    )


# ---------------------------------------------------------------- classification


def test_the_server_is_found_by_walking_parents_not_by_ppid():
    """The driver's parent is its wrapper; only the walk reaches the server.

    This is why `orphaned` (ppid == 1) misses the whole shape: no process in the
    chain has ppid 1 until the server itself.
    """
    rows = _rows()
    assert ConversationHolders.server_of(201, rows) == (RETIRED_SOCKET, 100)
    assert ConversationHolders.server_of(200, rows) == (RETIRED_SOCKET, 100)
    assert all(ppid != 1 for pid, ppid, _ in rows if pid != 100)


def test_a_driver_in_a_retired_clientless_server_is_stranded():
    found = _stranded(_rows())
    assert [(h["pid"], h["socket"]) for h in found] == [(201, RETIRED_SOCKET)]


def test_a_server_someone_is_attached_to_is_never_touched():
    """A client means a human can see it. That is use, not abandonment."""
    assert _stranded(_rows(), has_clients=lambda _socket, _server: True) == []


def test_an_unanswerable_client_question_protects_the_process():
    """None is not False. An unreadable answer must never authorize a signal."""
    assert _stranded(_rows(), has_clients=lambda _socket, _server: None) == []


def test_the_live_frames_own_driver_is_never_stranded():
    """Same socket as the frame being repaired: that is the session's own agent."""
    assert _stranded(_rows(), live_socket=RETIRED_SOCKET) == []


def test_a_server_this_package_did_not_name_is_left_alone():
    """Someone else's tmux is not ours to reason about, let alone signal."""
    rows = _rows(server_argv="tmux -L work-stuff -f /x/conf new-session -d")
    assert _stranded(rows) == []


def test_a_driver_with_no_tmux_ancestor_at_all_is_left_alone():
    """No server in the chain is no evidence of a retired frame."""
    rows = [
        (300, 1, f"sh -c claude --resume {REFERENCE}"),
        (301, 300, f"claude --resume {REFERENCE} --name SESSION"),
    ]
    assert _stranded(rows) == []


# ------------------------------------------------------------------- the action

#: The release path reads a server's environment from `/proc` (a stated limit —
#: see `_socket_root_of`), so on a platform without it the feature is inert by
#: design. These tests therefore SKIP there rather than fail: a documented gap
#: must not read as a broken build on the platform it was documented for.
pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or not pathlib.Path("/proc/self/environ").exists(),
    reason="needs tmux and a /proc that carries process environments",
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _fake_driver(tmp_path):
    """A real long-running process whose argv is an npm claude install's.

    The kill path vouches only for launches this package DEFINES, so the fixture
    cannot configure a driver of its own — the record has to be the bundled
    `claude`. That rules out a renamed copy of `sh`, which cannot be given
    `--resume <id>` without dying on the unknown option.

    An npm install is the way out, and it is a shape the package genuinely
    supports: a shebang script, so the kernel starts the INTERPRETER with the
    entrypoint as its first argument. `python3 …/claude-code/cli.js --resume
    <id> --name <name>` is that shape exactly — the file is named as npm names
    it and holds a sleeper, because what `ps` and the matcher read is the argv,
    never the language inside.
    """
    entrypoint = tmp_path / "npm" / "claude-code" / "cli.js"
    if not entrypoint.exists():
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("import time\ntime.sleep(600)\n")
    return entrypoint


def _fake_launch_argv(tmp_path, *, reference=REFERENCE):
    """The fake driver's argv — the bundled launch, run through an interpreter."""
    return [
        sys.executable, str(_fake_driver(tmp_path)),
        "--resume", reference, "--name", "SESSION",
    ]


def _wait_for_driver(entrypoint, *, reference=REFERENCE):
    """The pid of the fake driver, once the process table can actually see it.

    `tmux new-session -d` returns when the SERVER is up, not when the pane's
    command has been forked and exec'd, so a probe fired straight afterwards
    misses the driver about half the time — measured, not guessed. A test that
    probes without waiting therefore passes or fails on machine load rather than
    on the behaviour it names.
    """
    deadline = time.monotonic() + tmux_wait_budget(5.0)
    while time.monotonic() < deadline:
        for candidate, _ppid, command in ConversationHolders.snapshot():
            # argv[0] must be the INTERPRETER and argv[1] the entrypoint. The
            # tmux server's own argv carries the whole pane command — entrypoint
            # and reference included — so a substring match picks up the server
            # instead, which this helper did until the kill it asserted turned
            # out to be the server exiting as its last pane went away.
            tokens = command.split()
            if tokens[:2] == [sys.executable, str(entrypoint)] and reference in command:
                return candidate
        time.sleep(0.05)
    raise AssertionError("the fake driver never appeared in the process table")


@pytest.fixture
def retired_server(tmp_path):
    """A real second tmux server, clientless, running a real fake driver."""
    socket = f"hive-ide-retired-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    entrypoint = _fake_driver(tmp_path)
    # The pane runs the argv the PACKAGE generates for the bundled driver,
    # because that identity is what authorizes the release.
    subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-s", "stale",
         shlex.join(_fake_launch_argv(tmp_path))],
        check=True, capture_output=True, text=True,
    )
    pid = _wait_for_driver(entrypoint)
    try:
        yield socket, pid
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
        for _ in range(40):
            if not _alive(pid):
                break
            time.sleep(0.05)


def _repair_for(tmp_path, *, reference=REFERENCE):
    """A SessionRepair whose session wants `reference`, with NO window built.

    Not building the frame is deliberate: with no agent pane there is no live
    driver, which is exactly the state a stranded session is in, and it keeps
    this test about the release rather than about window construction.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="SESSION",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        # Resolved through the BUNDLED driver, as `create` and `refresh_driver`
        # both do: the record has to carry the launch argv, because that is what
        # the release path compares a live process against — and it has to be a
        # launch this package defines, or the release declines it.
        driver=bundled_drivers()["claude"].resolve(
            name="SESSION", working_dir=str(workspace), conversation_reference=reference
        ),
    )
    frame = Frame(store, socket=f"hive-ide-live-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    return SessionRepair(store, frame), record


@pytestmark_tmux
def test_repair_releases_a_stranded_driver_and_says_what_it_stopped(
    tmp_path, retired_server
):
    """End to end on real processes: the holder is gone and the report names it."""
    socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    actions: list[str] = []
    warnings: list[str] = []

    assert _alive(pid), "precondition: the stranded driver is running"
    repair._release_stranded_holder(record, actions, warnings, apply=True)

    assert not _alive(pid), "the stranded driver survived the repair"
    assert warnings == []
    assert any(f"pid {pid}" in line and socket in line for line in actions), actions
    assert any("conversation released" in line for line in actions), actions


@pytestmark_tmux
def test_a_dry_run_reports_the_stranded_driver_without_stopping_it(
    tmp_path, retired_server
):
    """`--dry-run` has to be safe on the one path that signals a process."""
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=False)

    assert _alive(pid), "a dry run stopped a process"
    assert any("would stop" in line for line in actions), actions


@pytestmark_tmux
def test_repair_leaves_the_driver_alone_when_a_client_is_attached(
    tmp_path, retired_server, monkeypatch
):
    """The gate that decides this is `_socket_has_clients`; hold it True."""
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    monkeypatch.setattr(repair, "_socket_has_clients", lambda _s, _p: True)
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "a server with a client attached was signalled anyway"
    assert actions == []


@pytestmark_tmux
def test_repair_leaves_the_driver_alone_when_the_question_cannot_be_answered(
    tmp_path, retired_server, monkeypatch
):
    """Unknown protects — the same rule the orphan sweeper already lives by."""
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    monkeypatch.setattr(repair, "_socket_has_clients", lambda _s, _p: None)
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "an unanswerable client question still got a signal"
    assert actions == []


@pytestmark_tmux
def test_a_session_whose_conversation_is_elsewhere_signals_nothing(
    tmp_path, retired_server
):
    """The holder must match THIS session's conversation, not merely exist."""
    _socket, pid = retired_server
    repair, record = _repair_for(
        tmp_path, reference="99999999-0000-4000-8000-000000000000"
    )
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "a driver on an unrelated conversation was stopped"
    assert actions == []
    # Positive control, so this cannot pass by finding nothing at all: the very
    # same process IS stranded when asked about the conversation it holds.
    assert ConversationHolders.stranded(
        REFERENCE,
        launched_as=_fake_launch_argv(tmp_path),
        live_socket=repair.frame.socket,
        has_clients=repair._socket_has_clients,
    ), "the fixture's driver was not stranded even for its own conversation"


@pytestmark_tmux
def test_the_client_question_tells_no_clients_apart_from_no_server(tmp_path):
    """False and None are different answers, and only False authorizes acting."""
    repair, _record = _repair_for(tmp_path)
    socket = f"hive-ide-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-s", "s", "sh -c 'sleep 30'"],
        check=True, capture_output=True, text=True,
    )
    server_pid = int(subprocess.run(
        ["tmux", "-L", socket, "display-message", "-p", "#{pid}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())
    try:
        assert repair._socket_has_clients(socket, server_pid) is False, (
            "a live server with nobody attached must answer False"
        )
        assert repair._socket_has_clients(socket, server_pid + 10_000_000) is None, (
            "a server that is not the one we walked to must answer None"
        )
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    assert repair._socket_has_clients(socket, server_pid) is None, (
        "a socket with no server must answer None, not False"
    )


@pytestmark_tmux
def test_a_holder_is_still_found_when_the_pane_is_narrow(retired_server, monkeypatch):
    """`ps` truncates the command to $COLUMNS — and every frame pane exports it.

    This is not a test-harness quirk. The package runs its `ps` from inside tmux
    panes, which set COLUMNS, so on a narrow pane a driver's argv was cut long
    before its `--resume <uuid>`. The holder then became invisible, the launch
    guard failed OPEN, and a second driver started on a conversation that was
    already held — the permanently blank pane `holders.py` exists to prevent.
    Reproduced at 80 columns, which is narrower than a phone frame's agent pane.
    """
    _socket, pid = retired_server
    monkeypatch.setenv("COLUMNS", "80")

    rows = ConversationHolders.snapshot()

    assert any(row[0] == pid for row in rows), "the holder vanished from `ps`"
    assert any(pid == row[0] and REFERENCE in row[2] for row in rows), (
        "the holder's row was truncated before its conversation reference"
    )


# ------------------------------------- what a `-L` name is, and is not (review)


@pytest.mark.parametrize(
    "argv, expected",
    [
        (f"tmux -L {RETIRED_SOCKET} new-session -d", RETIRED_SOCKET),
        (f"tmux -L{RETIRED_SOCKET} new-session -d", RETIRED_SOCKET),
        # tmux takes the LAST of a repeated option. A search returns the first,
        # which names a different server than the one actually running.
        (f"tmux -L {RETIRED_SOCKET} -L hive-ide-other new-session", "hive-ide-other"),
        # A name that only PARTLY matches is not this name: `hive-ide-old` is a
        # real and different socket from `hive-ide-old:active`.
        (f"tmux -L {RETIRED_SOCKET}:active new-session", None),
        # `-S` addresses a socket by path, so the name says nothing about where
        # the server is — including when both are given.
        ("tmux -S /elsewhere/active new-session", None),
        (f"tmux -L {RETIRED_SOCKET} -S /elsewhere/active new-session", None),
        ("tmux -L", None),
    ],
)
def test_the_socket_name_is_parsed_as_an_option_not_searched_for(argv, expected):
    assert ConversationHolders._socket_from_tmux_argv(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        f"tmux -L {RETIRED_SOCKET} -L hive-ide-other new-session",
        f"tmux -L {RETIRED_SOCKET}:active new-session",
        f"tmux -L {RETIRED_SOCKET} -S /elsewhere/active new-session",
    ],
)
def test_a_misread_socket_name_cannot_strand_the_wrong_server(argv):
    """The parse decides what gets signalled, so a wrong read must not reach it."""
    found = _stranded(_rows(server_argv=argv))
    assert [h.get("socket") for h in found] != [RETIRED_SOCKET]


@pytestmark_tmux
def test_a_server_in_another_socket_directory_is_still_reached(tmp_path, monkeypatch):
    """The reported case: the retired frame's socket is NOT where the live one's is.

    A frame from before the pinned socket directory keeps its socket in the
    default location while the live frame's lives elsewhere. Probing in the live
    frame's directory finds nothing there, answers "unknown", and the stranded
    session stays stuck forever — the feature failing on the exact shape it was
    built for (cross-model review, 2026-11-17).
    """
    # A unix socket path is capped near 108 bytes, so this root and this name
    # both stay short — a pytest tmp_path plus a descriptive name overruns it.
    elsewhere = pathlib.Path(tempfile.mkdtemp(prefix="hv"))
    entrypoint = _fake_driver(tmp_path)
    socket = f"hive-ide-e{uuid.uuid4().hex[:6]}"
    env = {**os.environ, "TMUX_TMPDIR": str(elsewhere)}
    started = subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-s", "stale",
         shlex.join(_fake_launch_argv(tmp_path))],
        capture_output=True, text=True, env=env,
    )
    assert started.returncode == 0, f"tmux refused: {started.stderr!r}"
    _wait_for_driver(entrypoint)
    repair, record = _repair_for(tmp_path)
    assert repair.frame.socket_tmpdir != str(elsewhere)
    actions: list[str] = []
    try:
        repair._release_stranded_holder(record, actions, [], apply=True)
        assert any("stopped stranded driver" in line for line in actions), (
            f"a retired server in another socket directory was never reached: {actions}"
        )
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"],
                       capture_output=True, env=env)
        shutil.rmtree(elsewhere, ignore_errors=True)


@pytestmark_tmux
def test_a_client_that_attaches_while_we_probe_calls_the_signal_off(
    tmp_path, retired_server, monkeypatch
):
    """Selection and signal are different moments, and the gap is not small.

    Choosing a candidate costs two tmux round trips and a `ps`; a client can
    attach inside that window, turning an abandoned server into one a human is
    sitting at. The verdict is therefore re-asked as late as it can be.
    """
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    answers = iter([False, True])       # free when chosen, attached when signalled

    monkeypatch.setattr(
        repair, "_socket_has_clients", lambda _s, _p: next(answers, True)
    )
    actions: list[str] = []
    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "a client attached mid-probe and the driver was signalled anyway"
    assert actions == []


@pytestmark_tmux
def test_a_recycled_pid_is_not_the_process_we_chose(tmp_path, retired_server):
    """`kill(pid, 0)` proves a number is live, never that it is the same program."""
    _socket, pid = retired_server
    repair, _record = _repair_for(tmp_path)
    holder = {
        "pid": pid,
        "command": "something else entirely --resume other",
        "socket": _socket,
        "server_pid": 1,
    }

    assert repair._still_stranded(holder) is False, (
        "a PID whose argv no longer matches was accepted as the same process"
    )
    assert _alive(pid)


def test_a_backgrounded_sessions_driver_child_is_never_stranded(monkeypatch):
    """A backgrounded session is held ON PURPOSE — host and children alike.

    The host spawns the real driver as a child carrying no marker of its own, so
    exempting only the host leaves that child looking abandoned. For the launch
    guard that cost a refused launch; here it would cost a SIGTERM to a session
    the user chose to background (cross-model review, 2026-11-17).
    """
    rows = _rows()
    monkeypatch.setattr(
        ConversationHolders, "background_tree",
        classmethod(lambda _cls, _reference, rows=None: {201}),
    )
    assert _stranded(rows) == []


def test_the_live_server_is_excluded_by_identity_not_by_name():
    """Same `-L` name, different server: that holder is stranded, not ours.

    Two servers can share a socket NAME under different `TMUX_TMPDIR` roots.
    Excluding by name alone would hide a genuinely stranded holder behind the
    coincidence, and repair could never release the conversation.
    """
    rows = _rows()
    same_name_other_server = _stranded(
        rows, live_socket=RETIRED_SOCKET, has_clients=lambda _s, _p: False
    )
    assert same_name_other_server == [], "no identity given: the name must still protect"

    found = ConversationHolders.stranded(
        REFERENCE,
        launched_as=CLAUDE_LAUNCH,
        live_socket=RETIRED_SOCKET,      # the live frame happens to share the name
        live_server_pid=999_999,         # but it is a different server
        has_clients=lambda _s, _p: False,
        argv_of=lambda pid: _argv_from(rows, pid),
        rows=rows,
    )
    assert [h["pid"] for h in found] == [201], (
        "a holder under a DIFFERENT server was hidden by a matching socket name"
    )

    mine = ConversationHolders.stranded(
        REFERENCE,
        launched_as=CLAUDE_LAUNCH,
        live_socket="unrelated-name",
        live_server_pid=100,             # this IS the live frame's own server
        has_clients=lambda _s, _p: False,
        argv_of=lambda pid: _argv_from(rows, pid),
        rows=rows,
    )
    assert mine == [], "the live frame's own driver was treated as stranded"


def test_a_driver_that_merely_MENTIONS_the_conversation_is_not_stranded():
    """`--resume OTHER -p 'see TARGET'` resumes OTHER. Signalling it is wrong.

    `holders()` matches the looser way on purpose — it feeds the launch guard,
    where a false positive only refuses a launch. On the kill path the same
    false positive terminates somebody's live, unrelated agent, so this path
    demands the reference be the VALUE of the selection flag.
    """
    rows = [
        (100, 1, f"tmux -L {RETIRED_SOCKET} new-session -d"),
        (201, 100, f"claude --resume {OTHER} -p Explain transcript {REFERENCE}"),
    ]
    # The loose matcher — the one the guard uses — does consider it a holder.
    assert ConversationHolders.holders(REFERENCE, rows=rows), (
        "precondition: the guard still sees it, which is what makes this a trap"
    )
    assert _stranded(rows) == [], "a driver resuming ANOTHER conversation was selected"


def test_a_boolean_flag_before_the_session_does_not_promote_the_prompt():
    """`codex resume --no-daemon OTHER TARGET` resumes OTHER, not TARGET.

    The first of six P1s, and the one that shows why the question changed. A
    boolean flag takes no value, so a parse that swallowed one shifted the
    window left and read the PROMPT as the session — authorizing a SIGTERM
    against a live agent on another conversation. Nothing here knows which flags
    take a value any more: this argv is simply not the one we launched.
    """
    argv = ["codex", "resume", "--no-daemon", OTHER, REFERENCE]

    assert not ConversationHolders.selects_exactly(
        argv, REFERENCE, launched_as=CODEX_LAUNCH
    ), "the prompt was read as the session this argv selects"
    # Nor does it select the conversation it really resumes. The old answer said
    # True here; the record cannot say so, because we launched no such argv.
    assert not ConversationHolders.selects_exactly(
        argv, OTHER, launched_as=_generated_launch("codex", reference=OTHER)
    )

    rows = [
        (100, 1, f"tmux -L {RETIRED_SOCKET} new-session -d"),
        (201, 100, " ".join(argv)),
    ]
    assert _stranded(rows, argv_of=lambda pid: argv if pid == 201 else None) == [], (
        "a driver resuming ANOTHER conversation was authorized for a signal"
    )
    # Positive control: the argv this package generates still releases, so the
    # proof moved without disabling the repair.
    assert ConversationHolders.selects_exactly(
        CODEX_LAUNCH, REFERENCE, launched_as=CODEX_LAUNCH
    )


@pytest.mark.parametrize(
    "argv, selected",
    [
        # What this package launches — and the shape found in the field on
        # 2026-11-17, which is why exactness had to stay reachable.
        (CLAUDE_LAUNCH, True),
        # argv[0] by executable name: the record holds the command as configured,
        # while the running process carries the path the exec resolved to.
        ([f"/opt/claude-code/bin/{CLAUDE_LAUNCH[0]}", *CLAUDE_LAUNCH[1:]], True),
        ([f"{CLAUDE_LAUNCH[0]}.cmd", *CLAUDE_LAUNCH[1:]], True),
        ([f"/Applications/Claude/{CLAUDE_LAUNCH[0].upper()}", *CLAUDE_LAUNCH[1:]], True),
        # ONE trailing positional is the launch prompt, which the record drops
        # once the launch has delivered it — refusing it would make this path
        # inert for every session started with `--prompt`.
        ([*CLAUDE_LAUNCH, "pick up where we left off"], True),
        # Beyond that one, agreement is the WHOLE argv and never a prefix of it.
        # `claude` takes the LAST of a repeated option, so an appended
        # `--resume other` moves the process to another conversation with every
        # token of our launch still present and in order.
        ([*CLAUDE_LAUNCH, "--resume", "other"], False),
        ([*CLAUDE_LAUNCH, "a prompt", "and another"], False),
        # A single trailing OPTION is not a prompt: a launch prompt can never
        # start with `-` (`launch_prompt_problem` refuses one), so an option
        # there is a token we did not launch — and keeping the allowance to
        # prompts is what stops it becoming a general "one extra token" rule.
        ([*CLAUDE_LAUNCH, "--debug"], False),
        # These two are refused one rule earlier, by
        # `OVERRIDING_SELECTION_OPTIONS`, whatever their position.
        ([*CLAUDE_LAUNCH, "--fork-session"], False),
        ([*CLAUDE_LAUNCH, "-c"], False),
        # Short of the launch, and one token apart from it — a session renamed
        # since its driver started. Refusing is the safe direction: the holder is
        # left alone and the stalemate stays visible.
        (CLAUDE_LAUNCH[:-1], False),
        ([*CLAUDE_LAUNCH[:-1], "RENAMED"], False),
        ([], False),
    ],
)
def test_only_the_argv_this_package_launched_selects_the_conversation(argv, selected):
    assert ConversationHolders.selects_exactly(
        argv, REFERENCE, launched_as=CLAUDE_LAUNCH
    ) is selected


@pytest.mark.parametrize(
    "argv, launched_as",
    [
        # A BOOLEAN flag takes no value, so a parse that swallowed one read the
        # PROMPT as the session. Shape could not rescue it either: a codex
        # session id may be a NAME as readily as a UUID.
        (["codex", "resume", "--no-daemon", OTHER, REFERENCE], CODEX_LAUNCH),
        (["codex", "resume", "--no-daemon", "my-other-session", REFERENCE], CODEX_LAUNCH),
        # `--last` picks the session ITSELF, leaving the positional a prompt.
        (["codex", "resume", "--last", REFERENCE], CODEX_LAUNCH),
        (["codex", "resume", "--last", "my-other-session", REFERENCE], CODEX_LAUNCH),
        # `--continue` is BOOLEAN: it resumes the most recent conversation and
        # the token after it is a prompt.
        (["claude", "--continue", REFERENCE], CLAUDE_LAUNCH),
        (["claude", "-c", REFERENCE], CLAUDE_LAUNCH),
        # A FORK starts a NEW conversation from the one it resumes, so the
        # resumed id is its SOURCE and the process does not hold it.
        (["claude", "--resume", REFERENCE, "--fork-session"], CLAUDE_LAUNCH),
        (["claude", "--fork-session", f"--resume={REFERENCE}"], CLAUDE_LAUNCH),
        # A repeated selector: the CLI takes the LAST, so believing the FIRST
        # match pointed this at a conversation the process had moved off.
        (["claude", "--resume", REFERENCE, "--resume", "other"], CLAUDE_LAUNCH),
        (["claude", "--resume", "other", "--resume", REFERENCE], CLAUDE_LAUNCH),
        # An option a later release adds, whose arity nothing here can know —
        # including a VARIADIC one such as `-i/--image <FILE>...`.
        (["codex", "resume", "--flag-from-a-later-codex", REFERENCE], CODEX_LAUNCH),
        (["codex", "resume", "-i", "a.png", REFERENCE], CODEX_LAUNCH),
        # A mention is not a selection, however it is spelled.
        (["claude", "--resume", "other", "-p", f"see {REFERENCE}"], CLAUDE_LAUNCH),
        (["claude", "-p", REFERENCE], CLAUDE_LAUNCH),
        (["claude", "-p", "resume", REFERENCE], CLAUDE_LAUNCH),
        (["claude", "--resume", f"{REFERENCE}x"], CLAUDE_LAUNCH),
    ],
)
def test_every_shape_that_once_authorized_a_signal_is_refused(argv, launched_as):
    """The six P1s of review rounds 6-8, and the near misses around them.

    Each was a different way to misread an argv, and each is refused here for one
    reason instead of six delicate ones: it is not what this package launched. So
    is the seventh shape nobody has thought of — which is the point of the
    change, and also why this table alone proves nothing. The arm that
    discriminates lives in
    `test_only_the_argv_this_package_launched_selects_the_conversation`, and both
    arms are mutation-checked.
    """
    assert not ConversationHolders.selects_exactly(
        argv, REFERENCE, launched_as=launched_as
    )


def test_a_launch_that_names_no_conversation_proves_nothing_about_one():
    """`agy -c` resumes by WORKSPACE: its argv names no conversation at all.

    The record is what names the conversation now, so a launch carrying no
    reference can never prove WHICH conversation a process holds — even when the
    process is exactly the one we launched. The old answer reached the same
    verdict by knowing that `-c` is boolean; this one needs no such knowledge.
    """
    launched_as = _generated_launch("antigravity")

    assert REFERENCE not in launched_as, launched_as
    assert not ConversationHolders.selects_exactly(
        launched_as, REFERENCE, launched_as=launched_as
    )


def test_with_no_record_of_launching_it_nothing_is_stranded():
    """No record, no proof — and the holder is left alone rather than signalled."""
    rows = _rows()

    assert _stranded(rows), "precondition: this holder IS released with the record"
    assert _stranded(rows, launched_as=[]) == []
    assert _stranded(rows, launched_as=_generated_launch("claude", reference=OTHER)) == []


def test_the_launch_compared_against_is_the_launch_that_runs():
    """One source for what we start and what we recognise.

    The proof is identity with our own launch, so an expectation assembled
    anywhere but the launcher would be a second guess about our own behaviour —
    the exact mistake, one layer in, that this predicate was rewritten to remove.
    """
    record = {
        "name": "SESSION",
        "driver": {
            "id": "codex",
            "launch_argv": CODEX_LAUNCH,
            "resume": {"strategy": "conversation_id", "reference": REFERENCE},
        },
    }

    launched_as = Frame.agent_launch_argv(record)

    assert launched_as == CODEX_LAUNCH, launched_as
    assert shlex.join(launched_as) in Frame._agent_command(record)
    assert ConversationHolders.selects_exactly(
        launched_as, REFERENCE, launched_as=launched_as
    )
    assert not ConversationHolders.selects_exactly(
        launched_as, "other-reference-id", launched_as=launched_as
    )


def test_a_launch_prompt_the_record_has_already_dropped_still_matches():
    """The one place a running argv outlives what the record can render.

    A launch prompt is passed as the driver's positional argument, and the record
    drops it the moment that launch delivers it, so from then on the process
    carries a token the record cannot reproduce. A stranded driver is weeks old
    by definition, so refusing it would leave every `--prompt` session stuck.
    """
    record = {
        "name": "SESSION",
        "driver": {
            "id": "codex",
            "launch_argv": CODEX_LAUNCH,
            "resume": {"strategy": "conversation_id", "reference": REFERENCE},
        },
        "launch_prompt": "pick up where we left off",
    }

    with_prompt = Frame.agent_launch_argv(record)
    assert with_prompt == [*CODEX_LAUNCH, "pick up where we left off"], with_prompt

    # ... and once the launch has delivered it and the record has dropped it, the
    # process still running that argv is recognised all the same.
    record.pop("launch_prompt")
    launched_as = Frame.agent_launch_argv(record)
    assert launched_as == CODEX_LAUNCH, launched_as
    assert ConversationHolders.selects_exactly(
        with_prompt, REFERENCE, launched_as=launched_as
    )


@pytestmark_tmux
def test_the_signal_is_withheld_when_the_argv_changed_at_the_last_moment(
    tmp_path, retired_server
):
    """The final re-read is the only thing standing between a gap and a stranger.

    Everything before it — two tmux probes at ten seconds apiece — happens while
    the PID could be reaped and reissued to an unrelated program. So the argv is
    read once more immediately before the signal, with a pidfd already pinning
    the identity, and a mismatch calls the whole thing off.
    """
    _socket, pid = retired_server

    repair, _record = _repair_for(tmp_path)
    sent = repair._stop_and_confirm(pid, "some other program --flag")

    assert sent is False, "a process whose argv no longer matched was signalled"
    assert _alive(pid), "the signal went out despite the identity having changed"


@pytestmark_tmux
def test_a_pane_already_running_a_driver_stops_the_walk(tmp_path, retired_server, monkeypatch):
    """A live agent means nothing is stranded — including a pane with no shell.

    `_has_live_shell_agent` answers only for a pane running a shell that spawned
    a driver. A pane whose root process IS the driver answered False there, so
    the first gate did not hold for that shape.
    """
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    monkeypatch.setattr(
        repair.frame, "agent_pane_command", lambda _record: "claude --resume x"
    )
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "the walk ran although the pane was running a driver"
    assert actions == []


@pytestmark_tmux
def test_the_real_argv_is_read_with_its_boundaries_intact(tmp_path, retired_server):
    """`/proc` keeps arguments separate; `ps` flattens them into one line."""
    _socket, pid = retired_server

    argv = ConversationHolders.argv_of(pid)

    assert argv is not None, "the driver's argv could not be read"
    assert argv[:2] == [sys.executable, str(_fake_driver(tmp_path))], argv
    assert REFERENCE in argv, argv
    assert ConversationHolders.selects_exactly(
        argv, REFERENCE, launched_as=_fake_launch_argv(tmp_path)
    )


@pytest.mark.parametrize(
    "resume_flags",
    [
        # A fork holds the conversation it CREATED, so the id in its argv is the
        # source.
        ["--fork-session", "--resume"],
        # `--continue` and `--last` pick a conversation themselves, so the id is
        # a PROMPT and the process is on something this argv does not name.
        ["--continue"],
        ["-c"],
        ["--last"],
    ],
)
def test_a_configured_launch_that_names_a_conversation_another_way_is_refused(resume_flags):
    """Our record vouches for what we launched, not for how a CLI reads it.

    `resume_argv` is configuration, so a declarative driver can be given any of
    these with the `conversation_id` strategy — and the argv this package then
    generates is an exact match for a process whose live conversation is NOT the
    record's. Agreement with our own launch is normally the whole proof; here it
    would authorize a signal against a conversation nobody asked to release
    (cross-model review, 2026-11-18, rounds 9 and 10).
    """
    def launch_for(resume_argv):
        registry = DriverRegistry()
        registry.add_declarative("claude", {
            "command": ["claude"],
            "resume_strategy": "conversation_id",
            "resume_argv": resume_argv,
        })
        resolved = registry.get("claude").resolve(
            name="SESSION", working_dir="/w", conversation_reference=REFERENCE
        )
        return [str(part) for part in resolved["launch_argv"]]

    generated = launch_for(["claude", *resume_flags])
    assert REFERENCE in generated, generated
    assert not ConversationHolders.selects_exactly(
        generated, REFERENCE, launched_as=generated
    ), "our own launch vouched for a process on another conversation"

    # Positive control: the same driver without those flags still releases, so
    # this cannot be passing because the generated argv stopped matching at all.
    plain = launch_for(["claude", "--resume"])
    assert ConversationHolders.selects_exactly(plain, REFERENCE, launched_as=plain)


@pytest.mark.parametrize(
    "argv, selected",
    [
        # An npm install is a shebang script, so the kernel runs the INTERPRETER
        # with the entrypoint as its first argument. `_is_driver` has always
        # recognised this shape; judging argv[0] alone made the release path
        # inert for every such install (cross-model review, 2026-11-18).
        (["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js",
          *CLAUDE_LAUNCH[1:]], True),
        (["/usr/bin/node", "/opt/n/lib/node_modules/claude-code/cli.js",
          *CLAUDE_LAUNCH[1:]], True),
        # The entrypoint is required: `node <anything> --resume <id>` is not
        # evidence that this is our driver.
        (["node", "/tmp/something-else.js", *CLAUDE_LAUNCH[1:]], False),
        # ... and it must be argv[1], the one place a shebang can put it.
        # Accepting a marker anywhere let an unrelated program carry one in a
        # later argument and have everything before it discarded, while node was
        # really running the first script.
        (["node", "/tmp/task.js", "--resume", OTHER,
          "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js",
          *CLAUDE_LAUNCH[1:]], False),
        (["node", *CLAUDE_LAUNCH[1:]], False),
        # And past the entrypoint the arguments must still be ours, exactly.
        (["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js",
          "--resume", REFERENCE, "--name", "OTHER"], False),
        (["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js",
          *CLAUDE_LAUNCH[1:], "--fork-session"], False),
        # An interpreter this package does not run drivers through.
        (["ruby", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js",
          *CLAUDE_LAUNCH[1:]], False),
    ],
)
def test_an_interpreter_installed_driver_is_the_same_program(argv, selected):
    # The launch guard sees these as drivers, which is what makes the two paths
    # disagreeing a defect rather than a difference of opinion.
    if selected:
        assert ConversationHolders._is_driver(" ".join(argv)), (
            "precondition: the guard recognises this install shape"
        )
    assert ConversationHolders.selects_exactly(
        argv, REFERENCE, launched_as=CLAUDE_LAUNCH
    ) is selected


def test_a_configured_resume_argv_is_not_a_launch_this_package_can_vouch_for(tmp_path):
    """Our record proves what we LAUNCHED. It cannot prove how a CLI reads it.

    `resume_argv` is configuration, and `["codex", "resume", OTHER]` generates
    `codex resume OTHER --no-daemon <id>`, where OTHER is the session and the id
    is merely its prompt. Nothing in that argv distinguishes it from a launch
    that really does select the id — compared against the record it matches
    perfectly — so no amount of comparing can save it, and three review rounds
    produced three variations on it (a fork flag, an implicit selector, and this
    positional). The kill path therefore vouches only for the launch THIS
    PACKAGE defines (cross-model review, 2026-11-18, rounds 9-11).
    """
    repair, record = _repair_for(tmp_path)
    assert repair._launch_this_package_defines(record, REFERENCE), (
        "precondition: a bundled driver's own launch IS vouched for"
    )

    registry = DriverRegistry()
    registry.add_declarative("codex", {
        "command": ["codex"],
        "resume_strategy": "conversation_id",
        "resume_argv": ["codex", "resume", OTHER],
    })
    configured = registry.get("codex").resolve(
        name="SESSION", working_dir="/w", conversation_reference=REFERENCE
    )
    argv = [str(part) for part in configured["launch_argv"]]
    assert argv == ["codex", "resume", OTHER, "--no-daemon", REFERENCE], argv
    # Compared with itself it matches, which is exactly why comparison is not
    # enough on its own ...
    assert ConversationHolders.selects_exactly(argv, REFERENCE, launched_as=argv)

    # ... and the gate is what withholds it: this is not a launch we define.
    record["driver"] = configured
    assert repair._launch_this_package_defines(record, REFERENCE) == []


def test_the_gate_vouches_for_every_bundled_driver_not_just_claude(tmp_path):
    """The record's OWN driver decides what we would have launched.

    A codex session's stranded holder is released on the same terms as a
    claude one — the field case was a claude driver, which is exactly why this
    needs its own assertion rather than a shared one.
    """
    repair, record = _repair_for(tmp_path)
    record["driver"] = bundled_drivers()["codex"].resolve(
        name=str(record.get("name") or ""),
        working_dir=str(record.get("working_dir") or ""),
        conversation_reference=REFERENCE,
    )

    launched_as = repair._launch_this_package_defines(record, REFERENCE)

    assert launched_as, "a bundled codex launch was not vouched for"
    assert "resume" in launched_as and REFERENCE in launched_as, launched_as


def test_the_argv_this_package_actually_launches_codex_with_is_recognised():
    """Pinned against the real generator, not a copy of it.

    The subcommand form was first written from a guess at the shape and rejected
    the one this package emits, so the launch guard saw the holder while the
    release path did not — leaving Codex sessions stuck for good. Deriving the
    argv here means a change to the driver breaks this test instead of silently
    disabling the repair (cross-model review, 2026-11-17).
    """
    argv = _generated_launch("codex")

    assert "resume" in argv, f"the codex launch no longer uses the subcommand: {argv}"
    assert REFERENCE in argv, f"the codex launch no longer names the conversation: {argv}"
    assert ConversationHolders.selects_exactly(argv, REFERENCE, launched_as=argv), (
        f"the argv this package launches is not recognised as selecting: {argv}"
    )
    assert not ConversationHolders.selects_exactly(
        argv, "other-reference-id", launched_as=argv
    )
